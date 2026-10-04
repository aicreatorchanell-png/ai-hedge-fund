"""Research harness: walk-forward search over strategy families with overfitting controls.

    ResearchPlan      frozen and hashed before any run: families (their grids are fixed in
                      families.py), instruments, development window, walk-forward geometry,
                      cost multipliers, risk limits. Its development window must end before
                      `reserve_start` (proposed final out-of-sample year) and before any
                      sealed holdout; load_bars enforces the sealed holdouts as well.
    run_config        one backtest of one configuration over the whole development window,
                      with the governor reset at every walk-forward test start, recorded in
                      the experiment registry (the trial counter). Returns daily returns and
                      closed trades.
    walk_forward      per fold: among configurations with enough training trades, pick the
                      best *training* Sharpe; its *test* returns form the out-of-sample series.
                      Nothing about a test window influences that window's choice.
    combine           equal-weight combination of several out-of-sample series (strategy
                      or market combinations), each chosen on training data only.
    evidence          deflated Sharpe (trial count from the registry), PBO over the
                      configuration matrix, bootstrap Sharpe CI, max drawdown, share of
                      positive test folds, trade count -> validation.gates.evaluate_gates.

A cost-stressed rerun (cost_multiplier 2.0) of every selected configuration must also
pass before anything is called a candidate.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hedge_fund.trading.backtest import apply_financing, financing_costs, funding_costs, run_backtest
from hedge_fund.trading.data.fence import last_research_day
from hedge_fund.trading.families import FAMILIES, build
from hedge_fund.trading.governor import TradeRiskConfig
from hedge_fund.trading.venue import VenueSpec
from hedge_fund.validation.gates import GateConfig, evaluate_gates
from hedge_fund.validation.stats import bootstrap_sharpe_ci, deflated_sharpe, pbo, sharpe


class WalkForward(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    train_months: int = Field(24, ge=6)
    test_months: int = Field(6, ge=1)
    step_months: int = Field(6, ge=1)


class ResearchPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str
    families: tuple[str, ...]
    instruments: tuple[str, ...]
    dev_start: str = "2018-07-01"
    dev_end: str = "2025-08-31"
    reserve_start: str = "2025-09-01"
    walk_forward: WalkForward = WalkForward()
    cost_multipliers: tuple[float, ...] = (1.0, 2.0)
    starting_cash: float = Field(10_000.0, gt=0)
    periods_per_year: int = Field(365, description="365 for 24/7 crypto, 252 for exchange markets")
    min_train_trades: int = Field(20, ge=1)
    risk: TradeRiskConfig = TradeRiskConfig()
    bar_minutes: int = Field(1, description="execution bars: 1 (1-minute) or 1440 (daily)")

    @model_validator(mode="after")
    def _windows(self) -> ResearchPlan:
        unknown = set(self.families) - set(FAMILIES)
        if unknown:
            raise ValueError(f"unknown families {sorted(unknown)}")
        if not self.dev_start < self.dev_end < self.reserve_start:
            raise ValueError("need dev_start < dev_end < reserve_start")
        if self.dev_end > last_research_day("*"):
            raise ValueError("development window reaches into a sealed holdout")
        if min(self.cost_multipliers) < 1.0:
            raise ValueError("cost multipliers must be >= 1")
        if not self.folds():
            raise ValueError("development window too short for one walk-forward fold")
        if self.bar_minutes not in (1, 1440):
            raise ValueError("bar_minutes must be 1 or 1440")
        return self

    def plan_hash(self) -> str:
        d = self.model_dump(mode="json")
        if d.get("bar_minutes") == 1:          # fields added later hash only when set: frozen plans keep their hash
            d.pop("bar_minutes")
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]

    def folds(self) -> list[tuple[str, str, str, str]]:
        """(train_start, train_end, test_start, test_end), ISO dates, ends inclusive."""
        wf, out = self.walk_forward, []
        start = pd.Timestamp(self.dev_start)
        end = pd.Timestamp(self.dev_end)
        while True:
            test_start = start + pd.DateOffset(months=wf.train_months)
            test_end = test_start + pd.DateOffset(months=wf.test_months) - pd.Timedelta(days=1)
            if test_end > end:
                return out
            out.append((start.date().isoformat(), (test_start - pd.Timedelta(days=1)).date().isoformat(),
                        test_start.date().isoformat(), test_end.date().isoformat()))
            start += pd.DateOffset(months=wf.step_months)

    def n_configs(self) -> int:
        return sum(len(FAMILIES[f].configs()) for f in self.families) * len(self.instruments)


@dataclass
class ConfigRun:
    key: str
    family: str
    params: dict
    instrument: str
    cost_multiplier: float
    daily: pd.Series
    trades: pd.Series                      # realized P&L indexed by close time
    audit: dict = field(default_factory=dict)

    def trades_in(self, start: str, end: str) -> int:
        if self.trades.empty:                 # no trades: the index may not be a DatetimeIndex
            return 0
        t = self.trades.index
        return int(((t >= pd.Timestamp(start, tz="UTC")) & (t < pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1))).sum())


def config_key(family: str, params: dict, instrument: str, cost_multiplier: float) -> str:
    return json.dumps({"f": family, "p": params, "i": instrument, "c": cost_multiplier}, sort_keys=True)


def daily_returns(equity: pd.Series) -> pd.Series:
    d = equity.resample("1D").last().ffill()
    return d.pct_change().dropna()


def run_config(plan: ResearchPlan, spec, bars, family: str, params: dict, *, cost_multiplier: float = 1.0,
               registry=None, code_commit: str = "", data_hash: str = "") -> ConfigRun:
    """Backtest one configuration over the development window (bars already loaded)."""
    inst = spec.instrument(cost_multiplier)
    segs = tuple(int(pd.Timestamp(f[2], tz="UTC").value) for f in plan.folds())
    strategy = build(family, {**params, "segment_starts_ns": segs}, instrument_id=inst.id,
                     bar_type=bars[0].bar_type, risk=plan.risk, allow_short=spec.margin)
    cash = plan.starting_cash
    if spec.margin and spec.base == "USD" and spec.quote != "USD":   # e.g. USDJPY: same USD capital, in the quote
        cash = round(plan.starting_cash * float(bars[0].open), 2)
    venue = VenueSpec(name=spec.venue, account_type="MARGIN" if spec.margin else "CASH",
                      starting_balances=({spec.quote: cash} if spec.margin
                                         else {spec.quote: plan.starting_cash, spec.base: 0}))
    res = run_backtest(inst, bars, strategy, venue, market=spec.asset_class)
    pos = res.positions
    if spec.perpetual:
        from hedge_fund.trading.data import binance, funding
        rates = funding.load(spec.funding_symbol, binance.months(plan.dev_start, plan.dev_end))["rate"]
        if rates.empty:
            raise ValueError(f"{spec.instrument_id}: no funding settlements cached; perpetuals need funding")
        fin = funding_costs(pos, rates, bars[-1].ts_event, cost_multiplier)
    else:
        fin = financing_costs(pos, spec, bars[-1].ts_event) * cost_multiplier
    if pos.empty:
        trades = pd.Series(dtype=float)
    else:
        closed = pos[pos["ts_closed"].notna()]
        trades = pd.Series([float(str(x).split()[0]) for x in closed["realized_pnl"]],
                           index=pd.to_datetime(closed["ts_closed"], utc=True), dtype=float).sort_index()
        if not fin.empty:                                    # financing reduces each closed trade's P&L
            trades = trades - fin.reindex(trades.index).fillna(0.0).to_numpy()
    equity = apply_financing(res.equity, fin)
    run = ConfigRun(config_key(family, params, spec.instrument_id, cost_multiplier), family, params,
                    spec.instrument_id, cost_multiplier, daily_returns(equity), trades,
                    {**res.audit, "financing_total": float(fin.sum()) if not fin.empty else 0.0})
    if registry is not None:
        registry.record(family=f"active/{family}", spec={"params": params, "instrument": spec.instrument_id,
                                                         "cost_multiplier": cost_multiplier,
                                                         "plan": plan.plan_hash()},
                        stage="development", window=(plan.dev_start, plan.dev_end), code_commit=code_commit,
                        data_hash=data_hash, metrics={"n_trades": len(trades),
                                                      "ambiguous_exits": res.audit["ambiguous_exits"]})
    return run


def _slice(s: pd.Series, start: str, end: str) -> pd.Series:
    return s[(s.index >= pd.Timestamp(start, tz="UTC")) & (s.index <= pd.Timestamp(end, tz="UTC"))]


@dataclass
class WalkForwardResult:
    oos: pd.Series
    choices: list[dict]
    fold_sharpes: list[float]
    n_oos_trades: int


def walk_forward(plan: ResearchPlan, runs: list[ConfigRun]) -> WalkForwardResult:
    """Select on training windows only; concatenate the chosen configs' test returns."""
    pieces, choices, fold_sr, n_trades = [], [], [], 0
    for tr_s, tr_e, te_s, te_e in plan.folds():
        eligible = [r for r in runs if r.trades_in(tr_s, tr_e) >= plan.min_train_trades]
        if not eligible:
            choices.append({"fold": te_s, "choice": None})
            test = pd.Series(0.0, index=pd.date_range(te_s, te_e, freq="D", tz="UTC"))
        else:
            best = max(eligible, key=lambda r: (sharpe(_slice(r.daily, tr_s, tr_e).to_numpy()), r.key))
            choices.append({"fold": te_s, "choice": best.key,
                            "train_sharpe": sharpe(_slice(best.daily, tr_s, tr_e).to_numpy())})
            test = _slice(best.daily, te_s, te_e)
            n_trades += best.trades_in(te_s, te_e)
        pieces.append(test)
        fold_sr.append(sharpe(test.to_numpy()))
    oos = pd.concat(pieces) if pieces else pd.Series(dtype=float)
    return WalkForwardResult(oos, choices, fold_sr, n_trades)


def combine(series: list[pd.Series]) -> pd.Series:
    """Equal-weight combination of daily return series (missing days count as flat)."""
    if not series:
        return pd.Series(dtype=float)
    df = pd.concat(series, axis=1).fillna(0.0)
    return df.mean(axis=1)


def max_drawdown(returns: pd.Series) -> float:
    eq = (1 + returns).cumprod()
    return float(-(eq / eq.cummax() - 1).min()) if len(eq) else 0.0


def evidence(plan: ResearchPlan, wf: WalkForwardResult, runs: list[ConfigRun], *, n_trials: int) -> dict:
    r = wf.oos.to_numpy()
    matrix = pd.concat([x.daily.rename(x.key) for x in runs], axis=1).fillna(0.0)
    ev = {
        "n_trades": wf.n_oos_trades,
        "oos_sharpe_annual": sharpe(r) * math.sqrt(plan.periods_per_year),
        "deflated_sharpe": deflated_sharpe(r, n_trials=max(n_trials, 1)),
        "pbo": pbo(matrix.to_numpy(), n_splits=8) if matrix.shape[1] >= 2 and len(matrix) >= 8 else 1.0,
        "max_drawdown": max_drawdown(wf.oos),
        "walk_forward_positive_share": float(np.mean([s > 0 for s in wf.fold_sharpes])) if wf.fold_sharpes else 0.0,
        "bootstrap_sharpe_lower": bootstrap_sharpe_ci(r)[0] if len(r) > 40 else float("-inf"),
    }
    return ev


def gate(plan: ResearchPlan, wf: WalkForwardResult, runs: list[ConfigRun], gates: GateConfig, *, n_trials: int):
    return evaluate_gates(evidence(plan, wf, runs, n_trials=n_trials), gates)
