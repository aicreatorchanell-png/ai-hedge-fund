"""Strategy families for systematic search. None is assumed to work.

Each family is a GuardedStrategy (bracket entries, stop-distance sizing, governor) with
a parameter grid fixed here *before* any real-data backtest. The grids are small on
purpose: every point is a trial counted by the research harness for the deflated
Sharpe ratio and PBO, so a wider grid raises the bar a result must clear.

    donchian_breakout     close beyond the prior N-bar high/low                 (trend)
    ema_trend             fast/slow EMA cross, ATR stop and target                (trend)
    tsmom                 sign of the N-bar return, time stop                     (momentum)
    volatility_breakout   bar range > k * ATR, close in the bar's outer quarter   (expansion)
    bollinger_reversion   close outside k-sigma bands, target the middle band     (mean reversion)
    opening_range         break of the first M minutes after a session open       (session)

Signals use bars that have closed; entries fill at the next 1-minute bar's open.
Spot crypto is long-only (a cash account cannot short); short sides are used on
margin venues (FX, index CFDs).
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from nautilus_trader.indicators import AverageTrueRange, BollingerBands, ExponentialMovingAverage
from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import OrderSide

from hedge_fund.trading.breakout import BreakoutConfig, DonchianBreakout
from hedge_fund.trading.strategy import GuardedConfig, GuardedStrategy


def _atr(strategy: GuardedStrategy, period: int) -> AverageTrueRange:
    atr = AverageTrueRange(period)
    strategy.register_indicator_for_bars(strategy.signal_bar_type, atr)
    return atr


def _bracket(strategy: GuardedStrategy, long: bool, close: float, dist: float, rr: float, bar: Bar) -> None:
    if dist <= 0:
        return
    if long:
        strategy.enter(OrderSide.BUY, close - dist, close + rr * dist, bar)
    elif strategy.config.allow_short:
        strategy.enter(OrderSide.SELL, close + dist, close - rr * dist, bar)


# -- trend: EMA cross ---------------------------------------------------------

class EmaTrendConfig(GuardedConfig, frozen=True):
    fast: int = 12
    slow: int = 48
    atr_period: int = 14
    atr_stop: float = 2.5
    reward_risk: float = 2.0


class EmaTrend(GuardedStrategy):
    def register_indicators(self) -> None:
        c = self.config
        self.fast = ExponentialMovingAverage(c.fast)
        self.slow = ExponentialMovingAverage(c.slow)
        for ind in (self.fast, self.slow):
            self.register_indicator_for_bars(self.signal_bar_type, ind)
        self.atr = _atr(self, c.atr_period)
        self.prev: float | None = None

    def on_signal(self, bar: Bar) -> None:
        if not (self.slow.initialized and self.atr.initialized):
            return
        diff = self.fast.value - self.slow.value
        prev, self.prev = self.prev, diff
        if prev is None or not self.is_flat():
            return
        c = self.config
        if prev <= 0 < diff:
            _bracket(self, True, float(bar.close), c.atr_stop * self.atr.value, c.reward_risk, bar)
        elif prev >= 0 > diff:
            _bracket(self, False, float(bar.close), c.atr_stop * self.atr.value, c.reward_risk, bar)


# -- momentum: time-series return sign ----------------------------------------

class TsmomConfig(GuardedConfig, frozen=True):
    lookback: int = 72
    threshold: float = 0.0
    atr_period: int = 14
    atr_stop: float = 3.0
    reward_risk: float = 3.0


class Tsmom(GuardedStrategy):
    def register_indicators(self) -> None:
        self.closes: deque[float] = deque(maxlen=self.config.lookback + 1)
        self.atr = _atr(self, self.config.atr_period)

    def on_signal(self, bar: Bar) -> None:
        self.closes.append(float(bar.close))
        c = self.config
        if len(self.closes) <= c.lookback or not self.atr.initialized or not self.is_flat():
            return
        ret = self.closes[-1] / self.closes[0] - 1
        if abs(ret) > c.threshold:
            _bracket(self, ret > 0, float(bar.close), c.atr_stop * self.atr.value, c.reward_risk, bar)


# -- expansion: volatility breakout -------------------------------------------

class VolBreakoutConfig(GuardedConfig, frozen=True):
    atr_period: int = 14
    range_mult: float = 2.0
    atr_stop: float = 1.5
    reward_risk: float = 2.0


class VolatilityBreakout(GuardedStrategy):
    def register_indicators(self) -> None:
        self.atr = _atr(self, self.config.atr_period)
        self.prev_atr: float | None = None

    def on_signal(self, bar: Bar) -> None:
        prev, self.prev_atr = self.prev_atr, (self.atr.value if self.atr.initialized else None)
        if prev is None or not self.is_flat():
            return
        c = self.config
        hi, lo, close = float(bar.high), float(bar.low), float(bar.close)
        rng = hi - lo
        if rng <= c.range_mult * prev:                     # compare with ATR *before* this bar
            return
        pos = (close - lo) / rng
        if pos >= 0.75:
            _bracket(self, True, close, c.atr_stop * prev, c.reward_risk, bar)
        elif pos <= 0.25:
            _bracket(self, False, close, c.atr_stop * prev, c.reward_risk, bar)


# -- mean reversion: Bollinger bands ------------------------------------------

class BollingerConfig(GuardedConfig, frozen=True):
    period: int = 20
    k: float = 2.0
    atr_period: int = 14
    atr_stop: float = 2.0
    max_hold_bars: int = 24


class BollingerReversion(GuardedStrategy):
    """Fade a close outside the bands; target the middle band, stop atr_stop * ATR away."""

    def register_indicators(self) -> None:
        self.bb = BollingerBands(self.config.period, self.config.k)
        self.register_indicator_for_bars(self.signal_bar_type, self.bb)
        self.atr = _atr(self, self.config.atr_period)

    def on_signal(self, bar: Bar) -> None:
        if not (self.bb.initialized and self.atr.initialized) or not self.is_flat():
            return
        close, mid, dist = float(bar.close), self.bb.middle, self.config.atr_stop * self.atr.value
        if close < self.bb.lower and mid > close:
            self.enter(OrderSide.BUY, close - dist, mid, bar)
        elif close > self.bb.upper and mid < close and self.config.allow_short:
            self.enter(OrderSide.SELL, close + dist, mid, bar)


# -- session: opening-range breakout ------------------------------------------

class OpeningRangeConfig(GuardedConfig, frozen=True):
    session_open_utc: str = "00:00"
    range_minutes: int = 60
    session_minutes: int = 480
    reward_risk: float = 1.5


class OpeningRange(GuardedStrategy):
    """One trade per session: break of the opening range, stop at its other side, flat at
    session end. Uses signal bars (signal_minutes must divide range_minutes)."""

    def register_indicators(self) -> None:
        if self.config.range_minutes % self.config.signal_minutes:
            raise ValueError("range_minutes must be a multiple of signal_minutes")
        h, m = map(int, self.config.session_open_utc.split(":"))
        self.open_min = h * 60 + m
        self.session: str | None = None
        self.hi = self.lo = None
        self.traded = False

    def on_signal(self, bar: Bar) -> None:
        c = self.config
        t = datetime.fromtimestamp(bar.ts_event / 1e9, tz=timezone.utc)
        mins = (t.hour * 60 + t.minute - self.open_min) % 1440       # minutes since the open, at bar close
        if mins == 0:
            mins = 1440
        sess = (t.date().toordinal() * 1440 + t.hour * 60 + t.minute - mins)
        if sess != self.session:
            self.session, self.hi, self.lo, self.traded = sess, None, None, False
        if mins > c.session_minutes:
            if not self.is_flat():
                self.flatten("session end")
            return
        if mins <= c.range_minutes:
            self.hi = max(self.hi or float(bar.high), float(bar.high))
            self.lo = min(self.lo or float(bar.low), float(bar.low))
            return
        if self.traded or self.hi is None or not self.is_flat():
            return
        close = float(bar.close)
        if close > self.hi:
            self.traded = True
            _bracket(self, True, close, close - self.lo, c.reward_risk, bar)
        elif close < self.lo and c.allow_short:
            self.traded = True
            _bracket(self, False, close, self.hi - close, c.reward_risk, bar)


# -- registry -----------------------------------------------------------------

@dataclass(frozen=True)
class Family:
    name: str
    style: str
    strategy: type
    config: type
    grid: dict

    def configs(self) -> list[dict]:
        keys = sorted(self.grid)
        return [dict(zip(keys, vals)) for vals in itertools.product(*(self.grid[k] for k in keys))]


FAMILIES: dict[str, Family] = {f.name: f for f in [
    Family("donchian_breakout", "trend", DonchianBreakout, BreakoutConfig,
           {"channel": [20, 55], "atr_stop": [1.5, 3.0], "reward_risk": [1.5, 3.0], "signal_minutes": [15, 60]}),
    Family("ema_trend", "trend", EmaTrend, EmaTrendConfig,
           {"fast": [12, 24], "slow": [48, 96], "reward_risk": [2.0, 4.0], "signal_minutes": [15, 60]}),
    Family("tsmom", "momentum", Tsmom, TsmomConfig,
           {"lookback": [24, 72, 168], "atr_stop": [2.0, 4.0], "max_hold_bars": [24, 72], "signal_minutes": [60]}),
    Family("volatility_breakout", "expansion", VolatilityBreakout, VolBreakoutConfig,
           {"range_mult": [1.5, 2.5], "atr_stop": [1.0, 2.0], "reward_risk": [1.5, 3.0], "signal_minutes": [15, 60]}),
    Family("bollinger_reversion", "mean_reversion", BollingerReversion, BollingerConfig,
           {"period": [20, 50], "k": [2.0, 2.5], "atr_stop": [1.5, 3.0], "signal_minutes": [5, 15]}),
    Family("opening_range", "session", OpeningRange, OpeningRangeConfig,
           {"session_open_utc": ["00:00", "13:30"], "range_minutes": [30, 60], "reward_risk": [1.0, 2.0],
            "signal_minutes": [5]}),
]}


def build(family: str, params: dict, *, instrument_id, bar_type, risk, allow_short: bool = False,
          kill_dir=None) -> GuardedStrategy:
    f = FAMILIES[family]
    cfg = f.config(instrument_id=instrument_id, bar_type=bar_type, allow_short=allow_short, **params)
    return f.strategy(cfg, risk, kill_dir=kill_dir)


def n_trials() -> int:
    return sum(len(f.configs()) for f in FAMILIES.values())
