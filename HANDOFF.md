# Research Desk — Handoff

A private, single-user market-research workspace (Flask). Deployed on Fly.io.
Formerly "stock-watchlist" / "Signalbook"; the UI brand is now **Research Desk**.

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
  `screener_alerts/`, `uploads/`, `conversations/`, `todos/`. Seeded from the
  image-baked `watchlist.json` on first boot (`server.ensure_data_dir`).
- **Ephemeral (container FS, `.dockerignore`d, reset every deploy):** the **news
  store** (`news_alerts/news_store.json`) and announcements store live in the app
  dir, not the volume. So "Clear news" and every redeploy wipe them.
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
- **Notes** (the "Research"→"Notes" tab): per-ticker markdown research notes.

## Models (all via OpenRouter, one key)

- **Chat reasoning:** dropdown `REASON_MODELS` (Grok 4.5 default, Kimi K3, GPT-5.6
  Sol, Claude Opus 5, Claude Sonnet 5, GLM-5.2 — all `:online`). Allowlisted server-side.
- **Chat TTS:** dropdown `TTS_MODELS` — Gemini Flash (Charon, PCM, chunk pipeline),
  Grok Voice (`eve`, MP3 passthrough), Kokoro (`af_heart`, MP3). Per-model config
  (voice/format/pipeline). `microsoft/mai-voice-2-flash` omitted — no working voice found.
- **STT:** `openai/gpt-4o-transcribe`, `language=en` + a finance prompt.
- **News scanner:** `DEFAULT_MODEL=z-ai/glm-5.2`, **overridden by `OPENROUTER_MODEL`
  env** — so the secret governs news. **Announcements scanner:** `x-ai/grok-4.5`,
  **hardcoded, ignores the env**. (Inconsistency, see below.)

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
