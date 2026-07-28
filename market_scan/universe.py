"""Exchange universes, fetched from public symbol directories.

There are no CSVs in the repo. Every universe is pulled from its exchange's own
directory and cached as JSON on the volume, so a redeploy doesn't lose it and a
weekly refresh picks up new listings without anyone touching the image.

Two upstream files cover all four markets:
  - NSE's EQUITY_L.csv          -> india
  - NASDAQ Trader's SymDir files -> nasdaq (nasdaqlisted) and nyse/amex (otherlisted)

Adding a fifth market is one `Market` entry plus one parser.
"""

import csv
import io
import json
import os
import threading
import time

import requests

from .domain import Market, UniverseEntry

# Terse on purpose — see the note in feed.py. NSE rejects a bare python-requests
# UA, and Yahoo 429s the full Chrome string, so this is what satisfies both.
UA = "Mozilla/5.0"

NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"


class UniverseUnavailable(Exception):
    """No usable symbol list — neither a live fetch nor a cached copy.

    Raised rather than returning [], so a broken universe is a loud failure
    instead of a scan that quietly reports "no movers today".
    """


def _rows(raw: bytes, delimiter: str) -> list[dict]:
    """Parse delimited text into dicts with whitespace-stripped keys and values.

    NSE's header is literally `SYMBOL,NAME OF COMPANY, SERIES,...` — the leading
    spaces are part of the field names, so stripping is not cosmetic.
    """
    text = raw.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        return []
    out = []
    for row in reader:
        if len(row) < len(header):
            continue  # short/ragged line, including the SymDir footer
        out.append({h: (v or "").strip() for h, v in zip(header, row)})
    return out


def parse_nse(raw: bytes) -> list[UniverseEntry]:
    """NSE cash-market equities. Only the EQ series — SM (SME), BE (trade-to-
    trade) and the rest are different instruments with their own liquidity."""
    entries = []
    for r in _rows(raw, ","):
        sym = r.get("SYMBOL", "")
        if not sym or r.get("SERIES") != "EQ":
            continue
        entries.append(UniverseEntry(f"{sym}.NS", r.get("NAME OF COMPANY", sym)))
    return entries


def _is_common_stock(r: dict) -> bool:
    """Drop ETFs and test issues — neither is a company anyone researches."""
    return r.get("ETF") != "Y" and r.get("Test Issue") != "Y"


def parse_nasdaq(raw: bytes) -> list[UniverseEntry]:
    entries = []
    for r in _rows(raw, "|"):
        sym = r.get("Symbol", "")
        # SymDir files end with a `File Creation Time: ...` line in column 1.
        if not sym or sym.startswith("File Creation Time") or not _is_common_stock(r):
            continue
        entries.append(UniverseEntry(sym, r.get("Security Name", sym)))
    return entries


def _parse_otherlisted(raw: bytes, exchange: str) -> list[UniverseEntry]:
    """otherlisted.txt covers every non-NASDAQ venue; `Exchange` picks one.
    N = NYSE, A = NYSE American, P = NYSE Arca, Z = Cboe BZX, V = IEX."""
    entries = []
    for r in _rows(raw, "|"):
        sym = r.get("ACT Symbol", "")
        if (not sym or sym.startswith("File Creation Time")
                or r.get("Exchange") != exchange or not _is_common_stock(r)):
            continue
        # Yahoo writes share classes with a hyphen: BRK.A -> BRK-A.
        entries.append(UniverseEntry(sym.replace(".", "-"),
                                     r.get("Security Name", sym)))
    return entries


def parse_nyse(raw: bytes) -> list[UniverseEntry]:
    return _parse_otherlisted(raw, "N")


def parse_amex(raw: bytes) -> list[UniverseEntry]:
    return _parse_otherlisted(raw, "A")


# Turnover floors are in each market's own currency: roughly "a real day's
# trading", set so a spike backed by a few thousand rupees/dollars is ignored.
MARKETS: dict[str, Market] = {
    "india": Market("india", "India (NSE)", NSE_URL, parse_nse, "INR", 10_000_000),
    "nasdaq": Market("nasdaq", "NASDAQ", NASDAQ_LISTED_URL, parse_nasdaq, "USD", 1_000_000),
    "nyse": Market("nyse", "NYSE", OTHER_LISTED_URL, parse_nyse, "USD", 1_000_000),
    "amex": Market("amex", "NYSE American", OTHER_LISTED_URL, parse_amex, "USD", 250_000),
}


class UniverseRepository:
    """Loads a market's symbol list, preferring a fresh cache to the network.

    `load` returns (entries, stale). `stale` is True when the upstream fetch
    failed and we fell back to an expired cache — a week-old symbol list is a
    fine basis for a scan, a failed scan is not, but the page should say so.
    """

    def __init__(self, cache_dir: str, ttl_days: float = 7.0, timeout: float = 60.0):
        self._dir = cache_dir
        self._ttl = ttl_days * 86400
        self._timeout = timeout
        self._lock = threading.RLock()
        self._mem: dict[str, tuple[float, list[UniverseEntry]]] = {}

    def _path(self, key: str) -> str:
        return os.path.join(self._dir, f"{key}.json")

    def load(self, market_key: str) -> tuple[list[UniverseEntry], bool]:
        market = MARKETS.get(market_key)
        if market is None:
            raise UniverseUnavailable(f"unknown market: {market_key}")
        with self._lock:
            cached, age = self._read(market_key)
            if cached and age is not None and age < self._ttl:
                return cached, False
            try:
                entries = self._fetch(market)
            except Exception as e:
                if cached:
                    return cached, True
                raise UniverseUnavailable(
                    f"could not fetch the {market.label} symbol list and no "
                    f"cached copy exists: {e}") from e
            if not entries:
                if cached:
                    return cached, True
                raise UniverseUnavailable(
                    f"the {market.label} symbol list came back empty")
            self._write(market_key, entries)
            return entries, False

    # --- internals ----------------------------------------------------------

    def _fetch(self, market: Market) -> list[UniverseEntry]:
        r = requests.get(market.source, headers={"User-Agent": UA},
                         timeout=self._timeout)
        r.raise_for_status()
        return market.parser(r.content)

    def _read(self, key: str) -> tuple[list[UniverseEntry], float | None]:
        """Cached entries plus their age in seconds, or ([], None) if absent."""
        path = self._path(key)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return [], None
        age = max(0.0, time.time() - mtime)
        memo = self._mem.get(key)
        if memo and memo[0] == mtime:
            return memo[1], age
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = [UniverseEntry(e["symbol"], e.get("name") or e["symbol"])
                       for e in data.get("entries", []) if e.get("symbol")]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return [], None
        if not entries:
            return [], None
        self._mem[key] = (mtime, entries)
        return entries, age

    def _write(self, key: str, entries: list[UniverseEntry]):
        os.makedirs(self._dir, exist_ok=True)
        path = self._path(key)
        tmp = f"{path}.tmp"
        payload = {"market": key, "fetched_at": time.time(),
                   "entries": [{"symbol": e.symbol, "name": e.name} for e in entries]}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
        try:
            self._mem[key] = (os.path.getmtime(path), entries)
        except OSError:
            self._mem.pop(key, None)
