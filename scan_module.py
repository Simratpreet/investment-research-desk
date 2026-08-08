"""Movers — market-wide spike scanner, Flask blueprint.

GET  /movers                    the page
GET  /api/movers/markets        the market registry + each one's scan state
GET  /api/movers?market=<key>   latest stored run for a market (the poll target)
POST /api/movers/scan           {market}  start a scan
POST /api/movers/scan/stop      {market}  stop a running scan

All routes sit behind the app-wide auth gate. The blueprint is deliberately
thin: orchestration, locking and progress all live in market_scan.service, the
same way todo_module defers to todo_store.
"""

import os
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request

from config import (MARKET_SCAN_DIR, MOVERS_ANALYSIS_CONCURRENCY,
                    MOVERS_ANALYSIS_MAX, MOVERS_LOOKBACK, MOVERS_MAX_WORKERS,
                    MOVERS_MIN_CHANGE_PCT, MOVERS_MIN_RVOL, MOVERS_MODEL,
                    MOVERS_RETAIN_SESSIONS, MOVERS_RETRY_DAILY_S,
                    MOVERS_RETRY_INTERVALS, MOVERS_UNIVERSE_MAX_AGE_DAYS)
from market_scan.domain import ScanCriteria
from market_scan.service import ScanService
from market_scan.store import ScanStore
from market_scan.universe import MARKETS
from security import client_ip, rate_limit_ok
from voice_module import _api_key   # one OpenRouter key for the whole app

scan_bp = Blueprint("movers", __name__)

RUNS_DIR = os.path.join(MARKET_SCAN_DIR, "runs")
# Optional override, searched before the exports committed with the package —
# so a refreshed CSV can be dropped onto the volume without a redeploy.
UNIVERSE_DIR = os.path.join(MARKET_SCAN_DIR, "universes")

CRITERIA = ScanCriteria(min_rvol=MOVERS_MIN_RVOL,
                        min_change_pct=MOVERS_MIN_CHANGE_PCT,
                        lookback=MOVERS_LOOKBACK)

store = ScanStore(RUNS_DIR)
service = ScanService(store, UNIVERSE_DIR, CRITERIA,
                      api_key_fn=_api_key, model=MOVERS_MODEL,
                      max_workers=MOVERS_MAX_WORKERS,
                      analysis_max=MOVERS_ANALYSIS_MAX,
                      analysis_concurrency=MOVERS_ANALYSIS_CONCURRENCY,
                      retain_sessions=MOVERS_RETAIN_SESSIONS,
                      universe_max_age_days=MOVERS_UNIVERSE_MAX_AGE_DAYS,
                      retry_intervals=MOVERS_RETRY_INTERVALS,
                      retry_daily_s=MOVERS_RETRY_DAILY_S)


def _market(data) -> str | None:
    """The requested market key, or None if it isn't one we know.

    Validated against the registry rather than sanitised, so a bad key can never
    reach a filesystem path.
    """
    key = (data.get("market") or "").strip().lower()
    return key if key in MARKETS else None


def _session_date(data) -> str | None:
    """An explicit backfill date, or None. Strictly validated so a garbage
    string can never reach a filename — the store builds
    `<market>_<session_date>.json` from it.
    """
    raw = (data.get("session_date") or "").strip()
    if not raw:
        return None
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    return raw


def _backfill_rejected(market: str, session_date: str) -> str | None:
    """Error message when a manual backfill target sits outside the retained
    window (its run file would be pruned before notes could be written), else
    None."""
    if not session_date:
        return None
    newer = [d for d in store.list_runs(market) if d > session_date]
    if len(newer) >= MOVERS_RETAIN_SESSIONS:
        return (f"{session_date} is outside the retained window — only the "
                f"newest {MOVERS_RETAIN_SESSIONS} sessions per market are kept, "
                "so its results would be pruned before notes could be written. "
                "Backfill within the retention window.")
    return None


@scan_bp.route("/movers")
def movers_page():
    return render_template("movers.html")


@scan_bp.route("/api/movers/markets", methods=["GET"])
def get_markets():
    """The registry plus, for each market, its scan state and last run summary —
    enough for the page to render its selector before any run is chosen."""
    states = service.states()
    out = []
    for key, market in MARKETS.items():
        runs = store.list_runs(key)
        out.append({
            "key": key,
            "label": market.label,
            "currency": market.currency,
            "scan": states.get(key),
            "last_session": runs[0] if runs else None,
            "runs": len(runs),
            # A traded-but-unpublished day, shown so the page can say "waiting
            # on data" instead of implying the market is quiet.
            "pending": store.pending(key),
        })
    return jsonify({"markets": out, "criteria": CRITERIA.to_dict()})


@scan_bp.route("/api/movers", methods=["GET"])
def get_movers():
    """A market's retained sessions, newest first, with live scan state attached.

    Every stored session is returned, not just the newest, so the page shows a
    week of movers rather than a single day — one quiet session then reads as a
    quiet session rather than as an empty page.

    This is what the page polls while a scan is running, so it must always
    answer: a market with no run yet returns an empty payload, not a 404.
    """
    key = _market(request.args)
    if key is None:
        return jsonify({"error": "unknown market"}), 400
    runs = store.recent(key, MOVERS_RETAIN_SESSIONS)
    latest = runs[0] if runs else {}

    hits, analyses, sessions = [], {}, []
    for run in runs:
        session = run.get("session_date") or ""
        run_hits = run.get("hits") or []
        hits.extend(run_hits)
        # Namespaced by session: the same ticker can spike on two days, and a
        # bare ticker key would let one day's note overwrite the other's.
        for ticker, analysis in (run.get("analyses") or {}).items():
            analyses[f"{session}|{ticker}"] = analysis
        sessions.append({
            "session_date": session,
            "hits": len(run_hits),
            "generated_at": run.get("generated_at"),
            "degraded": run.get("degraded", False),
            "stats": run.get("stats", {}),
        })

    return jsonify({
        "market": key,
        "label": MARKETS[key].label,
        "session_date": latest.get("session_date"),
        "generated_at": latest.get("generated_at"),
        "sessions": sessions,
        "retained": MOVERS_RETAIN_SESSIONS,
        "hits": hits,
        "analyses": analyses,
        "stats": latest.get("stats", {}),
        "degraded": latest.get("degraded", False),
        "universe_stale": latest.get("universe_stale", False),
        "criteria": latest.get("criteria") or CRITERIA.to_dict(),
        "scan": service.state(key),
        # The newest day this market traded that no source has published yet.
        # The page shows "traded, waiting on data" while this exists and there
        # is no completed run for the same date.
        "pending": store.pending(key),
    })


@scan_bp.route("/api/movers/scan", methods=["POST"])
def start_scan():
    """Kick off a scan in the background. Returns immediately; poll /api/movers.

    `session_date` (YYYY-MM-DD) backfills that specific session — a day Yahoo
    never published, or a day that predates the page — instead of the market's
    calendar target. `force` re-scans the target even when already stored.
    """
    if not rate_limit_ok(f"movers:{client_ip(request)}", 10, 300):
        return jsonify({"error": "Too many scans — wait a few minutes."}), 429
    body = request.get_json(silent=True) or {}
    key = _market(body)
    if key is None:
        return jsonify({"error": "unknown market"}), 400
    session_date = _session_date(body)
    if (body.get("session_date") or "").strip() and session_date is None:
        return jsonify({"error": "session_date must be YYYY-MM-DD"}), 400
    rejected = _backfill_rejected(key, session_date)
    if rejected:
        return jsonify({"error": rejected}), 400
    # Without `force` the run checks first and stops if the exchange has
    # published nothing newer than the session already stored.
    started, message = service.start(key, force=bool(body.get("force")),
                                     session_date=session_date)
    if not started:
        # Matches the News/Announcements contract: a second scan is a 409.
        return jsonify({"message": message, "scan": service.state(key)}), 409
    return jsonify({"message": message, "scan": service.state(key)}), 202


@scan_bp.route("/api/movers/scan/stop", methods=["POST"])
def stop_scan():
    key = _market(request.get_json(silent=True) or {})
    if key is None:
        return jsonify({"error": "unknown market"}), 400
    if not service.stop(key):
        return jsonify({"message": "No scan is running"}), 409
    return jsonify({"message": "Stopping scan…"}), 202
