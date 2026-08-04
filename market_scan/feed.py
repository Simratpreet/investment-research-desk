"""Daily price/volume bars from Yahoo's chart endpoint.

One shared Session (connection pooling matters across 8 workers) and a browser
UA.

The important distinction is 404 vs everything else. A 404 means the symbol
doesn't exist on Yahoo — a delisted name still sitting in the exchange
directory — and is a normal, expected outcome for a few dozen names per run. A
429 or a 5xx means Yahoo is pushing back and the scan is losing visibility;
those retry, and if they keep failing they count as failures so the run can be
flagged degraded rather than reported as a quiet market.

Rate limiting is the thing that decides whether this scanner works at all, and
it is handled *across* workers rather than per symbol. Yahoo limits by IP and by
burst rate, so when one worker sees a 429 every other worker is about to see one
too; eight threads retrying independently turns a momentary limit into a
self-inflicted outage.

Three mechanisms, in order of how much they matter:

  1. **Pacing.** Every worker claims a slot from one shared schedule, so the
     whole run holds a steady requests-per-second regardless of worker count.
     This is what avoids the limit rather than reacting to it. Measured on a
     fresh budget the endpoint sustained ~17/s, but that budget refills slowly
     and a burst that big is what exhausts it, so the default is deliberately
     lower.
  2. **AIMD.** Each 429 halves the rate; sustained success walks it back toward
     the target. The real limit is undocumented and varies by IP, so the feed
     discovers it instead of assuming it.
  3. **A circuit breaker.** After a run of limits with nothing getting through,
     stop waiting and fail the rest fast. Yahoo will not relent inside this
     scan, and a run that hangs for half an hour is worse than one that finishes
     in two minutes and says plainly that it was throttled — `degraded` exists
     exactly so an unreliable run is reported as unreliable rather than as a
     quiet market.
"""

import random
import threading
import time

import requests

from .domain import PriceSeries

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Deliberately terse, and NOT the usual full Chrome string. Measured against the
# live endpoint: a bare "Mozilla/5.0" returns 200, while
#   Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ...
# returns 429 on the very first request, every time, from an IP with an
# untouched budget — Yahoo fingerprints that well-known headless/scraper
# signature. Sending no User-Agent at all also 429s. Please don't "fix" this by
# making it look more like a real browser; that is precisely what breaks it.
UA = "Mozilla/5.0"

# Target pace, in seconds between requests across all workers. 0.06 => ~16/s,
# measured clean over repeated runs: NSE's ~2,000 names in about two minutes and
# NASDAQ's ~4,300 in four and a half. The endpoint will burst faster (30/s
# measured over 150 symbols) but a short burst and a sustained 4,000-symbol run
# are different asks, and AIMD widens this the moment Yahoo objects.
BASE_INTERVAL_S = 0.06
MAX_INTERVAL_S = 4.0
# Consecutive successes before easing the pace back toward BASE_INTERVAL_S.
RECOVER_AFTER = 25

# Cooldown after a 429, doubling per consecutive limit and capped so a run can
# still finish. Reset as soon as a request succeeds.
COOLDOWN_BASE_S = 5.0
COOLDOWN_MAX_S = 60.0

# Consecutive limits, with nothing getting through, before the breaker opens.
MAX_CONSECUTIVE_LIMITS = 5


class FeedError(Exception):
    """Transient upstream failure that survived the retries."""


class YahooPriceFeed:
    def __init__(self, session: requests.Session | None = None, timeout: float = 25.0,
                 retries: int = 3, range_: str = "3mo",
                 interval: float = BASE_INTERVAL_S):
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": UA, "Accept": "application/json"})
        self._timeout = timeout
        self._retries = retries
        self._range = range_
        self._gate = threading.Lock()
        self._base_interval = interval
        self._interval = interval     # current pace, widened by AIMD on a 429
        self._next_slot = 0.0         # monotonic time the next request may go
        self._cooldown_deadline = 0.0  # monotonic; nothing goes out before this
        self._streak = 0              # consecutive 429s, drives the backoff
        self._ok_streak = 0           # consecutive successes, drives recovery

    # --- shared throttle ----------------------------------------------------

    def tripped(self) -> bool:
        """True once we've given up on getting through this run."""
        with self._gate:
            return self._streak >= MAX_CONSECUTIVE_LIMITS

    def _claim_slot(self) -> float:
        """Reserve this request's place in the shared schedule.

        Claiming under the lock and sleeping outside it is what makes the pace
        hold: workers queue for distinct slots instead of all waking at once and
        firing together, which is the burst that gets a run limited.
        """
        with self._gate:
            slot = max(time.monotonic(), self._next_slot, self._cooldown_deadline)
            self._next_slot = slot + self._interval
            return slot

    def _wait_turn(self):
        """Sleep until this request's slot (and any cooldown) has arrived."""
        slot = self._claim_slot()
        while not self.tripped():
            delay = slot - time.monotonic()
            if delay <= 0:
                return
            time.sleep(min(delay, 1.0))

    def _on_limit(self):
        """Multiplicative decrease: halve the rate and pause everyone."""
        with self._gate:
            self._streak += 1
            self._ok_streak = 0
            self._interval = min(self._interval * 2 or self._base_interval,
                                 MAX_INTERVAL_S)
            delay = min(COOLDOWN_BASE_S * (2 ** (self._streak - 1)), COOLDOWN_MAX_S)
            deadline = time.monotonic() + delay * (0.75 + random.random() * 0.5)
            # Never shorten a cooldown another worker already opened.
            self._cooldown_deadline = max(self._cooldown_deadline, deadline)
            self._next_slot = max(self._next_slot, self._cooldown_deadline)

    def _on_success(self):
        """Additive increase: ease back toward the target pace, slowly."""
        with self._gate:
            self._streak = 0
            self._cooldown_deadline = 0.0
            self._ok_streak += 1
            if self._ok_streak >= RECOVER_AFTER and self._interval > self._base_interval:
                self._ok_streak = 0
                self._interval = max(self._base_interval, self._interval * 0.7)

    # --- fetching -----------------------------------------------------------

    def fetch(self, symbol: str) -> PriceSeries | None:
        """Bars for `symbol`, or None if the symbol has no usable data.

        Raises FeedError when Yahoo is unreachable or throttling us.
        """
        url = CHART_URL.format(symbol=symbol)
        params = {"range": self._range, "interval": "1d"}
        last_error = "unknown"
        for attempt in range(self._retries):
            if self.tripped():
                # The breaker is open: drain the remaining symbols immediately
                # instead of each one sitting through its own cooldown.
                raise FeedError(f"{symbol}: rate limited, giving up on this run")
            self._wait_turn()
            try:
                r = self._session.get(url, params=params, timeout=self._timeout)
            except requests.RequestException as e:
                last_error = str(e)
            else:
                if r.status_code == 404:
                    self._on_success()
                    return None          # no such symbol — move on quietly
                if r.status_code == 200:
                    self._on_success()
                    try:
                        return _to_series(symbol, r.json())
                    except ValueError:
                        return None      # well-formed HTTP, unusable payload
                    except (TypeError, KeyError, AttributeError):
                        return None
                last_error = f"HTTP {r.status_code}"
                if r.status_code == 429:
                    self._on_limit()
                    continue             # the shared wait replaces a local sleep
                if r.status_code < 500:
                    # A 400/403 for one symbol is that symbol's problem.
                    return None
            if attempt < self._retries - 1:
                # Jittered backoff for connection errors and 5xx, which are
                # per-request rather than a limit on the whole run.
                time.sleep((2 ** attempt) * (0.5 + random.random()))
        raise FeedError(f"{symbol}: {last_error}")


def _to_series(symbol: str, payload: dict) -> PriceSeries:
    """Turn a chart response into a PriceSeries with the null bars removed.

    A bar is real iff it has a timestamp AND a volume. A null close survives:
    Yahoo pads holidays and halts with all-null bars (still dropped here), but
    a publication-lag bar carries real volume with the close still pending —
    measured live, NVDA's 08-03 bar served volume 127,547,828 with close=null.
    Such bars only occur at the series end (Yahoo publishes a session's bars in
    order), so keeping them never shortens a lookback window.
    """
    chart = (payload or {}).get("chart") or {}
    results = chart.get("result") or []
    if not results:
        raise ValueError("no result")
    res = results[0]
    stamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    if not stamps or len(closes) != len(stamps) or len(volumes) != len(stamps):
        raise ValueError("ragged series")

    ts, cl, vol = [], [], []
    for t, c, v in zip(stamps, closes, volumes):
        if t is None or v is None:
            continue
        ts.append(int(t))
        cl.append(float(c) if c is not None else None)
        vol.append(float(v))
    if not ts:
        raise ValueError("no usable bars")
    return PriceSeries(symbol, tuple(ts), tuple(cl), tuple(vol),
                       res.get("meta") or {})
