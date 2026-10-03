"""Dukascopy historical 1-minute candles (FX, index CFDs, commodities).

    https://datafeed.dukascopy.com/datafeed/{SYM}/{YYYY}/{MM-1:02}/{DD}/{BID|ASK}_candles_min_1.bi5

One LZMA-compressed file per day and side; each record is 24 bytes, big-endian:
seconds from midnight UTC (int32), open, close, low, high (int32, in points), volume
(float32). Prices are points / `POINTS[symbol]`. An empty file is a closed day.

The datafeed rate-limits aggressively (HTTP 429/503). Requests are serialized with a
minimum interval and exponential backoff; every day file is cached, so a load can stop
and resume. Both BID and ASK are fetched: mid = (bid + ask) / 2 is the traded price
series and ask - bid gives the measured spread for the cost model.
"""

from __future__ import annotations

import lzma
import os
import struct
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from hedge_fund.paths import CACHE_DIR
from hedge_fund.trading.data.fence import last_research_day

BASE = "https://datafeed.dukascopy.com/datafeed"
RAW_DIR = CACHE_DIR / "market" / "dukascopy"
POINTS = {"EURUSD": 1e5, "GBPUSD": 1e5, "AUDUSD": 1e5, "USDJPY": 1e3, "USA500IDXUSD": 1e3, "DEUIDXEUR": 1e3}
RECORD = struct.Struct(">5if")


class RateLimited(RuntimeError):
    pass


def decode(raw: bytes, day: date, point: float) -> pd.DataFrame:
    """bi5 bytes -> DataFrame indexed by open time with open/high/low/close/volume."""
    cols = ["open", "high", "low", "close", "volume"]
    if not raw:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC", name="open_time"), dtype=float)
    data = lzma.decompress(raw)
    if len(data) % RECORD.size:
        raise ValueError("truncated bi5 file")
    rec = np.array(list(RECORD.iter_unpack(data)), dtype=float)
    t0 = pd.Timestamp(day, tz="UTC")
    df = pd.DataFrame({"open": rec[:, 1] / point, "high": rec[:, 4] / point, "low": rec[:, 3] / point,
                       "close": rec[:, 2] / point, "volume": rec[:, 5]},
                      index=pd.DatetimeIndex(t0 + pd.to_timedelta(rec[:, 0], unit="s"), name="open_time"))
    return df


def encode(df: pd.DataFrame, day: date, point: float) -> bytes:
    """Inverse of decode (used by tests)."""
    t0 = pd.Timestamp(day, tz="UTC")
    out = b"".join(RECORD.pack(int((t - t0).total_seconds()), round(r.open * point), round(r.close * point),
                               round(r.low * point), round(r.high * point), float(r.volume))
                   for t, r in df.iterrows())
    return lzma.compress(out)


class DukascopyClient:
    def __init__(self, root: Path = RAW_DIR, *, min_interval: float = 2.0, max_backoff: float = 600.0,
                 session=None) -> None:
        self.root = Path(root)
        self.min_interval = min_interval
        self.max_backoff = max_backoff
        self.session = session or requests.Session()
        self._last = 0.0

    def path(self, symbol: str, day: date, side: str) -> Path:
        return self.root / symbol / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}_{side}.bi5"

    def fetch_day(self, symbol: str, day: date, side: str = "BID", *, retries: int = 8) -> bytes:
        from hedge_fund.trading.data.markets import market
        if day.isoformat() > last_research_day(market(symbol).asset_class):
            raise PermissionError(f"{day} is inside a sealed holdout window")
        p = self.path(symbol, day, side)
        if p.exists():
            return p.read_bytes()
        url = f"{BASE}/{symbol}/{day.year}/{day.month - 1:02d}/{day.day:02d}/{side}_candles_min_1.bi5"
        wait = 30.0
        for _ in range(retries):
            time.sleep(max(0.0, self._last + self.min_interval - time.monotonic()))
            self._last = time.monotonic()
            try:
                r = self.session.get(url, timeout=60)
            except requests.RequestException:                 # dropped connection: treat like a rate limit
                time.sleep(wait)
                wait = min(wait * 2, self.max_backoff)
                continue
            if r.status_code == 200:
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".tmp")
                tmp.write_bytes(r.content)
                os.replace(tmp, p)
                return r.content
            if r.status_code == 404:
                return b""
            if r.status_code in (429, 503):
                time.sleep(wait)
                wait = min(wait * 2, self.max_backoff)
                continue
            r.raise_for_status()
        raise RateLimited(f"{symbol} {day} {side}: still rate-limited after {retries} attempts")

    def day_mid(self, symbol: str, day: date) -> pd.DataFrame:
        """Mid-price candles plus the bar's close spread (ask - bid) in price units."""
        pt = POINTS[symbol]
        bid = decode(self.fetch_day(symbol, day, "BID"), day, pt)
        ask = decode(self.fetch_day(symbol, day, "ASK"), day, pt)
        both = bid.join(ask, lsuffix="_b", rsuffix="_a", how="inner")
        out = pd.DataFrame({k: (both[f"{k}_b"] + both[f"{k}_a"]) / 2 for k in ("open", "high", "low", "close")},
                           index=both.index)
        out["volume"] = both["volume_b"]
        out["spread"] = both["close_a"] - both["close_b"]
        return out[out["volume"] > 0]                      # Dukascopy pads closed minutes with flat zero-volume bars
