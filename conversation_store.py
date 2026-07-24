"""Persistent chat history for the Chat page.

A ConversationStore owns a directory on the volume and persists conversations as
plain JSON (text only — no audio, by design; see HANDOFF). It is the single
place that knows the on-disk layout, the write locking, and the retention
policy, so callers deal in conversations and turns rather than files.

Layout under `base_dir`:
    index.json          denormalised list for the sidebar (source of truth is
                        the per-conversation files; the index is a rebuilt cache)
    <id>.json           one conversation: metadata + ordered turns

Concurrency: the app runs a single worker (rate limiter, TTS semaphore and the
scheduler all assume it), but multiple browser tabs and the async answer worker
can still write concurrently, so every mutation takes one lock and writes
atomically (tmp + os.replace).

Retention: conversations untouched for `retention_days` are pruned. Pruning is
lazy — it runs on read and on write — so there is no background timer to own.
"""

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Conversation:
    """One conversation: metadata plus an ordered list of turns.

    A turn is a stored question/answer exchange, the same shape the Chat page
    renders, minus the audio: {question, answer, symbol, model, sources, ts}.
    This is a thin value object — persistence lives in ConversationStore.
    """

    TITLE_MAX = 70

    def __init__(self, id, title, symbol="", model="", created_at=None,
                 updated_at=None, turns=None):
        self.id = id
        self.title = title
        self.symbol = symbol
        self.model = model
        self.created_at = created_at or _now_iso()
        self.updated_at = updated_at or self.created_at
        self.turns = turns or []

    @classmethod
    def new(cls, symbol="", model="") -> "Conversation":
        return cls(id=uuid.uuid4().hex, title="New chat", symbol=symbol, model=model)

    @classmethod
    def from_dict(cls, d: dict) -> "Conversation":
        return cls(
            id=d["id"],
            title=d.get("title") or "Chat",
            symbol=d.get("symbol", "") or "",
            model=d.get("model", "") or "",
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
            turns=d.get("turns") or [],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "symbol": self.symbol,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turns": self.turns,
        }

    def summary(self) -> dict:
        """The lightweight shape the sidebar needs — no turn bodies."""
        return {
            "id": self.id,
            "title": self.title,
            "symbol": self.symbol,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turn_count": len(self.turns),
        }

    @staticmethod
    def _title_from(question: str) -> str:
        t = " ".join((question or "").split()).strip()
        if len(t) > Conversation.TITLE_MAX:
            t = t[:Conversation.TITLE_MAX - 1].rstrip() + "…"
        return t or "New chat"

    def add_turn(self, turn: dict):
        """Append a turn and roll the conversation's own metadata forward.

        The first turn's question seeds the title (until the user renames it),
        and symbol/model track the most recent turn so the sidebar chip and a
        resumed session reflect what the conversation currently is.
        """
        if not self.turns and self.title in ("", "New chat"):
            self.title = self._title_from(turn.get("question", ""))
        self.symbol = turn.get("symbol", self.symbol) or ""
        self.model = turn.get("model", self.model) or ""
        self.turns.append(turn)
        self.updated_at = _now_iso()


class ConversationStore:
    _ID_RE = re.compile(r"[0-9a-f]{32}\Z")

    def __init__(self, base_dir: str, retention_days: int = 7):
        self._dir = base_dir
        self._index_path = os.path.join(base_dir, "index.json")
        self._retention_secs = retention_days * 86400
        self._lock = threading.RLock()

    # --- public API ---------------------------------------------------------

    def list(self) -> list:
        """Conversation summaries, newest first, after pruning stale ones."""
        with self._lock:
            self._prune_locked()
            return [c.summary() for c in self._sorted_locked()]

    def get(self, conv_id: str):
        """A full conversation dict, or None if unknown/invalid/expired."""
        if not self._valid(conv_id):
            return None
        with self._lock:
            conv = self._load_locked(conv_id)
            if conv is None or self._expired(conv):
                return None
            return conv.to_dict()

    def append_turn(self, conv_id: str, turn: dict):
        """Upsert: create the conversation on its first turn, else append.

        Returns the conversation summary (so the caller can hand the id and
        title back to the client), or None if conv_id is malformed.
        """
        if not self._valid(conv_id):
            return None
        with self._lock:
            conv = self._load_locked(conv_id) or Conversation(
                id=conv_id, title="New chat",
                symbol=turn.get("symbol", ""), model=turn.get("model", ""))
            conv.add_turn(turn)
            self._save_locked(conv)
            self._prune_locked()
            return conv.summary()

    def rename(self, conv_id: str, title: str) -> bool:
        title = " ".join((title or "").split()).strip()[:Conversation.TITLE_MAX]
        if not self._valid(conv_id) or not title:
            return False
        with self._lock:
            conv = self._load_locked(conv_id)
            if conv is None:
                return False
            conv.title = title
            self._save_locked(conv)
            return True

    def delete(self, conv_id: str) -> bool:
        if not self._valid(conv_id):
            return False
        with self._lock:
            existed = self._delete_file_locked(conv_id)
            self._reindex_locked()
            return existed

    # --- internals ----------------------------------------------------------

    def _valid(self, conv_id: str) -> bool:
        # A 32-hex id can't contain a path separator or "..", so it can never
        # escape the store directory.
        return bool(conv_id) and bool(self._ID_RE.fullmatch(conv_id))

    def _path(self, conv_id: str) -> str:
        return os.path.join(self._dir, f"{conv_id}.json")

    def _expired(self, conv: "Conversation") -> bool:
        ts = _parse_iso(conv.updated_at)
        if ts is None:
            return False  # unparseable timestamp — keep rather than silently drop
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > self._retention_secs

    def _load_locked(self, conv_id: str):
        try:
            with open(self._path(conv_id), "r", encoding="utf-8") as f:
                return Conversation.from_dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError, KeyError, OSError):
            return None

    def _save_locked(self, conv: "Conversation"):
        os.makedirs(self._dir, exist_ok=True)
        self._atomic_write(self._path(conv.id), conv.to_dict())
        self._reindex_locked()

    def _delete_file_locked(self, conv_id: str) -> bool:
        try:
            os.remove(self._path(conv_id))
            return True
        except OSError:
            return False

    def _all_locked(self) -> list:
        """Every stored conversation, loading each file. Small volume of files
        for a single-user app; if this ever grows, the index becomes the list
        source instead."""
        out = []
        try:
            names = os.listdir(self._dir)
        except OSError:
            return out
        for name in names:
            if name == "index.json" or not name.endswith(".json"):
                continue
            conv = self._load_locked(name[:-5])
            if conv is not None:
                out.append(conv)
        return out

    def _sorted_locked(self) -> list:
        return sorted(self._all_locked(),
                      key=lambda c: c.updated_at or "", reverse=True)

    def _prune_locked(self):
        removed = False
        for conv in self._all_locked():
            if self._expired(conv):
                self._delete_file_locked(conv.id)
                removed = True
        if removed:
            self._reindex_locked()

    def _reindex_locked(self):
        """Rebuild index.json from the per-conversation files (they are the
        source of truth; the index is only a sidebar cache)."""
        os.makedirs(self._dir, exist_ok=True)
        index = [c.summary() for c in self._sorted_locked()]
        self._atomic_write(self._index_path, index)

    @staticmethod
    def _atomic_write(path: str, data):
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)


def _parse_iso(s):
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
