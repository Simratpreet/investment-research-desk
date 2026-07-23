"""Scraper microservice — populate the voice S3 bucket for one symbol on request.

POST /scrape  {"symbol": "VENUSPIPES", "transcripts": 4, "annual": true, "force": false}
  Authorization: Bearer <SCRAPER_TOKEN>
  -> scrapes screener.in, extracts transcript/annual-report text, uploads
     <SYMBOL>/*.txt to S3, returns a summary.

GET /healthz -> {"ok": true}

Deployed as a separate Fly app; the main watchlist app proxies to it (the token
stays server-side, never in the browser). Single small container.
"""

import logging
import os

from flask import Flask, jsonify, request

import scrape

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("scraper_service")

app = Flask(__name__)
SCRAPER_TOKEN = os.getenv("SCRAPER_TOKEN", "")
# Bound concurrent scrapes: each is network + PDF-parse heavy. One at a time is
# fine for a manual per-symbol form and keeps memory/CPU predictable.
import threading
_SEMA = threading.BoundedSemaphore(int(os.getenv("SCRAPE_CONCURRENCY", "1")))


def _authorized(req) -> bool:
    if not SCRAPER_TOKEN:
        return False  # fail closed: no token configured => refuse everything
    auth = req.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and \
        _consteq(auth[7:].strip(), SCRAPER_TOKEN)


def _consteq(a: str, b: str) -> bool:
    import secrets
    return secrets.compare_digest(a, b)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "bucket_set": bool(scrape.S3_BUCKET)})


@app.route("/scrape", methods=["POST"])
def do_scrape():
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip()
    if not scrape.valid_symbol(symbol.upper()):
        return jsonify({"error": "invalid symbol"}), 400

    def _clamp(v, default, hi):
        try:
            return max(0, min(int(v), hi))
        except (TypeError, ValueError):
            return default
    transcripts = _clamp(data.get("transcripts", 2), 2, 12)
    ppts = _clamp(data.get("ppts", 1), 1, 12)
    annual = bool(data.get("annual", True))
    force = bool(data.get("force", False))

    if not _SEMA.acquire(blocking=False):
        return jsonify({"error": "another scrape is running — try again shortly"}), 429
    try:
        result = scrape.scrape_symbol(symbol, transcripts=transcripts, ppts=ppts,
                                      annual=annual, force=force)
        log.info("scraped %s: uploaded=%d skipped=%d errors=%d",
                 result["symbol"], len(result["uploaded"]),
                 len(result["skipped"]), len(result["errors"]))
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.exception("scrape failed for %s", symbol)
        return jsonify({"error": "scrape failed", "detail": str(e)[:200]}), 502
    finally:
        _SEMA.release()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8090"))
    app.run(host="0.0.0.0", port=port, threaded=True)
