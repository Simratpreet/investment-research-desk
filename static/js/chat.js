const recBtn = document.getElementById("rec");
const statusEl = document.getElementById("status");
const symbolEl = document.getElementById("symbol");
const modelEl = document.getElementById("model");
let mediaRecorder, chunks = [], stream, recMime = "";

// Remember the last chosen reasoning model across visits. A saved id that is no
// longer in the dropdown is ignored, so retiring a model can't strand the page
// on a value the server would reject.
function modelLabel(id) {
  const opt = [...modelEl.options].find(o => o.value === id);
  return opt ? opt.text : "";
}
try {
  const saved = localStorage.getItem("chatModel");
  if (saved && modelLabel(saved)) modelEl.value = saved;
} catch (e) { /* private mode / storage disabled — just use the default */ }
modelEl.addEventListener("change", () => {
  try { localStorage.setItem("chatModel", modelEl.value); } catch (e) {}
  updateContext();
  setStatus(`Model set to ${modelLabel(modelEl.value)} — applies from your next question.`);
});

// --- Context chip + setup disclosure ----------------------------------------
// The chip states what the NEXT question will actually use. Without it the
// grounding is invisible: you can't tell from the composer whether an answer
// will come from filings, your documents, or neither.
// Declared here, not with the upload code below, because updateContext() runs at
// init and `typeof` on a let in its temporal dead zone throws rather than
// returning "undefined".
let docs = [];                    // upload index entries from the server
let attached = new Set();         // ids ticked for the current conversation

const setupPanel = document.getElementById("setupPanel");
const ctxToggle = document.getElementById("ctxToggle");
const chipSymbol = document.getElementById("chipSymbol");
const chipModel = document.getElementById("chipModel");
const chipDocs = document.getElementById("chipDocs");

function updateContext() {
  const sym = symbolEl.value.trim().toUpperCase();
  chipSymbol.textContent = sym || "General";
  chipModel.textContent = modelLabel(modelEl.value) || "Model";
  const n = attached.size;
  chipDocs.textContent = n ? `${n} document${n === 1 ? "" : "s"}` : "no documents";
  // Filled dot = the answer will be grounded in something specific.
  ctxToggle.classList.toggle("is-grounded", Boolean(sym) || n > 0);
}

ctxToggle.addEventListener("click", () => {
  const open = setupPanel.hidden;
  setupPanel.hidden = !open;
  ctxToggle.setAttribute("aria-expanded", String(open));
  if (open) symbolEl.focus();
});
symbolEl.addEventListener("input", updateContext);

// Grow the composer with the question instead of scrolling a one-line box.
const qtextEl = document.getElementById("qtext");
function autosize() {
  qtextEl.style.height = "auto";
  qtextEl.style.height = Math.min(qtextEl.scrollHeight, 176) + "px";
}
qtextEl.addEventListener("input", autosize);

updateContext();   // paint the chip immediately, before docs load

// Safari/iOS MediaRecorder with no mimeType records mp4/AAC but we used to hardcode
// "audio/webm" — STT then got mp4 bytes labeled webm → garbage on iPhone. Pick a mime
// the browser actually supports, and label the blob with its true extension.
function pickMime() {
  const cands = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4",
                 "audio/aac", "audio/ogg;codecs=opus", "audio/ogg"];
  if (window.MediaRecorder && MediaRecorder.isTypeSupported) {
    for (const m of cands) { if (MediaRecorder.isTypeSupported(m)) return m; }
  }
  return "";  // let the browser choose; extFor() still resolves a sane extension
}
function extFor(mime) {
  mime = (mime || "").toLowerCase();
  if (mime.includes("mp4") || mime.includes("m4a")) return "mp4";
  if (mime.includes("aac")) return "aac";
  if (mime.includes("ogg")) return "ogg";
  if (mime.includes("wav")) return "wav";
  return "webm";
}

// Multi-turn memory: prior Q&A for the current symbol, sent back each request.
// "" is a real value here — it identifies the symbol-less free conversation, so
// consecutive general questions keep their memory instead of resetting each turn.
let history = [];
let historySymbol = null;   // null only before the first question

// `busy` shows a spinner in the status line, so a minute-long wait looks alive
// rather than hung. Every terminal state clears it.
function setStatus(t, busy) {
  statusEl.textContent = t;
  statusEl.classList.toggle("is-busy", Boolean(busy));
}

function resetHistory(sym) {
  history = [];
  historySymbol = sym;
}

// --- Conversation sidebar controller ----------------------------------------
// Owns the saved-chat list, the current conversation id, and load/resume. Kept
// as one object so all sidebar state and behaviour live in a single place.
const Conversations = {
  currentId: null,
  items: [],

  init() {
    this.listEl = document.getElementById("convList");
    this.sidebar = document.getElementById("sidebar");
    this.scrim = document.getElementById("scrim");
    this.toggle = document.getElementById("convToggle");
    document.getElementById("convNew").addEventListener("click", () => this.startNew());
    document.getElementById("newConv").addEventListener("click", () => this.startNew());
    this.toggle.addEventListener("click", () => this.openDrawer());
    this.scrim.addEventListener("click", () => this.closeDrawer());
    this.refresh();
  },

  async refresh() {
    try {
      const r = await fetch("/api/voice/conversations", { cache: "no-store" });
      if (!r.ok) return;
      this.items = (await r.json()).conversations || [];
    } catch (e) { return; }
    this.render();
  },

  fmtWhen(iso) {
    const t = Date.parse(iso);
    if (!t) return "";
    const s = (Date.now() - t) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    if (s < 7 * 86400) return Math.floor(s / 86400) + "d ago";
    return new Date(t).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  },

  render() {
    this.listEl.innerHTML = "";
    if (!this.items.length) {
      const e = document.createElement("div");
      e.className = "conv-empty-list";
      e.textContent = "No saved chats yet. Ask something and it'll appear here.";
      this.listEl.append(e);
      return;
    }
    for (const c of this.items) {
      const row = document.createElement("div");
      row.className = "conv-item" + (c.id === this.currentId ? " is-active" : "");
      row.tabIndex = 0;
      row.addEventListener("click", (ev) => {
        if (ev.target.closest(".conv-act")) return;   // action buttons handle themselves
        this.open(c.id);
      });
      row.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") this.open(c.id);
      });

      const body = document.createElement("div"); body.className = "conv-item-body";
      const title = document.createElement("div"); title.className = "conv-item-title";
      title.textContent = c.title; title.title = c.title;   // textContent — user data
      const meta = document.createElement("div"); meta.className = "conv-item-meta";
      const when = document.createElement("span"); when.textContent = this.fmtWhen(c.updated_at);
      meta.append(when);
      if (c.symbol) {
        const sym = document.createElement("span"); sym.className = "conv-item-sym";
        sym.textContent = c.symbol; meta.append(sym);
      }
      body.append(title, meta);

      const acts = document.createElement("div"); acts.className = "conv-actions";
      const ren = document.createElement("button");
      ren.className = "conv-act"; ren.textContent = "✎";
      ren.title = ren.ariaLabel = "Rename"; ren.type = "button";
      ren.addEventListener("click", () => this.doRename(c));
      const del = document.createElement("button");
      del.className = "conv-act del"; del.textContent = "🗑";
      del.title = del.ariaLabel = "Delete"; del.type = "button";
      del.addEventListener("click", () => this.doDelete(c));
      acts.append(ren, del);

      row.append(body, acts);
      this.listEl.append(row);
    }
  },

  setActive(id) {
    this.currentId = id;
    this.render();
  },

  // Adopt the id the server minted for a brand-new chat, so the follow-up turn
  // lands in the same conversation.
  adopt(id) { if (id) this.currentId = id; },

  startNew() {
    this.currentId = null;
    resetHistory(symbolEl.value.trim().toUpperCase());
    document.getElementById("log").innerHTML = "";
    document.getElementById("convoEmpty").hidden = false;
    this.render();
    this.closeDrawer();
    setStatus("New chat — ask your first question.");
    document.getElementById("qtext").focus();
  },

  async open(id) {
    setStatus("Loading conversation…");
    let conv;
    try {
      const r = await fetch("/api/voice/conversations/" + encodeURIComponent(id), { cache: "no-store" });
      if (!r.ok) { setStatus("✗ That chat is no longer available."); this.refresh(); return; }
      conv = await r.json();
    } catch (e) { setStatus("✗ " + e.message); return; }

    // Restore the context this conversation ran under, then replay its turns.
    symbolEl.value = conv.symbol || "";
    if (conv.model && modelLabel(conv.model)) modelEl.value = conv.model;
    const turns = conv.turns || [];
    history = [];
    document.getElementById("log").innerHTML = "";
    for (const t of turns) {                 // oldest→newest; addEntry prepends
      addEntry({ question: t.question, answer: t.answer, symbol: t.symbol,
                 model: t.model, sources: t.sources }, { historical: true });
      history.push({ role: "user", content: t.question });
      history.push({ role: "assistant", content: t.answer });
    }
    history = history.slice(-24);            // bound what the next question carries
    historySymbol = (conv.symbol || "").toUpperCase();
    document.getElementById("convoEmpty").hidden = turns.length > 0;
    updateContext();
    this.setActive(id);
    this.closeDrawer();
    setStatus(turns.length ? "Resumed — ask a follow-up." : "Empty chat — ask a question.");
  },

  async doRename(c) {
    const next = window.prompt("Rename chat:", c.title);
    if (next == null) return;
    const title = next.trim();
    if (!title || title === c.title) return;
    try {
      const r = await fetch("/api/voice/conversations/" + encodeURIComponent(c.id),
        { method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title }) });
      if (!r.ok) { setStatus("✗ Could not rename."); return; }
    } catch (e) { setStatus("✗ " + e.message); return; }
    this.refresh();
  },

  async doDelete(c) {
    if (!window.confirm(`Delete "${c.title}"? This can't be undone.`)) return;
    try {
      const r = await fetch("/api/voice/conversations/" + encodeURIComponent(c.id), { method: "DELETE" });
      if (!r.ok && r.status !== 404) { setStatus("✗ Could not delete."); return; }
    } catch (e) { setStatus("✗ " + e.message); return; }
    if (c.id === this.currentId) this.startNew();
    this.refresh();
  },

  openDrawer() {
    this.sidebar.classList.add("open");
    this.scrim.classList.add("show");
    this.toggle.setAttribute("aria-expanded", "true");
  },
  closeDrawer() {
    this.sidebar.classList.remove("open");
    this.scrim.classList.remove("show");
    this.toggle.setAttribute("aria-expanded", "false");
  },
};

const SKIP_SECONDS = 15;
const ICON = {
  play: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>',
  pause: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>',
  back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 4 4 11l7 7"/><path d="M4 11h11a5 5 0 0 1 0 10h-1"/></svg>',
  fwd: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m13 4 7 7-7 7"/><path d="M20 11H9a5 5 0 0 0 0 10h1"/></svg>',
};

function fmtTime(s) {
  if (!isFinite(s) || s < 0) s = 0;
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m + ":" + String(sec).padStart(2, "0");
}

// Playback speed cycles through these. Remembered across answers so a long
// session doesn't reset to 1× every turn.
const SPEEDS = [1, 1.25, 1.5, 2];
function loadSpeed() {
  const v = parseFloat(localStorage.getItem("chatSpeed"));
  return SPEEDS.includes(v) ? v : 1;
}
function saveSpeed(v) { try { localStorage.setItem("chatSpeed", String(v)); } catch (e) {} }
const fmtSpeed = (v) => (Number.isInteger(v) ? v + "×" : v + "×");

// A compact custom player driving a hidden <audio>. Native controls only appear
// at the very bottom of the answer, so on a long answer you had to scroll past
// all the text to reach Play. This bar sticks to the top of the answer instead.
function buildPlayer(src) {
  const audio = new Audio(src);
  audio.preload = "metadata";

  const wrap = document.createElement("div");
  wrap.className = "player";

  const mk = (cls, html, label) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "p-btn " + cls; b.innerHTML = html;
    b.setAttribute("aria-label", label); b.title = label;
    return b;
  };
  const back = mk("p-skip", "«15", "Back 15 seconds");
  const play = mk("p-play", ICON.play, "Play");
  const fwd  = mk("p-skip", "15»", "Forward 15 seconds");

  // Speed toggle — starts at the remembered rate, cycles on each click.
  audio.playbackRate = loadSpeed();
  const speed = mk("p-speed", fmtSpeed(audio.playbackRate), "Playback speed");
  speed.onclick = () => {
    const next = SPEEDS[(SPEEDS.indexOf(audio.playbackRate) + 1) % SPEEDS.length] || 1;
    audio.playbackRate = next;
    speed.textContent = fmtSpeed(next);
    saveSpeed(next);
  };

  const scrub = document.createElement("input");
  scrub.type = "range"; scrub.className = "p-scrub";
  scrub.min = 0; scrub.max = 1000; scrub.value = 0;
  scrub.setAttribute("aria-label", "Seek");
  let scrubbing = false;

  const time = document.createElement("span");
  time.className = "p-time"; time.textContent = "0:00 / 0:00";

  const seekTo = (t) => {
    const d = audio.duration;
    if (isFinite(d) && d > 0) audio.currentTime = Math.max(0, Math.min(d, t));
  };
  back.onclick = () => seekTo(audio.currentTime - SKIP_SECONDS);
  fwd.onclick  = () => seekTo(audio.currentTime + SKIP_SECONDS);
  play.onclick = () => { if (audio.paused) audio.play().catch(() => {}); else audio.pause(); };

  const paint = () => {
    const d = audio.duration || 0, t = audio.currentTime || 0;
    if (!scrubbing) {
      scrub.value = d > 0 ? Math.round((t / d) * 1000) : 0;
      scrub.style.setProperty("--p", (d > 0 ? (t / d) * 100 : 0) + "%");
    }
    time.textContent = fmtTime(t) + " / " + fmtTime(d);
  };

  scrub.addEventListener("input", () => {
    scrubbing = true;
    scrub.style.setProperty("--p", (scrub.value / 10) + "%");
    const d = audio.duration;
    if (isFinite(d) && d > 0) time.textContent = fmtTime((scrub.value / 1000) * d) + " / " + fmtTime(d);
  });
  scrub.addEventListener("change", () => {
    const d = audio.duration;
    if (isFinite(d) && d > 0) audio.currentTime = (scrub.value / 1000) * d;
    scrubbing = false;
  });

  audio.addEventListener("loadedmetadata", paint);
  audio.addEventListener("timeupdate", paint);
  audio.addEventListener("play", () => { play.innerHTML = ICON.pause; play.title = play.ariaLabel = "Pause"; wrap.classList.add("is-playing"); });
  audio.addEventListener("pause", () => { play.innerHTML = ICON.play; play.title = play.ariaLabel = "Play"; wrap.classList.remove("is-playing"); });
  audio.addEventListener("ended", () => { play.innerHTML = ICON.play; play.title = play.ariaLabel = "Play"; wrap.classList.remove("is-playing"); });

  wrap.append(back, play, fwd, scrub, time, speed);
  return { wrap, audio };
}

// Append a Q&A entry to the log (newest on top); each keeps its own audio.
// One card per exchange: question and answer were separate floating cards
// before, which read as two unrelated objects rather than a single turn.
// opts.historical: a turn loaded from saved history. Persistence is text-only,
// so there's no audio to play — render without a player and don't autoplay.
function addEntry(j, opts = {}) {
  const label = (text, tag) => {
    const el = document.createElement("div");
    el.className = "entry-label";
    el.append(document.createTextNode(text));
    if (tag) {
      const t = document.createElement("span");
      t.className = "entry-tag"; t.textContent = tag;   // textContent — never innerHTML
      el.append(t);
    }
    return el;
  };

  const el = document.createElement("div");
  el.className = "entry";

  const q = document.createElement("div"); q.className = "entry-q";
  const qt = document.createElement("div"); qt.className = "q"; qt.textContent = j.question;
  q.append(label("You asked", j.symbol || null), qt);

  const a = document.createElement("div"); a.className = "entry-a";
  const at = document.createElement("div"); at.className = "a"; at.textContent = j.answer;
  a.append(label("Answer", j.model ? (modelLabel(j.model) || j.model) : null));
  // Tag which model produced this — the dropdown can change between turns.
  // Player sits between the label and the text so it's the first thing reachable.
  let audio = null;
  if (j.audio) {
    const p = buildPlayer(j.audio); audio = p.audio;
    a.append(p.wrap);
  }
  a.append(at);

  // Free conversation has no filings behind it — don't render an empty Sources line.
  const sources = j.sources || [];
  if (sources.length) {
    const src = document.createElement("div"); src.className = "sources";
    src.textContent = "Sources: " + sources.join(", ");
    a.append(src);
  }

  el.append(q, a);
  // Prepend either way: live answers arrive newest-first, and a loaded
  // conversation is replayed oldest→newest, so prepending each yields the same
  // newest-on-top order in both cases.
  document.getElementById("log").prepend(el);
  document.getElementById("convoEmpty").hidden = true;
  if (audio && !opts.historical) audio.play().catch(() => {});
}

let recording = false;
let starting = false;   // synchronous latch: blocks a double-tap before `recording` flips
let inFlight = false;   // a request is mid-flight: block new records/asks that would race history
let transcribing = false;  // STT of a recording is running (voice → textbox)

async function startRec() {
  // Synchronous guards FIRST — `recording` only flips after an await, so without
  // `starting` a fast double-click opens a second mic stream and leaks it.
  if (starting || recording || inFlight || transcribing) return;
  starting = true;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    setStatus("Mic permission denied — allow the microphone and try again.");
    starting = false;
    return;
  }
  chunks = [];
  recMime = pickMime();
  try {
    mediaRecorder = recMime ? new MediaRecorder(stream, { mimeType: recMime })
                            : new MediaRecorder(stream);
  } catch (e) {  // some browsers reject the options form for a supported type
    mediaRecorder = new MediaRecorder(stream);
    recMime = mediaRecorder.mimeType || "";
  }
  if (!recMime) recMime = mediaRecorder.mimeType || "";  // reflect the browser's real choice
  mediaRecorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  mediaRecorder.onstop = () => { stream.getTracks().forEach(t => t.stop()); send(); };
  mediaRecorder.start();
  recording = true;
  starting = false;
  recBtn.classList.add("recording");
  recBtn.textContent = "⏹";
  recBtn.title = recBtn.ariaLabel = "Stop and send";
  setStatus("Recording… tap again to stop and send.");
}

function stopRec() {
  if (mediaRecorder && mediaRecorder.state === "recording") mediaRecorder.stop();
  recording = false;
  recBtn.classList.remove("recording");
  recBtn.textContent = "🎙";
  recBtn.title = recBtn.ariaLabel = "Ask by voice";
}

// One button: click to start recording, click again to stop and send.
recBtn.addEventListener("click", () => {
  if (recording) stopRec();
  else startRec();
});

const textBtn = document.getElementById("askText");

// --- Async job flow ---------------------------------------------------------
// A question takes up to a minute, and a phone that sleeps mid-request tears
// down the connection. So the POST only *starts* the work and returns a job id;
// we poll for the result. The id is kept in sessionStorage, so even if iOS
// discards and reloads the page while the screen is off, we reconnect to the
// job on return instead of losing a minute of paid reasoning.
const JOB_KEY = "chatPendingJob";
const POLL_MS = 3000;
const POLL_GIVE_UP_MS = 8 * 60 * 1000;
let pollTimer = null;
let wakeLock = null;

function saveJob(id) {
  try { sessionStorage.setItem(JOB_KEY, JSON.stringify({ id, started: Date.now() })); } catch (e) {}
}
function clearJob() {
  try { sessionStorage.removeItem(JOB_KEY); } catch (e) {}
}
function readJob() {
  try { return JSON.parse(sessionStorage.getItem(JOB_KEY) || "null"); } catch (e) { return null; }
}

// Keep the screen awake while a question is running. This prevents the common
// case (put the phone down, it auto-dims) rather than merely surviving it;
// polling still covers a manual lock or an app switch. Unsupported on older
// Safari — the catch is the whole error handling.
async function acquireWakeLock() {
  try {
    if ("wakeLock" in navigator) {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => { wakeLock = null; });
    }
  } catch (e) { wakeLock = null; }
}
function releaseWakeLock() {
  try { if (wakeLock) wakeLock.release(); } catch (e) {}
  wakeLock = null;
}

function endJob() {
  clearJob();
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  releaseWakeLock();
  inFlight = false;
  recBtn.disabled = textBtn.disabled = false;
}

async function pollJob() {
  const job = readJob();
  if (!job) return;
  if (Date.now() - job.started > POLL_GIVE_UP_MS) {
    setStatus("✗ Gave up waiting for the answer — ask again.");
    endJob();
    return;
  }
  let j;
  try {
    const r = await fetch("/api/voice/job/" + encodeURIComponent(job.id), { cache: "no-store" });
    if (r.status === 401) { setStatus("✗ Signed out — reload and sign in."); endJob(); return; }
    j = await r.json();
    if (r.status === 404) { setStatus("✗ " + (j.error || "answer expired")); endJob(); return; }
  } catch (e) {
    // Offline or asleep: keep the job and try again. Do NOT fail the question —
    // surviving exactly this is the point of the job flow.
    pollTimer = setTimeout(pollJob, POLL_MS);
    return;
  }
  // A second poll chain (visibilitychange can start one while a poll is already
  // awaiting) may have delivered this already — endJob clears the stored job
  // synchronously, so a stale chain sees it gone and drops out here.
  const still = readJob();
  if (!still || still.id !== job.id) return;
  if (j.status === "running") { pollTimer = setTimeout(pollJob, POLL_MS); return; }
  if (j.status === "error") { setStatus("✗ " + (j.error || "failed")); endJob(); return; }
  // Record the turn so the next question can reference it.
  history.push({ role: "user", content: j.question });
  history.push({ role: "assistant", content: j.answer });
  addEntry(j);
  // The server persisted this turn; refresh the sidebar so a new chat appears
  // (with its auto-title) or an existing one moves to the top.
  Conversations.adopt(j.conversation_id);
  Conversations.setActive(Conversations.currentId);
  Conversations.refresh();
  setStatus("Done ✓ — ask a follow-up, or start a new chat.");
  endJob();
}

// Coming back from a locked screen or a background tab: check immediately
// rather than waiting out the remainder of the poll interval.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || !readJob()) return;
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  if (!wakeLock) acquireWakeLock();   // wake locks are dropped when hidden
  pollJob();
});

// Shared submit for both audio and typed questions.
async function submit(fd) {
  if (inFlight) return;  // block a concurrent duplicate that would scramble history order
  // Blank symbol is allowed: it means a free conversation with no filings context.
  const symbol = symbolEl.value.trim();
  // A genuinely different symbol at ask-time starts a fresh conversation — and so
  // does switching between a company and the general chat, in either direction.
  const sym = symbol.toUpperCase();
  // A symbol change resets the in-memory thread; make it start a new SAVED chat
  // too, so one conversation record never mixes different companies' contexts.
  if (sym !== historySymbol) { resetHistory(sym); Conversations.currentId = null; }
  fd.append("symbol", symbol);
  fd.append("history", JSON.stringify(history));
  fd.append("docs", JSON.stringify([...attached]));
  fd.append("model", modelEl.value);
  fd.append("conversation_id", Conversations.currentId || "");
  inFlight = true;
  recBtn.disabled = textBtn.disabled = true;
  acquireWakeLock();
  setStatus(symbol || attached.size
    ? "Reasoning over the attached material and generating audio… (can take up to ~1 min)"
    : "Thinking and generating audio… (can take up to ~1 min)", true);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 120000);  // the upload itself, not the answer
  try {
    const r = await fetch("/api/voice/ask", { method: "POST", body: fd, signal: ctrl.signal });
    const j = await r.json();
    if (!r.ok || !j.job_id) {
      setStatus("✗ " + (j.error || r.status));
      endJob();
      return;
    }
    Conversations.adopt(j.conversation_id);   // new chat: adopt the server's id
    saveJob(j.job_id);
    pollTimer = setTimeout(pollJob, POLL_MS);
  } catch (e) {
    setStatus(e.name === "AbortError" ? "✗ Upload timed out — try again." : "✗ " + e.message);
    endJob();
  } finally {
    clearTimeout(timer);
  }
}

// Page loaded (or reloaded by iOS after a long sleep) with a question still in
// flight — pick it back up rather than stranding the user on an idle screen.
(function resumePendingJob() {
  if (!readJob()) return;
  inFlight = true;
  recBtn.disabled = textBtn.disabled = true;
  setStatus("Reconnecting to your question…", true);
  acquireWakeLock();
  pollJob();
})();

// Voice is an input method for the question box, not a direct submit: transcribe
// the recording, drop the text into the composer for review/edit, and let the
// user press Ask. Transcription is misheard often enough (and the answer is
// expensive enough) that sending it unseen is the wrong default.
async function send() {
  if (transcribing || inFlight) return;
  const type = recMime || mediaRecorder?.mimeType || "audio/webm";
  const blob = new Blob(chunks, { type });
  if (blob.size < 1200) { setStatus("Too short — hold a bit longer."); return; }
  const fd = new FormData();
  fd.append("audio", blob, "q." + extFor(type));  // true extension so STT reads the right codec

  transcribing = true;
  recBtn.disabled = true;
  setStatus("Transcribing…", true);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 60000);
  try {
    const r = await fetch("/api/voice/transcribe", { method: "POST", body: fd, signal: ctrl.signal });
    const j = await r.json();
    if (!r.ok) { setStatus("✗ " + (j.error || r.status)); return; }
    // Append to whatever's already typed rather than clobbering it.
    const cur = qtextEl.value.trim();
    qtextEl.value = cur ? cur + " " + j.text : j.text;
    autosize();
    qtextEl.focus();
    // Put the caret at the end so an edit continues the transcript.
    qtextEl.setSelectionRange(qtextEl.value.length, qtextEl.value.length);
    setStatus("Review and edit if needed, then press Ask.");
  } catch (e) {
    setStatus(e.name === "AbortError" ? "✗ Transcription timed out — try again." : "✗ " + e.message);
  } finally {
    clearTimeout(timer);
    transcribing = false;
    recBtn.disabled = false;
  }
}

function sendText() {
  if (inFlight) return;
  const q = qtextEl.value.trim();
  if (!q) { setStatus("Type a question first."); return; }
  const fd = new FormData();
  fd.append("text", q);
  qtextEl.value = "";
  submit(fd);
}

textBtn.addEventListener("click", sendText);
qtextEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendText(); }
});

// Fetch filings: ask the scraper service (via the server proxy) to download this
// symbol's transcripts/PPTs/annual report into the S3 chat index.
const fetchBtn = document.getElementById("fetchFilings");
let fetching = false;
function optInt(id, def) {
  const v = parseInt(document.getElementById(id).value, 10);
  return Number.isFinite(v) && v >= 0 ? v : def;
}
fetchBtn.addEventListener("click", async () => {
  if (fetching) return;
  const symbol = symbolEl.value.trim().toUpperCase();
  if (!symbol) { setStatus("Fetching filings needs a symbol — enter one above."); return; }
  const body = {
    symbol,
    transcripts: optInt("optTranscripts", 2),
    ppts: optInt("optPpts", 1),
    annual: document.getElementById("optAnnual").checked,
  };
  fetching = true;
  fetchBtn.disabled = true;
  setStatus(`Fetching filings for ${symbol} from screener.in… (can take ~30–60s)`, true);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 180000);
  try {
    const r = await fetch("/api/scrape", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    const j = await r.json();
    if (!r.ok) { setStatus("✗ " + (j.error || r.status)); return; }
    const up = (j.uploaded || []).length, sk = (j.skipped || []).length;
    setStatus(up
      ? `✓ Indexed ${up} filing${up === 1 ? "" : "s"} for ${symbol}${sk ? ` (${sk} already present)` : ""}. Ask your question.`
      : (sk ? `✓ ${symbol} already indexed (${sk} filings). Ask your question.`
            : `Nothing new found on screener.in for ${symbol}.`));
  } catch (e) {
    setStatus(e.name === "AbortError" ? "✗ Fetch timed out — try again." : "✗ " + e.message);
  } finally {
    clearTimeout(timer);
    fetching = false;
    fetchBtn.disabled = false;
  }
});

// --- Uploaded context documents ---------------------------------------------
// Uploads are stored server-side as extracted text and persist across sessions,
// so previously-uploaded docs load unticked — you opt them into a conversation
// rather than having every old file silently stuffed into the prompt.
const docListEl = document.getElementById("docList");
const docFilesEl = document.getElementById("docFiles");
const uploadBtn = document.getElementById("uploadDocs");
let uploading = false;

function fmtChars(n) {
  return n >= 1000 ? Math.round(n / 1000) + "k chars" : n + " chars";
}

function renderDocs() {
  updateContext();
  if (!docs.length) {
    docListEl.innerHTML = `<div class="doc-empty">No documents uploaded yet.</div>`;
    return;
  }
  docListEl.innerHTML = "";
  for (const d of docs) {
    const row = document.createElement("div");
    row.className = "doc-item";

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = attached.has(d.id);
    cb.title = "Include this document as context";
    cb.addEventListener("change", () => {
      if (cb.checked) attached.add(d.id); else attached.delete(d.id);
      updateContext();
    });

    const name = document.createElement("span");
    name.className = "doc-name";
    name.textContent = d.name;          // textContent, never innerHTML — filename is user data
    name.title = d.name;

    const meta = document.createElement("span");
    meta.className = "doc-meta";
    meta.textContent = fmtChars(d.chars || 0);

    const del = document.createElement("button");
    del.className = "doc-del";
    del.textContent = "✕";
    del.title = "Delete this document";
    del.addEventListener("click", () => deleteDoc(d));

    row.append(cb, name, meta, del);
    docListEl.append(row);
  }
}

async function loadDocs() {
  try {
    const r = await fetch("/api/voice/docs");
    if (!r.ok) return;
    const j = await r.json();
    docs = j.docs || [];
    // Drop ticks for anything deleted elsewhere so we never send a stale id.
    const ids = new Set(docs.map(d => d.id));
    attached = new Set([...attached].filter(id => ids.has(id)));
  } catch (e) {
    docs = [];
  }
  renderDocs();
}

async function deleteDoc(d) {
  try {
    const r = await fetch("/api/voice/docs/" + encodeURIComponent(d.id), { method: "DELETE" });
    if (!r.ok && r.status !== 404) { setStatus("✗ Could not delete " + d.name); return; }
  } catch (e) {
    setStatus("✗ " + e.message);
    return;
  }
  attached.delete(d.id);
  docs = docs.filter(x => x.id !== d.id);
  renderDocs();
  setStatus(`Removed ${d.name}.`);
}

uploadBtn.addEventListener("click", async () => {
  if (uploading) return;
  const files = docFilesEl.files;
  if (!files || !files.length) { setStatus("Choose a .txt, .md or .pdf file first."); return; }
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  uploading = true;
  uploadBtn.disabled = true;
  setStatus(`Extracting text from ${files.length} file${files.length === 1 ? "" : "s"}…`, true);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 180000);
  try {
    const r = await fetch("/api/voice/docs", { method: "POST", body: fd, signal: ctrl.signal });
    const j = await r.json();
    const added = j.added || [], errors = j.errors || [];
    // Newly uploaded docs are attached immediately — that's why you uploaded them.
    for (const d of added) attached.add(d.id);
    docs = added.concat(docs);
    docFilesEl.value = "";
    renderDocs();
    const errText = errors.length
      ? " · Failed: " + errors.map(e => `${e.name} (${e.error})`).join("; ")
      : "";
    setStatus(added.length
      ? `✓ Attached ${added.length} document${added.length === 1 ? "" : "s"}.${errText}`
      : ("✗ Nothing uploaded." + errText));
  } catch (e) {
    setStatus(e.name === "AbortError" ? "✗ Upload timed out — try again." : "✗ " + e.message);
  } finally {
    clearTimeout(timer);
    uploading = false;
    uploadBtn.disabled = false;
  }
});

loadDocs();
Conversations.init();
