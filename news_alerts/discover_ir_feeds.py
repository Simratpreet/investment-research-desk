#!/usr/bin/env python3
"""
Best-effort discovery of company IR / press-release RSS feeds.

For each watchlist ticker (already resolved by scan.py into
data/names_cache.json) this script:

  1. looks up the company website via Yahoo Finance (cookie+crumb flow),
  2. scans the homepage and common IR subdomains (ir./investors./investor.)
     for advertised <link rel="alternate" type="application/rss+xml"> feeds,
  3. probes well-known feed paths used by IR platforms (e.g. Q4's
     /rss/news-releases.xml) and generic /feed, /rss,
  4. validates every candidate (must parse as RSS/Atom with >= 1 item),
  5. writes up to 2 best-scoring feeds per ticker into ir_feeds.json.

ir_feeds.json is merged, never clobbered: entries with "source": "manual"
are left untouched, and already-discovered tickers are skipped unless
--refresh is given. Add feeds by hand like:

    "NASDAQ:MSFT": {"feeds": ["https://..."], "source": "manual"}

Usage:
    python3 discover_ir_feeds.py                 # all unchecked tickers
    python3 discover_ir_feeds.py --tickers NASDAQ:MSFT,LSE:WISE
    python3 discover_ir_feeds.py --limit 20
    python3 discover_ir_feeds.py --refresh       # recheck everything
"""

import argparse
import concurrent.futures
import http.cookiejar
import json
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from scan import (BASE_DIR, RunLogger, USER_AGENT, http_request,
                  load_name_cache, WATCHLIST_FILE, NEWS_TRACK)


def load_news_universe() -> dict[str, str]:
    """Return {"EXCHANGE:TICKER": company} for every news-tracked name in the
    master watchlist. Falls back to the name cache if the master is absent."""
    try:
        rows = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {t: (e.get("company") or t)
                for t, e in load_name_cache().items()}
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        track = r["track"] if isinstance(r.get("track"), list) \
            else ["news", "ta"]
        if NEWS_TRACK not in track:
            continue
        ex = str(r.get("exchange", "")).strip().upper()
        ti = str(r.get("ticker", "")).strip()
        if ex and ti:
            out[f"{ex}:{ti}"] = (r.get("company") or "").strip() or f"{ex}:{ti}"
    return out

IR_FEEDS_FILE = BASE_DIR / "ir_feeds.json"
WORKERS = 10
PROBE_TIMEOUT = 10
MAX_FEEDS_PER_TICKER = 2

LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE | re.DOTALL)
ATTR_RE = re.compile(r"""([a-zA-Z-]+)\s*=\s*["']([^"']*)["']""")

# Substrings that make a feed URL/title look IR-relevant (higher = better).
SCORE_KEYWORDS = [
    ("news-release", 50), ("press-release", 50), ("pressrelease", 50),
    ("/ir/", 30), ("ir.", 25), ("investor", 40), ("press", 20),
    ("news", 15), ("media", 10), ("releases", 20), ("announcement", 25),
]
JUNK_KEYWORDS = ["comment", "podcast", "jobs", "career", "event"]


def yahoo_session(log: RunLogger):
    """Return (opener, crumb) for Yahoo endpoints that need auth crumbs."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", USER_AGENT)]
    try:
        opener.open("https://fc.yahoo.com", timeout=10)
    except Exception:
        pass  # 404/redirect is fine; we only need the cookie it sets
    crumb = opener.open("https://query1.finance.yahoo.com/v1/test/getcrumb",
                        timeout=10).read().decode().strip()
    if not crumb or "<" in crumb:
        raise RuntimeError("could not obtain Yahoo crumb")
    log.debug("yahoo_session_ok")
    return opener, crumb


def get_website(opener, crumb: str, yahoo_symbol: str) -> str | None:
    url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
           f"{urllib.parse.quote(yahoo_symbol)}?modules=assetProfile"
           f"&crumb={urllib.parse.quote(crumb)}")
    with opener.open(url, timeout=15) as resp:
        data = json.loads(resp.read())
    results = (data.get("quoteSummary") or {}).get("result") or []
    if results:
        return (results[0].get("assetProfile") or {}).get("website")
    return None


def fetch_text(url: str, log: RunLogger) -> str | None:
    try:
        raw = http_request(url, timeout=PROBE_TIMEOUT, retries=1, log=log,
                           what=f"probe:{url[:60]}")
        return raw.decode("utf-8", "replace")
    except Exception:
        return None


def feeds_advertised_in(page_url: str, html_text: str) -> list[str]:
    found = []
    for tag in LINK_TAG_RE.findall(html_text):
        attrs = {k.lower(): v for k, v in ATTR_RE.findall(tag)}
        if attrs.get("rel", "").lower() != "alternate":
            continue
        if "rss" not in attrs.get("type", "") and \
           "atom" not in attrs.get("type", ""):
            continue
        href = attrs.get("href")
        if href:
            found.append(urllib.parse.urljoin(page_url, href))
    return found


def validate_feed(url: str, log: RunLogger) -> dict | None:
    """Return {'url','title','items'} if url is a parseable RSS/Atom feed."""
    text = fetch_text(url, log)
    if not text or ("<rss" not in text[:2000] and "<feed" not in text[:2000]):
        return None
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return None
    items = list(root.iter("item")) + \
        list(root.iter("{http://www.w3.org/2005/Atom}entry"))
    if not items:
        return None
    title = (root.findtext("channel/title")
             or root.findtext("{http://www.w3.org/2005/Atom}title") or "")
    return {"url": url, "title": title.strip()[:120], "items": len(items)}


def score_feed(feed: dict) -> int:
    hay = (feed["url"] + " " + feed["title"]).lower()
    if any(j in hay for j in JUNK_KEYWORDS):
        return -100
    return sum(pts for kw, pts in SCORE_KEYWORDS if kw in hay)


def candidates_for(website: str) -> tuple[list[str], list[str]]:
    """Return (pages to scan for advertised feeds, direct feed URLs)."""
    parsed = urllib.parse.urlparse(website)
    domain = parsed.netloc.removeprefix("www.")
    site = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    pages = [site]
    direct = []
    for sub in ("ir", "investors", "investor"):
        pages.append(f"https://{sub}.{domain}")
        # Q4 Inc. hosted IR sites (very common for US/CA/UK listings)
        direct.append(f"https://{sub}.{domain}/rss/news-releases.xml")
    direct += [f"{site}/feed", f"{site}/rss", f"{site}/rss.xml",
               f"{site}/news/rss", f"{site}/feed.xml"]
    return pages, direct


def discover_one(ticker: str, company: str, yahoo_symbol: str,
                 opener, crumb: str, log: RunLogger) -> dict:
    entry = {"company": company, "website": None, "feeds": [],
             "source": "discovered",
             "checked_at": datetime.now(timezone.utc).isoformat(
                 timespec="seconds")}
    try:
        entry["website"] = get_website(opener, crumb, yahoo_symbol)
    except Exception as e:
        log.warn("website_lookup_failed", ticker=ticker, error=str(e)[:150])
    if not entry["website"]:
        log.info("no_website", ticker=ticker)
        return entry

    pages, direct = candidates_for(entry["website"])
    candidate_urls: list[str] = []
    for page in pages:
        html_text = fetch_text(page, log)
        if html_text:
            candidate_urls += feeds_advertised_in(page, html_text)
    candidate_urls += direct

    seen, validated = set(), []
    for url in candidate_urls:
        norm = url.rstrip("/")
        if norm in seen:
            continue
        seen.add(norm)
        feed = validate_feed(url, log)
        if feed:
            feed["score"] = score_feed(feed)
            if feed["score"] > -100:
                validated.append(feed)
    validated.sort(key=lambda f: -f["score"])
    entry["feeds"] = [f["url"] for f in validated[:MAX_FEEDS_PER_TICKER]]
    entry["feed_titles"] = [f["title"] for f in validated[:MAX_FEEDS_PER_TICKER]]
    if entry["feeds"]:
        log.info("feeds_found", ticker=ticker, feeds=entry["feeds"])
    else:
        log.info("no_feeds", ticker=ticker, website=entry["website"])
    return entry


def yahoo_symbol_for(ticker: str) -> str:
    """Reconstruct the Yahoo symbol scan.py resolved this ticker under."""
    from scan import Name, yahoo_candidates
    exchange, _, t = ticker.partition(":")
    return yahoo_candidates(Name(raw=ticker, exchange=exchange, ticker=t))[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--tickers", help="comma-separated subset")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--refresh", action="store_true",
                    help="recheck tickers already in ir_feeds.json")
    args = ap.parse_args()

    run_id = datetime.now(timezone.utc).strftime("discover-%Y%m%d-%H%M%S")
    log = RunLogger(run_id)

    universe = load_news_universe()
    if not universe:
        log.error("watchlist_empty",
                  msg="no news-tracked names in master watchlist.json")
        return 2

    try:
        existing = json.loads(IR_FEEDS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {}

    tickers = list(universe.keys())
    if args.tickers:
        wanted = {t.strip().upper() for t in args.tickers.split(",")}
        tickers = [t for t in tickers if t.upper() in wanted]
    if not args.refresh:
        tickers = [t for t in tickers
                   if t not in existing or existing[t].get("source") is None]
    tickers = [t for t in tickers
               if existing.get(t, {}).get("source") != "manual"]
    if args.limit:
        tickers = tickers[:args.limit]
    if not tickers:
        log.info("nothing_to_discover",
                 hint="all tickers checked; use --refresh to recheck")
        return 0
    log.info("discover_start", tickers=len(tickers))

    try:
        opener, crumb = yahoo_session(log)
    except Exception as e:
        log.error("yahoo_session_failed", error=str(e)[:200])
        return 2

    results, lock = {}, threading.Lock()

    def work(ticker: str):
        company = universe.get(ticker, ticker)
        try:
            entry = discover_one(ticker, company, yahoo_symbol_for(ticker),
                                 opener, crumb, log)
        except Exception as e:
            log.warn("discover_failed", ticker=ticker, error=str(e)[:200])
            entry = {"company": company, "website": None, "feeds": [],
                     "source": "discovered", "error": str(e)[:200],
                     "checked_at": datetime.now(timezone.utc).isoformat(
                         timespec="seconds")}
        with lock:
            results[ticker] = entry

    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(work, tickers))

    merged = {**existing, **results}
    tmp = IR_FEEDS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=1, ensure_ascii=False,
                              sort_keys=True), encoding="utf-8")
    tmp.replace(IR_FEEDS_FILE)

    with_feeds = sum(1 for e in merged.values() if e.get("feeds"))
    log.info("discover_done", checked=len(results),
             total_in_file=len(merged), tickers_with_feeds=with_feeds,
             path=str(IR_FEEDS_FILE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
