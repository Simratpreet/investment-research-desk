"""
Notification module.
Sends alerts via Telegram bot and logs them locally.
Falls back to console output if Telegram is not configured.

Delivery is rate-limit aware:
  * Sends are serialized and throttled to ~1 message/sec (Telegram's per-chat
    limit); bursts above that return HTTP 429.
  * 429 responses are honored via `retry_after` and retried, so messages are
    not silently dropped.
  * When a batch exceeds DIGEST_THRESHOLD new alerts, they are combined into
    one (or a few) digest messages chunked under Telegram's 4096-char limit,
    rather than sent one-by-one — faster, cheaper, and immune to rate limits.
"""

import json
import threading
import time
import requests
from datetime import datetime, timezone
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALERT_LOG_FILE

# --- Rate-limit / batching tunables ---
TELEGRAM_MIN_INTERVAL = 1.1     # seconds between messages (per-chat ~1/sec)
TELEGRAM_MAX_CHARS = 3900       # stay safely under Telegram's 4096 hard cap
DIGEST_THRESHOLD = 8            # > this many new alerts -> send a digest
SEND_RETRIES = 3               # attempts per message on 429 / transient error

_send_lock = threading.Lock()   # serialize sends (scheduler + check-now thread)
_last_send_ts = [0.0]           # monotonic timestamp of the last send


def _throttle():
    """Block until at least TELEGRAM_MIN_INTERVAL has passed since the last
    send. Caller must hold _send_lock."""
    wait = TELEGRAM_MIN_INTERVAL - (time.monotonic() - _last_send_ts[0])
    if wait > 0:
        time.sleep(wait)
    _last_send_ts[0] = time.monotonic()


def send_telegram(message: str) -> bool:
    """Send one message via the Telegram bot, throttled and 429-aware.
    Returns True on delivery, False if unconfigured or all retries failed."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    for attempt in range(1, SEND_RETRIES + 1):
        try:
            with _send_lock:
                _throttle()
                resp = requests.post(url, json=payload, timeout=15)

            if resp.status_code == 429:
                # Too Many Requests — Telegram tells us how long to wait.
                retry_after = 1
                try:
                    retry_after = int(resp.json()
                                      .get("parameters", {})
                                      .get("retry_after", 1))
                except Exception:
                    pass
                print(f"[telegram] 429 rate-limited, waiting {retry_after}s "
                      f"(attempt {attempt}/{SEND_RETRIES})")
                time.sleep(min(retry_after, 30) + 0.5)
                continue

            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"[telegram] send error (attempt {attempt}/{SEND_RETRIES}): {e}")
            time.sleep(1.5 * attempt)
    return False


def log_alert(alert: dict):
    """Append alert to alert_log.json (rolling last 500)."""
    try:
        with open(ALERT_LOG_FILE, "r") as f:
            log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log = []

    log.append(alert)

    # Keep last 500 alerts
    if len(log) > 500:
        log = log[-500:]

    with open(ALERT_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


def get_alert_log() -> list[dict]:
    """Get the alert history."""
    try:
        with open(ALERT_LOG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def send_alert(alert: dict):
    """Send a single alert through all channels and log it. (Kept for one-off
    use; batch sends go through send_batch_alerts.)"""
    message = alert.get("message", "Unknown alert")
    log_alert(alert)
    sent = send_telegram(message)
    print(f"[ALERT{' → Telegram' if sent else ''}] {message}")


def _chunk_digest(alerts: list[dict]) -> list[str]:
    """Pack alert messages into as few Telegram-sized chunks as possible."""
    header = (f"📊 <b>{len(alerts)} watchlist alerts</b> — "
              f"{datetime.now(timezone.utc).strftime('%d %b %H:%M UTC')}")
    chunks, cur = [], header
    for alert in alerts:
        line = alert.get("message", "Unknown alert")
        addition = "\n\n" + line
        if len(cur) + len(addition) > TELEGRAM_MAX_CHARS:
            chunks.append(cur)
            cur = line
        else:
            cur += addition
    if cur:
        chunks.append(cur)
    return chunks


def _send_digest(alerts: list[dict]):
    chunks = _chunk_digest(alerts)
    for i, chunk in enumerate(chunks):
        prefix = f"(part {i + 1}/{len(chunks)})\n" if len(chunks) > 1 else ""
        if not send_telegram(prefix + chunk):
            print(f"[ALERT-DIGEST] delivery failed for part {i + 1}; "
                  f"{len(alerts)} alerts still saved to history.")


def send_batch_alerts(alerts: list[dict]):
    """Send multiple alerts, deduplicating against today's history. Small
    batches go out as individual (throttled) messages; large batches are
    combined into digest message(s) to stay under Telegram rate limits.
    Every new alert is logged to history regardless of delivery mode."""
    if not alerts:
        return

    # Dedup within the same day by type:ticker:exchange.
    recent = get_alert_log()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    recent_keys = {
        f"{r.get('type')}:{r.get('ticker')}:{r.get('exchange')}"
        for r in recent if r.get("timestamp", "").startswith(today)
    }

    new_alerts = []
    for alert in alerts:
        key = f"{alert.get('type')}:{alert.get('ticker')}:{alert.get('exchange')}"
        if key not in recent_keys:
            new_alerts.append(alert)
            recent_keys.add(key)

    if not new_alerts:
        print(f"[notifier] No new alerts (all {len(alerts)} already sent today)")
        return

    # Persist every new alert first (history + tomorrow's dedup) so a delivery
    # failure never loses the record.
    for alert in new_alerts:
        log_alert(alert)

    n = len(new_alerts)
    if n <= DIGEST_THRESHOLD:
        print(f"[notifier] Sending {n} alert(s) individually...")
        for alert in new_alerts:
            msg = alert.get("message", "Unknown alert")
            if not send_telegram(msg):
                print(f"[ALERT] {msg}")
    else:
        print(f"[notifier] Sending {n} alerts as digest "
              f"(> {DIGEST_THRESHOLD} threshold)...")
        _send_digest(new_alerts)
