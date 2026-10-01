"""Quarterly PIT top-100 universe — SEC stages only (no Tiingo download, no price read).

For every quarterly reconstitution date 2011-07-01 .. 2026-07-01 (development
period; nothing reaches the 2026-09-01 fence) this runs the real
UniverseBuilder stages that need only SEC EDGAR public data:

    (symbols: curated history, SEC current map, cover-page dei:TradingSymbol on the
    latest four periodic filings, then the 10-K text's listing sentence)

    discovery  top 4 x top_n public-float filers per frame (years D-2..D), and
               only rows whose float was filed by D (UNIVERSE_VERSION 3)
    lineage    predecessor/successor registrants as one company
    screen     point-in-time filings, filer type, plausible float

and then resolves, for the screened candidates in float order, the exact
symbols the pricing stage would try (curated history, SEC current map, the
trading symbol on the company's own latest filing — in the builder's order).

The pricing stage (Tiingo prices for market-cap ranking) is NOT run: it needs
licensed downloads beyond what is cached. The symbol lists it would need are
written instead, as exact sets:

    first_try    the first symbol of each of the first pool_size (= 2 x top_n)
                 screened candidates per date — what pricing needs if every
                 first symbol prices (lower bound)
    contingency  every symbol of the first pool_size + buffer candidates per
                 date — what pricing could need if symbols fail and deeper
                 candidates must fill the pool (upper bound used for planning)

Tiingo is opened offline and only asked whether a symbol's file exists.
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

from hedge_fund.data.edgar import EdgarClient
from hedge_fund.data.tiingo import TiingoClient, tiingo_symbol
from hedge_fund.universe.builder import UniverseBuilder, reconstitution_dates
from hedge_fund.universe.models import UniverseConfig

OUT = Path(__file__).resolve().parent
START, END = "2011-07-01", "2026-07-01"
TOP_N = 100
BUFFER = 0.25           # extra candidates per date beyond the pool, sized from the measured 19% pricing failures


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def main() -> int:
    config = UniverseConfig(top_n=TOP_N)
    pool = config.pool_size
    depth = pool + math.ceil(BUFFER * pool)
    with TiingoClient(offline=True) as tiingo, EdgarClient(price_source=tiingo) as edgar:
        sessions = [p.time[:10] for p in tiingo.get_prices("SPY", START, "2026-07-31")]
        dates = reconstitution_dates(sessions, START, END, "quarterly")
        builder = UniverseBuilder(edgar, tiingo, config, max_price_downloads=0)
        log(f"{len(dates)} quarterly dates {dates[0]}..{dates[-1]}, pool {pool}, depth {depth}")
        snapshots = []
        for d in dates:
            n_canonical, excluded, screened = builder.screened(d)
            rows = []
            for rank, cand in enumerate(screened[:depth], start=1):
                symbols = [t for t, _ in builder._symbols(cand, d)]
                sources = [s for _, s in builder._symbols(cand, d)]
                if not symbols:                                     # delisted names: the filing's own symbol
                    cover = builder._cover_symbols(cand) or builder._text_symbols(cand)
                    symbols, sources = [t for t, _ in cover], [s for _, s in cover]
                rows.append({"float_rank": rank, "cik": cand.cik, "name": cand.name,
                             "public_float": cand.public_float, "float_filed": cand.float_filed,
                             "latest_filing": cand.latest.filed, "symbols": symbols, "sources": sources,
                             "in_pool": rank <= pool})
            reasons: dict[str, int] = {}
            for e in excluded:
                reasons[e.reason] = reasons.get(e.reason, 0) + 1
            snapshots.append({"as_of": d, "candidates": n_canonical, "screened": len(screened),
                              "exclusions": reasons, "ranked": rows,
                              "excluded_detail": [e.model_dump() for e in excluded
                                                  if e.reason in ("predecessor", "successor_not_yet",
                                                                  "stopped_filing", "nominated_after_as_of")]})
            log(f"{d}: candidates {n_canonical} screened {len(screened)} "
                f"no-symbol {sum(1 for r in rows if not r['symbols'])} SEC requests {edgar.requests}")

        first_try, contingency = set(), set()
        for s in snapshots:
            for r in s["ranked"]:
                if r["symbols"]:
                    if r["in_pool"]:
                        first_try.add(tiingo_symbol(r["symbols"][0]))
                    contingency |= {tiingo_symbol(t) for t in r["symbols"]}
        stored = {s for s in contingency if tiingo.is_stored(s)}
        report = {
            "config": config.model_dump(mode="json"), "config_digest": config.digest(), "dates": dates,
            "pool_size": pool, "depth": depth, "sec_requests": edgar.requests, "tiingo_requests": tiingo.requests,
            "first_try": {"symbols": sorted(first_try), "n": len(first_try),
                          "cached": sorted(first_try & stored), "missing": sorted(first_try - stored)},
            "contingency": {"symbols": sorted(contingency), "n": len(contingency),
                            "cached": sorted(contingency & stored), "missing": sorted(contingency - stored)},
            "candidates_without_symbol": sorted({(r["cik"], r["name"]) for s in snapshots for r in s["ranked"]
                                                 if r["in_pool"] and not r["symbols"]}),
        }
    (OUT / "top100_sec_stage.json").write_text(json.dumps({"summary": report, "snapshots": snapshots}, indent=1,
                                                           default=str))
    log(f"first_try {len(first_try)} (cached {len(first_try & stored)}), contingency {len(contingency)} "
        f"(cached {len(contingency & stored)}); Tiingo requests {report['tiingo_requests']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
