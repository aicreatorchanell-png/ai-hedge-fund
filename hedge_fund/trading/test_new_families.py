"""H-FUNDING-CROWDING and H-VOL-SCALED-TREND building blocks: point-in-time funding,
funding cash flows, the trend rules, perpetual specs behind the crypto fence, daily bars."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from nautilus_trader.model.data import BarType

from hedge_fund.trading.backtest import funding_costs, run_backtest
from hedge_fund.trading.families import build
from hedge_fund.trading.governor import TradeRiskConfig
from hedge_fund.trading.synthetic import synthetic_bars, synthetic_funding
from hedge_fund.trading.test_trading import BT, INST, MARGIN

BARS = synthetic_bars(INST, BT, 6000, price=30_000, seed=11)
FUNDING = synthetic_funding(n=200, seed=3)
P = {"quantile": 0.95, "atr_stop": 2.0, "max_hold_bars": 24, "signal_minutes": 60, "min_history": 30}


def _fc(series, **kw):
    return build("funding_crowding", {**P, "funding_series": series, **kw}, instrument_id=INST.id, bar_type=BT,
                 risk=TradeRiskConfig(), allow_short=True)


def _decisions(r, cut=None):
    return [(b["ts"], b["side"]) for b in r.brackets if cut is None or b["ts"] <= cut]


def test_funding_crowding_fades_extremes_in_both_directions():
    r = run_backtest(INST, BARS, _fc(FUNDING), MARGIN)
    assert {"BUY", "SELL"} <= {b["side"] for b in r.brackets}
    rate_at = dict(FUNDING)
    for b in r.brackets:                                  # each decision follows an extreme settlement
        latest = max(t for t in rate_at if t <= b["ts"])
        assert (rate_at[latest] > 0) == (b["side"] == "SELL")


def test_funding_after_the_decision_never_changes_it():
    cut = BARS[3500].ts_event
    scrambled = tuple((t, -r * 3 if t > cut else r) for t, r in FUNDING)
    a = run_backtest(INST, BARS, _fc(FUNDING), MARGIN)
    b = run_backtest(INST, BARS, _fc(scrambled), MARGIN)
    assert _decisions(a, cut) == _decisions(b, cut) and _decisions(a) != _decisions(b)


def test_a_settlement_is_used_only_from_the_first_signal_bar_closing_after_it():
    # one extreme settlement 1 ms after the 01:00 bar closes on day 2: the 01:00 bar cannot see it
    t0 = pd.Timestamp("2020-01-02 01:00", tz="UTC").value
    base = tuple((t, 1e-4) for t, _ in FUNDING if t < t0)
    series = base + ((t0 + 1_000_000, 5e-3),)
    r = run_backtest(INST, BARS, _fc(series, min_history=10), MARGIN)
    assert r.brackets and r.brackets[0]["ts"] == t0 + 3_600_000_000_000 and r.brackets[0]["side"] == "SELL"


def test_funding_cash_flows_long_pays_short_receives_and_stress_scales_payments_only():
    t = pd.date_range("2024-01-01 08:00", periods=6, freq="8h", tz="UTC")
    rates = pd.Series([1e-4, 1e-4, -2e-4, 1e-4, 1e-4, 1e-4], index=t)
    pos = pd.DataFrame({"ts_opened": [t[0] - pd.Timedelta("1h")] * 2, "ts_closed": [t[3] + pd.Timedelta("1h")] * 2,
                        "entry": ["BUY", "SELL"], "peak_qty": [2.0, 2.0], "avg_px_open": [100.0, 100.0]})
    long_only, short_only = funding_costs(pos.iloc[:1], rates, 0), funding_costs(pos.iloc[1:], rates, 0)
    assert long_only.sum() == pytest.approx(200 * (1e-4 * 3 - 2e-4))
    assert short_only.sum() == pytest.approx(-200 * (1e-4 * 3 - 2e-4))
    assert funding_costs(pos.iloc[:1], rates, 0, 2.0).sum() == pytest.approx(200 * (2e-4 * 3 - 2e-4))
    open_pos = pos.iloc[:1].assign(ts_closed=[pd.NaT])     # open: charged up to the end
    assert funding_costs(open_pos, rates, t[-1].value).sum() == pytest.approx(200 * (1e-4 * 5 - 2e-4))


def _vt(**kw):
    return build("vol_managed_trend", {"lookback": 60, "vol_stop": 3.0, **kw}, instrument_id=INST.id, bar_type=BT,
                 risk=TradeRiskConfig(), allow_short=True)


def test_vol_managed_trend_follows_direction_and_scales_stop_with_volatility():
    r = run_backtest(INST, BARS, _vt(), MARGIN)
    assert {"BUY", "SELL"} <= {b["side"] for b in r.brackets}
    closes = {b.ts_event: float(b.close) for b in BARS}
    ts = sorted(closes)
    import numpy as np
    for b in r.brackets[:20]:
        i = ts.index(b["ts"])
        px = [closes[x] for x in ts[i - 60:i + 1]]
        assert (px[-1] > px[0]) == (b["side"] == "BUY")
        vol = np.diff(np.log(px[-21:])).std(ddof=1)
        assert abs(b["stop"] - b["reference"]) == pytest.approx(3.0 * vol * px[-1], rel=1e-3, abs=0.02)


def test_vol_managed_trend_flattens_on_reversal():
    s = _vt(lookback=20)
    run_backtest(INST, BARS, s, MARGIN)
    assert any(e["reason"] == "trend reversal" for e in s.exits)


def test_frozen_plan_hash_unchanged_and_daily_plans_hash_differently():
    from hedge_fund.trading.research import ResearchPlan
    doc = json.loads((Path(__file__).resolve().parents[2] / "runs/active/research/plan_crypto_v1.json").read_text())
    plan = ResearchPlan.model_validate(doc["plan"])
    assert plan.plan_hash() == doc["plan_hash"]
    assert plan.model_copy(update={"bar_minutes": 1440}).plan_hash() != doc["plan_hash"]
    with pytest.raises(ValueError):
        ResearchPlan.model_validate({**doc["plan"], "bar_minutes": 60})


def test_perpetuals_are_margin_instruments_behind_the_crypto_fence():
    from hedge_fund.trading.data.markets import PERPS, market
    from hedge_fund.validation.holdout_guard import HoldoutAccessDenied, market_data_fence
    spec = market("BTCUSDT-PERP")
    assert spec.perpetual and spec.margin and spec.asset_class == "crypto" and spec.funding_symbol == "BTCUSDT"
    assert all(float(s.instrument().taker_fee) > float(s.instrument().maker_fee) > 0 for s in PERPS.values())
    with pytest.raises(HoldoutAccessDenied):
        market_data_fence("2025-09-01", "2025-09-01", spec.asset_class)
    assert not market("BTCUSDT").margin and market("EURUSD").margin


def test_daily_bars_from_hours_merge_weekend_hours_into_monday():
    from hedge_fund.trading.data.dukascopy import daily_from_hours
    idx = pd.date_range("2024-01-05 20:00", "2024-01-08 02:00", freq="1h", tz="UTC")   # Fri .. Mon
    h = pd.DataFrame({"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1.0}, index=idx)
    h.loc[(idx.weekday == 5) | ((idx.weekday == 6) & (idx.hour < 22)), "volume"] = 0.0   # closed: padding
    h.loc[idx[-1], "close"] = 1.05
    d = daily_from_hours(h)
    assert list(d.index.weekday) == [4, 0] and d.iloc[1]["volume"] == 2 + 3 and d.iloc[1]["close"] == 1.05


def test_catalog_daily_bar_type():
    from hedge_fund.trading.data.catalog import bar_type
    assert str(bar_type("EURUSD.DUKASCOPY", 1440)) == "EURUSD.DUKASCOPY-1-DAY-LAST-EXTERNAL"


def test_index_cfds_are_cfd_instruments_without_a_base_leg():
    from nautilus_trader.model.instruments import Cfd

    from hedge_fund.trading.data.markets import INDICES
    for spec in INDICES.values():
        inst = spec.instrument()
        assert isinstance(inst, Cfd) and str(inst.quote_currency) == spec.quote and float(inst.taker_fee) > 0


def test_non_positive_target_is_refused_not_stranded():
    from nautilus_trader.model.enums import OrderSide

    from hedge_fund.trading.strategy import GuardedConfig, GuardedStrategy

    class Bad(GuardedStrategy):
        def on_signal(self, bar):
            if self.is_flat():
                self.enter(OrderSide.SELL, float(bar.close) * 1.01, -1.0, bar)

    s = Bad(GuardedConfig(instrument_id=INST.id, bar_type=BT, allow_short=True), TradeRiskConfig())
    r = run_backtest(INST, BARS[:300], s, MARGIN)
    assert not r.brackets and r.refusals and r.refusals[0]["reason"] == "non-positive stop or target price"
    assert r.orders.empty


def test_vol_trend_short_target_stays_positive_on_huge_volatility():
    import numpy as np
    from nautilus_trader.model.data import Bar
    rng = np.random.default_rng(1)
    px = 100 * np.exp(np.cumsum(np.concatenate([np.full(80, -0.004), rng.normal(0, 0.2, 400)])))
    bars = []
    for i, c in enumerate(px):
        o = px[i - 1] if i else c
        ts = BARS[0].ts_event + i * 60_000_000_000
        bars.append(Bar(BT, INST.make_price(o), INST.make_price(max(o, c) * 1.001), INST.make_price(min(o, c) * 0.999),
                        INST.make_price(c), INST.make_qty(1000), ts, ts))
    s = _vt(lookback=20, vol_stop=5.0)
    r = run_backtest(INST, bars, s, MARGIN)
    shorts = [b for b in r.brackets if b["side"] == "SELL"]
    assert shorts and all(b["take_profit"] > 0 for b in r.brackets)     # longs whose stop would be <= 0 are refused


def test_usd_base_fx_starts_with_the_same_usd_capital():
    from hedge_fund.trading.data.markets import market
    from hedge_fund.trading.research import ResearchPlan, run_config
    plan = ResearchPlan(plan_id="t", families=("vol_managed_trend",), instruments=("USDJPY.DUKASCOPY",),
                        dev_start="2020-01-01", dev_end="2022-12-31", reserve_start="2025-09-01", bar_minutes=1440)
    spec = market("USDJPY")
    bt = BarType.from_str("USDJPY.DUKASCOPY-1-DAY-LAST-EXTERNAL")
    inst = spec.instrument()
    from nautilus_trader.model.data import Bar
    bars = [Bar(bt, b.open, b.high, b.low, b.close, inst.make_qty(1e9), b.ts_event, b.ts_init)
            for b in synthetic_bars(inst, bt, 200, price=110.0, minutes=1440, vol=0.005, seed=2)]
    r = run_config(plan, spec, bars, "vol_managed_trend", {"lookback": 20, "vol_stop": 2.5})
    assert len(r.trades) > 0                              # 10k USD worth of JPY sizes above the 1000-unit minimum
