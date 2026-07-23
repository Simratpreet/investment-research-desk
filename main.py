"""
Stock Watchlist & Alert System — Main Daemon
Runs the web UI server and a background scheduler that checks for alerts.
"""

import json
import threading
import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import WATCHLIST_FILE, CHECK_INTERVAL_MINUTES
from alerts.earnings import check_earnings
from alerts.price_action import check_price_action
from notifier import send_batch_alerts
from server import app, SERVER_HOST, SERVER_PORT, require_auth_configured


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
    print("=" * 55)
    print("  📡 Stock Watchlist & Alert System")
    print("=" * 55)
    print(f"  Web UI:    http://localhost:{SERVER_PORT}")
    print(f"  Interval:  Every {CHECK_INTERVAL_MINUTES} minutes")
    print(f"  Watchlist: {WATCHLIST_FILE}")
    print("=" * 55)

    # Set up scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_checks,
        trigger=IntervalTrigger(minutes=CHECK_INTERVAL_MINUTES),
        id="alert_checks",
        name="Stock Alert Checks",
        next_run_time=datetime.now(),  # Run immediately on start
    )
    scheduler.start()

    # Start Flask web server (blocking)
    try:
        app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        print("\nShutting down...")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
