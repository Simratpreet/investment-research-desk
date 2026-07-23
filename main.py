"""
Stock Watchlist & Alert System — Main Daemon
Runs the web UI server and a background scheduler that checks for alerts.
"""

import json
import threading
import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import (WATCHLIST_FILE, ALERT_SCHEDULE_HOUR, ALERT_SCHEDULE_MINUTE,
                    ALERT_SCHEDULE_TZ)
from alerts.earnings import check_earnings
from alerts.price_action import check_price_action
from notifier import send_batch_alerts
from server import app, SERVER_HOST, SERVER_PORT, require_auth_configured, ensure_data_dir


def load_watchlist() -> list[dict]:
    """Names for the recurring earnings + price-action alert loop.

    The master watchlist.json is shared with the news_alerts subsystem and
    holds ~310 names, but only those tagged `track: ["...ta..."]` (the
    curated set) should drive the yfinance-heavy alert checks. Rows without
    a `track` field default to being tracked, for backward compatibility."""
    try:
        with open(WATCHLIST_FILE, "r") as f:
            rows = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    def tracked(r):
        # Absent `track` -> default on; explicit [] -> user disabled alerts.
        track = r["track"] if isinstance(r.get("track"), list) else ["ta"]
        return "ta" in track
    return [r for r in rows if isinstance(r, dict) and tracked(r)]


def run_checks():
    """Run all alert checks against the watchlist."""
    watchlist = load_watchlist()
    if not watchlist:
        print(f"[{datetime.now().strftime('%H:%M')}] Watchlist is empty, skipping checks.")
        return

    print(f"\n[{datetime.now().strftime('%H:%M')}] Running checks on {len(watchlist)} stocks...")

    all_alerts = []

    # Earnings check
    try:
        earnings_alerts = check_earnings(watchlist)
        all_alerts.extend(earnings_alerts)
        print(f"  ├─ Earnings: {len(earnings_alerts)} alerts")
    except Exception as e:
        print(f"  ├─ Earnings check failed: {e}")

    # Price action check
    try:
        price_alerts = check_price_action(watchlist)
        all_alerts.extend(price_alerts)
        print(f"  └─ Price action: {len(price_alerts)} alerts")
    except Exception as e:
        print(f"  └─ Price action check failed: {e}")

    # Send alerts
    send_batch_alerts(all_alerts)
    print(f"[{datetime.now().strftime('%H:%M')}] Check complete. Total alerts: {len(all_alerts)}")


def main():
    require_auth_configured()
    ensure_data_dir()
    print("=" * 55)
    print("  📡 Stock Watchlist & Alert System")
    print("=" * 55)
    print(f"  Web UI:    http://localhost:{SERVER_PORT}")
    print(f"  Schedule:  Daily at {ALERT_SCHEDULE_HOUR:02d}:{ALERT_SCHEDULE_MINUTE:02d} "
          f"{ALERT_SCHEDULE_TZ} (+ on-demand)")
    print(f"  Watchlist: {WATCHLIST_FILE}")
    print("=" * 55)

    # Set up scheduler: run once a day at the configured local time. No
    # next_run_time -> it does NOT run on boot; use the dashboard "Run check"
    # button (/api/check-now) for an immediate/on-demand check.
    import pytz
    scheduler = BackgroundScheduler(timezone=pytz.timezone(ALERT_SCHEDULE_TZ))
    scheduler.add_job(
        run_checks,
        trigger=CronTrigger(hour=ALERT_SCHEDULE_HOUR, minute=ALERT_SCHEDULE_MINUTE,
                            timezone=pytz.timezone(ALERT_SCHEDULE_TZ)),
        id="alert_checks",
        name="Daily Stock Alert Checks",
    )
    scheduler.start()

    # Start Flask web server (blocking). threaded=True so a ~1-min voice request
    # doesn't stall the dashboard; single process keeps the scheduler + in-memory
    # rate-limit/semaphore state coherent (do NOT run multiple workers).
    try:
        app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False,
                use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        print("\nShutting down...")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
