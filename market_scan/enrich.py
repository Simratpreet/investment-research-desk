"""Best-effort sector and market cap for the hits.

The public symbol directories carry a ticker and a company name and nothing
else, so sector and market cap have to come from somewhere. Yahoo's
`quoteSummary` needs a crumb+cookie handshake — unauthenticated it just returns
429 — but `yfinance` already does that dance and is already a pinned dependency
(`alerts/price_action.py` reads `yf.Ticker(...).info` for currency), so this
reuses a path the app already trusts.

Two rules make this safe to bolt onto the end of a run:

  - **Hits only, never the universe.** ~33 lookups, not 4,000.
  - **Never raises, never blocks the result.** The scan is already persisted
    before this runs; a 429 leaves sector/market cap null and the table renders
    an em dash. Missing metadata is a cosmetic loss, a lost scan is not.
"""

import time
from concurrent.futures import ThreadPoolExecutor

# yfinance is heavier on Yahoo than the chart endpoint (several requests per
# symbol), and it runs immediately after a scan that just made thousands. Keep
# concurrency low and pace the calls so enrichment doesn't provoke the limit
# that the scan itself managed to avoid.
MAX_WORKERS = 2
PAUSE_S = 0.3

_FIELDS = ("sector", "industry", "marketCap")


def enrich(hits, store, market: str, session_date: str,
           max_workers: int = MAX_WORKERS) -> int:
    """Fill sector/market cap for `hits`, writing each back as it lands.

    Returns the number enriched. Swallows everything — including yfinance being
    absent entirely, which is how the unit tests run.
    """
    try:
        import yfinance as yf
    except ImportError:
        return 0

    filled = 0

    def one(hit):
        try:
            info = yf.Ticker(hit.ticker).info or {}
        except Exception:
            return False
        sector = info.get("sector") or info.get("industry")
        market_cap = info.get("marketCap")
        if not sector and not market_cap:
            return False
        try:
            store.update_hit(market, session_date, hit.ticker,
                             sector=sector,
                             market_cap=float(market_cap) if market_cap else None)
        except Exception:
            return False
        time.sleep(PAUSE_S)
        return True

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            filled = sum(1 for ok in pool.map(one, hits) if ok)
    except Exception:
        return filled
    return filled
