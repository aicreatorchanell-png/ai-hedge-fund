"""Run a frozen ResearchPlan end to end (parallel), then select, stress and gate.

    run_plan(plan, ...)  1. every (family config x instrument) at cost x1.0 over the
                            development window -> daily returns + trades (cached as parquet
                            in out_dir; one registry record per run = one counted trial)
                         2. per (family, instrument): walk-forward selection on training
                            windows, out-of-sample evidence, gates
                         3. cost stress: the configs chosen in any fold are rerun at every
                            other cost multiplier with the *same* fold choices; gates again
                         4. combinations: per family across instruments, and all
                            family-instrument out-of-sample series together (equal weight;
                            members were chosen on training data only)
                         5. summary.json: evidence, gate checks, trial count, plan hash

Every candidate line reports pass/fail for each gate; nothing is promoted here.
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from hedge_fund.trading.data.catalog import Catalog
from hedge_fund.trading.data.markets import market
from hedge_fund.trading.families import FAMILIES
from hedge_fund.trading.research import (ConfigRun, ResearchPlan, WalkForwardResult, _slice, combine, evidence,
                                         run_config, walk_forward)
from hedge_fund.validation.gates import evaluate_gates, load_gates
from hedge_fund.validation.registry import ExperimentRegistry

_BARS: dict[tuple, list] = {}


def _bars(catalog_path: str, instrument: str, start: str, end: str) -> list:
    key = (catalog_path, instrument, start, end)
    if key not in _BARS:
        _BARS.clear()
        _BARS[key] = Catalog(catalog_path).load_bars(instrument, start, end)
    return _BARS[key]


def _task(args) -> dict:
    plan_json, catalog_path, instrument, family, params, cost = args
    plan = ResearchPlan.model_validate_json(plan_json)
    spec = market(instrument.split(".")[0])
    run = run_config(plan, spec, _bars(catalog_path, instrument, plan.dev_start, plan.dev_end), family, params,
                     cost_multiplier=cost)
    return {"key": run.key, "family": family, "params": params, "instrument": instrument, "cost": cost,
            "daily": run.daily, "trades": run.trades, "audit": run.audit}


def _load_or_run(plan: ResearchPlan, tasks: list[tuple], out_dir: Path, catalog_path: str, workers: int,
                 registry: ExperimentRegistry, code_commit: str) -> dict[str, ConfigRun]:
    cache = out_dir / "runs"
    cache.mkdir(parents=True, exist_ok=True)
    done, todo = {}, []
    for t in tasks:
        _, _, instrument, family, params, cost = t
        key = json.dumps({"f": family, "p": params, "i": instrument, "c": cost}, sort_keys=True)
        f = cache / f"{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"
        if f.exists() and json.loads(f.read_text())["key"] == key:
            done[key] = _from_json(json.loads(f.read_text()))
        else:
            todo.append((t, f))
    pj = plan.model_dump_json()
    jobs = [(pj, catalog_path, *t[2:]) for t, _ in todo]
    files = {json.dumps({"f": j[3], "p": j[4], "i": j[2], "c": j[5]}, sort_keys=True): f for j, (_, f) in zip(jobs, todo)}
    if not jobs:
        return done
    pool = ProcessPoolExecutor(min(workers, len(jobs))) if workers > 1 else None
    try:
        for n, res in enumerate((pool.map if pool else map)(_task, jobs), 1):
            _store(res, plan, registry, code_commit, files, done)
            print(f"{time.strftime('%H:%M:%S')} {n}/{len(jobs)} {res['family']} {res['instrument']} "
                  f"cost x{res['cost']}", flush=True)
    finally:
        if pool:
            pool.shutdown()
    return done


def _store(res: dict, plan: ResearchPlan, registry: ExperimentRegistry, code_commit: str, files: dict,
           done: dict) -> None:
    run = ConfigRun(res["key"], res["family"], res["params"], res["instrument"], res["cost"], res["daily"],
                    res["trades"], res["audit"])
    registry.record(family=f"active/{run.family}", spec={"params": run.params, "instrument": run.instrument,
                                                         "cost_multiplier": run.cost_multiplier,
                                                         "plan": plan.plan_hash()},
                    stage="development", window=(plan.dev_start, plan.dev_end), code_commit=code_commit,
                    metrics={"n_trades": len(run.trades), "ambiguous_exits": run.audit.get("ambiguous_exits")})
    files[run.key].write_text(json.dumps(_to_json(run)))
    done[run.key] = run


RESEARCH_DIR = Path(__file__).resolve().parents[2] / "runs" / "active" / "research"


def all_registries(root: Path = RESEARCH_DIR) -> list[Path]:
    """Every active-research registry in the repository (the cumulative trial record)."""
    return sorted(root.glob("*/experiments.jsonl"))


def count_trials(registry: ExperimentRegistry, prior: list[Path | str] = ()) -> int:
    """Distinct configurations (family, params, instrument, plan) run at base cost in this
    registry plus every prior registry. The count never resets between research phases;
    cost-stressed reruns are not new hypotheses."""
    seen = set()
    regs = [registry] + [ExperimentRegistry(p) for p in prior if Path(p).resolve() != registry.path.resolve()]
    for reg in regs:
        seen |= {r["spec_hash"] for r in reg.records()
                 if r["family"].startswith("active/") and r["spec"].get("cost_multiplier") == 1.0}
    return len(seen)


def _to_json(r: ConfigRun) -> dict:
    return {"key": r.key, "family": r.family, "params": r.params, "instrument": r.instrument,
            "cost": r.cost_multiplier, "audit": r.audit,
            "daily": {str(k): v for k, v in r.daily.items()}, "trades": {str(k): v for k, v in r.trades.items()}}


def _from_json(d: dict) -> ConfigRun:
    s = lambda m: pd.Series({pd.Timestamp(k): v for k, v in m.items()}, dtype=float)  # noqa: E731
    return ConfigRun(d["key"], d["family"], d["params"], d["instrument"], d["cost"], s(d["daily"]), s(d["trades"]),
                     d["audit"])


def _replay(plan: ResearchPlan, wf: WalkForwardResult, stressed: dict[str, ConfigRun]) -> WalkForwardResult:
    """Same fold choices, returns from the cost-stressed runs."""
    pieces, srs, n = [], [], 0
    from hedge_fund.validation.stats import sharpe
    for (_, _, te_s, te_e), c in zip(plan.folds(), wf.choices):
        if c["choice"] is None:
            test = pd.Series(0.0, index=pd.date_range(te_s, te_e, freq="D", tz="UTC"))
        else:
            run = stressed[c["choice"]]
            test = _slice(run.daily, te_s, te_e)
            n += run.trades_in(te_s, te_e)
        pieces.append(test)
        srs.append(sharpe(test.to_numpy()))
    return WalkForwardResult(pd.concat(pieces), wf.choices, srs, n)


def run_plan(plan: ResearchPlan, out_dir: Path | str, *, catalog_path: str | None = None, workers: int = 1,
             registry_path: Path | str | None = None, code_commit: str = "",
             prior_registries: list[Path | str] | None = None, require_hypothesis: bool = True) -> dict:
    """prior_registries: earlier registries whose trials count toward the deflated Sharpe
    (default: every registry under runs/active/research)."""
    prior = all_registries() if prior_registries is None else list(prior_registries)
    if require_hypothesis:
        from hedge_fund.trading.hypothesis import require_hypotheses
        require_hypotheses(plan)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = catalog_path or str(Catalog().path)
    registry = ExperimentRegistry(registry_path or out_dir / "experiments.jsonl")
    gates = load_gates()
    base = [(None, None, i, f, p, 1.0) for i in plan.instruments for f in plan.families for p in FAMILIES[f].configs()]
    runs = _load_or_run(plan, base, out_dir, catalog_path, workers, registry, code_commit)
    n_trials = count_trials(registry, prior)
    lines, wfs_all, by_family = [], [], {}
    for f in plan.families:
        for i in plan.instruments:
            group = [r for r in runs.values() if r.family == f and r.instrument == i and r.cost_multiplier == 1.0]
            wf = walk_forward(plan, group)
            ev = evidence(plan, wf, group, n_trials=n_trials)
            res = evaluate_gates(ev, gates)
            stress = {}
            chosen = sorted({c["choice"] for c in wf.choices if c["choice"]})
            for cost in [c for c in plan.cost_multipliers if c != 1.0]:
                tasks = [(None, None, i, f, json.loads(k)["p"], cost) for k in chosen]
                st = _load_or_run(plan, tasks, out_dir, catalog_path, workers, registry, code_commit)
                remap = {k: st[json.dumps({**json.loads(k), "c": cost}, sort_keys=True)] for k in chosen}
                swf = _replay(plan, wf, remap)
                sres = evaluate_gates(evidence(plan, swf, group, n_trials=n_trials), gates)
                stress[str(cost)] = {"passed": sres.passed, "checks": sres.checks, "values": sres.values}
            passed = res.passed and all(s["passed"] for s in stress.values())
            lines.append({"family": f, "instrument": i, "passed": passed, "checks": res.checks, "values": res.values,
                          "cost_stress": stress, "choices": wf.choices})
            wfs_all.append(wf)
            by_family.setdefault(f, []).append(wf)
    from hedge_fund.validation.stats import sharpe
    combos = {}
    base_runs = [r for r in runs.values() if r.cost_multiplier == 1.0]
    for name, members in [*((f"family:{f}", m) for f, m in by_family.items()), ("all", wfs_all)]:
        oos = combine([m.oos for m in members])
        folds = [sharpe(_slice(oos, te_s, te_e).to_numpy()) for _, _, te_s, te_e in plan.folds()]
        cwf = WalkForwardResult(oos, [], folds, sum(m.n_oos_trades for m in members))
        res = evaluate_gates(evidence(plan, cwf, base_runs, n_trials=n_trials), gates)
        combos[name] = {"passed": res.passed, "checks": res.checks, "values": res.values, "members": len(members)}
    summary = {"plan": plan.model_dump(mode="json"), "plan_hash": plan.plan_hash(), "n_trials": n_trials,
               "gates_hash": gates.config_hash(), "lines": lines, "combinations": combos,
               "any_passed": any(x["passed"] for x in lines)}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary
