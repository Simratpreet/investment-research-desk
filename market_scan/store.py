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
import time

from .domain import Hit, HitAnalysis, ScanResult

_RUN_RE = re.compile(r"^(?P<market>[a-z0-9_]+)_(?P<date>\d{4}-\d{2}-\d{2})\.json$")


class ScanStore:
    def __init__(self, root: str):
        self._root = root
        self._lock = threading.RLock()

    # --- paths --------------------------------------------------------------

    def _path(self, market: str, session_date: str) -> str:
        return os.path.join(self._root, f"{market}_{session_date}.json")

    # --- writing ------------------------------------------------------------

    def save(self, result: ScanResult) -> str:
        """Persist a completed scan. Called *before* enrichment or analysis, so
        a failure in either leaves a usable page rather than nothing."""
        payload = result.to_dict()
        payload["generated_at"] = _now()
        payload["analyses"] = {}
        with self._lock:
            self._write(result.market, result.session_date, payload)
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

    def list_runs(self, market: str) -> list[str]:
        """Session dates for a market, newest first."""
        dates = []
        for name in self._names():
            m = _RUN_RE.match(name)
            if m and m.group("market") == market:
                dates.append(m.group("date"))
        return sorted(dates, reverse=True)

    def prune(self, retention_days: int):
        """Drop runs older than `retention_days`. Bounds volume growth: a daily
        scan of four markets is ~1,500 files a year otherwise."""
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            for name in self._names():
                if not _RUN_RE.match(name):
                    continue
                path = os.path.join(self._root, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass

    # --- internals ----------------------------------------------------------

    def _names(self) -> list[str]:
        try:
            return os.listdir(self._root)
        except OSError:
            return []

    def _read(self, market: str, session_date: str) -> dict | None:
        try:
            with open(self._path(market, session_date), "r", encoding="utf-8") as f:
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
