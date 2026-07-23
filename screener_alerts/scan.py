#!/usr/bin/env python3
"""
Hourly scan of the logged-in user's screener.in watchlist for new
stock-exchange announcements, summarized via OpenRouter (x-ai/grok-4.5).

Project files (all alongside this script):
  cookie.txt          - Cookie header value copied from a logged-in browser session
  openrouter_key.txt   - OpenRouter API key (fallback; primary is ../.env)
  seen.json           - state file of announcement URLs already processed (auto-managed)
  digest.md           - running log of hourly digests (auto-managed)
  scan.log            - timestamped run log, tail it or view from the dashboard (auto-managed)
"""
import argparse
import io
import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pypdf import PdfReader

CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CONFIG_DIR.parent          # stock-watchlist/
# Mutable state (seen/pending/store/digest/log) persists on the mounted volume
# (DATA_DIR) in cloud so `seen` survives redeploys — otherwise every deploy wipes
# it and the next scan re-discovers all announcements as "new". Falls back to
# CONFIG_DIR locally. Secrets/config (cookie, key) stay in CONFIG_DIR.
STATE_DIR = (Path(os.environ["DATA_DIR"]) / "screener_alerts"
             if os.environ.get("DATA_DIR") else CONFIG_DIR)
STATE_DIR.mkdir(parents=True, exist_ok=True)
COOKIE_FILE = CONFIG_DIR / "cookie.txt"
KEY_FILE = CONFIG_DIR / "openrouter_key.txt"
SEEN_FILE = STATE_DIR / "seen.json"
DIGEST_FILE = STATE_DIR / "digest.md"
PENDING_FILE = STATE_DIR / "pending.json"
# Structured store the dashboard reads (one entry per scan run, newest last).
STORE_FILE = STATE_DIR / "announcements_store.json"
MAX_STORE_RUNS = 50
# Timestamped run log (tail it, or view from the dashboard). Shared by
# annual_reports.py, which reuses scan.log().
LOG_FILE = STATE_DIR / "scan.log"
LOG_MAX_BYTES = 2_000_000       # ~2 MB; older lines are trimmed past this

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
MAX_SEEN = 20000
OPENROUTER_MODEL = "x-ai/grok-4.5"
REQUEST_DELAY_SECONDS = 1.5
MAX_RETRIES = 3
MAX_WORKERS = 2
GLOBAL_COOLDOWN_SECONDS = 30
PDF_FETCH_DELAY_SECONDS = 1.0
PDF_MAX_PAGES = 6           # pages read per filing (more source -> more detail)
PDF_MAX_CHARS = 4000        # chars of filing text fed to the model per filing
PDF_MAX_TOTAL_PAGES = 40

# Shared circuit breaker: if ANY thread hits a 429, every thread pauses together
# instead of each one independently retrying into the same rate limit — that
# uncoordinated retry-storm (10 unthrottled workers each retrying on their own) is
# what caused the earlier IP ban, not concurrency itself.
_cooldown_until = 0.0
_cooldown_lock = threading.Lock()


def _respect_cooldown():
    with _cooldown_lock:
        until = _cooldown_until
    remaining = until - time.time()
    if remaining > 0:
        log(f"  [cooldown] pausing {remaining:.0f}s (global backoff from a recent 429)")
        time.sleep(remaining)


def _trigger_cooldown(seconds):
    global _cooldown_until
    with _cooldown_lock:
        _cooldown_until = max(_cooldown_until, time.time() + seconds)

COMPANY_LINK_RE = re.compile(r'href="/company/([A-Za-z0-9\.\-&]+)/[^"]*"')
H1_RE = re.compile(r'<h1[^>]*>\s*([^<]+?)\s*</h1>')
ANNOUNCEMENTS_TAB_RE = re.compile(
    r'<div id="company-announcements-tab">(.*?)</div>\s*</div>\s*</div>', re.S
)
ENTRY_RE = re.compile(
    r'<a href="([^"]+)"\s*\n?\s*target="_blank"[^>]*>\s*(.*?)\s*(?:<div class="ink-600 smaller">\s*([^<]*?)\s*</div>)?\s*</a>',
    re.S,
)


_log_lock = threading.Lock()


def log(msg):
    """Print to stderr (captured by the server when launched from the UI) AND
    append a timestamped line to scan.log. File logging is best-effort — it
    never raises, so a disk hiccup can't derail a scan."""
    print(msg, file=sys.stderr)
    try:
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _log_lock:
            # Cheap size cap: once the log passes LOG_MAX_BYTES, keep the most
            # recent half so it stays bounded without external rotation.
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
                LOG_FILE.write_bytes(LOG_FILE.read_bytes()[-LOG_MAX_BYTES // 2:])
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"{ts}  {msg}\n")
    except Exception:
        pass


TRANSIENT_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError)


def fetch(url, cookie=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if cookie:
        req.add_header("Cookie", cookie)

    delay = REQUEST_DELAY_SECONDS
    for attempt in range(1, MAX_RETRIES + 1):
        _respect_cooldown()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                final_url = resp.geturl()
                body = resp.read().decode("utf-8", errors="replace")
            return final_url, body
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _trigger_cooldown(GLOBAL_COOLDOWN_SECONDS)
                if attempt < MAX_RETRIES:
                    log(f"  [retry] 429 on {url}, backing off {delay:.1f}s")
                    time.sleep(delay)
                    delay *= 3
                    continue
            raise
        except TRANSIENT_ERRORS as e:
            if attempt < MAX_RETRIES:
                log(f"  [retry] transient error on {url}: {e}, retrying in {delay:.1f}s")
                time.sleep(delay)
                delay *= 3
                continue
            raise


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    delay = REQUEST_DELAY_SECONDS
    for attempt in range(1, MAX_RETRIES + 1):
        _respect_cooldown()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _trigger_cooldown(GLOBAL_COOLDOWN_SECONDS)
                if attempt < MAX_RETRIES:
                    time.sleep(delay)
                    delay *= 3
                    continue
            raise
        except TRANSIENT_ERRORS as e:
            if attempt < MAX_RETRIES:
                log(f"  [retry] transient error on {url}: {e}, retrying in {delay:.1f}s")
                time.sleep(delay)
                delay *= 3
                continue
            raise


def extract_pdf_text(url):
    """Download an announcement PDF and pull out its text. Discards anything at or
    beyond PDF_MAX_TOTAL_PAGES (annual reports, investor decks, etc. — long documents
    that aren't worth the extraction time/tokens); for eligible short filings, only
    reads the first PDF_MAX_PAGES pages/PDF_MAX_CHARS chars. Returns None on any
    failure or when discarded, so callers fall back to screener's own blurb."""
    try:
        data = fetch_bytes(url)
        reader = PdfReader(io.BytesIO(data))
        page_count = len(reader.pages)
        if page_count >= PDF_MAX_TOTAL_PAGES:
            log(f"  [skip] PDF at {url} has {page_count} pages (>= {PDF_MAX_TOTAL_PAGES}), discarding")
            return None
        text_parts = []
        for page in reader.pages[:PDF_MAX_PAGES]:
            text_parts.append(page.extract_text() or "")
        text = re.sub(r"\s+", " ", " ".join(text_parts)).strip()
        return text[:PDF_MAX_CHARS] if text else None
    except Exception as e:
        log(f"  [warn] PDF extraction failed for {url}: {e}")
        return None


def enrich_with_pdf_text(new_entries, on_checkpoint):
    """Fetch and extract the underlying PDF for each new announcement that hasn't
    been attempted yet, so the digest can be built from the actual filing instead of
    just screener's often-terse one-line blurb. Checkpoints periodically like the
    main scan, since this also makes network calls and can be interrupted."""
    pending_indices = [i for i, e in enumerate(new_entries) if not e.get("pdf_done")]
    for n, i in enumerate(pending_indices):
        e = new_entries[i]
        e["pdf_text"] = extract_pdf_text(e["url"])
        e["pdf_done"] = True
        if (n + 1) % CHECKPOINT_EVERY == 0 or n == len(pending_indices) - 1:
            on_checkpoint(new_entries)
            log(f"  [checkpoint] PDF text fetched for {n + 1}/{len(pending_indices)} new announcements")
        if n < len(pending_indices) - 1:
            time.sleep(PDF_FETCH_DELAY_SECONDS)


def load_text(path):
    if not path.exists():
        return None
    val = path.read_text(encoding="utf-8").strip()
    return val or None


def load_cookie():
    """screener.in session Cookie header, resolved from the environment first
    (SCREENER_COOKIE — how it's supplied in the cloud, easy to refresh via
    `fly secrets set`), then a local cookie.txt fallback for dev."""
    env_val = os.environ.get("SCREENER_COOKIE", "").strip()
    if env_val:
        return env_val
    return load_text(COOKIE_FILE)


def load_openrouter_key():
    """OpenRouter API key, resolved from a single project-wide source:
    an OPENROUTER_API_KEY already in the environment, then the shared
    stock-watchlist/.env, then a local openrouter_key.txt fallback."""
    env_val = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_val:
        return env_val
    dotenv = PROJECT_ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY=") and not line.startswith("#"):
                val = line.split("=", 1)[1].strip().strip("'\"")
                if val:
                    return val
    return load_text(KEY_FILE)


def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()).get("seen", []))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen(seen_set):
    trimmed = list(seen_set)[-MAX_SEEN:]
    SEEN_FILE.write_text(json.dumps({"seen": trimmed}, indent=0))


def load_pending():
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text()).get("pending", [])
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_pending(entries):
    if entries:
        PENDING_FILE.write_text(json.dumps({"pending": entries}, indent=0))
    elif PENDING_FILE.exists():
        PENDING_FILE.unlink()


def get_watchlist_companies(cookie):
    final_url, body = fetch("https://www.screener.in/watchlist/", cookie=cookie)
    if "/register/" in final_url or "/login/" in final_url:
        raise RuntimeError(
            "screener.in redirected to login — the session cookie is missing or expired. "
            f"Refresh {COOKIE_FILE} with a fresh Cookie header."
        )
    codes = []
    seen_codes = set()
    for code in COMPANY_LINK_RE.findall(body):
        if code not in seen_codes:
            seen_codes.add(code)
            codes.append(code)
    return codes


def get_company_announcements(code):
    url = f"https://www.screener.in/company/{code}/"
    try:
        _, body = fetch(url)
    except urllib.error.HTTPError as e:
        log(f"  [warn] {code}: HTTP {e.code} fetching company page, skipping")
        return None, []

    name_match = H1_RE.search(body)
    name = name_match.group(1).strip() if name_match else code

    tab_match = ANNOUNCEMENTS_TAB_RE.search(body)
    if not tab_match:
        return name, []

    entries = []
    for link, title, blurb in ENTRY_RE.findall(tab_match.group(1)):
        title_clean = re.sub(r"\s+", " ", title).strip()
        blurb_clean = re.sub(r"\s+", " ", blurb).strip() if blurb else ""
        if blurb_clean:
            blurb_clean = re.sub(r"^\S+\s*-\s*", "", blurb_clean)  # strip leading "2m - " age prefix
        entries.append({"company": name, "code": code, "url": link, "title": title_clean, "blurb": blurb_clean})
    return name, entries


def summarize_with_openrouter(new_entries, api_key):
    lines = []
    for e in new_entries:
        detail = f" — {e['blurb']}" if e['blurb'] else ""
        pdf_text = e.get("pdf_text")
        source = f"\n  Filing text: {pdf_text}" if pdf_text else ""
        lines.append(f"- [{e['company']}] {e['title']}{detail}{source}")
    bullet_list = "\n".join(lines)

    prompt = (
        "You are briefing an Indian investor on new stock-exchange announcements from their "
        "screener.in watchlist. Below is the raw list (company, title, screener's short blurb, "
        "and — where available — an excerpt of the actual filing text pulled from the underlying "
        "PDF). Prefer the filing text over the blurb when both are present, since the blurb is "
        "often a generic acknowledgment with no real detail.\n\n"
        "Write a DETAILED digest:\n"
        "- Group by company (put the company name in bold on its own line).\n"
        "- For each material announcement, write a substantive summary — typically 3–6 sentences "
        "— that captures the specifics, not just the headline. Pull out concrete figures and "
        "terms wherever the source provides them, for example:\n"
        "    • Financial results: revenue, EBITDA, PAT/net profit, margins, EPS, and YoY / QoQ "
        "changes; segment performance; notable one-offs.\n"
        "    • Dividends / buybacks: amount per share, record and payment dates, total payout.\n"
        "    • M&A / investments / orders / contracts: counterparties, deal size, stake %, "
        "consideration, key conditions, expected timeline, and strategic rationale.\n"
        "    • Management / board changes: names, roles, effective dates, stated reason.\n"
        "    • Fundraising / debt: instrument, amount, coupon or pricing, and use of proceeds.\n"
        "    • Ratings / defaults / litigation / regulatory: agency, old vs new rating, amounts, "
        "and current status.\n"
        "- Where the source supports it, add a sentence on WHY it matters (impact on the business "
        "or what to watch next).\n"
        "- Skip genuinely routine, non-material filings (e.g. newspaper publication copies, "
        "trading-window closure notices, duplicate acknowledgments) unless nothing else exists "
        "for that company — for those, a single line is fine.\n"
        "- Base everything STRICTLY on the blurb and filing text — never invent numbers, names, "
        "or facts. If the filing announces something but discloses no specifics, say what was "
        "announced and note that details were not provided, rather than padding.\n"
        "- Be factual, specific, and thorough; no disclaimers, no generic filler.\n\n"
        f"{bullet_list}"
    )

    req_body = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 16000,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=req_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
    delay = REQUEST_DELAY_SECONDS
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"].get("content")
            if not content:
                reason = data["choices"][0].get("finish_reason")
                if attempt < MAX_RETRIES:
                    log(f"  [retry] OpenRouter returned empty content (finish_reason={reason}), retrying in {delay:.1f}s")
                    time.sleep(delay)
                    delay *= 3
                    continue
                raise RuntimeError(f"OpenRouter returned empty content after {MAX_RETRIES} attempts (finish_reason={reason})")
            return content.strip()
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_HTTP_CODES and attempt < MAX_RETRIES:
                log(f"  [retry] OpenRouter HTTP {e.code}, retrying in {delay:.1f}s")
                time.sleep(delay)
                delay *= 3
                continue
            raise
        except TRANSIENT_ERRORS as e:
            if attempt < MAX_RETRIES:
                log(f"  [retry] OpenRouter transient error: {e}, retrying in {delay:.1f}s")
                time.sleep(delay)
                delay *= 3
                continue
            raise


def append_digest(text):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ts = __import__("subprocess").check_output(
        ["date", "+%Y-%m-%d %H:%M %Z"]
    ).decode().strip()
    with DIGEST_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n## {ts}\n\n{text}\n")


def update_store(new_entries, digest_text):
    """Append this run to announcements_store.json (structured, read by the
    dashboard): the GLM digest markdown plus the raw source filings (company,
    title, link) so the UI can render summaries and link through. Merged, not
    clobbered; capped to the last MAX_STORE_RUNS runs; written atomically."""
    from datetime import datetime, timezone
    try:
        store = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        runs = store.get("runs", []) if isinstance(store, dict) else []
    except (FileNotFoundError, json.JSONDecodeError):
        runs = []
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")
    items = [{"company": e["company"], "code": e["code"], "title": e["title"],
              "url": e["url"], "blurb": e.get("blurb", "")} for e in new_entries]
    runs.append({"ts": ts, "count": len(new_entries),
                 "digest": digest_text, "items": items})
    runs = runs[-MAX_STORE_RUNS:]
    tmp = STORE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"generated_at": ts, "runs": runs},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STORE_FILE)


CHECKPOINT_EVERY = 20


def _fetch_company_task(code):
    """Worker-thread body: self-paces (so each of the MAX_WORKERS threads sends
    requests roughly REQUEST_DELAY_SECONDS apart) and only does network I/O + parsing
    — no shared-state mutation happens here, so no locks are needed around it."""
    time.sleep(REQUEST_DELAY_SECONDS)
    try:
        _, entries = get_company_announcements(code)
        return code, entries, None
    except Exception as e:
        return code, [], e


def fetch_all_announcements(codes, seen, new_entries, persist_pending=True):
    """Fetch every company's announcements using a small pool of self-paced worker
    threads (polite to screener.in — see the circuit breaker above for what happens
    on a 429), consumed one at a time on the main thread so `seen`/`new_entries`
    mutation and checkpointing stay race-free. Checkpoints periodically so a crash
    mid-run loses neither the scan progress nor the announcements already found
    (which would otherwise be marked seen but never surface in a digest). Pass
    persist_pending=False for baseline seeding, where new_entries are throwaway and
    shouldn't leak into a later real run's digest."""
    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_company_task, code): code for code in codes}
        for future in as_completed(futures):
            code = futures[future]
            _, entries, err = future.result()
            completed += 1
            if err is not None:
                log(f"  [warn] {code}: {err}, skipping")
            else:
                for e in entries:
                    if e["url"] not in seen:
                        new_entries.append(e)
                        seen.add(e["url"])
            if completed % CHECKPOINT_EVERY == 0:
                save_seen(seen)
                if persist_pending:
                    save_pending(new_entries)
                log(f"  [checkpoint] {completed}/{len(codes)} companies processed, {len(new_entries)} pending")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N watchlist companies (for testing)")
    parser.add_argument("--seed-only", action="store_true", help="Mark all currently visible announcements as seen without summarizing (baseline seed)")
    args = parser.parse_args()

    log("=" * 60)
    log(f"SCAN START  model={OPENROUTER_MODEL}"
        + (f"  limit={args.limit}" if args.limit else "")
        + ("  seed-only" if args.seed_only else ""))

    cookie = load_cookie()
    api_key = load_openrouter_key()

    if not cookie:
        log("No screener.in cookie. Set the SCREENER_COOKIE env var (cloud) or "
            f"paste the Cookie header into {COOKIE_FILE} (local).")
        sys.exit(1)

    try:
        codes = get_watchlist_companies(cookie)
    except RuntimeError as e:
        log(str(e))
        append_digest(f"**Error:** {e}")
        sys.exit(1)

    if not codes:
        log("Watchlist parsed but no companies found — the page markup may not match the scraper's expectations.")
        sys.exit(1)

    log(f"Watchlist companies found: {len(codes)}")
    if args.limit:
        codes = codes[: args.limit]
        log(f"Limiting this run to first {len(codes)} companies")

    seen = load_seen()
    new_entries = [] if args.seed_only else load_pending()
    if new_entries:
        log(f"Resuming {len(new_entries)} pending announcements left over from an interrupted prior run")
    fetch_all_announcements(codes, seen, new_entries, persist_pending=not args.seed_only)

    if args.seed_only:
        log(f"Seeding baseline: marking {len(new_entries)} currently visible announcements as seen (no summary generated).")
        save_seen(seen)
        save_pending([])
        return

    if not new_entries:
        log("No new announcements since last run.")
        save_seen(seen)
        return

    log(f"New announcements: {len(new_entries)}")

    enrich_with_pdf_text(new_entries, save_pending)

    if api_key:
        try:
            digest = summarize_with_openrouter(new_entries, api_key)
        except Exception as e:
            log(f"[warn] OpenRouter summarization failed: {e}")
            digest = "_(OpenRouter summarization failed — raw list below)_\n\n" + "\n".join(
                f"- [{e['company']}]({e['url']}) {e['title']}" for e in new_entries
            )
    else:
        log(f"No OpenRouter key found at {KEY_FILE}, logging raw announcements only.")
        digest = "\n".join(f"- [{e['company']}]({e['url']}) {e['title']}" for e in new_entries)

    append_digest(digest)
    update_store(new_entries, digest)
    save_seen(seen)
    save_pending([])
    log("Digest appended.")


if __name__ == "__main__":
    main()
