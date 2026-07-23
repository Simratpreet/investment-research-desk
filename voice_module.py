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
import os
import re
import wave
from pathlib import Path

HISTORY_TURNS = 12  # cap prior turns fed back (6 Q&A pairs) to bound token growth

import requests
from flask import Blueprint, jsonify, render_template, request

OR_URL = "https://openrouter.ai/api/v1"
STT_MODEL = "openai/gpt-4o-transcribe"
# REASON_MODEL = "google/gemini-3.1-pro-preview:online"
REASON_MODEL = "openai/gpt-5.6-sol:online"
TTS_MODEL = "google/gemini-3.1-flash-tts-preview"
TTS_VOICE = "Charon"          # steady "informative" narrator
TTS_PCM_RATE = 24000
TTS_CHAR_LIMIT = 1000         # ~85s/chunk — safely under Gemini TTS's ~120s cap
TTS_WORKERS = 6              # synthesize answer chunks in parallel
MAX_CONTEXT_CHARS = 800_000   # generous; gemini-3.6-flash has a 1M-token window

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
        "spoken prose, and say numbers naturally (e.g. 'twelve point nine percent').\n\n"
        f"===== FILINGS FOR {symbol.upper()} =====\n{context}"
    )
    messages = [{"role": "system", "content": system}]
    for turn in (history or []):
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
    return resp.json()["choices"][0]["message"]["content"].strip()


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
    resp = requests.post(
        f"{OR_URL}/audio/speech",
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
        json={"model": TTS_MODEL, "input": text, "voice": TTS_VOICE, "response_format": "pcm"},
        timeout=180,
    )
    resp.raise_for_status()
    return resp.content


def synthesize_wav(text: str) -> bytes:
    """Chunk the answer, synthesize the pieces in parallel, concat PCM to one WAV."""
    import concurrent.futures

    cleaned = re.sub(r"[\[\]*`#]", " ", text)  # strip markup TTS reads oddly
    chunks = _chunk(cleaned, TTS_CHAR_LIMIT)
    if len(chunks) == 1:
        pcm = _tts_pcm(chunks[0])
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=TTS_WORKERS) as ex:
            pcm = b"".join(ex.map(_tts_pcm, chunks))  # ex.map preserves order
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
    symbol = (request.form.get("symbol") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", symbol):
        return jsonify({"error": "enter a valid symbol"}), 400

    typed = (request.form.get("text") or "").strip()
    audio = request.files.get("audio")
    if not typed and not audio:
        return jsonify({"error": "no question — type or record one"}), 400

    context, sources = load_context(symbol)
    if not context:
        return jsonify({"error": f"no .txt filings found for {symbol.upper()} "
                                 "(download them first via the bot's /screener or /global)"}), 404

    audio_b64 = fmt = None
    if not typed:
        fmt = (audio.filename.rsplit(".", 1)[-1] or "webm").lower()
        if fmt not in {"webm", "wav", "mp3", "m4a", "ogg", "flac", "aac"}:
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

    try:
        if typed:
            question = typed
        else:
            question = transcribe(audio_b64, fmt)
            if not question:
                return jsonify({"error": "couldn't understand the audio — try again"}), 422
        answer = reason(question, context, symbol, history)
        wav = synthesize_wav(answer)
    except requests.HTTPError as e:
        body = e.response.text[:300] if e.response is not None else str(e)
        return jsonify({"error": f"upstream error: {body}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "symbol": symbol.upper(),
        "sources": sources,
        "question": question,
        "answer": answer,
        "audio": "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii"),
    })
