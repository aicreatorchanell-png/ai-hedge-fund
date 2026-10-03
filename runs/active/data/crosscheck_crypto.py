"""Cross-check catalog bars against another exchange through CCXT (public data, no keys).

For each crypto symbol, eight seeded random 5-hour windows (one per year 2018-2025)
are fetched from a second exchange and compared with the catalog (median absolute
close difference in bps, correlation of 1-minute returns). Writes
crosscheck_crypto.json (statistics only, no prices).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd

from hedge_fund.trading.data.catalog import Catalog
from hedge_fund.trading.data.ccxt_check import compare, fetch_ohlcv
from hedge_fund.trading.data.markets import CRYPTO

OUT = Path(__file__).resolve().parent / "crosscheck_crypto.json"
EXCHANGES = ["okx", "kraken", "coinbase"]


def main() -> int:
    cat = Catalog()
    rng = np.random.default_rng(20261003)
    results = {}
    for sym, spec in CRYPTO.items():
        pair = f"{spec.base}/{spec.quote}"
        rows = []
        for year in range(2018, 2026):
            day = pd.Timestamp(f"{year}-01-01", tz="UTC") + pd.Timedelta(days=int(rng.integers(0, 330)))
            start = day + pd.Timedelta(hours=int(rng.integers(0, 19)))
            bars = cat.load_bars(spec.instrument_id, start.date().isoformat(), start.date().isoformat())
            ours = pd.DataFrame({"close": [float(b.close) for b in bars]},
                                index=pd.to_datetime([b.ts_event - 60_000_000_000 for b in bars], utc=True))
            ours = ours[(ours.index >= start) & (ours.index < start + pd.Timedelta(hours=5))]
            row = {"window_start": str(start), "ours": len(ours)}
            for name in EXCHANGES:
                ex = getattr(ccxt, name)({"enableRateLimit": True})
                ex.session.trust_env = True                   # honour the environment's proxy and CA bundle
                try:
                    p = pair if name != "coinbase" else f"{spec.base}/USD"
                    theirs = fetch_ohlcv(ex, p, start, 300)
                except Exception as exc:                      # unlisted pair or no history that far back
                    row[name] = {"error": type(exc).__name__}
                    continue
                if len(theirs) >= 30:
                    row[name] = compare(ours, theirs)
                    break
                row[name] = {"overlap": len(theirs)}
            rows.append(row)
        checked = [v for r in rows for k, v in r.items() if k in EXCHANGES and isinstance(v, dict) and "median_abs_diff_bps" in v]
        results[sym] = {"windows": rows, "checked": len(checked), "ok": sum(c["ok"] for c in checked),
                        "median_diff_bps": float(np.median([c["median_abs_diff_bps"] for c in checked]))
                        if checked else None,
                        "median_return_corr": float(np.median([c["return_corr"] for c in checked])) if checked else None}
        print(sym, {k: v for k, v in results[sym].items() if k != "windows"}, flush=True)
    OUT.write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
