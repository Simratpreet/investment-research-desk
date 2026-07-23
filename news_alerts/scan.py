#!/usr/bin/env python3
"""
international-watchlist-alerts — on-demand news scanner.

Reads watchlist.txt (comma-separated EXCHANGE:TICKER pairs), finds news /
press releases / investor-relations updates from the last N hours (default 12)
for each name via Google News RSS, summarizes hits with an LLM through
OpenRouter (default: GLM 5.2), and appends a per-run report section to
reports.md (append-only).

Zero third-party dependencies — Python 3.10+ stdlib only.

Usage:
    python3 scan.py                    # full run
    python3 scan.py --limit 10         # first 10 tickers only
    python3 scan.py --tickers NASDAQ:MSFT,LSE:WISE
    python3 scan.py --window-hours 24
    python3 scan.py --no-llm           # headlines only, skip summaries
    python3 scan.py --dry-run          # don't write reports.md

Exit codes: 0 = success, 1 = completed with partial failures, 2 = fatal.
"""

import argparse
import concurrent.futures
import html
import json
import os
import random
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# Single source of truth: the merged master watchlist lives one level up in
# the stock-watchlist project root. We consume every entry tagged "news".
PROJECT_ROOT = BASE_DIR.parent
WATCHLIST_FILE = PROJECT_ROOT / "watchlist.json"
NEWS_TRACK = "news"
REPORT_FILE = BASE_DIR / "reports.md"
# Machine-readable store the dashboard reads (latest news per name, merged
# across runs so a partial --tickers scan only updates those names).
NEWS_STORE_FILE = BASE_DIR / "news_store.json"
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
NAME_CACHE_FILE = DATA_DIR / "names_cache.json"
IR_FEEDS_FILE = BASE_DIR / "ir_feeds.json"
LOCK_FILE = DATA_DIR / ".scan.lock"
METRICS_FILE = LOG_DIR / "metrics.jsonl"

DEFAULT_WINDOW_HOURS = 12
DEFAULT_MODEL = "z-ai/glm-5.2"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) watchlist-alerts/1.0"
MAX_ARTICLES_PER_NAME = 8
NEWS_WORKERS = 6
SUMMARY_WORKERS = 5   # concurrent OpenRouter calls (kept low to avoid 429s)
RESOLVE_WORKERS = 4
LOCK_STALE_SECONDS = 2 * 3600

# Exchange prefix -> candidate Yahoo Finance suffixes (tried in order).
# Covers every exchange code that appears in the merged master watchlist,
# including the curated-side codes (NSE/BSE/AIM/ENXTPA/SGX/XETRA/OB) that
# predate the international list.
YAHOO_SUFFIXES = {
    "NASDAQ": [""], "NYSE": [""], "AMEX": [""], "OTC": [""],
    "TSX": [".TO"], "TSXV": [".V"],
    "LSE": [".L"], "AIM": [".L"], "LSIN": [".IL", ".L"],
    "TWSE": [".TW"], "TPEX": [".TWO"],
    "ASX": [".AX"], "XETR": [".DE"], "XETRA": [".DE"],
    "GETTEX": [".DE", ".MU", ".F"],
    "FWB": [".F", ".DE"], "OMXSTO": [".ST"], "MIL": [".MI"],
    "TSE": [".T"], "HKEX": [".HK"], "KRX": [".KS", ".KQ"],
    "SZSE": [".SZ"], "GPW": [".WA"], "BME": [".MC"],
    "EURONEXT": [".PA", ".AS", ".BR", ".LS"],
    "ENXTPA": [".PA"], "SIX": [".SW"],
    "VIE": [".VI"], "OSL": [".OL"], "OB": [".OL"], "JSE": [".JO"],
    "BMFBOVESPA": [".SA"], "BMV": [".MX"], "BIST": [".IS"],
    "HOSE": [".VN"], "NSE": [".NS"], "BSE": [".BO"], "SGX": [".SI"],
}

# Corporate suffixes stripped to build a news search phrase.
NAME_SUFFIX_RE = re.compile(
    r"[,\s]+(incorporated|corporation|company|holdings?|group|limited|ltd\.?|"
    r"inc\.?|corp\.?|plc|p\.l\.c\.|ag|se|sa|s\.a\.?|nv|n\.v\.?|ab|asa|as|oyj|"
    r"spa|s\.p\.a\.?|kk|k\.k\.|co\.?|gmbh|kgaa|adr|class [a-c])\.?\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Logging / observability
# ---------------------------------------------------------------------------

class RunLogger:
    """Console output + structured JSONL event log for the run."""

    def __init__(self, run_id: str):
        LOG_DIR.mkdir(exist_ok=True)
        self.path = LOG_DIR / f"run-{run_id}.jsonl"
        self._fh = open(self.path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def event(self, level: str, event: str, **fields):
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": level,
            "event": event,
            **fields,
        }
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            if level in ("INFO", "WARN", "ERROR"):
                tag = {"INFO": "•", "WARN": "⚠", "ERROR": "✗"}[level]
                detail = " ".join(f"{k}={v}" for k, v in fields.items()
                                  if k not in ("traceback",))
                print(f"{tag} {event} {detail}"[:300], flush=True)

    def debug(self, event, **f): self.event("DEBUG", event, **f)
    def info(self, event, **f): self.event("INFO", event, **f)
    def warn(self, event, **f): self.event("WARN", event, **f)
    def error(self, event, **f): self.event("ERROR", event, **f)

    def close(self):
        self._fh.close()


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def load_dotenv(path: Path):
    """Load KEY=VALUE pairs from .env without overriding existing env vars."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


class HttpError(Exception):
    def __init__(self, status: int, body: str = ""):
        self.status = status
        self.body = body[:500]
        super().__init__(f"HTTP {status}: {self.body[:120]}")


def http_request(url: str, *, data: bytes | None = None,
                 headers: dict | None = None, timeout: float = 30,
                 retries: int = 3, log: RunLogger | None = None,
                 what: str = "http") -> bytes:
    """GET/POST with exponential backoff. Retries timeouts, connection
    errors, 429 and 5xx. Raises HttpError (4xx) or the last error."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            last_exc = HttpError(e.code, body)
            if e.code not in (429, 500, 502, 503, 504):
                raise last_exc  # non-retryable 4xx
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                ConnectionError, OSError) as e:
            last_exc = e
        if attempt < retries:
            delay = min(30.0, (2 ** attempt) + random.uniform(0, 1))
            if log:
                log.debug("http_retry", what=what, attempt=attempt,
                          delay=round(delay, 1), error=str(last_exc)[:200])
            time.sleep(delay)
    raise last_exc


def acquire_lock(log: RunLogger) -> bool:
    """Prevent concurrent runs. Stale locks (dead PID or too old) are broken."""
    DATA_DIR.mkdir(exist_ok=True)
    if LOCK_FILE.exists():
        try:
            info = json.loads(LOCK_FILE.read_text())
            pid, ts = int(info.get("pid", -1)), float(info.get("ts", 0))
            alive = False
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    alive = True
                except (ProcessLookupError, PermissionError):
                    alive = False
            fresh = (time.time() - ts) < LOCK_STALE_SECONDS
            if alive and fresh:
                log.error("lock_held", pid=pid,
                          age_s=int(time.time() - ts),
                          msg="another scan is running; aborting")
                return False
            log.warn("stale_lock_broken", pid=pid, alive=alive, fresh=fresh)
        except (json.JSONDecodeError, ValueError, OSError):
            log.warn("corrupt_lock_broken")
        LOCK_FILE.unlink(missing_ok=True)
    LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    return True


def release_lock():
    LOCK_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Watchlist + name resolution
# ---------------------------------------------------------------------------

@dataclass
class Name:
    raw: str                      # "NASDAQ:MSFT"
    exchange: str
    ticker: str
    company: str | None = None    # resolved long name
    search_phrase: str | None = None
    resolve_error: str | None = None
    articles: list = field(default_factory=list)
    news_error: str | None = None
    ir_feeds: list = field(default_factory=list)
    ir_errors: list = field(default_factory=list)
    summary: str | None = None
    summary_error: str | None = None


def load_watchlist(log: RunLogger) -> list[Name]:
    """Load the merged master watchlist.json and return the names this
    subsystem tracks (track contains "news"). Company names already present
    in the master are used verbatim, so the news scanner needs no per-name
    Yahoo resolution for the vast majority of the list."""
    if not WATCHLIST_FILE.exists():
        log.error("watchlist_missing", path=str(WATCHLIST_FILE))
        sys.exit(2)
    try:
        rows = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error("watchlist_parse_error", path=str(WATCHLIST_FILE),
                  error=str(e))
        sys.exit(2)
    if not isinstance(rows, list):
        log.error("watchlist_bad_shape", path=str(WATCHLIST_FILE))
        sys.exit(2)

    entries, seen, bad, skipped_track = [], set(), [], 0
    for row in rows:
        if not isinstance(row, dict):
            bad.append(str(row)[:40])
            continue
        # Distinguish an absent `track` key (default to tracking everything,
        # so a hand-added row is never silently dropped) from an explicitly
        # empty list (the user disabled both in the app — respect it).
        track = row["track"] if isinstance(row.get("track"), list) \
            else ["news", "ta"]
        if NEWS_TRACK not in track:
            skipped_track += 1
            continue
        exchange = str(row.get("exchange", "")).strip().upper()
        ticker = str(row.get("ticker", "")).strip()
        if not exchange or not ticker:
            bad.append(json.dumps(row)[:60])
            continue
        raw = f"{exchange}:{ticker}"
        if raw.upper() in seen:
            log.debug("duplicate_skipped", token=raw)
            continue
        seen.add(raw.upper())
        company = (row.get("company") or "").strip() or None
        entries.append(Name(
            raw=raw, exchange=exchange, ticker=ticker, company=company,
            search_phrase=make_search_phrase(company) if company else None))
    if bad:
        log.warn("malformed_entries_skipped", count=len(bad),
                 examples=bad[:5])
    log.info("watchlist_filtered", tracked=len(entries),
             skipped_non_news=skipped_track)
    if not entries:
        log.error("watchlist_empty", path=str(WATCHLIST_FILE))
        sys.exit(2)
    return entries


def yahoo_candidates(name: Name) -> list[str]:
    t = name.ticker.rstrip(".").replace("/", "").replace(" ", "")
    if name.exchange in ("TSX", "TSXV") and "." in t:
        t = t.replace(".", "-")  # Yahoo: FIH.U -> FIH-U.TO
    suffixes = YAHOO_SUFFIXES.get(name.exchange, [""])
    return [f"{t}{s}" for s in suffixes]


def make_search_phrase(company: str) -> str:
    phrase = company
    for _ in range(3):  # strip up to 3 stacked suffixes
        new = NAME_SUFFIX_RE.sub("", phrase).strip(" ,.")
        # Refuse to strip down to a short single token ("PWR Holdings
        # Limited" must become "PWR Holdings", not "PWR" — which would
        # match unrelated news).
        if new == phrase or not new or (" " not in new and len(new) < 5):
            break
        phrase = new
    return phrase or company


def load_name_cache() -> dict:
    try:
        return json.loads(NAME_CACHE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_name_cache(cache: dict, log: RunLogger):
    DATA_DIR.mkdir(exist_ok=True)
    tmp = NAME_CACHE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cache, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(NAME_CACHE_FILE)
    except OSError as e:
        log.warn("name_cache_save_failed", error=str(e))


def resolve_one(name: Name, log: RunLogger) -> Name:
    """Resolve EXCHANGE:TICKER -> company long name via Yahoo symbol search."""
    for symbol in yahoo_candidates(name):
        url = ("https://query1.finance.yahoo.com/v1/finance/search?"
               + urllib.parse.urlencode({"q": symbol, "quotesCount": 5,
                                         "newsCount": 0}))
        try:
            raw = http_request(url, timeout=15, retries=3, log=log,
                               what=f"resolve:{name.raw}")
            quotes = json.loads(raw).get("quotes", [])
        except Exception as e:
            name.resolve_error = f"yahoo lookup failed: {e}"
            continue
        for q in quotes:
            if q.get("symbol", "").upper() == symbol.upper():
                company = q.get("longname") or q.get("shortname")
                if company:
                    name.company = company
                    name.search_phrase = make_search_phrase(company)
                    name.resolve_error = None
                    return name
        name.resolve_error = "no matching symbol on Yahoo"
    log.debug("resolve_failed", name=name.raw, error=name.resolve_error)
    return name


def resolve_names(names: list[Name], log: RunLogger):
    cache = load_name_cache()
    pending = []
    for n in names:
        # Master watchlist already carries the company name for most rows;
        # trust it and skip the network round-trip entirely.
        if n.company:
            continue
        hit = cache.get(n.raw)
        if hit and hit.get("company"):
            n.company = hit["company"]
            n.search_phrase = hit.get("search_phrase") or make_search_phrase(
                n.company)
        else:
            pending.append(n)
    log.info("resolve_start", cached=len(names) - len(pending),
             pending=len(pending))
    if pending:
        with concurrent.futures.ThreadPoolExecutor(RESOLVE_WORKERS) as pool:
            list(pool.map(lambda n: resolve_one(n, log), pending))
        for n in pending:
            if n.company:
                cache[n.raw] = {"company": n.company,
                                "search_phrase": n.search_phrase,
                                "resolved_at": datetime.now(
                                    timezone.utc).isoformat()}
        save_name_cache(cache, log)
    unresolved = [n for n in names if not n.company]
    if unresolved:
        log.warn("resolve_unresolved", count=len(unresolved),
                 examples=[n.raw for n in unresolved[:8]])


# ---------------------------------------------------------------------------
# News fetch (company IR feeds + Google News RSS)
# ---------------------------------------------------------------------------

def load_ir_feeds(log: RunLogger) -> dict[str, list[str]]:
    """ticker -> list of feed URLs, from ir_feeds.json (see
    discover_ir_feeds.py). Missing/corrupt file just means no IR feeds."""
    try:
        data = json.loads(IR_FEEDS_FILE.read_text(encoding="utf-8"))
        return {t: e.get("feeds", []) for t, e in data.items()
                if e.get("feeds")}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, AttributeError) as e:
        log.warn("ir_feeds_unreadable", path=str(IR_FEEDS_FILE),
                 error=str(e)[:150])
        return {}


ATOM = "{http://www.w3.org/2005/Atom}"


def _parse_feed_time(raw: str):
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)          # RFC 822 (RSS)
    except (ValueError, TypeError):
        try:
            dt = datetime.fromisoformat(raw.strip())  # ISO 8601 (Atom)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_ir_articles(name: Name, feed_url: str, cutoff, log: RunLogger
                      ) -> list[dict]:
    """Fetch one company IR/press-release feed (RSS or Atom); return
    in-window articles tagged ir=True. Failures are recorded on the name
    but never raised."""
    try:
        raw = http_request(feed_url, timeout=20, retries=2, log=log,
                           what=f"ir:{name.raw}")
        root = ET.fromstring(raw)
    except Exception as e:
        name.ir_errors.append(f"{feed_url}: {str(e)[:120]}")
        log.warn("ir_feed_failed", name=name.raw, feed=feed_url,
                 error=str(e)[:150])
        return []
    source = (root.findtext("channel/title")
              or root.findtext(f"{ATOM}title") or "company IR feed").strip()
    out = []
    for item in list(root.iter("item")) + list(root.iter(f"{ATOM}entry")):
        title = html.unescape(
            (item.findtext("title") or item.findtext(f"{ATOM}title")
             or "").strip())
        link = (item.findtext("link") or "").strip()
        if not link:  # Atom: link lives in an attribute
            for lk in item.findall(f"{ATOM}link"):
                if lk.get("rel") in (None, "alternate"):
                    link = lk.get("href", "")
                    break
        pub = _parse_feed_time(item.findtext("pubDate")
                               or item.findtext(f"{ATOM}published")
                               or item.findtext(f"{ATOM}updated") or "")
        if not title or (pub and pub < cutoff):
            continue
        if pub is None:
            continue  # undated items can't be windowed; skip
        out.append({"title": title, "source": source, "link": link,
                    "published": pub.isoformat(timespec="minutes"),
                    "ir": True})
    return out


def _title_key(title: str) -> str:
    return re.sub(r"\W+", "", title.lower())[:80]


def fetch_news(name: Name, window_hours: int, log: RunLogger) -> Name:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    seen_titles = set()

    # Company's own IR / press-release feeds first — they are the
    # authoritative source and must survive the per-name article cap.
    for feed_url in name.ir_feeds:
        for art in fetch_ir_articles(name, feed_url, cutoff, log):
            key = _title_key(art["title"])
            if key and key not in seen_titles:
                seen_titles.add(key)
                name.articles.append(art)
    name.articles = name.articles[:MAX_ARTICLES_PER_NAME]

    query = f'"{name.search_phrase}"' if name.search_phrase else name.raw
    when = f"when:{window_hours}h"
    url = ("https://news.google.com/rss/search?"
           + urllib.parse.urlencode({"q": f"{query} {when}", "hl": "en-US",
                                     "gl": "US", "ceid": "US:en"}))
    try:
        raw = http_request(url, timeout=20, retries=3, log=log,
                           what=f"news:{name.raw}")
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        name.news_error = f"rss parse error: {e}"
        log.warn("news_fetch_failed", name=name.raw, error=name.news_error)
        return name
    except Exception as e:
        name.news_error = str(e)[:200]
        log.warn("news_fetch_failed", name=name.raw, error=name.news_error)
        return name

    for item in root.iter("item"):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        pub_raw = item.findtext("pubDate") or ""
        try:
            pub = parsedate_to_datetime(pub_raw)
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pub = None
        if pub and pub < cutoff:
            continue  # belt-and-braces on top of when:Xh
        # Google appends " - Publisher" to titles; drop it for dedup/display.
        display = re.sub(r"\s+-\s+[^-]{2,60}$", "", title).strip() or title
        key = _title_key(display)
        if not key or key in seen_titles:
            continue
        seen_titles.add(key)
        name.articles.append({
            "title": display,
            "source": source,
            "link": link,
            "published": pub.isoformat(timespec="minutes") if pub else "?",
            "ir": False,
        })
        if len(name.articles) >= MAX_ARTICLES_PER_NAME:
            break
    return name


# ---------------------------------------------------------------------------
# LLM summarization (OpenRouter)
# ---------------------------------------------------------------------------

class LlmClient:
    """OpenRouter chat client with retries and a circuit breaker: after 3
    consecutive hard failures (auth / bad model / repeated 5xx) it stops
    calling out and every later summary degrades gracefully to headlines."""

    def __init__(self, api_key: str, model: str, log: RunLogger):
        self.api_key = api_key
        self.model = model
        self.log = log
        self.consecutive_failures = 0
        self.tripped_reason: str | None = None
        self.calls_ok = 0
        self.calls_failed = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self._lock = threading.Lock()

    @staticmethod
    def _extract_text(data: dict) -> str:
        """Pull the assistant text out of an OpenRouter response, tolerating
        reasoning-model quirks (null content, text under a 'reasoning' field,
        content returned as a list of parts). Raises with a legible reason
        when there is genuinely nothing usable."""
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("response had no choices")
        choice = choices[0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        # Some providers return content as a list of {type,text} parts.
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content
                if isinstance(p, dict))
        text = (content or "").strip()
        if not text:
            # Reasoning models sometimes leave visible content empty and put
            # everything in a reasoning field; use it rather than fail.
            reasoning = msg.get("reasoning") or msg.get("reasoning_content")
            text = (reasoning or "").strip()
        if not text:
            finish = choice.get("finish_reason") or "unknown"
            raise ValueError(
                f"empty completion (finish_reason={finish}; try a larger "
                f"max_tokens or a non-reasoning model)")
        return text

    def summarize(self, name: Name, window_hours: int) -> str:
        with self._lock:
            if self.tripped_reason:
                raise RuntimeError(f"circuit open: {self.tripped_reason}")
        articles = "\n".join(
            f"- [{'COMPANY IR FEED — ' if a.get('ir') else ''}"
            f"{a['source'] or 'unknown source'}] {a['title']} "
            f"(published {a['published']})"
            for a in name.articles)
        prompt = (
            f"You are an equity-news analyst. Company: {name.company or name.raw} "
            f"(ticker {name.raw}). Below are news headlines from the last "
            f"{window_hours} hours.\n\n{articles}\n\n"
            "Write a 2-4 sentence factual summary of what happened, focusing on "
            "anything material: press releases, investor-relations updates, "
            "earnings, guidance, M&A, contracts, regulatory or management news. "
            "Items marked 'COMPANY IR FEED' come directly from the company's "
            "own investor-relations feed and should be weighted highest. "
            "Ignore generic market commentary and analyst listicles. "
            "End with one line exactly in the form "
            "'Significance: High|Medium|Low — <short reason>'. "
            "Do not invent facts not implied by the headlines.")
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            # GLM 5.2 is a reasoning model: it spends tokens on hidden
            # reasoning before the visible answer. Too small a budget and it
            # returns finish_reason=length with null content. Give headroom.
            "max_tokens": 2000,
            "temperature": 0.2,
        }).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost/international-watchlist-alerts",
            "X-Title": "international-watchlist-alerts",
        }
        try:
            raw = http_request(OPENROUTER_URL, data=payload, headers=headers,
                               timeout=120, retries=3, log=self.log,
                               what=f"llm:{name.raw}")
            data = json.loads(raw)
            if "error" in data and not data.get("choices"):
                raise HttpError(int(data["error"].get("code", 500) or 500),
                                str(data["error"]))
            # Count tokens first so usage is recorded even if the body is
            # awkward to parse.
            usage = data.get("usage", {}) or {}
            with self._lock:
                self.tokens_in += usage.get("prompt_tokens", 0) or 0
                self.tokens_out += usage.get("completion_tokens", 0) or 0
            text = self._extract_text(data)
            with self._lock:
                self.calls_ok += 1
                self.consecutive_failures = 0
            return text
        except Exception as e:
            with self._lock:
                self.calls_failed += 1
                self.consecutive_failures += 1
                hard_auth = isinstance(e, HttpError) and e.status in (401, 402,
                                                                      403, 404)
                if hard_auth or self.consecutive_failures >= 3:
                    self.tripped_reason = (
                        f"{'auth/model error' if hard_auth else '3 consecutive failures'}: "
                        f"{str(e)[:160]}")
                    self.log.error("llm_circuit_tripped",
                                   reason=self.tripped_reason,
                                   hint=("check OPENROUTER_API_KEY / "
                                         "OPENROUTER_MODEL"))
            raise


def summarize_all(names_with_news: list[Name], llm: LlmClient | None,
                  window_hours: int, log: RunLogger):
    if llm is None:
        for n in names_with_news:
            n.summary_error = "LLM disabled (--no-llm or missing API key)"
        return

    # Summaries dominate wall-clock (~15s each on a reasoning model), so run
    # them concurrently. LlmClient is thread-safe (locked counters + circuit
    # breaker); once the breaker trips, in-flight calls fail fast. Concurrency
    # is capped (SUMMARY_WORKERS) to stay under OpenRouter rate limits.
    def one(n: Name):
        try:
            n.summary = llm.summarize(n, window_hours)
            log.info("summary_ok", name=n.raw, articles=len(n.articles))
        except Exception as e:
            n.summary_error = str(e)[:200]
            log.warn("summary_failed", name=n.raw, error=n.summary_error)

    workers = min(SUMMARY_WORKERS, len(names_with_news))
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, names_with_news))


# ---------------------------------------------------------------------------
# Report (append-only markdown)
# ---------------------------------------------------------------------------

def render_report(run_id: str, started: datetime, names: list[Name],
                  window_hours: int, model: str, status: str,
                  llm: LlmClient | None, duration_s: float) -> str:
    hits = [n for n in names if n.articles]
    news_failed = [n for n in names if n.news_error]
    unresolved = [n for n in names if not n.company]
    lines = [
        "",
        "---",
        "",
        f"## Scan {started.astimezone().strftime('%Y-%m-%d %H:%M %Z')} "
        f"(run `{run_id}`)",
        "",
        f"- **Window:** last {window_hours}h · **Names scanned:** "
        f"{len(names)} · **With news:** {len(hits)} · "
        f"**Status:** {status} · **Duration:** {duration_s:.0f}s",
        f"- **Model:** `{model}`" + (
            f" · LLM ok/failed: {llm.calls_ok}/{llm.calls_failed}"
            f" · tokens in/out: {llm.tokens_in}/{llm.tokens_out}"
            if llm else " · LLM: disabled"),
        "",
    ]
    if not hits:
        lines.append(f"_No news found in the last {window_hours}h for any "
                     f"watchlist name._\n")
    for n in sorted(hits, key=lambda x: -len(x.articles)):
        title = f"### {n.raw} — {n.company or 'unresolved name'}"
        lines.append(title)
        lines.append("")
        if n.summary:
            lines.append(n.summary)
        else:
            lines.append(f"> ⚠ Summary unavailable "
                         f"({n.summary_error or 'unknown error'}). "
                         f"Headlines listed below.")
        lines.append("")
        for a in n.articles:
            src = f" — {a['source']}" if a["source"] else ""
            tag = "**[IR]** " if a.get("ir") else ""
            lines.append(f"- {tag}[{a['title']}]({a['link']}){src} "
                         f"({a['published']})")
        lines.append("")
    ir_failed = [n for n in names if n.ir_errors]
    if ir_failed:
        lines.append(f"**⚠ IR feed fetch failed for {len(ir_failed)} "
                     f"name(s)** (Google News still checked): "
                     + ", ".join(f"`{n.raw}`" for n in ir_failed[:20])
                     + (" …" if len(ir_failed) > 20 else ""))
        lines.append("")
    if news_failed:
        lines.append(f"**⚠ News lookup failed for {len(news_failed)} "
                     f"name(s):** "
                     + ", ".join(f"`{n.raw}`" for n in news_failed[:20])
                     + (" …" if len(news_failed) > 20 else ""))
        lines.append("")
    if unresolved:
        lines.append(f"**⚠ Could not resolve company name for "
                     f"{len(unresolved)} ticker(s)** (searched by raw "
                     f"ticker instead): "
                     + ", ".join(f"`{n.raw}`" for n in unresolved[:20])
                     + (" …" if len(unresolved) > 20 else ""))
        lines.append("")
    return "\n".join(lines)


def append_report(section: str, log: RunLogger):
    is_new = not REPORT_FILE.exists()
    with open(REPORT_FILE, "a", encoding="utf-8") as fh:
        if is_new:
            fh.write("# International Watchlist — News & IR Alerts\n\n"
                     "Append-only report. Newest scans at the bottom.\n")
        fh.write(section)
        fh.flush()
        os.fsync(fh.fileno())
    log.info("report_appended", path=str(REPORT_FILE),
             bytes=len(section.encode("utf-8")))


SIGNIFICANCE_RE = re.compile(
    r"Significance:\s*(High|Medium|Low)\b\s*[—\-:]?\s*(.*)$",
    re.IGNORECASE | re.MULTILINE)


def parse_significance(summary: str | None) -> tuple[str, str]:
    """Pull the (level, reason) out of an LLM summary's trailing
    'Significance: High — ...' line. Returns ("", "") if absent."""
    if not summary:
        return "", ""
    m = SIGNIFICANCE_RE.search(summary)
    if not m:
        return "", ""
    return m.group(1).capitalize(), m.group(2).strip()


def update_news_store(hits: list[Name], run_id: str, started: datetime,
                      window_hours: int, log: RunLogger):
    """Upsert each name's latest news into news_store.json (keyed by
    EXCHANGE:TICKER), so the dashboard can render structured news. Merged,
    not clobbered — a partial scan only touches its own names. Written
    atomically."""
    try:
        store = json.loads(NEWS_STORE_FILE.read_text(encoding="utf-8"))
        if not isinstance(store, dict):
            store = {}
    except (FileNotFoundError, json.JSONDecodeError):
        store = {}
    items = store.setdefault("items", {}) if "items" in store else {}
    store = {"items": items}

    scanned_at = started.astimezone().isoformat(timespec="minutes")
    for n in hits:
        level, reason = parse_significance(n.summary)
        # Strip the trailing Significance line from the displayed summary
        # (it's surfaced separately as a badge).
        body = n.summary
        if body:
            body = SIGNIFICANCE_RE.sub("", body).strip()
        items[n.raw] = {
            "ticker": n.ticker,
            "exchange": n.exchange,
            "company": n.company or "",
            "summary": body or "",
            "summary_error": n.summary_error or "",
            "significance": level,
            "significance_reason": reason,
            "article_count": len(n.articles),
            "has_ir": any(a.get("ir") for a in n.articles),
            "articles": n.articles,
            "scanned_at": scanned_at,
            "run_id": run_id,
            "window_hours": window_hours,
        }
    store["generated_at"] = scanned_at
    tmp = NEWS_STORE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(store, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(NEWS_STORE_FILE)
        log.info("news_store_updated", path=str(NEWS_STORE_FILE),
                 names=len(items), updated=len(hits))
    except OSError as e:
        log.warn("news_store_write_failed", error=str(e))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    ap.add_argument("--limit", type=int, help="scan only the first N names")
    ap.add_argument("--tickers", help="comma-separated subset, e.g. "
                                      "NASDAQ:MSFT,LSE:WISE")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip LLM summaries (headlines only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print report to stdout, don't append to reports.md")
    args = ap.parse_args()

    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%d-%H%M%S")
    log = RunLogger(run_id)
    log.info("run_start", run_id=run_id, window_h=args.window_hours,
             dry_run=args.dry_run)

    # Shared project .env (stock-watchlist/.env) first, then any local
    # override inside news_alerts/.
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv(BASE_DIR / ".env")
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    model = os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL).strip()

    llm: LlmClient | None = None
    if args.no_llm:
        log.info("llm_disabled", reason="--no-llm flag")
    elif not api_key:
        log.error("llm_key_missing",
                  msg="OPENROUTER_API_KEY not set (env or .env). "
                      "Run will continue in headlines-only mode; summaries "
                      "will be marked unavailable.")
    else:
        llm = LlmClient(api_key, model, log)

    if not acquire_lock(log):
        return 2

    # Make sure the lock is released on Ctrl-C / SIGTERM too.
    def _bail(signum, _frame):
        log.error("interrupted", signal=signum)
        release_lock()
        sys.exit(2)
    signal.signal(signal.SIGINT, _bail)
    signal.signal(signal.SIGTERM, _bail)

    try:
        names = load_watchlist(log)
        if args.tickers:
            wanted = {t.strip().upper() for t in args.tickers.split(",")}
            names = [n for n in names if n.raw.upper() in wanted]
            missing = wanted - {n.raw.upper() for n in names}
            if missing:
                log.warn("tickers_not_in_watchlist", tickers=sorted(missing))
        if args.limit:
            names = names[:args.limit]
        if not names:
            log.error("nothing_to_scan")
            return 2
        log.info("watchlist_loaded", names=len(names))

        resolve_names(names, log)

        ir_map = load_ir_feeds(log)
        for n in names:
            n.ir_feeds = ir_map.get(n.raw, [])
        log.info("ir_feeds_loaded",
                 names_with_feeds=sum(1 for n in names if n.ir_feeds),
                 feeds_total=sum(len(n.ir_feeds) for n in names))

        log.info("news_scan_start", names=len(names),
                 window_h=args.window_hours)
        with concurrent.futures.ThreadPoolExecutor(NEWS_WORKERS) as pool:
            list(pool.map(lambda n: fetch_news(n, args.window_hours, log),
                          names))
        hits = [n for n in names if n.articles]
        news_failed = [n for n in names if n.news_error]
        log.info("news_scan_done", with_news=len(hits),
                 failed=len(news_failed),
                 articles=sum(len(n.articles) for n in hits))

        summarize_all(hits, llm, args.window_hours, log)

        # Run status: FAILED if nothing could be checked at all,
        # PARTIAL on any per-name failure, else SUCCESS. An explicit
        # --no-llm run is not a failure; a missing API key is.
        summaries_failed = ([n for n in hits if not n.summary]
                            if llm else [])
        if len(news_failed) == len(names):
            status, exit_code = "FAILED", 2
        elif (news_failed or summaries_failed or (llm and llm.tripped_reason)
              or any(n.ir_errors for n in names)):
            status, exit_code = "PARTIAL", 1
        elif not args.no_llm and not api_key and hits:
            status, exit_code = "PARTIAL", 1
        else:
            status, exit_code = "SUCCESS", 0

        duration = (datetime.now(timezone.utc) - started).total_seconds()
        section = render_report(run_id, started, names, args.window_hours,
                                model if (llm or api_key) else "(disabled)",
                                status, llm, duration)
        if args.dry_run:
            print(section)
            log.info("dry_run_no_write")
        else:
            append_report(section, log)
            update_news_store(hits, run_id, started, args.window_hours, log)

        metrics = {
            "run_id": run_id,
            "started_at": started.isoformat(timespec="seconds"),
            "duration_s": round(duration, 1),
            "window_hours": args.window_hours,
            "names_total": len(names),
            "names_resolved": sum(1 for n in names if n.company),
            "names_with_news": len(hits),
            "articles_total": sum(len(n.articles) for n in hits),
            "ir_feeds_names": sum(1 for n in names if n.ir_feeds),
            "ir_articles": sum(1 for n in hits for a in n.articles
                               if a.get("ir")),
            "ir_feed_failures": sum(len(n.ir_errors) for n in names),
            "news_failed": len(news_failed),
            "summaries_ok": sum(1 for n in hits if n.summary),
            "summaries_failed": len(summaries_failed),
            "llm_tokens_in": llm.tokens_in if llm else 0,
            "llm_tokens_out": llm.tokens_out if llm else 0,
            "llm_circuit_tripped": bool(llm and llm.tripped_reason),
            "status": status,
            "exit_code": exit_code,
            "dry_run": args.dry_run,
        }
        LOG_DIR.mkdir(exist_ok=True)
        with open(METRICS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(metrics) + "\n")
        log.info("run_done", **{k: v for k, v in metrics.items()
                                if k not in ("started_at", "dry_run")})
        return exit_code
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        log.error("run_fatal", error=str(e)[:300],
                  traceback=traceback.format_exc())
        return 2
    finally:
        release_lock()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
