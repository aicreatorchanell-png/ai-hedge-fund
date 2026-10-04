"""Tests for the research-framework protections: auditor, causal regimes, fold and
stability diagnostics, diversification, health monitor, costs, trial counting."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from nautilus_trader.model.enums import OrderSide
from pydantic import ValidationError

from hedge_fund.research.pipeline import GovernanceViolation
from hedge_fund.trading import audit as audit_mod
from hedge_fund.trading import backtest as backtest_mod
from hedge_fund.trading import families
from hedge_fund.trading.backtest import CostsDisabled, run_backtest
from hedge_fund.trading.data.markets import CRYPTO, FX, INDICES
from hedge_fund.trading.diagnostics import concentration, diversification, fold_report, neighbourhood, neighbours
from hedge_fund.trading.health import (BacktestExpectations, Health, HealthMonitor, HealthThresholds,
                                       load_thresholds)
from hedge_fund.trading.regimes import classify, guard_research_dates, labels_for_returns, performance_by_regime
from hedge_fund.trading.research import ConfigRun, ResearchPlan, WalkForward, WalkForwardResult
from hedge_fund.trading.runner import count_trials
from hedge_fund.trading.strategy import GuardedStrategy
from hedge_fund.trading.test_trading import INST, VENUE, Scripted, bar, flat
from hedge_fund.validation.holdout_guard import HoldoutAccessDenied
from hedge_fund.validation.registry import ExperimentRegistry

DAY = 86_400 * 10**9


def small_plan(**kw):
    base = dict(plan_id="t", families=("ema_trend",), instruments=("BTCUSDT.BINANCE",), dev_start="2019-01-01",
                dev_end="2021-12-31", reserve_start="2025-09-01",
                walk_forward=WalkForward(train_months=12, test_months=6, step_months=6))
    return ResearchPlan(**{**base, **kw})


# -- 1. adversarial auditor ----------------------------------------------------

class Peeker(GuardedStrategy):
    """Planted bug: reads the bars being backtested, including future ones."""
    DATA: list = []

    def on_signal(self, b):
        ts = [x.ts_event for x in Peeker.DATA]
        i = ts.index(b.ts_event)
        if i + 5 < len(ts) and float(Peeker.DATA[i + 5].close) > float(b.close) * 1.0005 and self.is_flat():
            c = float(b.close)
            self.enter(OrderSide.BUY, c * 0.99, c * 1.02, b)


@pytest.fixture
def peeker(monkeypatch):
    fam = families.Family("peeker", "planted-bug", Peeker, families.GuardedConfig, {"signal_minutes": [1]})
    monkeypatch.setitem(families.FAMILIES, "peeker", fam)
    real = backtest_mod.run_backtest

    def spy(instrument, bars, strategy, venue, **kw):
        Peeker.DATA = list(bars)
        return real(instrument, bars, strategy, venue, **kw)
    monkeypatch.setattr(backtest_mod, "run_backtest", spy)
    return fam


def test_auditor_fails_a_strategy_that_reads_future_bars(peeker):
    r = audit_mod.AuditReport("planted")
    audit_mod.probe_family("peeker", {"signal_minutes": 1}, r, n=3000)
    status = {c.name: c.status for c in r.checks}
    assert status["look_ahead"] == "FAIL" and status["indicator_repainting"] == "FAIL"
    assert not r.passed


def test_auditor_passes_honest_families_on_timing_and_duplicates():
    r = audit_mod.AuditReport("honest")
    audit_mod.probe_family("donchian_breakout", families.FAMILIES["donchian_breakout"].configs()[0], r)
    assert {c.name: c.status for c in r.checks} == {"look_ahead": "PASS", "indicator_repainting": "PASS",
                                                      "execution_timing": "PASS", "duplicate_orders": "PASS"}


@pytest.mark.parametrize("minutes", [5, 15, 60])
def test_auditor_resampling_probe(minutes):
    r = audit_mod.AuditReport("resampling")
    audit_mod.probe_resampling(minutes, r)
    assert r.checks[0].status == "PASS"


def test_auditor_static_checks():
    r = audit_mod.AuditReport("static")
    audit_mod.check_costs(small_plan(cost_multipliers=(1.0,)), r)
    audit_mod.check_leakage(small_plan(), r)
    audit_mod.check_survivorship(small_plan(), r)
    audit_mod.probe_future_fitting(r)
    s = {c.name: c.status for c in r.checks}
    assert s["fees"] == "PASS" and s["spread_slippage"] == "PASS" and s["cost_stress"] == "FAIL"
    assert s["train_test_holdout"] == "PASS" and s["survivorship"] == "WARNING" and s["future_fitting"] == "PASS"
    assert not r.passed and r.counts()["FAIL"] == 1


def test_auditor_fails_when_the_reserve_is_not_sealed():
    r = audit_mod.AuditReport("unsealed")
    audit_mod.check_leakage(small_plan(reserve_start="2022-06-01"), r)      # 2022-06 is readable
    assert r.checks[0].status == "FAIL"


def test_auditor_fills_check_uses_ambiguity_share():
    for share, status in ((0.01, "PASS"), (0.10, "WARNING"), (0.30, "FAIL")):
        r = audit_mod.AuditReport("fills")
        audit_mod.check_fills(r, [{"ambiguous_share": share}])
        assert r.checks[0].status == status


# -- 2. regimes ----------------------------------------------------------------

def _closes(n=700, seed=3, start="2019-01-01"):
    rng = np.random.default_rng(seed)
    drift = np.repeat([0.003, -0.003, 0.0, 0.002], n // 4 + 1)[:n]
    vol = np.repeat([0.01, 0.04, 0.01, 0.03], n // 4 + 1)[:n]
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(drift, vol))),
                     index=pd.date_range(start, periods=n, freq="D", tz="UTC"))


def test_regime_classification_is_causal():
    c = _closes()
    full = classify(c)
    for t in (150, 300, 451, 699):
        part = classify(c.iloc[:t])
        assert part.iloc[-1][["trend", "vol"]].tolist() == full.iloc[t - 1][["trend", "vol"]].tolist()
    altered = c.copy()
    altered.iloc[400:] *= np.linspace(1, 3, len(altered) - 400)
    assert classify(altered).iloc[:400].equals(full.iloc[:400])


def test_regimes_label_returns_with_the_previous_close_and_find_dominance():
    c = _closes()
    reg = classify(c)
    rets = c.pct_change().dropna()
    lab = labels_for_returns(reg, rets)
    assert lab.loc[rets.index[300], "trend"] == reg.loc[reg.index[299], "trend"]
    assert {"bull", "bear", "sideways"} & set(reg["trend"]) and {"high", "low"} <= set(reg["vol"])
    only_bull = rets.where(lab["trend"] == "bull", 0.0).clip(lower=0) + 1e-6
    perf = performance_by_regime(only_bull, reg)
    assert perf["trend"]["top_regime"] == "bull" and perf["trend"]["dominated"]


def test_analysis_refuses_sealed_holdout_dates():
    idx = pd.date_range("2025-08-30", periods=5, freq="D", tz="UTC")
    with pytest.raises(HoldoutAccessDenied):
        guard_research_dates(idx, "crypto")
    with pytest.raises(HoldoutAccessDenied):
        classify(pd.Series(np.linspace(1, 2, 5), index=idx), market="crypto")
    guard_research_dates(pd.date_range("2025-01-01", periods=5, freq="D", tz="UTC"), "crypto")


# -- 3/4. folds and stability --------------------------------------------------

def _wf(plan, fold_returns):
    pieces, choices = [], []
    for (_, _, te_s, te_e), r in zip(plan.folds(), fold_returns):
        idx = pd.date_range(te_s, te_e, freq="D", tz="UTC")
        pieces.append(pd.Series(r / len(idx), index=idx))
        choices.append({"choice": "k"})
    return WalkForwardResult(pd.concat(pieces), choices, [0.0] * len(pieces), 0)


def test_fold_report_flags_a_result_that_depends_on_one_fold():
    p = small_plan()
    run = ConfigRun("k", "ema_trend", {}, "BTCUSDT.BINANCE", 1.0, pd.Series(dtype=float),
                    pd.Series(1.0, index=pd.date_range("2019-01-01", "2021-12-31", periods=100, tz="UTC")))
    fragile = fold_report(p, _wf(p, [0.60, -0.05, -0.04, -0.03]), {"k": run})
    s = fragile["summary"]
    assert s["fragile"] and s["positive"] == 1 and s["worst_fold"]["return"] < 0 and s["total_without_best_fold"] < 0
    steady = fold_report(p, _wf(p, [0.05, 0.04, 0.06, 0.05]), {"k": run})["summary"]
    assert not steady["fragile"] and steady["negative"] == 0
    assert all(f["trades"] > 0 for f in fragile["folds"])


def test_neighbourhood_flags_sharp_isolated_peaks():
    grid = {"a": [1, 2, 3], "b": [10, 20]}
    assert len(neighbours(grid, {"a": 2, "b": 10})) == 3
    metric = {frozenset({"a": a, "b": b}.items()): 0.1 for a in grid["a"] for b in grid["b"]}
    metric[frozenset({"a": 2, "b": 10}.items())] = 2.0
    peak = neighbourhood(grid, {"a": 2, "b": 10}, metric)
    assert peak["sharp_peak"] and not peak["unstable_neighbours"]
    metric[frozenset({"a": 1, "b": 10}.items())] = -0.5
    assert neighbourhood(grid, {"a": 2, "b": 10}, metric)["unstable_neighbours"]
    flat_ = {k: 1.0 for k in metric}
    assert not neighbourhood(grid, {"a": 2, "b": 10}, flat_)["sharp_peak"]


def test_concentration_flags_few_trades_and_one_asset():
    pnls = [100.0] + [-1.0] * 40 + [0.5] * 20
    c = concentration(pnls, by_asset={"BTC": 80.0, "ETH": 5.0, "XRP": -3.0}, regime_perf={"trend": {"dominated": True}})
    assert c["dominated_by_few_trades"] and c["dominated_by_one_asset"] and c["dominated_by_one_regime"]
    even = concentration([1.0] * 50, by_asset={"BTC": 25.0, "ETH": 25.0})
    assert not even["dominated_by_few_trades"] and not even["dominated_by_one_asset"]


# -- 6. diversification --------------------------------------------------------

def test_diversification_measures_overlap_without_claiming_profit():
    idx = pd.date_range("2020-01-01", periods=300, freq="D", tz="UTC")
    rng = np.random.default_rng(1)
    a = pd.Series(rng.normal(0, 0.01, 300), index=idx)
    b = a * 0.9 + pd.Series(rng.normal(0, 0.002, 300), index=idx)
    c = pd.Series(rng.normal(0, 0.01, 300), index=idx)
    d = diversification({"a": a, "b": b, "c": c}, family={"a": "trend", "b": "trend", "c": "meanrev"},
                        asset={"a": "BTC", "b": "BTC", "c": "ETH"},
                        exposure={"a": (a > 0).astype(float), "b": (b > 0).astype(float), "c": (c > 0).astype(float)})
    assert ("a", "b", d["same_source_pairs"][0][2]) == d["same_source_pairs"][0] and len(d["same_source_pairs"]) == 1
    assert d["by_family"]["trend"] == pytest.approx(2 / 3) and d["hhi_asset"] > 0.5
    assert 1.5 < d["effective_independent_sources"] < 2.5 and "not evidence of profitability" in d["note"]
    assert d["overlap"]["a"]["b"] > d["overlap"]["a"]["c"]


# -- 5. health monitor ---------------------------------------------------------

EXP = BacktestExpectations(sharpe_annual=1.0, max_drawdown=0.10, win_rate=0.5, avg_win=2.0, avg_loss=1.0,
                           trades_per_day=1.0, slippage_bps=2.0, cost_per_trade=0.5, trades_per_year=365)


def monitor():
    return HealthMonitor(load_thresholds(), EXP)


def test_thresholds_are_versioned_frozen_and_hashed():
    t = load_thresholds()
    assert t.version == "1.0.0" and len(t.config_hash()) == 16
    with pytest.raises(ValidationError):
        t.loss_streak_halt = 99
    assert HealthThresholds(**{**t.model_dump(), "loss_streak_halt": 12}).config_hash() != t.config_hash()


def test_health_states_escalate_and_halt_latches():
    m = monitor()
    m.record_equity(0, 100.0)
    for i in range(30):                                     # healthy trading
        m.record_trade(i * DAY, 2.0 if i % 2 else -1.0, slippage_bps=2.0)
    assert m.state is Health.HEALTHY
    assert m.record_equity(31 * DAY, 89.0).state is Health.WARNING          # dd 11% > expected 10%
    assert m.record_equity(32 * DAY, 84.0).state is Health.DEGRADED         # dd 16% > 1.5x
    assert m.record_equity(33 * DAY, 79.0).state is Health.HALT             # dd 21% > 2x
    assert not m.allows_entries()
    assert m.record_equity(34 * DAY, 100.0).state is Health.HALT            # latched even after recovery


def test_loss_streak_and_slippage_halt():
    m = monitor()
    for i in range(10):
        m.record_trade(i * DAY, -1.0)
    assert m.state is Health.HALT and "consecutive losses" in m.latched
    s = monitor()
    for i in range(25):
        s.record_trade(i * DAY, 2.0 if i % 2 else -1.0, slippage_bps=9.0)
    assert s.state is Health.HALT and "slippage" in s.latched


def test_inactivity_and_cost_drift_are_flagged():
    m = monitor()
    m.record_equity(0, 100.0)
    assert m.evaluate(5 * DAY).state is Health.DEGRADED                    # 5 days idle > 3 x 1 day
    c = monitor()
    for i in range(25):
        c.record_trade(i * DAY, 2.0 if i % 2 else -1.0, expected_cost=0.5, actual_cost=1.0, slippage_bps=2.0)
    rep = c.evaluate(25 * DAY)
    assert rep.state is Health.WARNING and any("costs" in r for r in rep.reasons)


def test_halt_reset_is_human_only():
    m = monitor()
    for i in range(10):
        m.record_trade(i * DAY, -1.0)
    for actor in ("ai:claude-opus-5-5", "ai:kimi-k3", "system"):
        with pytest.raises(GovernanceViolation):
            m.reset(actor, "please resume")
    assert not m.allows_entries()
    m.reset("human:owner", "reviewed")
    assert m.allows_entries()


def test_health_halt_blocks_new_entries_in_the_engine():
    m = monitor()
    for i in range(10):
        m.record_trade(i, -1.0)
    assert not m.allows_entries()
    bars = flat(3) + [bar(3, 100, 100.1, 99.9, 100)] + flat(3, start=4)
    s = Scripted({2: (OrderSide.BUY, 99.0, 101.0)}, health=m)
    r = run_backtest(INST, bars, s, VENUE)
    assert r.orders.empty and r.refusals[0]["reason"] == "health HALT"


def test_closed_trades_and_equity_feed_the_monitor():
    m = monitor()
    bars = flat(3) + [bar(3, 100.0, 100.1, 99.9, 100.0), bar(4, 99.5, 99.6, 98.5, 98.7)] + flat(3, 98.7, start=5)
    run_backtest(INST, bars, Scripted({2: (OrderSide.BUY, 99.0, 103.0)}, health=m), VENUE)
    assert len(m.trades) == 1 and m.trades[0].pnl < 0 and m.equity


# -- costs, trials -------------------------------------------------------------

def test_costs_cannot_be_disabled():
    from decimal import Decimal

    from hedge_fund.trading.instruments import to_nautilus
    from hedge_fund.core.instruments import AssetClass, Instrument
    free = to_nautilus(Instrument(symbol="BTC/USDT", asset_class=AssetClass.CRYPTO, currency="USDT"), "BINANCE")
    assert free.taker_fee == Decimal(0)
    with pytest.raises(CostsDisabled):
        run_backtest(free, flat(5), Scripted({}), VENUE)
    for spec in [*CRYPTO.values(), *FX.values(), *INDICES.values()]:
        assert float(spec.instrument().taker_fee) > 0
        with pytest.raises(ValueError):
            spec.fees(0.0)
    with pytest.raises(ValidationError):
        backtest_mod.VenueSpec(name="X", starting_balances={"USD": 1}, latency_ns=0)


def test_trial_count_is_cumulative_across_phases(tmp_path):
    old = ExperimentRegistry(tmp_path / "old.jsonl")
    for i in range(3):
        old.record(family="active/ema_trend", spec={"params": {"x": i}, "cost_multiplier": 1.0}, stage="development",
                   metrics={})
    old.record(family="active/ema_trend", spec={"params": {"x": 0}, "cost_multiplier": 2.0}, stage="development",
               metrics={})
    new = ExperimentRegistry(tmp_path / "new.jsonl")
    new.record(family="active/tsmom", spec={"params": {"y": 1}, "cost_multiplier": 1.0}, stage="development",
               metrics={})
    assert count_trials(new) == 1 and count_trials(new, [tmp_path / "old.jsonl"]) == 4


def test_repository_trial_count_includes_crypto_v1(tmp_path):
    from hedge_fund.trading.runner import all_registries
    assert count_trials(ExperimentRegistry(tmp_path / "x.jsonl"), all_registries()) >= 504


# -- liquidity cap and financing ------------------------------------------------

def test_entries_are_capped_by_recent_bar_volume():
    bars = [bar(i, 100, 100.1, 99.9, 100, volume=20) for i in range(3)] + [bar(3, 100, 100.1, 99.9, 100)] + \
        flat(3, start=4)
    r = run_backtest(INST, bars, Scripted({2: (OrderSide.BUY, 99.0, 102.0)}), VENUE)
    assert r.brackets[0]["quantity"] == pytest.approx(2.0)          # 10% of median volume 20, not 41.7 from risk
    with pytest.raises(ValueError):
        from hedge_fund.trading.strategy import GuardedConfig
        GuardedStrategy(GuardedConfig(instrument_id=INST.id, bar_type=bars[0].bar_type,
                                      max_volume_participation=0.5), __import__(
            "hedge_fund.trading.governor", fromlist=["x"]).TradeRiskConfig())


def test_financing_is_charged_on_margin_positions_and_reduces_equity():
    from hedge_fund.trading.backtest import apply_financing, financing_costs
    eur = FX["EURUSD"]
    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    pos = pd.DataFrame({"ts_opened": [t0, t0], "ts_closed": [t0 + pd.Timedelta(days=10), pd.NaT],
                        "peak_qty": [100_000.0, 50_000.0], "quantity": [0.0, 50_000.0],
                        "avg_px_open": [1.10, 1.10], "entry": ["BUY", "SELL"]})
    costs = financing_costs(pos, eur, (t0 + pd.Timedelta(days=20)).value)
    assert costs.sum() == pytest.approx(100_000 * 1.10 * 1e-4 * 10 + 50_000 * 1.10 * 1e-4 * 20)
    eq = pd.Series(10_000.0, index=pd.date_range(t0, periods=25, freq="D", tz="UTC"))
    net = apply_financing(eq, costs)
    assert net.iloc[5] == 10_000 and net.iloc[-1] == pytest.approx(10_000 - costs.sum())
    assert financing_costs(pos, CRYPTO["BTCUSDT"], t0.value).empty               # spot crypto: no borrowing
    for spec in [*FX.values(), *INDICES.values()]:
        assert spec.financing_long_bps_day > 0 and spec.financing_short_bps_day > 0


def test_auditor_fails_margin_markets_without_financing(monkeypatch):
    from hedge_fund.trading.data import markets
    r = audit_mod.AuditReport("fin")
    audit_mod.check_costs(small_plan(instruments=("EURUSD.DUKASCOPY",)), r)
    assert {c.name: c.status for c in r.checks}["financing"] == "PASS"
    monkeypatch.setitem(markets.FX, "EURUSD", markets.FX["EURUSD"].model_copy(
        update={"financing_long_bps_day": 0.0, "financing_short_bps_day": 0.0}))
    r = audit_mod.AuditReport("fin")
    audit_mod.check_costs(small_plan(instruments=("EURUSD.DUKASCOPY",)), r)
    assert {c.name: c.status for c in r.checks}["financing"] == "FAIL" and not r.passed
