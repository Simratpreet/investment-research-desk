"""Unit tests for the Movers scanner. No network, no clock, no API keys.

Run with:  python3 -m unittest discover tests

Stdlib unittest on purpose: requirements.txt is pinned for reproducible
container builds, and a test-only dependency would either bloat the image or
drift from what the image actually installs.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from market_scan.detector import SpikeDetector
from market_scan.domain import (Hit, HitAnalysis, Market, PriceSeries,
                                ScanCriteria, ScanResult, UniverseEntry)
from market_scan.feed import FeedError, _to_series
from market_scan.scanner import MarketScanner
from market_scan.session import SessionSelector
from market_scan.store import ScanStore, hits_from_stored
from market_scan.universe import (MARKETS, UniverseRepository,
                                  UniverseUnavailable, parse_amex, parse_nasdaq,
                                  parse_nse, parse_nyse)

DAY = 86400
INDIA = MARKETS["india"]

# A market with no liquidity floor, so the detector tests assert the spike rule
# and nothing else. The floor gets its own dedicated test against INDIA.
NO_FLOOR = Market("test", "Test Market", "https://example.invalid",
                  lambda raw: [], "INR", 0.0)


def series(volumes, closes=None, meta=None, start=1_700_000_000):
    """A PriceSeries with one bar per day, oldest first."""
    n = len(volumes)
    closes = closes if closes is not None else [100.0] * n
    return PriceSeries("TEST.NS", tuple(start + i * DAY for i in range(n)),
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
        self.entry = UniverseEntry("TEST.NS", "Test Ltd")

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
        # 100x volume and +100%, but the whole day traded 200 rupees. Clears the
        # spike rule and is still not a mover — India's floor is 1 crore.
        vols = [1.0] * 20 + [100.0]
        closes = [1.0] * 20 + [2.0]
        self.assertIsNotNone(self.evaluate(vols, closes, market=NO_FLOOR))
        self.assertIsNone(self.evaluate(vols, closes, market=INDIA))

    def test_evaluates_a_mid_series_bar(self):
        # The target is not always the last bar: mid-session runs look back one.
        vols, closes = flat(23, volume=1000.0)
        vols[21] = 10_000.0
        closes[21] = 110.0
        self.assertIsNotNone(self.evaluate(vols, closes, index=21))
        self.assertIsNone(self.evaluate(vols, closes, index=22))

    def test_prefers_the_feeds_company_name(self):
        vols, closes = flat(21, volume=1000.0)
        vols[-1], closes[-1] = 10_000.0, 110.0
        s = series(vols, closes, meta={"longName": "Test Industries Limited",
                                       "currency": "INR"})
        hit = self.det.evaluate(self.entry, s, len(vols) - 1, NO_FLOOR, "2026-07-24")
        self.assertEqual(hit.name, "Test Industries Limited")
        self.assertEqual(hit.currency, "INR")


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


# --- universe parsers -------------------------------------------------------

NSE_CSV = (b"SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE,"
           b" MARKET LOT, ISIN NUMBER, FACE VALUE\n"
           b"20MICRONS,20 Microns Limited,EQ,06-OCT-2008,5,1,INE144J01027,5\n"
           b"SMALLCO,Small Co Limited,SM,01-JAN-2020,10,1,INE000A01000,10\n"
           b"BIGCO,Big Co Limited,EQ,01-JAN-2020,10,1,INE000A01001,10\n")

NASDAQ_TXT = (b"Symbol|Security Name|Market Category|Test Issue|Financial Status|"
              b"Round Lot Size|ETF|NextShares\n"
              b"AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\n"
              b"QQQ|Invesco QQQ Trust|Q|N|N|100|Y|N\n"
              b"ZTEST|Test Issue|Q|Y|N|100|N|N\n"
              b"File Creation Time: 0727202618:00|||||||\n")

OTHER_TXT = (b"ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|"
             b"Test Issue|NASDAQ Symbol\n"
             b"A|Agilent Technologies, Inc. Common Stock|N|A|N|100|N|A\n"
             b"BRK.A|Berkshire Hathaway Class A|N|BRKA|N|1|N|BRK.A\n"
             b"SPY|SPDR S&P 500|P|SPY|Y|100|N|SPY\n"
             b"IMO|Imperial Oil Limited|A|IMO|N|100|N|IMO\n")


class TestParsers(unittest.TestCase):
    def test_nse_keeps_only_the_eq_series_and_suffixes(self):
        entries = parse_nse(NSE_CSV)
        self.assertEqual([e.symbol for e in entries], ["20MICRONS.NS", "BIGCO.NS"])
        self.assertEqual(entries[0].name, "20 Microns Limited")

    def test_nse_header_has_leading_spaces(self):
        # ` SERIES` with a leading space is really what NSE serves; if the
        # parser stopped stripping header whitespace every row would be dropped.
        self.assertTrue(parse_nse(NSE_CSV))

    def test_nasdaq_drops_etfs_test_issues_and_the_footer(self):
        entries = parse_nasdaq(NASDAQ_TXT)
        self.assertEqual([e.symbol for e in entries], ["AAPL"])

    def test_nyse_filters_by_exchange_and_rewrites_share_classes(self):
        entries = parse_nyse(OTHER_TXT)
        # Yahoo writes Berkshire's A class as BRK-A, not BRK.A.
        self.assertEqual([e.symbol for e in entries], ["A", "BRK-A"])

    def test_amex_reads_the_same_file_with_a_different_exchange(self):
        self.assertEqual([e.symbol for e in parse_amex(OTHER_TXT)], ["IMO"])

    def test_every_registered_market_has_a_working_parser(self):
        for key, market in MARKETS.items():
            self.assertTrue(callable(market.parser), key)
            self.assertTrue(market.source.startswith("https://"), key)


class TestUniverseRepository(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def repo(self, **kw):
        return UniverseRepository(self.dir, **kw)

    def test_fetch_then_serve_from_cache(self):
        repo = self.repo()
        calls = []

        def fake_fetch(market):
            calls.append(market.key)
            return [UniverseEntry("AAA.NS", "Aaa Ltd")]

        repo._fetch = fake_fetch
        entries, stale = repo.load("india")
        self.assertEqual(len(entries), 1)
        self.assertFalse(stale)

        # A second load inside the TTL must not hit the network again.
        repo2 = self.repo()
        repo2._fetch = fake_fetch
        entries, stale = repo2.load("india")
        self.assertEqual([e.symbol for e in entries], ["AAA.NS"])
        self.assertEqual(len(calls), 1)

    def test_stale_cache_is_used_when_the_fetch_fails(self):
        repo = self.repo()
        repo._write("india", [UniverseEntry("AAA.NS", "Aaa Ltd")])
        # Age the cache past the TTL.
        path = repo._path("india")
        old = time.time() - 30 * 86400
        os.utime(path, (old, old))

        def boom(market):
            raise RuntimeError("NSE is down")

        repo._fetch = boom
        entries, stale = repo.load("india")
        self.assertEqual([e.symbol for e in entries], ["AAA.NS"])
        self.assertTrue(stale)   # a week-old symbol list beats a failed scan

    def test_no_cache_and_a_failed_fetch_raises(self):
        repo = self.repo()

        def boom(market):
            raise RuntimeError("NSE is down")

        repo._fetch = boom
        # Loud, so a broken universe can never look like a quiet market.
        with self.assertRaises(UniverseUnavailable):
            repo.load("india")

    def test_empty_result_raises_rather_than_scanning_nothing(self):
        repo = self.repo()
        repo._fetch = lambda market: []
        with self.assertRaises(UniverseUnavailable):
            repo.load("india")

    def test_unknown_market_raises(self):
        with self.assertRaises(UniverseUnavailable):
            self.repo().load("atlantis")


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
        # Sized to clear India's 1-crore turnover floor, so these tests exercise
        # failure isolation rather than accidentally testing the liquidity rule.
        vols = [5_000.0] * 20 + [200_000.0]
        closes = [100.0] * 20 + [120.0]
        s = series(vols, closes, meta=self.meta, start=start)
        return PriceSeries(symbol, s.timestamps, s.closes, s.volumes, s.meta)


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
        result = build(feed, symbols, max_workers=4).scan("india")
        self.assertEqual(result.stats["failed"], 3)
        self.assertEqual(result.stats["no_data"], 3)
        self.assertEqual(result.stats["scanned"], 14)
        self.assertEqual(len(result.hits), 14)   # the rest still came through

    def test_a_mostly_failed_run_is_degraded_not_empty(self):
        symbols = [f"S{i}" for i in range(20)]
        result = build(StubFeed(broken=symbols[:15]), symbols).scan("india")
        self.assertTrue(result.degraded)
        self.assertEqual(result.stats["failed"], 15)

    def test_a_healthy_quiet_run_is_not_degraded(self):
        symbols = [f"S{i}" for i in range(20)]
        result = build(StubFeed(missing=symbols), symbols).scan("india")
        self.assertFalse(result.degraded)
        self.assertEqual(len(result.hits), 0)

    def test_stale_sessions_are_excluded_from_hits(self):
        # Names whose last completed session is weeks old (halted, dormant) get
        # counted, not shown: their spike is against a stale baseline.
        symbols = [f"S{i}" for i in range(10)]
        result = build(StubFeed(stale=symbols[:3]), symbols).scan("india")
        self.assertEqual(result.stats["stale"], 3)
        self.assertEqual(len(result.hits), 7)

    def test_hits_are_sorted_by_rvol(self):
        symbols = [f"S{i}" for i in range(5)]
        result = build(StubFeed(), symbols).scan("india")
        rvols = [h.rvol for h in result.hits]
        self.assertEqual(rvols, sorted(rvols, reverse=True))

    def test_progress_is_reported_for_every_symbol(self):
        symbols = [f"S{i}" for i in range(12)]
        seen = []
        build(StubFeed(), symbols, max_workers=3).scan(
            "india", progress_cb=lambda done, total: seen.append((done, total)))
        self.assertEqual(len(seen), 12)
        self.assertEqual(max(d for d, _ in seen), 12)
        self.assertTrue(all(t == 12 for _, t in seen))

    def test_a_stop_event_short_circuits_the_rest(self):
        import threading
        symbols = [f"S{i}" for i in range(30)]
        stop = threading.Event()
        stop.set()
        result = build(StubFeed(), symbols).scan("india", stop_event=stop)
        self.assertTrue(result.stopped)
        self.assertEqual(result.stats["skipped"], 30)

    def test_limit_caps_the_universe(self):
        symbols = [f"S{i}" for i in range(50)]
        result = build(StubFeed(), symbols).scan("india", limit=10)
        self.assertEqual(result.stats["total"], 10)

    def test_universe_staleness_is_carried_onto_the_result(self):
        result = build(StubFeed(), ["S1"], universe_stale=True).scan("india")
        self.assertTrue(result.universe_stale)


# --- persistence ------------------------------------------------------------

def make_result(market="india", session="2026-07-24", tickers=("AAA.NS",)):
    hits = tuple(
        Hit(t, f"{t} Ltd", 8.0, 9.0, 100.0, 5000.0, 600.0, 500_000.0, "INR", session)
        for t in tickers)
    return ScanResult(market, session, hits, ScanCriteria(), {"total": 1})


class TestScanStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.store = ScanStore(self.dir)

    def test_round_trip(self):
        self.store.save(make_result())
        data = self.store.latest("india")
        self.assertEqual(data["session_date"], "2026-07-24")
        self.assertEqual(len(data["hits"]), 1)
        self.assertEqual(data["hits"][0]["ticker"], "AAA.NS")

    def test_latest_picks_the_newest_session(self):
        self.store.save(make_result(session="2026-07-20"))
        self.store.save(make_result(session="2026-07-24"))
        self.assertEqual(self.store.latest("india")["session_date"], "2026-07-24")
        self.assertEqual(self.store.list_runs("india"),
                         ["2026-07-24", "2026-07-20"])

    def test_markets_are_kept_separate(self):
        self.store.save(make_result(market="india"))
        self.store.save(make_result(market="nasdaq", tickers=("BBB",)))
        self.assertEqual(self.store.latest("nasdaq")["hits"][0]["ticker"], "BBB")
        self.assertEqual(self.store.list_runs("amex"), [])

    def test_update_hit_merges_enrichment(self):
        self.store.save(make_result())
        self.assertTrue(self.store.update_hit("india", "2026-07-24", "AAA.NS",
                                              sector="Energy", market_cap=1.2e9))
        hit = self.store.latest("india")["hits"][0]
        self.assertEqual(hit["sector"], "Energy")
        self.assertEqual(hit["market_cap"], 1.2e9)
        # Untouched fields survive the merge.
        self.assertEqual(hit["rvol"], 8.0)

    def test_update_hit_on_a_missing_run_is_false_not_an_error(self):
        self.assertFalse(self.store.update_hit("india", "1999-01-01", "AAA.NS",
                                               sector="X"))

    def test_analyses_are_written_back_one_at_a_time(self):
        self.store.save(make_result(tickers=("AAA.NS", "BBB.NS")))
        self.store.save_analysis("india", "2026-07-24",
                                 HitAnalysis("AAA.NS", "A note.", "ok"))
        data = self.store.latest("india")
        # The first note is durable even though the second never arrived.
        self.assertEqual(data["analyses"]["AAA.NS"]["summary"], "A note.")
        self.assertNotIn("BBB.NS", data["analyses"])

    def test_writes_are_atomic(self):
        self.store.save(make_result())
        # No .tmp file survives a completed write, so a crash can only ever
        # leave the previous complete file or the new one — never a partial.
        self.assertFalse([n for n in os.listdir(self.dir) if n.endswith(".tmp")])

    def test_prune_drops_old_runs_and_keeps_recent_ones(self):
        self.store.save(make_result(session="2026-07-24"))
        self.store.save(make_result(session="2026-01-01"))
        old = self.store._path("india", "2026-01-01")
        stamp = time.time() - 120 * 86400
        os.utime(old, (stamp, stamp))
        self.store.prune(60)
        self.assertEqual(self.store.list_runs("india"), ["2026-07-24"])

    def test_latest_on_an_unknown_market_is_none(self):
        self.assertIsNone(self.store.latest("nasdaq"))

    def test_corrupt_file_is_ignored_rather_than_crashing(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "india_2026-07-24.json"), "w") as f:
            f.write("{not json")
        self.assertIsNone(self.store.latest("india"))

    def test_hits_rebuild_from_stored_json(self):
        self.store.save(make_result(tickers=("AAA.NS", "BBB.NS")))
        hits = hits_from_stored(self.store.latest("india"))
        self.assertEqual([h.ticker for h in hits], ["AAA.NS", "BBB.NS"])
        self.assertEqual(hits[0].session_date, "2026-07-24")

    def test_hits_rebuild_skips_malformed_rows(self):
        hits = hits_from_stored({"hits": [{"no_ticker": 1},
                                          {"ticker": "OK.NS", "name": "Ok"}]})
        self.assertEqual([h.ticker for h in hits], ["OK.NS"])


# --- the analyst ------------------------------------------------------------

class TestAnalyst(unittest.TestCase):
    def setUp(self):
        from market_scan.analyst import OpenRouterAnalyst, build_messages
        self.build_messages = build_messages
        self.Analyst = OpenRouterAnalyst
        self.hit = Hit("DEEPINDS.NS", "Deep Industries Limited", 6.2, 8.4, 516.45,
                       1_631_764, 263_000, 8.4e8, "INR", "2026-07-24",
                       sector="Energy")

    def test_prompt_carries_the_measured_facts(self):
        messages = self.build_messages(self.hit, "India (NSE)")
        self.assertEqual(messages[0]["role"], "system")
        user = messages[1]["content"]
        # The model must explain *this* move, not the company in general.
        for fragment in ("DEEPINDS.NS", "Deep Industries Limited", "India (NSE)",
                         "2026-07-24", "6.2x", "+8.4%", "Energy"):
            self.assertIn(fragment, user)

    def test_prompt_forbids_inventing_a_catalyst(self):
        system = self.build_messages(self.hit, "India (NSE)")[0]["content"]
        self.assertIn("no identifiable public catalyst", system)

    def test_prompt_asks_the_users_question(self):
        user = self.build_messages(self.hit, "India (NSE)")[1]["content"]
        self.assertIn("business model and investment thesis", user)
        self.assertIn("margin expansion", user)

    def test_unknown_sector_and_cap_are_stated_not_faked(self):
        bare = Hit("X.NS", "X Ltd", 6.0, 6.0, 10.0, 1.0, 1.0, 1.0, "INR", "2026-07-24")
        user = self.build_messages(bare, "India (NSE)")[1]["content"]
        self.assertIn("Sector: not known", user)
        self.assertIn("Market cap: not known", user)

    def test_a_missing_api_key_fails_the_note_not_the_run(self):
        analysis = self.Analyst("", "some/model").explain(self.hit)
        self.assertEqual(analysis.status, "failed")
        self.assertIn("OPENROUTER_API_KEY", analysis.error)
        self.assertEqual(analysis.ticker, "DEEPINDS.NS")

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

class TestEnrich(unittest.TestCase):
    def test_enrichment_never_raises_when_yfinance_is_missing(self):
        # The tests run without yfinance installed; enrichment must degrade to a
        # no-op rather than taking the run down with it.
        from market_scan.enrich import enrich
        hit = Hit("X.NS", "X", 6.0, 6.0, 1.0, 1.0, 1.0, 1.0, "INR", "2026-07-24")

        class ExplodingStore:
            def update_hit(self, *a, **kw):
                raise RuntimeError("store is down")

        self.assertIsInstance(enrich([hit], ExplodingStore(), "india",
                                     "2026-07-24"), int)


if __name__ == "__main__":
    unittest.main()
