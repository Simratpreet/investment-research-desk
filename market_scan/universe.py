"""Exchange universes, read from committed CSV exports.

Each market is one screener export checked into `market_scan/universes/`:
columns `Ticker, Company, Market Cap` (TSX adds an unused `Country`). Tickers
are already in Yahoo form — `AAB.TO`, `NVDA`, `BRK-B` — so nothing here rewrites
a symbol, and there is no candidate-fallback guesswork to get wrong.

Reading from a file rather than an exchange directory API buys three things that
matter more than freshness:

  - **Companies only.** These exports carry no ETFs, no closed-end funds, no
    bond trusts. The page is for businesses people are buying into, and a
    leveraged crypto ETF posting 5x volume on a +5% day is exactly the row that
    would crowd them out. Verified: zero fund-named rows across all three files.
  - **Market cap for free.** The exports carry it, so a hit arrives with a cap
    already attached and enrichment only has to find a sector.
  - **No network at scan time.** A scan can't fail because a directory endpoint
    is down, and the symbol list is identical run to run.

The cost is that the files go stale as listings change. `load` reports that:
anything older than `max_age_days` still scans, but comes back flagged so the
page can say the list needs refreshing rather than quietly missing new names.

To refresh, drop a new export over the committed copy — or, without a redeploy,
into `DATA_DIR/market_scan/universes/`, which takes precedence.
"""

import csv
import io
import os
import threading
import time
from datetime import datetime, timezone

from .domain import Market, UniverseEntry

# The image-baked copies, next to this module.
BAKED_DIR = os.path.join(os.path.dirname(__file__), "universes")

# Beyond this the list is old enough to be missing recent listings. It still
# scans — a slightly short universe beats no scan — but the page says so.
DEFAULT_MAX_AGE_DAYS = 120.0

_CAP_MULTIPLIERS = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


class UniverseUnavailable(Exception):
    """No usable symbol list for a market.

    Raised rather than returning [], so a missing or unreadable export is a loud
    failure instead of a scan that quietly reports "no movers today".
    """


def parse_market_cap(value: str) -> float | None:
    """'5.08B' -> 5_080_000_000.0. None when absent or unparseable.

    Absolute units, deliberately: that is what yfinance returns during
    enrichment and what the page's formatter expects, so a cap from the CSV and
    a cap from Yahoo are the same kind of number.
    """
    s = (value or "").replace(",", "").replace("$", "").strip().upper()
    if not s:
        return None
    multiplier = 1.0
    if s[-1] in _CAP_MULTIPLIERS:
        multiplier, s = _CAP_MULTIPLIERS[s[-1]], s[:-1]
    try:
        cap = float(s) * multiplier
    except ValueError:
        return None
    # Rounded because binary floats make 4.02M come out as 4019999.9999999995,
    # and a market cap has no meaningful sub-currency-unit precision anyway.
    return round(cap, 2) if cap > 0 else None


def parse_screener_export(raw: bytes) -> list[UniverseEntry]:
    """Rows from a `Ticker, Company, Market Cap` export.

    One parser serves all three markets because all three exports share those
    columns; TSX's extra `Country` is simply not read. A row without a ticker is
    skipped rather than raising — one malformed line must not cost the universe.
    """
    text = raw.decode("utf-8-sig", errors="replace")
    entries, seen = [], set()
    for row in csv.DictReader(io.StringIO(text)):
        ticker = (row.get("Ticker") or "").strip()
        if not ticker or ticker in seen:
            continue          # duplicate listings appear in some exports
        seen.add(ticker)
        entries.append(UniverseEntry(
            symbol=ticker,
            name=(row.get("Company") or "").strip() or ticker,
            market_cap=parse_market_cap(row.get("Market Cap") or ""),
        ))
    return entries


# `min_turnover` is a liquidity floor in the market's own currency, applied to
# the spike session's turnover: roughly "real money changed hands", so a 5x day
# on a few thousand dollars of stock is ignored. TSX sits lower than the US
# venues because it is a smaller market where genuine names trade thinner.
MARKETS: dict[str, Market] = {
    "nasdaq": Market("nasdaq", "NASDAQ", "nasdaq_stocks.csv",
                     parse_screener_export, "USD", 1_000_000,
                     exported_on="2026-06-22"),
    "nyse": Market("nyse", "NYSE", "nyse_stocks.csv",
                   parse_screener_export, "USD", 1_000_000,
                   exported_on="2026-06-22"),
    "tsx": Market("tsx", "Toronto Stock Exchange", "tsx_stocks.csv",
                  parse_screener_export, "CAD", 500_000,
                  exported_on="2026-06-21"),
}


class UniverseRepository:
    """Loads a market's symbol list from disk, newest override winning.

    `load` returns (entries, stale). `stale` means the list is older than
    `max_age_days` — the scan is still worth running, but it predates whatever
    has listed since, and the page should say so.

    Age comes from the market's recorded `exported_on` for a committed export,
    and from the file's mtime for a DATA_DIR override. Mtime alone would be
    useless for the committed copy: a git checkout and a Docker build both stamp
    it with the build time, so every deploy would reset the clock and the
    warning would never fire. An override is different — someone put that file
    there deliberately, and when they did is exactly what mtime records.
    """

    def __init__(self, override_dir: str | None = None, baked_dir: str = BAKED_DIR,
                 max_age_days: float = DEFAULT_MAX_AGE_DAYS):
        self._override_dir = override_dir
        self._baked_dir = baked_dir
        self._max_age = max_age_days * 86400
        self._lock = threading.RLock()
        # path -> (mtime, entries). Re-parsing 2,000 rows per scan is pointless,
        # and keying on mtime means a dropped-in refresh is picked up anyway.
        self._cache: dict[str, tuple[float, list[UniverseEntry]]] = {}

    def path_for(self, market_key: str) -> str | None:
        """The file that would be read, override first. None if neither exists."""
        market = MARKETS.get(market_key)
        if market is None:
            return None
        candidates = []
        if self._override_dir:
            candidates.append(os.path.join(self._override_dir, market.csv_file))
        baked = os.path.join(self._baked_dir, market.csv_file)
        if baked not in candidates:
            # Locally DATA_DIR is the repo, so the two resolve to one file.
            candidates.append(baked)
        return next((p for p in candidates if os.path.isfile(p)), None)

    def load(self, market_key: str) -> tuple[list[UniverseEntry], bool]:
        market = MARKETS.get(market_key)
        if market is None:
            raise UniverseUnavailable(f"unknown market: {market_key}")
        path = self.path_for(market_key)
        if path is None:
            raise UniverseUnavailable(
                f"no symbol list for {market.label}: expected "
                f"{market.csv_file} in the universes directory")
        with self._lock:
            entries, mtime = self._read(path, market)
        if not entries:
            raise UniverseUnavailable(
                f"the {market.label} symbol list at {path} has no usable rows")
        return entries, self._age_seconds(market, path, mtime) > self._max_age

    def _age_seconds(self, market: Market, path: str, mtime: float) -> float:
        """How old the list is: mtime for an override, `exported_on` otherwise."""
        is_baked = os.path.dirname(os.path.abspath(path)) == \
            os.path.abspath(self._baked_dir)
        if is_baked and market.exported_on:
            try:
                taken = datetime.strptime(market.exported_on, "%Y-%m-%d")
            except ValueError:
                return 0.0     # a malformed date must not fake a stale universe
            return max(0.0, time.time() - taken.replace(tzinfo=timezone.utc).timestamp())
        return max(0.0, time.time() - mtime)

    # --- internals ----------------------------------------------------------

    def _read(self, path: str, market: Market) -> tuple[list[UniverseEntry], float]:
        try:
            mtime = os.path.getmtime(path)
        except OSError as e:
            raise UniverseUnavailable(f"cannot stat {path}: {e}") from e
        cached = self._cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1], mtime
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            raise UniverseUnavailable(f"cannot read {path}: {e}") from e
        try:
            entries = market.parser(raw)
        except Exception as e:
            raise UniverseUnavailable(f"cannot parse {path}: {e}") from e
        self._cache[path] = (mtime, entries)
        return entries, mtime
