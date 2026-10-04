"""Multi-leg daily portfolio research: funding carry, cross-sectional momentum, pairs.

The Nautilus harness backtests one instrument per run; these families hold several legs at
once (spot + perpetual, a ranked basket, a hedged pair). They run on this small daily
engine and go through the *same* research stack: frozen ResearchPlan, registry trial per
run, walk-forward selection on training windows only, deflated Sharpe with the cumulative
trial count, PBO, bootstrap, cost stress, combinations, adversarial audit.

Engine (`simulate`), per day t, in this order:

    gap       positions are marked from the previous close to today's open
    execute   target weights decided at the close of day t-1 (data <= t-1 only) are traded
              at today's open: every changed leg pays its taker cost (commission + half
              spread + slippage, from markets.py, x cost_multiplier); weights are fractions
              of current equity; unchanged targets are not traded (positions drift)
    funding   perpetual legs pay/receive every settlement of the day (long pays a positive
              rate); settlements in the first minute of the day are charged on the worse
              of the pre- and post-trade position; payments x cost_multiplier, receipts x1
    intraday  positions are marked from the open to the close
    delisting a leg whose instrument stops trading is closed at its last close (with cost)

Signals see closes up to the decision day and funding settled by its end. Each family's
`targets` function is pure: (data, line instruments, params) -> DataFrame of target
weights indexed by decision day. Trades are position episodes per group of legs
(a coin for momentum; all legs of a carry or pair line), P&L in currency of starting cash.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from hedge_fund.trading.data.markets import CRYPTO, PERPS, market

GROSS = 0.9                      # gross exposure per line (fraction of equity); no leverage beyond it


@dataclass
class DailyData:
    open: pd.DataFrame           # index: day (00:00 UTC), columns: instrument ids
    close: pd.DataFrame
    funding: dict[str, pd.Series]   # perpetual id -> settled rate (per settlement) indexed by settlement time

    def slice_until(self, day: pd.Timestamp) -> "DailyData":
        """Everything known at the close of *day* (used by the look-ahead probes)."""
        end = day + pd.Timedelta(days=1)
        return DailyData(self.open[self.open.index <= day], self.close[self.close.index <= day],
                         {k: v[v.index < end] for k, v in self.funding.items()})


def load_daily(instruments, start: str, end: str, *, catalog=None) -> DailyData:
    from hedge_fund.trading.data import binance, funding
    from hedge_fund.trading.data.catalog import Catalog
    cat = catalog or Catalog()
    opens, closes = {}, {}
    for i in instruments:
        bars = cat.load_bars(i, start, end, minutes=1440)          # fence enforced for the market
        day = [pd.Timestamp(b.ts_event, unit="ns", tz="UTC") - pd.Timedelta(days=1) for b in bars]
        opens[i] = pd.Series([float(b.open) for b in bars], index=day)
        closes[i] = pd.Series([float(b.close) for b in bars], index=day)
    o, c = pd.DataFrame(opens).sort_index(), pd.DataFrame(closes).sort_index()
    fund = {}
    for i in instruments:
        spec = market(i.split(".")[0])
        if spec.perpetual:
            df = funding.load(spec.funding_symbol, binance.months(start, end))
            r = df["rate"]
            fund[i] = r[(r.index >= pd.Timestamp(start, tz="UTC")) & (r.index < c.index[-1] + pd.Timedelta(days=1))]
    return DailyData(o, c, fund)


# -- engine -----------------------------------------------------------------------

def simulate(data: DailyData, targets: pd.DataFrame, *, groups: list[tuple[str, ...]], cost_multiplier: float = 1.0,
             starting_cash: float = 10_000.0) -> tuple[pd.Series, pd.Series, dict]:
    """-> (daily returns, closed-episode P&L indexed by exit day, stats)."""
    if cost_multiplier < 1.0:
        raise ValueError("costs may be stressed up, never down")
    inst = list(targets.columns)
    fee = {i: float(market(i.split(".")[0]).fees(cost_multiplier)[1]) for i in inst}
    days = data.close.index
    op, cl = data.open.reindex(days)[inst], data.close.reindex(days)[inst]
    tg = targets.reindex(days).fillna(0.0)[inst]
    fund_by_day = {}
    for i in inst:
        r = data.funding.get(i)
        if r is not None and len(r):
            d = r.index.floor("D")
            first_minute = (r.index - d) < pd.Timedelta(minutes=1)
            fund_by_day[i] = pd.DataFrame({"rate": r.to_numpy(), "day": d, "early": first_minute})
    fund_lookup = {i: {day: g for day, g in df.groupby("day")} for i, df in fund_by_day.items()}
    E, q = 1.0, {i: 0.0 for i in inst}
    prev_close = {i: np.nan for i in inst}
    executed = pd.Series(0.0, index=inst)
    eq, pnl_inst = [], []
    trades_n = 0
    for k, t in enumerate(days):
        day_pnl = {i: 0.0 for i in inst}
        o, c = op.loc[t], cl.loc[t]
        for i in inst:                                            # gap (and delisting exits)
            if q[i] == 0.0:
                continue
            if np.isnan(o[i]):                                    # stopped trading: close at the last close
                cost = abs(q[i]) * fee[i]
                day_pnl[i] -= cost
                E -= cost
                q[i] = 0.0
                trades_n += 1
                continue
            g = q[i] * (o[i] / prev_close[i] - 1) if not np.isnan(prev_close[i]) else 0.0
            day_pnl[i] += g
            E += g
            q[i] *= (o[i] / prev_close[i]) if not np.isnan(prev_close[i]) else 1.0
        pre = dict(q)
        want = tg.iloc[k - 1] if k > 0 else pd.Series(0.0, index=inst)   # decided at the previous close
        if not np.allclose(want.to_numpy(), executed.to_numpy()):
            for i in inst:
                if np.isnan(o[i]):
                    continue                                      # not tradable today: keep, retry tomorrow
                new = float(want[i]) * E
                cost = abs(new - q[i]) * fee[i]
                if abs(new - q[i]) > 1e-12:
                    trades_n += 1
                day_pnl[i] -= cost
                E -= cost
                q[i] = new
                executed[i] = float(want[i])
        for i in inst:                                            # funding
            g = fund_lookup.get(i, {}).get(t)
            if g is None:
                continue
            for rate, early in zip(g["rate"], g["early"]):
                pays = [p * rate for p in ((pre[i], q[i]) if early else (q[i],))]
                pay = max(pays)                                   # early: the worse of pre/post-trade position
                f = pay * cost_multiplier if pay > 0 else pay
                day_pnl[i] -= f
                E -= f
        for i in inst:                                            # intraday
            if q[i] != 0.0 and not np.isnan(c[i]):
                g = q[i] * (c[i] / o[i] - 1)
                day_pnl[i] += g
                E += g
                q[i] *= c[i] / o[i]
            if not np.isnan(c[i]):
                prev_close[i] = c[i]
        if E <= 0:
            raise RuntimeError("equity exhausted")
        eq.append(E)
        pnl_inst.append(day_pnl)
    equity = pd.Series(eq, index=days)
    returns = equity.pct_change().fillna(equity.iloc[0] - 1.0)
    pnl = pd.DataFrame(pnl_inst, index=days)[inst] * starting_cash
    held = tg.shift(1).fillna(0.0)                                # target in force during day t
    trades = _episodes(pnl, held, groups)
    return returns, trades, {"leg_trades": trades_n, "final_equity": E}


def _episodes(pnl: pd.DataFrame, held: pd.DataFrame, groups: list[tuple[str, ...]]) -> pd.Series:
    out = []
    for g in groups:
        g = [i for i in g if i in held.columns]
        active = (held[g].abs().sum(axis=1) > 0).to_numpy()
        sig = np.sign(held[g].to_numpy()).tolist()
        acc, start = 0.0, None
        for k, day in enumerate(held.index):
            if active[k] and start is not None and sig[k] != sig[k - 1]:     # direction change = new episode
                out.append((held.index[k - 1], acc))
                acc, start = 0.0, None
            if active[k] or (start is not None):
                acc += float(pnl[g].iloc[k].sum())
            if active[k] and start is None:
                start = day
            if not active[k] and start is not None:
                out.append((day, acc))                            # exit day includes the exit cost
                acc, start = 0.0, None
        if start is not None:
            out.append((held.index[-1], acc))
    if not out:
        return pd.Series(dtype=float)
    s = pd.Series([v for _, v in out], index=pd.DatetimeIndex([d for d, _ in out]), dtype=float)
    return s.sort_index()


# -- families -------------------------------------------------------------------

def carry_targets(data: DailyData, line: tuple[str, str], p: dict) -> pd.DataFrame:
    """Long spot / short perpetual while the trailing mean funding (last `lookback`
    settlements, annualized) is above `entry_apr`; exit below half of it."""
    spot, perp = line
    days = data.close.index
    out = pd.DataFrame(0.0, index=days, columns=[spot, perp])
    r = data.funding.get(perp)
    if r is None or r.empty:
        return out
    per_year = 365 * 24 * 3600 / max(1.0, float(np.median(np.diff(r.index.asi8)) / 1e9))
    apr = r.rolling(p["lookback"]).mean() * per_year
    on = False
    ends = apr.index.searchsorted(days + pd.Timedelta(days=1), side="left")   # settled before the day ends
    for d, e in zip(days, ends):
        v = apr.iloc[e - 1] if e > 0 else np.nan
        tradable = not (np.isnan(data.close.at[d, spot]) or np.isnan(data.close.at[d, perp]))
        if not tradable or np.isnan(v):
            on = False
        elif not on and v > p["entry_apr"]:
            on = True
        elif on and v < p["entry_apr"] / 2:
            on = False
        if on:
            out.loc[d] = [GROSS / 2, -GROSS / 2]
    return out


def xsmom_targets(data: DailyData, line: tuple[str, ...], p: dict) -> pd.DataFrame:
    """Every `rebalance_days`: long the top `k`, short the bottom `k` coins by return over
    the last `lookback` days (coins with a full window only); equal weights, GROSS total."""
    days = data.close.index
    cl = data.close[list(line)]
    out = pd.DataFrame(0.0, index=days, columns=list(line))
    cur = pd.Series(0.0, index=list(line))
    for n, d in enumerate(days):
        if n % p["rebalance_days"] == 0 and n >= p["lookback"]:
            past = cl.iloc[n - p["lookback"]]
            now = cl.iloc[n]
            ret = (now / past - 1).dropna()
            cur = pd.Series(0.0, index=list(line))
            if len(ret) >= 2 * p["k"]:
                ranked = ret.sort_values()
                w = GROSS / 2 / p["k"]
                cur[ranked.index[-p["k"]:]] = w
                cur[ranked.index[:p["k"]]] = -w
        cur = cur.where(cl.iloc[n].notna(), 0.0)                  # drop coins that stopped trading
        out.loc[d] = cur
    return out


def pairs_targets(data: DailyData, line: tuple[str, str], p: dict) -> pd.DataFrame:
    """Rolling OLS hedge ratio of log A on log B over `window` days; spread z-score.
    Enter when |z| > z_entry (short the rich leg, long the cheap leg, hedge ratio fixed at
    entry); exit when |z| < 0.5, |z| > 4 (stop) or after window/2 days."""
    a, b = line
    days = data.close.index
    out = pd.DataFrame(0.0, index=days, columns=[a, b])
    la, lb = np.log(data.close[a]), np.log(data.close[b])
    n_win = p["window"]
    state, beta_in, held = 0, 0.0, 0
    for k, d in enumerate(days):
        if k + 1 < n_win:
            continue
        xa, xb = la.iloc[k + 1 - n_win:k + 1], lb.iloc[k + 1 - n_win:k + 1]
        if xa.isna().any() or xb.isna().any():
            state = 0
            continue
        beta = float(np.cov(xa, xb)[0, 1] / np.var(xb, ddof=1))
        spread = xa - (beta_in if state else beta) * xb
        sd = float(spread.std(ddof=1))
        if sd <= 0:
            continue
        z = float((spread.iloc[-1] - spread.mean()) / sd)
        if state == 0:
            if abs(z) > p["z_entry"]:
                state, beta_in, held = (-1 if z > 0 else 1), beta, 0
        else:
            held += 1
            if abs(z) < 0.5 or abs(z) > 4.0 or held >= n_win // 2:
                state = 0
        if state:
            wa = GROSS / (1 + abs(beta_in))
            out.loc[d] = [state * wa, -state * beta_in * wa]
    return out


@dataclass(frozen=True)
class PortfolioFamily:
    name: str
    style: str
    grid: dict
    lines: dict                              # line name -> instruments (all legs)
    targets: Callable
    per_coin_trades: bool = False            # momentum: episodes per coin; others: per line

    def configs(self) -> list[dict]:
        keys = sorted(self.grid)
        return [dict(zip(keys, v)) for v in itertools.product(*(self.grid[k] for k in keys))]

    def groups(self, line: str) -> list[tuple[str, ...]]:
        inst = self.lines[line]
        return [(i,) for i in inst] if self.per_coin_trades else [tuple(inst)]


PERP12 = tuple(sorted(f"{s}.BINANCE" for s in PERPS))
PORTFOLIO_FAMILIES: dict[str, PortfolioFamily] = {f.name: f for f in [
    PortfolioFamily("funding_carry", "carry", {"lookback": [3, 21], "entry_apr": [0.10, 0.25]},
                    {c: (f"{c}.BINANCE", f"{c}-PERP.BINANCE") for c in CRYPTO}, carry_targets),
    PortfolioFamily("xs_momentum_crypto", "cross_sectional_momentum",
                    {"lookback": [7, 28], "k": [2, 3], "rebalance_days": [7]}, {"PERP12": PERP12}, xsmom_targets,
                    per_coin_trades=True),
    PortfolioFamily("pairs_statarb", "statistical_arbitrage", {"window": [30, 90], "z_entry": [1.5, 2.5]},
                    {"BTC/ETH": ("BTCUSDT-PERP.BINANCE", "ETHUSDT-PERP.BINANCE"),
                     "ETH/ETC": ("ETHUSDT-PERP.BINANCE", "ETCUSDT-PERP.BINANCE"),
                     "BTC/BCH": ("BTCUSDT-PERP.BINANCE", "BCHUSDT-PERP.BINANCE"),
                     "BTC/LTC": ("BTCUSDT-PERP.BINANCE", "LTCUSDT-PERP.BINANCE"),
                     "XRP/XLM": ("XRPUSDT-PERP.BINANCE", "XLMUSDT-PERP.BINANCE")}, pairs_targets),
]}


def plan_instruments(families) -> tuple[str, ...]:
    return tuple(sorted({i for f in families for inst in PORTFOLIO_FAMILIES[f].lines.values() for i in inst}))


# -- research run -----------------------------------------------------------------

def run_line(plan, data: DailyData, family: str, line: str, params: dict, cost_multiplier: float):
    from hedge_fund.trading.research import ConfigRun, config_key
    fam = PORTFOLIO_FAMILIES[family]
    inst = fam.lines[line]
    tg = fam.targets(data, inst, params)
    rets, trades, stats = simulate(data, tg, groups=fam.groups(line), cost_multiplier=cost_multiplier,
                                   starting_cash=plan.starting_cash)
    return ConfigRun(config_key(family, params, line, cost_multiplier), family, params, line, cost_multiplier,
                     rets, trades, {"ambiguous_exits": 0, "ambiguous_share": 0.0, **stats})


def run_portfolio_plan(plan, out_dir: Path | str, *, registry_path: Path | str, code_commit: str = "",
                       data: DailyData | None = None, prior_registries=None, require_hypothesis: bool = True) -> dict:
    from hedge_fund.trading.hypothesis import require_hypotheses
    from hedge_fund.trading.research import WalkForwardResult, _slice, combine, evidence, walk_forward
    from hedge_fund.trading.runner import _replay, _to_json, all_registries, count_trials
    from hedge_fund.validation.gates import evaluate_gates, load_gates
    from hedge_fund.validation.registry import ExperimentRegistry
    from hedge_fund.validation.stats import sharpe

    if require_hypothesis:
        require_hypotheses(plan)
    prior = all_registries() if prior_registries is None else list(prior_registries)
    out_dir = Path(out_dir)
    (out_dir / "runs").mkdir(parents=True, exist_ok=True)
    registry = ExperimentRegistry(registry_path)
    gates = load_gates()
    data = data or load_daily(plan.instruments, plan.dev_start, plan.dev_end)

    def run(f, line, p, cost):
        r = run_line(plan, data, f, line, p, cost)
        registry.record(family=f"active/{f}", spec={"params": p, "instrument": line, "cost_multiplier": cost,
                                                     "plan": plan.plan_hash()},
                        stage="development", window=(plan.dev_start, plan.dev_end), code_commit=code_commit,
                        metrics={"n_trades": len(r.trades), "ambiguous_exits": 0})
        (out_dir / "runs" / f"{abs(hash(r.key)) % 10**16:016d}.json").write_text(json.dumps(_to_json(r)))
        print(f"{f} {line} {json.dumps(p, sort_keys=True)} x{cost}: {len(r.trades)} trades", flush=True)
        return r

    runs = [run(f, line, p, 1.0) for f in plan.families for line in PORTFOLIO_FAMILIES[f].lines
            for p in PORTFOLIO_FAMILIES[f].configs()]
    n_trials = count_trials(registry, prior)
    lines, wfs_all, by_family, stressed = [], [], {}, {}
    for f in plan.families:
        for line in PORTFOLIO_FAMILIES[f].lines:
            group = [r for r in runs if r.family == f and r.instrument == line]
            wf = walk_forward(plan, group)
            res = evaluate_gates(evidence(plan, wf, group, n_trials=n_trials), gates)
            stress = {}
            chosen = sorted({c["choice"] for c in wf.choices if c["choice"]})
            for cost in [c for c in plan.cost_multipliers if c != 1.0]:
                remap = {k: run(f, line, json.loads(k)["p"], cost) for k in chosen}
                swf = _replay(plan, wf, remap)
                sres = evaluate_gates(evidence(plan, swf, group, n_trials=n_trials), gates)
                stress[str(cost)] = {"passed": sres.passed, "checks": sres.checks, "values": sres.values}
                for g in (f"family:{f}", "all"):
                    stressed.setdefault(str(cost), {}).setdefault(g, []).append(swf)
            lines.append({"family": f, "instrument": line, "passed": res.passed and all(s["passed"] for s in stress.values()),
                          "checks": res.checks, "values": res.values, "cost_stress": stress, "choices": wf.choices})
            wfs_all.append(wf)
            by_family.setdefault(f, []).append(wf)
    combos = {}
    for name, members in [*((f"family:{f}", m) for f, m in by_family.items()), ("all", wfs_all)]:
        oos = combine([m.oos for m in members])
        folds = [sharpe(_slice(oos, a, b).to_numpy()) for _, _, a, b in plan.folds()]
        res = evaluate_gates(evidence(plan, WalkForwardResult(oos, [], folds, sum(m.n_oos_trades for m in members)),
                                      runs, n_trials=n_trials), gates)
        cstress = {}
        for cost, groups in stressed.items():
            ms = groups.get(name, [])
            soos = combine([m.oos for m in ms])
            sf = [sharpe(_slice(soos, a, b).to_numpy()) for _, _, a, b in plan.folds()]
            sres = evaluate_gates(evidence(plan, WalkForwardResult(soos, [], sf, sum(m.n_oos_trades for m in ms)),
                                           runs, n_trials=n_trials), gates)
            cstress[cost] = {"passed": sres.passed, "checks": sres.checks, "values": sres.values}
        combos[name] = {"passed": res.passed and all(v["passed"] for v in cstress.values()), "base_passed": res.passed,
                        "checks": res.checks, "values": res.values, "cost_stress": cstress, "members": len(members)}
    summary = {"plan": plan.model_dump(mode="json"), "plan_hash": plan.plan_hash(), "n_trials": n_trials,
               "gates_hash": gates.config_hash(), "lines": lines, "combinations": combos,
               "any_passed": any(x["passed"] for x in lines),
               "any_combination_passed": any(c["passed"] for c in combos.values())}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary


# -- adversarial audit ----------------------------------------------------------

def _perturb_after(data: DailyData, cut: pd.Timestamp) -> DailyData:
    """Mirror every price path in log space after *cut* and flip/scale funding after it."""
    def mirror(df):
        out = df.copy()
        piv = df[df.index <= cut].iloc[-1]
        later = df.index > cut
        out.loc[later] = (piv ** 2) / df.loc[later]
        return out
    fund = {k: v.where(v.index < cut + pd.Timedelta(days=1), -3 * v) for k, v in data.funding.items()}
    return DailyData(mirror(data.open), mirror(data.close), fund)


def audit_portfolio_plan(plan, *, catalog=None, data: DailyData | None = None):
    from hedge_fund.trading.audit import (FAIL, PASS, WARNING, AuditReport, check_catalog, check_costs, check_leakage,
                                          check_survivorship)
    report = AuditReport(subject=f"plan {plan.plan_id} ({plan.plan_hash()})")
    data = data or load_daily(plan.instruments, plan.dev_start, plan.dev_end, catalog=catalog)
    days = data.close.index
    for f in plan.families:
        fam = PORTFOLIO_FAMILIES[f]
        line = next(iter(fam.lines))
        for p in (fam.configs()[0], fam.configs()[-1]):
            tag = f"{f} {line} {json.dumps(p, sort_keys=True)}"
            full = fam.targets(data, fam.lines[line], p)
            leaks, active = [], bool(full.abs().to_numpy().sum())
            for frac in (0.3, 0.5, 0.7, 0.9):
                cut = days[int(len(days) * frac)]
                alt = fam.targets(_perturb_after(data, cut), fam.lines[line], p)
                if not np.allclose(full[full.index <= cut].to_numpy(), alt[alt.index <= cut].to_numpy()):
                    leaks.append(frac)
            if leaks:
                report.add("look_ahead", FAIL, f"{tag}: targets up to T change when data after T change ({leaks})")
            elif not active:
                report.add("look_ahead", WARNING, f"{tag}: no positions; check not exercised")
            else:
                report.add("look_ahead", PASS, f"{tag}: targets up to T unchanged when prices and funding after T "
                                               f"are mirrored/scrambled (4 cuts)")
    # execution timing: a jump on the decision day's close must not reach a position decided at that close
    idx = pd.date_range("2021-01-01", periods=10, freq="D", tz="UTC")
    px = pd.Series(100.0, index=idx)
    px.iloc[5:] = 150.0
    probe_i = plan.instruments[0]
    d = DailyData(pd.DataFrame({probe_i: px.shift(1).fillna(100.0)}), pd.DataFrame({probe_i: px}), {})
    tg = pd.DataFrame({probe_i: [0.0] * 5 + [0.5] * 5}, index=idx)           # decided at the jump's close
    r, _, _ = simulate(d, tg, groups=[(probe_i,)])
    ok = abs(r.iloc[5]) < 1e-12 and r.iloc[6] < 0                           # no gain from the jump, only cost
    report.add("execution_timing", PASS if ok else FAIL,
               "targets decided at a close are executed at the next open (decision-day move not captured)" if ok
               else "a position captured the move of its own decision day")
    check_costs(plan, report)
    report.add("fills", PASS, "daily next-open execution at taker cost; no stop/target orders, so no intrabar "
                              "ambiguity; weights drift between rebalances (traded only when the target changes)")
    check_leakage(plan, report)
    check_survivorship(plan, report)
    check_catalog(plan, report, catalog)
    return report
