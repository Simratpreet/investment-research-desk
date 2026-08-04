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
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .analyst import OpenRouterAnalyst
from .domain import HitAnalysis, ScanCriteria
from .enrich import enrich
from .scanner import build_scanner
from .session import target_session
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
                 retain_sessions: int = 5, universe_max_age_days: float = 120.0,
                 scanner=None, retry_intervals=(600, 1200, 2400, 4800, 9600, 14400),
                 retry_daily_s: int = 86400):
        self._store = store
        self._criteria = criteria
        self._api_key_fn = api_key_fn
        self._model = model
        self._analysis_max = analysis_max
        self._analysis_concurrency = analysis_concurrency
        self._retain_sessions = retain_sessions
        self._scanner = scanner or build_scanner(universe_dir, criteria,
                                                 max_workers=max_workers,
                                                 max_age_days=universe_max_age_days)
        # Retry-watcher cadence (seconds): poll for a traded-but-unpublished
        # session at these intervals — 10 min doubling up to 4 h — then drop to
        # a daily check. Env-overridable via config.py.
        self._retry_intervals = tuple(retry_intervals) or (600,)
        self._retry_daily_s = max(60, int(retry_daily_s))
        self._lock = threading.Lock()
        self._states: dict[str, dict] = {k: _idle_state() for k in MARKETS}
        self._stops: dict[str, threading.Event] = {k: threading.Event() for k in MARKETS}
        # (market, target) pairs with a watcher thread live; one per day, so a
        # re-pending scan cannot stack watchers.
        self._watchers: set[tuple[str, str]] = set()

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

    def start(self, market: str, force: bool = False,
              session_date: str | None = None) -> tuple[bool, str]:
        """Begin a scan. False when one is already running for this market —
        the blueprint turns that into a 409, matching News.

        `session_date` backfills that specific day. Without it the run targets
        the market's calendar session (see `_target_session`). `force` re-scans
        the target even when it is already stored — it is target-anchored, so
        it can never again re-buy the last Yahoo-complete day you did not ask
        for.
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
        threading.Thread(target=self._run, args=(market, force, session_date),
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

    def _target_session(self, market: str) -> str:
        """The intended session: the calendar's most recent completed trading
        day, independent of what Yahoo has published. Yahoo's own session
        window (one probe-sample request) decides whether today has closed; on
        any probe failure the resolver makes the conservative choice and
        targets yesterday.
        """
        window = None
        try:
            window = self._scanner.exchange_window(market)
        except Exception:
            window = None
        return target_session(MARKETS[market], window=window)

    def _held_session(self, market: str, target: str) -> str | None:
        """The stored session when the target is already covered — i.e. "up to
        date" is true. With calendar targeting, nothing newer than the target
        can exist, so the old probe comparison is gone. The remaining reasons
        to re-scan are a degraded/stopped run and a run whose closes came from
        the meta fill (the official bar has since landed; re-scanning replaces
        the provisional prices). A pending day is never held: it has no
        completed run to hold.
        """
        stored = self._store.latest(market)
        if not stored or stored.get("degraded") or stored.get("stopped"):
            return None
        if stored.get("session_date") != target:
            return None
        if stored.get("filled_from_quote"):
            return None
        return target

    def _run(self, market: str, force: bool = False,
             session_date: str | None = None):
        label = MARKETS[market].label
        target = session_date or self._target_session(market)

        if not force and not self._stops[market].is_set():
            held = self._held_session(market, target)
            if held:
                # The target is already stored and complete. Still top up any
                # notes the last run failed to write, then stop — re-reading a
                # day already on disk buys nothing and re-buys every note.
                self._set(market, phase="analysing",
                          message="Already scanned — checking notes…")
                added = self._analyse(market, held, label)
                message = f"Already up to date — {label} last closed on {held}."
                if added:
                    message += f" {added} missing note(s) written."
                self._finish(market, message, up_to_date=True)
                return

        self._set(market, phase="scanning", message="Scanning…")
        try:
            result = self._scanner.scan(
                market,
                target_date=target,
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

        if result.stopped:
            self._finish(market, f"Stopped — {len(result.hits)} mover(s) found so far.")
            return

        reported = result.stats.get("target_reported", 0)
        if result.pending_completion or reported == 0:
            # The target traded but no source has complete data for it. Never
            # persist this as a completed run — a zero-hit completed file would
            # read as "nothing moved" and skip the day forever — and never say
            # "nothing newer". Write the pending marker and arm the watcher to
            # finish the job.
            self._store.save_pending(market, target)
            self._watch(market, target)
            if result.degraded:
                message = (f"Scan was degraded — could not tell whether {target} "
                           f"traded; will retry automatically.")
            elif reported == 0:
                message = (f"{target} traded; Yahoo dropped this session. "
                           f"Will backfill when a source has it.")
            else:
                message = (f"{target} traded — no source has finished "
                           f"publishing; retrying automatically.")
            self._finish(market, message)
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

    def _watch(self, market: str, target: str):
        """Finish a traded-but-unpublished session in the background.

        Armed by the pending path in `_run`: the marker is on disk and this
        thread polls the cheap probe (`target_complete`, five requests) at the
        retry cadence, bumping the marker's attempt count each round, and
        fires the full scan the moment the target's bars land. It stops when
        the marker is gone — a completed run clears it, whether that run came
        from here or from a manual scan — which is also what makes it
        single-flight: a re-pended scan finds its (market, target) already
        registered and does not stack a second watcher.

        The loop never gives up while the marker lives: `start` is
        single-flight per market, so a scan already in flight (a user's own
        run, or one this watcher just fired) simply declines, and the run
        itself either clears the marker or re-pends — and the next round fires
        again. A user stop sets the stop event, which wakes this thread's wait
        and ends it.
        """
        with self._lock:
            if (market, target) in self._watchers:
                return
            self._watchers.add((market, target))

        def loop():
            try:
                attempt = 0
                while True:
                    if self._stops[market].is_set():
                        return
                    # None means the marker is gone: a completed run for the
                    # target landed and its save() cleared it.
                    if self._store.bump_pending(market, target) is None:
                        return
                    if self._scanner.target_complete(market, target):
                        # The day's bars are out. Fire the full scan; the run
                        # decides the rest — complete bars save and clear the
                        # marker, a still-partial publication re-pends (and
                        # re-arming this watcher is a no-op), so the next
                        # round just fires again.
                        self.start(market, session_date=target)
                    wait = (self._retry_intervals[attempt]
                            if attempt < len(self._retry_intervals)
                            else self._retry_daily_s)
                    attempt += 1
                    if self._stops[market].wait(wait):
                        return
            finally:
                with self._lock:
                    self._watchers.discard((market, target))

        threading.Thread(target=loop, daemon=True,
                         name=f"movers-watch-{market}-{target}").start()

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
