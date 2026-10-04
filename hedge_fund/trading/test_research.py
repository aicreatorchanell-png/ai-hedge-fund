"""Research harness: plan validation, fold geometry, train-only selection, gates evidence."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from hedge_fund.trading.data.markets import CRYPTO
from hedge_fund.trading.research import (ConfigRun, ResearchPlan, WalkForward, combine, evidence, gate,
                                         max_drawdown, run_config, walk_forward)
from hedge_fund.trading.synthetic import synthetic_bars
from hedge_fund.trading.test_trading import BT
from hedge_fund.validation.gates import GateConfig
from hedge_fund.validation.registry import ExperimentRegistry


def plan(**kw):
    base = dict(plan_id="t", families=("ema_trend",), instruments=("BTCUSDT.BINANCE",), dev_start="2020-01-01",
                dev_end="2022-12-31", walk_forward=WalkForward(train_months=12, test_months=6, step_months=6))
    return ResearchPlan(**{**base, **kw})


def test_plan_refuses_reserve_and_holdout_overlap():
    with pytest.raises(ValidationError):
        plan(dev_end="2025-10-31")                                   # past reserve_start
    with pytest.raises(ValidationError):
        plan(dev_end="2026-09-15", reserve_start="2026-12-01")      # sealed holdout fence
    with pytest.raises(ValidationError):
        plan(families=("magic",))
    with pytest.raises(ValidationError):
        plan(cost_multipliers=(0.5,))
    assert plan().plan_hash() == plan().plan_hash() != plan(plan_id="u").plan_hash()


def test_folds_are_contiguous_and_test_follows_train():
    f = plan().folds()
    assert f[0] == ("2020-01-01", "2020-12-31", "2021-01-01", "2021-06-30")
    assert [x[2] for x in f] == ["2021-01-01", "2021-07-01", "2022-01-01", "2022-07-01"]
    for tr_s, tr_e, te_s, te_e in f:
        assert tr_s < tr_e < te_s <= te_e and pd.Timestamp(te_s) - pd.Timestamp(tr_e) == pd.Timedelta(days=1)


def _run(key, train_mu, test_mu, n_trades=400, seed=0):
    p = plan()
    idx = pd.date_range(p.dev_start, p.dev_end, freq="D", tz="UTC")
    rng = np.random.default_rng(seed)
    mu = np.where(idx < pd.Timestamp("2021-01-01", tz="UTC"), train_mu, train_mu)
    in_test = np.zeros(len(idx), bool)
    for _, _, te_s, te_e in p.folds():
        in_test |= (idx >= pd.Timestamp(te_s, tz="UTC")) & (idx <= pd.Timestamp(te_e, tz="UTC"))
    mu = np.where(in_test, test_mu, mu)
    daily = pd.Series(rng.normal(mu, 0.01), index=idx)
    trades = pd.Series(1.0, index=pd.date_range(p.dev_start, p.dev_end, periods=n_trades, tz="UTC"))
    return ConfigRun(key, "ema_trend", {}, "BTCUSDT.BINANCE", 1.0, daily, trades)


def test_selection_uses_training_data_only():
    # 'peeker' is poor before the first test window; making it spectacular inside test
    # window k must not change the choice for fold k or any earlier fold.
    p = plan()
    base = [_run("peeker", -0.002, -0.002, seed=1), _run("steady", 0.002, 0.002, seed=2)]
    ref = [c["choice"] for c in walk_forward(p, base).choices]
    assert ref[0] == "steady"
    for k, (_, _, te_s, te_e) in enumerate(p.folds()):
        peek = _run("peeker", -0.002, -0.002, seed=1)
        mask = (peek.daily.index >= pd.Timestamp(te_s, tz="UTC")) & (peek.daily.index <= pd.Timestamp(te_e, tz="UTC"))
        peek.daily[mask] = 0.05
        got = [c["choice"] for c in walk_forward(p, [peek, base[1]]).choices]
        assert got[:k + 1] == ref[:k + 1]


def test_configs_without_enough_training_trades_are_not_selected():
    runs = [_run("thin", 0.01, 0.01, n_trades=3), _run("ok", 0.001, 0.0, seed=3)]
    assert {c["choice"] for c in walk_forward(plan(), runs).choices} == {"ok"}
    wf = walk_forward(plan(), [_run("thin", 0.01, 0.01, n_trades=3)])
    assert all(c["choice"] is None for c in wf.choices) and (wf.oos == 0).all()


def test_evidence_feeds_gates_and_noise_fails():
    runs = [_run(f"c{i}", 0.0, 0.0, seed=i) for i in range(6)]
    wf = walk_forward(plan(), runs)
    ev = evidence(plan(), wf, runs, n_trials=84)
    assert set(ev) == {"n_trades", "oos_sharpe_annual", "deflated_sharpe", "pbo", "max_drawdown",
                       "walk_forward_positive_share", "bootstrap_sharpe_lower"}
    assert not gate(plan(), wf, runs, GateConfig(), n_trials=84).passed


def test_combine_and_drawdown():
    a = pd.Series([0.01, -0.02], index=pd.date_range("2020-01-01", periods=2, tz="UTC"))
    b = pd.Series([0.03], index=pd.date_range("2020-01-02", periods=1, tz="UTC"))
    assert combine([a, b]).tolist() == pytest.approx([0.005, 0.005])
    assert max_drawdown(pd.Series([0.1, -0.5, 0.2])) == pytest.approx(0.5)


def test_run_config_records_a_trial(tmp_path):
    with pytest.raises(ValidationError, match="too short"):
        plan(dev_start="2020-01-01", dev_end="2020-01-04", walk_forward=WalkForward(train_months=6, test_months=1,
                                                                                     step_months=1))
    p = plan(dev_start="2020-01-01", dev_end="2020-08-31", walk_forward=WalkForward(train_months=6, test_months=1,
                                                                                    step_months=1))
    reg = ExperimentRegistry(tmp_path / "reg.jsonl")
    bars = synthetic_bars(CRYPTO["BTCUSDT"].instrument(), BT, 3000, price=30_000, seed=5)
    r = run_config(p, CRYPTO["BTCUSDT"], bars, "ema_trend", {"fast": 12, "slow": 48, "reward_risk": 2.0,
                                                               "signal_minutes": 15}, registry=reg)
    assert len(r.daily) >= 1 and reg.n_trials("active/ema_trend") == 1
    reg.verify()


def test_run_plan_end_to_end_on_a_synthetic_catalog(tmp_path, monkeypatch):
    from hedge_fund.trading import families, runner
    from hedge_fund.trading.data.catalog import Catalog

    tiny = families.Family("ema_trend", "trend", families.EmaTrend, families.EmaTrendConfig,
                           {"fast": [12], "slow": [48, 96], "reward_risk": [2.0], "signal_minutes": [60]})
    monkeypatch.setitem(families.FAMILIES, "ema_trend", tiny)
    cat = Catalog(tmp_path / "cat")
    inst = CRYPTO["BTCUSDT"].instrument()
    cat.write_instrument(inst)
    bars = synthetic_bars(inst, BT, 60 * 24 * 213, price=30_000, seed=9, start="2020-01-01")
    df = pd.DataFrame({"open": [float(b.open) for b in bars], "high": [float(b.high) for b in bars],
                       "low": [float(b.low) for b in bars], "close": [float(b.close) for b in bars],
                       "volume": [float(b.volume) for b in bars]},
                      index=pd.to_datetime([b.ts_event - 60_000_000_000 for b in bars], utc=True))
    cat.write_bars(inst, df, "all")
    p = ResearchPlan(plan_id="e2e", families=("ema_trend",), instruments=("BTCUSDT.BINANCE",), dev_start="2020-01-01",
                     dev_end="2020-07-31", reserve_start="2020-09-01", min_train_trades=1,
                     walk_forward=WalkForward(train_months=6, test_months=1, step_months=1))
    out = tmp_path / "out"
    s1 = runner.run_plan(p, out, catalog_path=str(cat.path), prior_registries=[])
    assert s1["n_trials"] == 2 and s1["plan_hash"] == p.plan_hash()          # stressed reruns are not new trials
    assert len(ExperimentRegistry(out / "experiments.jsonl").records()) == 3    # but every run is recorded
    (line,) = s1["lines"]
    assert set(line["checks"]) >= {"min_trades", "deflated_sharpe", "pbo", "walk_forward"} and "2.0" in line["cost_stress"]
    assert set(s1["combinations"]) == {"family:ema_trend", "all"}
    reg = ExperimentRegistry(out / "experiments.jsonl")
    n = len(reg.records())
    s2 = runner.run_plan(p, out, catalog_path=str(cat.path), prior_registries=[])                  # cached: no rerun, no new trials
    assert len(reg.records()) == n and s2["lines"][0]["values"] == line["values"]
    reg.verify()
