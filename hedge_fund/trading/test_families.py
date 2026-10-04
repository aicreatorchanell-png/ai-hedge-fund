"""Strategy families: each runs on multi-timeframe bars, decides only on closed bars,
and respects its family rules (one trade per session, time stops, long-only spot)."""

from __future__ import annotations

import pytest
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.data import Bar

from hedge_fund.trading.backtest import run_backtest
from hedge_fund.trading.families import FAMILIES, build, n_trials
from hedge_fund.trading.governor import RiskGovernor, TradeRiskConfig
from hedge_fund.trading.strategy import GuardedConfig, GuardedStrategy
from hedge_fund.trading.synthetic import synthetic_bars, synthetic_funding
from hedge_fund.trading.test_trading import BT, INST, MARGIN, VENUE, bar
from hedge_fund.trading.venue import add_venue

BARS = synthetic_bars(INST, BT, 6000, price=30_000, seed=11)
FUNDING = synthetic_funding(n=200, seed=3)            # hourly settlements across the synthetic bars


def _strategy(name, params, allow_short=False):
    if name == "funding_crowding":                    # external data: a synthetic settlement series
        params = {"funding_series": FUNDING, "min_history": 30, **params}
    return build(name, params, instrument_id=INST.id, bar_type=BT, risk=TradeRiskConfig(), allow_short=allow_short)


def test_grids_are_fixed_and_counted():
    assert n_trials() == 84 + 8 + 6                   # plan_crypto_v1 families + funding_crowding + vol_managed_trend
    assert {f.style for f in FAMILIES.values()} == {"trend", "momentum", "expansion", "mean_reversion", "session",
                                                    "crowding", "trend_premium"}


@pytest.mark.parametrize("name", sorted(FAMILIES))
def test_every_family_runs_and_trades_on_synthetic_data(name):
    params = FAMILIES[name].configs()[0]
    r = run_backtest(INST, BARS, _strategy(name, params, allow_short=True), MARGIN)
    assert len(r.brackets) > 0, name
    assert all(b["side"] in ("BUY", "SELL") for b in r.brackets)


@pytest.mark.parametrize("name", sorted(FAMILIES))
def test_family_decisions_ignore_future_bars(name):
    params = FAMILIES[name].configs()[-1]
    cut = BARS[4000].ts_event
    future = synthetic_bars(INST, BT, 2000, price=float(BARS[4000].close), seed=77)
    altered = BARS[:4001] + [Bar(BT, f.open, f.high, f.low, f.close, f.volume, b.ts_event, b.ts_init)
                             for f, b in zip(future, BARS[4001:])]
    a = run_backtest(INST, BARS, _strategy(name, params, True), MARGIN)
    b = run_backtest(INST, altered, _strategy(name, params, True), MARGIN)
    pre = lambda r: [(x["ts"], x["side"], x["quantity"]) for x in r.brackets if x["ts"] <= cut]  # noqa: E731
    assert pre(a) == pre(b)


def test_spot_cash_account_is_long_only():
    r = run_backtest(INST, BARS, _strategy("ema_trend", FAMILIES["ema_trend"].configs()[0], allow_short=True), VENUE)
    assert {b["side"] for b in r.brackets} == {"BUY"}
    assert any(x["reason"] == "cash account cannot short" for x in r.refusals)


def test_opening_range_trades_at_most_once_per_session_and_is_flat_at_session_end():
    p = {"session_open_utc": "00:00", "range_minutes": 30, "reward_risk": 2.0, "signal_minutes": 5}
    s = _strategy("opening_range", {**p, "session_minutes": 240}, True)
    r = run_backtest(INST, BARS, s, MARGIN)
    days = [b["ts"] // (86_400 * 10**9) for b in r.brackets]
    assert len(days) == len(set(days)) and days
    assert any(e["reason"] == "session end" for e in s.exits) or not r.positions["ts_closed"].isna().any()


def test_time_stop_closes_after_max_hold_bars():
    s = _strategy("bollinger_reversion", {**FAMILIES["bollinger_reversion"].configs()[0], "max_hold_bars": 2}, True)
    run_backtest(INST, BARS, s, MARGIN)
    assert any(e["reason"] == "time stop" for e in s.exits)


def test_signal_bars_aggregate_closed_execution_bars_only():
    seen = []

    class Probe(GuardedStrategy):
        def on_signal(self, b):
            seen.append(((b.ts_event - BT_T0) // 60_000_000_000, float(b.open), float(b.high), float(b.close)))

    BT_T0 = 1_577_836_800_000_000_000
    bars = [bar(i, 100 + i, 100 + i + 0.5, 100 + i - 0.5, 100 + i) for i in range(12)]
    s = Probe(GuardedConfig(instrument_id=INST.id, bar_type=BT, signal_minutes=5), TradeRiskConfig())
    e = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR")))
    try:
        add_venue(e, VENUE)
        e.add_instrument(INST)
        e.add_data(bars)
        e.add_strategy(s)
        e.run()
    finally:
        e.dispose()
    # 5-minute bar closing at minute 5 = 1-minute bars closing at 1..5 (opens 100..104)
    assert seen[:2] == [(5, 100.0, 104.5, 104.0), (10, 105.0, 109.5, 109.0)]


def test_segment_reset_clears_kill_latch_but_not_kill_file(tmp_path):
    g = RiskGovernor(TradeRiskConfig(max_drawdown=0.1), 1000, kill_dir=tmp_path)
    g.update(0, 1000)
    g.update(60 * 10**9, 850)
    assert g.killed
    g.start_segment(120 * 10**9, 850)
    assert g.killed is None and g.events[-1]["event"] == "segment_reset"
    (tmp_path / "KILL_SWITCH").touch()
    g.start_segment(180 * 10**9, 850)
    assert g.killed == "KILL_SWITCH file present"
