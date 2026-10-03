"""Binance spot klines from the public bulk archive (data.binance.vision).

    https://data.binance.vision/data/spot/monthly/klines/{SYM}/{tf}/{SYM}-{tf}-{YYYY-MM}.zip
    plus a .CHECKSUM file (sha256) for every archive; both are verified before use.

CSV columns: open_time, open, high, low, close, volume, close_time, quote_volume,
trades, taker_buy_base, taker_buy_quote, ignore. Timestamps are milliseconds until
2024-12 and microseconds from 2025-01; parse() normalizes both to nanoseconds.
Months that reach into a sealed holdout window are never downloaded.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

from hedge_fund.paths import CACHE_DIR
from hedge_fund.trading.data.fence import last_research_day

BASE = "https://data.binance.vision/data/spot/monthly/klines"
RAW_DIR = CACHE_DIR / "market" / "binance" / "spot"
COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades",
           "taker_buy_base", "taker_buy_quote", "ignore"]


class DownloadError(RuntimeError):
    pass


def months(start: str, end: str) -> list[str]:
    """YYYY-MM months from start to end inclusive."""
    return [p.strftime("%Y-%m") for p in pd.period_range(start[:7], end[:7], freq="M")]


def month_allowed(month: str, last_day: str | None = None) -> bool:
    """A month may be downloaded only if it ends on or before the last research day."""
    last_day = last_day or last_research_day("crypto")
    end = (pd.Period(month, freq="M").end_time.date()).isoformat()
    return end <= last_day


def archive_path(symbol: str, month: str, tf: str = "1m", root: Path = RAW_DIR) -> Path:
    return root / symbol / tf / f"{symbol}-{tf}-{month}.zip"


def download_month(symbol: str, month: str, tf: str = "1m", *, root: Path = RAW_DIR, session=None,
                   retries: int = 4) -> Path | None:
    """Download and verify one monthly archive. Returns None if Binance has no file
    (symbol not listed that month). Idempotent: a verified file is not fetched again."""
    if not month_allowed(month):
        raise PermissionError(f"{month} reaches into a sealed holdout window")
    dest = archive_path(symbol, month, tf, root)
    sums = dest.with_suffix(".zip.CHECKSUM")
    if dest.exists() and sums.exists() and _verify(dest, sums):
        return dest
    url = f"{BASE}/{symbol}/{tf}/{dest.name}"
    s = session or requests.Session()
    for attempt in range(retries):
        try:
            r = s.get(url + ".CHECKSUM", timeout=60)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            z = s.get(url, timeout=300)
            z.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(sums, r.content)
            _atomic_write(dest, z.content)
            if not _verify(dest, sums):
                raise DownloadError(f"checksum mismatch for {dest.name}")
            return dest
        except (requests.RequestException, DownloadError):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** (attempt + 1))
    return None


def parse(path: Path) -> pd.DataFrame:
    """Archive -> DataFrame indexed by open time (UTC, ns) with open/high/low/close/volume/trades."""
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None, names=COLUMNS)
    if not str(df.iloc[0, 0]).isdigit():                  # some archives carry a header row
        df = df.iloc[1:]
    t = df["open_time"].astype("int64")
    unit = "us" if t.iloc[0] > 10**14 else "ms"
    out = df[["open", "high", "low", "close", "volume", "trades"]].astype(float)
    out.index = pd.to_datetime(t, unit=unit, utc=True)
    out.index.name = "open_time"
    return out


def _verify(path: Path, sums: Path) -> bool:
    expected = sums.read_text().split()[0].strip().lower()
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest() == expected


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
