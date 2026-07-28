"""The per-stock note, written by Kimi K3 with web search.

A row on the Movers page says a stock traded five times its usual volume and
closed up seven percent. That is the question, not the answer. The note is what
turns the row into something worth acting on: what the business actually does,
why interest is showing up now, and whether there is a durable story — growth
accelerating, margins expanding, debt coming down — behind the move.

The one instruction that matters most is the negative one. An unexplained 5x
volume day is itself the interesting signal, so the model is told to say plainly
that it found no public reason rather than reach for a plausible-sounding
catalyst. An invented explanation is worse than none: it converts "look into
this" into "already understood" and the name gets dropped.
"""

import random
import threading
import time
from datetime import datetime, timezone

import requests

from .domain import Hit, HitAnalysis

OR_URL = "https://openrouter.ai/api/v1"

# The user's question, verbatim. Kept as one string so it reads the way it was
# asked rather than being reassembled from fragments.
QUESTION = (
    "Why did this stock is getting investor interest recently? Are there signs "
    "of revenue growth acceleration to come or margin expansion to happen or "
    "deleveraging, or something else? Explain the business model and investment "
    "thesis in brief"
)

SYSTEM = (
    "You are an equity research analyst writing a short briefing note for a "
    "private investor's daily scan of unusual market activity.\n\n"
    "Ground the note in what you can actually verify by searching. If you "
    "cannot find a public reason for the move, say so plainly — write that "
    "there is no identifiable public catalyst. Do NOT reach for a plausible "
    "explanation to fill the gap: an unexplained volume spike is itself a "
    "useful signal, and an invented catalyst destroys it by making the name "
    "look understood.\n\n"
    "Be specific about numbers and dates, cite sources as markdown links, and "
    "distinguish clearly between what is reported and what you are inferring. "
    "Keep it brief — a few short paragraphs, not a full report."
)


class AnalystError(Exception):
    pass


def build_messages(hit: Hit, market_label: str) -> list[dict]:
    """System prompt + the measured facts + the question.

    The facts block exists so the model explains *this* move rather than
    whatever it last read about the company: it gets the session, the actual
    relative volume and the actual change, not just a ticker.
    """
    cap = (f"{hit.market_cap:,.0f} {hit.currency}" if hit.market_cap else "not known")
    facts = (
        f"Company: {hit.name}\n"
        f"Ticker: {hit.ticker} ({market_label})\n"
        f"Sector: {hit.sector or 'not known'}\n"
        f"Market cap: {cap}\n"
        f"Session: {hit.session_date}\n"
        f"Measured that session: volume was {hit.rvol:.1f}x its 20-day average "
        f"({hit.volume:,.0f} shares vs {hit.avg_volume:,.0f} average), the price "
        f"closed {hit.change_pct:+.1f}% at {hit.price:,.2f} {hit.currency}, and "
        f"turnover was {hit.turnover:,.0f} {hit.currency}.\n\n"
    )
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": facts + QUESTION},
    ]


class OpenRouterAnalyst:
    """Posts to OpenRouter chat/completions, matching voice_module's call shape.

    Concurrency is bounded by a semaphore rather than a pool size so the same
    instance can be shared; 429s and 5xx retry with jittered backoff.
    """

    def __init__(self, api_key: str, model: str, market_label: str = "",
                 concurrency: int = 3, timeout: float = 300.0, retries: int = 3):
        self._key = api_key
        self._model = model
        self._market_label = market_label
        self._sem = threading.BoundedSemaphore(max(1, concurrency))
        self._timeout = timeout
        self._retries = retries

    def explain(self, hit: Hit) -> HitAnalysis:
        """A note for one hit. Never raises — failure is a status on the note,
        because losing a row's note must not cost us the row."""
        if not self._key:
            return HitAnalysis(hit.ticker, status="failed", model=self._model,
                               error="no OPENROUTER_API_KEY configured",
                               generated_at=_now())
        try:
            text = self._call(build_messages(hit, self._market_label))
        except Exception as e:
            return HitAnalysis(hit.ticker, status="failed", model=self._model,
                               error=str(e)[:300], generated_at=_now())
        if not text:
            return HitAnalysis(hit.ticker, status="failed", model=self._model,
                               error="empty response", generated_at=_now())
        return HitAnalysis(hit.ticker, summary=text, status="ok",
                           model=self._model, generated_at=_now())

    def _call(self, messages: list[dict]) -> str:
        last = "unknown"
        for attempt in range(self._retries):
            with self._sem:
                try:
                    r = requests.post(
                        f"{OR_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {self._key}",
                                 "Content-Type": "application/json"},
                        json={"model": self._model, "messages": messages},
                        timeout=self._timeout,
                    )
                except requests.RequestException as e:
                    last = str(e)
                else:
                    if r.status_code == 200:
                        msg = (r.json().get("choices") or [{}])[0].get("message") or {}
                        # content can be null on a refusal or a reasoning-only
                        # turn — coalesce rather than .strip(None).
                        return (msg.get("content") or "").strip()
                    last = f"HTTP {r.status_code}"
                    if r.status_code not in (429, 500, 502, 503, 504):
                        break
            if attempt < self._retries - 1:
                time.sleep((2 ** attempt) * (1.0 + random.random()))
        raise AnalystError(last)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
