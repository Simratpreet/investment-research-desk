"""Best-effort sector for the hits, and a market cap only where one is missing.

The exports carry a ticker, a company name and a market cap, but no sector — so
that is what this fetches. Yahoo's `quoteSummary` needs a crumb+cookie handshake
(unauthenticated it just returns 429), but `yfinance` already does that dance
and is already a pinned dependency (`alerts/price_action.py` reads
`yf.Ticker(...).info` for currency), so this reuses a path the app already
trusts.

Three rules make this safe to bolt onto the end of a run:

  - **Hits only, never the universe.** ~30 lookups, not 4,000.
  - **Never raises, never blocks the result.** The scan is already persisted
    before this runs; a 429 leaves the sector null and the table renders an em
    dash. Missing metadata is a cosmetic loss, a lost scan is not.
  - **Never overwrites what the export already knew.** Yahoo's cap can be badly
    wrong for microcaps — a $0.33 name that turned over $67M in a session came
    back as a $1.9M company — so a cap from the CSV wins and Yahoo only fills a
    genuine gap.
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
        fields = {}
        sector = info.get("sector") or info.get("industry")
        if sector:
            fields["sector"] = sector
        # Only ever fills a gap — see the note at the top about Yahoo's caps.
        if hit.market_cap is None and info.get("marketCap"):
            try:
                fields["market_cap"] = float(info["marketCap"])
            except (TypeError, ValueError):
                pass
        if not fields:
            return False
        try:
            store.update_hit(market, session_date, hit.ticker, **fields)
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
