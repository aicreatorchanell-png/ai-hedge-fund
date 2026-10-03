"""Bar data quality checks. Problems are reported, never silently repaired.

    hard failures   duplicate timestamps, OHLC inconsistency (high below open/close, low
                    above them), non-positive prices, unsorted index
    reported        missing bars (gaps) and the longest gap, zero-volume bars, extreme
                    one-bar moves (|log return| above a threshold)

Missing bars stay missing: no forward fill, so a strategy never trades on a price
that did not print.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class DataQualityError(ValueError):
    pass


def quality_report(df: pd.DataFrame, freq: str = "1min", *, extreme: float = 0.15,
                   expected_index: pd.DatetimeIndex | None = None) -> dict:
    idx = df.index
    o, h, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    bad_ohlc = int(((h < np.maximum(o, c) - 1e-12) | (lo > np.minimum(o, c) + 1e-12) | (h < lo)).sum())
    nonpos = int(((df[["open", "high", "low", "close"]] <= 0).any(axis=1)).sum())
    dups = int(idx.duplicated().sum())
    full = expected_index if expected_index is not None else pd.date_range(idx.min(), idx.max(), freq=freq)
    missing = full.difference(idx)
    step = pd.Timedelta(freq)
    gaps = (idx.to_series().diff().dropna() / step - 1)
    lr = np.abs(np.diff(np.log(c))) if len(c) > 1 else np.array([])
    rep = {
        "rows": int(len(df)), "expected": int(len(full)), "missing": int(len(missing)),
        "missing_share": float(len(missing) / len(full)) if len(full) else 0.0,
        "longest_gap_bars": int(gaps.max()) if len(gaps) else 0,
        "duplicates": dups, "bad_ohlc": bad_ohlc, "non_positive": nonpos,
        "sorted": bool(idx.is_monotonic_increasing),
        "zero_volume": int((df["volume"] <= 0).sum()) if "volume" in df else 0,
        "extreme_moves": int((lr > extreme).sum()),
        "start": str(idx.min()), "end": str(idx.max()),
    }
    rep["ok"] = rep["duplicates"] == 0 and rep["bad_ohlc"] == 0 and rep["non_positive"] == 0 and rep["sorted"]
    return rep


def require_clean(df: pd.DataFrame, freq: str = "1min", **kw) -> dict:
    rep = quality_report(df, freq, **kw)
    if not rep["ok"]:
        raise DataQualityError(f"bar data failed hard checks: {rep}")
    return rep
