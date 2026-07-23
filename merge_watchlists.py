#!/usr/bin/env python3
"""
One-shot migration: fold international-watchlist-alerts/watchlist.txt into the
curated stock-watchlist/watchlist.json to produce a single MASTER watchlist.

Design decisions (confirmed with the owner):
  * Master format is the rich JSON (ticker/exchange/company/notes/added_date).
  * Every entry gains a `track` list controlling which subsystems consume it:
      - "ta"   -> TA scoring + earnings + price-action alerts (curated only)
      - "news" -> news / IR scanner (everything)
    Curated names (already in watchlist.json) get ["news", "ta"].
    Net-new international names get ["news"] only, so the recurring
    earnings/price alert loop stays on the curated set.
  * On overlap (same ticker in both lists), keep the curated entry
    (preserves notes + added_date) and adopt the more-correct exchange label.
    Every exchange-code conflict here is cosmetic for Yahoo resolution
    (OB/OSL->.OL, XETRA/XETR->.DE, AIM/LSE->.L, US venues->no suffix), so
    relabeling carries no functional risk to the TA pipeline.

Idempotent-ish: rewrites watchlist.json from the .pre-merge.bak baseline +
the txt, so it can be re-run after tweaking the tables below.

Usage:
    python3 merge_watchlists.py           # write the merged master
    python3 merge_watchlists.py --dry-run # report only, don't write
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
CURATED_BASELINE = BASE / "watchlist.json.pre-merge.bak"  # pristine 91-name list
CURATED_LIVE = BASE / "watchlist.json"
INTL_TXT = BASE.parent / "international-watchlist-alerts" / "watchlist.txt"
OUT = BASE / "watchlist.json"

# --- Exchange label normalization (cosmetic; all map to same Yahoo suffix) ---
# Collapse pure aliases so the master uses one code per venue.
EXCHANGE_ALIASES = {
    "OB": "OSL",       # Oslo Børs  -> .OL
    "XETRA": "XETR",   # Deutsche Börse Xetra -> .DE
    # AIM kept distinct from LSE (both .L) — AIM is a real, separate market.
}

# Overlapping US tickers whose curated label is the wrong primary listing.
# Only entries the maintainer is certain about; all map to no Yahoo suffix,
# so this is a display-accuracy fix, not a resolution fix.
US_PRIMARY_LISTING_FIX = {
    "AXON": "NASDAQ",  # Axon Enterprise
    "MELI": "NASDAQ",  # MercadoLibre
    "KSPI": "NASDAQ",  # Kaspi.kz ADS
    "ASYS": "NASDAQ",  # Amtech Systems
    "HURC": "NASDAQ",  # Hurco
    "GWRE": "NYSE",    # Guidewire
    "APP": "NASDAQ",   # AppLovin (curated had a stale NYSE:APP row too)
    "KSPI": "NASDAQ",  # Kaspi.kz (curated had both NASDAQ + NYSE rows)
}


def norm_exchange(code: str) -> str:
    code = code.strip().upper()
    return EXCHANGE_ALIASES.get(code, code)


def load_curated() -> list[dict]:
    src = CURATED_BASELINE if CURATED_BASELINE.exists() else CURATED_LIVE
    return json.loads(src.read_text())


def load_intl() -> list[tuple[str, str]]:
    out = []
    for tok in re.split(r"[,\n]+", INTL_TXT.read_text()):
        tok = tok.strip()
        if ":" in tok:
            ex, ti = tok.split(":", 1)
            out.append((norm_exchange(ex), ti.strip().upper()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    curated = load_curated()
    intl = load_intl()

    # Index curated by ticker (there are 0 duplicate tickers across exchanges
    # in practice; if any, the first wins and others are kept as-is).
    by_ticker: dict[str, dict] = {}
    master: list[dict] = []
    relabeled = []
    for e in curated:
        e = dict(e)
        tkr = e["ticker"].upper()
        old_ex = e["exchange"].upper()
        new_ex = norm_exchange(old_ex)
        new_ex = US_PRIMARY_LISTING_FIX.get(tkr, new_ex)
        if new_ex != old_ex:
            relabeled.append((tkr, old_ex, new_ex))
        e["exchange"] = new_ex
        e["track"] = ["news", "ta"]            # curated -> everything
        master.append(e)
        by_ticker.setdefault(tkr, e)

    # Add net-new international names (news-only). Skip any ticker already
    # curated — the curated entry already covers news via its track tag.
    added, skipped = 0, 0
    now = datetime.now(timezone.utc).isoformat()
    for ex, ti in intl:
        if ti in by_ticker:
            skipped += 1
            continue
        entry = {
            "ticker": ti,
            "exchange": ex,
            "company": "",          # resolved lazily by the news scanner
            "notes": "",
            "added_date": now,
            "track": ["news"],
        }
        master.append(entry)
        by_ticker[ti] = entry
        added += 1

    # Collapse any identical EXCHANGE:TICKER rows (the curated list carried a
    # few same-ticker-two-exchange entries that relabeling made identical).
    # Keep the first occurrence; union the track tags; keep the richest notes.
    deduped: dict[str, dict] = {}
    collapsed = []
    for e in master:
        key = f"{e['exchange']}:{e['ticker']}".upper()
        if key in deduped:
            keep = deduped[key]
            keep["track"] = sorted(set(keep["track"]) | set(e["track"]),
                                   reverse=True)  # "news","ta" order
            keep["notes"] = keep.get("notes") or e.get("notes", "")
            collapsed.append(key)
        else:
            deduped[key] = e
    master = list(deduped.values())
    if collapsed:
        print(f"Collapsed {len(collapsed)} duplicate pair(s): {collapsed}")

    ta_count = sum(1 for e in master if "ta" in e["track"])
    news_count = sum(1 for e in master if "news" in e["track"])

    print(f"curated (kept, track=news+ta): {len(curated)}")
    print(f"international net-new (track=news): {added}")
    print(f"international skipped (ticker already curated): {skipped}")
    print(f"MASTER total: {len(master)}  |  ta-tracked: {ta_count}  "
          f"news-tracked: {news_count}")
    if relabeled:
        print(f"\nExchange relabels ({len(relabeled)}):")
        for tkr, old, new in relabeled:
            print(f"  {tkr}: {old} -> {new}")

    all_codes = sorted({e["exchange"] for e in master})
    print(f"\nMaster exchange vocabulary ({len(all_codes)}): {all_codes}")

    if args.dry_run:
        print("\n[dry-run] watchlist.json not written.")
        return 0

    OUT.write_text(json.dumps(master, indent=2, ensure_ascii=False))
    print(f"\nWrote {OUT} ({len(master)} names).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
