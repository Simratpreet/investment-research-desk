"""
Configuration for the Stock Watchlist & Alert System.
"""

import os
from pathlib import Path

# Load .env file
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

# --- Exchange Suffix Mapping ---
# Yahoo Finance requires a suffix for non-US exchanges. Superset covering
# every exchange code present in the merged master watchlist (curated TA
# names + international news-only names). For venues Yahoo disambiguates with
# several suffixes (e.g. Euronext, Gettex), the primary/most-common one is
# used here; the news scanner (news_alerts/scan.py) keeps its own multi-suffix
# candidate list for lookup fallback.
EXCHANGE_SUFFIXES = {
    # US
    "NASDAQ": "", "NYSE": "", "AMEX": "", "OTC": "",
    # Canada
    "TSX": ".TO", "TSXV": ".V",
    # UK / Ireland
    "LSE": ".L", "AIM": ".L", "LSIN": ".IL",
    # Germany
    "XETRA": ".DE", "XETR": ".DE", "GETTEX": ".DE", "FWB": ".F",
    # Rest of Europe
    "ENXTPA": ".PA", "EURONEXT": ".PA", "OMXSTO": ".ST", "MIL": ".MI",
    "BME": ".MC", "SIX": ".SW", "VIE": ".VI", "OSL": ".OL", "OB": ".OL",
    "GPW": ".WA",
    # Asia-Pacific
    "NSE": ".NS", "BSE": ".BO", "SGX": ".SI", "ASX": ".AX", "HKEX": ".HK",
    "TWSE": ".TW", "TPEX": ".TWO", "TSE": ".T", "KRX": ".KS", "SZSE": ".SZ",
    "HOSE": ".VN",
    # Other
    "JSE": ".JO", "BMFBOVESPA": ".SA", "BMV": ".MX", "BIST": ".IS",
}

# --- Alert Thresholds ---
EARNINGS_WARN_DAYS = 14         # Alert when earnings are within N days
EARNINGS_IMMINENT_DAYS = 1      # Urgent alert when earnings are today or tomorrow
PRICE_MOVE_THRESHOLD = 5.0     # Alert on daily move >= N% (absolute)
VOLUME_SPIKE_MULTIPLIER = 2.0  # Alert when volume >= N × 20-day average

# --- Movers (market-wide spike scanner) ---
# A "mover" needs BOTH an unusual volume day and a real price rise on the last
# completed session. Requiring both is what makes the page readable: measured
# live, either-condition alone yields 800-1,200 hits a day across these markets,
# which is unreadable and unaffordable to run an LLM over.
#
# Relaxed from 5x/5% to 3x/3% to widen the net — 5x volume *and* a 5% rise is a
# violent day, and plenty of genuine accumulation starts quieter than that.
# Expect materially more hits per run; MOVERS_ANALYSIS_MAX is what bounds the
# spend, and anything past it is marked "skipped" rather than dropped.
#
# A quiet day can still legitimately yield zero; the page renders that as a
# normal outcome, not an error.
MOVERS_MIN_RVOL       = float(os.getenv("MOVERS_MIN_RVOL", "3.0"))
MOVERS_MIN_CHANGE_PCT = float(os.getenv("MOVERS_MIN_CHANGE_PCT", "3.0"))
MOVERS_LOOKBACK       = int(os.getenv("MOVERS_LOOKBACK", "20"))
# Concurrency against Yahoo's chart endpoint. The feed paces itself globally
# (see market_scan/feed.py), so this bounds threads, not request rate.
MOVERS_MAX_WORKERS    = int(os.getenv("MOVERS_MAX_WORKERS", "8"))
# DeepSeek V4 Flash with OpenRouter's web-search plugin (":online"), so notes
# can cite what actually happened. ANALYSIS_MAX is the cost guard: notes are the
# only per-run spend, and this prompt asks for a business model plus a thesis,
# so they are not short.
MOVERS_MODEL          = os.getenv("MOVERS_MODEL", "deepseek/deepseek-v4-flash-0731:online")
MOVERS_ANALYSIS_MAX   = int(os.getenv("MOVERS_ANALYSIS_MAX", "40"))
# Notes run concurrently: each is a web-search call taking most of a minute, so
# a serial pass over a busy day would leave the page half-written for half an
# hour. Bounded, because the point is to overlap the waiting rather than to open
# every billable call at once.
MOVERS_ANALYSIS_CONCURRENCY = int(os.getenv("MOVERS_ANALYSIS_CONCURRENCY", "3"))
# How many sessions of history the page keeps and shows per market. Counted in
# stored runs rather than calendar days, so a market scanned twice a week still
# shows five scans rather than two — and pruning can't be reset by a redeploy
# restamping every file's mtime.
MOVERS_RETAIN_SESSIONS = int(os.getenv("MOVERS_RETAIN_SESSIONS", "5"))
# Symbol lists are CSV exports committed under market_scan/universes/ (override
# by dropping a file into DATA_DIR/market_scan/universes/). Past this age a scan
# still runs but is flagged, since the list predates any recent listings.
MOVERS_UNIVERSE_MAX_AGE_DAYS = float(os.getenv("MOVERS_UNIVERSE_MAX_AGE_DAYS", "120"))
# Comma-separated market keys (nasdaq,nyse,tsx) to scan on a daily
# schedule. Empty => on-demand only, from the page.
MOVERS_SCHEDULE_MARKETS = os.getenv("MOVERS_SCHEDULE_MARKETS", "")
MOVERS_SCHEDULE_HOUR    = int(os.getenv("MOVERS_SCHEDULE_HOUR", "22"))
MOVERS_SCHEDULE_MINUTE  = int(os.getenv("MOVERS_SCHEDULE_MINUTE", "30"))
# Retry-watcher cadence for a session that traded but has not been published
# yet: poll at these intervals (10 min doubling up to 4 h), then a daily check,
# until the day's bars land. Each poll is the 5-name probe, not a full scan.
MOVERS_RETRY_INTERVALS = tuple(
    int(x) for x in os.getenv(
        "MOVERS_RETRY_INTERVALS", "600,1200,2400,4800,9600,14400").split(",")
    if x.strip())
MOVERS_RETRY_DAILY_S = int(os.getenv("MOVERS_RETRY_DAILY_S", "86400"))

# --- Check Schedule ---
# Alert checks run once a day at ALERT_SCHEDULE_HOUR:MINUTE in ALERT_SCHEDULE_TZ
# (default 23:00 Europe/London = 11 PM UK time, auto-adjusting BST↔GMT), plus
# on-demand via the dashboard "Run check" button (/api/check-now). No run on boot.
ALERT_SCHEDULE_HOUR = int(os.getenv("ALERT_SCHEDULE_HOUR", "23"))
ALERT_SCHEDULE_MINUTE = int(os.getenv("ALERT_SCHEDULE_MINUTE", "0"))
ALERT_SCHEDULE_TZ = os.getenv("ALERT_SCHEDULE_TZ", "Europe/London")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
# Send alerts to Telegram? Disabled by default — alerts are recorded and shown
# on the dashboard Alerts page (grouped by run) instead. Set =1 to re-enable.
ALERTS_TELEGRAM_ENABLED = os.getenv("ALERTS_TELEGRAM_ENABLED", "").strip().lower() in ("1", "true", "yes")

# --- Auth / sessions ---
# APP_PASSWORD gates the ENTIRE app (single shared password -> signed session
# cookie). Empty => the server refuses to start (fail closed) so we can never
# accidentally deploy open. SECRET_KEY signs the session cookie; if unset, a
# random per-process key is generated (sessions reset on restart — set it in
# prod to keep users logged in across deploys). COOKIE_SECURE=1 marks the
# session cookie Secure (set it in production, where TLS terminates at the proxy).
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SECRET_KEY   = os.getenv("SECRET_KEY", "")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")

# Reject uploads larger than this (voice audio). Bounds cost/DoS on /api/voice/ask.
MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", str(25 * 1024 * 1024)))

# --- Paths ---
# Mutable state lives under DATA_DIR so it survives container redeploys on a
# mounted volume (Fly). Defaults to the repo dir for local dev, where the seed
# files already sit. SEED_* point at the image-baked copies used to populate an
# empty volume on first boot (see server.ensure_data_dir).
_REPO_DIR = os.path.dirname(__file__)
DATA_DIR = os.getenv("DATA_DIR", _REPO_DIR)

WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")
MARKET_SCAN_DIR = os.path.join(DATA_DIR, "market_scan")   # runs/ + universes/
ALERT_LOG_FILE = os.path.join(DATA_DIR, "alert_log.json")
RESEARCH_DIR   = os.path.join(DATA_DIR, "research")
SEED_WATCHLIST = os.path.join(_REPO_DIR, "watchlist.json")  # baked default

# --- Voice context source (S3) ---
# When VOICE_S3_BUCKET is set, voice_module.load_context reads <SYMBOL>/*.txt
# filings from S3 instead of the local CONTEXT_DIRS (which only exist on the
# dev Mac). The scraper microservice populates this bucket.
VOICE_S3_BUCKET = os.getenv("VOICE_S3_BUCKET", "")
AWS_REGION      = os.getenv("AWS_REGION", "us-east-1")

# --- Scraper microservice (populates VOICE_S3_BUCKET on demand) ---
# The dashboard's "Fetch filings" form proxies to this service server-side so
# the token never reaches the browser. Empty SCRAPER_URL => the form is disabled.
SCRAPER_URL   = os.getenv("SCRAPER_URL", "").rstrip("/")
SCRAPER_TOKEN = os.getenv("SCRAPER_TOKEN", "")

# --- Server ---
SERVER_HOST = "0.0.0.0"
# Honor $PORT if the platform injects one; otherwise the app's native 8088.
SERVER_PORT = int(os.getenv("PORT", "8088"))
