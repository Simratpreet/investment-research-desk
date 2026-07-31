# Research Desk — Handoff

A private, single-user market-research workspace (Flask). Deployed on Fly.io.
Formerly "stock-watchlist" / "Signalbook"; the UI brand is now **Research Desk**.

## Code Rules
- Follow /Users/simrat/Desktop/claude-howto/clean-code-rules.md

## Deployed

- **Main app:** https://stock-watchlist-noble-leaf-2877.fly.dev — Fly app
  `stock-watchlist-noble-leaf-2877`, region `iad`, always-on, 1 GB volume at `/data`.
  Deploy: `fly deploy --app stock-watchlist-noble-leaf-2877` (remote builder, no local Docker).
- **Scraper microservice:** Fly app `stock-scraper` (`scraper_service/`), scales to
  zero. `POST /scrape {symbol}` (bearer `SCRAPER_TOKEN`) → screener.in → pypdf →
  uploads `<SYMBOL>/*.txt` to S3. The main app proxies to it via `POST /api/scrape`.
- **Secrets** (main app, set via `fly secrets set`): `APP_PASSWORD`, `SECRET_KEY`,
  `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `TELEGRAM_*`, `AWS_ACCESS_KEY_ID/SECRET`,
  `VOICE_S3_BUCKET=simrat-company-docs`, `SCRAPER_URL`, `SCRAPER_TOKEN`, `SCREENER_COOKIE`.
  `fly secrets list` shows digests only. Rotation helper: `./rotate_secrets.sh`.

## Architecture (important constraints)

- **Single process, single worker — do NOT scale to >1.** The in-memory rate
  limiter, the voice `BoundedSemaphore`, the async job store, and the in-process
  APScheduler all assume one process. Entry point is `python main.py` (threaded),
  not gunicorn-multiworker.
- **Auth:** one shared password → signed session cookie (`APP_PASSWORD`, fail-closed;
  the server refuses to start without it). `@before_request` gates every route
  except login/logout/healthz/static.
- **Persistent state on the volume** via `DATA_DIR` (config.py; `/data` in prod,
  repo dir locally): `watchlist.json`, `alert_log.json`, `research/`,
  `screener_alerts/` (announcements store + seen set), `news_alerts/` (news store
  + name cache), `uploads/`, `conversations/`, `todos/`,
  `market_scan/runs/` (one JSON per market per session) +
  `market_scan/universes/` (cached exchange symbol lists). Seeded from the
  image-baked `watchlist.json` on first boot (`server.ensure_data_dir`).
  Both scanners resolve their own state dir from `$DATA_DIR` and fall back to
  their package dir locally (`news_alerts/scan.py` + `screener_alerts/scan.py`
  `STATE_DIR`); `server.py` mirrors those paths. The scan subprocesses inherit
  `DATA_DIR` from the environment, so don't pass a stripped `env=` to `Popen`.
- **Ephemeral (container FS, `.dockerignore`d, reset every deploy):** news
  artifacts only — `news_alerts/reports.md` (append-only run history) and
  `news_alerts/logs/` (per-run jsonl + metrics). Nothing the dashboard reads.
  (`screener_alerts/digest.md` and `scan.log` are on the volume.) "Clear news"
  still empties the news store on demand.
- **S3** (`simrat-company-docs`): `<SYMBOL>/*.txt` filings (read by Chat, written by
  the scraper / fiscal-agent `--s3`), plus `tts-cache/` (content-addressed rendered audio).

## Pages / features

- **Watchlist** (`/`, `index.html` + `watchlist.js`): a scan-list of names monitored
  for news/alerts. A–Z by ticker, paginated 50/page. Per-row News/Alerts toggles.
  Tabs: Watchlist · News · Announcements · Alerts · Notes. "Check now" runs alerts.
- **Alerts tab:** sub-tabs **All / Price up / Price down / Volume / EMA cross /
  Earnings**, each sorted by what matters (biggest move, soonest earnings), with
  colour-coded magnitude badges. Alerts are recorded to history; Telegram is OFF by
  default (`ALERTS_TELEGRAM_ENABLED`). Schedule: daily 23:00 Europe/London +
  on-demand (main.py CronTrigger, no run-on-boot).
- **Chat** (`/voice`, `voice.html` + `chat.js`, `voice_module.py`): voice/text Q&A
  grounded in a symbol's S3 filings and/or uploaded docs; blank symbol = free chat.
  Async job flow (`/api/voice/ask` → poll `/api/voice/job/<id>`) so a sleeping phone
  can't lose the answer. Persistent conversations sidebar (`conversation_store.py`,
  text-only, 7-day retention). Custom audio player (±15s, speed, sticky). On-demand
  re-synthesis of old answers. STT reviewed in the textbox before sending.
  **Reasoning-model dropdown** (allowlist `REASON_MODELS`) and **voice/TTS-model
  dropdown** (allowlist `TTS_MODELS`).
- **To-do** (`/todos`, `todos.html` + `todos.js`, `todo_store.py` + `todo_module.py`):
  weekly board (Prioritise/Done/Rejected), resets each Monday (implicit — keyed by
  week's Monday). Current + future weeks editable; past weeks read-only. Add tasks to
  future weeks; "→ Next wk" defers a task forward. 26-week retention.
- **Movers** (`/movers`, `movers.html` + `movers.js`, `market_scan/` + `scan_module.py`):
  scans whole exchange universes for stocks whose **last completed session** showed
  BOTH a volume spike and a price rise — "what is piquing investor interest",
  before it reaches the watchlist. Per-market on demand; a sortable table (company,
  sector, mkt cap, RVOL, change, price, turnover) with an expandable per-stock AI
  note. Empty is a normal outcome, rendered as such.
  - **Criteria:** `RVOL ≥ 3×` **AND** `change ≥ +3%`, up only, 20-day baseline, all
    `MOVERS_*` in config.py and env-overridable. Requiring *both* is the part that
    matters: measured live, `or` yields 800–1,200 hits/day (unreadable,
    unaffordable to analyse). Thresholds were 5×/5% until 2026-07-31 and were
    relaxed to catch accumulation that starts quieter than a violent day; expect
    materially more hits, with `MOVERS_ANALYSIS_MAX` bounding the LLM spend.
  - **Markets:** `nasdaq`, `nyse`, `tsx`, `tsxv`, `asx`, `etr`, `sw`, `sto` — 9,549
    symbols. Symbol lists are **screener CSV exports committed to
    `market_scan/universes/`** (`Ticker, Company[, Market Cap][, Industry]`; tickers
    already in Yahoo form). Four exports carry an industry, so those markets need no
    per-hit sector lookup; Stockholm's has neither cap nor industry and relies on
    enrichment for both. TSX Venture is the one odd file — `Symbol, Company Name`
    with an exchange prefix (`TSXV:OTS.H` → `OTS-H.V`; hyphen, verified live, the
    dotted form returns nothing) — so it gets its own parser.
    Read from disk, so a scan needs no network to know what
    to scan and the list is identical run to run. Crucially the exports are
    **companies only** — no ETFs, no closed-end funds — which an exchange directory
    is not; a leveraged or crypto ETF really does post 5× volume on a +5% day. The
    detector also rejects anything Yahoo labels non-`EQUITY`, so a hand-dropped CSV
    can't reintroduce funds. Refresh by overwriting the CSV, or drop one into
    `DATA_DIR/market_scan/universes/` to skip a redeploy; past
    `MOVERS_UNIVERSE_MAX_AGE_DAYS` (120) a scan still runs but is flagged stale.
    Adding a market is one `Market` entry in `market_scan/universe.py` + a CSV.
  - **Pipeline:** `scan → PERSIST → enrich → analyse`. The run is written to disk
    before enrichment or notes begin, and both write back incrementally — an
    OpenRouter outage or a yfinance failure costs notes, never the scan. Enrichment
    fetches the **sector** only; market cap comes from the export and is never
    overwritten (Yahoo's cap is unreliable for microcaps).
  - **Cost:** notes are the only per-run spend. `MOVERS_ANALYSIS_MAX` (default 40)
    caps them and `MOVERS_ANALYSIS_CONCURRENCY` (3) overlaps the waiting; the prompt
    asks for a business model *and* a thesis, so they are not short. Scanning is free.
  - **History:** the page shows the last `MOVERS_RETAIN_SESSIONS` (5) stored
    sessions for a market in one table, with a Session column, sorted newest
    session first then by RVOL within it. Pruning keeps the newest 5 runs **per
    market, counted by session date** — not by file age, which would wipe a
    market scanned weekly and would be reset anyway by a redeploy restamping
    every file's mtime. Notes are namespaced `session|ticker` in the API, since
    the same ticker can spike on more than one retained day.
  - **Re-running is cheap.** An exchange publishes a session's bars hours after
    the close — Yahoo serves the timestamp with null OHLCV meanwhile, which the
    feed drops — so a scan run too early legitimately reports yesterday and gets
    run again. Before scanning, a **probe** fetches the market's ~5 largest names
    and asks what session it is on; if that matches a clean stored run, the full
    scan is skipped (seconds, not minutes) and only missing notes are filled in.
    An ambiguous probe (no majority, feed trouble) falls through to the real
    scan — guessing would silently skip a session. `ScanStore.save` also keeps
    notes already written for a session, and `_analyse` skips any hit whose note
    is already `ok`; a `failed` or `skipped` note *is* retried, which is how a
    re-run recovers from an OpenRouter outage. "Rescan anyway" (`force: true`)
    appears on the page once a run reports it is up to date.
  - **Concurrency:** scans are single-flight *per market* but independent across
    them, so several can run at once — the page's "Scans in progress" list shows
    every one, whichever market is selected, and keeps polling while any is live.
    All markets share one `YahooPriceFeed`, so its global pacer holds the same
    total request rate no matter how many run; concurrent scans divide that budget
    rather than multiplying it.
  - **Schedule:** on-demand only unless `MOVERS_SCHEDULE_MARKETS` names markets
    (comma-separated), which registers a daily APScheduler job in `main.py`.
- **Notes** (the "Research"→"Notes" tab): per-ticker markdown research notes.

## Models (all via OpenRouter, one key)

- **Chat reasoning:** dropdown `REASON_MODELS` (Grok 4.5 default, Kimi K3, GPT-5.6
  Sol, Claude Opus 5, Claude Sonnet 5, GLM-5.2 — all `:online`). Allowlisted server-side.
- **Chat TTS:** dropdown `TTS_MODELS` — Gemini Flash (Charon, PCM, chunk pipeline),
  Grok Voice (`eve`, MP3 passthrough), Kokoro (`af_heart`, MP3). Per-model config
  (voice/format/pipeline). `microsoft/mai-voice-2-flash` omitted — no working voice found.
  **MP3 models get their Xing header rewritten** (`mp3_repair.py`) before delivery:
  Kokoro returns its segments concatenated, each with its own header, so the
  leading one describes only the first segment. Safari trusts it — a 103s answer
  reported 11s, stopped there, and ignored the speed control because the element
  had already fired `ended`. Chrome ignored the header and estimated ~7% long.
  `MP3_RENDER_VERSION` is in the S3 cache key so pre-fix clips aren't served.
- **STT:** `openai/gpt-4o-transcribe`, `language=en` + a finance prompt.
- **News scanner:** `DEFAULT_MODEL=z-ai/glm-5.2`, **overridden by `OPENROUTER_MODEL`
  env** — so the secret governs news. **Announcements scanner:** `x-ai/grok-4.5`,
  **hardcoded, ignores the env**. (Inconsistency, see below.)
- **Movers notes:** `MOVERS_MODEL`, default `moonshotai/kimi-k3:online` (web search
  matters — the note has to find what actually happened). The prompt is in
  `market_scan/analyst.py`: measured facts (RVOL, change, turnover, session) plus
  the standing question about growth acceleration / margin expansion / deleveraging
  and the business model. It explicitly instructs the model to **say there is no
  identifiable public catalyst rather than invent one** — an unexplained 5× volume
  day is itself the signal, and a fabricated reason destroys it by making the name
  look understood.

## Outstanding / known issues

- **AWS key still `polly-agent` with `S3FullAccess`.** Scope it to `simrat-company-docs`
  (Get/Put/List, incl. `tts-cache/`; no Delete). Least-privilege policy JSON was
  provided earlier in chat. Biggest open security item. Also rotate OpenRouter /
  Telegram keys when convenient (`./rotate_secrets.sh` handles the self-minted ones).
- **News shows stale headlines (e.g. "Fevertree Q2 2023").** DIAGNOSED, NOT FIXED
  (user said leave it). Root cause: **TipRanks auto-generates evergreen "earnings
  report" pages for old quarters and re-timestamps them daily**, so Google News RSS
  returns them with a *fresh* pubDate — no recency filter can catch it. The LLM then
  faithfully summarises the garbage. Correct fix = **source blocklist** (drop
  `TipRanks`) and/or a title guard for `Q[1-4] <past-year>`, in `fetch_news`
  (`news_alerts/scan.py`). A wrong recency-filter attempt was committed then reverted
  (`e717d14` / `df5d7c7`) — don't repeat it.
- **No dark theme.** App is light-only (no `prefers-color-scheme` in the CSS).
- **Stat tiles** on the watchlist still have cryptic WL/N/A/EX badges (cosmetic tidy pending).
- **News vs Announcements model inconsistency** — news honours `OPENROUTER_MODEL`,
  announcements hard-codes Grok 4.5. Make announcements honour the env too if you want one knob.
- **Earnings alerts fire daily** while the condition holds (offered a fire-on-transition
  change; not implemented).
- **fiscal-agent** (`/Users/simrat/Desktop/fiscal-agent`) has a `--s3` flag to upload
  extracted `.txt` to `<SYMBOL>/` (needs `pip install boto3` + `S3_BUCKET` in its .env).
  Its Playwright/OAuth downloader can't move to cloud; run it on the Mac.

## Dev / ops gotchas

- **Static cache-busting:** every `/static` URL is stamped `?v=<newest mtime>`
  (server.py context processor). Mobile Safari caches hard — this forces fresh assets.
- **Headless screenshot pipeline (for UI work — I can't see the live authed app):**
  playwright + chromium are installed in the venv. Run the app locally with a scratch
  `DATA_DIR` + `APP_PASSWORD`, then drive playwright to log in and screenshot each page
  (light/dark, desktop/mobile). Read the PNGs to actually see the UI. This is how the
  redesign was verified. The Chrome extension (Claude-in-Chrome) would not connect.
- **Files are split:** templates are thin HTML; CSS in `static/css/{base,watchlist,chat,todos}.css`
  (base.css = shared design tokens + app-bar), JS in `static/js/`. Fonts: Geist / Geist Mono.
- **Secrets never pass through the assistant** — the user runs `fly secrets set` and
  generates values locally. The `!`-prefix in the Claude Code prompt runs a shell
  command in-session; in the user's own zsh, `!` is history expansion (a past gotcha).
- **Config.py** loads `.env`. `SERVER_PORT` honours `$PORT`. Timezone for alerts +
  to-do weeks is `Europe/London` (pytz, bundled tz data — safer than zoneinfo on slim).
- **Tests:** `python3 -m unittest discover tests` (65 tests, stdlib only, no network).
  Deliberately not pytest — `requirements.txt` is pinned for reproducible container
  builds and a test-only dep would either bloat the image or drift from it.
- **Yahoo 429s the realistic Chrome User-Agent.** `market_scan/feed.py` sends a bare
  `Mozilla/5.0` on purpose. The full
  `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ... Chrome/125 ...` string returns
  **429 on the first request, every time**, from an IP with an untouched budget —
  Yahoo fingerprints that well-known scraper signature. Sending no UA also 429s.
  Don't "fix" this by making it look more like a browser; that is what breaks it.
  (This cost an hour of chasing a rate-limit that wasn't one. The feed also paces
  itself globally at ~16/s with AIMD backoff and a circuit breaker, so a genuine
  limit degrades the run instead of hanging it.)
- **`meta.regularMarketTime` is the last *trade* time, not the clock.** Movers'
  session selection (`market_scan/session.py`) compares the wall clock against
  `currentTradingPeriod.regular.end`. Using `regularMarketTime` looks equivalent and
  isn't: a thin stock whose final trade lands seconds before the bell reads as "still
  trading" for the rest of the day, so it gets scanned against yesterday while
  everything liquid is scanned against today — and thin stocks are exactly the ones
  that spike. Covered by a regression test.
