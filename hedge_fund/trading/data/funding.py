"""Binance USD-M perpetual funding rates from the public bulk archive (data.binance.vision).

    https://data.binance.vision/data/futures/um/monthly/fundingRate/{SYM}/{SYM}-fundingRate-{YYYY-MM}.zip

CSV (with header): calc_time (ms), funding_interval_hours, last_funding_rate.
A rate is settled and public at calc_time, so a decision at time t may use only rows with
calc_time <= t (`known_at`). Months reaching into a sealed crypto holdout are never fetched.
Raw archives stay in the private cache, never in git.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pandas as pd

from hedge_fund.paths import CACHE_DIR
from hedge_fund.trading.data.binance import fetch_verified, month_allowed

BASE = "https://data.binance.vision/data/futures/um/monthly/fundingRate"
RAW_DIR = CACHE_DIR / "market" / "binance" / "futures_um" / "fundingRate"


def archive_path(symbol: str, month: str, root: Path = RAW_DIR) -> Path:
    return root / symbol / f"{symbol}-fundingRate-{month}.zip"


def download_month(symbol: str, month: str, *, root: Path = RAW_DIR, session=None, retries: int = 4) -> Path | None:
    """Download and verify one month of funding settlements; None if Binance has no file."""
    if not month_allowed(month):
        raise PermissionError(f"{month} reaches into a sealed holdout window")
    dest = archive_path(symbol, month, root)
    return fetch_verified(f"{BASE}/{symbol}/{dest.name}", dest, session=session, retries=retries)


def parse(path: Path) -> pd.DataFrame:
    """Archive -> DataFrame indexed by settlement time (UTC) with `rate` and `interval_hours`."""
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw))
    t = df["calc_time"].astype("int64")
    unit = "us" if t.iloc[0] > 10**14 else "ms"
    out = pd.DataFrame({"rate": df["last_funding_rate"].astype(float),
                        "interval_hours": df["funding_interval_hours"].astype(int)})
    out.index = pd.to_datetime(t, unit=unit, utc=True)
    out.index.name = "calc_time"
    return out.sort_index()


def load(symbol: str, months: list[str], *, root: Path = RAW_DIR) -> pd.DataFrame:
    """Concatenate cached months (no download). Missing months are skipped."""
    parts = [parse(p) for m in months if (p := archive_path(symbol, m, root)).exists()]
    if not parts:
        return pd.DataFrame(columns=["rate", "interval_hours"])
    df = pd.concat(parts)
    return df[~df.index.duplicated(keep="last")].sort_index()


def known_at(rates: pd.DataFrame, ts) -> pd.DataFrame:
    """Point-in-time view: only settlements published at or before *ts*."""
    return rates.loc[rates.index <= pd.Timestamp(ts)]
