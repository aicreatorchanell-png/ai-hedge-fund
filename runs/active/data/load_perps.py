"""Load Binance USD-M perpetual bars and funding settlements into the private cache/catalog.

    python runs/active/data/load_perps.py 1m BTCUSDT ETHUSDT ...     # 1-minute execution bars
    python runs/active/data/load_perps.py 1d BCHUSDT EOSUSDT ...     # daily bars

Months 2020-01 .. the last month before the sealed crypto holdout (fence). Archives are
checksum-verified, parsed, quality-checked and written to the Nautilus catalog; funding
settlements for the same months are fetched too. Resumable. The repo gets only
perps_quality.json (counts, no prices).
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

from hedge_fund.trading.data import binance, funding
from hedge_fund.trading.data.catalog import Catalog, bar_type
from hedge_fund.trading.data.fence import last_research_day
from hedge_fund.trading.data.markets import PERPS
from hedge_fund.trading.data.quality import require_clean

OUT = Path(__file__).resolve().parent / "perps_quality.json"
START = "2020-01"
MINUTES = {"1m": 1, "1d": 1440}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def main(tf: str, symbols: list[str]) -> int:
    minutes = MINUTES[tf]
    end = last_research_day("crypto")[:7]
    months = [m for m in binance.months(START, end) if binance.month_allowed(m)]
    cat = Catalog()
    report = json.loads(OUT.read_text()) if OUT.exists() else {}
    session = requests.Session()
    session.trust_env = True
    for sym in symbols:
        spec = PERPS[f"{sym}-PERP"]
        inst = spec.instrument()
        cat.write_instrument(inst)
        with ThreadPoolExecutor(4) as ex:
            paths = dict(zip(months, ex.map(lambda m: binance.download_futures_month(sym, m, tf, session=session),
                                            months)))
            fpaths = list(ex.map(lambda m: funding.download_month(sym, m, session=session), months))
        rows = report.setdefault(f"{sym}:{tf}", {})
        bt = str(bar_type(spec.instrument_id, minutes))
        listed = False
        for m, path in paths.items():
            if path is None:
                rows[m] = {"status": "not_listed" if not listed else "missing"}
                continue
            df = binance.parse(path)
            per = pd.Period(m, freq="M")
            freq = "1min" if minutes == 1 else "1D"
            expected = pd.date_range(per.start_time, per.end_time.floor(freq), freq=freq, tz="UTC")
            if not listed:
                expected = expected[expected >= df.index[0]]
            if not cat.has(f"bars:{bt}:{m}"):
                rep = require_clean(df, freq, expected_index=expected)
                cat.write_bars(inst, df, m, minutes=minutes, source={"file": path.name})
                rows[m] = {"status": "ok", **{k: rep[k] for k in ("rows", "expected", "missing", "longest_gap_bars",
                                                                   "zero_volume", "extreme_moves")}}
            listed = True
        OUT.write_text(json.dumps(report, indent=1, sort_keys=True))
        ok = [r for r in rows.values() if r.get("status") == "ok"]
        fr = funding.load(sym, months)
        log(f"{sym} {tf}: {len(ok)} months, {sum(r['rows'] for r in ok):,} bars, missing "
            f"{sum(r['missing'] for r in ok):,}; funding {sum(p is not None for p in fpaths)} months, {len(fr)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
