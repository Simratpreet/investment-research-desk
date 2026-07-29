"use strict";
// Movers — stocks whose last completed session showed both a volume spike and a
// price rise. Poll-driven, matching the News/Announcements scan UX on the
// watchlist page: start a scan, poll every 5s while it runs, stop polling when
// it finishes.

const marketSelect = document.getElementById("marketSelect");
const scanBtn = document.getElementById("scanBtn");
const stopBtn = document.getElementById("stopBtn");
const forceBtn = document.getElementById("forceBtn");
const scanMeta = document.getElementById("scanMeta");
const progress = document.getElementById("progress");
const progressFill = document.getElementById("progressFill");
const progressText = document.getElementById("progressText");
const banner = document.getElementById("banner");
const results = document.getElementById("results");
const activeScans = document.getElementById("activeScans");
const activeList = document.getElementById("activeList");

const POLL_MS = 5000;

// Model output is untrusted and full of web links: parse, then strip anything
// active. Same helper as watchlist.js.
function mdSafe(md) {
  return DOMPurify.sanitize(marked.parse(String(md ?? "")));
}

function markdownReady() {
  return !!(window.marked && window.DOMPurify);
}

// A row's identity across the whole table. The same ticker can spike on more
// than one retained session, and those are separate rows with separate notes.
function rowKey(hit) {
  return `${hit.session_date || ""}|${hit.ticker}`;
}

const SORTS = {
  session_date: (a, b) => String(b.session_date || "").localeCompare(String(a.session_date || ""))
                          || b.rvol - a.rvol,
  rvol:       (a, b) => b.rvol - a.rvol,
  change_pct: (a, b) => b.change_pct - a.change_pct,
  turnover:   (a, b) => b.turnover - a.turnover,
  price:      (a, b) => b.price - a.price,
  market_cap: (a, b) => (b.market_cap || 0) - (a.market_cap || 0),
  name:       (a, b) => String(a.name).localeCompare(String(b.name)),
  ticker:     (a, b) => String(a.ticker).localeCompare(String(b.ticker)),
  sector:     (a, b) => String(a.sector || "~").localeCompare(String(b.sector || "~")),
};

const COLUMNS = [
  { key: "session_date", label: "Session", num: false },
  { key: "name",       label: "Company",   num: false },
  { key: "sector",     label: "Sector",    num: false },
  { key: "market_cap", label: "Mkt cap",   num: true },
  { key: "rvol",       label: "RVOL",      num: true },
  { key: "change_pct", label: "Change",    num: true },
  { key: "price",      label: "Price",     num: true },
  { key: "turnover",   label: "Turnover",  num: true },
];

const Movers = {
  market: null,
  data: null,
  markets: [],
  poll: null,
  // Newest session first, strongest spike within it — the table spans several
  // days now, so ordering by RVOL alone would interleave them.
  sort: { key: "session_date", asc: false },
  expanded: new Set(),

  async init() {
    await this.loadMarkets();
    marketSelect.addEventListener("change", () => this.select(marketSelect.value));
    scanBtn.addEventListener("click", () => this.startScan());
    forceBtn.addEventListener("click", () => this.startScan(true));
    stopBtn.addEventListener("click", () => this.stopScan());
    await this.load();
  },

  select(key) {
    if (!key || key === this.market) return;
    this.market = key;
    marketSelect.value = key;
    try { localStorage.setItem("movers.market", key); } catch (e) { /* private mode */ }
    this.expanded.clear();
    this.data = null;
    this.load();
  },

  async loadMarkets() {
    await this.refreshStates();
    marketSelect.innerHTML = "";
    for (const m of this.markets) {
      const opt = document.createElement("option");
      opt.value = m.key;
      opt.textContent = this.optionLabel(m);
      marketSelect.append(opt);
    }
    let saved = null;
    try { saved = localStorage.getItem("movers.market"); } catch (e) { /* private mode */ }
    const known = this.markets.some((m) => m.key === saved);
    this.market = known ? saved : (this.markets[0] && this.markets[0].key) || null;
    if (this.market) marketSelect.value = this.market;
  },

  optionLabel(m) {
    return m.last_session ? `${m.label} · ${m.last_session}` : m.label;
  },

  // Every market's scan state, not just the selected one — scans are
  // single-flight per market but run independently across them.
  async refreshStates() {
    try {
      const r = await fetch("/api/movers/markets", { cache: "no-store" });
      if (!r.ok) return;
      const payload = await r.json();
      this.markets = payload.markets || [];
    } catch (e) { /* offline — keep the states we already have */ }
  },

  running() {
    return this.markets.filter((m) => m.scan && m.scan.running);
  },

  async load() {
    if (!this.market) { this.renderEmpty("No markets are configured."); return; }
    await this.refreshStates();
    try {
      const r = await fetch(`/api/movers?market=${encodeURIComponent(this.market)}`,
                            { cache: "no-store" });
      if (r.ok) this.data = await r.json();
    } catch (e) { /* offline — leave the last render up */ }

    // Keep polling while ANY market is scanning: switching away from a running
    // market must not stop its progress from updating.
    const busy = this.running().length > 0;
    if (busy && !this.poll) {
      this.poll = setInterval(() => this.load(), POLL_MS);
    } else if (!busy && this.poll) {
      clearInterval(this.poll);
      this.poll = null;
    }
    this.renderActive();
    this.render();
  },

  renderActive() {
    const running = this.running();
    activeScans.hidden = running.length === 0;
    activeList.innerHTML = "";
    for (const m of running) {
      activeList.append(this.activeRow(m));
    }
    // Session dates change as runs finish; refresh the labels in place rather
    // than rebuilding the <select>, which would close it mid-interaction.
    for (const opt of marketSelect.options) {
      const m = this.markets.find((x) => x.key === opt.value);
      if (m) opt.textContent = this.optionLabel(m);
    }
  },

  activeRow(m) {
    const scan = m.scan || {};
    const li = document.createElement("li");
    li.className = "active-row";

    const name = document.createElement("div");
    name.className = "active-name";
    const dot = document.createElement("i");
    dot.className = "active-dot";
    const label = document.createElement("span");
    label.textContent = m.label;
    name.append(dot, label);

    const bar = document.createElement("div");
    bar.className = "active-bar";
    const fill = document.createElement("i");
    const pct = scan.total ? Math.min(100, Math.round((scan.done / scan.total) * 100)) : 0;
    fill.style.width = pct + "%";
    bar.append(fill);

    const status = document.createElement("div");
    status.className = "active-status";
    status.textContent = scan.total
      ? `${phaseLabel(scan.phase)} ${scan.done.toLocaleString()}/${scan.total.toLocaleString()} (${pct}%)`
      : phaseLabel(scan.phase) + "…";

    const view = document.createElement("button");
    view.className = "active-view";
    view.type = "button";
    if (m.key === this.market) {
      view.textContent = "viewing";
      view.disabled = true;
    } else {
      view.textContent = "view";
      view.addEventListener("click", () => this.select(m.key));
    }
    status.append(view);

    li.append(name, bar, status);
    return li;
  },

  render() {
    const d = this.data || {};
    const scan = d.scan || {};
    const running = !!scan.running;

    scanBtn.hidden = running;
    stopBtn.hidden = !running;
    // Only worth offering once a run has told us there is nothing newer.
    forceBtn.hidden = running || !scan.up_to_date;
    scanBtn.disabled = false;

    this.renderProgress(scan, running);
    this.renderMeta(d, scan);
    this.renderBanner(d, scan);

    const hits = d.hits || [];
    if (!hits.length) {
      // An empty result is a normal outcome, not an error — but only once a
      // scan has actually run. Before that, say so plainly.
      if (!d.session_date) {
        this.renderEmpty("Pick a market and run a scan to see what moved.",
                         "Nothing scanned yet");
      } else if (d.degraded) {
        this.renderEmpty(
          "The last scan couldn't reach enough of the market to trust the result. " +
          "Run it again in a few minutes.", "Scan was incomplete");
      } else {
        const n = (d.sessions || []).length;
        this.renderEmpty(
          n > 1
            ? `Every name traded within its usual range across the last ${n} ` +
              "scanned sessions. That's a quiet stretch, not a failure."
            : "Every name traded within its usual range on " + d.session_date +
              ". That's a normal, quiet session.",
          "No movers cleared the threshold");
      }
      return;
    }
    this.renderTable(d, hits);
  },

  renderProgress(scan, running) {
    // Hidden while the active-scans list is up: that list already shows this
    // market's progress alongside every other one, and two bars for the same
    // run reads as two runs.
    if (!running || !scan.total || !activeScans.hidden) { progress.hidden = true; return; }
    progress.hidden = false;
    const pct = Math.min(100, Math.round((scan.done / scan.total) * 100));
    progressFill.style.width = pct + "%";
    progressText.textContent =
      `${phaseLabel(scan.phase)} — ${scan.done}/${scan.total} (${pct}%)`;
  },

  renderMeta(d, scan) {
    const bits = [];
    if (scan.message) bits.push(scan.message);
    const st = d.stats || {};
    if (!scan.running && st.total) {
      bits.push(`${st.total.toLocaleString()} symbols in ${st.elapsed}s`);
    }
    if (d.generated_at) {
      const when = new Date(d.generated_at).toLocaleString("en-GB",
        { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
      bits.push(`Last run <b>${when}</b>`);
    }
    scanMeta.innerHTML = bits.join(" · ");
  },

  renderBanner(d, scan) {
    const st = d.stats || {};
    const notes = [];
    if (st.rate_limited) {
      notes.push("<b>Rate limited.</b> Yahoo throttled the last run, so these " +
                 "results are incomplete — try again in a few minutes.");
    } else if (d.degraded) {
      notes.push(`<b>Degraded run.</b> ${st.failed || 0} of ` +
                 `${st.total || 0} symbols failed, so movers may be missing.`);
    }
    if (d.universe_stale) {
      notes.push("The symbol list couldn't be refreshed, so a cached copy was used.");
    }
    if (!notes.length || scan.running) { banner.hidden = true; return; }
    banner.hidden = false;
    banner.innerHTML = notes.join(" ");
  },

  renderEmpty(message, title) {
    results.innerHTML = "";
    const box = document.createElement("div");
    box.className = "empty";
    box.innerHTML =
      `<div class="empty-mark">📈</div>
       <h3></h3><p></p>`;
    box.querySelector("h3").textContent = title || "Nothing to show";
    box.querySelector("p").textContent = message;
    results.append(box);
  },

  renderTable(d, hits) {
    const sorted = hits.slice().sort(SORTS[this.sort.key] || SORTS.rvol);
    if (this.sort.asc) sorted.reverse();

    results.innerHTML = "";

    const head = document.createElement("div");
    head.className = "result-head";
    const c = d.criteria || {};
    head.innerHTML =
      `<h3>${hits.length} mover${hits.length === 1 ? "" : "s"}</h3>
       <span class="session-badge"></span>
       <span class="criteria-note"></span>`;
    head.querySelector(".session-badge").textContent = sessionSpan(d);
    head.querySelector(".criteria-note").textContent =
      `RVOL ≥ ${c.min_rvol}× and change ≥ +${c.min_change_pct}% · ` +
      `${c.lookback}-day baseline`;
    results.append(head);

    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    const table = document.createElement("table");
    table.className = "movers";

    const thead = document.createElement("thead");
    const hrow = document.createElement("tr");
    for (const col of COLUMNS) {
      const th = document.createElement("th");
      th.textContent = col.label;
      th.classList.add("col-" + col.key);
      if (col.num) th.classList.add("num");
      if (this.sort.key === col.key) {
        th.classList.add("sorted");
        if (this.sort.asc) th.classList.add("asc");
      }
      th.addEventListener("click", () => {
        if (this.sort.key === col.key) this.sort.asc = !this.sort.asc;
        else this.sort = { key: col.key, asc: false };
        this.render();
      });
      hrow.append(th);
    }
    hrow.append(document.createElement("th"));   // note toggle column
    thead.append(hrow);
    table.append(thead);

    const tbody = document.createElement("tbody");
    for (const hit of sorted) tbody.append(...this.renderRow(hit, d));
    table.append(tbody);

    wrap.append(table);
    results.append(wrap);
  },

  renderRow(hit, d) {
    const key = rowKey(hit);
    const analysis = (d.analyses || {})[key];
    const open = this.expanded.has(key);

    const tr = document.createElement("tr");
    tr.className = "row-main";

    tr.append(cell(hit.session_date || "—", false, "col-session_date"));

    const name = document.createElement("td");
    name.className = "name-cell col-name";
    name.textContent = hit.name;
    const sub = document.createElement("small");
    sub.textContent = hit.ticker;
    // Repeated under the company for narrow screens, where the session column
    // is dropped — without it a row from five merged days has no date at all.
    const when = document.createElement("small");
    when.className = "row-session";
    when.textContent = hit.session_date || "";
    name.append(sub, when);
    tr.append(name);

    tr.append(cell(hit.sector || "—", !hit.sector, "col-sector"));
    tr.append(numCell(fmtCap(hit.market_cap, hit.currency), !hit.market_cap,
                      "col-market_cap"));

    const rvol = document.createElement("td");
    rvol.className = "num col-rvol";
    const badge = document.createElement("span");
    badge.className = "rvol-badge" + (hit.rvol >= 20 ? " blazing" : hit.rvol >= 10 ? " hot" : "");
    badge.textContent = hit.rvol.toFixed(1) + "×";
    rvol.append(badge);
    tr.append(rvol);

    const chg = document.createElement("td");
    chg.className = "num chg-up col-change_pct";
    chg.textContent = (hit.change_pct >= 0 ? "+" : "") + hit.change_pct.toFixed(1) + "%";
    tr.append(chg);

    tr.append(numCell(fmtNum(hit.price), false, "col-price"));
    tr.append(numCell(fmtCap(hit.turnover, hit.currency), false, "col-turnover"));

    const toggle = document.createElement("td");
    const btn = document.createElement("button");
    btn.className = "note-toggle";
    btn.textContent = noteLabel(analysis, open);
    toggle.append(btn);
    tr.append(toggle);

    const onToggle = () => {
      if (this.expanded.has(key)) this.expanded.delete(key);
      else this.expanded.add(key);
      this.render();
    };
    tr.addEventListener("click", onToggle);

    if (!open) return [tr];

    const noteRow = document.createElement("tr");
    noteRow.className = "row-note";
    const td = document.createElement("td");
    td.colSpan = COLUMNS.length + 1;
    td.append(renderNote(analysis));
    noteRow.append(td);
    return [tr, noteRow];
  },

  async startScan(force = false) {
    scanBtn.disabled = true;
    forceBtn.hidden = true;
    scanBtn.textContent = "Starting…";
    try {
      const r = await fetch("/api/movers/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ market: this.market, force }),
      });
      if (r.status === 409) banner.textContent = "A scan is already running for this market.";
    } catch (e) { /* the reload below surfaces the real state */ }
    scanBtn.textContent = "Run scan";
    if (!this.poll) this.poll = setInterval(() => this.load(), POLL_MS);
    await this.load();
  },

  async stopScan() {
    stopBtn.disabled = true;
    try {
      await fetch("/api/movers/scan/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ market: this.market }),
      });
    } catch (e) { /* ignore — the poll reports the real state */ }
    stopBtn.disabled = false;
    await this.load();
  },
};

// --- rendering helpers ------------------------------------------------------

function cell(text, muted, cls) {
  const td = document.createElement("td");
  td.textContent = text;
  if (muted) td.classList.add("muted");
  if (cls) td.classList.add(cls);
  return td;
}

function numCell(text, muted, cls) {
  const td = cell(text, muted, cls);
  td.classList.add("num");
  return td;
}

function noteLabel(analysis, open) {
  if (open) return "Hide ▴";
  if (!analysis) return "No note";
  if (analysis.status === "ok") return "Read note ▾";
  if (analysis.status === "skipped") return "Skipped";
  return "Note failed";
}

function renderNote(analysis) {
  if (!analysis) {
    const p = document.createElement("div");
    p.className = "note-status";
    p.textContent = "No note was written for this one yet.";
    return p;
  }
  if (analysis.status !== "ok") {
    const p = document.createElement("div");
    p.className = "note-status" + (analysis.status === "failed" ? " failed" : "");
    p.textContent = analysis.status === "skipped"
      ? "Skipped — " + (analysis.error || "beyond this run's note cap.")
      : "The note could not be written: " + (analysis.error || "unknown error");
    return p;
  }
  const div = document.createElement("div");
  div.className = "note-body";
  if (markdownReady()) {
    div.innerHTML = mdSafe(analysis.summary);
  } else {
    // The markdown CDN didn't load. Show the note as plain text with its line
    // breaks intact — unstyled but readable — rather than letting HTML collapse
    // a structured note into one unbroken wall of prose.
    div.classList.add("note-plain");
    div.textContent = analysis.summary;
  }
  if (analysis.model) {
    const foot = document.createElement("span");
    foot.className = "note-model";
    foot.textContent = analysis.model;
    div.append(foot);
  }
  return div;
}

// "2026-07-28" for one stored session, "2026-07-24 → 2026-07-28 · 5 sessions"
// for several. The table spans everything retained, so a single date in the
// header would misdescribe what is on screen.
function sessionSpan(d) {
  const dates = (d.sessions || []).map((s) => s.session_date).filter(Boolean);
  if (dates.length <= 1) return d.session_date || "";
  const oldest = dates[dates.length - 1];
  return `${oldest} → ${dates[0]} · ${dates.length} sessions`;
}

function phaseLabel(phase) {
  return phase === "analysing" ? "Writing notes"
       : phase === "enriching" ? "Fetching sector data"
       : phase === "checking" ? "Checking for a new session"
       : "Scanning";
}

function fmtNum(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString("en-GB", { maximumFractionDigits: 2 });
}

// Compact magnitudes: an Indian turnover figure runs to ten digits and a raw
// number in a table cell is unreadable.
function fmtCap(v, currency) {
  if (!v) return "—";
  const n = Number(v);
  const units = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
  for (const [size, suffix] of units) {
    if (n >= size) return (n / size).toFixed(n / size >= 100 ? 0 : 1) + suffix;
  }
  return n.toFixed(0);
}

Movers.init();
