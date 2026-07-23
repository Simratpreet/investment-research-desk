# stock-watchlist

A watchlist platform with one **master watchlist** ([`watchlist.json`](watchlist.json))
feeding several subsystems:

| Subsystem | Entry point | Consumes | What it does |
|---|---|---|---|
| **Alerts** | `main.py` (daemon), `server.py` (dashboard) | names tagged `"ta"` | Earnings + price-action alerts to Telegram, Flask dashboard on :8088 |
| **News / IR** | [`news_alerts/scan.py`](news_alerts/) | names tagged `"news"` | 12-hour news + investor-relations scan, GLM-5.2 summaries via OpenRouter, append-only `news_alerts/reports.md` |
| **Screener (India)** | [`screener_alerts/scan.py`](screener_alerts/) | a **separate** screener.in watchlist (via `cookie.txt`) | Scrapes new NSE/BSE announcements + site-wide annual reports, GLM-5.2 summaries, append-only `digest.md` / `annual_reports_digest.md` |

Every name is tracked by **both** subsystems by default (all ~308). Each name
can be opted out of either one from the dashboard — the **Tracking** column
has 📰 News and 🔔 Alerts toggles per row (backed by
`PUT /api/watchlist/<ticker>/<exchange>/track`).

### Viewing news in the app

The scanner writes a structured `news_alerts/news_store.json` (latest news
per name, merged across runs) alongside the append-only `reports.md`. The
dashboard's **News** tab reads it via `GET /api/news` and renders one card
per name — significance badge, GLM summary, and article links, sorted
High → Low. The **Scan news** button triggers a full background scan
(`POST /api/news/scan`); the tab polls until it finishes. Running the CLI
`news_alerts/scan.py` updates the same store, so both paths feed the tab.

## Master watchlist schema

`watchlist.json` is a list of objects:

```json
{
  "ticker": "MSFT",
  "exchange": "NASDAQ",
  "company": "Microsoft Corporation",
  "notes": "",
  "added_date": "2026-03-30T07:55:13+00:00",
  "track": ["news", "ta"]
}
```

The **`track`** array decides which subsystems process the name:

- `"ta"` → earnings + price-action Telegram alerts. (The tag name is
  historical — TA scoring was removed; it now gates only the alert loop.)
- `"news"` → the news/IR scanner.

New names default to both (`["news", "ta"]`). Toggle either one per name from
the dashboard's **Tracking** column. The two states are distinguished
deliberately: an **absent** `track` key defaults to both-on (a hand-edited or
legacy row is never silently dropped), while an **empty** list `[]` means the
user disabled both — and is respected as such.

`exchange` uses a single canonical vocabulary; `config.EXCHANGE_SUFFIXES`
maps every code to its Yahoo Finance suffix.

## Running

```bash
# Alerts + dashboard (uses the project venv)
./venv/bin/python main.py            # dashboard on :8088 + 60-min alert scheduler
./venv/bin/python server.py 8092     # dashboard only, on a chosen port

# News / IR scan (stdlib only, no venv needed)
cd news_alerts && python3 scan.py --limit 10   # smoke test
cd news_alerts && python3 scan.py              # full ~308-name run

# Screener.in (India) — needs the venv (pypdf) and a valid cookie.txt
cd screener_alerts && ../venv/bin/python scan.py            # new watchlist announcements -> digest.md
cd screener_alerts && ../venv/bin/python annual_reports.py  # new annual reports -> annual_reports_digest.md
```

Secrets live in one shared `.env` (Telegram and OpenRouter keys).

### Screener (India) subsystem

`screener_alerts/` is **independent of the master `watchlist.json`** — it tracks
your logged-in [screener.in](https://www.screener.in) watchlist, not the global
list. It scrapes new stock-exchange announcements (`scan.py`) and freshly
uploaded annual reports site-wide (`annual_reports.py`), pulls each filing's PDF,
and summarizes via OpenRouter GLM-5.2 into append-only digests.

- Requires `pypdf` (installed in the venv) and `screener_alerts/cookie.txt` — a
  session `Cookie` header copied from a logged-in browser. When it expires the
  scripts log a clear "refresh cookie" error.
- The OpenRouter key comes from the shared `.env` (`OPENROUTER_API_KEY`); a local
  `openrouter_key.txt` is an optional fallback.
- State is per-script (`seen.json`, `annual_reports_seen.json`) so nothing is
  re-summarized across runs.
- **Dashboard integration:** the **Announcements** tab drives `scan.py` from the
  UI. `scan.py` writes a structured `announcements_store.json` (one entry per
  run: the GLM digest markdown + the raw source filings) that the tab reads via
  `GET /api/announcements`. The **Scan announcements** button triggers a
  background run (`POST /api/announcements/scan`, uses the venv python for
  pypdf); the tab polls until it finishes. **Clear** (`DELETE /api/announcements`)
  empties the store but leaves the append-only `digest.md` history intact.
  `annual_reports.py` remains CLI-only.

## Migration note

The news scanner was previously the standalone `international-watchlist-alerts`
project. `merge_watchlists.py` folded its 278-name list into this master
(deduped against the curated names, exchange labels reconciled, `track` tags
assigned). The pre-merge watchlist is preserved at
`watchlist.json.pre-merge.bak`; re-running `merge_watchlists.py` rebuilds the
master from that baseline plus the original ticker list.

See [`news_alerts/README.md`](news_alerts/README.md) for the news subsystem's
sources, failure handling, observability, and troubleshooting.
