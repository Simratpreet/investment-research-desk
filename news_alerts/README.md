# news_alerts — news & IR scanner

On-demand news and investor-relations scanner. It is a **subsystem of the
stock-watchlist project** and reads the shared **master watchlist**
([`../watchlist.json`](../watchlist.json)) — every entry tagged
`"track": ["news", ...]` (currently all ~308 names). One run:

1. finds news, press releases, and IR updates from the **last 12 hours**
   (configurable), pulled from the company's **own IR/press-release feed**
   and **Google News**;
2. summarizes every name that has news using **GLM 5.2 via OpenRouter**;
3. appends the result to an **append-only** [reports.md](reports.md) —
   history is never rewritten.

Zero third-party dependencies: Python 3.10+ standard library only.

**Relationship to the rest of stock-watchlist:** the master watchlist is one
file shared by two subsystems. The TA scoring + earnings + price-action
alert loop (`../main.py`) consumes only names tagged `"ta"` (the curated
~89). This news scanner consumes everything tagged `"news"` (all ~308).
The company name in each master row is used directly, so this scanner does
**no** per-name Yahoo lookups on a normal run.

---

## Quick start

```bash
cd stock-watchlist/news_alerts
# OPENROUTER_API_KEY lives in the shared ../.env (already set up).
python3 scan.py --limit 10  # smoke test on the first 10 names
python3 scan.py             # full run
```

A full ~308-name run takes well under a minute. Results land in
`reports.md`; each run appends a new dated section at the bottom.

## Usage

```bash
python3 scan.py                           # full scan, last 12h
python3 scan.py --window-hours 24         # wider time window
python3 scan.py --tickers NASDAQ:MSFT,LSE:WISE   # specific names only
python3 scan.py --limit 10                # first N names
python3 scan.py --no-llm                  # headlines only, no summaries
python3 scan.py --dry-run                 # print report, don't write it
```

**Exit codes:** `0` success · `1` completed with partial failures (details
in the report footer and logs) · `2` fatal (bad watchlist, concurrent run,
everything failed).

## Configuration

Environment variables are loaded from the **shared project `../.env`** first,
then any local `news_alerts/.env` override (existing environment variables
win):

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes, for summaries | — | OpenRouter API key. Without it the scan still runs, but reports headlines only and exits `1` (PARTIAL). |
| `OPENROUTER_MODEL` | no | `z-ai/glm-5.2` | Model slug for summaries. If OpenRouter rejects the slug, the run says so explicitly — check the exact name at <https://openrouter.ai/models>. |

## How a scan works

1. **Load** the master `../watchlist.json` and keep rows tagged `"news"`.
   Each row already carries its `company` name, so no per-name resolution is
   needed. (A row missing `company` is resolved via Yahoo symbol search as a
   fallback and cached in `data/names_cache.json`.) Duplicate
   `EXCHANGE:TICKER` rows are de-duplicated and logged.
2. **Scan two sources per company**, merged and deduped by title:
   - **Company IR feeds** from `ir_feeds.json` — fetched first, tagged
     **[IR]** in the report, exempt from being crowded out by press
     coverage. This is the authoritative source: it catches releases no
     outlet picks up, minutes after publication.
   - **Google News RSS** (`"<company name>" when:12h`) — third-party
     coverage (Reuters, Bloomberg, wire services, trade press), and the
     only source for companies without a discoverable feed.
   Publish times are re-checked client-side; max 8 articles per name.
3. **Summarize** each name with news via one OpenRouter chat call. The
   prompt weights IR-feed items highest, asks for a 2–4 sentence factual
   summary of material developments (earnings, guidance, M&A, contracts,
   regulatory), ignores analyst listicles, and ends with a
   `Significance: High|Medium|Low` line.
4. **Append** the run section to `reports.md` and one metrics line to
   `logs/metrics.jsonl`.

## Company IR feeds

`ir_feeds.json` maps tickers to their company's own RSS/Atom feeds.
Populate and maintain it with the discovery helper:

```bash
python3 discover_ir_feeds.py            # check tickers not yet checked
python3 discover_ir_feeds.py --refresh  # re-probe everything
python3 discover_ir_feeds.py --tickers NASDAQ:MSFT,LSE:WISE
```

Discovery looks up each company's website (Yahoo), scans the homepage and
`ir.` / `investors.` / `investor.` subdomains for advertised RSS
`<link rel="alternate">` tags, probes standard IR-platform paths (e.g. Q4's
`/rss/news-releases.xml`), validates every candidate as parseable RSS/Atom,
and keeps up to 2 best-scoring feeds per ticker. Expect roughly **40–50%**
of companies to have a discoverable feed; the rest fall back to Google News
only.

To pin a feed by hand (discovery never overwrites manual entries):

```json
"NASDAQ:MSFT": {"feeds": ["https://news.microsoft.com/feed/"], "source": "manual"}
```

## Failure handling

Designed so that one bad ticker, feed, or API never takes down a run:

- **Retries everywhere** — every HTTP call gets 3 attempts with exponential
  backoff + jitter on timeouts, connection errors, 429 and 5xx.
- **Per-name isolation** — a failure resolving, fetching, or summarizing
  one name is recorded and listed in the report footer; the run continues.
- **LLM circuit breaker** — on auth/model errors (401/402/403/404) or 3
  consecutive failures, the script stops calling OpenRouter for the rest of
  the run and every affected name degrades to headlines-with-a-warning
  instead of crashing.
- **Missing API key** degrades to headlines-only, marked PARTIAL, exit `1`.
- **IR feed failures** never block a name — Google News is still checked;
  the failure is logged, footnoted in the report, and counted in metrics.
- **Concurrency lock** — `data/.scan.lock` prevents overlapping runs
  (exit `2`). Stale locks (dead PID or older than 2h) are broken
  automatically; Ctrl-C/SIGTERM release the lock on the way out.
- **Durable writes** — `reports.md` is opened append-only and fsync'd; the
  name cache and `ir_feeds.json` are written atomically (temp file +
  rename). A crash mid-run cannot corrupt earlier reports.

## Observability

- **`logs/run-<runid>.jsonl`** — structured event log per run: every fetch,
  retry, failure, and summary with timestamps. The console shows the same
  events human-readably as the run proceeds.
- **`logs/metrics.jsonl`** — one JSON line per run: names scanned/resolved/
  with-news, IR feed counts and failures, summaries ok/failed, LLM token
  usage, duration, status, exit code.

  ```bash
  tail -5 logs/metrics.jsonl | jq '{run_id, status, names_with_news, ir_articles, duration_s}'
  ```

- **Traceability** — every section in `reports.md` carries its run id,
  window, counts, model, and status, so any report line can be traced back
  to the exact log that produced it.

## Files

| Path | What |
|---|---|
| `../watchlist.json` | **Input** — the shared master watchlist (rows tagged `"news"`) |
| `scan.py` | The scan job (load → fetch → summarize → append) |
| `discover_ir_feeds.py` | IR feed auto-discovery, writes `ir_feeds.json` |
| `ir_feeds.json` | Ticker → company IR/press-release feed URLs |
| `reports.md` | **Output** — append-only report, newest run at the bottom |
| `../.env` | `OPENROUTER_API_KEY` (+ optional `OPENROUTER_MODEL`), shared |
| `data/names_cache.json` | Fallback ticker → company-name cache (auto-managed) |
| `data/.scan.lock` | Concurrency lock (auto-managed) |
| `logs/` | Per-run JSONL event logs + `metrics.jsonl` |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Every summary says "Summary unavailable" + `llm_circuit_tripped` in log | Bad API key or wrong model slug. Check `.env`; verify the slug at openrouter.ai/models and set `OPENROUTER_MODEL` if it differs. |
| `lock_held … aborting`, exit 2 | Another scan is running. If you're sure it isn't, delete `data/.scan.lock` (a dead run's lock clears itself after 2h anyway). |
| A ticker shows "Could not resolve company name" | Yahoo doesn't know that symbol form. News is still searched by raw ticker. Rerun later, or check the symbol exists on finance.yahoo.com. |
| Recurring `ir_feed_failures` in metrics for the same name | The company changed IR platforms. `python3 discover_ir_feeds.py --refresh` to re-probe, or pin the new feed manually in `ir_feeds.json`. |
| Off-topic articles under a name (common words, e.g. "Wise") | Inherent to name-based news search; the LLM prompt is instructed to ignore non-material items, so noise washes out of summaries. |

## Known limitations

- **English-biased news search** — Google News is queried with `hl=en-US`;
  a release published only in Japanese/Korean/Chinese may not appear until
  an English outlet covers it. TSE/KRX/TWSE names are most affected — the
  IR feeds (language-independent) partially compensate.
- **Regulatory filings are not news** — exchange disclosures (SEDAR, RNS,
  ASX announcements, TDnet, EDGAR) surface only if a publication or the
  company's own feed carries them. Per-exchange filing feeds would be the
  natural next fetcher to plug into the pipeline.
- **Discovery is a snapshot** — companies change websites and IR vendors;
  re-run discovery with `--refresh` occasionally.
