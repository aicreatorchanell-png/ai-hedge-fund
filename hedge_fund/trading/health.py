"""Strategy health monitor for paper and live trading: compare behaviour with the
validated backtest and stop new entries when it breaks down.

    HealthThresholds      frozen, versioned, hashed; loaded from configs/health-thresholds.yaml.
                          Thresholds are fixed BEFORE deployment; changing them is the
                          human-only protected action "change_health_thresholds".
    BacktestExpectations  what validation promised: Sharpe, max drawdown, win rate, payoff,
                          trade rate, slippage and costs per trade.
    HealthMonitor         rolling metrics from closed trades and daily equity -> state

States (worst condition wins):

    HEALTHY    behaviour consistent with expectations
    WARNING    one or more metrics drifting (Sharpe, win rate, payoff, trade rate, costs,
               slippage, moderate drawdown, loss streak)
    DEGRADED   clear deterioration (negative rolling Sharpe, larger drawdown, inactivity,
               abnormal single loss)
    HALT       drawdown beyond the halt limits, a long loss streak, or extreme slippage.
               HALT latches: `allows_entries()` is False until a human resets it
               (`reset(actor)` requires the protected action "reset_strategy_halt").
               GuardedStrategy refuses every new entry while the monitor is in HALT;
               exits, stops and the kill switch keep working.

The monitor is deterministic Python. Nothing here calls or accepts input from an LLM.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "configs" / "health-thresholds.yaml"


class Health(IntEnum):
    HEALTHY = 0
    WARNING = 1
    DEGRADED = 2
    HALT = 3


class HealthThresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    min_trades: int = Field(20, ge=5)
    window_trades: int = Field(50, ge=10)
    sharpe_warning_ratio: float = 0.5
    sharpe_degraded: float = 0.0
    drawdown_warning_mult: float = Field(1.0, gt=0)
    drawdown_degraded_mult: float = Field(1.5, gt=0)
    drawdown_halt_mult: float = Field(2.0, gt=0)
    drawdown_halt_cap: float = Field(0.25, gt=0, le=0.5)
    loss_streak_warning: int = Field(6, ge=2)
    loss_streak_halt: int = Field(10, ge=3)
    win_rate_drop: float = Field(0.15, gt=0)
    payoff_ratio_drop: float = Field(0.5, gt=0, le=1)
    trade_rate_low: float = Field(0.25, gt=0)
    trade_rate_high: float = Field(4.0, gt=1)
    slippage_warning_mult: float = Field(2.0, gt=1)
    slippage_halt_mult: float = Field(4.0, gt=1)
    cost_warning_mult: float = Field(1.5, gt=1)
    inactivity_mult: float = Field(3.0, gt=1)
    abnormal_loss_mult: float = Field(5.0, gt=1)

    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()[:16]


def load_thresholds(path: Path | str = DEFAULT_PATH) -> HealthThresholds:
    return HealthThresholds(**yaml.safe_load(Path(path).read_text()))


class BacktestExpectations(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sharpe_annual: float
    max_drawdown: float = Field(gt=0, le=1)
    win_rate: float = Field(ge=0, le=1)
    avg_win: float = Field(gt=0)
    avg_loss: float = Field(gt=0, description="positive number: mean loss size")
    trades_per_day: float = Field(gt=0)
    slippage_bps: float = Field(ge=0)
    cost_per_trade: float = Field(ge=0)
    trades_per_year: float = Field(gt=0)


@dataclass
class TradeRecord:
    ts: int
    pnl: float
    expected_cost: float = 0.0
    actual_cost: float = 0.0
    slippage_bps: float = 0.0


@dataclass
class HealthReport:
    state: Health
    reasons: list[str]
    metrics: dict

    @property
    def name(self) -> str:
        return self.state.name


@dataclass
class HealthMonitor:
    thresholds: HealthThresholds
    expected: BacktestExpectations
    trades: list[TradeRecord] = field(default_factory=list)
    equity: list[tuple[int, float]] = field(default_factory=list)
    latched: str | None = None
    history: list[dict] = field(default_factory=list)

    # -- inputs -------------------------------------------------------------

    def record_trade(self, ts: int, pnl: float, *, expected_cost: float = 0.0, actual_cost: float = 0.0,
                     slippage_bps: float = 0.0) -> HealthReport:
        self.trades.append(TradeRecord(ts, float(pnl), expected_cost, actual_cost, slippage_bps))
        return self.evaluate(ts)

    def record_equity(self, ts: int, equity: float) -> HealthReport:
        self.equity.append((ts, float(equity)))
        return self.evaluate(ts)

    # -- state --------------------------------------------------------------

    def allows_entries(self) -> bool:
        return self.latched is None

    @property
    def state(self) -> Health:
        return self.history[-1]["state"] if self.history else Health.HEALTHY

    def reset(self, actor: str, reason: str) -> None:
        """Clear a HALT. Human only (protected action reset_strategy_halt)."""
        from hedge_fund.research.pipeline import authorize

        authorize(actor, "reset_strategy_halt")
        self.history.append({"ts": None, "state": Health.HEALTHY, "reasons": [f"HALT cleared by {actor}: {reason}"]})
        self.latched = None

    def evaluate(self, now: int) -> HealthReport:
        t, e = self.thresholds, self.expected
        reasons: list[tuple[Health, str]] = []
        m: dict = {"trades": len(self.trades), "thresholds_version": t.version, "thresholds_hash": t.config_hash()}

        # drawdown from equity
        if self.equity:
            eq = np.array([v for _, v in self.equity])
            dd = float(1 - eq[-1] / eq.max())
            m["drawdown"] = dd
            if dd >= t.drawdown_halt_cap or dd >= t.drawdown_halt_mult * e.max_drawdown:
                reasons.append((Health.HALT, f"drawdown {dd:.1%} beyond halt limit"))
            elif dd >= t.drawdown_degraded_mult * e.max_drawdown:
                reasons.append((Health.DEGRADED, f"drawdown {dd:.1%} > {t.drawdown_degraded_mult} x expected"))
            elif dd >= t.drawdown_warning_mult * e.max_drawdown:
                reasons.append((Health.WARNING, f"drawdown {dd:.1%} > expected {e.max_drawdown:.1%}"))

        # loss streak (always judged)
        streak = 0
        for tr in reversed(self.trades):
            if tr.pnl < 0:
                streak += 1
            else:
                break
        m["loss_streak"] = streak
        if streak >= t.loss_streak_halt:
            reasons.append((Health.HALT, f"{streak} consecutive losses"))
        elif streak >= t.loss_streak_warning:
            reasons.append((Health.WARNING, f"{streak} consecutive losses"))

        # abnormal single loss
        if self.trades and self.trades[-1].pnl < -t.abnormal_loss_mult * e.avg_loss:
            reasons.append((Health.DEGRADED, f"abnormal loss {self.trades[-1].pnl:.2f}"))

        # inactivity
        last = self.trades[-1].ts if self.trades else (self.equity[0][0] if self.equity else now)
        idle_days = (now - last) / 86_400e9
        m["idle_days"] = idle_days
        if idle_days > t.inactivity_mult / e.trades_per_day:
            reasons.append((Health.DEGRADED, f"no trade for {idle_days:.1f} days"))

        # rolling statistics once enough trades exist
        w = self.trades[-t.window_trades:]
        if len(w) >= t.min_trades:
            pnl = np.array([x.pnl for x in w])
            wins, losses = pnl[pnl > 0], -pnl[pnl < 0]
            win_rate = float((pnl > 0).mean())
            payoff = (wins.mean() / losses.mean()) if len(wins) and len(losses) else float("inf")
            per_trade_sr = pnl.mean() / pnl.std(ddof=1) if pnl.std(ddof=1) > 0 else 0.0
            sharpe = float(per_trade_sr * math.sqrt(e.trades_per_year))
            span_days = max((w[-1].ts - w[0].ts) / 86_400e9, 1e-9)
            rate = (len(w) - 1) / span_days if len(w) > 1 else 0.0
            slip = float(np.mean([x.slippage_bps for x in w]))
            exp_cost = float(np.mean([x.expected_cost for x in w]))
            act_cost = float(np.mean([x.actual_cost for x in w]))
            m.update({"win_rate": win_rate, "payoff": float(payoff), "rolling_sharpe": sharpe, "trades_per_day": rate,
                      "slippage_bps": slip, "cost_per_trade": act_cost})
            if sharpe < t.sharpe_degraded:
                reasons.append((Health.DEGRADED, f"rolling Sharpe {sharpe:.2f} < {t.sharpe_degraded}"))
            elif sharpe < t.sharpe_warning_ratio * e.sharpe_annual:
                reasons.append((Health.WARNING, f"rolling Sharpe {sharpe:.2f} < {t.sharpe_warning_ratio} x expected"))
            if win_rate < e.win_rate - t.win_rate_drop:
                reasons.append((Health.WARNING, f"win rate {win_rate:.0%} vs expected {e.win_rate:.0%}"))
            if payoff < t.payoff_ratio_drop * (e.avg_win / e.avg_loss):
                reasons.append((Health.WARNING, f"payoff {payoff:.2f} below {t.payoff_ratio_drop} x expected"))
            if not t.trade_rate_low * e.trades_per_day <= rate <= t.trade_rate_high * e.trades_per_day:
                reasons.append((Health.WARNING, f"trade rate {rate:.2f}/day vs expected {e.trades_per_day:.2f}"))
            if e.slippage_bps > 0 and slip >= t.slippage_halt_mult * e.slippage_bps:
                reasons.append((Health.HALT, f"slippage {slip:.1f} bp >= {t.slippage_halt_mult} x expected"))
            elif e.slippage_bps > 0 and slip >= t.slippage_warning_mult * e.slippage_bps:
                reasons.append((Health.WARNING, f"slippage {slip:.1f} bp >= {t.slippage_warning_mult} x expected"))
            ref = exp_cost if exp_cost > 0 else e.cost_per_trade
            if ref > 0 and act_cost >= t.cost_warning_mult * ref:
                reasons.append((Health.WARNING, f"costs {act_cost:.2f}/trade vs expected {ref:.2f}"))

        state = max((s for s, _ in reasons), default=Health.HEALTHY)
        if state is Health.HALT and self.latched is None:
            self.latched = "; ".join(r for s, r in reasons if s is Health.HALT)
        if self.latched is not None:
            state = Health.HALT
        rep = HealthReport(state, [r for _, r in sorted(reasons, key=lambda x: -x[0])], m)
        self.history.append({"ts": now, "state": state, "reasons": rep.reasons})
        return rep
