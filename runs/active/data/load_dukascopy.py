"""Load Dukascopy 1-minute mid-price bars (FX, index CFDs) into the catalog. Resumable.

    python runs/active/data/load_dukascopy.py SYMBOL START_MONTH END_MONTH

BID and ASK candles are fetched per day (cached), combined into mid-price bars and
written per month. The measured close spread (median, 95th percentile, in bps) is
recorded per month in dukascopy_quality.json so the cost model can be checked against
it. The datafeed rate-limits hard: requests are serialized with backoff.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

from hedge_fund.trading.data import dukascopy
from hedge_fund.trading.data.binance import month_allowed, months
from hedge_fund.trading.data.catalog import Catalog
from hedge_fund.trading.data.markets import market
from hedge_fund.trading.data.quality import require_clean

OUT = Path(__file__).resolve().parent / "dukascopy_quality.json"


def main(symbol: str, start: str, end: str) -> int:
    spec = market(symbol)
    inst = spec.instrument()
    cat = Catalog()
    cat.write_instrument(inst)
    client = dukascopy.DukascopyClient(min_interval=3.0)
    report = json.loads(OUT.read_text()) if OUT.exists() else {}
    rows = report.setdefault(symbol, {})
    for m in months(start, end):
        if not month_allowed(m) or cat.has(f"bars:{spec.instrument_id}-1-MINUTE-LAST-EXTERNAL:{m}"):
            continue
        per = pd.Period(m, freq="M")
        days = [d.date() for d in pd.date_range(per.start_time, per.end_time, freq="D")]
        frames = [client.day_mid(symbol, d) for d in days]
        df = pd.concat([f for f in frames if not f.empty]).sort_index()
        rep = require_clean(df, expected_index=df.index)          # market hours: gaps are closures
        spread_bps = (df["spread"] / df["close"] * 1e4)
        cat.write_bars(inst, df.drop(columns="spread"), m, source={"source": "dukascopy", "sides": "mid"})
        rows[m] = {"rows": rep["rows"], "spread_bps_median": round(float(spread_bps.median()), 3),
                   "spread_bps_p95": round(float(spread_bps.quantile(0.95)), 3), "extreme_moves": rep["extreme_moves"]}
        OUT.write_text(json.dumps(report, indent=1, sort_keys=True))
        print(time.strftime("%H:%M:%S"), symbol, m, rows[m], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
