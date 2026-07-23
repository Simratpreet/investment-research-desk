"""Voice research module — ask a company's filings out loud, hear the answer.

Pipeline (all via OpenRouter, one key):
  mic audio --> gpt-4o-transcribe (STT)
            --> gemini-3.6-flash, high reasoning, grounded in local .txt filings
            --> gemini-3.1 TTS --> spoken answer

Context lives locally, one company at a time, from:
  screener_scraper/Q4FY26/downloads/<SYMBOL>/*.txt
  fiscal-agent/downloads/<SYMBOL>/*.txt

Exposed as a Flask blueprint: GET /voice (page), POST /api/voice/ask.
"""

import base64
import glob
import io
import json
import logging
import os
import random
import re
import threading
import time
import wave
from pathlib import Path

log = logging.getLogger(__name__)

HISTORY_TURNS = 12  # cap prior turns fed back (6 Q&A pairs) to bound token growth

import requests
from flask import Blueprint, jsonify, render_template, request

from security import client_ip, rate_limit_ok

# Bound concurrent expensive pipelines (STT + high-effort reasoning + parallel
# TTS) so a burst can't fan out unbounded OpenRouter spend / memory. Per-worker.
_VOICE_SEMA = threading.BoundedSemaphore(2)

OR_URL = "https://openrouter.ai/api/v1"
STT_MODEL = "openai/gpt-4o-transcribe"
# REASON_MODEL = "google/gemini-3.1-pro-preview:online"
REASON_MODEL = "x-ai/grok-4.5:online"
TTS_MODEL = "google/gemini-3.1-flash-tts-preview"
TTS_VOICE = "Charon"          # steady "informative" narrator
TTS_PCM_RATE = 24000
TTS_CHAR_LIMIT = 1000         # ~85s/chunk — safely under Gemini TTS's ~120s cap
TTS_WORKERS = 6              # synthesize answer chunks in parallel
MAX_CONTEXT_CHARS = 800_000   # generous; gemini-3.6-flash has a 1M-token window
MAX_TYPED_CHARS = 2000        # a typed question longer than this is abuse, not a question
TTS_RETRIES = 3               # attempts per chunk before giving up on that chunk

CONTEXT_DIRS = [
    Path("/Users/simrat/Desktop/screener_scraper/Q4FY26/downloads"),
    Path("/Users/simrat/Desktop/fiscal-agent/downloads"),
]

voice_bp = Blueprint("voice", __name__)


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:  # fall back to the sibling .env
        envp = Path(__file__).resolve().parent / ".env"
        if envp.exists():
            for line in envp.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("'\"")
                    break
    return key


def load_context(symbol: str):
    """Read all .txt filings for a symbol from both download trees."""
    sym = symbol.strip().upper()
    parts, names = [], []
    total = 0
    for base in CONTEXT_DIRS:
        for path in sorted(glob.glob(str(base / sym / "*.txt"))):
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not text.strip():
                continue
            label = f"{Path(path).parent.parent.name}/{sym}/{Path(path).name}"
            parts.append(f"===== {Path(path).name} =====\n{text}")
            names.append(label)
            total += len(text)
    joined = "\n\n".join(parts)[:MAX_CONTEXT_CHARS]
    return joined, names


def transcribe(audio_b64: str, fmt: str) -> str:
    resp = requests.post(
        f"{OR_URL}/audio/transcriptions",
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
        json={
            "model": STT_MODEL,
            "input_audio": {"data": audio_b64, "format": fmt},
            "language": "en",
        },
        timeout=120,
    )
    resp.raise_for_status()
    return (resp.json().get("text") or "").strip()


def reason(question: str, context: str, symbol: str, history=None) -> str:
    system = (
        f"You are an expert equity-research analyst answering a spoken question about {symbol.upper()}. "
        "Answer from the company filings provided below — quarterly transcripts, slides and reports. Use web search if asked"
        "Be precise and specific with numbers, guidance and management commentary; cite the quarter when relevant. "
        "If the answer is not in any filings or web search, say so plainly rather than guessing "
        "This is an ongoing spoken conversation: resolve follow-up references like 'that', 'those', "
        "'the same quarter' or 'and the margin?' against the earlier turns below. "
        "Keep the answer tight and conversational since it will be read aloud. "
        "Do NOT use markdown, bullet points, headers or asterisks — reply in flowing "
        "spoken prose.\n\n"
        f"===== FILINGS FOR {symbol.upper()} =====\n{context}"
    )
    messages = [{"role": "system", "content": system}]
    for turn in (history or []):
        if not isinstance(turn, dict):
            continue  # malformed history element (e.g. [1, null]) — don't crash on .get
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    resp = requests.post(
        f"{OR_URL}/chat/completions",
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
        json={
            "model": REASON_MODEL,
            "reasoning": {"effort": "high"},
            "messages": messages,
        },
        timeout=300,
    )
    resp.raise_for_status()
    # content can be null on a refusal or reasoning-only turn — coalesce, don't .strip(None).
    msg = (resp.json().get("choices") or [{}])[0].get("message") or {}
    return (msg.get("content") or "").strip()


def _chunk(text: str, limit: int):
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    chunks, cur = [], ""
    for p in parts:
        if not p:
            continue
        while len(p) > limit:
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(p[:limit]); p = p[limit:]
        if len(cur) + len(p) + 1 <= limit:
            cur = f"{cur} {p}".strip()
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks


def _tts_pcm(text: str) -> bytes:
    """Synthesize one chunk to raw PCM, retrying transient failures with backoff.

    Guards against the two silent-corruption modes: a 429/5xx (retry) and a 200
    whose body is empty or a JSON error blob (which, written as PCM, becomes noise).
    """
    last_err = None
    for attempt in range(TTS_RETRIES):
        try:
            resp = requests.post(
                f"{OR_URL}/audio/speech",
                headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
                json={"model": TTS_MODEL, "input": text, "voice": TTS_VOICE, "response_format": "pcm"},
                timeout=180,
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"TTS status {resp.status_code}", response=resp)
            resp.raise_for_status()
            pcm = resp.content
            ctype = resp.headers.get("Content-Type", "").lower()
            if not pcm or "json" in ctype or "text" in ctype:
                raise ValueError(f"empty or non-PCM TTS body (ctype={ctype!r}, len={len(pcm)})")
            return pcm
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < TTS_RETRIES - 1:
                time.sleep((2 ** attempt) * 0.5 + random.random() * 0.3)  # exp backoff + jitter
    raise last_err


def synthesize_wav(text: str) -> bytes:
    """Chunk the answer, synthesize the pieces in parallel, concat PCM to one WAV."""
    import concurrent.futures

    cleaned = re.sub(r"[\[\]*`#]", " ", text)  # strip markup TTS reads oddly
    chunks = _chunk(cleaned, TTS_CHAR_LIMIT)
    if not chunks:
        raise ValueError("nothing to synthesize")
    if len(chunks) == 1:
        pcm = _tts_pcm(chunks[0])
    else:
        # Synthesize in parallel but isolate failures: a chunk that fails after
        # retries drops to b"" (a small gap) instead of discarding the other five.
        pieces = [b""] * len(chunks)
        with concurrent.futures.ThreadPoolExecutor(max_workers=TTS_WORKERS) as ex:
            futs = {ex.submit(_tts_pcm, c): i for i, c in enumerate(chunks)}
            for fut in concurrent.futures.as_completed(futs):
                i = futs[fut]
                try:
                    pieces[i] = fut.result()
                except Exception as e:
                    log.warning("TTS chunk %d/%d failed, dropping: %s", i + 1, len(chunks), e)
        pcm = b"".join(pieces)  # index order preserved regardless of completion order
        if not pcm:
            raise RuntimeError("all TTS chunks failed")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TTS_PCM_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


@voice_bp.route("/voice")
def voice_page():
    return render_template("voice.html")


@voice_bp.route("/api/voice/ask", methods=["POST"])
def voice_ask():
    if not rate_limit_ok(f"voice:{client_ip(request)}", 20, 60):
        return jsonify({"error": "Too many requests — slow down a moment."}), 429

    symbol = (request.form.get("symbol") or "").strip()
    # Must start alnum and contain no "..", so it can't traverse out of a download dir.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,19}", symbol) or ".." in symbol:
        return jsonify({"error": "enter a valid symbol"}), 400

    typed = (request.form.get("text") or "").strip()
    if len(typed) > MAX_TYPED_CHARS:
        return jsonify({"error": "question too long"}), 400
    audio = request.files.get("audio")
    if not typed and not audio:
        return jsonify({"error": "no question — type or record one"}), 400

    context, sources = load_context(symbol)
    if not context:
        return jsonify({"error": f"no .txt filings found for {symbol.upper()} "
                                 "(download them first via the bot's /screener or /global)"}), 404

    audio_b64 = fmt = None
    if not typed:
        fmt = ((audio.filename or "").rsplit(".", 1)[-1] or "webm").lower()
        if fmt not in {"webm", "wav", "mp3", "mp4", "m4a", "ogg", "flac", "aac"}:
            fmt = "webm"
        audio_b64 = base64.b64encode(audio.read()).decode("ascii")

    history = []
    raw_hist = request.form.get("history")
    if raw_hist:
        try:
            parsed = json.loads(raw_hist)
            if isinstance(parsed, list):
                history = parsed[-HISTORY_TURNS:]
        except (ValueError, TypeError):
            history = []

    if not _VOICE_SEMA.acquire(blocking=False):
        return jsonify({"error": "Server busy with another request — "
                                 "try again in a moment."}), 429
    try:
        try:
            if typed:
                question = typed
            else:
                question = transcribe(audio_b64, fmt)
                if not question:
                    return jsonify({"error": "couldn't understand the audio — try again"}), 422
            answer = reason(question, context, symbol, history)
            if not answer:
                return jsonify({"error": "the model returned an empty answer — try rephrasing"}), 502
            wav = synthesize_wav(answer)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            log.warning("voice upstream error (status=%s): %s", status, e)
            return jsonify({"error": "upstream service error — try again in a moment."}), 502
        except Exception as e:
            log.exception("voice pipeline error: %s", e)
            return jsonify({"error": "internal error while processing your question"}), 500

        return jsonify({
            "symbol": symbol.upper(),
            "sources": sources,
            "question": question,
            "answer": answer,
            "audio": "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii"),
        })
    finally:
        _VOICE_SEMA.release()
