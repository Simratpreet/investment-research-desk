"""Picking the last *completed* session, and resolving the intended one.

This is the correctness detail a manually-run scanner can ignore and a scheduled
one cannot. During market hours Yahoo returns a partial, in-progress bar as the
last element of the daily series. Comparing a half-day's volume against a
full-day average reports nonsense — every liquid name looks quiet at 11am and
half the market looks like a spike at 15:25.

Yahoo hands us the session window in the chart `meta` block, so there is no
timezone table, no exchange close-time config and no holiday calendar to keep
current:

    meta.currentTradingPeriod.regular.start / .end   epoch seconds

The last daily bar is in progress iff it belongs to the session that is open
*right now*. Daily bars are stamped at the session open, so:

  - Mid-session:  timestamps[-1] >= regular.start and now < regular.end
                  -> in progress, drop it.
  - After close:  now >= regular.end
                  -> complete, keep it.
  - Overnight /
    weekend:      Yahoo rolls currentTradingPeriod forward to the next session,
                  so timestamps[-1] < regular.start
                  -> the last bar is the previous session's, complete, keep it.

The comparison uses the wall clock, deliberately, and NOT `meta.regularMarketTime`.
That field is the last *trade* time, not the current time, and for a thin stock
it lags the closing bell — measured live, `20MICRONS.NS` closed a session with a
regularMarketTime eight seconds before `regular.end`, and hours after the close
that still reads as "before the end". Using it would mark a finished session as
in-progress for precisely the illiquid names most likely to show a volume spike,
silently scanning yesterday for them and today for everyone else. `now` is
injectable so this stays unit-testable without freezing time globally.

`target_session` answers *which* session a scan is about, independently of Yahoo:
the most recent scheduled trading day whose session has ended (the calendar
walk). Session selection above then picks that day's bar out of the series.
"""

import time
from datetime import datetime, timedelta, timezone

from .domain import Market, PriceSeries


def _market_offset(market: Market) -> int:
    """The exchange's UTC offset in seconds, used for the calendar walk.

    The probe's `meta.gmtoffset` is preferred by the caller (it is DST-correct
    for right now); this is the fallback when no probe is available. An IANA
    zone is used where the host ships tzdata; on a bare image it degrades to
    UTC, which for the walk-back is never worse than off-by-a-few-hours around
    midnight — and only when the probe failed too.
    """
    if market.tz_name:
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(market.tz_name)
            return int(datetime.now(tz).utcoffset().total_seconds())
        except Exception:
            pass
    return 0


def target_session(market: Market, now: float | None = None,
                   window: tuple[float, float] | None = None,
                   gmtoffset_sec: int | None = None) -> str:
    """The most recent *completed* scheduled trading day for `market`.

    "Yesterday" is a calendar concept, not a Yahoo-publication artifact: the
    target is the newest trading day (walking back over weekends and the
    market's holiday list) whose session has actually ended. `window` is the
    (start, end) of Yahoo's current/next session from a probe sample; when its
    date is today and `now` is before its end, today's session is still open
    and the walk starts from yesterday. Without a window the same conservative
    choice is made, because targeting a session that has not closed yet would
    evaluate a half-day bar against a full-day baseline.
    """
    now = time.time() if now is None else now
    offset = gmtoffset_sec if gmtoffset_sec is not None else _market_offset(market)
    day = datetime.fromtimestamp(now + offset, tz=timezone.utc)
    today = day.date()
    if window is not None:
        start, end = window
        period_date = datetime.fromtimestamp(start + offset,
                                             tz=timezone.utc).date()
        today_ended = not (period_date == today and now < end)
    else:
        today_ended = False
    if not today_ended:
        day -= timedelta(days=1)
    holidays = market.holidays or frozenset()
    while True:
        if day.weekday() < 5 and day.strftime("%m-%d") not in holidays:
            return day.strftime("%Y-%m-%d")
        day -= timedelta(days=1)


class SessionSelector:
    def target_index(self, series: PriceSeries, now: float | None = None) -> int | None:
        """Index of the last completed daily bar, or None if there isn't one."""
        n = len(series)
        if n == 0:
            return None
        idx = n - 1 if not self.last_bar_in_progress(series, now) else n - 2
        return idx if idx >= 0 else None

    def last_bar_in_progress(self, series: PriceSeries, now: float | None = None) -> bool:
        reg = ((series.meta or {}).get("currentTradingPeriod") or {}).get("regular") or {}
        start, end = reg.get("start"), reg.get("end")
        if start is None or end is None:
            # No trading-period metadata: assume the bar is complete rather than
            # silently discarding a real session. The scanner's modal-date check
            # catches anything that lands on the wrong day.
            return False
        now = time.time() if now is None else now
        return series.timestamps[-1] >= start and now < end

    def session_date(self, series: PriceSeries, index: int) -> str:
        """The bar's calendar date in the exchange's own timezone.

        A US bar stamped 14:30 UTC and an Indian bar stamped 03:45 UTC are both
        "that day" locally; converting with the exchange offset keeps the run's
        session_date readable and comparable across names in one market.
        """
        offset = (series.meta or {}).get("gmtoffset") or 0
        ts = series.timestamps[index] + int(offset)
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
