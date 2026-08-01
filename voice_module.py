"""Voice research module — ask a company's filings out loud, hear the answer.

Pipeline (all via OpenRouter, one key):
  mic audio --> gpt-4o-transcribe (STT)
            --> gemini-3.6-flash, high reasoning, grounded in local .txt filings
            --> gemini-3.1 TTS --> spoken answer

Context comes from up to three places, any combination of which may be empty:
  * S3 <SYMBOL>/*.txt when VOICE_S3_BUCKET is set (cloud), else the local
    download trees (screener_scraper/…, fiscal-agent/… — dev Mac only);
  * documents the user uploads (.txt/.md/.pdf), stored as extracted text under
    DATA_DIR/uploads and attached per-question;
  * nothing at all — with no symbol and no attachments it's a free conversation
    on any topic.

Questions run asynchronously: POST /api/voice/ask starts a job and returns its
id, GET /api/voice/job/<id> collects the result. A phone that sleeps mid-answer
drops the connection, and this way the work isn't lost with it.

Exposed as a Flask blueprint: GET /voice (page), POST /api/voice/ask,
GET /api/voice/job/<id>, GET|POST /api/voice/docs, DELETE /api/voice/docs/<id>.
"""

import base64
import glob
import hashlib
import io
import json
import logging
import os
import random
import re
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

HISTORY_TURNS = 12  # cap prior turns fed back (6 Q&A pairs) to bound token growth

import requests
from flask import Blueprint, jsonify, render_template, request

from config import VOICE_S3_BUCKET, AWS_REGION, DATA_DIR
from security import client_ip, rate_limit_ok
from conversation_store import ConversationStore
from mp3_repair import repair_xing_header

# Persistent chat history (text only). Conversations untouched for 7 days are
# pruned; the store owns its own directory, locking and retention.
conversations = ConversationStore(os.path.join(DATA_DIR, "conversations"),
                                  retention_days=7)

# --- User-uploaded context documents ----------------------------------------
# Extracted plain text lives on the volume so uploads survive a redeploy and can
# be re-attached to later conversations. One .txt per doc plus a JSON index.
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
UPLOAD_INDEX = os.path.join(UPLOAD_DIR, "index.json")
UPLOAD_EXTS = {".txt", ".md", ".text", ".pdf"}
MAX_DOC_CHARS = 400_000       # truncate a single document's extracted text
MAX_DOCS_CHARS = 800_000      # total upload text fed into one prompt
MAX_ATTACHED_DOCS = 10        # docs attachable to a single question
MIN_DOC_CHARS = 20            # below this a PDF is almost certainly scanned/empty
_uploads_lock = threading.Lock()
_DOC_ID_RE = re.compile(r"[0-9a-f]{32}\Z")

# Lazily-created, reused S3 client (boto3 clients are thread-safe).
_s3_lock = threading.Lock()
_s3_client_obj = None


def _s3():
    global _s3_client_obj
    if _s3_client_obj is None:
        with _s3_lock:
            if _s3_client_obj is None:
                import boto3  # imported lazily so local dev without boto3 still runs
                _s3_client_obj = boto3.client("s3", region_name=AWS_REGION)
    return _s3_client_obj

# Bound concurrent expensive pipelines (STT + high-effort reasoning + parallel
# TTS) so a burst can't fan out unbounded OpenRouter spend / memory. Per-worker.
_VOICE_SEMA = threading.BoundedSemaphore(2)

# --- Async question jobs ----------------------------------------------------
# A question takes up to a minute; a phone that sleeps mid-request drops the
# connection and the user loses paid work that the server went on to finish.
# So /api/voice/ask returns a job id and the client polls for the result.
# In-memory by design — this app already requires a single worker process (the
# rate limiter, _VOICE_SEMA and the scheduler all assume it). A restart drops
# pending jobs; the client then gets a 404 and re-asks.
_jobs = {}
_jobs_lock = threading.Lock()
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
JOB_TTL = 1800            # a finished answer stays collectable for 30 min
JOB_MAX_FINISHED = 4      # each holds a multi-MB base64 WAV — keep memory bounded


def _sweep_jobs():
    """Drop expired jobs and cap retained finished ones. Caller holds the lock."""
    now = time.time()
    for jid in [j for j, v in _jobs.items()
                if v.get("done") and now - v["done"] > JOB_TTL]:
        _jobs.pop(jid, None)
    # A running job is never evicted by the cap — only completed ones, oldest first.
    finished = sorted(((v.get("done", 0), j) for j, v in _jobs.items() if v.get("done")))
    for _, jid in finished[:max(0, len(finished) - JOB_MAX_FINISHED)]:
        _jobs.pop(jid, None)

OR_URL = "https://openrouter.ai/api/v1"
STT_MODEL = "openai/gpt-4o-transcribe"
# usd per 1M tokens for STT_MODEL, measured from actual billed transcriptions
# (usage.cost / token counts, see tests/test_usage_cost.py + cost spec v2 §3.1).
# Fallback only — the primary source is the transcription JSON's exact usage.cost.
STT_COST_PER_M = 2.50       # usd per 1M input tokens (measured)
STT_OUT_COST_PER_M = 10.00  # usd per 1M output tokens (measured)

# Reasoning models offered in the Chat page dropdown. This is an allowlist, not
# a suggestion list: voice_ask only ever forwards an id found here, so a crafted
# request can't bill an arbitrary (or far more expensive) OpenRouter model.
# The ":online" suffix is OpenRouter's web-search plugin shorthand.
REASON_MODELS = [
    {"id": "x-ai/grok-4.5:online",          "label": "Grok 4.5"},
    {"id": "moonshotai/kimi-k3:online",     "label": "Kimi K3"},
    {"id": "openai/gpt-5.6-sol:online",     "label": "GPT-5.6 Sol"},
    {"id": "anthropic/claude-opus-5:online",   "label": "Claude Opus 5"},
    {"id": "anthropic/claude-sonnet-5:online", "label": "Claude Sonnet 5"},
    {"id": "z-ai/glm-5.2:online",           "label": "GLM-5.2"},
    {"id": "deepseek/deepseek-v4-flash-0731:online", "label": "DeepSeek V4 Flash"},
]
REASON_MODEL_IDS = {m["id"] for m in REASON_MODELS}
# REASON_MODEL = "google/gemini-3.1-pro-preview:online"
REASON_MODEL = REASON_MODELS[0]["id"]   # default when none is chosen

# usd per 1M tokens, per model id, from OpenRouter /api/v1/models pricing.*
# Kept in step with REASON_MODELS, but these are placeholders — the real values
# are a maintenance task (see the cost spec §9). Because the primary source is
# OpenRouter's own usage.cost, small drift here only affects the token-math
# fallback and is non-fatal.
MODEL_COSTS = {
    # (in, out) usd per 1M tokens, matching DEFAULT_MODEL_COST's tuple shape.
    "x-ai/grok-4.5:online":              (3.00, 15.00),
    "moonshotai/kimi-k3:online":         (1.10, 4.40),
    "openai/gpt-5.6-sol:online":         (2.50, 10.00),
    "anthropic/claude-opus-5:online":    (5.00, 25.00),
    "anthropic/claude-sonnet-5:online":  (3.00, 15.00),
    "z-ai/glm-5.2:online":               (0.80, 0.80),
    "deepseek/deepseek-v4-flash-0731:online": (0.25, 1.25),
}
DEFAULT_MODEL_COST = (1.00, 5.00)   # sane default for any unlisted id


def usage_cost(usage: dict | None, model_id: str) -> tuple[float | None, int | None]:
    """Return (cost_usd, total_tokens) for a completed call. NEVER raises.

    Prefers OpenRouter's own usage.cost (captures the `:online` web-search spend
    that token math can't). Falls back to MODEL_COSTS x token counts. Returns
    (None, None) when the usage block is missing or unconsumable — the UI shows
    nothing for these, never a wrong number.
    """
    if not usage:
        return None, None
    prompt = usage.get("prompt_tokens")
    comp = usage.get("completion_tokens")
    # None unless at least one token count is actually present, so a missing
    # usage never renders a misleading "0 tokens".
    if isinstance(prompt, int) or isinstance(comp, int):
        total = int(usage.get("total_tokens") or ((prompt or 0) + (comp or 0)))
    else:
        total = None
    cost = usage.get("cost")
    if isinstance(cost, str):
        try:
            cost = float(cost.lstrip("$").replace(",", ""))
        except ValueError:
            cost = None
    if not isinstance(cost, (int, float)):
        cost = None
    if cost is None and isinstance(prompt, int) and isinstance(comp, int):
        p, o = MODEL_COSTS.get(model_id, DEFAULT_MODEL_COST)
        cost = (prompt / 1e6 * p) + (comp / 1e6 * o)
    return (round(cost, 6) if cost is not None else None), total


def stt_cost(usage: dict | None) -> float | None:
    """Estimated usd for an STT call. Prefer usage.cost; else a measured
    per-token price x input/output tokens. String-cost coercion identical to
    usage_cost. NEVER raises."""
    if not usage:
        return None
    cost = usage.get("cost")
    if isinstance(cost, str):
        try:
            cost = float(cost.lstrip("$").replace(",", ""))
        except ValueError:
            cost = None
    if not isinstance(cost, (int, float)):
        cost = None
    if cost is None:
        # Transcriptions report input_tokens/output_tokens (not prompt_tokens).
        # Fall back to the measured per-M prices so a missing usage.cost still
        # yields an honest figure for the speech line.
        p_in = usage.get("input_tokens")
        p_out = usage.get("output_tokens")
        if isinstance(p_in, (int, float)):
            cost = p_in / 1e6 * STT_COST_PER_M
            if isinstance(p_out, (int, float)):
                cost += p_out / 1e6 * STT_OUT_COST_PER_M
    return round(cost, 6) if cost is not None else None


def stt_is_estimate(usage: dict | None) -> bool:
    """True when the STT figure is a fallback estimate, i.e. the transcription
    JSON had no exact usage.cost to read. NEVER raises."""
    return not (usage and usage.get("cost") is not None)


def tts_cost(chars: int, cfg: dict) -> float | None:
    """Estimated usd to synthesize `chars` chars with `cfg`. None when the model
    has no per-char price set. NEVER raises."""
    p = cfg.get("cost_per_char")
    return round(chars * p, 6) if p is not None else None


# TTS models offered in the Chat dropdown. Unlike reasoning models these are NOT
# uniform — each has its own voice set and audio format, verified against the
# endpoint. Two pipelines:
#   pcm  — chunk, silence-trim, crossfade, join to WAV (Gemini; instruction-following,
#          so it gets the performance prompt). Loses prosody past ~1 min, hence chunking.
#   mp3  — one request, return the encoded audio as-is. These aren't instruction-
#          following, so NO performance prompt (they'd read it aloud).
# cost_per_char (usd per audio char) is MEASURED from real billed OpenRouter
# requests at a realistic answer length (~800 chars, diluting the per-request
# floor), see tests/test_usage_cost.py + cost spec v2 §3.1. `~` always marks
# the voice line as an estimate.
TTS_MODELS = [
    {"id": "google/gemini-3.1-flash-tts-preview", "label": "Gemini Flash — expressive",
     "voice": "Charon", "pipeline": "pcm", "cost_per_char": 0.00003276},
    {"id": "x-ai/grok-voice-tts-1.0", "label": "Grok Voice", "voice": "eve",
     "pipeline": "mp3", "cost_per_char": 0.00001500},
    {"id": "hexgrad/kokoro-82m", "label": "Kokoro — open weights", "voice": "af_heart",
     "pipeline": "mp3", "cost_per_char": 0.00000062},
]
TTS_MODEL_BY_ID = {m["id"]: m for m in TTS_MODELS}
TTS_MODEL = TTS_MODELS[0]["id"]   # default
TTS_VOICE = TTS_MODELS[0]["voice"]


# Pre-authored prompts selectable on the Chat page. Each is {id, label, text}.
# Selecting one fills the chat input (`#qtext`) — the user can edit it before
# asking; the question is then sent unchanged through /api/voice/ask. This is an
# allowlist served to the template (and as JSON to the frontend), so UI and
# server can't drift. Add more entries to extend the picker.
PROMPT_PRESETS = [
    {
        "id": "podcast-note",
        "label": "Podcast research note",
        "text": (
            "Act as a research analyst writing a podcast-style spoken note about the "
            "company in the context (the filings, transcripts and documents I've "
            "attached). It will be read aloud by a text-to-speech engine while I'm "
            "driving, so write it to be listened to, not skimmed.\n\n"
            "Write it as flowing spoken prose — never bullet points, headings, tables, "
            "or symbols that won't read naturally out loud. Aim for roughly 1,800–2,200 "
            "words (about 10–15 minutes of speech). Open by naming the company plainly, "
            "then cover in order:\n"
            "1. What the business does and how it makes money (the business model).\n"
            "2. Product/service use cases from the customer's lens — who actually buys "
            "and uses the product, the concrete jobs and use cases it is bought for, the "
            "problem it solves for the customer, and why the customer picks it over "
            "alternatives today.\n"
            "3. Where it sits in the value chain — its suppliers and customers, and what "
            "leverage it has over each.\n"
            "4. The competitive landscape — who the real rivals are, the basis of "
            "competition, and what moat (if any) protects it.\n"
            "5. The investment thesis — the bull case, the bear case, and the key "
            "numbers or assumptions that would break it.\n\n"
            "Quality requirements:\n"
            "- Every sentence must carry information. Cut fluff, filler, hedging, and "
            "sugar-coating. Do not pad to reach length; if the facts are weak, say so "
            "plainly.\n"
            "- Do not introduce any term, acronym, or metric you have not defined. The "
            "first time you mention anything technical, give a one-line plain-English "
            "explanation.\n"
            "- Ground every claim in the attached material. Say explicitly what the "
            "filings support vs. what is inference or judgment.\n"
            "- Be balanced and honest — state risks and negatives as clearly as positives.\n"
            "- Use natural complete sentences suited to TTS: no URLs, symbols, or "
            "abbreviations that would be mispronounced. Write numbers as digits (e.g. "
            "\"1.2x\", \"₹4,800 crore\", \"32%\"), not spelled out in words.\n\n"
            "Write the note now, in one continuous piece of prose."
        ),
    },
]
PROMPT_PRESETS_BY_ID = {p["id"]: p for p in PROMPT_PRESETS}
TTS_PCM_RATE = 24000
# Measured, not estimated: this voice speaks ~17.5 chars/sec (calibrated by
# synthesizing 400/700/1000-char samples and dividing PCM bytes by 48000 B/s).
# A single generation loses prosody past roughly a minute, so cap each chunk
# near 50s and leave margin for the rate varying with content.
TTS_CHARS_PER_SEC = 17.5
TTS_CHAR_LIMIT = 880          # ~50s/chunk (the ~120s API cap is ~2100 chars)
TTS_WORKERS = 6              # synthesize answer chunks in parallel

# Every chunk is a separate, stateless generation: the model re-derives pitch,
# pace and register from scratch each time, which is what makes joins audible.
# Sending identical direction with each chunk anchors those choices so the
# pieces match. Verified against the endpoint — the direction is obeyed, not
# read aloud. Deliberately avoids "unhurried"/"slow"/"no rush", which measurably
# drag the pace; the goal is consistency, not languor.
TTS_DIRECTION = (
    "Synthesize speech for the performance defined below. "
    "Speak ONLY the lines under #### TRANSCRIPT.\n\n"
    "### PERFORMANCE\n"
    "A measured equity analyst briefing a client at a normal conversational pace. "
    "Warm and sincere, clear and matter-of-fact. Hold the same pitch centre, "
    "energy and tempo from the first word to the last.\n\n"
    "#### TRANSCRIPT\n"
)

# Seam treatment: trim each generation's own lead-in/tail silence, fade the cut
# edges so the splice can't click, and insert one fixed pause between pieces.
# Chunks break at sentence ends, so a pause is what belongs there anyway.
TTS_JOIN_PAUSE_MS = 160
TTS_FADE_MS = 12
TTS_SILENCE_FLOOR = 0.006     # fraction of full scale counted as silence
TTS_EDGE_PAD_MS = 25          # keep this much silence either side of speech
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


def _load_context_s3(sym: str):
    """Read <SYMBOL>/*.txt filings from the S3 bucket the scraper populates."""
    s3 = _s3()
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=VOICE_S3_BUCKET, Prefix=f"{sym}/"):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".txt"):
                keys.append(obj["Key"])
    parts, names = [], []
    for key in sorted(keys):  # stable order regardless of listing order
        try:
            body = s3.get_object(Bucket=VOICE_S3_BUCKET, Key=key)["Body"].read()
        except Exception as e:
            log.warning("S3 get_object failed for %s: %s", key, e)
            continue
        text = body.decode("utf-8", errors="replace")
        if not text.strip():
            continue
        parts.append(f"===== {key.split('/')[-1]} =====\n{text}")
        names.append(key)
    joined = "\n\n".join(parts)[:MAX_CONTEXT_CHARS]
    return joined, names


def load_context(symbol: str):
    """All .txt filings for a symbol. Source: S3 when VOICE_S3_BUCKET is set
    (cloud), else the local download trees (dev Mac)."""
    sym = symbol.strip().upper()
    if VOICE_S3_BUCKET:
        try:
            return _load_context_s3(sym)
        except Exception as e:
            log.warning("S3 context load failed for %s: %s", sym, e)
            return "", []
    parts, names = [], []
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
    joined = "\n\n".join(parts)[:MAX_CONTEXT_CHARS]
    return joined, names


# --- Uploaded documents -----------------------------------------------------

def _read_index() -> list:
    """The upload index, newest first. Caller holds _uploads_lock for writes."""
    try:
        with open(UPLOAD_INDEX, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _write_index(entries: list):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    tmp = UPLOAD_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, UPLOAD_INDEX)   # atomic: never leave a half-written index


def _doc_path(doc_id: str) -> str:
    return os.path.join(UPLOAD_DIR, f"{doc_id}.txt")


def extract_upload_text(filename: str, raw: bytes) -> str:
    """Plain text from an uploaded .txt/.md or .pdf. Raises ValueError with a
    user-facing message when the file is unusable."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in UPLOAD_EXTS:
        raise ValueError("only .txt, .md and .pdf files are supported")
    if not raw:
        raise ValueError("file is empty")

    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as e:
            log.warning("PDF extract failed for %r: %s", filename, e)
            raise ValueError("could not read this PDF (encrypted or corrupt?)")
    else:
        text = raw.decode("utf-8", errors="replace")

    text = text.strip()
    if len(text) < MIN_DOC_CHARS:
        raise ValueError("no extractable text — a scanned PDF needs OCR first")
    return text[:MAX_DOC_CHARS]


def save_upload(filename: str, text: str) -> dict:
    """Persist extracted text under a fresh id and return its index entry."""
    doc_id = uuid.uuid4().hex
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(_doc_path(doc_id), "w", encoding="utf-8") as f:
        f.write(text)
    entry = {
        "id": doc_id,
        # Display name only — the id, not this, determines the path on disk.
        "name": os.path.basename(filename or "document")[:120],
        "chars": len(text),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    with _uploads_lock:
        _write_index([entry] + _read_index())
    return entry


def delete_upload(doc_id: str) -> bool:
    if not _DOC_ID_RE.fullmatch(doc_id or ""):
        return False
    with _uploads_lock:
        entries = _read_index()
        remaining = [e for e in entries if e.get("id") != doc_id]
        if len(remaining) == len(entries):
            return False
        _write_index(remaining)
    try:
        os.remove(_doc_path(doc_id))
    except OSError:
        pass
    return True


def load_uploads(doc_ids):
    """Concatenated text for the given upload ids, plus their display names.
    Unknown or malformed ids are skipped rather than failing the question."""
    if not doc_ids:
        return "", []
    by_id = {e.get("id"): e for e in _read_index()}
    parts, names = [], []
    for doc_id in doc_ids[:MAX_ATTACHED_DOCS]:
        entry = by_id.get(doc_id)
        if not entry or not _DOC_ID_RE.fullmatch(doc_id or ""):
            continue
        try:
            with open(_doc_path(doc_id), "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if not text.strip():
            continue
        name = entry.get("name") or doc_id
        parts.append(f"===== {name} =====\n{text}")
        names.append(name)
    return "\n\n".join(parts)[:MAX_DOCS_CHARS], names


# Biases the transcriber toward English financial vocabulary. `language: "en"`
# alone has been seen to fail on short clips (returning the words spelled out in
# another script); the prompt is a second, softer nudge. The textbox review step
# on the client is the real backstop when both miss.
_STT_PROMPT = ("An English-language question about a public company's quarterly "
               "results, guidance and management commentary.")


def transcribe(audio_b64: str, fmt: str) -> tuple[str, dict | None]:
    resp = requests.post(
        f"{OR_URL}/audio/transcriptions",
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
        json={
            "model": STT_MODEL,
            "input_audio": {"data": audio_b64, "format": fmt},
            "language": "en",
            "prompt": _STT_PROMPT,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    usage = data.get("usage") or None
    return (data.get("text") or "").strip(), usage


_SPOKEN_STYLE = (
    "This is an ongoing spoken conversation: resolve follow-up references like 'that', "
    "'those', 'the same quarter' or 'and the margin?' against the earlier turns below. "
    "Keep the answer tight and conversational since it will be read aloud. "
    "Do NOT use markdown, bullet points, headers or asterisks — reply in flowing "
    "spoken prose. "
    "NEVER cite sources with links, URLs, domain names or bracketed references — "
    "no [text](url), no https://…, no 'sec.gov', no [1]. Every character you write "
    "is spoken by a text-to-speech engine, and a URL read aloud is unusable. "
    "When attribution matters, name the source in plain words instead — say "
    "'per Nokia's Q2 press release' or 'according to the 20-F', never a link."
)

# --- Answer sanitiser -------------------------------------------------------
# The ":online" models attach web-search citations as markdown links no matter
# what the prompt says. The answer is fed straight to TTS, so a stray URL is
# read out character by character. Strip citations here as a hard guarantee —
# the prompt above is the primary fix, this is the net under it.
# Order matters: markdown links must go before bare URLs, or the bare-URL
# pattern eats the target out of "[label](url)" and orphans the brackets.
_MD_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(\s*<?\s*(?:https?://|www\.)[^)\s]*\s*>?\s*\)")
_NUM_REF_RE = re.compile(r"\[\s*\d+(?:\s*[,;–—-]\s*\d+)*\s*\]")
_BARE_URL_RE = re.compile(r"<?\b(?:https?://|www\.)[^\s<>()\[\]]+>?")
# A label that is just a bare domain ("sec.gov", "via.ritzau.dk") is a citation,
# not prose — TTS would spell it out letter by letter. Human labels are kept.
_DOMAIN_ONLY_RE = re.compile(r"\A[\w-]+(?:\.[\w-]+)+\Z")


# Removing a citation can orphan the phrase that introduced it ("…the segment
# table in ."). The repair below is anchored on a sentinel marking where a
# citation actually stood, so it can only ever touch text the removal broke —
# a global "strip dangling prepositions" pass would mangle innocent prose.
_CITE = "\x00"
_ORPHAN_RE = re.compile(
    r"(?i)[,;]?\s*\b(?:according to|per|via|see|from|source|sources|in|at|on|of|by|to)\b"
    r"\s*[:,]?\s*" + _CITE + r"(?=\s*(?:[.,;:!?)]|$))")


def _clean_answer(text: str) -> str:
    """Strip web-search citation artefacts so nothing unspeakable reaches TTS."""
    if not text:
        return ""

    def _delink(m):
        # A human-readable label is real prose — keep it, drop only the target.
        label = (m.group(1) or "").strip()
        return _CITE if (not label or _DOMAIN_ONLY_RE.match(label)) else label

    text = text.replace(_CITE, "")          # never trust the model with our sentinel
    text = _MD_LINK_RE.sub(_delink, text)
    text = _NUM_REF_RE.sub(_CITE, text)
    text = _BARE_URL_RE.sub(_CITE, text)

    text = _ORPHAN_RE.sub("", text)         # drop the introducer it belonged to
    text = re.sub(r"\(\s*" + _CITE + r"\s*\)", "", text)   # "(<url>)" wholesale
    text = text.replace(_CITE, "")

    # Whatever removal left behind: empty brackets/parens, doubled spaces, a
    # space before punctuation, and punctuation doubled by the excision.
    text = re.sub(r"\(\s*[,;:]?\s*\)|\[\s*\]", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,.;:])\1+", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def resolve_model(requested: str) -> str:
    """Map a requested model id onto the allowlist; anything else -> default."""
    requested = (requested or "").strip()
    return requested if requested in REASON_MODEL_IDS else REASON_MODEL


def reason(question: str, context: str, symbol: str, history=None, model=None) -> tuple[str, dict | None]:
    """`context` is pre-assembled labelled blocks (filings and/or uploaded
    documents) built by voice_ask — it may be empty in free conversation.

    Returns `(answer, usage)`: `usage` is the OpenRouter `usage` dict (which
    carries OpenRouter's own computed `cost`, capturing the `:online` web-search
    spend that token math can't) or None when absent. Cost is computed once
    here and persisted by the caller."""
    if symbol:
        system = (
            f"You are an expert equity-research analyst answering a spoken question about {symbol.upper()}. "
            "Answer from the reference material provided below — quarterly transcripts, slides, reports "
            "and any documents the user has uploaded. Use web search if asked. "
            "Be precise and specific with numbers, guidance and management commentary; cite the quarter when relevant. "
            "If the answer is not in the material or web search, say so plainly rather than guessing. "
            + _SPOKEN_STYLE + "\n\n" + context
        )
    else:
        # Free-conversation mode: no symbol, so no filings corpus. Same analyst
        # persona and spoken style, answering on any topic from general knowledge
        # — plus any documents the user attached.
        system = (
            "You are a sharp, well-read research analyst having an open conversation "
            "on whatever topic the user raises — markets, a company, an industry, or "
            "anything else. Answer from your own knowledge; use web search if asked. "
            "Be specific and concrete: name figures, dates and sources where you can, "
            "and say plainly when you do not know or are unsure rather than guessing. "
            + _SPOKEN_STYLE
        )
        if context:
            system += (
                "\n\nThe user has attached the reference material below. When the "
                "question touches on it, ground the answer in it and prefer it over "
                "your own recollection.\n\n" + context
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
            "model": resolve_model(model),
            "reasoning": {"effort": "high"},
            "messages": messages,
        },
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    # content can be null on a refusal or reasoning-only turn — coalesce, don't .strip(None).
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    usage = data.get("usage") or None
    # Sanitise here so the displayed text and the TTS input are the same string.
    answer = _clean_answer((msg.get("content") or "").strip())
    return answer, usage


def _chunk(text: str, limit: int):
    """Split into synthesis-sized pieces, preferring a paragraph break to a
    mid-paragraph one. Greedy packing to exactly `limit` puts the cut wherever
    the character count happens to land; ending a chunk where the writing
    already ends puts the unavoidable pause where a pause belongs."""
    chunks, cur = [], ""

    def flush():
        nonlocal cur
        if cur:
            chunks.append(cur)
            cur = ""

    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        for sent in [s for s in re.split(r"(?<=[.!?])\s+|\n+", para) if s]:
            # A single sentence longer than the limit has no good break; cut it.
            while len(sent) > limit:
                flush()
                chunks.append(sent[:limit])
                sent = sent[limit:]
            if cur and len(cur) + len(sent) + 1 > limit:
                flush()
            cur = f"{cur} {sent}".strip()
        # Prefer to end here, but only once the chunk is substantial — flushing a
        # two-line paragraph on its own would make more seams, not fewer.
        if len(cur) >= limit * 0.6:
            flush()
    flush()
    return chunks


def _pcm_array(pcm: bytes):
    import numpy as np
    if len(pcm) % 2:
        pcm = pcm[:-1]          # a truncated final sample would shift every frame
    return np.frombuffer(pcm, dtype="<i2")


def _trim_and_fade(pcm: bytes) -> bytes:
    """Drop a generation's own leading/trailing silence and fade the cut edges.
    Each response carries its own dead air; concatenating it raw is half of why
    joins are audible, and slicing mid-waveform is the other half (a step in
    amplitude is a click)."""
    import numpy as np
    a = _pcm_array(pcm)
    if a.size == 0:
        return b""
    loud = np.flatnonzero(np.abs(a) > int(32767 * TTS_SILENCE_FLOOR))
    if loud.size == 0:
        return b""              # a chunk of pure silence contributes nothing
    pad = int(TTS_PCM_RATE * TTS_EDGE_PAD_MS / 1000)
    a = a[max(0, loud[0] - pad): min(a.size, loud[-1] + pad + 1)].astype(np.float32)
    fade = min(int(TTS_PCM_RATE * TTS_FADE_MS / 1000), a.size // 2)
    if fade > 0:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        a[:fade] *= ramp
        a[-fade:] *= ramp[::-1]
    return a.astype("<i2").tobytes()


def _join_pcm(pieces) -> bytes:
    """Concatenate synthesized chunks with a single consistent pause between."""
    parts = [p for p in (_trim_and_fade(p) for p in pieces if p) if p]
    if not parts:
        return b""
    gap = b"\x00\x00" * int(TTS_PCM_RATE * TTS_JOIN_PAUSE_MS / 1000)
    return gap.join(parts)


def resolve_tts_model(requested):
    """A TTS model config from the allowlist; unknown/absent -> default."""
    return TTS_MODEL_BY_ID.get((requested or "").strip(), TTS_MODELS[0])


def _tts_request(text: str, cfg: dict, fmt: str) -> bytes:
    """One TTS call for one piece of text, retrying transient failures. Guards
    the silent-corruption modes: 429/5xx (retry) and a 200 whose body is empty
    or a JSON error blob (which, played as audio, is noise). The performance
    prompt is only prepended for instruction-following (pcm) models."""
    body_input = (TTS_DIRECTION + text) if cfg["pipeline"] == "pcm" else text
    last_err = None
    for attempt in range(TTS_RETRIES):
        try:
            resp = requests.post(
                f"{OR_URL}/audio/speech",
                headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
                json={"model": cfg["id"], "input": body_input,
                      "voice": cfg["voice"], "response_format": fmt},
                timeout=180,
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"TTS status {resp.status_code}", response=resp)
            resp.raise_for_status()
            data = resp.content
            ctype = resp.headers.get("Content-Type", "").lower()
            if not data or "json" in ctype or "text" in ctype:
                raise ValueError(f"empty or non-audio TTS body (ctype={ctype!r}, len={len(data)})")
            return data
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < TTS_RETRIES - 1:
                time.sleep((2 ** attempt) * 0.5 + random.random() * 0.3)  # exp backoff + jitter
    raise last_err


def _synthesize_pcm_wav(text: str) -> bytes:
    """Gemini path: chunk, synthesize in parallel, trim/fade/join PCM to one WAV."""
    import concurrent.futures
    cfg = TTS_MODELS[0]
    chunks = _chunk(text, TTS_CHAR_LIMIT)
    if not chunks:
        raise ValueError("nothing to synthesize")
    if len(chunks) == 1:
        pcm = _join_pcm([_tts_request(chunks[0], cfg, "pcm")])
    else:
        pieces = [b""] * len(chunks)
        with concurrent.futures.ThreadPoolExecutor(max_workers=TTS_WORKERS) as ex:
            futs = {ex.submit(_tts_request, c, cfg, "pcm"): i for i, c in enumerate(chunks)}
            for fut in concurrent.futures.as_completed(futs):
                i = futs[fut]
                try:
                    pieces[i] = fut.result()
                except Exception as e:
                    log.warning("TTS chunk %d/%d failed, dropping: %s", i + 1, len(chunks), e)
        pcm = _join_pcm(pieces)
        if not pcm:
            raise RuntimeError("all TTS chunks failed")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(TTS_PCM_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def synthesize_audio(text: str, cfg: dict = None):
    """Render `text` with the given TTS model config. Returns (bytes, mime, ext).
    PCM models get the chunk/seam pipeline; MP3 models get one passthrough call."""
    cfg = cfg or TTS_MODELS[0]
    # Strip markup TTS reads oddly (applies to every model).
    cleaned = re.sub(r"[\[\]*`#]", " ", text)
    if cfg["pipeline"] == "pcm":
        return _synthesize_pcm_wav(cleaned), "audio/wav", "wav"
    # MP3 models aren't instruction-following and have their own length handling;
    # one request. Kokoro returns its segments concatenated, so the leading Xing
    # header describes only the first — repair it or Safari plays ~11s of a
    # 100s answer and calls it done (see mp3_repair).
    if not cleaned.strip():
        raise ValueError("nothing to synthesize")
    return repair_xing_header(_tts_request(cleaned, cfg, "mp3")), "audio/mpeg", "mp3"


# Back-compat alias — the old name returned a WAV via the default (Gemini) model.
def synthesize_wav(text: str) -> bytes:
    return synthesize_audio(text, TTS_MODELS[0])[0]


# --- TTS audio cache (S3) ---------------------------------------------------
# TTS is the expensive step (an OpenRouter call per ~50s chunk). Cache each
# rendered clip in S3, content-addressed by the exact text + model + voice, so
# the same answer is never synthesized twice — a replay of an old answer is a
# cheap S3 GET, and an answer generated during /ask is already warm for replay.
# Under its own prefix so it never collides with the <SYMBOL>/*.txt filings the
# voice loader reads. No S3 configured (local dev) => transparent passthrough.
TTS_CACHE_PREFIX = "tts-cache/"
# Bumped when the MP3 post-processing changes, so clips cached by an older
# rendering aren't served forever. WAV renderings are unaffected and keep their
# keys — re-synthesizing those costs a Gemini call per chunk.
MP3_RENDER_VERSION = "xing-repaired"


def _tts_cache_key(text: str, cfg: dict, ext: str) -> str:
    # Model+voice+prompt in the hash so switching model/voice yields a different
    # key rather than serving a stale rendering.
    sig = f"{cfg['id']}|{cfg['voice']}|{cfg['pipeline']}|{TTS_PCM_RATE}|{TTS_DIRECTION}"
    if cfg["pipeline"] == "mp3":
        sig += f"|{MP3_RENDER_VERSION}"
    digest = hashlib.sha256((sig + "\x00" + text).encode("utf-8")).hexdigest()
    return f"{TTS_CACHE_PREFIX}{digest}.{ext}"


def synthesize_audio_cached(text: str, cfg: dict = None):
    """(bytes, mime, synthesized) for `text`, served from the S3 cache when
    present else synthesized and cached. Any cache error degrades to a plain
    synthesis so a flaky bucket can never break playback. `synthesized` is
    True only when a fresh synthesis actually ran (a cache hit is free), which
    is what decides whether a TTS cost should be shown."""
    cfg = cfg or TTS_MODELS[0]
    mime = "audio/wav" if cfg["pipeline"] == "pcm" else "audio/mpeg"
    ext = "wav" if cfg["pipeline"] == "pcm" else "mp3"
    if not VOICE_S3_BUCKET:
        return synthesize_audio(text, cfg)[0], mime, True
    key = _tts_cache_key(text, cfg, ext)
    try:
        obj = _s3().get_object(Bucket=VOICE_S3_BUCKET, Key=key)
        data = obj["Body"].read()
        if data:
            return data, mime, False
    except Exception:
        pass  # miss or transient error — synthesize
    audio, mime, _ = synthesize_audio(text, cfg)
    try:
        _s3().put_object(Bucket=VOICE_S3_BUCKET, Key=key, Body=audio, ContentType=mime)
    except Exception as e:
        log.warning("TTS cache write failed for %s: %s", key, e)
    return audio, mime, True


def synthesize_wav_cached(text: str) -> bytes:
    """Back-compat: cached synthesis with the default model, WAV bytes only."""
    return synthesize_audio_cached(text, TTS_MODELS[0])[0]


@voice_bp.route("/voice")
def voice_page():
    # Render the dropdowns from the same allowlists the API validates against,
    # so UI and server can never drift apart.
    return render_template("voice.html", models=REASON_MODELS,
                           default_model=REASON_MODEL,
                           tts_models=TTS_MODELS, default_tts=TTS_MODEL,
                           prompt_presets=PROMPT_PRESETS)


def _audio_fmt(audio) -> str:
    """Resolve an uploaded recording's format from its filename, falling back to
    webm. Shared by transcribe-only and (historically) the ask pipeline."""
    fmt = ((audio.filename or "").rsplit(".", 1)[-1] or "webm").lower()
    if fmt not in {"webm", "wav", "mp3", "mp4", "m4a", "ogg", "flac", "aac"}:
        fmt = "webm"
    return fmt


@voice_bp.route("/api/voice/transcribe", methods=["POST"])
def voice_transcribe():
    """Speech-to-text only. The client shows the result in the question box for
    review/edit before the (expensive) answer pipeline runs, so a misheard
    question can be corrected instead of being reasoned over and spoken back.
    Fast enough (~seconds) to be a plain synchronous request, unlike /ask."""
    if not rate_limit_ok(f"stt:{client_ip(request)}", 30, 60):
        return jsonify({"error": "Too many requests — slow down a moment."}), 429
    audio = request.files.get("audio")
    if not audio:
        return jsonify({"error": "no audio"}), 400
    try:
        audio_b64 = base64.b64encode(audio.read()).decode("ascii")
        text, _usage = transcribe(audio_b64, _audio_fmt(audio))
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        log.warning("STT upstream error (status=%s): %s", status, e)
        return jsonify({"error": "transcription service error — try again"}), 502
    except Exception as e:
        log.exception("STT error: %s", e)
        return jsonify({"error": "could not transcribe the audio"}), 500
    if not text:
        return jsonify({"error": "couldn't understand the audio — try again"}), 422
    return jsonify({"text": text})


@voice_bp.route("/api/voice/speak", methods=["POST"])
def voice_speak():
    """Text-to-speech for an existing answer. Saved chats are stored text-only,
    so replaying an old answer's audio means re-synthesizing it on demand.

    Async like /ask, and for the same reason: synthesis takes up to a minute, and
    a phone that sleeps mid-request would otherwise lose it. Returns a job id; the
    client polls /api/voice/job/<id>. The text is a stored answer (already
    citation-sanitised); synthesize_wav cleans it again regardless."""
    if not rate_limit_ok(f"speak:{client_ip(request)}", 12, 300):
        return jsonify({"error": "Too many requests — slow down a moment."}), 429
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "nothing to speak"}), 400
    if len(text) > 20000:
        return jsonify({"error": "text too long to synthesize"}), 400

    tts_cfg = resolve_tts_model(data.get("tts_model"))

    # Bound concurrent TTS the same way /ask does; released by the worker.
    if not _VOICE_SEMA.acquire(blocking=False):
        return jsonify({"error": "Server busy with another request — try again in a moment."}), 429
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _sweep_jobs()
        _jobs[job_id] = {"status": "running", "created": time.time()}
    threading.Thread(target=_run_speak, args=(job_id, text, tts_cfg), daemon=True).start()
    return jsonify({"job_id": job_id, "status": "running"}), 202


def _run_speak(job_id, text, tts_cfg=None):
    """Synthesize off-request and stash the result in the job store. Never raises."""
    try:
        try:
            audio, mime, _synth = synthesize_audio_cached(text, tts_cfg)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            log.warning("TTS upstream error (status=%s): %s", status, e)
            return _finish_job(job_id, {"error": "speech service error — try again in a moment."}, "error")
        except Exception as e:
            log.exception("TTS error: %s", e)
            return _finish_job(job_id, {"error": "could not synthesize audio"}, "error")
        _finish_job(job_id, {
            "audio": f"data:{mime};base64," + base64.b64encode(audio).decode("ascii"),
        }, "done")
    finally:
        _VOICE_SEMA.release()


@voice_bp.route("/api/voice/ask", methods=["POST"])
def voice_ask():
    if not rate_limit_ok(f"voice:{client_ip(request)}", 20, 60):
        return jsonify({"error": "Too many requests — slow down a moment."}), 429

    # Symbol is OPTIONAL. Blank => free conversation: no filings context, general
    # knowledge only. When present it must start alnum and contain no "..", so it
    # can't traverse out of a download dir / S3 prefix.
    symbol = (request.form.get("symbol") or "").strip()
    if symbol and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,19}", symbol)
                   or ".." in symbol):
        return jsonify({"error": "enter a valid symbol"}), 400

    typed = (request.form.get("text") or "").strip()
    if len(typed) > MAX_TYPED_CHARS:
        return jsonify({"error": "question too long"}), 400
    audio = request.files.get("audio")
    if not typed and not audio:
        return jsonify({"error": "no question — type or record one"}), 400

    # Unknown/absent ids silently fall back to the default rather than 400ing —
    # a stale dropdown value shouldn't cost you the question you just recorded.
    model = resolve_model(request.form.get("model"))
    tts_cfg = resolve_tts_model(request.form.get("tts_model"))

    # TTS-off per message: "0" skips the answer's speech synthesis. Anything
    # else (or absent) keeps current behavior. Lenient parse — a bad value or
    # a missing field must never fail the ask.
    tts_enabled = (request.form.get("tts_enabled") or "1") != "0"

    doc_ids = []
    raw_docs = request.form.get("docs")
    if raw_docs:
        try:
            parsed = json.loads(raw_docs)
            if isinstance(parsed, list):
                doc_ids = [d for d in parsed if isinstance(d, str)][:MAX_ATTACHED_DOCS]
        except (ValueError, TypeError):
            doc_ids = []

    docs_text, doc_names = load_uploads(doc_ids)

    blocks, sources = [], []
    if symbol:
        filings, filing_names = load_context(symbol)
        # Attached documents alone are enough context — only insist on filings
        # when the user gave a symbol and attached nothing.
        if not filings and not docs_text:
            return jsonify({"error": f"no .txt filings found for {symbol.upper()} "
                                     "(fetch them first, or attach a document)"}), 404
        if filings:
            blocks.append(f"===== FILINGS FOR {symbol.upper()} =====\n{filings}")
            sources += filing_names
    if docs_text:
        blocks.append("===== UPLOADED DOCUMENTS =====\n" + docs_text)
        sources += doc_names
    context = "\n\n".join(blocks)

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

    # Which conversation this turn belongs to. A client resuming an existing
    # chat sends its id; a new chat sends nothing and we mint one, returning it
    # so the client can adopt it. The conversation record itself isn't written
    # until the answer succeeds (in _run_ask), so a failed question leaves no
    # empty conversation behind.
    conv_id = (request.form.get("conversation_id") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", conv_id):
        conv_id = uuid.uuid4().hex

    if not _VOICE_SEMA.acquire(blocking=False):
        return jsonify({"error": "Server busy with another request — "
                                 "try again in a moment."}), 429

    # Hand off to a worker and return a job id immediately. A phone that sleeps
    # mid-question drops the HTTP connection; holding the answer server-side
    # means the client can reconnect and collect it instead of losing a minute
    # of paid reasoning. Everything below runs off the request context, so all
    # request data (form fields, uploaded audio bytes) is already materialised.
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _sweep_jobs()
        _jobs[job_id] = {"status": "running", "created": time.time()}
    threading.Thread(
        target=_run_ask,
        args=(job_id, typed, audio_b64, fmt, context, sources, symbol, history, model, conv_id, tts_cfg, tts_enabled),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "status": "running",
                    "conversation_id": conv_id}), 202


def _finish_job(job_id: str, payload: dict, status: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(status=status, payload=payload, done=time.time())


def _run_ask(job_id, typed, audio_b64, fmt, context, sources, symbol, history, model, conv_id=None, tts_cfg=None, tts_enabled=True):
    """The STT -> reason -> TTS pipeline, run off-request. Never raises."""
    try:
        try:
            stt_usage = None
            if typed:
                question = typed
            else:
                question, stt_usage = transcribe(audio_b64, fmt)
                if not question:
                    return _finish_job(job_id, {"error": "couldn't understand the audio — try again"}, "error")
            answer, usage = reason(question, context, symbol, history, model)
            if not answer:
                return _finish_job(job_id, {"error": "the model returned an empty answer — try rephrasing"}, "error")
            audio = mime = None
            tts_synth = False
            if tts_enabled:
                audio, mime, tts_synth = synthesize_audio_cached(answer, tts_cfg)
            # Accounting is deliberately in its own guarded block: a cost bug
            # must never sink a paid-for answer. The helpers are total, but
            # defend anyway in case that guarantee is broken later.
            cost = tokens = voice_cost = stt_amt = None
            stt_est = True
            tts_chars = 0
            try:
                cost, tokens = usage_cost(usage, model)
                stt_amt = stt_cost(stt_usage)
                # stt_est means "the figure is genuinely an estimate" — True
                # only when there was no exact usage.cost to read. An STT that
                # resolved exact via usage.cost renders `speech $Z` (no ~).
                stt_est = stt_is_estimate(stt_usage)
                if tts_synth:
                    # characters actually sent to TTS ~= the stripped text
                    # synthesize_audio processes (see _clean text below).
                    tts_chars = len(re.sub(r"[\[\]*`#]", " ", answer))
                    voice_cost = tts_cost(tts_chars, tts_cfg)
            except Exception:
                log.warning("cost accounting failed for turn (answer still delivered)")
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            log.warning("voice upstream error (status=%s): %s", status, e)
            return _finish_job(job_id, {"error": "upstream service error — try again in a moment."}, "error")
        except Exception as e:
            log.exception("voice pipeline error: %s", e)
            return _finish_job(job_id, {"error": "internal error while processing your question"}, "error")

        # Persist the completed turn (text only) before delivering it, so a
        # client that vanished mid-answer still finds the turn saved on reconnect
        # — the same durability the job store gives the in-flight answer. A
        # persistence failure must never sink an answer we already paid for.
        sym = symbol.upper()
        if conv_id:
            try:
                conversations.append_turn(conv_id, {
                    "question": question, "answer": answer, "symbol": sym,
                    "model": model, "sources": sources,
                    "cost": cost, "tokens": tokens,
                    "voice_cost": voice_cost, "voice_chars": tts_chars,
                    "stt_cost": stt_amt,
                    "stt_est": stt_est, "voice_est": True,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                log.warning("failed to persist turn to %s: %s", conv_id, e)

        payload = {
            "symbol": sym,   # "" in free-conversation mode
            "model": model,
            "sources": sources,
            "question": question,
            "answer": answer,
            "cost": cost, "tokens": tokens,
            "voice_cost": voice_cost, "voice_chars": tts_chars,
            "stt_cost": stt_amt,
            "stt_est": stt_est, "voice_est": True,
            "conversation_id": conv_id,
        }
        # Only attach audio when it was produced (TTS-off turns omit it so the
        # card renders text-only; None would crash base64.b64encode).
        if audio is not None:
            payload["audio"] = f"data:{mime};base64," + base64.b64encode(audio).decode("ascii")
        _finish_job(job_id, payload, "done")
    finally:
        _VOICE_SEMA.release()


@voice_bp.route("/api/voice/job/<job_id>", methods=["GET"])
def voice_job(job_id):
    """Poll for a question's result. Cheap and generously rate-limited: a phone
    waking from sleep may poll several times in quick succession."""
    if not rate_limit_ok(f"job:{client_ip(request)}", 240, 60):
        return jsonify({"error": "Too many requests — slow down a moment."}), 429
    if not _JOB_ID_RE.fullmatch(job_id or ""):
        return jsonify({"status": "missing", "error": "unknown job"}), 404
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            # Expired, evicted, or lost to a restart — the client must re-ask.
            return jsonify({"status": "missing",
                            "error": "that answer is no longer available — ask again"}), 404
        status = job["status"]
        payload = job.get("payload")
    if status == "running":
        return jsonify({"status": "running"})
    # Deliberately 200 for both done and error: the HTTP status describes the
    # poll, not the job. The body carries the outcome.
    return jsonify({"status": status, **(payload or {})})


# --- Upload endpoints -------------------------------------------------------
# All are behind the app-wide auth gate (server._require_auth). Uploads are
# stored as extracted text only — the original .pdf/.txt bytes are discarded.

@voice_bp.route("/api/voice/docs", methods=["GET"])
def list_docs():
    return jsonify({"docs": _read_index()})


@voice_bp.route("/api/voice/docs", methods=["POST"])
def upload_docs():
    if not rate_limit_ok(f"upload:{client_ip(request)}", 30, 300):
        return jsonify({"error": "Too many uploads — wait a few minutes."}), 429

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "no files uploaded"}), 400

    added, errors = [], []
    for f in files[:MAX_ATTACHED_DOCS]:
        name = f.filename or "document"
        try:
            text = extract_upload_text(name, f.read())
            added.append(save_upload(name, text))
        except ValueError as e:
            errors.append({"name": os.path.basename(name)[:120], "error": str(e)})
        except OSError as e:
            log.warning("upload save failed for %r: %s", name, e)
            errors.append({"name": os.path.basename(name)[:120],
                           "error": "could not save the extracted text"})

    if not added and errors:
        return jsonify({"added": [], "errors": errors}), 400
    return jsonify({"added": added, "errors": errors})


@voice_bp.route("/api/voice/docs/<doc_id>", methods=["DELETE"])
def remove_doc(doc_id):
    if not delete_upload(doc_id):
        return jsonify({"error": "not found"}), 404
    return jsonify({"deleted": doc_id})


# --- Conversation history ---------------------------------------------------
# Text-only persistence for the sidebar. All behind the app-wide auth gate.

@voice_bp.route("/api/voice/conversations", methods=["GET"])
def list_conversations():
    return jsonify({"conversations": conversations.list()})


@voice_bp.route("/api/voice/conversations/<conv_id>", methods=["GET"])
def get_conversation(conv_id):
    conv = conversations.get(conv_id)
    if conv is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(conv)


@voice_bp.route("/api/voice/conversations/<conv_id>", methods=["PATCH"])
def rename_conversation(conv_id):
    data = request.get_json(silent=True) or {}
    if not conversations.rename(conv_id, data.get("title", "")):
        return jsonify({"error": "not found or invalid title"}), 404
    return jsonify({"id": conv_id, "title": data.get("title", "").strip()})


@voice_bp.route("/api/voice/conversations/<conv_id>", methods=["DELETE"])
def delete_conversation(conv_id):
    if not conversations.delete(conv_id):
        return jsonify({"error": "not found"}), 404
    return jsonify({"deleted": conv_id})
