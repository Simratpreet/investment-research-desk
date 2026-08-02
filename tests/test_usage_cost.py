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
    """synthesize_audio_cached returns (audio, mime, synthesized, stored):
    synthesized True only when a fresh synthesis ran; stored True only when the
    render is stably in the S3 cache (a hit or a successful PUT)."""

    class _FakeBody:
        def read(self):
            return b"cached-audio-bytes"

    def test_cache_hit_is_false_synthesized_true_stored(self):
        client = mock.MagicMock()
        client.get_object.return_value = {"Body": self._FakeBody()}
        with mock.patch.object(voice_module, "VOICE_S3_BUCKET", "bucket"), \
             mock.patch.object(voice_module, "_s3", return_value=client):
            data, mime, synthesized, stored = voice_module.synthesize_audio_cached("text")
        self.assertEqual(data, b"cached-audio-bytes")
        self.assertEqual(mime, "audio/wav")
        self.assertFalse(synthesized)
        self.assertTrue(stored)

    def test_fresh_synthesis_is_true_and_stored(self):
        client = mock.MagicMock()
        client.get_object.side_effect = Exception("miss")
        with mock.patch.object(voice_module, "VOICE_S3_BUCKET", "bucket"), \
             mock.patch.object(voice_module, "_s3", return_value=client), \
             mock.patch.object(voice_module, "synthesize_audio",
                               return_value=(b"new-audio", "audio/mpeg", None)):
            data, _mime, synthesized, stored = voice_module.synthesize_audio_cached("text")
        self.assertEqual(data, b"new-audio")
        self.assertTrue(synthesized)
        self.assertTrue(stored)
        client.put_object.assert_called_once()

    def test_write_failure_is_not_stored(self):
        client = mock.MagicMock()
        client.get_object.side_effect = Exception("miss")
        client.put_object.side_effect = Exception("write failed")
        with mock.patch.object(voice_module, "VOICE_S3_BUCKET", "bucket"), \
             mock.patch.object(voice_module, "_s3", return_value=client), \
             mock.patch.object(voice_module, "synthesize_audio",
                               return_value=(b"new-audio", "audio/mpeg", None)):
            data, _mime, synthesized, stored = voice_module.synthesize_audio_cached("text")
        self.assertEqual(data, b"new-audio")     # still delivers the render
        self.assertTrue(synthesized)
        self.assertFalse(stored)                  # but not a stable S3 copy

    def test_no_bucket_is_not_stored(self):
        with mock.patch.object(voice_module, "VOICE_S3_BUCKET", ""), \
             mock.patch.object(voice_module, "synthesize_audio",
                               return_value=(b"audio", "audio/mpeg", None)):
            _data, _mime, synthesized, stored = voice_module.synthesize_audio_cached("text")
        self.assertTrue(synthesized)
        self.assertFalse(stored)


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
        captured, synth = self._run_ask(False, (b"audio", "audio/mpeg", True, True))
        synth.assert_not_called()
        payload = captured["payload"]
        self.assertNotIn("audio", payload)
        self.assertIsNone(payload["voice_cost"])
        self.assertEqual(payload["voice_chars"], 0)

    def test_enabled_calls_tts_and_includes_audio(self):
        captured, synth = self._run_ask(True, (b"audio", "audio/mpeg", True, True))
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


class TestMp3Chunking(unittest.TestCase):
    """MP3 path now chunks like the PCM path so long Kokoro/Grok answers stay
    under the provider's per-request length cap (a single unchunked long
    request 400'd deterministically). Uses the existing per-chunk transient
    retry inside _tts_request; must never hard-code the limit itself."""

    @staticmethod
    def _mp3_cfg():
        return next(c for c in voice_module.TTS_MODELS
                    if c["pipeline"] == "mp3")

    def test_mp3_long_text_chunks_under_limit(self):
        # Long text -> multiple _tts_request calls, each with a sub-limit
        # input; output is the byte-concatenation, Xing-repaired once.
        mp3 = self._mp3_cfg()
        limit = voice_module.TTS_CHAR_LIMIT
        long_text = ("The quick brown fox jumps over the lazy dog. " * limit)
        inputs, calls = [], []
        with mock.patch.object(voice_module, "_tts_request",
                               side_effect=lambda t, cfg, fmt:
                               (inputs.append(t), calls.append((cfg, fmt)),
                                b"SEG")[2]), \
             mock.patch.object(voice_module, "repair_xing_header",
                               side_effect=lambda b: b):
            out = voice_module._synthesize_mp3(long_text, mp3)
        # More than one chunk was synthesized.
        self.assertGreater(len(inputs), 1)
        # Every chunk request stayed under the provider length cap.
        for chunk in inputs:
            self.assertLessEqual(len(chunk), limit)
        # Each request used the mp3 format and the same model config.
        for cfg, fmt in calls:
            self.assertEqual(cfg, mp3)
            self.assertEqual(fmt, "mp3")
        # Output is the byte-concatenation of the chunk renderings.
        self.assertEqual(out, b"SEG" * len(inputs))

    def test_mp3_short_text_single_request(self):
        # A short answer (< TTS_CHAR_LIMIT) is a single request, unchanged.
        mp3 = self._mp3_cfg()
        short = "A short answer."
        calls = []
        with mock.patch.object(voice_module, "_tts_request",
                               side_effect=lambda t, cfg, fmt:
                               (calls.append((t, fmt)), b"ONE")[1]), \
             mock.patch.object(voice_module, "repair_xing_header",
                               side_effect=lambda b: b):
            out = voice_module._synthesize_mp3(short, mp3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], short)
        self.assertEqual(calls[0][1], "mp3")
        self.assertEqual(out, b"ONE")

    def test_mp3_empty_raises(self):
        with self.assertRaises(ValueError):
            voice_module._synthesize_mp3("   ", self._mp3_cfg())

    def test_synthesize_audio_routes_mp3_to_chunked_path(self):
        mp3 = self._mp3_cfg()
        with mock.patch.object(voice_module, "_synthesize_mp3",
                               return_value=b"OUT") as sm:
            audio, mime, ext = voice_module.synthesize_audio("hello", mp3)
        sm.assert_called_once_with("hello", mp3)
        self.assertEqual((audio, mime, ext), (b"OUT", "audio/mpeg", "mp3"))

    def test_pcm_path_unaffected(self):
        # Regression guard: the mp3 change must not disturb the PCM/Gemini path.
        pcm = voice_module.TTS_MODELS[0]
        self.assertEqual(pcm["pipeline"], "pcm")
        with mock.patch.object(voice_module, "_synthesize_pcm_wav",
                               return_value=b"WAV") as pw:
            audio, mime, ext = voice_module.synthesize_audio("hi", pcm)
        pw.assert_called_once_with("hi")
        self.assertEqual((audio, mime, ext), (b"WAV", "audio/wav", "wav"))


class _ReplayBase(unittest.TestCase):
    """Shared blueprint test client for the audio-replay route tests. The
    app-wide /api auth gate is out of scope here (owned by server.py)."""

    @classmethod
    def setUpClass(cls):
        app = Flask(__name__)
        app.register_blueprint(voice_bp)
        cls.client = app.test_client()


class TestPersistAudioKey(unittest.TestCase):
    """Change A: _run_ask records audio_key only when the clip is stably stored
    (revya's stored-guard), so a failed S3 write never persists a 404-ing key."""

    def _run_ask(self, synth_result):
        convs = mock.MagicMock()
        with mock.patch.object(voice_module, "_VOICE_SEMA", mock.MagicMock()), \
             mock.patch.object(voice_module, "conversations", convs), \
             mock.patch.object(voice_module, "reason",
                               return_value=("answer", {"cost": 1})), \
             mock.patch.object(voice_module, "synthesize_audio_cached",
                               return_value=synth_result), \
             mock.patch.object(voice_module, "usage_cost",
                               return_value=(1.0, 100)), \
             mock.patch.object(voice_module, "stt_cost", return_value=None), \
             mock.patch.object(voice_module, "stt_is_estimate",
                               return_value=True), \
             mock.patch.object(voice_module, "_finish_job"):
            voice_module._run_ask(
                "job1", "my question", None, None, None, [], "AAA", [],
                "x-ai/grok-4.5:online", conv_id="c" * 32, tts_cfg=TTS_MODELS[0],
                tts_enabled=True)
        return convs

    def test_records_key_when_stored(self):
        convs = self._run_ask((b"audio", "audio/mpeg", True, True))
        turn = convs.append_turn.call_args.args[1]
        cfg = TTS_MODELS[0]
        expected = voice_module._tts_cache_key(
            "answer", cfg, "wav" if cfg["pipeline"] == "pcm" else "mp3")
        self.assertEqual(turn["audio_key"], expected)

    def test_omits_key_when_write_failed(self):
        convs = self._run_ask((b"audio", "audio/mpeg", True, False))
        turn = convs.append_turn.call_args.args[1]
        self.assertNotIn("audio_key", turn)


class TestVoiceAudioRoute(_ReplayBase):
    """Change B: /api/voice/audio/<key> streams only valid tts-cache objects,
    never arbitrary keys (filings share the bucket — must not leak)."""

    class _Body:
        def read(self):
            return b"MP3DATA"

    def test_streams_valid_mp3(self):
        client = mock.MagicMock()
        client.get_object.return_value = {"Body": self._Body()}
        with mock.patch.object(voice_module, "VOICE_S3_BUCKET", "bucket"), \
             mock.patch.object(voice_module, "_s3", return_value=client):
            r = self.client.get("/api/voice/audio/tts-cache/" + "a" * 64 + ".mp3")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_data(), b"MP3DATA")
        self.assertEqual(r.mimetype, "audio/mpeg")

    def test_streams_wav_content_type(self):
        client = mock.MagicMock()
        client.get_object.return_value = {"Body": self._Body()}
        with mock.patch.object(voice_module, "VOICE_S3_BUCKET", "bucket"), \
             mock.patch.object(voice_module, "_s3", return_value=client):
            r = self.client.get("/api/voice/audio/tts-cache/" + "a" * 64 + ".wav")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "audio/wav")

    def test_missing_object_404(self):
        client = mock.MagicMock()
        client.get_object.side_effect = Exception("nope")
        with mock.patch.object(voice_module, "VOICE_S3_BUCKET", "bucket"), \
             mock.patch.object(voice_module, "_s3", return_value=client):
            r = self.client.get("/api/voice/audio/tts-cache/" + "b" * 64 + ".mp3")
        self.assertEqual(r.status_code, 404)

    def test_non_tts_cache_keys_rejected_400(self):
        client = mock.MagicMock()
        with mock.patch.object(voice_module, "VOICE_S3_BUCKET", "bucket"), \
             mock.patch.object(voice_module, "_s3", return_value=client):
            for bad in ["AAPL/2026-08-01.x.txt",        # a filing, same bucket
                        "tts-cache/../../etc/passwd",   # traversal attempt
                        "tts-cache/" + "Z" * 64 + ".mp3",  # non-hex digest
                        "tts-cache/" + "a" * 64 + ".txt"]:  # wrong extension
                r = self.client.get("/api/voice/audio/" + bad)
                self.assertEqual(r.status_code, 400, bad)
        client.get_object.assert_not_called()   # never touched S3 on a bad key

    def test_404_when_bucket_unset(self):
        with mock.patch.object(voice_module, "VOICE_S3_BUCKET", ""):
            r = self.client.get("/api/voice/audio/tts-cache/" + "c" * 64 + ".mp3")
        self.assertEqual(r.status_code, 404)


class TestConversationAudioUrl(_ReplayBase):
    """Change C: get_conversation enriches turns with audio_url from audio_key."""

    def test_enriches_turn_with_audio_url(self):
        key = "tts-cache/" + "a" * 64 + ".mp3"
        convs = mock.MagicMock()
        convs.get.return_value = {"id": "x", "turns": [
            {"question": "q1", "answer": "a1", "audio_key": key},
            {"question": "q2", "answer": "a2"},
        ]}
        with mock.patch.object(voice_module, "conversations", convs):
            r = self.client.get("/api/voice/conversations/cid")
        self.assertEqual(r.status_code, 200)
        turns = r.get_json()["turns"]
        self.assertEqual(turns[0]["audio_url"], "/api/voice/audio/" + key)
        # Back-compat seam (revya SA426): absent, never a stub empty string.
        self.assertNotIn("audio_url", turns[1])

    def test_not_found_404(self):
        convs = mock.MagicMock()
        convs.get.return_value = None
        with mock.patch.object(voice_module, "conversations", convs):
            r = self.client.get("/api/voice/conversations/cid")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
