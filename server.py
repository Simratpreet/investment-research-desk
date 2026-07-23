"""
Flask web server for managing the stock watchlist.
"""

import json
import os
import sys
import threading
from flask import Flask, request, jsonify, render_template
from config import WATCHLIST_FILE, SERVER_HOST, SERVER_PORT, EXCHANGE_SUFFIXES, ALERT_LOG_FILE, RESEARCH_DIR
from datetime import datetime, timezone

app = Flask(__name__)

# Voice research module — GET /voice page + POST /api/voice/ask
from voice_module import voice_bp
app.register_blueprint(voice_bp)


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


NEWS_STORE_FILE = os.path.join(os.path.dirname(__file__),
                               "news_alerts", "news_store.json")
NEWS_SCAN_SCRIPT = os.path.join(os.path.dirname(__file__),
                                "news_alerts", "scan.py")

# In-memory state for a background news scan triggered from the dashboard.
_news_scan = {"running": False, "started_at": None, "finished_at": None,
              "returncode": None, "message": ""}
_news_scan_lock = threading.Lock()

_SIG_ORDER = {"High": 0, "Medium": 1, "Low": 2, "": 3}


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
    import subprocess
    with _news_scan_lock:
        _news_scan.update(running=True, started_at=datetime.now(
            timezone.utc).isoformat(), finished_at=None, returncode=None,
            message="Scanning…")
    try:
        proc = subprocess.run(
            ["python3", NEWS_SCAN_SCRIPT],
            cwd=os.path.dirname(NEWS_SCAN_SCRIPT),
            capture_output=True, text=True, timeout=3600)
        rc, msg = proc.returncode, (proc.stdout or "")[-400:]
    except subprocess.TimeoutExpired:
        rc, msg = -1, "Scan timed out after 30 min."
    except Exception as e:
        rc, msg = -1, f"Scan failed to launch: {e}"
    with _news_scan_lock:
        _news_scan.update(running=False, finished_at=datetime.now(
            timezone.utc).isoformat(), returncode=rc,
            message=("Scan complete." if rc == 0 else
                     f"Scan finished with issues (code {rc})."))


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
ANN_STORE_FILE = os.path.join(SCREENER_DIR, "announcements_store.json")
ANN_SCAN_SCRIPT = os.path.join(SCREENER_DIR, "scan.py")
ANN_LOG_FILE = os.path.join(SCREENER_DIR, "scan.log")

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
    import subprocess
    with _ann_scan_lock:
        _ann_scan.update(running=True, started_at=datetime.now(
            timezone.utc).isoformat(), finished_at=None, returncode=None,
            message="Scanning…")
    try:
        # Use this interpreter (the venv python has pypdf, which scan.py needs).
        proc = subprocess.run(
            [sys.executable, ANN_SCAN_SCRIPT],
            cwd=SCREENER_DIR,
            capture_output=True, text=True, timeout=3600)
        rc = proc.returncode
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        msg = msg[-1] if msg else ""
    except subprocess.TimeoutExpired:
        rc, msg = -1, "Scan timed out after 60 min."
    except Exception as e:
        rc, msg = -1, f"Scan failed to launch: {e}"
    with _ann_scan_lock:
        _ann_scan.update(running=False, finished_at=datetime.now(
            timezone.utc).isoformat(), returncode=rc,
            message=("Scan complete." if rc == 0 else
                     f"Scan finished with issues (code {rc}). {msg}"[:300]))


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
    # Return most recent first, limit to 100
    return jsonify(alerts[-100:][::-1])


@app.route("/api/alerts", methods=["DELETE"])
def clear_alerts():
    with open(ALERT_LOG_FILE, "w") as f:
        json.dump([], f)
    return jsonify({"message": "Alert history cleared"}), 200


@app.route("/api/check-now", methods=["POST"])
def check_now():
    """Trigger an on-demand alert check."""
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


def start_server():
    """Start the Flask dashboard (no scheduler). Port comes from the first
    CLI arg or the PORT env var, else config.SERVER_PORT — e.g.
    `python server.py 8092` to run alongside the main.py daemon."""
    os.makedirs(RESEARCH_DIR, exist_ok=True)
    port = SERVER_PORT
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    elif os.environ.get("PORT"):
        port = int(os.environ["PORT"])
    app.run(port=port, debug=False)


# --- Research Notes API (folder-per-ticker) ---

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
    filepath = os.path.join(RESEARCH_DIR, ticker, f"{slug}.md")
    if not os.path.exists(filepath):
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
    ticker_dir = os.path.join(RESEARCH_DIR, ticker)
    os.makedirs(ticker_dir, exist_ok=True)
    data = request.get_json()
    content = data.get("content", "")
    if not content.strip():
        return jsonify({"error": "Content cannot be empty"}), 400
    filepath = os.path.join(ticker_dir, f"{slug}.md")
    with open(filepath, "w") as f:
        f.write(content)
    return jsonify({"ticker": ticker, "slug": slug, "message": "Saved"}), 200


@app.route("/api/research/<ticker>/<slug>", methods=["DELETE"])
def delete_note(ticker, slug):
    """Delete a single note."""
    ticker = ticker.upper()
    filepath = os.path.join(RESEARCH_DIR, ticker, f"{slug}.md")
    if not os.path.exists(filepath):
        return jsonify({"error": "Not found"}), 404
    os.remove(filepath)
    # Remove ticker folder if empty
    ticker_dir = os.path.join(RESEARCH_DIR, ticker)
    if not os.listdir(ticker_dir):
        os.rmdir(ticker_dir)
    return jsonify({"message": f"Deleted {slug} from {ticker}"}), 200


@app.route("/api/research/<ticker>", methods=["DELETE"])
def delete_ticker(ticker):
    """Delete all notes for a ticker."""
    import shutil
    ticker = ticker.upper()
    ticker_dir = os.path.join(RESEARCH_DIR, ticker)
    if not os.path.isdir(ticker_dir):
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
        document.getElementById('content').innerHTML = marked.parse(md);
    </script>
</body>
</html>"""

@app.route("/share/<ticker>/<slug>")
def share_note(ticker, slug):
    """Public read-only page for a single note."""
    from flask import render_template_string
    ticker = ticker.upper()
    filepath = os.path.join(RESEARCH_DIR, ticker, f"{slug}.md")
    if not os.path.exists(filepath):
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
        content_json=json.dumps(content),
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
            document.getElementById('note-' + (i + 1)).innerHTML = marked.parse(n.content);
        });
    </script>
</body>
</html>"""


@app.route("/share/<ticker>")
def share_company(ticker):
    """Combined page with all notes for a ticker."""
    from flask import render_template_string
    ticker = ticker.upper()
    ticker_dir = os.path.join(RESEARCH_DIR, ticker)
    if not os.path.isdir(ticker_dir):
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
        notes_json=json.dumps([{"slug": n["slug"], "content": n["content"]} for n in notes]),
    )


if __name__ == "__main__":
    start_server()
