"""Performance changes must not change results: whole-backtest equivalence checks.

Two rewrites are covered: `marks_for` (windowed, vectorized read) and the
per-session memoization of as-of views in SimulatedExecution. Each scenario
runs the full backtester as it is now and again with both original paths
restored (full-history marks, no memoization), and requires identical trades, fills,
rejections, liquidations, NAV, cash, exposure and metrics (the complete
serialized result), plus determinism across repeated runs.
"""

from __future__ import annotations

import functools

import pytest

from hedge_fund.systematic import backtest as bt_mod
from hedge_fund.systematic import decision as decision_mod
from hedge_fund.systematic.backtest import SystematicBacktester
from hedge_fund.systematic.execution import CostModel, SimulatedExecution
from hedge_fund.systematic.small_account import SmallAccountConfig, small_account_universe
from hedge_fund.systematic.strategies import MeanReversion, TimeSeriesMomentum
from hedge_fund.systematic.test_backtest import Fixed, build, config
from hedge_fund.systematic.testing import RawBar, SyntheticMarket, weekdays


def reference_marks_for(view, symbols):
    """The original implementation, verbatim."""
    symbols = sorted(symbols)
    if not symbols:
        return {}
    closes = view.bars("close", tickers=symbols, tradable_only=False)
    tradable = view.tradable(tickers=symbols)
    out = {}
    for s in symbols:
        good = closes[s].where(tradable[s]).dropna()
        out[s] = float(good.iloc[-1]) if len(good) else float(closes[s].dropna().iloc[-1])
    return out


def halting_panel():
    """Names with zero-volume halts longer than the 21-session window."""
    from hedge_fund.systematic import MarketPanel

    days = weekdays("2021-01-04", "2022-12-30")
    m = SyntheticMarket()
    m.add_series("SPY", days, SyntheticMarket.random_walk(days, seed=3, drift=0.0003, vol=0.01), volume=5e7)
    for i, s in enumerate(["H1", "H2", "H3", "H4"]):
        walk = SyntheticMarket.random_walk(days, seed=40 + i, drift=0.0006, vol=0.02)
        lo, hi = 200 + 40 * i, 200 + 40 * i + (5, 30, 60, 3)[i]
        m.add(s, [RawBar(d, p, p * 1.01, p * 0.99, p, 0 if lo <= k < hi else 1e6) for k, (d, p) in
                  enumerate(zip(days, walk))])
    return MarketPanel.build(m, ["SPY", "H1", "H2", "H3", "H4"], days[0], days[-1])


SMALL = CostModel(commission_min=1.0, half_spread_bps=10, impact_coef=0.1, max_participation=0.05)
SCENARIOS = {
    "tsmom_100k": lambda: (build(), [TimeSeriesMomentum(lookback=126, skip=5, vol_lookback=40, exclude=("SPY",))],
                           config(), {}),
    "meanrev_weekly_eur200": lambda: (build(), [MeanReversion(exclude=("SPY",))],
                                      config(capital=200.0, costs=SMALL, rebalance="weekly"),
                                      {"instruments": small_account_universe([], SmallAccountConfig())}),
    "delisted_holding": lambda: (build(), [Fixed(hold=("GONE", "A"))], config(start="2022-03-01", end="2022-12-30"),
                                 {}),
    "halts_longer_than_window": lambda: (halting_panel(), [TimeSeriesMomentum(lookback=60, skip=0, vol_lookback=20,
                                                                              exclude=("SPY",))],
                                         config(start="2021-06-01", end="2022-12-30", rebalance="weekly"), {}),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_marks_for_rewrite_is_result_identical(name, monkeypatch):
    panel, strategies, cfg, kw = SCENARIOS[name]()
    new = SystematicBacktester(panel, strategies, cfg, **kw).run().to_dict()
    again = SystematicBacktester(panel, strategies, cfg, **kw).run().to_dict()
    monkeypatch.setattr(bt_mod, "marks_for", reference_marks_for)
    monkeypatch.setattr(decision_mod, "marks_for", reference_marks_for)
    monkeypatch.setattr(bt_mod, "SimulatedExecution", functools.partial(SimulatedExecution, memoize=False))
    old = SystematicBacktester(panel, strategies, cfg, **kw).run().to_dict()
    assert new == again                                         # deterministic
    for key in ("trades", "rejected", "liquidations", "equity", "cash", "exposure", "net_exposure", "metrics",
                "decisions", "final_positions", "pnl_by_symbol", "reconciliation_error"):
        assert new[key] == old[key], key
    assert new == old
    assert new["trades"] or name == "halts_longer_than_window"
