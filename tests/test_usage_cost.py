"""Unit tests for per-message chat cost (reason + TTS + STT). No network.

Run with:  python3 -m unittest discover tests
"""

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import voice_module
from voice_module import (DEFAULT_MODEL_COST, PROMPT_PRESETS, STT_COST_PER_M,
                          STT_OUT_COST_PER_M, TTS_MODELS, reason, stt_cost,
                          stt_is_estimate, transcribe, tts_cost, usage_cost,
                          voice_bp)
from flask import Flask, request


class _FakeResp:
    """Minimal stand-in for a requests.Response: only what the callers touch."""

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class TestUsageCost(unittest.TestCase):
    def test_prefers_openrouter_cost_over_table(self):
        usage = {"prompt_tokens": 10_000_000, "completion_tokens": 1_000_000,
                 "cost": 12.34}
        cost, tokens = usage_cost(usage, "x-ai/grok-4.5:online")
        self.assertEqual(cost, 12.34)
        self.assertEqual(tokens, 11_000_000)

    def test_coerces_string_cost(self):
        # Reviewer blocker #1: OpenRouter can return usage.cost as a string.
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 500_000,
                 "cost": "$10.500000"}
        cost, tokens = usage_cost(usage, "x-ai/grok-4.5:online")
        self.assertEqual(tokens, 1_500_000)
        self.assertAlmostEqual(cost, 10.5)

    def test_malformed_cost_never_raises(self):
        # A garbage cost string must not raise — it falls back to table math.
        cost, tokens = usage_cost({"prompt_tokens": 1, "completion_tokens": 1,
                                   "cost": "not-a-number"}, "x")
        self.assertEqual(tokens, 2)
        self.assertIsNotNone(cost)   # token-math fallback used, not a crash

    def test_falls_back_to_table_math_when_cost_missing(self):
        # grok-4.5: in 3.00, out 15.00 -> (1e6/1e6*3) + (0.5e6/1e6*15) = 3 + 7.5
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}
        cost, tokens = usage_cost(usage, "x-ai/grok-4.5:online")
        self.assertEqual(tokens, 1_500_000)
        self.assertAlmostEqual(cost, 10.5)

    def test_none_or_empty_usage_is_unmeasurable(self):
        self.assertEqual(usage_cost(None, "x"), (None, None))
        self.assertEqual(usage_cost({}, "x"), (None, None))

    def test_unknown_model_uses_default_price(self):
        p, o = DEFAULT_MODEL_COST
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
        cost, _ = usage_cost(usage, "totally/unknown-model")
        self.assertAlmostEqual(cost, p + o)   # 1 M @ default prices each

    def test_total_tokens_derived_when_absent(self):
        cost, tokens = usage_cost({"prompt_tokens": 4, "completion_tokens": 6, "cost": 0.1}, "x")
        self.assertEqual(tokens, 10)
        self.assertEqual(cost, 0.1)

    def test_missing_tokens_yield_none_not_zero(self):
        # Reviewer blocker #2: no real token count -> total is None, never "0 tokens".
        cost, tokens = usage_cost({"total_tokens": 100}, "x")
        self.assertEqual((cost, tokens), (None, None))


class TestSttCost(unittest.TestCase):
    def test_prefers_usage_cost(self):
        self.assertEqual(stt_cost({"cost": 0.0042}), 0.0042)

    def test_string_cost_coerced(self):
        self.assertEqual(stt_cost({"cost": "$0.004200"}), 0.0042)

    def test_falls_back_to_measured_prices(self):
        # gpt-4o-transcribe measured: 2.50/M in, 10.00/M out.
        # 1M in -> 2.50; plus 1M out -> 10.00.
        self.assertAlmostEqual(
            stt_cost({"input_tokens": 1_000_000, "output_tokens": 1_000_000}), 12.5)
        self.assertAlmostEqual(stt_cost({"input_tokens": 1_000_000}), 2.5)

    def test_missing_usage_is_none(self):
        self.assertIsNone(stt_cost(None))
        self.assertIsNone(stt_cost({}))
        self.assertIsNone(stt_cost({"input_tokens": "nope"}))


class TestSttIsEstimate(unittest.TestCase):
    """stt_est must mean 'the figure is genuinely an estimate', i.e. exact
    usage.cost was absent — never 'which helper ran' (spec v2 §3.2)."""

    def test_exact_when_usage_cost_present(self):
        self.assertFalse(stt_is_estimate({"cost": 0.000123, "input_tokens": 10}))
        self.assertFalse(stt_is_estimate({"cost": "$0.000123"}))

    def test_estimate_when_cost_absent(self):
        self.assertTrue(stt_is_estimate({"input_tokens": 10, "output_tokens": 3}))
        self.assertTrue(stt_is_estimate(None))
        self.assertTrue(stt_is_estimate({}))


class TestTtsCost(unittest.TestCase):
    def test_chars_times_price(self):
        cfg = {"cost_per_char": 0.0000175}
        self.assertAlmostEqual(tts_cost(1000, cfg), 0.0175)

    def test_no_price_is_none(self):
        self.assertIsNone(tts_cost(1000, {"voice": "x"}))


class TestMeasuredPrices(unittest.TestCase):
    """Spec v2 §3.1: real unit prices measured from billed OpenRouter requests
    replace the old placeholder sentinels (STT 4.20, TTS 1.75e-05)."""

    def test_stt_prices_are_measured(self):
        self.assertNotEqual(STT_COST_PER_M, 4.20)          # old placeholder
        self.assertEqual(STT_COST_PER_M, 2.50)             # measured in/M
        self.assertEqual(STT_OUT_COST_PER_M, 10.00)        # measured out/M

    def test_tts_prices_are_measured_per_model(self):
        by_id = {m["id"]: m for m in TTS_MODELS}
        for mid, expected in [
            ("google/gemini-3.1-flash-tts-preview", 0.00003276),
            ("x-ai/grok-voice-tts-1.0", 0.00001500),
            ("hexgrad/kokoro-82m", 0.00000062),
        ]:
            cpc = by_id[mid]["cost_per_char"]
            self.assertAlmostEqual(cpc, expected)
            self.assertNotEqual(cpc, 0.0000175)            # old placeholder


class TestReasonAndTranscribeTuples(unittest.TestCase):
    def test_reason_returns_answer_and_usage(self):
        fake = {
            "choices": [{"message": {"content": "  Hello world.  "}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7,
                      "total_tokens": 12, "cost": 0.000123},
        }
        with mock.patch("voice_module.requests.post", return_value=_FakeResp(fake)):
            answer, usage = reason("Q", "", "")
        self.assertEqual(answer, "Hello world.")
        self.assertEqual(usage["cost"], 0.000123)
        self.assertEqual(usage["total_tokens"], 12)

    def test_reason_returns_none_usage_when_absent(self):
        fake = {"choices": [{"message": {"content": "Hello"}}]}
        with mock.patch("voice_module.requests.post", return_value=_FakeResp(fake)):
            answer, usage = reason("Q", "", "")
        self.assertEqual(answer, "Hello")
        self.assertIsNone(usage)

    def test_transcribe_returns_text_and_usage(self):
        fake = {"text": "  How are margins trending?  ",
                "usage": {"prompt_tokens": 30, "cost": 0.0001}}
        with mock.patch("voice_module.requests.post", return_value=_FakeResp(fake)):
            text, usage = transcribe("b64", "webm")
        self.assertEqual(text, "How are margins trending?")
        self.assertAlmostEqual(usage["cost"], 0.0001)


class TestSynthesizeAudioCachedFlag(unittest.TestCase):
    """synthesize_audio_cached returns a synthesized flag: True only when a
    fresh synthesis ran, False on an S3 cache hit (a free replay)."""

    class _FakeBody:
        def read(self):
            return b"cached-audio-bytes"

    def test_cache_hit_is_false(self):
        client = mock.MagicMock()
        client.get_object.return_value = {"Body": self._FakeBody()}
        with mock.patch.object(voice_module, "VOICE_S3_BUCKET", "bucket"), \
             mock.patch.object(voice_module, "_s3", return_value=client):
            data, mime, synthesized = voice_module.synthesize_audio_cached("text")
        self.assertEqual(data, b"cached-audio-bytes")
        self.assertFalse(synthesized)

    def test_fresh_synthesis_is_true(self):
        client = mock.MagicMock()
        client.get_object.side_effect = Exception("miss")
        with mock.patch.object(voice_module, "VOICE_S3_BUCKET", "bucket"), \
             mock.patch.object(voice_module, "_s3", return_value=client), \
             mock.patch.object(voice_module, "synthesize_audio",
                               return_value=(b"new-audio", "audio/mpeg", None)):
            data, _mime, synthesized = voice_module.synthesize_audio_cached("text")
        self.assertEqual(data, b"new-audio")
        self.assertTrue(synthesized)


class TestVoicePageRendersPresets(unittest.TestCase):
    """The Chat page renders the prompt-preset picker from the server allowlist,
    so UI and server can't drift (mirrors how the model dropdowns render)."""

    @classmethod
    def setUpClass(cls):
        repo = os.path.join(os.path.dirname(__file__), "..")
        app = Flask(__name__,
                    template_folder=os.path.join(repo, "templates"),
                    static_folder=os.path.join(repo, "static"))
        app.register_blueprint(voice_bp)

        @app.context_processor
        def _asset_version():
            return {"asset_v": "test"}

        cls.client = app.test_client()

    def test_voice_renders_preset_select_from_allowlist(self):
        resp = self.client.get("/voice")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('id="promptPreset"', html)
        self.assertIn('id="promptPresetsData"', html)
        for p in PROMPT_PRESETS:
            self.assertIn(f'value="{p["id"]}"', html)   # option value
            self.assertIn(p["label"], html)             # visible label
        self.assertIn("Prompt…", html)                  # idle placeholder

    def test_preset_option_ids_match_allowlist_exactly(self):
        resp = self.client.get("/voice")
        html = resp.get_data(as_text=True)
        # Scope to the promptPreset <select> block so model/tts dropdowns don't count.
        block = html.split('id="promptPreset"', 1)[1]
        block = block.split("</select>", 1)[0]
        option_ids = re.findall(r'<option value="([^"]*)"', block)
        # Idle placeholder + one option per allowlist entry, exactly.
        self.assertEqual(option_ids, [""] + [p["id"] for p in PROMPT_PRESETS])


class TestTtsOffToggle(unittest.TestCase):
    """Per-message TTS-off toggle: tts_enabled=0 skips TTS + audio in payload."""

    def _run_ask(self, tts_enabled, synth_result):
        captured = {}
        with mock.patch.object(voice_module, "_VOICE_SEMA",
                               mock.MagicMock()), \
             mock.patch.object(voice_module, "reason",
                               return_value=("answer", {"cost": 1})), \
             mock.patch.object(voice_module, "synthesize_audio_cached",
                               return_value=synth_result) as synth, \
             mock.patch.object(voice_module, "usage_cost",
                               return_value=(1.0, 100)), \
             mock.patch.object(voice_module, "stt_cost", return_value=None), \
             mock.patch.object(voice_module, "stt_is_estimate",
                               return_value=True), \
             mock.patch.object(voice_module, "_finish_job",
                               side_effect=lambda jid, payload, status:
                               captured.update(payload=payload, status=status)):
            voice_module._run_ask(
                "job1", "my question", None, None, None, [], "AAA", [],
                "x-ai/grok-4.5:online", conv_id=None, tts_cfg=TTS_MODELS[0],
                tts_enabled=tts_enabled)
        return captured, synth

    def test_disabled_skips_tts_and_omits_audio(self):
        captured, synth = self._run_ask(False, (b"audio", "audio/mpeg", True))
        synth.assert_not_called()
        payload = captured["payload"]
        self.assertNotIn("audio", payload)
        self.assertIsNone(payload["voice_cost"])
        self.assertEqual(payload["voice_chars"], 0)

    def test_enabled_calls_tts_and_includes_audio(self):
        captured, synth = self._run_ask(True, (b"audio", "audio/mpeg", True))
        synth.assert_called_once()
        payload = captured["payload"]
        self.assertIn("audio", payload)
        self.assertIsNotNone(payload["voice_cost"])

    def test_parser_lenient(self):
        # Mirrors the exact expression in voice_ask: "0" -> off; anything else
        # (or missing) -> on. Never raises on a garbage value.
        def parse(form):
            app = Flask(__name__)
            with app.test_request_context("/api/voice/ask", method="POST",
                                          data=form):
                return (request.form.get("tts_enabled") or "1") != "0"
        self.assertFalse(parse({"tts_enabled": "0"}))
        self.assertTrue(parse({}))                       # absent
        self.assertTrue(parse({"tts_enabled": "1"}))     # on
        self.assertTrue(parse({"tts_enabled": "garbage"}))  # lenient


if __name__ == "__main__":
    unittest.main()
