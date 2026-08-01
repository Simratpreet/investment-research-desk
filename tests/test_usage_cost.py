"""Unit tests for per-message chat cost (reason + TTS + STT). No network.

Run with:  python3 -m unittest discover tests
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import voice_module
from voice_module import (DEFAULT_MODEL_COST, reason, stt_cost, transcribe,
                          tts_cost, usage_cost)


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

    def test_falls_back_to_token_math(self):
        # gpt-4o-transcribe: 4.20 per 1M prompt tokens; 1M -> 4.2
        self.assertAlmostEqual(stt_cost({"prompt_tokens": 1_000_000}), 4.2)

    def test_missing_usage_is_none(self):
        self.assertIsNone(stt_cost(None))
        self.assertIsNone(stt_cost({}))
        self.assertIsNone(stt_cost({"prompt_tokens": "nope"}))


class TestTtsCost(unittest.TestCase):
    def test_chars_times_price(self):
        cfg = {"cost_per_char": 0.0000175}
        self.assertAlmostEqual(tts_cost(1000, cfg), 0.0175)

    def test_no_price_is_none(self):
        self.assertIsNone(tts_cost(1000, {"voice": "x"}))


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


if __name__ == "__main__":
    unittest.main()
