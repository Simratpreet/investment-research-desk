"""Pure data and rules for the Movers scanner. No I/O, no clock, no network.

Everything here is a frozen dataclass or a method over one, so the whole filter
rule is unit-testable without touching Yahoo. The layers above (feed, scanner,
service) are the only places that talk to the outside world.
"""

from dataclasses import dataclass, field, replace
from typing import Any, Callable

# Direction is fixed: we want stocks people are buying into. A -8% day on 5x
# volume is a different question (distribution) and would want its own page.
UP = "up"


@dataclass(frozen=True)
class Market:
    """One scannable exchange universe.

    `csv_file` is the export's filename inside the universes directory and
    `parser` the function that turns its bytes into UniverseEntry rows — see
    universe.py. `min_turnover` is in `currency` units and drops names that
    clear 5x volume on a handful of shares (an illiquid microcap's "spike" is a
    rounding error).
    """
    key: str
    label: str
    csv_file: str
    parser: Callable[[bytes], list]
    currency: str
    min_turnover: float
    # The date the committed export was taken, as YYYY-MM-DD. Recorded here
    # rather than read from the file's mtime because a git checkout and a Docker
    # build both stamp mtime with the build time — a year-old export would look
    # brand new in production and the staleness warning would never fire.
    # Update this when you replace the CSV.
    exported_on: str = ""


@dataclass(frozen=True)
class UniverseEntry:
    """A name to scan. Symbol is already in Yahoo form (suffix applied).

    `market_cap` (absolute, in the market's currency) and `sector` come straight
    from the export, so a hit carries both without a per-symbol lookup. Either
    is None when that export didn't have the column — Stockholm's has neither —
    and enrichment then tries to fill the gap.
    """
    symbol: str
    name: str
    market_cap: float | None = None
    sector: str | None = None


@dataclass(frozen=True)
class PriceSeries:
    """Daily bars for one symbol, nulls already dropped.

    `meta` is Yahoo's chart `meta` block carried through verbatim: session.py
    reads `currentTradingPeriod` from it to spot an in-progress bar, and the
    scanner reads `longName`/`currency` so a directory that only gives us a
    symbol still yields a usable row.
    """
    symbol: str
    timestamps: tuple[int, ...]
    closes: tuple[float, ...]
    volumes: tuple[float, ...]
    meta: dict = field(default_factory=dict, compare=False)

    def __len__(self) -> int:
        return len(self.timestamps)


@dataclass(frozen=True)
class ScanCriteria:
    """The whole filter rule, in one place.

    Both conditions must hold — that is the load-bearing part, not the numbers.
    Measured live, `or` yields 800-1,200 hits/day across these markets, which is
    unreadable and unaffordable to run an LLM over; `and` keeps it to a page you
    can actually read.

    The thresholds themselves are a judgement call and live in config.py, where
    they are env-overridable. These defaults exist only for direct construction
    and tests.
    """
    min_rvol: float = 3.0
    min_change_pct: float = 3.0
    lookback: int = 20
    direction: str = UP

    def matches(self, rvol: float, change_pct: float) -> bool:
        return rvol >= self.min_rvol and change_pct >= self.min_change_pct

    def to_dict(self) -> dict:
        return {"min_rvol": self.min_rvol, "min_change_pct": self.min_change_pct,
                "lookback": self.lookback, "direction": self.direction}


@dataclass(frozen=True)
class Hit:
    """A name that cleared the criteria on the target session.

    `sector`/`market_cap` are None until enrich.py fills them in — the public
    symbol directories carry neither, and enrichment is best-effort by design.
    """
    ticker: str
    name: str
    rvol: float
    change_pct: float
    price: float
    volume: float
    avg_volume: float
    turnover: float
    currency: str
    session_date: str
    sector: str | None = None
    market_cap: float | None = None

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker, "name": self.name,
            "rvol": round(self.rvol, 2), "change_pct": round(self.change_pct, 2),
            "price": round(self.price, 4), "volume": int(self.volume),
            "avg_volume": int(self.avg_volume), "turnover": round(self.turnover, 2),
            "currency": self.currency, "session_date": self.session_date,
            "sector": self.sector, "market_cap": self.market_cap,
        }


@dataclass(frozen=True)
class HitAnalysis:
    """The per-stock note. `status` is ok | failed | skipped."""
    ticker: str
    summary: str = ""
    status: str = "pending"
    error: str | None = None
    model: str | None = None
    generated_at: str | None = None

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "summary": self.summary,
                "status": self.status, "error": self.error,
                "model": self.model, "generated_at": self.generated_at}


@dataclass(frozen=True)
class ScanResult:
    """One market's run for one session.

    `degraded` says the failure rate was high enough that an empty `hits` list
    means "we could not see the market", not "nothing moved". Reporting those
    two the same way would be the worst bug this page could have.
    """
    market: str
    session_date: str
    hits: tuple[Hit, ...]
    criteria: ScanCriteria
    stats: dict[str, Any]
    degraded: bool = False
    universe_stale: bool = False
    stopped: bool = False

    def to_dict(self) -> dict:
        return {
            "market": self.market, "session_date": self.session_date,
            "hits": [h.to_dict() for h in self.hits],
            "criteria": self.criteria.to_dict(), "stats": self.stats,
            "degraded": self.degraded, "universe_stale": self.universe_stale,
            "stopped": self.stopped,
        }


def with_enrichment(hit: Hit, sector: str | None, market_cap: float | None) -> Hit:
    """A copy of `hit` carrying sector/market cap. Frozen dataclasses need this."""
    return replace(hit, sector=sector, market_cap=market_cap)
