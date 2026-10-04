"""Diagnostics and the pre-registered verdict for a completed research plan (read-only).

    python runs/active/research/analyze_plan.py runs/active/research/plan_<id>.json

Reads the plan, its summary.json, AUDIT.json and the cached per-run results; reruns nothing.
Diagnostics describe results and never change parameters:

    worst fold / fold dispersion   diagnostics.fold_report on every line's walk-forward
    regimes                        causal trend/volatility regimes of each instrument (daily closes)
    parameter stability            training-Sharpe neighbourhood of every fold's chosen config
    concentration                  share of profit from the top trades and from one asset
    diversification                correlations between the out-of-sample lines
    CPCV                           combinatorial purged CV of the selection rule on each line's
                                   config matrix (6 groups, 2 test, 5-day embargo): distribution of
                                   the selected config's test Sharpe
    hypothesis predictions         family-specific event studies on development data

Verdict: the plan's decision_rule (gates at every cost multiplier for a line or the family
combination, and no audit FAIL). Writes <plan_id>/ANALYSIS.json and <plan_id>/RESULT.md.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from hedge_fund.paths import CACHE_DIR
from hedge_fund.trading.data.catalog import Catalog
from hedge_fund.trading.data.markets import market
from hedge_fund.trading.diagnostics import concentration, diversification, fold_report, neighbourhood
from hedge_fund.trading.families import FAMILIES
from hedge_fund.trading.regimes import classify, performance_by_regime
from hedge_fund.trading.research import ResearchPlan, _slice, combine, max_drawdown, walk_forward
from hedge_fund.trading.runner import _from_json
from hedge_fund.validation.splits import cpcv
from hedge_fund.validation.stats import sharpe

HERE = Path(__file__).resolve().parent


def daily_closes(catalog: Catalog, iid: str, plan: ResearchPlan) -> pd.Series:
    bars = catalog.load_bars(iid, plan.dev_start, plan.dev_end, minutes=1440)
    s = pd.Series({pd.Timestamp(b.ts_event, unit="ns", tz="UTC") - pd.Timedelta(days=1): float(b.close) for b in bars})
    return s.sort_index()


def cpcv_selection(plan: ResearchPlan, group: list, *, n_groups: int = 6, k_test: int = 2, embargo: int = 5) -> dict:
    m = pd.concat([r.daily.rename(r.key) for r in group], axis=1).fillna(0.0)
    m = m[(m.index >= pd.Timestamp(plan.dev_start, tz="UTC"))]
    x = m.to_numpy()
    out = []
    for sp in cpcv(len(m), n_groups, k_test, embargo=embargo):
        tr, te = list(sp.train), list(sp.test)
        best = max(range(x.shape[1]), key=lambda j: sharpe(x[tr, j]))
        out.append(sharpe(x[te, best]) * math.sqrt(plan.periods_per_year))
    a = np.array(out)
    return {"paths": len(a), "median_test_sharpe": float(np.median(a)), "share_positive": float((a > 0).mean()),
            "p10": float(np.percentile(a, 10)), "p90": float(np.percentile(a, 90))}


def stability(plan: ResearchPlan, wf, group: list, grid: dict) -> dict:
    flags = {"sharp_peak": 0, "unstable_neighbours": 0, "isolated": 0, "folds": 0}
    by_key = {r.key: r for r in group}
    for (tr_s, tr_e, _, _), c in zip(plan.folds(), wf.choices):
        if not c.get("choice"):
            continue
        metric = {frozenset((k, v) for k, v in r.params.items() if k in grid):
                  sharpe(_slice(r.daily, tr_s, tr_e).to_numpy()) for r in group}
        params = {k: v for k, v in by_key[c["choice"]].params.items() if k in grid}
        nb = neighbourhood(grid, params, metric)
        flags["folds"] += 1
        for k in ("sharp_peak", "unstable_neighbours", "isolated"):
            flags[k] += int(bool(nb[k]))
    chosen = [json.loads(c["choice"])["p"] for c in wf.choices if c.get("choice")]
    flags["distinct_choices"] = len({json.dumps(p, sort_keys=True) for p in chosen})
    return flags


def oos_trades(plan: ResearchPlan, wf, group: list) -> pd.Series:
    by_key = {r.key: r for r in group}
    parts = []
    for (_, _, te_s, te_e), c in zip(plan.folds(), wf.choices):
        if c.get("choice"):
            t = by_key[c["choice"]].trades
            parts.append(t[(t.index >= pd.Timestamp(te_s, tz="UTC")) &
                           (t.index < pd.Timestamp(te_e, tz="UTC") + pd.Timedelta(days=1))])
    return pd.concat(parts) if parts else pd.Series(dtype=float)


def funding_event_study(plan: ResearchPlan, closes: dict[str, pd.Series], q: float = 0.95) -> dict:
    """Prediction 1 of H-FUNDING-CROWDING: returns after (causally) extreme positive funding are
    below unconditional returns over the next one to three days. Daily closes, development data."""
    from hedge_fund.trading.data import binance, funding
    out = {}
    for iid, c in closes.items():
        spec = market(iid.split(".")[0])
        rates = funding.load(spec.funding_symbol, binance.months(plan.dev_start, plan.dev_end))["rate"]
        rates = rates[(rates.index >= pd.Timestamp(plan.dev_start, tz="UTC")) &
                      (rates.index <= pd.Timestamp(plan.dev_end, tz="UTC") + pd.Timedelta(days=1))]
        hist, ext_pos, ext_neg = [], [], []
        for t, r in rates.items():
            if len(hist) >= 270:
                hi, lo = np.quantile(hist, q), np.quantile(hist, 1 - q)
                if r > 0 and r > hi:
                    ext_pos.append(t)
                elif r < 0 and r < lo:
                    ext_neg.append(t)
            hist.append(r)
        idx = c.index
        row = {}
        for h in (1, 3):
            def fwd(times):
                vals = []
                for t in times:
                    i = idx.searchsorted(t.normalize() + pd.Timedelta(days=1))   # first close after the settlement's day starts
                    if i + h < len(c):
                        vals.append(c.iloc[i + h] / c.iloc[i] - 1)
                return np.array(vals)
            allr = (c.shift(-h) / c - 1).dropna().to_numpy()
            pos, neg = fwd(sorted(set(ext_pos))), fwd(sorted(set(ext_neg)))
            row[f"{h}d"] = {"unconditional_mean": float(allr.mean()), "after_extreme_positive_mean": float(pos.mean()) if len(pos) else None,
                            "n_positive_events": int(len(pos)),
                            "after_extreme_negative_mean": float(neg.mean()) if len(neg) else None,
                            "n_negative_events": int(len(neg))}
        out[iid] = row
    return out


def main(path: str) -> int:
    doc = json.loads(Path(path).read_text())
    plan = ResearchPlan.model_validate(doc["plan"])
    out_dir = HERE / plan.plan_id
    summary = json.loads((out_dir / "summary.json").read_text())
    audit = json.loads((out_dir / "AUDIT.json").read_text()) if (out_dir / "AUDIT.json").exists() else None
    runs = [_from_json(json.loads(f.read_text())) for f in sorted((CACHE_DIR / "research" / plan.plan_id / "runs").glob("*.json"))]
    base = [r for r in runs if r.cost_multiplier == 1.0]
    catalog = Catalog()
    closes = {i: daily_closes(catalog, i, plan) for i in plan.instruments}
    lines, oos, assets, trades_all, by_asset_pnl = {}, {}, {}, [], {}
    for f in plan.families:
        grid = {k: v for k, v in FAMILIES[f].grid.items() if len(v) > 1}
        for i in plan.instruments:
            group = [r for r in base if r.family == f and r.instrument == i]
            wf = walk_forward(plan, group)
            mk = market(i.split(".")[0]).asset_class
            reg = classify(closes[i], market=mk)
            name = f"{f}:{i}"
            tr = oos_trades(plan, wf, group)
            trades_all.extend(tr.tolist())
            by_asset_pnl[i] = float(tr.sum())
            lines[name] = {
                "folds": fold_report(plan, wf, {r.key: r for r in group}, regimes=reg, market=mk),
                "regimes": performance_by_regime(wf.oos, reg, periods_per_year=plan.periods_per_year),
                "stability": stability(plan, wf, group, grid),
                "concentration": concentration(tr.tolist()),
                "cpcv": cpcv_selection(plan, group),
                "oos_return": float((1 + wf.oos).prod() - 1), "oos_max_drawdown": max_drawdown(wf.oos),
                "oos_sharpe_annual": sharpe(wf.oos.to_numpy()) * math.sqrt(plan.periods_per_year),
            }
            oos[name], assets[name] = wf.oos, mk
    fam_oos = combine(list(oos.values()))
    proxy = plan.instruments[0]
    combo = {
        "folds": fold_report(plan, type("W", (), {"oos": fam_oos, "choices": [{}] * len(plan.folds())})(), {},
                             market="*"),
        "regimes_vs_" + proxy: performance_by_regime(fam_oos, classify(closes[proxy], market=assets[next(iter(oos))]),
                                                     periods_per_year=plan.periods_per_year),
        "concentration": concentration(trades_all, by_asset=by_asset_pnl),
        "oos_return": float((1 + fam_oos).prod() - 1), "oos_max_drawdown": max_drawdown(fam_oos),
        "oos_sharpe_annual": sharpe(fam_oos.to_numpy()) * math.sqrt(plan.periods_per_year),
    }
    div = diversification(oos, asset=assets, family={n: n.split(":")[0] for n in oos})
    predictions = funding_event_study(plan, closes) if "funding_crowding" in plan.families else None
    if "vol_managed_trend" in plan.families:
        by_class = {}
        for n, s in oos.items():
            by_class.setdefault(assets[n], []).append(s)
        predictions = {"max_drawdown_by_asset_class": {k: max_drawdown(combine(v)) for k, v in by_class.items()},
                       "max_drawdown_portfolio": max_drawdown(fam_oos),
                       "prediction_portfolio_drawdown_below_every_class":
                           bool(max_drawdown(fam_oos) < min(max_drawdown(combine(v)) for v in by_class.values()))}
    audit_fail = None if audit is None else [c["name"] for c in audit["checks"] if c["status"] == "FAIL"]
    passed_lines = [f"{x['family']}:{x['instrument']}" for x in summary["lines"] if x["passed"]]
    passed_combos = [k for k, v in summary["combinations"].items() if k.startswith("family:") and v["passed"]]
    if audit is None:
        verdict = "INCOMPLETE (no audit)"
    else:
        verdict = "PASS" if (passed_lines or passed_combos) and not audit_fail else "FAIL"
    result = {"plan_id": plan.plan_id, "plan_hash": plan.plan_hash(), "n_trials_cumulative": summary["n_trials"],
              "gates_hash": summary["gates_hash"], "verdict": verdict, "passed_lines": passed_lines,
              "passed_combinations": passed_combos, "audit_fail": audit_fail,
              "lines": lines, "family_combination": combo, "diversification": div, "hypothesis_predictions": predictions}
    (out_dir / "ANALYSIS.json").write_text(json.dumps(result, indent=1, default=str))
    (out_dir / "RESULT.md").write_text(render(plan, doc, summary, result, audit))
    print(json.dumps({"verdict": verdict, "passed_lines": passed_lines, "passed_combinations": passed_combos,
                      "audit_fail": audit_fail}))
    return 0


def _fmt(x, pct=False):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "—"
    return f"{x:+.1%}" if pct else f"{x:.2f}"


def render(plan, doc, summary, result, audit) -> str:
    gate_names = ["min_trades", "oos_sharpe", "deflated_sharpe", "pbo", "max_drawdown", "walk_forward",
                  "bootstrap_sharpe_lower"]
    L = [f"# {plan.plan_id}: {result['verdict']}", "",
         f"Plan hash `{result['plan_hash']}`. Hypothesis: {', '.join(v['id'] for v in doc['hypotheses'].values())}. "
         f"Development window {plan.dev_start} to {plan.dev_end}; the sealed holdout from {plan.reserve_start} "
         f"was not read. Cumulative trial count used for the deflated Sharpe: {result['n_trials_cumulative']}. "
         f"Gate config hash `{result['gates_hash']}`.", "",
         f"Decision rule (pre-registered): {doc['decision_rule']}", "",
         "## Out-of-sample lines (walk-forward, cost x1; x2 stress shown as pass/fail)", "",
         "| Line | Trades | OOS Sharpe | Deflated Sharpe | PBO | Max DD | Positive folds | Failed gates (x1) | x2 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for x in summary["lines"]:
        v = x["values"]
        failed = [k for k, ok in x["checks"].items() if not ok]
        st = all(s["passed"] for s in x["cost_stress"].values())
        L.append(f"| {x['instrument'].split('.')[0]} | {int(v.get('n_trades', 0))} | {_fmt(v.get('oos_sharpe_annual'))} | "
                 f"{_fmt(v.get('deflated_sharpe'))} | {_fmt(v.get('pbo'))} | {_fmt(v.get('max_drawdown'), True)} | "
                 f"{_fmt(v.get('walk_forward_positive_share'), True)} | {', '.join(failed) or 'none'} | "
                 f"{'pass' if st else 'fail'} |")
    L += ["", "## Combinations", "", "| Combination | Trades | OOS Sharpe | Deflated Sharpe | Max DD | Failed gates (x1) | x2 |",
          "|---|---|---|---|---|---|---|"]
    for k, c in summary["combinations"].items():
        v = c["values"]
        failed = [g for g, ok in c["checks"].items() if not ok]
        st = all(s["passed"] for s in c.get("cost_stress", {}).values())
        L.append(f"| {k} | {int(v.get('n_trades', 0))} | {_fmt(v.get('oos_sharpe_annual'))} | {_fmt(v.get('deflated_sharpe'))} | "
                 f"{_fmt(v.get('max_drawdown'), True)} | {', '.join(failed) or 'none'} | {'pass' if st else 'fail'} |")
    L += ["", "## Diagnostics (descriptive; they cannot change the verdict)", "",
          "| Line | OOS return | Worst fold | Fragile | Stability flags (sharp/unstable/isolated of folds) | CPCV median Sharpe | CPCV share > 0 |",
          "|---|---|---|---|---|---|---|"]
    for n, d in result["lines"].items():
        s, f, cp = d["stability"], d["folds"]["summary"], d["cpcv"]
        worst = f["worst_fold"]
        L.append(f"| {n.split(':')[1].split('.')[0]} | {_fmt(d['oos_return'], True)} | "
                 f"{worst['test_start'] if worst else '—'} ({_fmt(worst['return'] if worst else None, True)}) | "
                 f"{'yes' if f['fragile'] else 'no'} | {s['sharp_peak']}/{s['unstable_neighbours']}/{s['isolated']} of {s['folds']} | "
                 f"{_fmt(cp['median_test_sharpe'])} | {_fmt(cp['share_positive'], True)} |")
    c = result["family_combination"]
    L += ["", f"Family combination: OOS return {_fmt(c['oos_return'], True)}, Sharpe {_fmt(c['oos_sharpe_annual'])}, "
          f"max drawdown {_fmt(c['oos_max_drawdown'], True)}; worst fold "
          f"{c['folds']['summary']['worst_fold']['test_start'] if c['folds']['summary']['worst_fold'] else '—'}; "
          f"effective independent sources {result['diversification']['effective_independent_sources']:.1f} "
          f"of {len(result['lines'])}.", ""]
    if result["hypothesis_predictions"]:
        L += ["## Hypothesis predictions (development data, descriptive)", "", "```",
              json.dumps(result["hypothesis_predictions"], indent=1, default=str), "```", ""]
    if audit is not None:
        cnt = {}
        for ch in audit["checks"]:
            cnt[ch["status"]] = cnt.get(ch["status"], 0) + 1
        L += ["## Adversarial audit", "", f"Counts: {cnt}.", ""]
        for ch in audit["checks"]:
            if ch["status"] != "PASS":
                L.append(f"- {ch['status']} `{ch['name']}`: {ch['detail']}")
        L.append("")
    L += [f"## Verdict: {result['verdict']}", "",
          f"Lines passing every gate at all cost multipliers: {result['passed_lines'] or 'none'}. "
          f"Family combinations passing: {result['passed_combinations'] or 'none'}. "
          f"Audit FAIL checks: {result['audit_fail'] or 'none'}.", ""]
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
