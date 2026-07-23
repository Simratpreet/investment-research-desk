# Code Review — stock-watchlist (pre-deploy)

Three parallel reviews (security, correctness, structure) on 2026-07-23, against the
current tree. Prioritized and deduped below. **Read this before deploying.**

Line numbers are from the review snapshot — verify against current code before editing.

---

## TIER 0 — BLOCKS PUBLIC DEPLOY (do these first, in order)

### 0.1 No authentication on ANY route
The app has zero auth. Going public means anyone can spend your money and mutate/delete
your data. Worst offenders:
- `POST /api/voice/ask` (voice_module.py) — STT + high-effort `gpt-5.6-sol` reasoning +
  Gemini TTS per call, no rate limit. A curl loop drains the OpenRouter key.
- `POST /api/news/scan`, `POST /api/announcements/scan` (server.py ~241, ~317) — spawn
  LLM scans over the whole watchlist.
- `POST /api/check-now` (server.py ~358) — sends Telegram messages (anyone spams your phone).
- Watchlist/research CRUD + `DELETE /api/news|alerts|announcements` — anyone edits/wipes data.
**Fix:** gate the ENTIRE app behind auth (a `before_request` token/session check, or
reverse-proxy basic-auth/SSO). Add per-IP rate limiting + a global concurrency cap on
voice/ask and the scan endpoints. Do not deploy public without this.

### 0.2 Destructive path traversal → rmtree of the whole project
`DELETE /api/research/<ticker>` → `shutil.rmtree(os.path.join(RESEARCH_DIR, ticker))`
with no sanitization (server.py ~508-517). `DELETE /api/research/%2e%2e` resolves to
`rmtree("/Users/simrat/Desktop/stock-watchlist")` — deletes the entire app incl. `.env`.
Same one-level traversal lets `GET/PUT /api/research/%2e%2e/<slug>` read/write any `.md`
in the project root (server.py ~459-490). (Verified by routing analysis, NOT executed.)
**Fix:** one shared path-containment helper — validate `ticker`/`slug` against
`^[A-Za-z0-9_-]+$`, reject `..`, and `os.path.realpath`-confirm the resolved path stays
inside `RESEARCH_DIR` before any open/rmtree. Apply to every research route.

### 0.3 Stored XSS on public /share pages + dashboard
Research note content is attacker-writable (0.1) and rendered unsanitized:
- `{{ content_json|safe }}` in inline `<script>` (server.py ~619, ~807) — `json.dumps`
  doesn't escape `/`, so a note with `</script><script>…</script>` breaks out → arbitrary JS.
- `marked.parse(...)` → `innerHTML` with no sanitizer (server.py ~620, ~809;
  index.html ~1982, ~2030) — `<img src=x onerror=…>` executes.
- Same pattern in news/announcements dashboard render (index.html ~2196-2213, ~2322-2348):
  `it.summary`, `it.significance_reason`, `run.digest`, and `<a href="${link}">` unescaped.
  Note the watchlist table IS correctly escaped via `escapeHtml` (index.html ~1686) — the
  helper exists, it's just not applied to these fields.
**Fix:** escape `</` in the JSON (`json.dumps(x).replace("</","<\\/")`); sanitize
`marked.parse` output with DOMPurify; escape the news/announcement fields; allowlist
`http(s)` link schemes.

### 0.4 Rotate the three exposed secrets — they've been plaintext on disk
`.env` (OpenRouter key, Telegram bot token, chat id), `.env.bak` (second copy), and
`screener_alerts/cookie.txt` (live screener.in `sessionid`). Now gitignored + dockerignored
(done this session), but they existed in plaintext, so **rotate all three**:
OpenRouter key, Telegram bot token (via @BotFather), screener.in session (re-login).
Then delete `.env.bak` and (once rotated) refresh `cookie.txt`. In cloud, inject secrets
via the platform env-var store / an IAM role, never a file.

---

## TIER 1 — CORRECTNESS BUGS (fix before/right after deploy)

Public endpoint means these are trivially triggerable, not theoretical.

- **Safari records mp4 but it's tagged `audio/webm`** (voice.html ~114/165/168). `MediaRecorder`
  with no mimeType → iOS/Safari produce mp4; `send()` hardcodes `type:"audio/webm"` +
  `q.webm`, so STT gets mp4 bytes labeled webm → garbage transcription **on iPhone**
  (the primary driving/gym use case). Fix: pick a supported mime via
  `MediaRecorder.isTypeSupported`, use the real mime + extension.
- **Malformed `history` → 500** (voice_module.py ~110-112, validated only as list at ~214).
  `history=[1,2,3]` or `[null]` makes `turn.get(...)` raise. Fix: `if not isinstance(turn, dict): continue`.
- **One TTS 429 nukes the whole answer** (voice_module.py ~171). 6 parallel chunks; first
  worker's HTTPError propagates through `b"".join(ex.map(...))` → 502, discarding the 5
  good chunks. No retry. Fix: retry-with-backoff on 429/5xx; per-future try/except so one
  failed chunk degrades gracefully.
- **`content: null` from reasoning model → 500** (voice_module.py ~127). `.content.strip()`
  crashes when the model returns reasoning-only/refusal. Fix: `(msg.get("content") or "").strip()`.
- **`audio.filename` None → 500** (voice_module.py ~205). Fix: `((audio.filename or "")...)`.
- **No length cap on typed text / audio size** (voice_module.py ~193, ~208) — cost/DoS.
  Fix: reject `len(typed) > N` with 400; set Flask `MAX_CONTENT_LENGTH`.
- **`_tts_pcm` no empty/non-PCM guard** (voice_module.py ~151) — a 200 with empty or JSON
  body → silent gap or garbled noise written as PCM. Fix: assert non-empty raw PCM; retry/fail loud.
- **Client re-entrancy:** double-click record leaks a 2nd mic stream (voice.html ~107-122,
  `recording` set only after await); Enter-spam bypasses the `disabled` guard → concurrent
  duplicate requests that corrupt `history` order (voice.html ~182 vs ~149). Fix: synchronous
  guard flag at top of `startRec`; gate `sendText`/Enter on an `inFlight` boolean.
- **Minor:** client `fetch` has no timeout/AbortController; error responses leak upstream
  bodies/`str(e)` to client (voice_module.py ~229-233); `load_context` symbol regex allows
  `..` (voice_module.py ~190) — low value (only `.txt`, one level) but tighten.
- Log/history cosmetic desync on symbol-change-at-ask (voice.html ~146 resets history but
  not `#log`). Cosmetic only; "New conversation" button clears both correctly.

**Confirmed NOT bugs:** `_chunk()` terminates, no empty chunks, no dropped text; regex
markup-strip doesn't mangle number-words; `ex.map` order IS preserved; typed-wins-over-audio
is intended; history pushed correctly on both paths; no command injection (list-arg
subprocess, no user input, no shell=True); no eval/exec/pickle/yaml; Flask debug off.
The `transcribe()` JSON/base64 call to OpenRouter is VALID — verified live earlier (~0.8s,
perfect transcription); ignore any review note calling it malformed.

---

## TIER 2 — MAINTAINABILITY / STRUCTURE

### Done this session
- `.fixed-staging/` full-app duplicate — **DELETED by user.** ✓
- `git init` + `.gitignore` + `.dockerignore` + `.env.example`; secrets/state excluded &
  verified; `reports.md`/`reports2.md` (3MB) untracked and deleted from disk. ✓

### Duplication to consolidate into a shared `services/`/`lib/` module
- **`.env`/OpenRouter-key loading implemented 4×:** config.py ~9-16; voice_module.py ~48-57
  `_api_key()`; news_alerts/scan.py ~145-156; screener_alerts/scan.py ~216-232. Standardize
  on ONE loader (or adopt `python-dotenv`). voice_module never imports config — it re-reads
  the same `.env`.
- **OpenRouter chat POST implemented 4×:** voice_module.py; news_alerts/scan.py ~561
  (`LlmClient`); screener_alerts/scan.py ~305; screener_alerts/annual_reports.py ~91. Plus an
  **HTTP-lib split** — voice_module/notifier use `requests`; the scanners hand-roll `urllib`.
  Pick `requests`, one client.
- `load_watchlist()` 3× (server.py ~20, main.py ~21, news_alerts/scan.py ~254); alert-log
  read 2× (notifier.py ~102, server.py ~33); "seen" store read/write 2× (screener scan vs
  annual_reports). (annual_reports correctly `import scan` for PDF extraction — leave that.)

### HTML/JS in Python + monoliths
- `SHARE_TEMPLATE` (server.py ~522-623) + `COMPANY_TEMPLATE` (~658-813) = ~255 lines of
  HTML/CSS/JS as Python strings via `render_template_string`, near-duplicate CSS. Move to
  `templates/share_note.html` + `templates/share_company.html`, share CSS via base template.
- `templates/index.html` is a **2,424-line monolith**; no `static/` dir. Extract CSS/JS to
  `static/css/dashboard.css` + `static/js/dashboard.js`.
- `server.py` (849 lines) mixes routing + storage I/O + inline templates + subprocess. Split
  into blueprints (watchlist / research+share / news / alerts / voice).
- Both share templates load `marked.min.js` + Google Fonts from CDN — vendor them (breaks
  under strict CSP).

### requirements.txt
Not pinned (`>=`), and **missing `numpy`** (alerts/price_action.py), **`pypdf`**
(screener_alerts/scan.py), **`boto3`** (planned S3). Pin all to `==`. `feedparser` NOT
needed (RSS via stdlib xml). This blocks the container from running — fix early.

### Other
- Introduce a single `DATA_DIR` env var — news/screener store paths are hardcoded in
  server.py ~170/~256 and the scanners, so the volume mount can't be a one-line change yet.
- Move one-off tools (`merge_watchlists.py`, `news_alerts/discover_ir_feeds.py`,
  `screener_alerts/annual_reports.py`) to a `tools/` dir excluded from the image (already
  dockerignored the first two this session).
- Delete `.env.bak` (after rotating secrets).

### Proposed target layout
```
app/{__init__.py(create_app), config.py, blueprints/, services/, scanners/}
templates/{base.html, index.html, voice.html, share_note.html, share_company.html}
static/{css/, js/, vendor/marked.min.js}
tools/   data/(volume, gitignored)   main.py  Dockerfile  requirements.txt
```

---

## Suggested execution order for next session
1. TIER 0 security (auth → path-containment helper → XSS escaping) — the deploy gate.
2. Rotate secrets (user action) + delete `.env.bak`.
3. requirements.txt pin + add numpy/pypdf/boto3 (container won't boot otherwise).
4. TIER 1 correctness — Safari mime + malformed-history + TTS-429 first.
5. TIER 2 refactor (services/ consolidation, template/static extraction, blueprints) —
   ideally BEFORE the S3 migration so load_context and the store paths change in a clean layout.
6. Then resume cloud migration per HANDOFF.md.
