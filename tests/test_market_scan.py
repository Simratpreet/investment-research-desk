"""Unit tests for the Movers scanner. No network, no clock, no API keys.

Run with:  python3 -m unittest discover tests

Stdlib unittest on purpose: requirements.txt is pinned for reproducible
container builds, and a test-only dependency would either bloat the image or
drift from what the image actually installs.
"""

import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from market_scan.detector import SpikeDetector
from market_scan.domain import (Hit, HitAnalysis, Market, PriceSeries,
                                ScanCriteria, ScanResult, UniverseEntry)
from market_scan.feed import FeedError, _to_series
from market_scan.scanner import MarketScanner
from market_scan.session import SessionSelector
from market_scan.store import ScanStore, hits_from_stored
from market_scan.universe import (BAKED_DIR, MARKETS, UniverseRepository,
                                  UniverseUnavailable, parse_market_cap,
                                  parse_screener_export, parse_tsxv_export)

DAY = 86400
NASDAQ = MARKETS["nasdaq"]

# A market with no liquidity floor, so the detector tests assert the spike rule
# and nothing else. The floor gets its own dedicated test against NASDAQ.
NO_FLOOR = Market("test", "Test Market", "test.csv", lambda raw: [], "USD", 0.0)


def series(volumes, closes=None, meta=None, start=1_700_000_000):
    """A PriceSeries with one bar per day, oldest first."""
    n = len(volumes)
    closes = closes if closes is not None else [100.0] * n
    return PriceSeries("TEST", tuple(start + i * DAY for i in range(n)),
                       tuple(float(c) for c in closes),
                       tuple(float(v) for v in volumes), meta or {})


def flat(n=25, volume=1000.0, close=100.0):
    return [volume] * n, [close] * n


# --- the filter rule --------------------------------------------------------

class TestScanCriteria(unittest.TestCase):
    def setUp(self):
        self.c = ScanCriteria(min_rvol=5.0, min_change_pct=5.0)

    def test_both_conditions_required(self):
        self.assertTrue(self.c.matches(5.0, 5.0))
        self.assertFalse(self.c.matches(5.0, 4.9))   # volume but no move
        self.assertFalse(self.c.matches(4.9, 5.0))   # move but no volume

    def test_thresholds_are_inclusive(self):
        self.assertTrue(self.c.matches(5.0, 5.0))
        self.assertFalse(self.c.matches(4.999, 5.0))
        self.assertFalse(self.c.matches(5.0, 4.999))

    def test_falls_are_rejected(self):
        # Direction is up-only: a 5x volume day on an -8% move is distribution,
        # a different question with a different answer.
        self.assertFalse(self.c.matches(20.0, -8.0))
        self.assertFalse(self.c.matches(20.0, 0.0))


# --- the detector -----------------------------------------------------------

class TestSpikeDetector(unittest.TestCase):
    def setUp(self):
        self.det = SpikeDetector(ScanCriteria(lookback=20))
        self.entry = UniverseEntry("TEST", "Test Ltd")

    def evaluate(self, vols, closes, index=None, market=NO_FLOOR):
        s = series(vols, closes)
        idx = len(vols) - 1 if index is None else index
        return self.det.evaluate(self.entry, s, idx, market, "2026-07-24")

    def test_detects_a_clean_spike(self):
        vols, closes = flat(21, volume=1000.0)
        vols[-1] = 10_000.0            # 10x
        closes[-1] = 110.0             # +10%
        hit = self.evaluate(vols, closes)
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit.rvol, 10.0)
        self.assertAlmostEqual(hit.change_pct, 10.0)
        self.assertEqual(hit.name, "Test Ltd")

    def test_baseline_excludes_the_target_bar(self):
        # If the spike bar leaked into its own baseline, a 21x day would read
        # as 21/(20*1+21)/21 ~= 10x. Assert the full 21x comes through.
        vols, closes = flat(21, volume=1000.0)
        vols[-1] = 21_000.0
        closes[-1] = 110.0
        hit = self.evaluate(vols, closes)
        self.assertAlmostEqual(hit.rvol, 21.0)

    def test_volume_without_a_price_rise_is_not_a_hit(self):
        vols, closes = flat(21)
        vols[-1] = 50_000.0
        closes[-1] = 100.5             # +0.5%
        self.assertIsNone(self.evaluate(vols, closes))

    def test_price_rise_without_volume_is_not_a_hit(self):
        vols, closes = flat(21)
        closes[-1] = 130.0
        self.assertIsNone(self.evaluate(vols, closes))

    def test_short_history_is_rejected(self):
        # A recent listing has no meaningful "average volume".
        vols, closes = flat(10, volume=1000.0)
        vols[-1] = 90_000.0
        closes[-1] = 150.0
        self.assertIsNone(self.evaluate(vols, closes))

    def test_zero_volume_baseline_is_rejected(self):
        vols = [0.0] * 20 + [5000.0]
        closes = [100.0] * 20 + [120.0]
        self.assertIsNone(self.evaluate(vols, closes))

    def test_turnover_floor_drops_illiquid_names(self):
        # 100x volume and +100%, but the whole day traded $200. Clears the spike
        # rule and is still not a mover — NASDAQ's floor is $1m.
        vols = [1.0] * 20 + [100.0]
        closes = [1.0] * 20 + [2.0]
        self.assertIsNotNone(self.evaluate(vols, closes, market=NO_FLOOR))
        self.assertIsNone(self.evaluate(vols, closes, market=NASDAQ))

    def test_evaluates_a_mid_series_bar(self):
        # The target is not always the last bar: mid-session runs look back one.
        vols, closes = flat(23, volume=1000.0)
        vols[21] = 10_000.0
        closes[21] = 110.0
        self.assertIsNotNone(self.evaluate(vols, closes, index=21))
        self.assertIsNone(self.evaluate(vols, closes, index=22))

    def spike(self):
        vols, closes = flat(21, volume=1000.0)
        vols[-1], closes[-1] = 10_000.0, 110.0
        return vols, closes

    def test_funds_are_rejected_however_they_got_into_the_universe(self):
        # A leveraged or crypto ETF genuinely posts 5x volume on a +5% day, and
        # would crowd out the businesses this page exists to surface. Yahoo
        # labels the instrument, so this holds even for a hand-dropped CSV.
        vols, closes = self.spike()
        for kind in ("ETF", "MUTUALFUND", "INDEX", "CRYPTOCURRENCY"):
            s = series(vols, closes, meta={"instrumentType": kind})
            self.assertIsNone(
                self.det.evaluate(self.entry, s, len(vols) - 1, NO_FLOOR,
                                  "2026-07-24"), kind)

    def test_equities_and_unlabelled_instruments_are_kept(self):
        # Absent metadata must not silently empty a scan, so it defaults to
        # equity rather than to rejection.
        vols, closes = self.spike()
        for meta in ({"instrumentType": "EQUITY"}, {"instrumentType": "equity"},
                     {"instrumentType": None}, {}):
            s = series(vols, closes, meta=meta)
            self.assertIsNotNone(
                self.det.evaluate(self.entry, s, len(vols) - 1, NO_FLOOR,
                                  "2026-07-24"), meta)

    def test_market_cap_comes_from_the_export(self):
        vols, closes = self.spike()
        entry = UniverseEntry("TEST", "Test Ltd", market_cap=4.02e6)
        hit = self.det.evaluate(entry, series(vols, closes), len(vols) - 1,
                                NO_FLOOR, "2026-07-24")
        self.assertEqual(hit.market_cap, 4.02e6)

    def test_prefers_the_feeds_company_name(self):
        vols, closes = flat(21, volume=1000.0)
        vols[-1], closes[-1] = 10_000.0, 110.0
        s = series(vols, closes, meta={"longName": "Test Industries Limited",
                                       "currency": "USD"})
        hit = self.det.evaluate(self.entry, s, len(vols) - 1, NO_FLOOR, "2026-07-24")
        self.assertEqual(hit.name, "Test Industries Limited")
        self.assertEqual(hit.currency, "USD")


# --- session selection ------------------------------------------------------

class TestSessionSelector(unittest.TestCase):
    """The rule that decides whether we are looking at today or yesterday.

    Regression cover for a real bug: the first version compared
    `meta.regularMarketTime` against the session end, but that field is the last
    *trade* time. A thin stock whose final trade lands seconds before the bell
    then reads as "still trading" for the rest of the day.
    """

    def setUp(self):
        self.sel = SessionSelector()
        self.open_ts = 1_800_000_000          # session open
        self.close_ts = self.open_ts + 6 * 3600

    def build(self, last_trade_offset=0):
        s = series([1000.0] * 5, start=self.open_ts - 4 * DAY)
        return PriceSeries(
            s.symbol,
            s.timestamps[:-1] + (self.open_ts,),
            s.closes, s.volumes,
            {"currentTradingPeriod": {"regular": {"start": self.open_ts,
                                                  "end": self.close_ts}},
             "regularMarketTime": self.close_ts + last_trade_offset,
             "gmtoffset": 0},
        )

    def test_mid_session_bar_is_dropped(self):
        s = self.build()
        now = self.open_ts + 3600            # one hour into the session
        self.assertTrue(self.sel.last_bar_in_progress(s, now=now))
        self.assertEqual(self.sel.target_index(s, now=now), len(s) - 2)

    def test_closed_session_bar_is_kept(self):
        s = self.build()
        now = self.close_ts + 3600
        self.assertFalse(self.sel.last_bar_in_progress(s, now=now))
        self.assertEqual(self.sel.target_index(s, now=now), len(s) - 1)

    def test_illiquid_name_whose_last_trade_preceded_the_bell(self):
        # The bug. Last trade 8 seconds before the close, checked hours later:
        # the session is over and the bar must count.
        s = self.build(last_trade_offset=-8)
        now = self.close_ts + 6 * 3600
        self.assertFalse(self.sel.last_bar_in_progress(s, now=now))

    def test_overnight_rolled_forward_period_keeps_the_last_bar(self):
        # After the close Yahoo advances currentTradingPeriod to the next
        # session, so the last bar predates its start.
        s = series([1000.0] * 5, start=self.open_ts - 4 * DAY)
        tomorrow = self.open_ts + DAY
        s = PriceSeries(s.symbol, s.timestamps, s.closes, s.volumes,
                        {"currentTradingPeriod": {"regular": {
                            "start": tomorrow, "end": tomorrow + 6 * 3600}},
                         "gmtoffset": 0})
        now = tomorrow - 3600
        self.assertFalse(self.sel.last_bar_in_progress(s, now=now))
        self.assertEqual(self.sel.target_index(s, now=now), len(s) - 1)

    def test_missing_metadata_keeps_the_bar(self):
        # Better to keep a real session than silently discard one; the scanner's
        # modal-date check catches anything that lands on the wrong day.
        s = series([1000.0] * 5)
        self.assertFalse(self.sel.last_bar_in_progress(s))

    def test_session_date_uses_the_exchange_offset(self):
        # 04:00 UTC in Mumbai (+5:30) is the morning of the same day; the naive
        # UTC date would be right here but wrong for a US afternoon bar.
        s = PriceSeries("X", (1_753_000_000,), (1.0,), (1.0,), {"gmtoffset": 19800})
        self.assertEqual(len(self.sel.session_date(s, 0)), 10)

    def test_empty_series_has_no_target(self):
        self.assertIsNone(self.sel.target_index(series([])))


# --- universe parsing -------------------------------------------------------

US_CSV = (b"Ticker,Company,Market Cap\n"
          b"NVDA,NVIDIA Corporation,5.23T\n"
          b"BRK-B,Berkshire Hathaway Inc.,1.03B\n"
          b"TINY,Tiny Holdings Inc.,900K\n"
          b"NOCAP,No Cap Corp,\n"
          b",Blank Ticker Inc.,1.00M\n"
          b"NVDA,NVIDIA Corporation,5.23T\n")

TSX_CSV = (b"Ticker,Company,Market Cap,Country\n"
           b"AAB.TO,Aberdeen International Inc.,4.02M,Canada\n"
           b"AGF-B.TO,AGF Management Limited,1.5B,Canada\n")

# Stockholm's export is `Ticker,Company` and nothing else, so it is the one
# market where both cap and sector have to come from enrichment.
CAPLESS_MARKETS = {"sto"}


class TestParsers(unittest.TestCase):
    def test_reads_ticker_company_and_cap(self):
        entries = parse_screener_export(US_CSV)
        self.assertEqual(entries[0].symbol, "NVDA")
        self.assertEqual(entries[0].name, "NVIDIA Corporation")
        self.assertEqual(entries[0].market_cap, 5.23e12)

    def test_cap_suffixes_become_absolute_units(self):
        # Absolute, so a CSV cap and a yfinance cap are the same kind of number.
        self.assertEqual(parse_market_cap("1.03B"), 1.03e9)
        self.assertEqual(parse_market_cap("900K"), 900_000)
        self.assertEqual(parse_market_cap("4.02M"), 4_020_000)
        self.assertEqual(parse_market_cap("$1,234"), 1234.0)

    def test_unparseable_or_absent_cap_is_none_not_zero(self):
        # None means "unknown" and lets enrichment fill it; 0.0 would look like
        # a real answer and block the lookup.
        for bad in ("", "   ", "n/a", "-"):
            self.assertIsNone(parse_market_cap(bad), bad)
        self.assertIsNone([e for e in parse_screener_export(US_CSV)
                           if e.symbol == "NOCAP"][0].market_cap)

    def test_rows_without_a_ticker_are_skipped(self):
        self.assertNotIn("", [e.symbol for e in parse_screener_export(US_CSV)])

    def test_duplicate_listings_are_collapsed(self):
        symbols = [e.symbol for e in parse_screener_export(US_CSV)]
        self.assertEqual(symbols.count("NVDA"), 1)

    def test_tsx_extra_country_column_is_ignored_and_tickers_kept_verbatim(self):
        entries = parse_screener_export(TSX_CSV)
        # Already Yahoo-form: nothing here rewrites a symbol.
        self.assertEqual([e.symbol for e in entries], ["AAB.TO", "AGF-B.TO"])

    def baked(self, market):
        with open(os.path.join(BAKED_DIR, market.csv_file), "rb") as f:
            return market.parser(f.read())

    def test_every_registered_market_has_a_committed_export(self):
        for key, market in MARKETS.items():
            self.assertTrue(callable(market.parser), key)
            path = os.path.join(BAKED_DIR, market.csv_file)
            self.assertTrue(os.path.isfile(path), f"{key}: missing {path}")
            self.assertGreater(len(self.baked(market)), 100, key)

    def test_committed_exports_are_companies_not_funds(self):
        # The whole reason for using exports rather than an exchange directory:
        # a leveraged or crypto ETF really does post 5x volume on a +5% day.
        for key, market in MARKETS.items():
            names = [e.name for e in self.baked(market)]
            funds = [n for n in names if re.search(r"\b(ETF|Index Fund)\b", n, re.I)]
            self.assertEqual(funds, [], f"{key} export contains funds")

    def test_committed_exports_carry_market_caps(self):
        # A cap from the CSV is what lets enrichment skip the lookup entirely.
        for key, market in MARKETS.items():
            entries = self.baked(market)
            with_cap = [e for e in entries if e.market_cap]
            if key in CAPLESS_MARKETS:
                self.assertEqual(with_cap, [], f"{key} gained a cap column")
                continue
            self.assertGreater(len(with_cap), len(entries) * 0.9, key)

    def test_exports_with_an_industry_column_populate_sector(self):
        # Four venues carry it, which means no per-hit lookup for those markets.
        for key in ("tsxv", "asx", "etr", "sw"):
            entries = self.baked(MARKETS[key])
            with_sector = [e for e in entries if e.sector]
            self.assertGreater(len(with_sector), len(entries) * 0.9, key)

    def test_tsxv_symbols_become_yahoo_tickers(self):
        rows = (b"Symbol,Company Name,Exchange,Industry,Market Cap\n"
                b"TSXV:TOI,Topicus.com Inc.,TSX Venture Exchange,Software,7592171330\n"
                b"TSXV:OTS.H,Ortus Inc.,TSX Venture Exchange,Shell,1000000\n"
                b"TSXV:NET.UN,Net Units,TSX Venture Exchange,REIT,2000000\n"
                b",No Symbol Inc.,TSX Venture Exchange,,0\n")
        entries = parse_tsxv_export(rows)
        # Measured against the live endpoint: OTS-H.V returns data, OTS.H.V does
        # not. Hyphens, exactly like NYSE share classes — so one right answer.
        self.assertEqual([e.symbol for e in entries],
                         ["TOI.V", "OTS-H.V", "NET-UN.V"])
        self.assertEqual(entries[0].name, "Topicus.com Inc.")
        self.assertEqual(entries[0].sector, "Software")

    def test_the_tsxv_export_really_is_all_suffixed(self):
        entries = self.baked(MARKETS["tsxv"])
        self.assertTrue(all(e.symbol.endswith(".V") for e in entries))
        self.assertNotIn(":", "".join(e.symbol for e in entries))


class TestUniverseRepository(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write_override(self, market_key, body: bytes):
        path = os.path.join(self.dir, MARKETS[market_key].csv_file)
        with open(path, "wb") as f:
            f.write(body)
        return path

    def test_reads_the_committed_export_by_default(self):
        entries, stale = UniverseRepository(None).load("nasdaq")
        self.assertGreater(len(entries), 100)
        self.assertFalse(stale)

    def test_override_directory_wins_over_the_committed_copy(self):
        self.write_override("nasdaq", US_CSV)
        entries, _ = UniverseRepository(self.dir).load("nasdaq")
        # The real export has thousands of rows; the override has a handful.
        self.assertEqual(entries[0].symbol, "NVDA")
        self.assertLess(len(entries), 10)

    def test_missing_override_falls_through_to_the_committed_copy(self):
        entries, _ = UniverseRepository(self.dir).load("tsx")
        self.assertGreater(len(entries), 100)

    def test_an_old_override_still_scans_but_is_flagged_stale(self):
        path = self.write_override("nasdaq", US_CSV)
        old = time.time() - 400 * 86400
        os.utime(path, (old, old))
        entries, stale = UniverseRepository(self.dir).load("nasdaq")
        self.assertTrue(entries)     # a dated list beats no scan at all
        self.assertTrue(stale)       # but the page has to say so

    def test_a_fresh_override_is_not_stale(self):
        self.write_override("nasdaq", US_CSV)
        self.assertFalse(UniverseRepository(self.dir).load("nasdaq")[1])

    def test_committed_export_age_comes_from_exported_on_not_mtime(self):
        # The bug this guards: a git checkout and a Docker build both stamp
        # mtime with the build time, so an mtime-based check would call a
        # year-old export brand new on every deploy and never warn.
        market = MARKETS["nasdaq"]
        self.assertTrue(market.exported_on, "nasdaq has no recorded export date")
        os.utime(os.path.join(BAKED_DIR, market.csv_file), None)   # mtime = now
        repo = UniverseRepository(None, max_age_days=0.5)
        self.assertTrue(repo.load("nasdaq")[1])
        # And a generous window still reads it as current.
        self.assertFalse(UniverseRepository(None, max_age_days=100_000)
                         .load("nasdaq")[1])

    def test_every_market_records_when_its_export_was_taken(self):
        for key, market in MARKETS.items():
            time.strptime(market.exported_on, "%Y-%m-%d")   # raises if malformed

    def test_reparse_is_skipped_while_the_file_is_unchanged(self):
        self.write_override("nasdaq", US_CSV)
        repo = UniverseRepository(self.dir)
        first, _ = repo.load("nasdaq")
        second, _ = repo.load("nasdaq")
        self.assertIs(first, second)

    def test_a_rewritten_file_is_picked_up(self):
        self.write_override("nasdaq", US_CSV)
        repo = UniverseRepository(self.dir)
        self.assertEqual(repo.load("nasdaq")[0][0].symbol, "NVDA")
        path = self.write_override("nasdaq", b"Ticker,Company,Market Cap\nZZZ,Zed,1M\n")
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertEqual(repo.load("nasdaq")[0][0].symbol, "ZZZ")

    def test_a_file_with_no_usable_rows_raises(self):
        self.write_override("nasdaq", b"Ticker,Company,Market Cap\n")
        # Loud, so a broken universe can never look like a quiet market.
        with self.assertRaises(UniverseUnavailable):
            UniverseRepository(self.dir).load("nasdaq")

    def test_a_missing_file_everywhere_raises(self):
        repo = UniverseRepository(self.dir, baked_dir=self.dir)
        with self.assertRaises(UniverseUnavailable):
            repo.load("nasdaq")

    def test_unknown_market_raises(self):
        with self.assertRaises(UniverseUnavailable):
            UniverseRepository(self.dir).load("atlantis")


# --- the feed's payload handling -------------------------------------------

class TestFeedPayload(unittest.TestCase):
    def payload(self, stamps, closes, volumes, meta=None):
        return {"chart": {"result": [{
            "timestamp": stamps, "meta": meta or {},
            "indicators": {"quote": [{"close": closes, "volume": volumes}]}}]}}

    def test_null_bars_are_dropped(self):
        # Yahoo pads holidays and halts with nulls. Left in, a "20-day average"
        # would silently be computed over fewer real sessions.
        s = _to_series("X", self.payload([1, 2, 3, 4],
                                         [10.0, None, 12.0, 13.0],
                                         [100.0, 200.0, None, 400.0]))
        self.assertEqual(s.timestamps, (1, 4))
        self.assertEqual(s.closes, (10.0, 13.0))

    def test_meta_is_carried_through(self):
        s = _to_series("X", self.payload([1], [10.0], [100.0],
                                         meta={"longName": "X Ltd"}))
        self.assertEqual(s.meta["longName"], "X Ltd")

    def test_empty_result_is_rejected(self):
        with self.assertRaises(ValueError):
            _to_series("X", {"chart": {"result": []}})

    def test_ragged_series_is_rejected(self):
        with self.assertRaises(ValueError):
            _to_series("X", self.payload([1, 2], [10.0], [100.0, 200.0]))

    def test_all_null_series_is_rejected(self):
        with self.assertRaises(ValueError):
            _to_series("X", self.payload([1, 2], [None, None], [None, None]))


# --- the scanner's failure isolation ---------------------------------------

class StubFeed:
    """A feed whose behaviour is chosen per symbol, so the scanner's handling of
    each outcome is directly assertable."""

    def __init__(self, good=(), missing=(), broken=(), stale=(), meta=None):
        self.good, self.missing = set(good), set(missing)
        self.broken, self.stale = set(broken), set(stale)
        self.meta = meta or {}

    def fetch(self, symbol):
        if symbol in self.missing:
            return None
        if symbol in self.broken:
            raise FeedError(f"{symbol}: HTTP 429")
        start = 1_700_000_000 - (40 * DAY if symbol in self.stale else 0)
        # Sized to clear the US $1m turnover floor, so these tests exercise
        # failure isolation rather than accidentally testing the liquidity rule.
        vols = [5_000.0] * 20 + [200_000.0]
        closes = [100.0] * 20 + [120.0]
        s = series(vols, closes, meta=self.meta, start=start)
        return PriceSeries(symbol, s.timestamps, s.closes, s.volumes, s.meta)


class CountingFeed(StubFeed):
    """Records how many symbols were actually fetched."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def fetch(self, symbol):
        self.calls += 1
        return super().fetch(symbol)


class StubRepo:
    def __init__(self, symbols, stale=False):
        self.symbols = symbols
        self.stale = stale

    def load(self, market_key):
        return [UniverseEntry(s, s) for s in self.symbols], self.stale


def build(feed, symbols, **kw):
    return MarketScanner(StubRepo(symbols, kw.pop("universe_stale", False)),
                         feed, SessionSelector(),
                         SpikeDetector(ScanCriteria(lookback=20)), **kw)


class TestScanner(unittest.TestCase):
    def test_one_bad_symbol_never_ends_the_run(self):
        symbols = [f"S{i}" for i in range(20)]
        feed = StubFeed(broken=symbols[:3], missing=symbols[3:6])
        result = build(feed, symbols, max_workers=4).scan("nasdaq")
        self.assertEqual(result.stats["failed"], 3)
        self.assertEqual(result.stats["no_data"], 3)
        self.assertEqual(result.stats["scanned"], 14)
        self.assertEqual(len(result.hits), 14)   # the rest still came through

    def test_a_mostly_failed_run_is_degraded_not_empty(self):
        symbols = [f"S{i}" for i in range(20)]
        result = build(StubFeed(broken=symbols[:15]), symbols).scan("nasdaq")
        self.assertTrue(result.degraded)
        self.assertEqual(result.stats["failed"], 15)

    def test_a_healthy_quiet_run_is_not_degraded(self):
        symbols = [f"S{i}" for i in range(20)]
        result = build(StubFeed(missing=symbols), symbols).scan("nasdaq")
        self.assertFalse(result.degraded)
        self.assertEqual(len(result.hits), 0)

    def test_stale_sessions_are_excluded_from_hits(self):
        # Names whose last completed session is weeks old (halted, dormant) get
        # counted, not shown: their spike is against a stale baseline.
        symbols = [f"S{i}" for i in range(10)]
        result = build(StubFeed(stale=symbols[:3]), symbols).scan("nasdaq")
        self.assertEqual(result.stats["stale"], 3)
        self.assertEqual(len(result.hits), 7)

    def test_hits_are_sorted_by_rvol(self):
        symbols = [f"S{i}" for i in range(5)]
        result = build(StubFeed(), symbols).scan("nasdaq")
        rvols = [h.rvol for h in result.hits]
        self.assertEqual(rvols, sorted(rvols, reverse=True))

    def test_progress_is_reported_for_every_symbol(self):
        symbols = [f"S{i}" for i in range(12)]
        seen = []
        build(StubFeed(), symbols, max_workers=3).scan(
            "nasdaq", progress_cb=lambda done, total: seen.append((done, total)))
        self.assertEqual(len(seen), 12)
        self.assertEqual(max(d for d, _ in seen), 12)
        self.assertTrue(all(t == 12 for _, t in seen))

    def test_a_stop_event_short_circuits_the_rest(self):
        import threading
        symbols = [f"S{i}" for i in range(30)]
        stop = threading.Event()
        stop.set()
        result = build(StubFeed(), symbols).scan("nasdaq", stop_event=stop)
        self.assertTrue(result.stopped)
        self.assertEqual(result.stats["skipped"], 30)

    def test_limit_caps_the_universe(self):
        symbols = [f"S{i}" for i in range(50)]
        result = build(StubFeed(), symbols).scan("nasdaq", limit=10)
        self.assertEqual(result.stats["total"], 10)

    def test_universe_staleness_is_carried_onto_the_result(self):
        result = build(StubFeed(), ["S1"], universe_stale=True).scan("nasdaq")
        self.assertTrue(result.universe_stale)


def _stub_date(offset_days: int) -> str:
    """The session date StubFeed's last bar lands on. Derived, not typed in, so
    it can't drift from the fixture."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(1_700_000_000 + offset_days * DAY,
                                  tz=timezone.utc).strftime("%Y-%m-%d")


# StubFeed builds 21 bars, so the last is 20 days after the start; a "stale"
# name starts 40 days earlier and therefore lands 20 days before it.
STUB_SESSION = _stub_date(20)
STUB_STALE_SESSION = _stub_date(-20)


class TestSessionProbe(unittest.TestCase):
    """A few requests answering "has this market moved on?", so a scan that
    would land on a session already stored never makes the other 4,000."""

    def probe(self, feed, symbols, **kw):
        return build(feed, symbols).probe_session("nasdaq", **kw)

    def test_reports_the_session_the_market_is_on(self):
        self.assertEqual(self.probe(StubFeed(), [f"S{i}" for i in range(9)]),
                         STUB_SESSION)

    def test_samples_only_a_handful_of_names(self):
        feed = CountingFeed()
        build(feed, [f"S{i}" for i in range(400)]).probe_session("nasdaq")
        self.assertLessEqual(feed.calls, 5)

    def test_a_minority_of_stale_names_cannot_decide_it(self):
        symbols = [f"S{i}" for i in range(9)]
        # Two of the five sampled are weeks behind; the majority still wins.
        self.assertEqual(self.probe(StubFeed(stale=symbols[:2]), symbols),
                         STUB_SESSION)

    def test_a_stale_majority_is_reported_as_the_session(self):
        # Three of five behind: that is what the market looks like from here,
        # and the caller compares it against what is already stored.
        symbols = [f"S{i}" for i in range(9)]
        self.assertEqual(self.probe(StubFeed(stale=symbols[:3]), symbols),
                         STUB_STALE_SESSION)

    def test_no_majority_returns_none_so_the_caller_scans(self):
        symbols = [f"S{i}" for i in range(9)]
        # Of the five sampled: two stale, two unreachable, one current. Nothing
        # clears half, so the probe declines to answer.
        self.assertIsNone(self.probe(
            StubFeed(stale=symbols[:2], broken=symbols[2:4]), symbols))

    def test_a_dead_feed_returns_none_rather_than_a_guess(self):
        symbols = [f"S{i}" for i in range(9)]
        # Guessing here would silently skip a real session, so the tie goes to
        # doing the work.
        self.assertIsNone(self.probe(StubFeed(broken=symbols), symbols))
        self.assertIsNone(self.probe(StubFeed(missing=symbols), symbols))

    def test_an_empty_universe_returns_none(self):
        self.assertIsNone(self.probe(StubFeed(), []))


# --- persistence ------------------------------------------------------------

def make_result(market="nasdaq", session="2026-07-24", tickers=("AAA",)):
    hits = tuple(
        Hit(t, f"{t} Ltd", 8.0, 9.0, 100.0, 5000.0, 600.0, 500_000.0, "USD", session)
        for t in tickers)
    return ScanResult(market, session, hits, ScanCriteria(), {"total": 1})


class TestScanStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.store = ScanStore(self.dir)

    def test_round_trip(self):
        self.store.save(make_result())
        data = self.store.latest("nasdaq")
        self.assertEqual(data["session_date"], "2026-07-24")
        self.assertEqual(len(data["hits"]), 1)
        self.assertEqual(data["hits"][0]["ticker"], "AAA")

    def test_latest_picks_the_newest_session(self):
        self.store.save(make_result(session="2026-07-20"))
        self.store.save(make_result(session="2026-07-24"))
        self.assertEqual(self.store.latest("nasdaq")["session_date"], "2026-07-24")
        self.assertEqual(self.store.list_runs("nasdaq"),
                         ["2026-07-24", "2026-07-20"])

    def test_markets_are_kept_separate(self):
        self.store.save(make_result(market="nasdaq"))
        self.store.save(make_result(market="nasdaq", tickers=("BBB",)))
        self.assertEqual(self.store.latest("nasdaq")["hits"][0]["ticker"], "BBB")
        self.assertEqual(self.store.list_runs("nyse"), [])

    def test_update_hit_merges_enrichment(self):
        self.store.save(make_result())
        self.assertTrue(self.store.update_hit("nasdaq", "2026-07-24", "AAA",
                                              sector="Energy", market_cap=1.2e9))
        hit = self.store.latest("nasdaq")["hits"][0]
        self.assertEqual(hit["sector"], "Energy")
        self.assertEqual(hit["market_cap"], 1.2e9)
        # Untouched fields survive the merge.
        self.assertEqual(hit["rvol"], 8.0)

    def test_update_hit_on_a_missing_run_is_false_not_an_error(self):
        self.assertFalse(self.store.update_hit("nasdaq", "1999-01-01", "AAA",
                                               sector="X"))

    def test_rescanning_a_session_keeps_the_notes_already_paid_for(self):
        # Exchanges publish a session's bars hours after the close, so scanning
        # the same day twice is routine. Wiping notes each time re-buys every
        # one of them from the model.
        self.store.save(make_result(tickers=("AAA", "BBB")))
        self.store.save_analysis("nasdaq", "2026-07-24",
                                 HitAnalysis("AAA", "A note.", "ok"))
        self.store.save(make_result(tickers=("AAA", "BBB")))
        data = self.store.latest("nasdaq")
        self.assertEqual(data["analyses"]["AAA"]["summary"], "A note.")

    def test_a_note_for_a_name_that_dropped_out_is_not_left_orphaned(self):
        self.store.save(make_result(tickers=("AAA", "BBB")))
        self.store.save_analysis("nasdaq", "2026-07-24",
                                 HitAnalysis("BBB", "Gone next time.", "ok"))
        self.store.save(make_result(tickers=("AAA",)))
        self.assertNotIn("BBB", self.store.latest("nasdaq")["analyses"])

    def test_a_different_session_starts_with_no_notes(self):
        self.store.save(make_result(session="2026-07-24"))
        self.store.save_analysis("nasdaq", "2026-07-24",
                                 HitAnalysis("AAA", "Monday.", "ok"))
        self.store.save(make_result(session="2026-07-25"))
        self.assertEqual(self.store.latest("nasdaq")["analyses"], {})

    def test_analyses_are_written_back_one_at_a_time(self):
        self.store.save(make_result(tickers=("AAA", "BBB")))
        self.store.save_analysis("nasdaq", "2026-07-24",
                                 HitAnalysis("AAA", "A note.", "ok"))
        data = self.store.latest("nasdaq")
        # The first note is durable even though the second never arrived.
        self.assertEqual(data["analyses"]["AAA"]["summary"], "A note.")
        self.assertNotIn("BBB", data["analyses"])

    def test_writes_are_atomic(self):
        self.store.save(make_result())
        # No .tmp file survives a completed write, so a crash can only ever
        # leave the previous complete file or the new one — never a partial.
        self.assertFalse([n for n in os.listdir(self.dir) if n.endswith(".tmp")])

    def test_prune_keeps_the_newest_n_sessions(self):
        for day in range(20, 28):
            self.store.save(make_result(session=f"2026-07-{day}"))
        self.store.prune(5)
        self.assertEqual(self.store.list_runs("nasdaq"),
                         ["2026-07-27", "2026-07-26", "2026-07-25",
                          "2026-07-24", "2026-07-23"])

    def test_prune_counts_each_market_separately(self):
        for day in (24, 25, 26):
            self.store.save(make_result(market="nasdaq", session=f"2026-07-{day}"))
            self.store.save(make_result(market="nyse", session=f"2026-07-{day}"))
        self.store.prune(2)
        self.assertEqual(len(self.store.list_runs("nasdaq")), 2)
        self.assertEqual(len(self.store.list_runs("nyse")), 2)

    def test_prune_ignores_file_age(self):
        # Age is the wrong measure: a redeploy restores files from the image and
        # stamps every one of them with the build time.
        self.store.save(make_result(session="2026-07-24"))
        self.store.save(make_result(session="2026-01-01"))
        stamp = time.time() - 900 * 86400
        for date in ("2026-07-24", "2026-01-01"):
            os.utime(self.store._path("nasdaq", date), (stamp, stamp))
        self.store.prune(5)
        self.assertEqual(len(self.store.list_runs("nasdaq")), 2)

    def test_prune_always_keeps_at_least_one_run(self):
        self.store.save(make_result(session="2026-07-24"))
        self.store.prune(0)
        self.assertEqual(self.store.list_runs("nasdaq"), ["2026-07-24"])

    def test_recent_returns_sessions_newest_first(self):
        for day in (24, 25, 26):
            self.store.save(make_result(session=f"2026-07-{day}"))
        runs = self.store.recent("nasdaq", 2)
        self.assertEqual([r["session_date"] for r in runs],
                         ["2026-07-26", "2026-07-25"])

    def test_recent_on_an_unscanned_market_is_empty(self):
        self.assertEqual(self.store.recent("nasdaq", 5), [])

    def test_recent_skips_a_corrupt_run_without_losing_the_others(self):
        for day in (24, 25):
            self.store.save(make_result(session=f"2026-07-{day}"))
        with open(self.store._path("nasdaq", "2026-07-25"), "w") as f:
            f.write("{ not json")
        runs = self.store.recent("nasdaq", 5)
        self.assertEqual([r["session_date"] for r in runs], ["2026-07-24"])

    def test_latest_on_an_unknown_market_is_none(self):
        self.assertIsNone(self.store.latest("nasdaq"))

    def test_corrupt_file_is_ignored_rather_than_crashing(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "nasdaq_2026-07-24.json"), "w") as f:
            f.write("{not json")
        self.assertIsNone(self.store.latest("nasdaq"))

    def test_hits_rebuild_from_stored_json(self):
        self.store.save(make_result(tickers=("AAA", "BBB")))
        hits = hits_from_stored(self.store.latest("nasdaq"))
        self.assertEqual([h.ticker for h in hits], ["AAA", "BBB"])
        self.assertEqual(hits[0].session_date, "2026-07-24")

    def test_hits_rebuild_skips_malformed_rows(self):
        hits = hits_from_stored({"hits": [{"no_ticker": 1},
                                          {"ticker": "OK", "name": "Ok"}]})
        self.assertEqual([h.ticker for h in hits], ["OK"])


# --- the analyst ------------------------------------------------------------

class TestAnalyst(unittest.TestCase):
    def setUp(self):
        from market_scan.analyst import OpenRouterAnalyst, build_messages
        self.build_messages = build_messages
        self.Analyst = OpenRouterAnalyst
        self.hit = Hit("HOOD", "Robinhood Markets, Inc.", 6.2, 8.4, 516.45,
                       1_631_764, 263_000, 8.4e8, "USD", "2026-07-24",
                       sector="Energy")

    def test_prompt_carries_the_measured_facts(self):
        messages = self.build_messages(self.hit, "NASDAQ")
        self.assertEqual(messages[0]["role"], "system")
        user = messages[1]["content"]
        # The model must explain *this* move, not the company in general.
        for fragment in ("HOOD", "Robinhood Markets, Inc.", "NASDAQ",
                         "2026-07-24", "6.2x", "+8.4%", "Energy"):
            self.assertIn(fragment, user)

    def test_prompt_forbids_inventing_a_catalyst(self):
        system = self.build_messages(self.hit, "NASDAQ")[0]["content"]
        self.assertIn("no identifiable public catalyst", system)

    def test_prompt_asks_the_users_question(self):
        user = self.build_messages(self.hit, "NASDAQ")[1]["content"]
        self.assertIn("business model and the investment thesis", user)
        self.assertIn("margins expanding", user)

    def test_unknown_sector_and_cap_are_stated_not_faked(self):
        bare = Hit("X", "X Ltd", 6.0, 6.0, 10.0, 1.0, 1.0, 1.0, "USD", "2026-07-24")
        user = self.build_messages(bare, "NASDAQ")[1]["content"]
        self.assertIn("Sector: not known", user)
        self.assertIn("Market cap: not known", user)

    def test_a_missing_api_key_fails_the_note_not_the_run(self):
        analysis = self.Analyst("", "some/model").explain(self.hit)
        self.assertEqual(analysis.status, "failed")
        self.assertIn("OPENROUTER_API_KEY", analysis.error)
        self.assertEqual(analysis.ticker, "HOOD")

    def test_an_upstream_error_fails_the_note_not_the_run(self):
        analyst = self.Analyst("key", "some/model")
        analyst._call = lambda messages: (_ for _ in ()).throw(RuntimeError("boom"))
        analysis = analyst.explain(self.hit)
        self.assertEqual(analysis.status, "failed")
        self.assertIn("boom", analysis.error)

    def test_an_empty_response_is_a_failure_not_a_blank_note(self):
        analyst = self.Analyst("key", "some/model")
        analyst._call = lambda messages: ""
        self.assertEqual(analyst.explain(self.hit).status, "failed")

    def test_a_good_response_becomes_an_ok_note(self):
        analyst = self.Analyst("key", "some/model")
        analyst._call = lambda messages: "It won a large order."
        analysis = analyst.explain(self.hit)
        self.assertEqual(analysis.status, "ok")
        self.assertEqual(analysis.summary, "It won a large order.")
        self.assertEqual(analysis.model, "some/model")
        self.assertTrue(analysis.generated_at)


# --- enrichment -------------------------------------------------------------

@contextlib.contextmanager
def fake_yfinance(info=None, explodes=False):
    """Swap in a stub `yfinance`, or remove it entirely.

    Without this the tests reach Yahoo for real: yfinance is a pinned
    dependency, so `import yfinance` inside enrich() succeeds and the "no
    network" promise at the top of this file quietly stops being true.
    """
    previous = sys.modules.get("yfinance")
    if info is None and not explodes:
        sys.modules["yfinance"] = None       # makes `import yfinance` raise
    else:
        module = types.ModuleType("yfinance")

        class Ticker:
            def __init__(self, symbol):
                if explodes:
                    raise RuntimeError("Yahoo is down")
                self.info = dict(info or {})

        module.Ticker = Ticker
        sys.modules["yfinance"] = module
    try:
        yield
    finally:
        if previous is None:
            sys.modules.pop("yfinance", None)
        else:
            sys.modules["yfinance"] = previous


class RecordingStore:
    def __init__(self):
        self.calls = []

    def update_hit(self, market, session_date, ticker, **fields):
        self.calls.append((ticker, fields))
        return True


class ExplodingStore:
    def update_hit(self, *a, **kw):
        raise RuntimeError("store is down")


class TestEnrich(unittest.TestCase):
    def hit(self, market_cap=None):
        return Hit("X", "X", 6.0, 6.0, 1.0, 1.0, 1.0, 1.0, "USD", "2026-07-24",
                   market_cap=market_cap)

    def run_enrich(self, store, hits):
        from market_scan.enrich import enrich
        return enrich(hits, store, "nasdaq", "2026-07-24")

    def test_a_missing_yfinance_is_a_no_op_not_a_crash(self):
        with fake_yfinance():
            store = RecordingStore()
            self.assertEqual(self.run_enrich(store, [self.hit()]), 0)
            self.assertEqual(store.calls, [])

    def test_sector_is_filled(self):
        with fake_yfinance({"sector": "Energy", "marketCap": 5e8}):
            store = RecordingStore()
            self.assertEqual(self.run_enrich(store, [self.hit(market_cap=1e9)]), 1)
            self.assertEqual(store.calls[0][1]["sector"], "Energy")

    def test_a_cap_from_the_export_is_never_overwritten(self):
        # Yahoo's cap can be badly wrong for microcaps, and the export's figure
        # is at least internally consistent — so it wins.
        with fake_yfinance({"sector": "Energy", "marketCap": 1_937_872}):
            store = RecordingStore()
            self.run_enrich(store, [self.hit(market_cap=72_000_000)])
            self.assertNotIn("market_cap", store.calls[0][1])

    def test_a_missing_cap_is_filled_from_yahoo(self):
        with fake_yfinance({"marketCap": 5e8}):
            store = RecordingStore()
            self.run_enrich(store, [self.hit(market_cap=None)])
            self.assertEqual(store.calls[0][1]["market_cap"], 5e8)

    def test_nothing_useful_means_no_write(self):
        with fake_yfinance({"longName": "X Ltd"}):
            store = RecordingStore()
            self.assertEqual(self.run_enrich(store, [self.hit(market_cap=1e9)]), 0)
            self.assertEqual(store.calls, [])

    def test_a_yahoo_failure_never_raises(self):
        with fake_yfinance(explodes=True):
            self.assertEqual(self.run_enrich(RecordingStore(), [self.hit()]), 0)

    def test_a_store_failure_never_raises(self):
        # The scan is already on disk; losing a sector must not cost the run.
        with fake_yfinance({"sector": "Energy"}):
            self.assertIsInstance(self.run_enrich(ExplodingStore(), [self.hit()]),
                                  int)


if __name__ == "__main__":
    unittest.main()
