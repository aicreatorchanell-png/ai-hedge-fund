"""Hard request budget for a metered data vendor (Tiingo), persisted across restarts.

Every HTTP attempt is appended to a JSONL ledger (time, vendor month, symbol, bytes,
status) before the next one may start, so a crashed or restarted download resumes
with the same counts. `before(symbol)` refuses the attempt (`BudgetExhausted`) if it
would exceed:

    unique symbols this month   (Tiingo counts each distinct ticker requested)
    bytes this month            (monthly bandwidth)
    requests today              (daily request limit)

and sleeps as needed to stay under the hourly request rate. Months and days follow
US Eastern time (Tiingo resets daily and monthly limits at midnight EST). The caps
are set below the plan's hard limits by the caller (headroom).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


class BudgetExhausted(RuntimeError):
    """A further request would exceed a pre-set vendor limit. Everything so far is kept."""


@dataclass(frozen=True)
class Limits:
    unique_symbols_per_month: int
    bytes_per_month: int
    requests_per_day: int
    requests_per_hour: int


class RequestBudget:
    def __init__(self, ledger: Path | str, limits: Limits, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.ledger = Path(ledger)
        self.limits = limits
        self._clock, self._sleep = clock, sleep

    # -- ledger ---------------------------------------------------------------

    def entries(self) -> list[dict]:
        if not self.ledger.exists():
            return []
        return [json.loads(x) for x in self.ledger.read_text().splitlines() if x.strip()]

    def record(self, symbol: str | None, nbytes: int, status: int | str, *, ts: float | None = None,
               note: str = "") -> None:
        ts = self._clock() if ts is None else ts
        local = datetime.fromtimestamp(ts, EASTERN)
        row = {"ts": datetime.fromtimestamp(ts, timezone.utc).isoformat(), "epoch": ts,
               "month": local.strftime("%Y-%m"), "day": local.strftime("%Y-%m-%d"),
               "symbol": symbol, "bytes": int(nbytes), "status": status, "note": note}
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger, "a") as fh:
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    # -- usage ----------------------------------------------------------------

    def usage(self) -> dict:
        now = datetime.fromtimestamp(self._clock(), EASTERN)
        month, day = now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
        rows = self.entries()
        m = [r for r in rows if r["month"] == month]
        return {"month": month, "unique_symbols": len({r["symbol"] for r in m if r["symbol"]}),
                "symbols": sorted({r["symbol"] for r in m if r["symbol"]}),
                "bytes": sum(r["bytes"] for r in m), "requests_today": sum(1 for r in rows if r["day"] == day),
                "requests_month": len(m)}

    # -- gate -------------------------------------------------------------------

    def before(self, symbol: str | None) -> None:
        lim, u = self.limits, self.usage()
        if symbol and symbol not in u["symbols"] and u["unique_symbols"] >= lim.unique_symbols_per_month:
            raise BudgetExhausted(f"{symbol}: unique-symbol cap {lim.unique_symbols_per_month} reached for {u['month']}")
        if u["bytes"] >= lim.bytes_per_month:
            raise BudgetExhausted(f"monthly bandwidth cap {lim.bytes_per_month:,} bytes reached ({u['bytes']:,})")
        if u["requests_today"] >= lim.requests_per_day:
            raise BudgetExhausted(f"daily request cap {lim.requests_per_day} reached")
        while True:                                            # rolling hour
            now = self._clock()
            recent = sorted(r["epoch"] for r in self.entries() if now - r["epoch"] < 3600)
            if len(recent) < lim.requests_per_hour:
                return
            self._sleep(max(1.0, recent[0] + 3600 - now + 1))
