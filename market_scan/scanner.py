"""Runs one market's universe through the feed and the detector.

The single rule that matters here: **one bad symbol must never end the run.**
A 4,000-name scan will always contain delisted tickers, halted names and
whatever Yahoo happens to be serving badly that minute. Every per-symbol
exception is caught and counted; the run's health is reported through `stats`
and the `degraded` flag instead.
"""

import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .domain import Market, ScanCriteria, ScanResult
from .universe import MARKETS, UniverseRepository

# Above this share of hard failures we stop believing the result. An empty hit
# list from a healthy run means "nothing moved"; from a throttled run it means
# "we couldn't see". Conflating those is the worst bug this page could ship.
DEGRADED_FAILURE_RATE = 0.25


class MarketScanner:
    def __init__(self, repository: UniverseRepository, feed, selector, detector,
                 max_workers: int = 8):
        self._repo = repository
        self._feed = feed
        self._selector = selector
        self._detector = detector
        self._max_workers = max_workers

    def scan(self, market_key: str, progress_cb=None, stop_event=None,
             limit: int | None = None) -> ScanResult:
        market: Market = MARKETS[market_key]
        entries, universe_stale = self._repo.load(market_key)
        if limit:
            entries = entries[:limit]
        total = len(entries)
        started = time.monotonic()

        counts = Counter()
        session_dates = Counter()
        candidates = []           # (session_date, Hit)
        lock = threading.Lock()
        done = 0

        def one(entry):
            nonlocal done
            outcome, hit, day = "no_data", None, None
            try:
                if stop_event is None or not stop_event.is_set():
                    series = self._feed.fetch(entry.symbol)
                    if series is not None:
                        index = self._selector.target_index(series)
                        if index is not None:
                            day = self._selector.session_date(series, index)
                            hit = self._detector.evaluate(entry, series, index,
                                                          market, day)
                            outcome = "scanned"
                else:
                    outcome = "skipped"
            except Exception:
                # Deliberately broad: a feed error, a malformed payload and an
                # arithmetic surprise all mean the same thing here — this one
                # name is lost, the other 3,999 are not.
                outcome = "failed"
            with lock:
                counts[outcome] += 1
                if day:
                    session_dates[day] += 1
                if hit is not None:
                    candidates.append((day, hit))
                done += 1
                progress = done
            if progress_cb is not None:
                progress_cb(progress, total)

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            list(pool.map(one, entries))

        # The run's session is whichever day most symbols reported. Anything
        # older is a halted or long-dormant name whose "last completed session"
        # is weeks back — its 5x spike is against a stale baseline, so it is
        # counted as stale and dropped rather than shown as today's news.
        session_date = session_dates.most_common(1)[0][0] if session_dates else ""
        hits = tuple(sorted((h for d, h in candidates if d == session_date),
                            key=lambda h: h.rvol, reverse=True))
        stale = sum(n for d, n in session_dates.items() if d != session_date)

        scanned = counts["scanned"]
        failed = counts["failed"]
        # If the feed's breaker tripped, say so explicitly: "we were rate
        # limited" is a far more actionable message than "0 movers found".
        rate_limited = bool(getattr(self._feed, "tripped", lambda: False)())
        stats = {
            "total": total, "scanned": scanned, "no_data": counts["no_data"],
            "failed": failed, "skipped": counts["skipped"], "stale": stale,
            "hits": len(hits), "rate_limited": rate_limited,
            "elapsed": round(time.monotonic() - started, 1),
        }
        return ScanResult(
            market=market_key,
            session_date=session_date,
            hits=hits,
            criteria=self._detector.criteria,
            stats=stats,
            degraded=rate_limited or (total > 0 and (failed / total) > DEGRADED_FAILURE_RATE),
            universe_stale=universe_stale,
            stopped=bool(stop_event is not None and stop_event.is_set()),
        )


def build_scanner(cache_dir: str, criteria: ScanCriteria, max_workers: int = 8,
                  ttl_days: float = 7.0) -> MarketScanner:
    """The standard wiring, so callers don't repeat the four constructors."""
    from .detector import SpikeDetector
    from .feed import YahooPriceFeed
    from .session import SessionSelector
    return MarketScanner(
        UniverseRepository(cache_dir, ttl_days=ttl_days),
        YahooPriceFeed(),
        SessionSelector(),
        SpikeDetector(criteria),
        max_workers=max_workers,
    )
