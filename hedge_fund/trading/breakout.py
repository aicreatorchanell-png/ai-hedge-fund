"""Reference strategy: Donchian channel breakout with ATR-based stop and target.

Engine demonstration only. Its parameters are not tuned and it has no research
status; a real candidate is pre-registered and validated (hedge_fund.research).

    long   close > highest high of the previous `channel` bars
    short  close < lowest low of the previous `channel` bars (only if allow_short)
    stop   atr_stop * ATR from the close; target reward_risk * that distance

The channel is taken from bars *before* the current one, so a bar never breaks
out of a channel that already contains it.
"""

from __future__ import annotations

from collections import deque

from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import OrderSide

from hedge_fund.trading.strategy import GuardedConfig, GuardedStrategy


class BreakoutConfig(GuardedConfig, frozen=True):
    channel: int = 20
    atr_period: int = 14
    atr_stop: float = 2.0
    reward_risk: float = 2.0


class DonchianBreakout(GuardedStrategy):
    def register_indicators(self) -> None:
        self.atr = AverageTrueRange(self.config.atr_period)
        self.register_indicator_for_bars(self.config.bar_type, self.atr)
        self.highs: deque[float] = deque(maxlen=self.config.channel)
        self.lows: deque[float] = deque(maxlen=self.config.channel)

    def on_signal(self, bar: Bar) -> None:
        c = self.config
        close = float(bar.close)
        ready = len(self.highs) == c.channel and self.atr.initialized
        upper, lower = (max(self.highs), min(self.lows)) if ready else (None, None)
        self.highs.append(float(bar.high))
        self.lows.append(float(bar.low))
        if not ready or not self.is_flat():
            return
        dist = c.atr_stop * self.atr.value
        if dist <= 0:
            return
        if close > upper:
            self.enter(OrderSide.BUY, close - dist, close + c.reward_risk * dist, bar)
        elif close < lower and c.allow_short:
            self.enter(OrderSide.SELL, close + dist, close - c.reward_risk * dist, bar)
