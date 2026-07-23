# Stock-Watchlist — Cloud Migration Handoff

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

## TODO (next steps, in order)
1. **`load_context()` → read from S3**, not local `CONTEXT_DIRS`.
   - Bucket `simrat-company-docs` EXISTS, region us-east-1, but is **EMPTY**.
   - Proposed key layout: `<SYMBOL>/*.txt`. List with paginator, get each object, join.
   - AWS creds available in `/Users/simrat/Desktop/coding-practice/tts-chat/.env`
     (user `polly-agent`, has S3FullAccess). Use boto3.
   - ⚠️ Filings must be UPLOADED to the bucket first (downloaders currently write local).
2. **Auth gate** covering ALL routes (app is currently open). Simple password /
   Flask session or Basic Auth env-var check.
3. **Navigation** between watchlist index (`/`) and `/voice`. Add a nav bar to both
   templates (index template + voice.html).
4. **Dockerfile + pinned requirements.txt.** Deps: flask, requests, yfinance, numpy,
   pypdf, apscheduler, boto3. `main.py` runs APScheduler (60-min checks) then app.run.
5. **Mutable state persistence** via a persistent volume. Files (see `config.py`):
   WATCHLIST_FILE, ALERT_LOG_FILE, NEWS_STORE_FILE, ANN_STORE_FILE, RESEARCH_DIR.
6. **Deploy** to a container host — Render / Fly.io / Railway (NOT serverless: voice
   requests take ~1 min and the scheduler must stay always-on). **Ask user which host**
   before deploying; it needs their account/CLI login.

## Key facts / gotchas
- `config.py`: SERVER_HOST=0.0.0.0, SERVER_PORT=8088; `.env` has TELEGRAM_BOT_TOKEN,
  TELEGRAM_CHAT_ID, OPENROUTER_API_KEY, OPENROUTER_MODEL(=z-ai/glm-5.2).
- `voice_module.py` REASON_MODEL currently `openai/gpt-5.6-sol:online` (was gemini-3.1-pro).
  STT `openai/gpt-4o-transcribe`, TTS `google/gemini-3.1-flash-tts-preview` voice Charon.
- Subprocess scanners in server.py are HTTP-based (no browser dep) → containerizable.
- fiscal-agent's Playwright/Google-OAuth downloader is the one piece hard to move to cloud;
  it can keep running on the Mac and upload .txt to S3.
