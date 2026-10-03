"""Nautilus ParquetDataCatalog in the private cache: write bars, read them through the fence.

    write_bars   one call per (instrument, month); a ledger records what is in the
                 catalog (rows, first/last bar, source file hash) so reruns skip it
    load_bars    passes the requested range through holdout_guard.market_data_fence
                 and drops any bar inside a sealed window, then reads the catalog

Bars are close-stamped: ts_event = ts_init = open time + bar length.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers import BarDataWrangler

from hedge_fund.paths import CACHE_DIR
from hedge_fund.validation.holdout_guard import market_data_fence, sealed_windows_in_force

CATALOG_DIR = CACHE_DIR / "catalog"


def bar_type(instrument_id: str, minutes: int = 1) -> BarType:
    return BarType.from_str(f"{instrument_id}-{minutes}-MINUTE-LAST-EXTERNAL")


class Catalog:
    def __init__(self, path: Path | str = CATALOG_DIR) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.catalog = ParquetDataCatalog(str(self.path))
        self.ledger_path = self.path / "ledger.json"
        self.ledger = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {}

    def has(self, key: str) -> bool:
        return key in self.ledger

    def write_instrument(self, instrument) -> None:
        key = f"instrument:{instrument.id}"
        if key not in self.ledger:
            self.catalog.write_data([instrument])
            self._record(key, {"instrument": str(instrument.id)})

    def write_bars(self, instrument, df: pd.DataFrame, chunk: str, *, minutes: int = 1, source: dict | None = None) -> int:
        """df: open-time UTC index, columns open/high/low/close/volume. Returns bars written."""
        bt = bar_type(str(instrument.id), minutes)
        key = f"bars:{bt}:{chunk}"
        if key in self.ledger:
            return 0
        data = df[["open", "high", "low", "close", "volume"]].copy()
        data.index = data.index + pd.Timedelta(minutes=minutes)
        data.index.name = "timestamp"
        bars = BarDataWrangler(bt, instrument).process(data)
        self.catalog.write_data(bars)
        self._record(key, {"rows": len(bars), "first": str(data.index[0]), "last": str(data.index[-1]),
                           **(source or {})})
        return len(bars)

    def load_bars(self, instrument_id: str, start: str, end: str, *, minutes: int = 1) -> list[Bar]:
        """Bars whose close lies in [start, end] (ISO dates, end inclusive), fence enforced."""
        market_data_fence(start, end)
        bt = bar_type(instrument_id, minutes)
        lo = pd.Timestamp(start, tz="UTC")
        hi = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(1, "ns")
        bars = self.catalog.query(Bar, identifiers=[str(bt)], start=lo, end=hi)
        windows = [(pd.Timestamp(a, tz="UTC").value, (pd.Timestamp(b, tz="UTC") + pd.Timedelta(days=1)).value)
                   for a, b in sealed_windows_in_force()]
        return [b for b in bars if lo.value <= b.ts_event <= hi.value
                and not any(a <= b.ts_event < z for a, z in windows)]

    def instrument(self, instrument_id: str):
        found = self.catalog.instruments(instrument_ids=[instrument_id])
        if not found:
            raise KeyError(instrument_id)
        return found[0]

    def summary(self) -> dict:
        out: dict[str, dict] = {}
        for k, v in self.ledger.items():
            if k.startswith("bars:"):
                _, bt, _ = k.split(":", 2)
                s = out.setdefault(bt, {"rows": 0, "chunks": 0, "first": v["first"], "last": v["last"]})
                s["rows"] += v["rows"]
                s["chunks"] += 1
                s["first"], s["last"] = min(s["first"], v["first"]), max(s["last"], v["last"])
        return out

    def _record(self, key: str, value: dict) -> None:
        self.ledger[key] = value
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.ledger, indent=1, sort_keys=True))
        os.replace(tmp, self.ledger_path)
