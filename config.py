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
CHECK_INTERVAL_MINUTES = 60     # How often to run checks

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

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
WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")
ALERT_LOG_FILE = os.path.join(os.path.dirname(__file__), "alert_log.json")
RESEARCH_DIR   = os.path.join(os.path.dirname(__file__), "research")

# --- Server ---
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8088
