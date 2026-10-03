"""Load Binance spot 1-minute bars for the fixed crypto research set into the catalog.

    python runs/active/data/load_crypto.py [SYMBOL ...]

Months 2018-01 .. 2026-08 (the day before the sealed holdout's fence, 2026-09-01).
Each archive is checksum-verified, parsed, quality-checked and written to the Nautilus
catalog in the private cache. Resumable: verified archives and catalog chunks are
skipped. The repo gets only crypto_quality.json (counts, no prices).
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

from hedge_fund.trading.data import binance
from hedge_fund.trading.data.catalog import Catalog
from hedge_fund.trading.data.fence import last_research_day
from hedge_fund.trading.data.markets import CRYPTO
from hedge_fund.trading.data.quality import require_clean

OUT = Path(__file__).resolve().parent / "crypto_quality.json"
START = "2018-01"


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def main() -> int:
    symbols = sys.argv[1:] or list(CRYPTO)
    end = last_research_day()[:7]
    months = [m for m in binance.months(START, end) if binance.month_allowed(m)]
    cat = Catalog()
    report = json.loads(OUT.read_text()) if OUT.exists() else {}
    session = requests.Session()
    for sym in symbols:
        spec = CRYPTO[sym]
        inst = spec.instrument()
        cat.write_instrument(inst)
        with ThreadPoolExecutor(4) as ex:
            paths = dict(zip(months, ex.map(lambda m: binance.download_month(sym, m, session=session), months)))
        rows = report.setdefault(sym, {})
        for m, path in paths.items():
            if path is None:
                rows[m] = {"status": "not_listed"}
                continue
            df = binance.parse(path)
            per = pd.Period(m, freq="M")
            expected = pd.date_range(per.start_time, per.end_time.floor("min"), freq="1min", tz="UTC")
            if m == months[0] or rows.get(m, {}).get("status") != "ok" or not cat.has(f"bars:{sym}.BINANCE-1-MINUTE-LAST-EXTERNAL:{m}"):
                first_listed = all(r.get("status") == "not_listed" for k, r in rows.items() if k < m)
                rep = require_clean(df, expected_index=expected[expected >= df.index[0]] if first_listed else expected)
                cat.write_bars(inst, df, m, source={"file": path.name})
                rows[m] = {"status": "ok", **{k: rep[k] for k in ("rows", "expected", "missing", "longest_gap_bars",
                                                                   "zero_volume", "extreme_moves")}}
        OUT.write_text(json.dumps(report, indent=1, sort_keys=True))
        ok = [r for r in rows.values() if r.get("status") == "ok"]
        log(f"{sym}: {len(ok)} months, {sum(r['rows'] for r in ok):,} bars, "
            f"missing {sum(r['missing'] for r in ok):,}, extreme moves {sum(r['extreme_moves'] for r in ok)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
