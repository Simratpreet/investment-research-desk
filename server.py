"""
Flask web server for managing the stock watchlist.
"""

import json
import os
import secrets
import sys
import threading
from datetime import timedelta
from flask import (Flask, request, jsonify, render_template,
                   render_template_string, session, redirect, url_for)
from config import (WATCHLIST_FILE, SERVER_HOST, SERVER_PORT, EXCHANGE_SUFFIXES,
                    ALERT_LOG_FILE, RESEARCH_DIR, APP_PASSWORD, SECRET_KEY,
                    COOKIE_SECURE, MAX_CONTENT_LENGTH, DATA_DIR, SEED_WATCHLIST,
                    SCRAPER_URL, SCRAPER_TOKEN)
from datetime import datetime, timezone
from security import client_ip, rate_limit_ok, valid_segment, resolve_within

app = Flask(__name__)
app.secret_key = SECRET_KEY or secrets.token_hex(32)
app.config.update(
    MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


# --- Static asset cache-busting ---------------------------------------------
# Mobile Safari caches JS/CSS hard and offers no easy hard-refresh, so a fix
# could sit undelivered for days. Stamp every static URL with a version derived
# from the newest static file's mtime; a deploy that changes any asset changes
# the version, forcing a fresh fetch. The HTML itself is a dynamic response and
# isn't cached, so it always carries the current version.
def _asset_version() -> str:
    latest = 0
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    for root, _, files in os.walk(static_dir):
        for f in files:
            try:
                latest = max(latest, int(os.path.getmtime(os.path.join(root, f))))
            except OSError:
                pass
    return str(latest)


ASSET_VERSION = _asset_version()


@app.context_processor
def _inject_asset_version():
    return {"asset_v": ASSET_VERSION}

# --- Auth gate (single shared password -> signed session cookie) ------------
# Covers EVERY route, including the voice blueprint and /share/* pages. Only
# the login page, logout, and the health check are reachable unauthenticated.
_PUBLIC_ENDPOINTS = {"login", "logout", "healthz", "static"}


@app.before_request
def _require_auth():
    if request.endpoint in _PUBLIC_ENDPOINTS or session.get("auth"):
        return
    # API callers get a clean 401 (their JS surfaces it); browsers get the form.
    if request.path.startswith("/api/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect(url_for("login", next=request.path))


_LOGIN_TEMPLATE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in</title><style>
  :root{color-scheme:light}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       font:16px/1.5 -apple-system,system-ui,"Segoe UI",Roboto,sans-serif;background:#f5f0eb;color:#2d2a26}
  form{background:#fff;padding:2rem;border-radius:14px;box-shadow:0 1px 3px rgba(0,0,0,.08);width:min(90vw,340px)}
  h1{font-size:1.15rem;margin:0 0 1.1rem}
  input{width:100%;box-sizing:border-box;font:inherit;padding:.7rem .8rem;border:1px solid #e5e7eb;
        border-radius:10px;background:#f9fafb;margin-bottom:.9rem}
  button{width:100%;font:inherit;font-weight:600;color:#fff;background:#c8553d;border:0;border-radius:10px;
         padding:.75rem;cursor:pointer}
  .err{color:#dc2626;font-size:.85rem;margin-bottom:.8rem;min-height:1em}
</style></head><body>
<form method="post" action="{{ action }}">
  <h1>🔒 Stock Watchlist</h1>
  <div class="err">{{ error }}</div>
  <input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
  <button type="submit">Sign in</button>
</form></body></html>"""


def _safe_next(raw: str) -> str:
    """Only allow same-site absolute paths as post-login redirect targets
    (blocks //evil.com and scheme-relative open redirects)."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = _safe_next(request.args.get("next", "/"))
    action = url_for("login", next=nxt)
    if request.method == "POST":
        if not rate_limit_ok(f"login:{client_ip(request)}", 10, 300):
            return render_template_string(
                _LOGIN_TEMPLATE, error="Too many attempts — wait a few minutes.",
                action=action), 429
        pw = request.form.get("password", "")
        if APP_PASSWORD and secrets.compare_digest(pw, APP_PASSWORD):
            session.clear()
            session["auth"] = True
            session.permanent = True
            return redirect(_safe_next(request.form.get("next")
                                       or request.args.get("next", "/")))
        return render_template_string(
            _LOGIN_TEMPLATE, error="Wrong password.", action=action), 401
    if session.get("auth"):
        return redirect(nxt)
    return render_template_string(_LOGIN_TEMPLATE, error="", action=action)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200


def require_auth_configured():
    """Fail closed: refuse to start serving without a password set, so the app
    can never be deployed publicly with the auth gate effectively open."""
    if not APP_PASSWORD:
        sys.exit(
            "FATAL: APP_PASSWORD is not set. Refusing to start with an open "
            "auth gate. Set APP_PASSWORD (and, in production, SECRET_KEY + "
            "COOKIE_SECURE=1) in the environment. See .env.example.")


def ensure_data_dir():
    """Prepare the (possibly volume-mounted) DATA_DIR for writes and seed it.

    On a fresh Fly volume DATA_DIR is empty; copy the image-baked watchlist in
    so the dashboard isn't blank on first boot. No-op locally (DATA_DIR == repo).
    """
    import shutil
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(RESEARCH_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "screener_alerts"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "news_alerts"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "uploads"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "conversations"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "todos"), exist_ok=True)
    # Movers: scan runs and the cached exchange symbol lists. Both go on the
    # volume — a redeploy must not lose a run, nor force a refetch of every
    # universe on first boot.
    os.makedirs(os.path.join(DATA_DIR, "market_scan", "runs"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "market_scan", "universes"), exist_ok=True)
    if (WATCHLIST_FILE != SEED_WATCHLIST
            and not os.path.exists(WATCHLIST_FILE)
            and os.path.exists(SEED_WATCHLIST)):
        shutil.copy(SEED_WATCHLIST, WATCHLIST_FILE)


# Voice research module — GET /voice page + POST /api/voice/ask
from voice_module import voice_bp
app.register_blueprint(voice_bp)

from todo_module import todo_bp
app.register_blueprint(todo_bp)

# Movers — GET /movers page + the market-scan API
from scan_module import scan_bp
app.register_blueprint(scan_bp)


def load_watchlist() -> list[dict]:
    try:
        with open(WATCHLIST_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_watchlist(watchlist: list[dict]):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlist, f, indent=2)


def load_alert_log() -> list[dict]:
    try:
        with open(ALERT_LOG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


@app.route("/")
def index():
    return render_template(
        "index.html",
        exchanges=list(EXCHANGE_SUFFIXES.keys()),
    )


@app.route("/api/exchanges")
def get_exchanges():
    return jsonify(EXCHANGE_SUFFIXES)


@app.route("/api/watchlist", methods=["GET"])
def get_watchlist():
    return jsonify(load_watchlist())


@app.route("/api/watchlist", methods=["POST"])
def add_stock():
    data = request.get_json()
    ticker = data.get("ticker", "").strip().upper()
    exchange = data.get("exchange", "").strip().upper()
    notes = data.get("notes", "").strip()

    if not ticker or not exchange:
        return jsonify({"error": "Ticker and exchange are required"}), 400

    if exchange not in EXCHANGE_SUFFIXES:
        return jsonify({"error": f"Unknown exchange: {exchange}"}), 400

    watchlist = load_watchlist()

    # Prevent duplicates
    for stock in watchlist:
        if stock["ticker"] == ticker and stock["exchange"] == exchange:
            return jsonify({"error": f"{ticker} ({exchange}) already in watchlist"}), 409

    # Validate ticker against Yahoo Finance
    import yfinance as yf
    suffix = EXCHANGE_SUFFIXES.get(exchange, "")
    yf_symbol = f"{ticker}{suffix}"
    try:
        info = yf.Ticker(yf_symbol).info
        company_name = info.get("longName") or info.get("shortName") or ""
        currency = info.get("currency", "")
        price = info.get("currentPrice") or info.get("regularMarketPrice")

        if not company_name and not price:
            return jsonify({"error": f"Could not find {ticker} on {exchange}. Check the ticker/exchange."}), 404
    except Exception:
        return jsonify({"error": f"Could not verify {ticker} on {exchange}. Yahoo Finance may be down."}), 502

    stock = {
        "ticker": ticker,
        "exchange": exchange,
        "company": company_name,
        "notes": notes,
        "added_date": datetime.now(timezone.utc).isoformat(),
        # Added via the dashboard -> curated: fed to both TA/alerts and news.
        "track": ["news", "ta"],
    }

    watchlist.append(stock)
    save_watchlist(watchlist)

    return jsonify(stock), 201


@app.route("/api/watchlist/<ticker>/<exchange>", methods=["DELETE"])
def remove_stock(ticker, exchange):
    watchlist = load_watchlist()
    ticker = ticker.upper()
    exchange = exchange.upper()

    new_watchlist = [
        s for s in watchlist
        if not (s["ticker"] == ticker and s["exchange"] == exchange)
    ]

    if len(new_watchlist) == len(watchlist):
        return jsonify({"error": "Stock not found"}), 404

    save_watchlist(new_watchlist)
    return jsonify({"message": f"Removed {ticker} ({exchange})"}), 200


@app.route("/api/watchlist/<ticker>/<exchange>/notes", methods=["PUT"])
def update_notes(ticker, exchange):
    data = request.get_json()
    notes = data.get("notes", "")
    watchlist = load_watchlist()
    ticker = ticker.upper()
    exchange = exchange.upper()

    for stock in watchlist:
        if stock["ticker"] == ticker and stock["exchange"] == exchange:
            stock["notes"] = notes
            save_watchlist(watchlist)
            return jsonify(stock), 200

    return jsonify({"error": "Stock not found"}), 404


@app.route("/api/watchlist/<ticker>/<exchange>/track", methods=["PUT"])
def update_track(ticker, exchange):
    """Toggle which subsystems a name feeds. Body: {"news": bool, "ta": bool}.
    "news" -> news/IR scanner; "ta" -> earnings + price-action Telegram
    alerts. Fields omitted from the body are left unchanged."""
    data = request.get_json() or {}
    ticker = ticker.upper()
    exchange = exchange.upper()
    watchlist = load_watchlist()

    for stock in watchlist:
        if stock["ticker"] == ticker and stock["exchange"] == exchange:
            current = set(stock["track"] if isinstance(stock.get("track"), list)
                          else ["news", "ta"])
            for kind in ("news", "ta"):
                if kind in data:
                    current.add(kind) if data[kind] else current.discard(kind)
            # Keep a stable, canonical order: news before ta.
            stock["track"] = [k for k in ("news", "ta") if k in current]
            save_watchlist(watchlist)
            return jsonify(stock), 200

    return jsonify({"error": "Stock not found"}), 404


# The store lives under DATA_DIR (volume) so it survives redeploys and matches
# where scan.py writes it (news_alerts/scan.py STATE_DIR). Locally DATA_DIR ==
# repo dir, so this resolves to the same news_alerts/ folder as the script.
NEWS_STORE_FILE = os.path.join(DATA_DIR, "news_alerts", "news_store.json")
NEWS_SCAN_SCRIPT = os.path.join(os.path.dirname(__file__),
                                "news_alerts", "scan.py")

# In-memory state for a background news scan triggered from the dashboard.
_news_scan = {"running": False, "started_at": None, "finished_at": None,
              "returncode": None, "message": ""}
_news_scan_lock = threading.Lock()

_SIG_ORDER = {"High": 0, "Medium": 1, "Low": 2, "": 3}

# Live handles for the running scan subprocesses so a stop endpoint can kill them.
# (subprocess.run gave no handle; Popen + communicate does.)
_scan_procs = {"news": None, "ann": None}
_scan_procs_lock = threading.Lock()


def _record_scan_failure(kind, cwd, rc, lines):
    """Append a failed scan's output to its own log file.

    Without this a scan that dies leaves no evidence anywhere: its log simply
    stops mid-run, and everything the process printed on the way out goes into a
    pipe that only the last line of ever survives — in memory, lost on restart.
    Best-effort by definition, so it can never turn a scan failure into a crash.
    """
    path = {"ann": ANN_LOG_FILE, "news": os.path.join(cwd, "logs", "scan.log")}.get(kind)
    if not path:
        return
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{stamp}  [server] scan exited with code {rc}. "
                    f"Last {min(len(lines), 40)} line(s) of its output:\n")
            for line in lines[-40:]:
                f.write(f"{stamp}    | {line}\n")
    except OSError:
        pass


def _run_scan(kind, argv, cwd, state, lock, timeout, ok_msg="Scan complete."):
    """Run a scan subprocess, tracking its handle so it can be stopped, and
    record the outcome in `state`. SIGTERM on stop — scan.py checkpoints its
    `seen` set periodically, so partial progress persists."""
    import subprocess
    with lock:
        state.update(running=True, started_at=datetime.now(timezone.utc).isoformat(),
                     finished_at=None, returncode=None, message="Scanning…", stopped=False)
    rc, tail = -1, ""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        with _scan_procs_lock:
            _scan_procs[kind] = proc
        try:
            out, err = proc.communicate(timeout=timeout)
            rc = proc.returncode
            lines = (err or out or "").strip().splitlines()
            tail = lines[-1] if lines else ""
            if rc != 0:
                # Keep the whole tail on disk, not just its last line in memory.
                # Two announcement scans died on 2026-07-29 and left no record
                # anywhere: the scan's own log stops mid-run, `message` is
                # in-process state lost on restart, and everything else went
                # into this pipe and was dropped.
                _record_scan_failure(kind, cwd, rc, lines)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.communicate()
            rc, tail = -1, f"Scan timed out after {timeout // 60} min."
            _record_scan_failure(kind, cwd, rc, [tail])
    except Exception as e:
        rc, tail = -1, f"Scan failed to launch: {e}"
    finally:
        with _scan_procs_lock:
            _scan_procs[kind] = None
    with lock:
        stopped = state.get("stopped")
        state.update(running=False, finished_at=datetime.now(timezone.utc).isoformat(),
                     returncode=rc,
                     message=("Scan stopped by user." if stopped else
                              ok_msg if rc == 0 else
                              f"Scan finished with issues (code {rc}). {tail}".strip()[:300]))


def _stop_scan(kind, state, lock):
    with lock:
        if not state["running"]:
            return jsonify({"message": "No scan is running"}), 409
        state["stopped"] = True
    with _scan_procs_lock:
        proc = _scan_procs.get(kind)
    if proc is not None:
        proc.terminate()
    return jsonify({"message": "Stopping scan…"}), 202


@app.route("/api/news", methods=["GET"])
def get_news():
    """Structured latest-news snapshot for the dashboard, newest first with
    High-significance names surfaced to the top."""
    try:
        with open(NEWS_STORE_FILE) as f:
            store = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify({"generated_at": None, "items": [], "scan": _news_scan})
    items = list((store.get("items") or {}).values())
    items.sort(key=lambda it: (_SIG_ORDER.get(it.get("significance", ""), 3),
                               it.get("scanned_at", "")),
               reverse=False)
    # Within the same significance, show most recently scanned first.
    items.sort(key=lambda it: it.get("scanned_at", ""), reverse=True)
    items.sort(key=lambda it: _SIG_ORDER.get(it.get("significance", ""), 3))
    return jsonify({"generated_at": store.get("generated_at"),
                    "items": items, "scan": _news_scan})


def _run_news_scan():
    _run_scan("news", ["python3", NEWS_SCAN_SCRIPT],
              os.path.dirname(NEWS_SCAN_SCRIPT), _news_scan, _news_scan_lock, 3600)


@app.route("/api/news/scan/stop", methods=["POST"])
def stop_news_scan():
    """Stop a running news scan (SIGTERM the subprocess)."""
    return _stop_scan("news", _news_scan, _news_scan_lock)


@app.route("/api/news", methods=["DELETE"])
def clear_news():
    """Empty the news store (the append-only reports.md history is untouched)."""
    with _news_scan_lock:
        if _news_scan["running"]:
            return jsonify({"error": "A scan is running; try again after it "
                            "finishes"}), 409
    try:
        with open(NEWS_STORE_FILE, "w") as f:
            json.dump({"items": {}, "generated_at": None}, f)
    except OSError as e:
        return jsonify({"error": f"Could not clear news: {e}"}), 500
    return jsonify({"message": "News cleared"}), 200


@app.route("/api/news/scan", methods=["POST"])
def trigger_news_scan():
    """Kick off a full news scan in the background. Returns immediately;
    poll /api/news (the `scan` field) for progress."""
    with _news_scan_lock:
        if _news_scan["running"]:
            return jsonify({"message": "A scan is already running",
                            "scan": _news_scan}), 409
    threading.Thread(target=_run_news_scan, daemon=True).start()
    return jsonify({"message": "News scan started"}), 202


# --- Screener.in announcements (India) ---

SCREENER_DIR = os.path.join(os.path.dirname(__file__), "screener_alerts")
ANN_SCAN_SCRIPT = os.path.join(SCREENER_DIR, "scan.py")
# Store + log live under DATA_DIR (volume) so they persist across redeploys and
# match where scan.py writes them (screener_alerts/scan.py STATE_DIR). Locally
# DATA_DIR == repo dir, so this resolves to the same screener_alerts/ folder.
_ANN_STATE_DIR = os.path.join(DATA_DIR, "screener_alerts")
ANN_STORE_FILE = os.path.join(_ANN_STATE_DIR, "announcements_store.json")
ANN_LOG_FILE = os.path.join(_ANN_STATE_DIR, "scan.log")

_ann_scan = {"running": False, "started_at": None, "finished_at": None,
             "returncode": None, "message": ""}
_ann_scan_lock = threading.Lock()


@app.route("/api/announcements", methods=["GET"])
def get_announcements():
    """Structured screener.in announcement digests for the dashboard, newest
    run first."""
    try:
        with open(ANN_STORE_FILE) as f:
            store = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify({"generated_at": None, "runs": [], "scan": _ann_scan})
    runs = list(reversed(store.get("runs") or []))
    return jsonify({"generated_at": store.get("generated_at"),
                    "runs": runs, "scan": _ann_scan})


@app.route("/api/announcements/log", methods=["GET"])
def get_announcements_log():
    """Tail of the screener scan log (screener_alerts/scan.log)."""
    try:
        with open(ANN_LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return jsonify({"exists": False, "lines": []})
    return jsonify({"exists": True,
                    "lines": [ln.rstrip("\n") for ln in lines[-400:]]})


def _run_ann_scan():
    # sys.executable: the venv python has pypdf, which scan.py needs.
    _run_scan("ann", [sys.executable, ANN_SCAN_SCRIPT], SCREENER_DIR,
              _ann_scan, _ann_scan_lock, 3600)


@app.route("/api/announcements/scan/stop", methods=["POST"])
def stop_ann_scan():
    """Stop a running announcement scan (SIGTERM; seen.json checkpoints persist)."""
    return _stop_scan("ann", _ann_scan, _ann_scan_lock)


@app.route("/api/announcements/scan", methods=["POST"])
def trigger_ann_scan():
    """Kick off a screener.in announcement scan in the background. Returns
    immediately; poll /api/announcements (the `scan` field) for progress."""
    with _ann_scan_lock:
        if _ann_scan["running"]:
            return jsonify({"message": "A scan is already running",
                            "scan": _ann_scan}), 409
    threading.Thread(target=_run_ann_scan, daemon=True).start()
    return jsonify({"message": "Announcement scan started"}), 202


@app.route("/api/announcements", methods=["DELETE"])
def clear_announcements():
    """Empty the announcements store (the append-only digest.md is untouched)."""
    with _ann_scan_lock:
        if _ann_scan["running"]:
            return jsonify({"error": "A scan is running; try again after it "
                            "finishes"}), 409
    try:
        with open(ANN_STORE_FILE, "w") as f:
            json.dump({"generated_at": None, "runs": []}, f)
    except OSError as e:
        return jsonify({"error": f"Could not clear announcements: {e}"}), 500
    return jsonify({"message": "Announcements cleared"}), 200


@app.route("/api/alerts", methods=["GET"])
def get_alerts():
    alerts = load_alert_log()
    # Return the full stored history, most recent first (log is capped at 500).
    return jsonify(alerts[::-1])


@app.route("/api/alerts", methods=["DELETE"])
def clear_alerts():
    with open(ALERT_LOG_FILE, "w") as f:
        json.dump([], f)
    return jsonify({"message": "Alert history cleared"}), 200


@app.route("/api/check-now", methods=["POST"])
def check_now():
    """Trigger an on-demand alert check."""
    if not rate_limit_ok(f"check-now:{client_ip(request)}", 5, 60):
        return jsonify({"error": "Too many checks — wait a minute."}), 429
    from alerts.earnings import check_earnings
    from alerts.price_action import check_price_action
    from notifier import send_batch_alerts

    # Alerts (earnings + price action → Telegram) run only over the curated
    # TA-tracked names, matching the recurring daemon in main.py. News-only
    # names (track without "ta") are never alerted on.
    watchlist = [
        s for s in load_watchlist()
        if "ta" in (s["track"] if isinstance(s.get("track"), list) else ["ta"])
    ]
    if not watchlist:
        return jsonify({"message": "Watchlist is empty", "alerts": 0}), 200

    all_alerts = []

    try:
        all_alerts.extend(check_earnings(watchlist))
    except Exception as e:
        print(f"[check-now] Earnings error: {e}")

    try:
        all_alerts.extend(check_price_action(watchlist))
    except Exception as e:
        print(f"[check-now] Price action error: {e}")

    send_batch_alerts(all_alerts)
    return jsonify({"message": f"Check complete", "alerts": len(all_alerts)}), 200


@app.route("/api/scrape", methods=["POST"])
def trigger_scrape():
    """Proxy a symbol to the scraper microservice, which downloads its filings
    from screener.in and uploads <SYMBOL>/*.txt to the voice S3 bucket. The
    service URL + token stay server-side; the browser only sends a symbol."""
    if not (SCRAPER_URL and SCRAPER_TOKEN):
        return jsonify({"error": "scraper service is not configured"}), 503
    if not rate_limit_ok(f"scrape:{client_ip(request)}", 10, 300):
        return jsonify({"error": "too many scrape requests — slow down"}), 429

    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    if not valid_segment(symbol):
        return jsonify({"error": "enter a valid symbol"}), 400

    # Pass through the scrape options the Chat form sends (counts + annual toggle).
    payload_out = {"symbol": symbol, "force": bool(data.get("force"))}
    for k in ("transcripts", "ppts"):
        if k in data:
            payload_out[k] = data[k]
    if "annual" in data:
        payload_out["annual"] = bool(data["annual"])

    import requests as _rq
    try:
        r = _rq.post(f"{SCRAPER_URL}/scrape",
                     json=payload_out,
                     headers={"Authorization": f"Bearer {SCRAPER_TOKEN}"},
                     timeout=180)
    except _rq.RequestException as e:
        return jsonify({"error": f"could not reach scraper: {str(e)[:120]}"}), 502
    try:
        payload = r.json()
    except ValueError:
        payload = {"error": f"scraper returned status {r.status_code}"}
    return jsonify(payload), r.status_code


def start_server():
    """Start the Flask dashboard (no scheduler). Port comes from the first
    CLI arg or the PORT env var, else config.SERVER_PORT — e.g.
    `python server.py 8092` to run alongside the main.py daemon."""
    require_auth_configured()
    ensure_data_dir()
    port = SERVER_PORT
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    elif os.environ.get("PORT"):
        port = int(os.environ["PORT"])
    # threaded so a ~1-min voice request can't block the dashboard/other calls.
    app.run(host=SERVER_HOST, port=port, debug=False, threaded=True)


# --- Research Notes API (folder-per-ticker) ---

def _note_path(ticker, slug):
    """Resolve RESEARCH_DIR/<ticker>/<slug>.md, or None if ticker/slug is
    unsafe or the resolved path escapes RESEARCH_DIR (path-traversal guard)."""
    if not (valid_segment(ticker) and valid_segment(slug)):
        return None
    return resolve_within(RESEARCH_DIR, ticker, f"{slug}.md")


def _ticker_dir(ticker):
    """Resolve RESEARCH_DIR/<ticker>, or None if unsafe / escapes RESEARCH_DIR."""
    if not valid_segment(ticker):
        return None
    return resolve_within(RESEARCH_DIR, ticker)


def _note_meta(ticker_dir, filename):
    """Extract metadata from a single note file."""
    filepath = os.path.join(ticker_dir, filename)
    stat = os.stat(filepath)
    with open(filepath, "r") as fh:
        content = fh.read()
    slug = filename[:-3]  # remove .md
    title = slug.replace("_", " ").title()
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            break
        elif line:
            title = line[:80]
            break
    preview = content[:300].replace("\n", " ").strip()[:200]
    return {
        "slug": slug,
        "filename": filename,
        "title": title,
        "preview": preview,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "size": len(content),
    }


@app.route("/api/research", methods=["GET"])
def list_research():
    """List all tickers with their notes."""
    os.makedirs(RESEARCH_DIR, exist_ok=True)
    tickers = []
    for d in sorted(os.listdir(RESEARCH_DIR)):
        ticker_dir = os.path.join(RESEARCH_DIR, d)
        if not os.path.isdir(ticker_dir) or d.startswith("."):
            continue
        md_files = sorted([f for f in os.listdir(ticker_dir) if f.endswith(".md")])
        if not md_files:
            continue
        # Get most recent update across all notes
        latest = max(
            os.stat(os.path.join(ticker_dir, f)).st_mtime for f in md_files
        )
        notes = [_note_meta(ticker_dir, f) for f in md_files]
        tickers.append({
            "ticker": d,
            "note_count": len(md_files),
            "notes": notes,
            "updated_at": datetime.fromtimestamp(latest, tz=timezone.utc).isoformat(),
        })
    return jsonify(tickers)


@app.route("/api/research/<ticker>/<slug>", methods=["GET"])
def get_research(ticker, slug):
    """Get a single note's content."""
    ticker = ticker.upper()
    filepath = _note_path(ticker, slug)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "Note not found"}), 404
    with open(filepath, "r") as f:
        content = f.read()
    stat = os.stat(filepath)
    return jsonify({
        "ticker": ticker,
        "slug": slug,
        "content": content,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    })


@app.route("/api/research/<ticker>/<slug>", methods=["PUT"])
def save_research(ticker, slug):
    """Create or update a note."""
    ticker = ticker.upper()
    filepath = _note_path(ticker, slug)
    if not filepath:
        return jsonify({"error": "Invalid ticker or note name"}), 400
    data = request.get_json()
    content = data.get("content", "")
    if not content.strip():
        return jsonify({"error": "Content cannot be empty"}), 400
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        f.write(content)
    return jsonify({"ticker": ticker, "slug": slug, "message": "Saved"}), 200


@app.route("/api/research/<ticker>/<slug>", methods=["DELETE"])
def delete_note(ticker, slug):
    """Delete a single note."""
    ticker = ticker.upper()
    filepath = _note_path(ticker, slug)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "Not found"}), 404
    os.remove(filepath)
    # Remove ticker folder if empty
    ticker_dir = _ticker_dir(ticker)
    if ticker_dir and os.path.isdir(ticker_dir) and not os.listdir(ticker_dir):
        os.rmdir(ticker_dir)
    return jsonify({"message": f"Deleted {slug} from {ticker}"}), 200


@app.route("/api/research/<ticker>", methods=["DELETE"])
def delete_ticker(ticker):
    """Delete all notes for a ticker."""
    import shutil
    ticker = ticker.upper()
    ticker_dir = _ticker_dir(ticker)
    # Never rmtree RESEARCH_DIR itself or anything outside it (path-traversal
    # guard): _ticker_dir returns None for unsafe names, and we require the
    # resolved path to be a strict child of RESEARCH_DIR.
    if (not ticker_dir
            or ticker_dir == os.path.realpath(RESEARCH_DIR)
            or not os.path.isdir(ticker_dir)):
        return jsonify({"error": "Not found"}), 404
    shutil.rmtree(ticker_dir)
    return jsonify({"message": f"Deleted all research for {ticker}"}), 200


# --- Share Page (read-only) ---

SHARE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ ticker }} — {{ title }}</title>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'DM Sans', -apple-system, sans-serif;
            background: #F5F0EB;
            color: #2D2A26;
            line-height: 1.7;
            padding: 0;
        }
        .header {
            background: #2D2A26;
            color: #F5F0EB;
            padding: 1.5rem 2rem;
        }
        .header .ticker {
            font-family: 'JetBrains Mono', monospace;
            color: #C8553D;
            font-size: 0.85rem;
            font-weight: 600;
            letter-spacing: 0.06em;
        }
        .header h1 {
            font-size: 1.4rem;
            font-weight: 700;
            margin-top: 0.3rem;
        }
        .header .meta {
            font-size: 0.75rem;
            color: #9B9590;
            margin-top: 0.3rem;
        }
        article {
            max-width: 780px;
            margin: 2rem auto;
            padding: 2rem 2.5rem;
            background: white;
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06);
        }
        article h1 { font-size: 1.3rem; margin: 1.2rem 0 0.5rem; font-weight: 700; }
        article h2 { font-size: 1.1rem; margin: 1.1rem 0 0.4rem; font-weight: 700; }
        article h3 { font-size: 0.95rem; margin: 1rem 0 0.35rem; font-weight: 700; color: #C8553D; }
        article p { margin-bottom: 0.6rem; }
        article ul, article ol { padding-left: 1.3rem; margin-bottom: 0.6rem; }
        article li { margin-bottom: 0.25rem; }
        article strong { font-weight: 700; }
        article hr { border: none; border-top: 1px solid #E8E0D8; margin: 1rem 0; }
        article blockquote {
            border-left: 3px solid #C8553D;
            background: #FAF7F4;
            margin: 1rem 0;
            padding: 0.75rem 1.1rem;
            border-radius: 0 8px 8px 0;
            color: #6B6560;
            font-style: italic;
            line-height: 1.7;
        }
        article blockquote p { margin-bottom: 0; }
        article table { width: 100%; border-collapse: collapse; margin: 0.8rem 0; font-size: 0.85rem; }
        article th, article td { text-align: left; padding: 0.5rem 0.75rem; border: 1px solid #E8E0D8; }
        article th { background: #FAF7F4; font-weight: 600; }
        article code {
            background: #F5F0EB;
            padding: 0.15rem 0.4rem;
            border-radius: 4px;
            font-size: 0.82rem;
            font-family: 'JetBrains Mono', monospace;
        }
        .footer {
            text-align: center;
            padding: 1.5rem;
            color: #9B9590;
            font-size: 0.72rem;
        }
        @media (max-width: 600px) {
            article { margin: 1rem; padding: 1.2rem; }
            .header { padding: 1rem 1.2rem; }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="ticker">{{ ticker }}</div>
        <h1>{{ title }}</h1>
        <div class="meta">Last updated {{ updated }}</div>
    </div>
    <article id="content"></article>
    <div class="footer">Shared from Stock Watchlist</div>
    <script>
        const md = {{ content_json|safe }};
        document.getElementById('content').innerHTML = DOMPurify.sanitize(marked.parse(md));
    </script>
</body>
</html>"""

@app.route("/share/<ticker>/<slug>")
def share_note(ticker, slug):
    """Read-only page for a single note (login required)."""
    ticker = ticker.upper()
    filepath = _note_path(ticker, slug)
    if not filepath or not os.path.exists(filepath):
        return "Note not found", 404
    with open(filepath, "r") as f:
        content = f.read()
    stat = os.stat(filepath)
    # Extract title
    title = slug.replace("_", " ").title()
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            break
        elif line:
            title = line[:80]
            break
    updated = datetime.fromtimestamp(stat.st_mtime).strftime("%d %b %Y, %H:%M")
    return render_template_string(
        SHARE_TEMPLATE,
        ticker=ticker,
        title=title,
        updated=updated,
        # Escape "</" so a note containing </script> can't break out of the
        # inline <script> block (json.dumps alone does not escape "/").
        content_json=json.dumps(content).replace("</", "<\\/"),
    )


# --- Company-wide combined page (all notes) ---

COMPANY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ ticker }} — Research Notes</title>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'DM Sans', -apple-system, sans-serif;
            background: #F5F0EB;
            color: #2D2A26;
            line-height: 1.7;
        }
        .header {
            background: #2D2A26;
            color: #F5F0EB;
            padding: 1.5rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header-left .ticker {
            font-family: 'JetBrains Mono', monospace;
            color: #C8553D;
            font-size: 0.85rem;
            font-weight: 600;
            letter-spacing: 0.06em;
        }
        .header-left h1 {
            font-size: 1.4rem;
            font-weight: 700;
            margin-top: 0.3rem;
        }
        .header-left .meta {
            font-size: 0.75rem;
            color: #9B9590;
            margin-top: 0.3rem;
        }
        .pdf-btn {
            background: #C8553D;
            color: white;
            border: none;
            padding: 0.5rem 1.2rem;
            border-radius: 8px;
            font-family: inherit;
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.15s;
        }
        .pdf-btn:hover { background: #a8432f; }
        .content-wrap {
            max-width: 780px;
            margin: 0 auto;
            padding: 1rem 0 2rem;
        }
        .note-section {
            background: white;
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06);
            padding: 2rem 2.5rem;
            margin: 1.5rem 1rem;
        }
        .note-section .note-label {
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.7rem;
            color: #9B9590;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.8rem;
            padding-bottom: 0.5rem;
            border-bottom: 1px solid #E8E0D8;
        }
        .note-section h1 { font-size: 1.3rem; margin: 1.2rem 0 0.5rem; font-weight: 700; }
        .note-section h2 { font-size: 1.1rem; margin: 1.1rem 0 0.4rem; font-weight: 700; }
        .note-section h3 { font-size: 0.95rem; margin: 1rem 0 0.35rem; font-weight: 700; color: #C8553D; }
        .note-section p { margin-bottom: 0.6rem; }
        .note-section ul, .note-section ol { padding-left: 1.3rem; margin-bottom: 0.6rem; }
        .note-section li { margin-bottom: 0.25rem; }
        .note-section strong { font-weight: 700; }
        .note-section hr { border: none; border-top: 1px solid #E8E0D8; margin: 1rem 0; }
        .note-section blockquote {
            border-left: 3px solid #C8553D;
            background: #FAF7F4;
            margin: 1rem 0;
            padding: 0.75rem 1.1rem;
            border-radius: 0 8px 8px 0;
            color: #6B6560;
            font-style: italic;
            line-height: 1.7;
        }
        .note-section blockquote p { margin-bottom: 0; }
        .note-section table { width: 100%; border-collapse: collapse; margin: 0.8rem 0; font-size: 0.85rem; }
        .note-section th, .note-section td { text-align: left; padding: 0.5rem 0.75rem; border: 1px solid #E8E0D8; }
        .note-section th { background: #FAF7F4; font-weight: 600; }
        .note-section code {
            background: #F5F0EB;
            padding: 0.15rem 0.4rem;
            border-radius: 4px;
            font-size: 0.82rem;
            font-family: 'JetBrains Mono', monospace;
        }
        .footer {
            text-align: center;
            padding: 1.5rem;
            color: #9B9590;
            font-size: 0.72rem;
        }
        @media print {
            body { background: white; }
            .header { position: static; }
            .pdf-btn { display: none !important; }
            .note-section {
                box-shadow: none;
                border: 1px solid #E8E0D8;
                break-inside: avoid;
                page-break-inside: avoid;
                margin: 1rem 0;
            }
            .footer { display: none; }
        }
        @media (max-width: 600px) {
            .note-section { margin: 1rem; padding: 1.2rem; }
            .header { padding: 1rem 1.2rem; flex-direction: column; gap: 0.8rem; align-items: flex-start; }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-left">
            <div class="ticker">{{ ticker }}</div>
            <h1>Research Notes</h1>
            <div class="meta">{{ note_count }} notes · Generated {{ generated }}</div>
        </div>
        <button class="pdf-btn" onclick="window.print()">📄 Save as PDF</button>
    </div>
    <div class="content-wrap">
        {% for note in notes %}
        <div class="note-section">
            <div class="note-label">{{ note.slug }}</div>
            <div id="note-{{ loop.index }}"></div>
        </div>
        {% endfor %}
    </div>
    <div class="footer">Shared from Stock Watchlist</div>
    <script>
        const notes = {{ notes_json|safe }};
        notes.forEach((n, i) => {
            document.getElementById('note-' + (i + 1)).innerHTML =
                DOMPurify.sanitize(marked.parse(n.content));
        });
    </script>
</body>
</html>"""


@app.route("/share/<ticker>")
def share_company(ticker):
    """Combined page with all notes for a ticker (login required)."""
    ticker = ticker.upper()
    ticker_dir = _ticker_dir(ticker)
    if not ticker_dir or not os.path.isdir(ticker_dir):
        return "Ticker not found", 404
    notes = []
    for fname in sorted(os.listdir(ticker_dir)):
        if not fname.endswith(".md"):
            continue
        filepath = os.path.join(ticker_dir, fname)
        with open(filepath, "r") as f:
            content = f.read()
        notes.append({
            "slug": fname[:-3],
            "content": content,
        })
    if not notes:
        return "No notes found", 404
    generated = datetime.now().strftime("%d %b %Y, %H:%M")
    return render_template_string(
        COMPANY_TEMPLATE,
        ticker=ticker,
        note_count=len(notes),
        generated=generated,
        notes=notes,
        notes_json=json.dumps(
            [{"slug": n["slug"], "content": n["content"]} for n in notes]
        ).replace("</", "<\\/"),
    )


if __name__ == "__main__":
    start_server()
