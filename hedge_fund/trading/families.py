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
    funding_crowding      fade extreme perpetual funding (H-FUNDING-CROWDING)    (crowding)
    vol_managed_trend     daily trend, volatility-scaled risk (H-VOL-SCALED-TREND) (trend premium)

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


# -- crowding: extreme perpetual funding (H-FUNDING-CROWDING) ------------------

class FundingCrowdingConfig(GuardedConfig, frozen=True):
    quantile: float = 0.95                  # extreme = strictly above this quantile of all *earlier* settlements
    min_history: int = 270                  # settlements (~90 days at 8h) before any decision
    atr_period: int = 24
    atr_stop: float = 3.0
    reward_risk: float = 3.0
    funding_symbol: str = ""                # "" = the instrument symbol without "-PERP"
    funding_series: tuple = ()              # ((calc_time_ns, rate), ...) for tests/probes; () = private cache


class FundingCrowding(GuardedStrategy):
    """At each newly *settled* funding rate (visible on the first signal bar closing at or after
    its settlement time), compare it with the distribution of all earlier settlements: if it is
    positive and strictly above the `quantile`, short (crowded longs); if negative and strictly
    below the 1 - quantile, long (crowded shorts). ATR stop, target reward_risk x stop, time stop
    `max_hold_bars` signal bars. Funding is paid/received through the backtest's funding flows."""

    def register_indicators(self) -> None:
        import bisect

        import numpy as np
        self._bisect, self._np = bisect, np
        self.atr = _atr(self, self.config.atr_period)
        self.f_ts, self.f_rate = self._load_funding()
        self.f_next = 0
        self.history: list[float] = []                  # sorted earlier settlements

    def _load_funding(self):
        c = self.config
        if c.funding_series:
            ts, rate = zip(*c.funding_series)
            return list(ts), list(rate)
        from hedge_fund.trading.data import binance, funding
        from hedge_fund.trading.data.fence import last_research_day
        sym = c.funding_symbol or str(c.instrument_id.symbol).split("-")[0]
        df = funding.load(sym, binance.months("2019-09", last_research_day("crypto")))
        return list(df.index.as_unit("ns").asi8), list(df["rate"].astype(float))

    def on_signal(self, bar: Bar) -> None:
        c = self.config
        latest = None
        while self.f_next < len(self.f_ts) and self.f_ts[self.f_next] <= bar.ts_event:
            r = self.f_rate[self.f_next]
            latest = None
            if len(self.history) >= c.min_history:
                h = self._np.asarray(self.history)
                latest = (r, float(self._np.quantile(h, c.quantile)), float(self._np.quantile(h, 1 - c.quantile)))
            self._bisect.insort(self.history, r)
            self.f_next += 1
        if latest is None or not self.atr.initialized or not self.is_flat():
            return
        r, hi, lo = latest
        dist = c.atr_stop * self.atr.value
        if r > 0 and r > hi:                  # strictly beyond: funding often sits exactly at the 0.01% default
            _bracket(self, False, float(bar.close), dist, c.reward_risk, bar)
        elif r < 0 and r < lo:
            _bracket(self, True, float(bar.close), dist, c.reward_risk, bar)


# -- trend premium: daily, volatility-scaled (H-VOL-SCALED-TREND) ---------------

class VolTrendConfig(GuardedConfig, frozen=True):
    lookback: int = 60                      # signal bars (days on daily bars)
    vol_window: int = 20
    vol_stop: float = 3.0                   # stop = vol_stop x trailing bar volatility; size = risk / stop,
    reward_risk: float = 10.0               # so exposure scales inversely with volatility; target far away


class VolManagedTrend(GuardedStrategy):
    """Direction = sign of the `lookback`-bar return. Flat -> enter in that direction with a
    volatility stop; position against the current direction -> flatten (re-entered on a later
    bar). Risk per trade is fixed, so position size is inversely proportional to volatility."""

    def register_indicators(self) -> None:
        import numpy as np
        self._np = np
        self.closes: deque[float] = deque(maxlen=max(self.config.lookback, self.config.vol_window) + 1)

    def on_signal(self, bar: Bar) -> None:
        from nautilus_trader.model.enums import PositionSide
        c = self.config
        self.closes.append(float(bar.close))
        if len(self.closes) <= max(c.lookback, c.vol_window):
            return
        px = list(self.closes)
        ret = px[-1] / px[-1 - c.lookback] - 1
        direction = 1 if ret > 0 else -1 if ret < 0 else 0
        positions = self.cache.positions_open(instrument_id=c.instrument_id)
        if positions:
            held = 1 if positions[0].side == PositionSide.LONG else -1
            if direction != held:
                self.flatten("trend reversal")
            return
        if direction == 0 or not self.is_flat():
            return
        lr = self._np.diff(self._np.log(px[-c.vol_window - 1:]))
        vol = float(lr.std(ddof=1))
        if vol <= 0:
            return
        _bracket(self, direction > 0, px[-1], c.vol_stop * vol * px[-1], c.reward_risk, bar)


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
    Family("funding_crowding", "crowding", FundingCrowding, FundingCrowdingConfig,
           {"quantile": [0.95, 0.99], "atr_stop": [2.0, 4.0], "max_hold_bars": [24, 72], "signal_minutes": [60]}),
    Family("vol_managed_trend", "trend_premium", VolManagedTrend, VolTrendConfig,
           {"lookback": [20, 60, 120], "vol_stop": [2.5, 5.0]}),
]}


def build(family: str, params: dict, *, instrument_id, bar_type, risk, allow_short: bool = False,
          kill_dir=None) -> GuardedStrategy:
    f = FAMILIES[family]
    cfg = f.config(instrument_id=instrument_id, bar_type=bar_type, allow_short=allow_short, **params)
    return f.strategy(cfg, risk, kill_dir=kill_dir)


def n_trials() -> int:
    return sum(len(f.configs()) for f in FAMILIES.values())
