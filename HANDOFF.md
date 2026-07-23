# Stock-Watchlist — Cloud Migration Handoff

## 🚀 DEPLOYED (Fly.io, region iad, 2026-07-23)
- **Main app:** https://stock-watchlist-noble-leaf-2877.fly.dev — app
  `stock-watchlist-noble-leaf-2877`, always-on, volume `data` (/data), 1GB.
  Secrets set: APP_PASSWORD, SECRET_KEY, OPENROUTER_*, TELEGRAM_*, AWS_*,
  VOICE_S3_BUCKET=simrat-company-docs, SCRAPER_URL, SCRAPER_TOKEN.
- **Scraper:** https://stock-scraper.fly.dev — app `stock-scraper`, scales to
  zero. Secrets: SCRAPER_TOKEN (same as main), AWS_*. Env VOICE_S3_BUCKET.
- Deploy/update either: `fly deploy -a <app>` from its dir. Logs: `fly logs -a <app>`.
- ⚠️ Still on the un-rotated `polly-agent` S3FullAccess key — scope it to just
  `simrat-company-docs` and rotate OpenRouter/Telegram when convenient, then
  re-run the relevant `fly secrets set`.


**Goal:** Move the whole Flask app (`/Users/simrat/Desktop/stock-watchlist`) to a
container host with HTTPS. Voice context should come from S3 bucket
`simrat-company-docs` instead of local disk. Add app navigation + auth.

## DONE (already committed to files)
- **Voice module is now multi-turn.** `voice_module.py` `reason()` takes a `history`
  list (system → prior turns → new question); route reads a `history` JSON form field,
  capped at `HISTORY_TURNS = 12` (6 Q&A pairs). Server stays **stateless** on purpose
  so it survives multi-instance cloud deploy.
- **`templates/voice.html`** keeps an in-memory `history` array, sends it each request.
  History clears ONLY on: page refresh, "🔄 New conversation" button, or a genuinely
  different symbol at ask-time (checked in `send()`, not on keystroke).

## ⚠️ READ `CODE-REVIEW.md` FIRST
A full pre-deploy review (security/correctness/structure) is in `CODE-REVIEW.md`.
It has TIER-0 deploy blockers (no auth; `rmtree` path traversal that deletes the whole
app; stored XSS on /share; secrets to rotate). Do those before the migration steps below.

## Done since original handoff
- `git init` + `.gitignore` + `.dockerignore` + `.env.example` (secrets/state/artifacts
  excluded and verified). `.fixed-staging` duplicate deleted. `reports*.md` (3MB) removed.

## TODO (next steps, in order)
1. ~~**`load_context()` → read from S3**, not local `CONTEXT_DIRS`.~~ ✓ DONE 2026-07-23.
   - `voice_module.load_context` reads `<SYMBOL>/*.txt` from `VOICE_S3_BUCKET`
     (paginator + get_object, sorted keys, pdf skipped) when the env var is set;
     falls back to local CONTEXT_DIRS in dev; S3 errors degrade to "no filings".
     Config: `VOICE_S3_BUCKET`, `AWS_REGION` in config.py. boto3 client is lazy+cached.
   - ⚠️ Bucket `simrat-company-docs` is still **EMPTY** — voice returns "no filings"
     until the **scraper microservice (Phase C, in progress)** populates it. AWS creds
     in `/Users/simrat/Desktop/coding-practice/tts-chat/.env` (user `polly-agent`, S3FullAccess).
2. ~~**Auth gate** covering ALL routes.~~ ✓ DONE 2026-07-23 — session-cookie
   login (`APP_PASSWORD` env, fail-closed). See CODE-REVIEW.md TIER 0.1. Also
   done: 0.2 path-traversal guard, 0.3 XSS sanitization. Set `APP_PASSWORD` in
   `.env` before running locally (server now refuses to start without it).
   ~~TIER 1 correctness bugs~~ ✓ DONE 2026-07-23 too — Safari mp4/webm mislabel,
   malformed-history/`content:null`/`audio.filename`-None crashes, TTS-429
   answer-loss (retry + per-chunk isolation), client re-entrancy, fetch timeout,
   error-body leak. See CODE-REVIEW.md TIER 1.
3. ~~**Navigation** between `/` and `/voice`.~~ ✓ DONE 2026-07-23 — `.top-nav` bar
   (Watchlist / Voice / Sign out) in both index.html header and voice.html.
4. ~~**Dockerfile + pinned requirements.txt.**~~ ✓ DONE 2026-07-23 — `Dockerfile`
   (python:3.12-slim, `CMD python main.py`), `requirements.txt` exact-pinned to the
   venv + boto3. `.dockerignore` also excludes `research/` (lives on volume).
5. ~~**Mutable state persistence** via a volume.~~ ✓ DONE 2026-07-23 — `DATA_DIR`
   env (config.py) relocates WATCHLIST_FILE / ALERT_LOG_FILE / RESEARCH_DIR to the
   mounted volume; `server.ensure_data_dir()` seeds watchlist from the baked copy on
   first boot. ⚠️ NEWS_STORE/ANN_STORE left in-repo (regenerable caches) — ephemeral
   on cloud for now; move to DATA_DIR later if their history matters.
6. **Deploy — host chosen: Fly.io.** `fly.toml` written (Mumbai `bom`, always-on,
   `/data` volume, 1GB). Server = single `python main.py` process (threaded), NOT
   gunicorn-multiworker — in-memory rate-limit/voice-semaphore + in-process scheduler
   require one process. Runbook below. Needs the user's `fly` login + `fly secrets set`.

### Fly deploy runbook (user runs these — needs your account)
```
brew install flyctl && fly auth login
cd /Users/simrat/Desktop/stock-watchlist
fly launch --no-deploy --copy-config --name stock-watchlist   # creates the app from fly.toml
fly volumes create stock_data --size 1 --region bom           # persistent /data
fly secrets set APP_PASSWORD=... SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_hex(32))") \
  OPENROUTER_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \
  AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...              # (rotated values — see 0.4)
fly deploy                                                     # remote builder; no local Docker needed
```
Set `VOICE_S3_BUCKET=simrat-company-docs` (uncomment in fly.toml or `fly secrets set`)
once the scraper is populating S3.

7. ~~**Scraper microservice (Phase C)**~~ ✓ DONE 2026-07-23 — `scraper_service/`
   (separate Fly app `stock-scraper`). `POST /scrape {symbol}` (bearer auth):
   screener.in → pypdf text → uploads `<SYMBOL>/*.txt` to S3. Triggered by a
   **"Fetch filings" form** on `/voice`, proxied through the main app's
   `POST /api/scrape` (token stays server-side). Scales to zero when idle.
   Wiring env (main app): `SCRAPER_URL`, `SCRAPER_TOKEN` (+ `VOICE_S3_BUCKET`).
   Deploy runbook: `scraper_service/README.md`. Both apps must share SCRAPER_TOKEN.

### End-to-end once both apps are live
Set on the MAIN app: `VOICE_S3_BUCKET=simrat-company-docs`, `SCRAPER_URL=https://stock-scraper.fly.dev`,
`SCRAPER_TOKEN=<same-as-scraper>`. Then on `/voice`: type a symbol → "Fetch
filings" (scraper pulls transcripts → S3) → ask your question (voice reads S3).

## Key facts / gotchas
- `config.py`: SERVER_HOST=0.0.0.0, SERVER_PORT=8088; `.env` has TELEGRAM_BOT_TOKEN,
  TELEGRAM_CHAT_ID, OPENROUTER_API_KEY, OPENROUTER_MODEL(=z-ai/glm-5.2).
- `voice_module.py` REASON_MODEL currently `openai/gpt-5.6-sol:online` (was gemini-3.1-pro).
  STT `openai/gpt-4o-transcribe`, TTS `google/gemini-3.1-flash-tts-preview` voice Charon.
- Subprocess scanners in server.py are HTTP-based (no browser dep) → containerizable.
- fiscal-agent's Playwright/Google-OAuth downloader is the one piece hard to move to cloud;
  it can keep running on the Mac and upload .txt to S3.
