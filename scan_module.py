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

from flask import Blueprint, jsonify, render_template, request

from config import (MARKET_SCAN_DIR, MOVERS_ANALYSIS_MAX, MOVERS_LOOKBACK,
                    MOVERS_MAX_WORKERS, MOVERS_MIN_CHANGE_PCT, MOVERS_MIN_RVOL,
                    MOVERS_MODEL, MOVERS_RETENTION_DAYS, MOVERS_UNIVERSE_TTL_DAYS)
from market_scan.domain import ScanCriteria
from market_scan.service import ScanService
from market_scan.store import ScanStore
from market_scan.universe import MARKETS
from security import client_ip, rate_limit_ok
from voice_module import _api_key   # one OpenRouter key for the whole app

scan_bp = Blueprint("movers", __name__)

RUNS_DIR = os.path.join(MARKET_SCAN_DIR, "runs")
UNIVERSE_DIR = os.path.join(MARKET_SCAN_DIR, "universes")

CRITERIA = ScanCriteria(min_rvol=MOVERS_MIN_RVOL,
                        min_change_pct=MOVERS_MIN_CHANGE_PCT,
                        lookback=MOVERS_LOOKBACK)

store = ScanStore(RUNS_DIR)
service = ScanService(store, UNIVERSE_DIR, CRITERIA,
                      api_key_fn=_api_key, model=MOVERS_MODEL,
                      max_workers=MOVERS_MAX_WORKERS,
                      analysis_max=MOVERS_ANALYSIS_MAX,
                      retention_days=MOVERS_RETENTION_DAYS,
                      universe_ttl_days=MOVERS_UNIVERSE_TTL_DAYS)


def _market(data) -> str | None:
    """The requested market key, or None if it isn't one we know.

    Validated against the registry rather than sanitised, so a bad key can never
    reach a filesystem path.
    """
    key = (data.get("market") or "").strip().lower()
    return key if key in MARKETS else None


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
        })
    return jsonify({"markets": out, "criteria": CRITERIA.to_dict()})


@scan_bp.route("/api/movers", methods=["GET"])
def get_movers():
    """The latest stored run for a market, with live scan state attached.

    This is what the page polls while a scan is running, so it must always
    answer: a market with no run yet returns an empty payload, not a 404.
    """
    key = _market(request.args)
    if key is None:
        return jsonify({"error": "unknown market"}), 400
    data = store.latest(key) or {}
    return jsonify({
        "market": key,
        "label": MARKETS[key].label,
        "session_date": data.get("session_date"),
        "generated_at": data.get("generated_at"),
        "hits": data.get("hits", []),
        "analyses": data.get("analyses", {}),
        "stats": data.get("stats", {}),
        "degraded": data.get("degraded", False),
        "universe_stale": data.get("universe_stale", False),
        "criteria": data.get("criteria") or CRITERIA.to_dict(),
        "scan": service.state(key),
    })


@scan_bp.route("/api/movers/scan", methods=["POST"])
def start_scan():
    """Kick off a scan in the background. Returns immediately; poll /api/movers."""
    if not rate_limit_ok(f"movers:{client_ip(request)}", 10, 300):
        return jsonify({"error": "Too many scans — wait a few minutes."}), 429
    key = _market(request.get_json(silent=True) or {})
    if key is None:
        return jsonify({"error": "unknown market"}), 400
    started, message = service.start(key)
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
