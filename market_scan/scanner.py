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
from dataclasses import replace

from .domain import Market, PriceSeries, ScanCriteria, ScanResult
from .universe import MARKETS, UniverseRepository

# Above this share of hard failures we stop believing the result. An empty hit
# list from a healthy run means "nothing moved"; from a throttled run it means
# "we couldn't see". Conflating those is the worst bug this page could ship.
DEGRADED_FAILURE_RATE = 0.25


# How many of a market's largest names decide the probe. Big names are the most
# reliably present and the least likely to be halted or dormant, which is
# exactly what asking "what session is this market on?" needs.
PROBE_SAMPLE = 5

# Meta-quote fill tolerance: `regularMarketTime` counts as the closing bell if
# it is within this many seconds of the session end. Yahoo stamps the last
# trade, which for a thin name can land a few seconds before the bell; anything
# further out is an intraday price and must not stand in for a close.
FILL_EPSILON_S = 300.0


def _fill_pending_close(series: PriceSeries,
                        index: int) -> tuple[PriceSeries, bool]:
    """A provisional close for `index` from the chart meta, when the bar has
    none but the meta proves the session closed.

    Publication lag: Yahoo serves the session's timestamp and volume with a
    null close until its EOD job runs. The same payload's meta carries
    `regularMarketTime` (last trade) and `regularMarketPrice`; when the last
    trade is at the bell, regularMarketPrice IS the close. Verified live
    (2026-08-04): NVDA's 08-03 bar was close=null with volume 127.5M, while
    meta carried regularMarketPrice=206.64 at regularMarketTime=the bell — so
    Monday's change was computable from the payload the scanner already
    fetches, with zero extra requests and no auth.

    The detector is untouched: this happens at the feed/scanner boundary,
    before evaluation. A later chart-complete re-scan overwrites the
    provisional close with the official bar.
    """
    meta = series.meta or {}
    reg = ((meta.get("currentTradingPeriod") or {}).get("regular") or {})
    end = reg.get("end")
    rmt = meta.get("regularMarketTime")
    rmp = meta.get("regularMarketPrice")
    if end is None or rmt is None or rmp is None:
        return series, False
    if index != len(series) - 1:
        # The meta's regularMarketTime/regularMarketPrice describe the LAST
        # session's bell, so only the last bar can be the session they prove.
        # A mid-series null close is a different day's bar (the feed contract
        # says lag bars only occur at the series end) — filling it would stamp
        # the next session's closing price into the backfilled target day.
        # Reachable on an explicit session_date backfill of a still-pending
        # day run right after the next session's bell and before the target's
        # EOD publish.
        return series, False
    if not (rmt >= end - FILL_EPSILON_S):
        # Last trade before the bell: this is an intraday price, not a close.
        return series, False
    closes = list(series.closes)
    if closes[index] is None:
        closes[index] = float(rmp)
    else:
        return series, False
    prev = meta.get("chartPreviousClose")
    if index - 1 >= 0 and closes[index - 1] is None and prev is not None:
        closes[index - 1] = float(prev)
    return replace(series, closes=tuple(closes)), True


class MarketScanner:
    def __init__(self, repository: UniverseRepository, feed, selector, detector,
                 max_workers: int = 8):
        self._repo = repository
        self._feed = feed
        self._selector = selector
        self._detector = detector
        self._max_workers = max_workers

    def probe_session(self, market_key: str,
                      sample: int = PROBE_SAMPLE) -> str | None:
        """The session this market would report right now, from a few requests.

        Exists so a scan that would land on a session already stored can be
        skipped before it makes four thousand requests and re-reads a day
        already on disk. Exchanges publish a session's bars hours after the
        close — Yahoo serves the timestamp with null OHLCV in the meantime — so
        running a scan and getting yesterday again is routine, not an error.

        Returns None whenever the answer isn't clear: a feed failure, or no
        majority among the sampled names. The caller then does the full scan.
        Guessing wrong here would silently skip a real session, so the tie goes
        to doing the work.
        """
        entries, _ = self._repo.load(market_key)
        ranked = sorted(entries, key=lambda e: e.market_cap or 0,
                        reverse=True)[:max(1, sample)]
        if not ranked:
            return None
        dates = Counter()
        for entry in ranked:
            try:
                series = self._feed.fetch(entry.symbol)
            except Exception:
                continue
            if series is None:
                continue
            index = self._selector.target_index(series)
            if index is not None:
                dates[self._selector.session_date(series, index)] += 1
        if not dates:
            return None
        date, votes = dates.most_common(1)[0]
        # A real majority of the sample, so one stale name cannot decide it.
        return date if votes * 2 > len(ranked) else None

    def exchange_window(self, market_key: str) -> tuple[float, float] | None:
        """The (start, end) of Yahoo's current/next session window for the
        market, from one probe-sample name. None when the feed won't answer —
        the caller then makes the conservative calendar choice instead.
        """
        entries, _ = self._repo.load(market_key)
        ranked = sorted(entries, key=lambda e: e.market_cap or 0,
                        reverse=True)[:max(1, PROBE_SAMPLE)]
        for entry in ranked:
            try:
                series = self._feed.fetch(entry.symbol)
            except Exception:
                continue
            if series is None:
                continue
            reg = ((series.meta or {}).get("currentTradingPeriod") or {}).get("regular") or {}
            start, end = reg.get("start"), reg.get("end")
            if start is not None and end is not None:
                return float(start), float(end)
        return None

    def target_complete(self, market_key: str, target_date: str) -> bool:
        """True when any probe-sample name has a real close for `target_date`.

        Yahoo publishes a market's bars near-simultaneously, so one complete
        name is enough to know the day landed. Five requests, for the retry
        watcher to poll with instead of a full scan.
        """
        entries, _ = self._repo.load(market_key)
        ranked = sorted(entries, key=lambda e: e.market_cap or 0,
                        reverse=True)[:max(1, PROBE_SAMPLE)]
        for entry in ranked:
            try:
                series = self._feed.fetch(entry.symbol)
            except Exception:
                continue
            if series is None:
                continue
            index = self._index_for_date(series, target_date)
            if index is not None and series.closes[index] is not None:
                return True
        return False

    def _index_for_date(self, series: PriceSeries, target_date: str,
                        now: float | None = None) -> int | None:
        """The bar whose session date is `target_date`, or None.

        The in-progress last bar is excluded the same way target_index excludes
        it — a half-day bar must never satisfy the target. Older dates (a
        backfill) are found by scanning back from the end.
        """
        n = len(series)
        if n == 0:
            return None
        last = n - 1 if not self._selector.last_bar_in_progress(series, now) else n - 2
        for i in range(last, -1, -1):
            if self._selector.session_date(series, i) == target_date:
                return i
        return None

    def scan(self, market_key: str, target_date: str | None = None,
             progress_cb=None, stop_event=None,
             limit: int | None = None) -> ScanResult:
        market: Market = MARKETS[market_key]
        entries, universe_stale = self._repo.load(market_key)
        if limit:
            entries = entries[:limit]
        total = len(entries)
        started = time.monotonic()

        counts = Counter()
        session_dates = Counter()
        pending_dates = Counter()
        candidates = []           # (session_date, Hit)
        reported = 0              # names whose target-date bar exists
        pending_names = 0         # of those, still close-less after the fill
        filled = 0                # names whose close came from the meta fill
        lock = threading.Lock()
        done = 0

        def one(entry):
            nonlocal done, reported, pending_names, filled
            outcome, hit, day = "no_data", None, None
            name_pending = False
            name_filled = False
            try:
                if stop_event is None or not stop_event.is_set():
                    series = self._feed.fetch(entry.symbol)
                    if series is not None:
                        if target_date is not None:
                            index = self._index_for_date(series, target_date)
                            if index is not None:
                                day = target_date
                        else:
                            index = self._selector.target_index(series)
                            if index is not None:
                                day = self._selector.session_date(series, index)
                        if index is not None:
                            series, name_filled = _fill_pending_close(series, index)
                            name_pending = series.closes[index] is None
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
                    if name_filled:
                        filled += 1
                    if target_date is not None:
                        reported += 1
                        if name_pending:
                            pending_names += 1
                    elif name_pending:
                        pending_dates[day] += 1
                if hit is not None:
                    candidates.append((day, hit))
                done += 1
                progress = done
            if progress_cb is not None:
                progress_cb(progress, total)

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            list(pool.map(one, entries))

        if target_date is not None:
            # The run's session IS the target — the majority-date selection
            # below is only the no-target fallback. Names without a bar for it
            # are no_data, never a stale hit from an older session.
            session_date = target_date
            pending_completion = pending_names * 2 > reported
            stale = 0
        else:
            # The run's session is whichever day most symbols reported. Anything
            # older is a halted or long-dormant name whose "last completed
            # session" is weeks back — its 5x spike is against a stale baseline,
            # so it is counted as stale and dropped rather than shown as today's
            # news.
            session_date = session_dates.most_common(1)[0][0] if session_dates else ""
            pending_completion = (pending_dates[session_date] * 2 >
                                  session_dates[session_date]) if session_dates else False
            stale = sum(n for d, n in session_dates.items() if d != session_date)
        hits = tuple(sorted((h for d, h in candidates if d == session_date),
                            key=lambda h: h.rvol, reverse=True))

        scanned = counts["scanned"]
        failed = counts["failed"]
        # If the feed's breaker tripped, say so explicitly: "we were rate
        # limited" is a far more actionable message than "0 movers found".
        rate_limited = bool(getattr(self._feed, "tripped", lambda: False)())
        stats = {
            "total": total, "scanned": scanned, "no_data": counts["no_data"],
            "failed": failed, "skipped": counts["skipped"], "stale": stale,
            "hits": len(hits), "rate_limited": rate_limited,
            "target_reported": reported, "target_pending": pending_names,
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
            pending_completion=pending_completion,
            filled_from_quote=filled > 0,
        )


def build_scanner(universe_dir: str | None, criteria: ScanCriteria,
                  max_workers: int = 8,
                  max_age_days: float = 120.0) -> MarketScanner:
    """The standard wiring, so callers don't repeat the four constructors.

    `universe_dir` is the optional DATA_DIR override searched before the
    exports committed alongside the package.
    """
    from .detector import SpikeDetector
    from .feed import YahooPriceFeed
    from .session import SessionSelector
    return MarketScanner(
        UniverseRepository(universe_dir, max_age_days=max_age_days),
        YahooPriceFeed(),
        SessionSelector(),
        SpikeDetector(criteria),
        max_workers=max_workers,
    )
