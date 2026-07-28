"""Picking the last *completed* session.

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
"""

import time
from datetime import datetime, timezone

from .domain import PriceSeries


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
