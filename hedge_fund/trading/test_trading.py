"""Tests for the Nautilus trading layer: sizing, governor, execution timing, brackets,
ambiguity audit, kill switch, determinism, look-ahead and the holdout fence."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from pydantic import ValidationError

from hedge_fund.core.instruments import AssetClass, Instrument
from hedge_fund.trading.backtest import check_bars, run_backtest
from hedge_fund.trading.breakout import BreakoutConfig, DonchianBreakout
from hedge_fund.trading.governor import KILL_SWITCH, RiskGovernor, TradeRiskConfig
from hedge_fund.trading.instruments import to_nautilus
from hedge_fund.trading.sizing import size_for_stop
from hedge_fund.trading.strategy import GuardedConfig, GuardedStrategy
from hedge_fund.trading.synthetic import synthetic_bars
from hedge_fund.trading.venue import VenueSpec
from hedge_fund.validation.holdout_guard import HoldoutAccessDenied
from hedge_fund.validation.stats import sharpe

INST = TestInstrumentProvider.btcusdt_binance()          # tick 0.01, step 1e-6, min notional 10, fee 0.1%
BT = BarType.from_str(f"{INST.id}-1-MINUTE-LAST-EXTERNAL")
T0 = 1_577_836_800_000_000_000                            # 2020-01-01T00:00Z
MIN = 60_000_000_000
VENUE = VenueSpec(name="BINANCE", starting_balances={"USDT": 10_000, "BTC": 0})


def bar(i, o, h, lo, c, t0=T0, volume=1000):
    ts = t0 + (i + 1) * MIN
    return Bar(BT, INST.make_price(o), INST.make_price(h), INST.make_price(lo), INST.make_price(c),
               INST.make_qty(volume), ts, ts)


def flat(n, px=100.0, t0=T0, start=0):
    return [bar(start + i, px, px + 0.1, px - 0.1, px, t0) for i in range(n)]


class Scripted(GuardedStrategy):
    """Enters on given bar indices: plan = {index: (side, stop, take_profit)}."""

    def __init__(self, plan, risk=TradeRiskConfig(), **kw):
        super().__init__(GuardedConfig(instrument_id=INST.id, bar_type=BT, allow_short=True), risk, **kw)
        self.plan, self.i = plan, -1

    def on_signal(self, b):
        self.i += 1
        if self.i in self.plan:
            side, stop, tp = self.plan[self.i]
            self.enter(side, stop, tp, b)


def fills(r):
    f = r.orders[r.orders["status"] == "FILLED"]
    return [(row.type, row.side, float(row.avg_px), int(row.ts_last)) for row in f.itertuples()]


# -- sizing ---------------------------------------------------------------

def test_size_from_stop_distance_and_cap():
    q = size_for_stop(10_000, 100, 99, risk_fraction=0.005, max_notional_fraction=1.0, size_increment=0.001)
    assert q == pytest.approx(50.0)                       # 50 / 1.0
    q = size_for_stop(10_000, 100, 99.9, risk_fraction=0.005, max_notional_fraction=0.5, size_increment=0.001)
    assert q == pytest.approx(50.0)                       # risk wants 500 units; notional cap 5000 binds
    q = size_for_stop(10_000, 100, 99, risk_fraction=0.005, max_notional_fraction=1, size_increment=1,
                      fee_rate=0.001)
    assert q == 41.0                                       # 50 / (1 + 0.2) = 41.67, floored to whole units


def test_size_below_venue_minimum_is_zero_and_bad_inputs_raise():
    assert size_for_stop(100, 100, 99, risk_fraction=0.005, max_notional_fraction=1, size_increment=1e-6,
                         min_notional=60) == 0.0            # 0.5 units = 50 notional < 60
    assert size_for_stop(100, 100, 99, risk_fraction=0.005, max_notional_fraction=1, size_increment=1) == 0.0
    assert size_for_stop(-5, 100, 99, risk_fraction=0.005, max_notional_fraction=1, size_increment=1) == 0.0
    for args in ((100, 100, 100), (100, 0, 99), (100, math.nan, 99)):
        with pytest.raises(ValueError):
            size_for_stop(*args, risk_fraction=0.005, max_notional_fraction=1, size_increment=1)


# -- governor -------------------------------------------------------------

def test_risk_config_is_frozen_and_bounded():
    c = TradeRiskConfig()
    with pytest.raises(ValidationError):
        c.risk_per_trade = 0.5
    for bad in ({"risk_per_trade": 0.05}, {"max_notional_fraction": 2.0}, {"max_drawdown": 0.9}):
        with pytest.raises(ValidationError):
            TradeRiskConfig(**bad)
    assert c.config_hash() == TradeRiskConfig().config_hash() != TradeRiskConfig(max_daily_loss=0.02).config_hash()


def test_governor_daily_loss_resets_next_day_and_drawdown_latches(tmp_path):
    g = RiskGovernor(TradeRiskConfig(max_daily_loss=0.03, max_drawdown=0.10), 1000)
    day = 86_400 * 10**9
    g.update(T0, 1000)
    assert g.can_enter(1000, 0) == (True, None)
    assert g.can_enter(965, 0) == (False, "daily loss limit")
    g.update(T0 + day, 965)                               # new UTC day: 965 is the new day start
    assert g.can_enter(965, 0) == (True, None)
    assert g.can_enter(965, 1) == (False, "max open positions")
    g.update(T0 + day + MIN, 899)                         # -10.1% from peak 1000
    assert g.killed and g.can_enter(2000, 0)[0] is False
    g.update(T0 + 2 * day, 2000)
    assert g.killed                                       # latched


def test_governor_kill_file(tmp_path: Path):
    g = RiskGovernor(TradeRiskConfig(), 1000, kill_dir=tmp_path)
    g.update(T0, 1000)
    assert not g.killed
    (tmp_path / KILL_SWITCH).touch()
    g.update(T0 + MIN, 1000)
    assert g.killed == "KILL_SWITCH file present"


def test_venue_spec_refuses_same_bar_fills_and_leverage():
    with pytest.raises(ValidationError):
        VenueSpec(name="X", starting_balances={"USD": 1}, latency_ns=0)
    with pytest.raises(ValidationError):
        VenueSpec(name="X", starting_balances={"USD": 1}, leverage=2.0)


# -- engine: execution timing and brackets --------------------------------

def test_entry_fills_at_next_bar_open_never_the_decision_bar():
    bars = flat(3) + [bar(3, 100.5, 100.6, 100.4, 100.5)] + flat(3, 100.5, start=4)
    r = run_backtest(INST, bars, Scripted({2: (OrderSide.BUY, 99.0, 102.0)}), VENUE)
    entry = fills(r)[0]
    assert entry[:2] == ("MARKET", "BUY")
    assert entry[3] == bars[3].ts_event                   # decided on bar 2, filled on bar 3
    assert entry[2] == pytest.approx(100.51)              # bar 3 open + one adverse tick, not bar 2 close (100)


def test_order_larger_than_bar_volume_walks_the_price():
    bars = flat(3) + [bar(3, 100.5, 100.6, 100.4, 100.5, volume=10)] + flat(3, 100.5, start=4)
    r = run_backtest(INST, bars, Scripted({2: (OrderSide.BUY, 99.0, 102.0)}), VENUE)
    assert r.brackets[0]["quantity"] > 40                 # about 4x the bar's volume of 10
    assert 100.51 < fills(r)[0][2] <= 100.6               # filled tick by tick above the open


def test_stop_loss_exit_cancels_target_and_loses_about_the_risk_budget():
    bars = flat(3) + [bar(3, 100.0, 100.1, 99.9, 100.0), bar(4, 99.5, 99.6, 98.5, 98.7)] + flat(3, 98.7, start=5)
    r = run_backtest(INST, bars, Scripted({2: (OrderSide.BUY, 99.0, 103.0)}), VENUE)
    status = r.orders.set_index("type")["status"].to_dict()
    assert status == {"MARKET": "FILLED", "STOP_MARKET": "FILLED", "LIMIT": "CANCELED"}
    (pnl,) = r.trade_pnls()
    assert -0.0075 * 10_000 < pnl < -0.004 * 10_000       # risk 0.5% plus slippage and fees
    assert r.audit["exits"] == 1 and r.audit["ambiguous_exits"] == 0


def test_ambiguous_bar_is_flagged_and_repriced_at_the_stop():
    bars = flat(3) + [bar(3, 100.0, 100.1, 99.9, 100.0), bar(4, 100.0, 102.0, 98.0, 99.5)] + flat(3, 99.5, start=5)
    r = run_backtest(INST, bars, Scripted({2: (OrderSide.BUY, 99.0, 101.0)}), VENUE)
    a = r.audit
    assert a["exits"] == 1 and a["ambiguous_exits"] == 1 and a["ambiguous_share"] == 1.0
    assert a["ambiguous"][0]["exit"] == "tp"              # open-high-low-close reaches the target first
    qty = r.brackets[0]["quantity"]
    assert a["pessimistic_pnl_adjustment"] == pytest.approx(-2.0 * qty)


def test_long_only_refuses_short_and_bad_bracket_raises():
    class LongOnly(Scripted):
        def __init__(self, plan):
            GuardedStrategy.__init__(self, GuardedConfig(instrument_id=INST.id, bar_type=BT), TradeRiskConfig())
            self.plan, self.i = plan, -1
    r = run_backtest(INST, flat(6), LongOnly({2: (OrderSide.SELL, 101.0, 99.0)}), VENUE)
    assert r.refusals[0]["reason"] == "short selling disabled" and r.orders.empty
    with pytest.raises(ValueError, match="long bracket"):
        run_backtest(INST, flat(6), Scripted({2: (OrderSide.BUY, 101.0, 102.0)}), VENUE)


def test_size_below_minimum_is_refused():
    tiny = VenueSpec(name="BINANCE", starting_balances={"USDT": 20, "BTC": 0})
    r = run_backtest(INST, flat(6), Scripted({2: (OrderSide.BUY, 99.0, 101.0)}), tiny)
    assert r.refusals[0]["reason"] == "size below venue minimum" and r.orders.empty


# -- kill switch ----------------------------------------------------------

def test_drawdown_kill_flattens_and_blocks_new_entries():
    risk = TradeRiskConfig(risk_per_trade=0.02, max_drawdown=0.005)
    bars = flat(3) + [bar(3, 100, 100.1, 99.9, 100), bar(4, 100, 100, 97, 97.2)] + flat(5, 97.2, start=5)
    r = run_backtest(INST, bars, Scripted({2: (OrderSide.BUY, 95.0, 110.0), 6: (OrderSide.BUY, 96.0, 99.0)}, risk), VENUE)
    assert r.risk_events and r.risk_events[0]["event"] == "kill_switch"
    assert not r.positions.empty and r.positions["ts_closed"].notna().all()     # flattened
    assert {"STOP_MARKET", "LIMIT"} <= set(r.orders[r.orders["status"] == "CANCELED"]["type"])
    assert len(r.brackets) == 1                                                   # bar-6 entry never sent


def test_kill_switch_file_blocks_all_entries(tmp_path):
    (tmp_path / KILL_SWITCH).touch()
    r = run_backtest(INST, flat(6), Scripted({2: (OrderSide.BUY, 99.0, 101.0)}, kill_dir=tmp_path), VENUE)
    assert r.orders.empty and r.risk_events[0]["reason"] == "KILL_SWITCH file present"


# -- determinism, look-ahead, data guards ---------------------------------

def _breakout(allow_short=False):
    return DonchianBreakout(BreakoutConfig(instrument_id=INST.id, bar_type=BT, allow_short=allow_short),
                            TradeRiskConfig())


MARGIN = VenueSpec(name="BINANCE", account_type="MARGIN", starting_balances={"USDT": 10_000})


def test_short_needs_a_margin_account_and_engine_aborts_are_raised():
    bars = flat(3) + [bar(3, 100, 100.1, 99.9, 100)] + flat(3, 100, start=4)
    r = run_backtest(INST, bars, Scripted({2: (OrderSide.SELL, 101.0, 98.0)}), VENUE)
    assert r.refusals[0]["reason"] == "cash account cannot short" and r.orders.empty
    r = run_backtest(INST, bars, Scripted({2: (OrderSide.SELL, 101.0, 98.0)}), MARGIN)
    assert fills(r)[0][:2] == ("MARKET", "SELL")
    sb = synthetic_bars(INST, BT, 1500, price=30_000, seed=3)
    r = run_backtest(INST, sb, _breakout(allow_short=True), MARGIN)
    assert {x["side"] for x in r.brackets} == {"BUY", "SELL"}


def test_backtest_is_deterministic():
    bars = synthetic_bars(INST, BT, 1500, price=30_000, seed=3)
    a, b = run_backtest(INST, bars, _breakout(), VENUE), run_backtest(INST, bars, _breakout(), VENUE)
    assert fills(a) == fills(b) and len(fills(a)) > 10
    assert a.equity.equals(b.equity)


def test_breakout_decisions_do_not_depend_on_future_bars():
    bars = synthetic_bars(INST, BT, 1500, price=30_000, seed=4)
    cut = bars[1000].ts_event
    future = synthetic_bars(INST, BT, 500, price=float(bars[1000].close), seed=99, start="2020-01-01")
    altered = bars[:1001] + [Bar(BT, f.open, f.high, f.low, f.close, f.volume, b.ts_event, b.ts_init)
                             for f, b in zip(future, bars[1001:])]
    a, b = run_backtest(INST, bars, _breakout(), VENUE), run_backtest(INST, altered, _breakout(), VENUE)
    pre = lambda r: [(x["ts"], x["side"], x["quantity"]) for x in r.brackets if x["ts"] <= cut]  # noqa: E731
    assert pre(a) == pre(b) and len(pre(a)) > 3


def test_returns_feed_the_validation_stack():
    r = run_backtest(INST, synthetic_bars(INST, BT, 1500, price=30_000, seed=5), _breakout(), VENUE)
    assert len(r.returns()) == 1499 and math.isfinite(sharpe(r.returns().to_numpy()))
    assert r.meta["risk_config_hash"] == TradeRiskConfig().config_hash()


def test_bar_checks():
    b = flat(3)
    with pytest.raises(ValueError, match="sorted"):
        check_bars([b[1], b[0]])
    early = Bar(BT, b[0].open, b[0].high, b[0].low, b[0].close, b[0].volume, b[0].ts_event, b[0].ts_event - 1)
    with pytest.raises(ValueError, match="before it closed"):
        check_bars([early])


def test_holdout_fence_blocks_sealed_window():
    t_fenced = 1_789_430_400_000_000_000                  # 2026-09-15T00:00Z, inside phase-b-prospective's fence
    with pytest.raises(HoldoutAccessDenied):
        run_backtest(INST, flat(6, t0=t_fenced), Scripted({}), VENUE)


# -- instrument bridge ----------------------------------------------------

def test_core_instruments_map_to_nautilus():
    btc = to_nautilus(Instrument(symbol="BTC/USDT", asset_class=AssetClass.CRYPTO, currency="USDT", tick_size=0.01,
                                 quantity_step=0.00001, min_notional=5), "BINANCE", taker_fee=0.001)
    assert (str(btc.id), btc.size_precision, float(btc.min_notional), float(btc.taker_fee)) == \
        ("BTC/USDT.BINANCE", 5, 5.0, 0.001)
    eurusd = to_nautilus(Instrument(symbol="EUR/USD", asset_class=AssetClass.FX, tick_size=0.00001,
                                    quantity_step=1000), "IDEALPRO")
    assert eurusd.price_precision == 5 and str(eurusd.base_currency) == "EUR"
    spy = to_nautilus(Instrument(symbol="SPY", asset_class=AssetClass.ETF), "ARCA")
    assert str(spy.id) == "SPY.ARCA" and spy.price_precision == 2
    with pytest.raises(ValueError):
        to_nautilus(Instrument(symbol="ES", asset_class=AssetClass.FUTURE), "CME")
    with pytest.raises(ValueError):
        to_nautilus(Instrument(symbol="BTCUSDT", asset_class=AssetClass.CRYPTO), "BINANCE")
