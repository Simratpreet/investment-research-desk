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
    """Rows from a `Ticker, Company[, Market Cap][, Industry]` export.

    One parser serves every market but TSX Venture, because the exports agree on
    those column names and simply carry different subsets of them — TSX adds an
    unread `Country`, Stockholm has neither cap nor industry, the European files
    add price and volume columns nobody here wants. Missing columns come back
    None and enrichment fills what it can.

    A row without a ticker is skipped rather than raising: one malformed line
    must not cost the universe.
    """
    return _entries(csv.DictReader(io.StringIO(raw.decode("utf-8-sig",
                                                          errors="replace"))),
                    symbol_key="Ticker", name_key="Company")


def parse_tsxv_export(raw: bytes) -> list[UniverseEntry]:
    """TSX Venture, whose export disagrees with every other one.

    Its columns are `Symbol, Company Name, ...` and the symbol is exchange-
    prefixed (`TSXV:TOI`) rather than a Yahoo ticker, so both need rewriting.
    """
    return _entries(csv.DictReader(io.StringIO(raw.decode("utf-8-sig",
                                                          errors="replace"))),
                    symbol_key="Symbol", name_key="Company Name",
                    symbol_fn=_tsxv_symbol)


def _tsxv_symbol(raw_symbol: str) -> str:
    """`TSXV:TOI` -> `TOI.V`, `TSXV:OTS.H` -> `OTS-H.V`.

    228 of the 1,539 names carry a suffix — NEX (`.H`), capital pool companies
    (`.P`), share classes and units. Yahoo writes all of them with a hyphen,
    exactly as it does NYSE share classes: measured against the live endpoint,
    `OTS-H.V` and `NET-UN.V` return data and `OTS.H.V` and `NET.UN.V` return
    nothing. So there is one right answer, not a list of candidates to try.
    """
    core = raw_symbol.split(":")[-1].strip()
    return f"{core.replace('.', '-')}.V" if core else ""


def _entries(rows, symbol_key: str, name_key: str,
             symbol_fn=None) -> list[UniverseEntry]:
    entries, seen = [], set()
    for row in rows:
        raw_symbol = (row.get(symbol_key) or "").strip()
        symbol = symbol_fn(raw_symbol) if symbol_fn else raw_symbol
        if not symbol or symbol in seen:
            continue          # duplicate listings appear in some exports
        seen.add(symbol)
        sector = (row.get("Industry") or "").strip()
        entries.append(UniverseEntry(
            symbol=symbol,
            name=(row.get(name_key) or "").strip() or symbol,
            market_cap=parse_market_cap(row.get("Market Cap") or ""),
            # Four of the exports carry an industry; taking it here means those
            # markets never need a per-hit lookup to fill the table's sector.
            sector=sector or None,
        ))
    return entries


# `min_turnover` is a liquidity floor in the market's own currency, applied to
# the spike session's turnover: roughly "real money changed hands", so a 5x day
# on a few thousand dollars of stock is ignored. The floors are not a single
# figure converted eight ways — each is set to what counts as a real day on that
# venue. The US pair sits highest; TSX Venture sits lowest because its names are
# genuinely tiny and a US-sized floor would empty the market entirely; Stockholm
# looks large only because a krona is worth about a tenth of a dollar.
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
    "tsxv": Market("tsxv", "TSX Venture", "tsxv_stocks.csv",
                   parse_tsxv_export, "CAD", 100_000,
                   exported_on="2026-06-27"),
    "asx": Market("asx", "Australia (ASX)", "asx_stocks.csv",
                  parse_screener_export, "AUD", 500_000,
                  exported_on="2026-06-23"),
    "etr": Market("etr", "Frankfurt (XETRA)", "etr_stocks.csv",
                  parse_screener_export, "EUR", 500_000,
                  exported_on="2026-06-21"),
    "sw": Market("sw", "SIX Swiss", "sw_stocks.csv",
                 parse_screener_export, "CHF", 500_000,
                 exported_on="2026-06-23"),
    "sto": Market("sto", "Stockholm (Nasdaq)", "sto_stocks.csv",
                  parse_screener_export, "SEK", 5_000_000,
                  exported_on="2026-06-23"),
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
