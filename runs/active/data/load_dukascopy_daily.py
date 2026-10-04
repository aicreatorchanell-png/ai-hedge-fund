"""Load Dukascopy daily bars (from monthly 1-hour BID candle files) for FX and index CFDs.

    python runs/active/data/load_dukascopy_daily.py START_MONTH END_MONTH [SYMBOL ...]

One request per (symbol, month); the datafeed throttles hard, so requests are spaced and
retried with backoff, and every month file is cached (resumable). BID prices are used: the
cost model charges the half spread on every fill. Hours are aggregated into one bar per
UTC weekday (weekend hours merge into Monday) before being split by month, then written
to the catalog as 1-DAY bars. The repo gets only dukascopy_daily_quality.json.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
import requests

from hedge_fund.trading.data import dukascopy
from hedge_fund.trading.data.binance import months
from hedge_fund.trading.data.catalog import Catalog, bar_type
from hedge_fund.trading.data.fence import last_research_day
from hedge_fund.trading.data.markets import FX, INDICES, market
from hedge_fund.trading.data.quality import require_clean

OUT = Path(__file__).resolve().parent / "dukascopy_daily_quality.json"


def main(start: str, end: str, symbols: list[str], *, cached_only: bool = False) -> int:
    """cached_only: use the month files already cached and stop at the first missing month
    (the datafeed refuses further requests); the gap is recorded in the quality report."""
    end = min(end, last_research_day("*")[:7])
    session = requests.Session()
    session.trust_env = True
    client = dukascopy.DukascopyClient(min_interval=12.0, max_backoff=900.0, session=session)
    cat = Catalog()
    report = json.loads(OUT.read_text()) if OUT.exists() else {}
    for sym in symbols or [*FX, *INDICES]:
        spec = market(sym)
        inst = spec.instrument()
        cat.write_instrument(inst)
        frames = []
        last_cached = None
        for m in months(start, end):
            if cached_only and not dukascopy.hour_path(client.root, sym, m, "BID").exists():
                break
            last_cached = m
            raw = dukascopy.fetch_month_hours(client, sym, m, "BID", retries=40)
            frames.append(dukascopy.decode_hours(raw, m, dukascopy.POINTS[sym]))
            print(time.strftime("%H:%M:%S"), sym, m, len(frames[-1]), flush=True)
        daily = dukascopy.daily_from_hours(pd.concat(frames).sort_index())
        daily = daily[(daily.index >= pd.Timestamp(start + "-01", tz="UTC"))]
        sym_end = last_cached or end
        if sym_end < end:
            report.setdefault(sym, {})["_gap"] = {"missing_from": months(sym_end, end)[1], "to": end,
                                                  "reason": "datafeed refused requests (HTTP 429)"}
        bt = str(bar_type(spec.instrument_id, 1440))
        rows = report.setdefault(sym, {})
        for m, df in daily.groupby(daily.index.strftime("%Y-%m")):
            if m > sym_end or cat.has(f"bars:{bt}:{m}"):
                continue
            rep = require_clean(df, "1D", expected_index=df.index)      # closures are not gaps
            cat.write_bars(inst, df, m, minutes=1440, source={"source": "dukascopy", "side": "BID", "tf": "1h->1D"})
            rows[m] = {k: rep[k] for k in ("rows", "zero_volume", "extreme_moves")}
        OUT.write_text(json.dumps(report, indent=1, sort_keys=True))
        print(time.strftime("%H:%M:%S"), sym, "daily bars", len(daily), flush=True)
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--cached-only"]
    sys.exit(main(args[0], args[1], args[2:], cached_only="--cached-only" in sys.argv))
