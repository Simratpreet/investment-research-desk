"""Orchestration: single-flight scans on a background thread, with live progress.

Mirrors the `_news_scan` / `_ann_scan` pattern in server.py — a state dict plus a
lock, a daemon thread, and a stop endpoint — but per market and encapsulated
here so the blueprint stays thin. Those two shell out to a subprocess; this runs
in-process because the scanner is a library, not a script.

The pipeline order is the load-bearing decision:

    scan  ->  PERSIST  ->  enrich  ->  analyse

The result is written to disk before either enrichment or analysis begins, and
both write back incrementally. A total OpenRouter outage, a yfinance 429 or a
container restart mid-batch therefore costs notes, never the scan itself. The
page renders from whatever is on disk at the moment it asks.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .analyst import OpenRouterAnalyst
from .domain import HitAnalysis, ScanCriteria
from .enrich import enrich
from .scanner import build_scanner
from .store import ScanStore, hits_from_stored
from .universe import MARKETS, UniverseUnavailable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _idle_state() -> dict:
    return {"running": False, "started_at": None, "finished_at": None,
            "message": "", "phase": "idle", "done": 0, "total": 0,
            "stopped": False,
            # True when the last run stopped early because the market had not
            # published a session newer than the one already stored. The page
            # turns this into a "Rescan anyway" button.
            "up_to_date": False}


class ScanService:
    def __init__(self, store: ScanStore, universe_dir: str, criteria: ScanCriteria,
                 *, api_key_fn, model: str, max_workers: int = 8,
                 analysis_max: int = 40, analysis_concurrency: int = 3,
                 retain_sessions: int = 5, universe_max_age_days: float = 120.0):
        self._store = store
        self._criteria = criteria
        self._api_key_fn = api_key_fn
        self._model = model
        self._analysis_max = analysis_max
        self._analysis_concurrency = analysis_concurrency
        self._retain_sessions = retain_sessions
        self._scanner = build_scanner(universe_dir, criteria,
                                      max_workers=max_workers,
                                      max_age_days=universe_max_age_days)
        self._lock = threading.Lock()
        self._states: dict[str, dict] = {k: _idle_state() for k in MARKETS}
        self._stops: dict[str, threading.Event] = {k: threading.Event() for k in MARKETS}

    # --- state --------------------------------------------------------------

    def state(self, market: str) -> dict:
        with self._lock:
            return dict(self._states.get(market) or _idle_state())

    def states(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._states.items()}

    def _set(self, market: str, **fields):
        with self._lock:
            self._states[market].update(fields)

    # --- control ------------------------------------------------------------

    def start(self, market: str, force: bool = False) -> tuple[bool, str]:
        """Begin a scan. False when one is already running for this market —
        the blueprint turns that into a 409, matching News.

        `force` re-scans a session already on disk. Without it the run checks
        first and stops if the market has published nothing newer.
        """
        if market not in MARKETS:
            return False, "unknown market"
        with self._lock:
            if self._states[market]["running"]:
                return False, "A scan is already running for this market"
            self._states[market] = _idle_state()
            self._states[market].update(running=True, started_at=_now(),
                                        phase="checking",
                                        message="Checking for a new session…"
                                                if not force else "Scanning…")
        self._stops[market].clear()
        threading.Thread(target=self._run, args=(market, force),
                         daemon=True).start()
        return True, "Scan started"

    def stop(self, market: str) -> bool:
        with self._lock:
            if not self._states.get(market, {}).get("running"):
                return False
            self._states[market]["stopped"] = True
            self._states[market]["message"] = "Stopping…"
        self._stops[market].set()
        return True

    # --- the run ------------------------------------------------------------

    def _held_session(self, market: str) -> str | None:
        """The stored session, if the market has published nothing newer.

        A few requests answer this; the full scan is thousands. Only a clean,
        completed run counts — a degraded or stopped one is worth redoing, and
        so is a session with notes still missing, which is why the caller still
        runs the analysis pass before finishing.
        """
        stored = self._store.latest(market)
        if not stored or stored.get("degraded") or stored.get("stopped"):
            return None
        session = stored.get("session_date")
        if not session:
            return None
        try:
            probed = self._scanner.probe_session(market)
        except Exception:
            return None      # unclear: fall through and do the real scan
        return session if probed == session else None

    def _run(self, market: str, force: bool = False):
        label = MARKETS[market].label

        if not force and not self._stops[market].is_set():
            held = self._held_session(market)
            if held:
                # Nothing newer to scan. Still top up any notes the last run
                # failed to write, then stop — re-reading a day already on disk
                # buys nothing and re-buys every note.
                self._set(market, phase="analysing",
                          message="Already scanned — checking notes…")
                added = self._analyse(market, held, label)
                message = (f"Already up to date — {label} has published nothing "
                           f"newer than {held}.")
                if added:
                    message += f" {added} missing note(s) written."
                self._finish(market, message, up_to_date=True)
                return

        self._set(market, phase="scanning", message="Scanning…")
        try:
            result = self._scanner.scan(
                market,
                progress_cb=lambda done, total: self._set(market, done=done, total=total),
                stop_event=self._stops[market],
            )
        except UniverseUnavailable as e:
            # A missing symbol list is a loud failure, never an empty scan.
            self._finish(market, f"Could not load the {label} universe: {e}")
            return
        except Exception as e:
            self._finish(market, f"Scan failed: {e}")
            return

        if not result.session_date:
            self._finish(market, "Scan produced no usable sessions — "
                                 "Yahoo may be rate limiting.")
            return

        self._store.save(result)
        self._store.prune(self._retain_sessions)

        if result.stopped:
            self._finish(market, f"Stopped — {len(result.hits)} mover(s) found so far.")
            return

        hits = list(result.hits)
        if not hits:
            self._finish(market, "Scan complete — no movers cleared the threshold."
                         if not result.degraded else
                         "Scan finished but was degraded — results are incomplete.")
            return

        # Enrichment and analysis are enrichment, never gates: the scan is
        # already on disk, so anything below can fail without costing the run.
        self._set(market, phase="enriching",
                  message=f"{len(hits)} mover(s) — fetching sector data…")
        try:
            enrich(hits, self._store, market, result.session_date)
        except Exception:
            pass

        analysed = self._analyse(market, result.session_date, label)
        suffix = f" · {analysed} note(s) written" if analysed else ""
        self._finish(market, f"Scan complete — {len(hits)} mover(s).{suffix}")

    def _analyse(self, market: str, session_date: str, label: str) -> int:
        """Write a note per hit, capped by `analysis_max` as the cost guard.

        Notes run concurrently. Each is an independent web-search call taking
        the better part of a minute, so a serial pass over a 40-hit day would
        leave the page half-written for half an hour. Concurrency is bounded —
        by the pool here and by the analyst's own semaphore — because the point
        is to overlap the waiting, not to open forty billable calls at once.
        """
        stored = self._store.latest(market) or {}
        hits = hits_from_stored(stored)     # re-read so notes see enriched sectors
        hits = [h for h in hits if h.session_date == session_date]

        # A note already written for this session is not bought again. Only an
        # `ok` note counts as done — a failure or a cap skip is retried, which
        # is what makes re-running a scan the way to fill in what an OpenRouter
        # outage cost you.
        done = {ticker for ticker, analysis in (stored.get("analyses") or {}).items()
                if (analysis or {}).get("status") == "ok"}
        pending = [h for h in hits if h.ticker not in done]
        targets, over_cap = (pending[:self._analysis_max],
                             pending[self._analysis_max:])

        for hit in over_cap:
            self._store.save_analysis(market, session_date, HitAnalysis(
                hit.ticker, status="skipped",
                error=f"beyond the {self._analysis_max}-note cap for one run"))

        if not targets:
            return 0
        workers = max(1, self._analysis_concurrency)
        analyst = OpenRouterAnalyst(self._api_key_fn(), self._model,
                                    market_label=label, concurrency=workers)
        total = len(targets)
        counted = threading.Lock()
        done = written = 0
        self._set(market, phase="analysing", done=0, total=total,
                  message=f"Writing notes… 0/{total}")

        def one(hit):
            nonlocal done, written
            if self._stops[market].is_set():
                return
            try:
                analysis = analyst.explain(hit)
                # Persisted as each note lands, so an outage part way through a
                # batch keeps every note produced so far. ScanStore serialises
                # its own writes, so concurrent saves are safe.
                self._store.save_analysis(market, session_date, analysis)
                ok = analysis.status == "ok"
            except Exception:
                # explain() doesn't raise by contract and the store is
                # defensive, but one note failing must never cost the others.
                ok = False
            with counted:
                done += 1
                written += 1 if ok else 0
                progress = done
            self._set(market, done=progress, total=total,
                      message=f"Writing notes… {progress}/{total}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, targets))
        return written

    def _finish(self, market: str, message: str, up_to_date: bool = False):
        self._set(market, running=False, finished_at=_now(), phase="idle",
                  message=message, up_to_date=up_to_date)
