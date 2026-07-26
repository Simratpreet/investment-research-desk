"use strict";
// Weekly to-do board. The current week is editable; past weeks are read-only.
// "Cleanup every Monday" is implicit: the server keys tasks by the week's Monday,
// so a new week is simply a new (empty) board — there's no scheduled reset.

const BUCKETS = [
  { key: "prioritize", title: "Prioritise", empty: "Nothing prioritised yet." },
  { key: "done",       title: "Done",       empty: "Nothing done yet." },
  { key: "rejected",   title: "Rejected",   empty: "Nothing rejected." },
];

const boardEl = document.getElementById("board");
const addRow = document.getElementById("addRow");
const taskInput = document.getElementById("taskInput");
const weekRangeEl = document.getElementById("weekRange");
const weekBadgeEl = document.getElementById("weekBadge");
const readonlyNote = document.getElementById("readonlyNote");
const prevBtn = document.getElementById("prevWeek");
const nextBtn = document.getElementById("nextWeek");
const thisBtn = document.getElementById("thisWeek");

const Todo = {
  viewing: null,   // week key being viewed (null => current)
  data: null,      // last board payload

  async load(week) {
    try {
      const q = week ? "?week=" + encodeURIComponent(week) : "";
      const r = await fetch("/api/todos" + q, { cache: "no-store" });
      if (!r.ok) return;
      this.data = await r.json();
      this.viewing = this.data.week;
      this.render();
    } catch (e) { /* offline — leave last render up */ }
  },

  render() {
    const d = this.data;
    if (!d) return;
    const ro = !d.editable;      // current + future are editable; past is read-only

    const [badgeText, badgeClass] = weekBadge(d);
    weekRangeEl.textContent = fmtRange(d.week);
    weekBadgeEl.textContent = badgeText;
    weekBadgeEl.className = "week-badge " + badgeClass;
    addRow.hidden = ro;
    readonlyNote.hidden = !ro;
    thisBtn.hidden = d.is_current;
    nextBtn.disabled = false;    // future weeks are for planning ahead
    taskInput.placeholder = "Add a task to " +
      (badgeText === "Future" ? "this week" : badgeText.toLowerCase()) + "…";

    boardEl.classList.toggle("is-readonly", ro);
    boardEl.innerHTML = "";
    for (const b of BUCKETS) {
      boardEl.append(this.renderColumn(b, d.buckets[b.key] || [], ro));
    }
  },

  renderColumn(b, tasks, ro) {
    const col = document.createElement("div");
    col.className = "col col-" + b.key;

    const head = document.createElement("div");
    head.className = "col-head";
    const dot = document.createElement("span"); dot.className = "col-dot";
    const title = document.createElement("span"); title.className = "col-title"; title.textContent = b.title;
    const count = document.createElement("span"); count.className = "col-count"; count.textContent = tasks.length;
    head.append(dot, title, count);

    const body = document.createElement("div");
    body.className = "col-body";
    if (!tasks.length) {
      const e = document.createElement("div"); e.className = "col-empty"; e.textContent = b.empty;
      body.append(e);
    } else {
      for (const t of tasks) body.append(this.renderTask(t, b.key, ro));
    }

    col.append(head, body);
    return col;
  },

  renderTask(t, bucket, ro) {
    const card = document.createElement("div");
    card.className = "task";
    const text = document.createElement("div");
    text.className = "task-text";
    text.textContent = t.text;   // textContent — user data, never innerHTML
    card.append(text);

    if (ro) return card;   // past weeks: no actions

    const acts = document.createElement("div");
    acts.className = "task-actions";
    const act = (label, cls, fn) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "task-act " + cls; b.textContent = label;
      b.addEventListener("click", fn);
      return b;
    };
    if (bucket === "prioritize") {
      acts.append(
        act("✓ Done", "done", () => this.move(t.id, "done")),
        act("✕ Reject", "reject", () => this.move(t.id, "rejected")),
        act("→ Next wk", "", () => this.defer(t.id)),
        act("Edit", "", () => this.edit(t)),
        act("Delete", "del", () => this.remove(t.id)),
      );
    } else {
      acts.append(
        act("↩ Prioritise", "", () => this.move(t.id, "prioritize")),
        act("Delete", "del", () => this.remove(t.id)),
      );
    }
    card.append(acts);
    return card;
  },

  // The week the current view's tasks operate on (the viewed week).
  get week() { return this.viewing || (this.data && this.data.current_week); },

  async add(text) {
    const r = await fetch("/api/todos", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, week: this.week }),
    });
    if (!r.ok) return false;
    await this.load(this.week);   // reloads the week the task landed in
    return true;
  },

  async move(id, bucket) {
    const r = await fetch("/api/todos/" + encodeURIComponent(id), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bucket, week: this.week }),
    });
    if (r.ok) this.load(this.week);
  },

  // Defer a task to the week after the one being viewed.
  async defer(id) {
    const to = shiftWeekKey(this.week, 1);
    const r = await fetch("/api/todos/" + encodeURIComponent(id), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ week: this.week, to_week: to }),
    });
    if (r.ok) this.load(this.week);
  },

  async edit(t) {
    const next = window.prompt("Edit task:", t.text);
    if (next == null) return;
    const text = next.trim();
    if (!text || text === t.text) return;
    const r = await fetch("/api/todos/" + encodeURIComponent(t.id), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, week: this.week }),
    });
    if (r.ok) this.load(this.week);
  },

  async remove(id) {
    const r = await fetch("/api/todos/" + encodeURIComponent(id) +
                          "?week=" + encodeURIComponent(this.week), { method: "DELETE" });
    if (r.ok || r.status === 404) this.load(this.week);
  },

  step(deltaDays) {
    const base = this.viewing || (this.data && this.data.current_week);
    if (!base) return;
    const d = new Date(base + "T00:00:00");
    d.setDate(d.getDate() + deltaDays);
    this.load(isoDate(d));
  },
};

// --- date helpers -----------------------------------------------------------
function isoDate(d) {
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
         "-" + String(d.getDate()).padStart(2, "0");
}
function weekKeyCompare(a, b) { return a < b ? -1 : a > b ? 1 : 0; }
function shiftWeekKey(key, weeks) {
  const d = new Date(key + "T00:00:00");
  d.setDate(d.getDate() + weeks * 7);
  return isoDate(d);
}
// [label, css-class] for the week badge, relative to the current week.
function weekBadge(d) {
  if (d.is_current) return ["This week", "current"];
  if (d.week === shiftWeekKey(d.current_week, 1)) return ["Next week", "future"];
  if (d.week > d.current_week) return ["Future", "future"];
  return ["Past week", "past"];
}
function fmtRange(mondayKey) {
  const mon = new Date(mondayKey + "T00:00:00");
  const sun = new Date(mon); sun.setDate(sun.getDate() + 6);
  const md = { day: "numeric", month: "short" };
  const sameMonth = mon.getMonth() === sun.getMonth();
  const left = mon.toLocaleDateString("en-GB", md);
  const right = sun.toLocaleDateString("en-GB",
    sameMonth ? { day: "numeric", month: "short", year: "numeric" } : { day: "numeric", month: "short", year: "numeric" });
  return left + " – " + right;
}

// --- wire up ----------------------------------------------------------------
addRow.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = taskInput.value.trim();
  if (!text) return;
  taskInput.value = "";
  await Todo.add(text);
  taskInput.focus();
});
prevBtn.addEventListener("click", () => Todo.step(-7));
nextBtn.addEventListener("click", () => { if (!nextBtn.disabled) Todo.step(7); });
thisBtn.addEventListener("click", () => Todo.load());

Todo.load();
