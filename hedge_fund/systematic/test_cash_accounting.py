"""Cash accounting: commissions are reserved before a fill; cash-only accounts never overdraw.

Regression for the Phase A EUR 200 finding: with a 1.00 minimum commission a
sell whose proceeds were below the commission (or a buy sized on stale cash)
was applied anyway and left cash negative. Rejected orders must change
nothing — no position, no cash, no fee, no journal entry.

Property tests are seeded randomized sweeps (the `hypothesis` package is not a
project dependency): every seed is fixed so a failure is reproducible.
"""

from __future__ import annotations

import copy
import random

import pytest

from hedge_fund.brokers.paper import PaperBroker
from hedge_fund.core import OrderRequest, OrderStatus
from hedge_fund.core.instruments import InstrumentRegistry
from hedge_fund.core.orders import FillEvent
from hedge_fund.systematic import MarketPanel
from hedge_fund.systematic.backtest import SystematicBacktester
from hedge_fund.systematic.execution import CostModel, FillTiming, SimulatedExecution
from hedge_fund.systematic.ledger import CASH_TOLERANCE, InsufficientCash, Ledger
from hedge_fund.systematic.small_account import SmallAccountConfig, small_account_universe
from hedge_fund.systematic.strategies import MeanReversion, TimeSeriesMomentum
from hedge_fund.systematic.test_backtest import build, config
from hedge_fund.systematic.testing import SyntheticMarket, weekdays

SMALL_COSTS = CostModel(commission_min=1.0, half_spread_bps=10, impact_coef=0.1, max_participation=0.05)


def fill(side, qty, price, commission=0.0, symbol="AAA", session="2023-02-02"):
    return FillEvent(client_order_id=f"{side}-{qty}-{price}", symbol=symbol, side=side, quantity=qty, price=price,
                     session=session, mid_price=price, commission=commission)


def ledger(cash, **kw):
    return Ledger(cash=cash, instruments=InstrumentRegistry(default_fractional=True), **kw)


def snapshot(led: Ledger):
    return (led.cash, dict(led.positions), list(led.flows), list(led.fills))


# -- regression -----------------------------------------------------------------


def test_regression_sell_whose_proceeds_do_not_cover_the_commission_is_refused():
    """The Phase A case: 0.72 of proceeds, 1.00 commission, cash already ~0."""
    led = ledger(1.0)
    led.apply_fill(fill("buy", 0.72, 1.0, commission=0.28))          # cash -> 0
    before = snapshot(led)
    with pytest.raises(InsufficientCash, match="insufficient cash"):
        led.apply_fill(fill("sell", 0.72, 1.0, commission=1.0))
    assert snapshot(led) == before                                    # nothing changed, no fee charged
    assert led.reconcile() == pytest.approx(0.0, abs=1e-12)


def test_regression_buy_that_ignores_its_commission_is_refused():
    led = ledger(100.0)
    before = snapshot(led)
    with pytest.raises(InsufficientCash):
        led.apply_fill(fill("buy", 1.0, 99.5, commission=1.0))       # 99.5 + 1.0 > 100
    assert snapshot(led) == before


def test_regression_eur200_backtest_never_overdraws():
    """Same synthetic EUR 200 run that produced cash of -0.28 before the fix."""
    r = SystematicBacktester(build(), [MeanReversion(exclude=("SPY",))],
                             config(capital=200.0, costs=SMALL_COSTS, rebalance="weekly"),
                             instruments=small_account_universe([], SmallAccountConfig())).run()
    assert r.cash is not None and len(r.cash) == len(r.sessions)
    assert r.cash.min() >= -CASH_TOLERANCE
    assert any("insufficient cash" in str(x["reason"]) for x in r.rejected)
    assert abs(r.reconciliation_error) < 1e-9
    assert r.equity.min() >= -CASH_TOLERANCE


# -- boundaries -----------------------------------------------------------------


def test_boundary_buy_using_exactly_all_cash_including_commission_is_allowed():
    led = ledger(101.0)
    led.apply_fill(fill("buy", 1.0, 100.0, commission=1.0))
    assert led.cash == pytest.approx(0.0, abs=1e-12) and led.quantity("AAA") == 1.0


def test_boundary_one_cent_short_is_refused():
    led = ledger(100.99)
    with pytest.raises(InsufficientCash):
        led.apply_fill(fill("buy", 1.0, 100.0, commission=1.0))
    assert led.cash == 100.99 and led.positions == {} and led.fills == []


def test_boundary_sell_whose_proceeds_exactly_equal_the_commission_with_zero_cash():
    led = ledger(2.0)
    led.apply_fill(fill("buy", 1.0, 1.0, commission=1.0))           # cash exactly 0
    assert led.cash == pytest.approx(0.0, abs=1e-12)
    led.apply_fill(fill("sell", 1.0, 1.0, commission=1.0))          # +1 - 1 = 0: allowed
    assert led.cash == pytest.approx(0.0, abs=1e-12) and led.positions == {}


def test_margin_account_may_go_negative_but_cash_only_may_not():
    margin = ledger(10.0, allow_short=True, allow_negative_cash=True)
    margin.apply_fill(fill("buy", 1.0, 20.0, commission=1.0))
    assert margin.cash == pytest.approx(-11.0)
    assert margin.reconcile() == pytest.approx(0.0)


def test_preview_is_cumulative_and_never_mutates():
    led = ledger(50.0)
    before = snapshot(led)
    assert led.preview([fill("buy", 1.0, 24.0, commission=1.0)]) is None
    assert "insufficient cash" in led.preview([fill("buy", 1.0, 24.0, commission=1.0),
                                               fill("buy", 1.0, 24.5, commission=1.0)])
    assert "short" in led.preview([fill("sell", 1.0, 10.0)])
    assert snapshot(led) == before


# -- backtester: rejected orders change nothing ------------------------------------


def test_backtester_rejects_unaffordable_sell_atomically():
    bt = SystematicBacktester(build(), [MeanReversion(exclude=("SPY",))], config(capital=200.0, costs=SMALL_COSTS),
                              instruments=small_account_universe([], SmallAccountConfig()))
    session = build().sessions_through("2022-06-30")[-1]
    price = build().as_of(session).close("A")
    # hold ~0.50 worth of A, cash 0.10: a 1.00-commission sell would overdraw
    led = Ledger(cash=0.10, instruments=bt.instruments, positions={"A": 0.5 / price})
    order = OrderRequest(symbol="A", side="sell", quantity=0.5 / price, decision_session="2022-06-29",
                         reference_price=price, strategy="t")
    before = snapshot(led)
    trades, rejected = bt._execute([order], session, led)
    assert trades == [] and len(rejected) == 1 and "insufficient cash" in rejected[0]["reason"]
    assert snapshot(led) == before


def test_fit_cash_shrinks_a_buy_so_notional_plus_commission_fits():
    sa = SmallAccountConfig()
    bt = SystematicBacktester(build(), [MeanReversion(exclude=("SPY",))],
                              config(capital=200.0, costs=SMALL_COSTS), instruments=small_account_universe([], sa))
    session = build().sessions_through("2022-06-30")[-1]
    price = build().as_of(session).close("A")
    led = Ledger(cash=50.0, instruments=bt.instruments)
    order = OrderRequest(symbol="A", side="buy", quantity=100.0 / price, decision_session="2022-06-29",
                         reference_price=price, strategy="t")
    trades, rejected = bt._execute([order], session, led)
    assert rejected == [] and len(trades) == 1
    assert trades[0]["quantity"] < order.quantity and trades[0]["commission"] == pytest.approx(1.0)
    assert 0.0 <= led.cash < 1.0


def test_fit_cash_rejects_when_only_the_commission_fits():
    sa = SmallAccountConfig()
    bt = SystematicBacktester(build(), [MeanReversion(exclude=("SPY",))],
                              config(capital=200.0, costs=SMALL_COSTS), instruments=small_account_universe([], sa))
    session = build().sessions_through("2022-06-30")[-1]
    price = build().as_of(session).close("A")
    led = Ledger(cash=0.99, instruments=bt.instruments)
    order = OrderRequest(symbol="A", side="buy", quantity=10.0 / price, decision_session="2022-06-29",
                         reference_price=price, strategy="t")
    before = snapshot(led)
    trades, rejected = bt._execute([order], session, led)
    assert trades == [] and rejected[0]["reason"] == "insufficient cash"
    assert snapshot(led) == before


# -- paper broker ---------------------------------------------------------------------

DAYS = weekdays("2023-01-02", "2023-03-31")
D0, D1 = "2023-02-01", "2023-02-02"


@pytest.fixture(scope="module")
def flat_panel():
    m = SyntheticMarket()
    m.add_series("SPY", DAYS, [400.0] * len(DAYS), volume=10_000_000)
    m.add_series("AAA", DAYS, [1.0] * len(DAYS), volume=10_000_000)
    return MarketPanel.build(m, ["SPY", "AAA"], DAYS[0], DAYS[-1])


def paper(panel, cash):
    costs = CostModel(commission_min=1.0, half_spread_bps=0, impact_coef=0, max_participation=1.0)
    return PaperBroker(cash, SimulatedExecution(panel, costs, FillTiming.NEXT_OPEN,
                                                InstrumentRegistry(default_fractional=True)))


def test_paper_broker_refuses_sell_whose_commission_would_overdraw(flat_panel):
    b = paper(flat_panel, 1.5)
    b.book.apply_fill(fill("buy", 0.5, 1.0, commission=1.0))         # cash 0.0, hold 0.5 @ 1.0
    sell = OrderRequest(symbol="AAA", side="sell", quantity=0.5, decision_session=D0, reference_price=1.0,
                        strategy="t")
    state = b.submit(sell)
    assert state.status is OrderStatus.REJECTED and "commission" in state.reject_reason
    assert b.cash() == pytest.approx(0.0, abs=1e-12) and b.positions() == {"AAA": 0.5}


def test_paper_broker_execution_check_reserves_commission(flat_panel):
    b = paper(flat_panel, 10.0)
    buy = OrderRequest(symbol="AAA", side="buy", quantity=9.5, decision_session=D0, reference_price=1.0,
                       strategy="t")
    assert b.submit(buy).status is OrderStatus.ACCEPTED                  # 9.5 + 1 <= 11 (submit slack)
    before = (b.cash(), b.positions(), list(b.book.flows))
    (state,) = b.process(D1)                                             # 9.5 + 1.0 > 10 at execution
    assert state.status is OrderStatus.CANCELLED and "insufficient cash" in state.reject_reason
    assert (b.cash(), b.positions(), list(b.book.flows)) == before


# -- property-based (seeded) --------------------------------------------------------------


@pytest.mark.parametrize("seed", range(40))
def test_property_random_fill_sequences_never_overdraw_and_rejections_are_atomic(seed):
    rng = random.Random(seed)
    led = ledger(rng.choice([5.0, 50.0, 200.0, 1_000.0]))
    symbols = ["AAA", "BBB", "CCC"]
    for _ in range(200):
        sym = rng.choice(symbols)
        held = led.quantity(sym)
        side = "sell" if held > 0 and rng.random() < 0.5 else "buy"
        price = rng.uniform(0.5, 150.0)
        qty = round(held * rng.uniform(0.05, 1.0), 6) if side == "sell" else round(rng.uniform(0.001, 3.0), 6)
        if qty <= 0:
            continue
        commission = rng.choice([0.0, 0.5, 1.0, rng.uniform(0, 2)])
        f = fill(side, qty, price, commission=commission, symbol=sym)
        before = copy.deepcopy(snapshot(led))
        expected_reason = led.preview([f])
        try:
            led.apply_fill(f)
        except ValueError:
            assert expected_reason is not None
            assert snapshot(led) == before                       # positions, cash, fees, journal unchanged
        else:
            assert expected_reason is None
            assert led.cash == pytest.approx(before[0] + led.cash_delta(f))
        assert led.cash >= -CASH_TOLERANCE
        assert all(q > 0 for q in led.positions.values())
        assert led.reconcile() == pytest.approx(0.0, abs=1e-7)


@pytest.mark.parametrize("seed,capital,commission_min,rebalance", [
    (0, 200.0, 1.0, "weekly"), (1, 50.0, 1.0, "weekly"), (2, 200.0, 2.5, "monthly"),
    (3, 20.0, 1.0, "weekly"), (4, 1_000.0, 5.0, "weekly"), (5, 200.0, 0.0, "monthly"),
])
def test_property_small_account_backtests_never_overdraw(seed, capital, commission_min, rebalance):
    m = SyntheticMarket()
    days = weekdays("2021-01-04", "2022-12-30")
    m.add_series("SPY", days, SyntheticMarket.random_walk(days, seed=seed, drift=0.0002, vol=0.01),
                 volume=50_000_000, gap=0.001)
    rng = random.Random(seed)
    names = ["SPY"] + [f"S{i}" for i in range(4)]
    for i, s in enumerate(names[1:]):
        m.add_series(s, days, SyntheticMarket.random_walk(days, seed=100 * seed + i, drift=rng.uniform(-0.001, 0.001),
                                                          vol=rng.uniform(0.01, 0.04)),
                     volume=rng.choice([50_000, 2_000_000]), gap=0.003)
    panel = MarketPanel.build(m, names, days[0], days[-1])
    costs = CostModel(commission_min=commission_min, half_spread_bps=10, impact_coef=0.1, max_participation=0.05)
    strat = (MeanReversion(exclude=("SPY",)) if seed % 2 else
             TimeSeriesMomentum(lookback=60, skip=0, vol_lookback=20, exclude=("SPY",)))
    r = SystematicBacktester(panel, [strat], config(start="2021-06-01", end=days[-1], capital=capital, costs=costs,
                                                    rebalance=rebalance),
                             instruments=small_account_universe([], SmallAccountConfig())).run()
    assert r.cash.min() >= -CASH_TOLERANCE                         # invariant: cash-only account
    assert r.equity.min() >= -CASH_TOLERANCE
    assert abs(r.reconciliation_error) < 1e-8
    assert all(t["fill_session"] > t["decision_session"] for t in r.trades)
    for x in r.rejected:
        assert x["reason"]


# -- marks: the windowed read equals the full-history definition ------------------------------


def _reference_marks(view, symbols):
    closes = view.bars("close", tickers=sorted(symbols), tradable_only=False)
    tradable = view.tradable(tickers=sorted(symbols))
    out = {}
    for s in sorted(symbols):
        good = closes[s].where(tradable[s]).dropna()
        out[s] = float(good.iloc[-1]) if len(good) else float(closes[s].dropna().iloc[-1])
    return out


@pytest.mark.parametrize("seed", range(4))
def test_marks_for_matches_full_history_definition_with_halts(seed):
    from hedge_fund.systematic.decision import marks_for
    from hedge_fund.systematic.testing import RawBar

    rng = random.Random(seed)
    days = weekdays("2022-01-03", "2022-09-30")
    m = SyntheticMarket()
    m.add_series("SPY", days, SyntheticMarket.random_walk(days, seed=seed, drift=0.0, vol=0.01), volume=1e7)
    names = [f"H{i}" for i in range(6)]
    for i, s in enumerate(names):
        walk = SyntheticMarket.random_walk(days, seed=50 + 10 * seed + i, drift=0.0, vol=0.02)
        halt_from = rng.randrange(20, len(days))
        halt_len = rng.choice([1, 5, 30, 200])                 # includes halts longer than the window
        m.add(s, [RawBar(d, p, p * 1.01, p * 0.99, p, 0 if halt_from <= k < halt_from + halt_len else 1e5)
                  for k, (d, p) in enumerate(zip(days, walk))])
    panel = MarketPanel.build(m, ["SPY"] + names, days[0], days[-1])
    for d in days[1::7]:
        view = panel.as_of(d)
        assert marks_for(view, names) == _reference_marks(view, names)
