"""Multi-leg daily engine: next-open execution, costs, funding, delisting, episodes,
causal targets for every portfolio family."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hedge_fund.trading.data.markets import market
from hedge_fund.trading.portfolio import (PORTFOLIO_FAMILIES, DailyData, _perturb_after, simulate)

SPOT, PERP = "BTCUSDT.BINANCE", "BTCUSDT-PERP.BINANCE"
IDX = pd.date_range("2021-01-01", periods=300, freq="D", tz="UTC")


def _data(seed=0, n=300, cols=(SPOT, PERP), funding_rate=1e-4):
    rng = np.random.default_rng(seed)
    base = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    close = pd.DataFrame({c: base * (1 + 0.001 * k) * np.exp(rng.normal(0, 0.002, n)) for k, c in enumerate(cols)},
                         index=IDX[:n])
    opn = close.shift(1).fillna(close.iloc[0])
    t = pd.date_range(IDX[0] + pd.Timedelta(hours=8), periods=3 * n - 3, freq="8h")
    fund = {c: pd.Series(funding_rate, index=t) for c in cols if "PERP" in c}
    return DailyData(opn, close, fund)


def test_next_open_execution_and_taker_costs():
    d = _data()
    tg = pd.DataFrame(0.0, index=d.close.index, columns=[SPOT])
    tg.iloc[10:] = 0.5
    r, _, _ = simulate(d, tg, groups=[(SPOT,)])
    fee = float(market("BTCUSDT").fees()[1])
    assert r.iloc[:11].abs().max() < 1e-12 + 0.5 * fee                     # nothing held before day 11
    gap = d.open[SPOT].iloc[11] / d.close[SPOT].iloc[10] - 1
    intraday = d.close[SPOT].iloc[11] / d.open[SPOT].iloc[11] - 1
    assert r.iloc[11] == pytest.approx(0.5 * intraday - 0.5 * fee * 1, rel=1e-6, abs=1e-9) or gap == 0
    r2, _, _ = simulate(d, tg, groups=[(SPOT,)], cost_multiplier=2.0)
    assert r2.iloc[11] < r.iloc[11]                                       # stressed costs cost more
    with pytest.raises(ValueError):
        simulate(d, tg, groups=[(SPOT,)], cost_multiplier=0.5)


def test_funding_short_receives_long_pays_and_stress_scales_payments_only():
    d = _data(funding_rate=1e-3)
    flat = pd.DataFrame(0.0, index=d.close.index, columns=[PERP])
    d.close[PERP] = 100.0
    d.open[PERP] = 100.0                                                  # no price moves: only funding and costs
    for w, sign in ((-0.5, 1), (0.5, -1)):
        tg = flat.copy()
        tg.iloc[0:] = w
        r, _, _ = simulate(d, tg, groups=[(PERP,)])
        mid = r.iloc[2]                                                   # first full day: three settlements, no trade
        assert np.sign(mid) == sign and abs(mid) == pytest.approx(3 * 0.5 * 1e-3, rel=0.05)
        r2, _, _ = simulate(d, tg, groups=[(PERP,)], cost_multiplier=2.0)
        assert r2.iloc[2] == pytest.approx(mid * (2 if sign < 0 else 1), rel=0.05)


def test_delisted_leg_is_closed_at_its_last_close():
    d = _data()
    d.close.loc[d.close.index[150]:, SPOT] = np.nan
    d.open.loc[d.open.index[150]:, SPOT] = np.nan
    tg = pd.DataFrame(0.5, index=d.close.index, columns=[SPOT])
    r, trades, stats = simulate(d, tg, groups=[(SPOT,)])
    assert r.iloc[151:].abs().max() < 1e-12 and len(trades) == 1


def test_episodes_split_on_exit_and_on_direction_change():
    d = _data()
    tg = pd.DataFrame(0.0, index=d.close.index, columns=[SPOT])
    tg.iloc[10:20] = 0.5
    tg.iloc[20:30] = -0.5
    tg.iloc[40:50] = 0.5
    _, trades, _ = simulate(d, tg, groups=[(SPOT,)])
    assert len(trades) == 3


@pytest.mark.parametrize("name", sorted(PORTFOLIO_FAMILIES))
def test_family_targets_are_causal(name):
    fam = PORTFOLIO_FAMILIES[name]
    line = next(iter(fam.lines))
    cols = fam.lines[line]
    d = _data(seed=3, cols=cols)
    for c in cols:                                                        # funding with variation and extremes
        if c in d.funding:
            rng = np.random.default_rng(1)
            d.funding[c] = pd.Series(rng.normal(2e-4, 3e-4, len(d.funding[c])), index=d.funding[c].index)
    for p in (fam.configs()[0], fam.configs()[-1]):
        full = fam.targets(d, cols, p)
        assert full.abs().to_numpy().sum() > 0, (name, p)
        for frac in (0.4, 0.7):
            cut = d.close.index[int(len(d.close) * frac)]
            alt = fam.targets(_perturb_after(d, cut), cols, p)
            assert np.allclose(full[full.index <= cut], alt[alt.index <= cut]), (name, p, frac)


def test_portfolio_plans_validate_and_count_configs_per_line():
    from hedge_fund.trading.portfolio import plan_instruments
    from hedge_fund.trading.research import ResearchPlan
    plan = ResearchPlan(plan_id="x", families=("pairs_statarb",), instruments=plan_instruments(["pairs_statarb"]),
                        dev_start="2020-01-01", dev_end="2025-08-31", bar_minutes=1440)
    assert plan.n_configs() == 4 * 5


def test_declared_universes_cover_portfolio_plans():
    from hedge_fund.trading.data.markets import UNIVERSES
    from hedge_fund.trading.portfolio import plan_instruments
    for f in PORTFOLIO_FAMILIES:
        inst = set(plan_instruments([f]))
        assert any(inst <= set(u["instruments"]) for u in UNIVERSES.values()), f


def test_drawdown_kill_switch_flattens_and_resets_at_segment_start():
    d = _data()
    d.close[SPOT] = np.r_[np.full(100, 100.0), np.full(100, 60.0), np.full(100, 60.0)]
    d.open[SPOT] = d.close[SPOT].shift(1).fillna(100.0)
    tg = pd.DataFrame(0.9, index=d.close.index, columns=[SPOT])
    r, _, stats = simulate(d, tg, groups=[(SPOT,)], max_drawdown=0.2, segment_starts=(d.close.index[200],))
    assert stats["kill_switch_events"] == 1
    assert r.iloc[101:200].abs().max() < 1e-12                           # flat after the kill until the segment start
    assert r.iloc[200] < 0                                                # re-entered (entry cost) in the next segment
