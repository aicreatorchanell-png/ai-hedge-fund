"""Load Binance spot DAILY bars (1d archives) for the crypto research set into the catalog.

    python runs/active/data/load_spot_daily.py [SYMBOL ...]

Months 2020-01 .. the last month before the sealed crypto holdout. Checksum-verified,
quality-checked, written as 1-DAY bars. Used by multi-leg research (spot leg of carry).
"""

from __future__ import annotations

import sys

import requests

from hedge_fund.trading.data import binance
from hedge_fund.trading.data.catalog import Catalog, bar_type
from hedge_fund.trading.data.fence import last_research_day
from hedge_fund.trading.data.markets import CRYPTO
from hedge_fund.trading.data.quality import require_clean


def main(symbols: list[str]) -> int:
    months = [m for m in binance.months("2020-01", last_research_day("crypto")[:7]) if binance.month_allowed(m)]
    s = requests.Session()
    s.trust_env = True
    cat = Catalog()
    for sym in symbols or list(CRYPTO):
        spec = CRYPTO[sym]
        inst = spec.instrument()
        cat.write_instrument(inst)
        bt = str(bar_type(spec.instrument_id, 1440))
        n = 0
        for m in months:
            p = binance.download_month(sym, m, "1d", session=s)
            if p is None or cat.has(f"bars:{bt}:{m}"):
                continue
            df = binance.parse(p)
            require_clean(df, "1D", expected_index=df.index)
            n += cat.write_bars(inst, df, m, minutes=1440, source={"file": p.name})
        print(sym, "daily bars written", n, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
