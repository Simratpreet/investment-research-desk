"""Weekly to-do board.

Three buckets — prioritize / done / rejected — that reset every Monday. There is
no scheduled cleanup: tasks are keyed by the Monday that starts their week, so
when Monday arrives the "current week" key simply points at a new, empty file.
Past weeks stay on disk and are viewable (read-only); only the current week is
editable. Old weeks are pruned after `retention_weeks` to bound storage.

Layout under `base_dir`:
    <YYYY-MM-DD>.json     one week, keyed by that week's Monday (local time)

Concurrency mirrors the rest of the app (single worker, but tabs/threads can
race): one lock, atomic tmp+replace writes.
"""

import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta

import pytz

BUCKETS = ("prioritize", "done", "rejected")
_WEEK_RE = re.compile(r"\d{4}-\d{2}-\d{2}\.json\Z")
_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
MAX_TASK_CHARS = 500


def _now(tz):
    return datetime.now(tz)


class TodoStore:
    def __init__(self, base_dir, tz_name="Europe/London", retention_weeks=26):
        self._dir = base_dir
        self._tz = pytz.timezone(tz_name)
        self._retention = retention_weeks
        self._lock = threading.RLock()

    # --- week math ----------------------------------------------------------

    def current_week(self) -> str:
        """The Monday (YYYY-MM-DD, local time) that starts this week."""
        today = _now(self._tz).date()
        monday = today - timedelta(days=today.weekday())  # weekday(): Mon==0
        return monday.isoformat()

    def shift_week(self, week: str, weeks: int) -> str:
        """The Monday `weeks` weeks after (or before) `week`."""
        d = datetime.strptime(week, "%Y-%m-%d").date() + timedelta(weeks=weeks)
        return d.isoformat()

    def _editable(self, week: str) -> bool:
        """The current week and any future week are editable; past weeks are
        read-only (they've closed). ISO YYYY-MM-DD sorts chronologically."""
        return self._valid_week(week) and week >= self.current_week()

    # --- public API ---------------------------------------------------------

    def board(self, week: str = None) -> dict:
        """A week's board plus the list of weeks that exist. `week` defaults to
        the current one; an unknown/malformed week yields an empty board."""
        cur = self.current_week()
        wk = week if (week and self._valid_week(week)) else cur
        with self._lock:
            self._prune_locked()
            tasks = self._read_locked(wk)
            weeks = self._weeks_locked()
        # The current week always appears in the picker even before its first task.
        if cur not in weeks:
            weeks = sorted(set(weeks) | {cur}, reverse=True)
        return {
            "week": wk,
            "is_current": wk == cur,
            "editable": wk >= cur,          # current + future weeks are editable
            "current_week": cur,
            "next_week": self.shift_week(wk, 1),
            "buckets": {b: [t for t in tasks if t["bucket"] == b] for b in BUCKETS},
            "weeks": weeks,
        }

    def add(self, text: str, week: str = None) -> dict:
        """Add a task to a week's Prioritize bucket. `week` defaults to current;
        it must be the current or a future week (past weeks have closed)."""
        text = (text or "").strip()[:MAX_TASK_CHARS]
        if not text:
            raise ValueError("task text is required")
        wk = week or self.current_week()
        if not self._editable(wk):
            raise ValueError("that week is read-only")
        now = _now(self._tz).isoformat()
        task = {"id": uuid.uuid4().hex, "text": text, "bucket": "prioritize",
                "created_at": now, "updated_at": now}
        with self._lock:
            tasks = self._read_locked(wk)
            tasks.append(task)
            self._write_locked(wk, tasks)
        return task

    def update(self, task_id: str, week: str = None, bucket: str = None, text: str = None) -> bool:
        """Move (bucket) and/or rename a task within an editable week."""
        if not self._valid_id(task_id):
            return False
        wk = week or self.current_week()
        if not self._editable(wk):
            return False
        if bucket is not None and bucket not in BUCKETS:
            return False
        if text is not None:
            text = text.strip()[:MAX_TASK_CHARS]
            if not text:
                return False
        with self._lock:
            tasks = self._read_locked(wk)
            for t in tasks:
                if t["id"] == task_id:
                    if bucket is not None:
                        t["bucket"] = bucket
                    if text is not None:
                        t["text"] = text
                    t["updated_at"] = _now(self._tz).isoformat()
                    self._write_locked(wk, tasks)
                    return True
        return False

    def delete(self, task_id: str, week: str = None) -> bool:
        if not self._valid_id(task_id):
            return False
        wk = week or self.current_week()
        if not self._editable(wk):
            return False
        with self._lock:
            tasks = self._read_locked(wk)
            kept = [t for t in tasks if t["id"] != task_id]
            if len(kept) == len(tasks):
                return False
            self._write_locked(wk, kept)
        return True

    def move(self, task_id: str, from_week: str, to_week: str) -> bool:
        """Move a task from one editable week to another, keeping its bucket.
        The task's text is preserved; timestamps roll forward."""
        if not self._valid_id(task_id):
            return False
        if not (self._editable(from_week) and self._editable(to_week)) or from_week == to_week:
            return False
        with self._lock:
            src = self._read_locked(from_week)
            moved = next((t for t in src if t["id"] == task_id), None)
            if moved is None:
                return False
            src = [t for t in src if t["id"] != task_id]
            moved["updated_at"] = _now(self._tz).isoformat()
            dst = self._read_locked(to_week)
            dst.append(moved)
            self._write_locked(to_week, dst)   # write target first
            self._write_locked(from_week, src)  # then remove from source
        return True

    # --- internals ----------------------------------------------------------

    def _valid_week(self, week: str) -> bool:
        # YYYY-MM-DD that parses as a real date; can't contain path separators.
        try:
            datetime.strptime(week, "%Y-%m-%d")
            return True
        except (TypeError, ValueError):
            return False

    def _valid_id(self, task_id: str) -> bool:
        return bool(task_id) and bool(_ID_RE.fullmatch(task_id))

    def _path(self, week: str) -> str:
        return os.path.join(self._dir, f"{week}.json")

    def _read_locked(self, week: str) -> list:
        try:
            with open(self._path(week), "r", encoding="utf-8") as f:
                data = json.load(f)
            tasks = data.get("tasks") if isinstance(data, dict) else None
            # Drop anything malformed rather than trusting the file blindly.
            return [t for t in (tasks or [])
                    if isinstance(t, dict) and t.get("id") and t.get("bucket") in BUCKETS]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _write_locked(self, week: str, tasks: list):
        os.makedirs(self._dir, exist_ok=True)
        path = self._path(week)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"week": week, "tasks": tasks}, f, indent=2)
        os.replace(tmp, path)

    def _weeks_locked(self) -> list:
        try:
            names = os.listdir(self._dir)
        except OSError:
            return []
        return sorted((n[:-5] for n in names if _WEEK_RE.match(n)), reverse=True)

    def _prune_locked(self):
        weeks = self._weeks_locked()  # newest first
        for wk in weeks[self._retention:]:
            try:
                os.remove(self._path(wk))
            except OSError:
                pass
