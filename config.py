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
EARNINGS_IMMINENT_DAYS = 1      # Urgent alert when earnings are tomorrow
PRICE_MOVE_THRESHOLD = 5.0     # Alert on daily move >= N% (absolute)
VOLUME_SPIKE_MULTIPLIER = 2.0  # Alert when volume >= N × 20-day average

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
