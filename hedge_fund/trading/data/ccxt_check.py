"""Independent cross-check of loaded bars against another exchange (via CCXT).

For a set of sample windows, fetch 1-minute OHLCV for the same pair from a second
exchange and compare with the catalog: median absolute close difference (bps) and the
correlation of 1-minute log returns. Prices on two venues differ by small basis and
microstructure noise; a symbol mapped to the wrong market, a scaling error, or a
shifted timestamp shows up as a large difference or a low correlation.

Read-only public market data; no API keys, no orders.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def fetch_ohlcv(exchange, pair: str, start: pd.Timestamp, minutes: int) -> pd.DataFrame:
    rows, since = [], int(start.value // 10**6)
    end = since + minutes * 60_000
    while since < end:
        batch = exchange.fetch_ohlcv(pair, "1m", since=since, limit=min(300, (end - since) // 60_000))
        if not batch:
            break
        rows += [b for b in batch if b[0] < end]
        since = batch[-1][0] + 60_000
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"]).drop_duplicates("t")
    df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
    return df.drop(columns="t")


def compare(ours: pd.DataFrame, theirs: pd.DataFrame) -> dict:
    j = ours[["close"]].join(theirs[["close"]], how="inner", lsuffix="_ours", rsuffix="_theirs")
    if len(j) < 30:
        return {"overlap": len(j), "ok": False}
    diff_bps = (np.abs(j["close_ours"] / j["close_theirs"] - 1) * 1e4).median()
    r1, r2 = np.diff(np.log(j["close_ours"])), np.diff(np.log(j["close_theirs"]))
    corr = float(np.corrcoef(r1, r2)[0, 1]) if r1.std() > 0 and r2.std() > 0 else float("nan")
    return {"overlap": len(j), "median_abs_diff_bps": float(diff_bps), "return_corr": corr,
            "ok": bool(diff_bps < 50 and corr > 0.5)}
