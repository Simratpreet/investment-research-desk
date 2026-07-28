"""The spike test. Pure: no network, no clock, no config lookups.

Everything the detector needs arrives as an argument, so the boundary cases —
short history, a zero-volume baseline, a stock that gapped from nothing — are
ordinary unit tests rather than something you can only find in production.
"""

from .domain import Hit, Market, PriceSeries, ScanCriteria, UniverseEntry


class SpikeDetector:
    def __init__(self, criteria: ScanCriteria):
        self.criteria = criteria

    def evaluate(self, entry: UniverseEntry, series: PriceSeries, index: int,
                 market: Market, session_date: str) -> Hit | None:
        """A Hit if the bar at `index` cleared the criteria, else None."""
        lookback = self.criteria.lookback
        # The baseline is the `lookback` sessions ending with the bar before the
        # target, so index `lookback` is the earliest bar that has a full window
        # behind it. Anything earlier is a recent listing whose "average volume"
        # would be an average of whatever happened to exist.
        if index < lookback or index >= len(series):
            return None

        meta = series.meta or {}
        # Companies only. The committed exports carry no funds, but a refreshed
        # export dropped into DATA_DIR might, and a leveraged or crypto ETF
        # genuinely does post 5x volume on a +5% day — exactly the row that
        # would crowd out the businesses this page exists to surface. Yahoo
        # labels the instrument itself, so this holds whatever the CSV says.
        # Absent metadata defaults to EQUITY: a missing field must not silently
        # empty the scan.
        if (meta.get("instrumentType") or "EQUITY").upper() != "EQUITY":
            return None

        volume = series.volumes[index]
        close = series.closes[index]
        prev_close = series.closes[index - 1]
        if close <= 0 or prev_close <= 0 or volume <= 0:
            return None

        # Baseline is the `lookback` sessions strictly before the target, so a
        # spike is never compared against a window that contains itself.
        window = series.volumes[index - lookback:index]
        avg_volume = sum(window) / len(window)
        if avg_volume <= 0:
            return None

        rvol = volume / avg_volume
        change_pct = (close - prev_close) / prev_close * 100.0
        if not self.criteria.matches(rvol, change_pct):
            return None

        turnover = close * volume
        if turnover < market.min_turnover:
            # A 5x day on an illiquid microcap is a rounding error, not interest.
            return None

        return Hit(
            ticker=entry.symbol,
            name=meta.get("longName") or entry.name or entry.symbol,
            rvol=rvol,
            change_pct=change_pct,
            price=close,
            volume=volume,
            avg_volume=avg_volume,
            turnover=turnover,
            currency=meta.get("currency") or market.currency,
            session_date=session_date,
            # From the export, so the note has a cap even if enrichment fails.
            market_cap=entry.market_cap,
        )
