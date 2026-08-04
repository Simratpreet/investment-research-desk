"""Scan runs persisted on the volume.

One file per market per session — `<market>_<YYYY-MM-DD>.json` under
DATA_DIR/market_scan/runs/. On the volume rather than the container FS, per the
lesson the news store already learned: anything a page reads has to survive a
redeploy.

Writes are atomic (tmp + os.replace) and serialised by one RLock, matching
todo_store.py. That matters more here than it does for to-dos: enrichment and
analysis both write back into a run *while* the page is polling it, so a
half-written file would be read by the browser rather than merely by the next
request.
"""

import json
import os
import re
import threading

from .domain import Hit, HitAnalysis, ScanResult

_RUN_RE = re.compile(r"^(?P<market>[a-z0-9_]+)_(?P<date>\d{4}-\d{2}-\d{2})\.json$")


class ScanStore:
    def __init__(self, root: str, pending_root: str | None = None):
        self._root = root
        self._pending_root = pending_root or os.path.join(
            os.path.dirname(root.rstrip(os.sep)), "pending")
        self._lock = threading.RLock()

    # --- paths --------------------------------------------------------------

    def _path(self, market: str, session_date: str) -> str:
        return os.path.join(self._root, f"{market}_{session_date}.json")

    def _pending_path(self, market: str, session_date: str) -> str:
        return os.path.join(self._pending_root, f"{market}_{session_date}.json")

    # --- writing ------------------------------------------------------------

    def save(self, result: ScanResult) -> str:
        """Persist a completed scan. Called *before* enrichment or analysis, so
        a failure in either leaves a usable page rather than nothing.

        Notes already written for this session are kept. Re-scanning the same
        day is routine — Yahoo publishes a session's bars hours after the close,
        so a scan run too early legitimately reports yesterday and gets run
        again — and wiping the notes each time would re-buy every one of them
        from the model. Notes for names no longer in the result are dropped
        rather than left orphaned.
        """
        payload = result.to_dict()
        payload["generated_at"] = _now()
        with self._lock:
            existing = self._read(result.market, result.session_date) or {}
            tickers = {h.get("ticker") for h in payload.get("hits", [])}
            payload["analyses"] = {
                ticker: analysis
                for ticker, analysis in (existing.get("analyses") or {}).items()
                if ticker in tickers
            }
            self._write(result.market, result.session_date, payload)
        # A completed run is the one thing that clears the day's pending marker
        # (see save_pending).
        self.clear_pending(result.market, result.session_date)
        return self._path(result.market, result.session_date)

    def update_hit(self, market: str, session_date: str, ticker: str, **fields) -> bool:
        """Merge fields into one stored hit (used by enrichment)."""
        with self._lock:
            data = self._read(market, session_date)
            if not data:
                return False
            for hit in data.get("hits", []):
                if hit.get("ticker") == ticker:
                    hit.update(fields)
                    self._write(market, session_date, data)
                    return True
        return False

    def save_analysis(self, market: str, session_date: str,
                      analysis: HitAnalysis) -> bool:
        """Write one note back as it completes, so an outage part-way through a
        batch keeps every note produced so far."""
        with self._lock:
            data = self._read(market, session_date)
            if not data:
                return False
            data.setdefault("analyses", {})[analysis.ticker] = analysis.to_dict()
            self._write(market, session_date, data)
        return True

    # --- reading ------------------------------------------------------------

    def latest(self, market: str) -> dict | None:
        runs = self.list_runs(market)
        if not runs:
            return None
        with self._lock:
            return self._read(market, runs[0])

    def recent(self, market: str, sessions: int) -> list[dict]:
        """The last `sessions` stored runs for a market, newest session first.

        Unreadable files are skipped rather than raising: one corrupt run must
        not cost the days either side of it.
        """
        out = []
        with self._lock:
            for date in self.list_runs(market)[:max(0, sessions)]:
                data = self._read(market, date)
                if data:
                    out.append(data)
        return out

    def list_runs(self, market: str) -> list[str]:
        """Session dates for a market, newest first."""
        dates = []
        for name in self._names():
            m = _RUN_RE.match(name)
            if m and m.group("market") == market:
                dates.append(m.group("date"))
        return sorted(dates, reverse=True)

    # --- pending markers ----------------------------------------------------

    def save_pending(self, market: str, session_date: str,
                     attempts: int = 0) -> str:
        """Record that `session_date` traded but no source has complete data
        yet.

        Deliberately NOT a run: the page must show "waiting on data", never a
        zero-hit completed session — a completed file would make `latest()`
        skip the day forever. A completed run for the same date clears it (see
        `save`).
        """
        payload = {"session_date": session_date, "first_seen": _now(),
                   "attempts": attempts, "updated_at": _now()}
        with self._lock:
            self._write_pending(market, session_date, payload)
        return self._pending_path(market, session_date)

    def bump_pending(self, market: str, session_date: str) -> int | None:
        """Increment a pending marker's attempt count. None when the marker is
        gone — a completed run landed and cleared it — which tells the retry
        watcher to stop.
        """
        with self._lock:
            data = self._read_pending(market, session_date)
            if not data:
                return None
            attempts = int(data.get("attempts") or 0) + 1
            data["attempts"] = attempts
            data["updated_at"] = _now()
            self._write_pending(market, session_date, data)
            return attempts

    def pending(self, market: str) -> dict | None:
        """The newest pending marker for a market, or None."""
        dates = self.list_pending(market)
        if not dates:
            return None
        return self._read_pending(market, dates[0])

    def list_pending(self, market: str) -> list[str]:
        dates = []
        for name in self._pending_names():
            m = _RUN_RE.match(name)
            if m and m.group("market") == market:
                dates.append(m.group("date"))
        return sorted(dates, reverse=True)

    def clear_pending(self, market: str, session_date: str):
        try:
            os.remove(self._pending_path(market, session_date))
        except OSError:
            pass

    def prune(self, keep_sessions: int):
        """Keep the newest `keep_sessions` runs per market, drop the rest.

        Counted per market and by session date, not by file age. Age would be
        the wrong measure twice over: a market scanned once a week would lose
        everything between runs, and a redeploy that restores files from the
        image stamps them all with the build time.
        """
        keep = max(1, keep_sessions)
        with self._lock:
            by_market: dict[str, list[str]] = {}
            for name in self._names():
                m = _RUN_RE.match(name)
                if m:
                    by_market.setdefault(m.group("market"), []).append(name)
            for names in by_market.values():
                # Filenames end in the ISO session date, so a reverse string
                # sort is a reverse chronological sort.
                for stale in sorted(names, reverse=True)[keep:]:
                    try:
                        os.remove(os.path.join(self._root, stale))
                    except OSError:
                        pass

    # --- internals ----------------------------------------------------------

    def _names(self) -> list[str]:
        try:
            return os.listdir(self._root)
        except OSError:
            return []

    def _pending_names(self) -> list[str]:
        try:
            return os.listdir(self._pending_root)
        except OSError:
            return []

    def _read(self, market: str, session_date: str) -> dict | None:
        try:
            with open(self._path(market, session_date), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _read_pending(self, market: str, session_date: str) -> dict | None:
        try:
            with open(self._pending_path(market, session_date), "r",
                      encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write(self, market: str, session_date: str, payload: dict):
        os.makedirs(self._root, exist_ok=True)
        path = self._path(market, session_date)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)

    def _write_pending(self, market: str, session_date: str, payload: dict):
        os.makedirs(self._pending_root, exist_ok=True)
        path = self._pending_path(market, session_date)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def hits_from_stored(data: dict) -> list[Hit]:
    """Rebuild Hit objects from a stored run, for the analyst and enricher."""
    out = []
    for h in (data or {}).get("hits", []):
        try:
            out.append(Hit(
                ticker=h["ticker"], name=h.get("name") or h["ticker"],
                rvol=float(h.get("rvol") or 0), change_pct=float(h.get("change_pct") or 0),
                price=float(h.get("price") or 0), volume=float(h.get("volume") or 0),
                avg_volume=float(h.get("avg_volume") or 0),
                turnover=float(h.get("turnover") or 0),
                currency=h.get("currency") or "", session_date=h.get("session_date") or "",
                sector=h.get("sector"), market_cap=h.get("market_cap"),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out
