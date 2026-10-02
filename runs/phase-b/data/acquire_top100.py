"""Acquire the Tiingo prices the PIT top-100 universe needs, and build it (resumable).

Runs the real UniverseBuilder (discovery, lineage, PIT screen, symbol resolution,
pricing, market-cap ranking) for the 61 quarterly dates 2011-07-01 .. 2026-07-01.
Pricing downloads each missing symbol once through a budget-gated TiingoClient:

  window     2008-01-01 .. 2026-08-31. Start: H-RESMOM regresses on 36 months of
             returns, so the first development rebalance (2011-07-01) needs data from
             2008-07; 2008-01-01 leaves a half-year margin. End: the day before the
             sealed holdout's fence (2026-09-01) — no embargo or holdout bar is fetched.
  budget     Starter plan (manually verified): 500 unique symbols/month, 50 req/hour,
             1000 req/day; 1 GB/month published (2 GB shown available). Caps at 90%:
             450 symbols, 45/hour, 900/day, 0.9 GB. The ledger is seeded with this
             month's earlier use (SPY measurement + key check on 2026-10-01).
  resume     builder snapshots are cached per date; Tiingo files per symbol; the budget
             ledger persists. Rerun after a stop continues where it left off.

Exit 0 = all dates built; 3 = a budget cap stopped it (resume next window/month).
Licensed data stays in the cache directory (outside git); the repo gets membership only.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from hedge_fund.data.edgar import EdgarClient
from hedge_fund.data.edgar.client import EdgarClientError
from hedge_fund.data.request_budget import BudgetExhausted, Limits, RequestBudget
from hedge_fund.data.tiingo import TiingoClient, TiingoClientError
from hedge_fund.paths import CACHE_DIR
from hedge_fund.universe.builder import UniverseBuilder, reconstitution_dates, save_schedule
from hedge_fund.universe.models import UniverseConfig, UniverseSchedule

OUT = Path(__file__).resolve().parent
START, END = "2011-07-01", "2026-07-01"
WINDOW = ("2008-01-01", "2026-08-31")
LIMITS = Limits(unique_symbols_per_month=450, bytes_per_month=900_000_000, requests_per_day=900,
                requests_per_hour=45)
LEDGER = CACHE_DIR / "ledgers" / "tiingo_requests.jsonl"
PRIVATE = CACHE_DIR / "universe_runs"            # full snapshots incl. prices: outside git


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def seed(budget: RequestBudget) -> None:
    if budget.entries():
        return
    t = 1790881680.0                                   # 2026-10-01T19:08Z
    budget.record(None, 70, 200, ts=t, note="/api/test key check (no symbol)")
    budget.record("SPY", 16_062, 200, ts=t + 60, note="wire-size measurement, 2026-01-02..2026-03-31")


def main() -> int:
    budget = RequestBudget(LEDGER, LIMITS)
    seed(budget)
    log(f"budget at start: {json.dumps({k: v for k, v in budget.usage().items() if k != 'symbols'})}")
    config = UniverseConfig(top_n=100)
    with TiingoClient(history_start=WINDOW[0], history_end=WINDOW[1], max_age_hours=None, budget=budget) as tiingo, \
            EdgarClient(price_source=tiingo, max_age_hours=None) as edgar:
        sessions = [p.time[:10] for p in tiingo.get_prices("SPY", START, "2026-07-31")]
        dates = reconstitution_dates(sessions, START, END, "quarterly")
        builder = UniverseBuilder(edgar, tiingo, config)
        snapshots = []
        for d in dates:
            for attempt in range(6):
                try:
                    snap = builder.snapshot(d)
                    break
                except BudgetExhausted as exc:
                    log(f"STOP at {d}: {exc}")
                    log(f"budget: {json.dumps({k: v for k, v in budget.usage().items() if k != 'symbols'})}")
                    return 3
                except (EdgarClientError, TiingoClientError) as exc:
                    if attempt == 5 or getattr(exc, "status_code", None) in (401, 403):
                        raise
                    log(f"{d}: transient error, retry in {60 * (attempt + 1)}s: {str(exc)[:100]}")
                    time.sleep(60 * (attempt + 1))
            snapshots.append(snap)
            u = budget.usage()
            log(f"{d}: members {len(snap.members)} candidates {snap.candidates} | Tiingo month: "
                f"{u['unique_symbols']} symbols, {u['bytes'] / 1e6:.1f} MB, {u['requests_today']} today")
        schedule = UniverseSchedule(config=config, snapshots=snapshots)
        PRIVATE.mkdir(parents=True, exist_ok=True)
        save_schedule(schedule, PRIVATE / "top100_schedule_full.json")         # has prices: private
        membership = {"config": config.model_dump(mode="json"), "config_digest": config.digest(),
                      "window": WINDOW,
                      "snapshots": [{"as_of": s.as_of, "candidates": s.candidates,
                                     "members": [{"rank": m.rank, "ticker": m.ticker, "cik": m.cik, "name": m.name,
                                                  "symbol_source": m.symbol_source} for m in s.members],
                                     "excluded_reasons": _reasons(s)} for s in snapshots]}
        (OUT / "top100_membership.json").write_text(json.dumps(membership, indent=1))
    log(f"done: {len(dates)} dates; budget {json.dumps({k: v for k, v in budget.usage().items() if k != 'symbols'})}")
    return 0


def _reasons(snap) -> dict:
    out: dict[str, int] = {}
    for e in snap.excluded:
        out[e.reason] = out.get(e.reason, 0) + 1
    return out


if __name__ == "__main__":
    sys.exit(main())
