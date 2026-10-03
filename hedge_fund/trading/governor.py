"""Trade-risk governor: frozen limits checked before every entry and on every bar.

    max_drawdown    equity below peak by this fraction -> kill switch (flatten, no entries)
    max_daily_loss  equity below the UTC day's opening equity -> no new entries that day
    KILL_SWITCH     a file of that name in kill_dir engages the kill switch
    caps            open positions, entries per day

Limits are a frozen pydantic model whose hash is recorded with every run. The kill
switch latches: only a new run (a human restart) clears it. AI output never edits
these limits (CLAUDE.md).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

KILL_SWITCH = "KILL_SWITCH"


class TradeRiskConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    risk_per_trade: float = Field(0.005, gt=0, le=0.02, description="equity fraction lost if the stop fills")
    max_notional_fraction: float = Field(0.9, gt=0, le=1.0,
                                         description="position notional / equity; no leverage, headroom for gaps")
    max_daily_loss: float = Field(0.03, gt=0, le=0.10)
    max_drawdown: float = Field(0.20, gt=0, le=0.50)
    max_open_positions: int = Field(1, ge=1)
    max_entries_per_day: int = Field(20, ge=1)

    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()[:16]


class RiskGovernor:
    def __init__(self, config: TradeRiskConfig, equity: float, *, kill_dir: Path | str | None = None) -> None:
        self.config = config
        self.kill_dir = Path(kill_dir) if kill_dir is not None else None
        self.peak = self.day_start = float(equity)
        self.day: str | None = None
        self.entries_today = 0
        self.killed: str | None = None
        self.events: list[dict] = []

    def update(self, ts_ns: int, equity: float) -> None:
        day = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).date().isoformat()
        if day != self.day:
            self.day, self.day_start, self.entries_today = day, equity, 0
        self.peak = max(self.peak, equity)
        if self.killed:
            return
        if self.kill_dir is not None and (self.kill_dir / KILL_SWITCH).exists():
            self.engage(ts_ns, "KILL_SWITCH file present")
        elif self.peak > 0 and equity / self.peak - 1 <= -self.config.max_drawdown:
            self.engage(ts_ns, f"drawdown {equity / self.peak - 1:.2%} breached {self.config.max_drawdown:.0%}")

    def engage(self, ts_ns: int, reason: str) -> None:
        if not self.killed:
            self.killed = reason
            self.events.append({"ts": ts_ns, "event": "kill_switch", "reason": reason})

    def can_enter(self, equity: float, open_positions: int) -> tuple[bool, str | None]:
        c = self.config
        if self.killed:
            return False, f"kill switch: {self.killed}"
        if self.day_start > 0 and equity / self.day_start - 1 <= -c.max_daily_loss:
            return False, "daily loss limit"
        if open_positions >= c.max_open_positions:
            return False, "max open positions"
        if self.entries_today >= c.max_entries_per_day:
            return False, "max entries per day"
        return True, None

    def record_entry(self) -> None:
        self.entries_today += 1
