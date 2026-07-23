# Code Review — stock-watchlist (pre-deploy)

Three parallel reviews (security, correctness, structure) on 2026-07-23, against the
current tree. Prioritized and deduped below. **Read this before deploying.**

Line numbers are from the review snapshot — verify against current code before editing.

---

## TIER 0 — BLOCKS PUBLIC DEPLOY (do these first, in order)

> **Status (2026-07-23, this session): 0.1, 0.2, 0.3 DONE and tested. 0.4 is a
> user action (rotate secrets) — still outstanding.** See `security.py` (new
> shared helpers), the auth gate + `_note_path`/`_ticker_dir` guards in
> `server.py`, DOMPurify+escaping in `templates/index.html` and both share
> templates, and the concurrency/rate-limit caps in `voice_module.py`.
> ⚠️ `APP_PASSWORD` is now REQUIRED — the server fails closed without it, so add
> it to your local `.env` before running (`.env.example` documents it).

### 0.1 No authentication on ANY route  ✓ DONE
Session-cookie login gate (chosen over Basic Auth for mobile UX). `/login` page
+ `secrets.compare_digest` password check → signed session; one `before_request`
hook gates EVERY route (API → 401 JSON, pages → 302 /login). Only `/login`,
`/logout`, `/healthz`, `static` are public. `/share/*` is gated too (user chose
this over keeping it public). Brute-force rate-limit on `/login` (10/5min per IP);
per-IP rate-limit + `BoundedSemaphore(2)` concurrency cap on `/api/voice/ask`;
rate-limit on `/api/check-now`; scans already single-flight. `MAX_CONTENT_LENGTH`
caps upload size. Fail-closed startup guard (`require_auth_configured()`) in both
`server.start_server()` and `main.main()`.

<details><summary>Original finding</summary>
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
</details>

### 0.2 Destructive path traversal → rmtree of the whole project  ✓ DONE
Shared containment helpers in `security.py`: `valid_segment()`
(`^[A-Za-z0-9_-]{1,64}$`, no dots → `..` impossible) + `resolve_within()`
(realpath must stay inside base). Applied via `_note_path()`/`_ticker_dir()` to
all 6 research/share routes. `delete_ticker` additionally refuses to rmtree
`RESEARCH_DIR` itself. Verified: `DELETE /api/research/%2e%2e` → 404 (no rmtree),
`GET/PUT /api/research/%2e%2e/<x>` → 404/400, legit `ASML/asml_2627` still 200.

<details><summary>Original finding</summary>
`DELETE /api/research/<ticker>` → `shutil.rmtree(os.path.join(RESEARCH_DIR, ticker))`
with no sanitization (server.py ~508-517). `DELETE /api/research/%2e%2e` resolves to
`rmtree("/Users/simrat/Desktop/stock-watchlist")` — deletes the entire app incl. `.env`.
Same one-level traversal lets `GET/PUT /api/research/%2e%2e/<slug>` read/write any `.md`
in the project root (server.py ~459-490). (Verified by routing analysis, NOT executed.)
**Fix:** one shared path-containment helper — validate `ticker`/`slug` against
`^[A-Za-z0-9_-]+$`, reject `..`, and `os.path.realpath`-confirm the resolved path stays
inside `RESEARCH_DIR` before any open/rmtree. Apply to every research route.
</details>

### 0.3 Stored XSS on /share pages + dashboard  ✓ DONE
Both share templates + `index.html` now load DOMPurify; every `marked.parse(...)`
sink is wrapped `DOMPurify.sanitize(marked.parse(...))` (share note, share
company, research note view/save, announcement digest). Inline `<script>` JSON in
the share templates escapes `</` (`json.dumps(x).replace("</","<\\/")`) — verified
a `</script>` payload can no longer break out. News/announcement fields
(`summary`, `summary_error`, `significance_reason`, `ticker`, `exchange`,
`company`, article `title`/`source`/`blurb`) escaped via the existing
`escapeHtml`; `<a href>` links passed through a new `safeUrl()` http(s) allowlist
then `escapeHtml`. Research note title `${ticker}/${slug}` also escaped.

<details><summary>Original finding</summary>
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
</details>

### 0.4 Rotate the three exposed secrets — they've been plaintext on disk  ⚠ USER ACTION (outstanding)
`.env` (OpenRouter key, Telegram bot token, chat id), `.env.bak` (second copy), and
`screener_alerts/cookie.txt` (live screener.in `sessionid`). Now gitignored + dockerignored
(done this session), but they existed in plaintext, so **rotate all three**:
OpenRouter key, Telegram bot token (via @BotFather), screener.in session (re-login).
Then delete `.env.bak` and (once rotated) refresh `cookie.txt`. In cloud, inject secrets
via the platform env-var store / an IAM role, never a file.

---

## TIER 1 — CORRECTNESS BUGS (fix before/right after deploy)

**✓ DONE 2026-07-23.** All correctness bugs fixed and verified (`voice_module.py`,
`templates/voice.html`). Verification: `py_compile` + `node --check` clean; unit tests
(no network, `requests` monkeypatched) confirm malformed-history no-crash, `content:null`→`""`,
`_tts_pcm` retry-then-succeed + empty/non-PCM guard raises, `synthesize_wav` drops one failed
chunk while keeping the rest (and raises only if ALL fail); authenticated test-client confirms
`..`/leading-dot symbols → 400, typed >2000 chars → 400, no-question → 400, and the route is
auth-gated (401 unauth). What changed, per original finding:

- **Safari mp4/webm** ✓ — `pickMime()` chooses a browser-supported mime via
  `MediaRecorder.isTypeSupported`; `MediaRecorder` constructed with it; `send()` uses the real
  recorded mime + `extFor()` extension; server now accepts `mp4` in the allowed-format set.
  (Code fix unambiguous; full confirmation still wants a real iPhone.)
- **Malformed `history` → 500** ✓ — `if not isinstance(turn, dict): continue` in `reason()`.
- **One TTS 429 nukes the answer** ✓ — `_tts_pcm` retries 429/5xx with exp backoff + jitter
  (`TTS_RETRIES=3`); `synthesize_wav` uses per-future `as_completed` with index-ordered
  results so a chunk that fails after retries drops to `b""` instead of killing the answer;
  raises only if every chunk fails.
- **`content: null` → 500** ✓ — `(msg.get("content") or "").strip()`; empty answer → 502 with
  a "try rephrasing" message rather than feeding "" to TTS.
- **`audio.filename` None → 500** ✓ — `((audio.filename or "").rsplit(...))`.
- **Length caps** ✓ — typed >`MAX_TYPED_CHARS` (2000) → 400; `MAX_CONTENT_LENGTH` set in TIER 0.
- **`_tts_pcm` empty/non-PCM guard** ✓ — rejects empty body or `json`/`text` content-type,
  folded into the retry loop.
- **Client re-entrancy** ✓ — synchronous `starting` latch at top of `startRec` (blocks the
  double-tap mic leak); `inFlight` boolean gates `submit`/`sendText`/Enter and record-start so
  concurrent requests can't scramble `history` order.
- **Minor** ✓ — client `fetch` now has a 180s `AbortController` timeout; error responses return
  generic messages and log detail server-side (no upstream-body/`str(e)` leak); `load_context`
  symbol regex tightened (must start alnum, no `..`).
- **Log/history cosmetic desync** — LEFT AS-IS (cosmetic only; scrollback of prior-symbol Q&A
  is arguably desirable; "New conversation" still clears both).

<details><summary>Original TIER 1 findings (pre-fix)</summary>

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

</details>

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
1. ~~TIER 0 security (auth → path-containment helper → XSS escaping) — the deploy gate.~~
   ✓ DONE 2026-07-23 (0.1/0.2/0.3). Not yet committed.
2. Rotate secrets (user action, **still outstanding**) + delete `.env.bak`.
3. requirements.txt pin + add numpy/pypdf/boto3 (container won't boot otherwise).
4. ~~TIER 1 correctness — Safari mime + malformed-history + TTS-429 first.~~
   ✓ DONE 2026-07-23. Not yet committed.
5. TIER 2 refactor (services/ consolidation, template/static extraction, blueprints) —
   ideally BEFORE the S3 migration so load_context and the store paths change in a clean layout.
6. Then resume cloud migration per HANDOFF.md.
