"""Run a frozen multi-leg (portfolio) research plan and audit it.

    python runs/active/research/run_portfolio_plan.py runs/active/research/plan_<id>.json

Refuses an edited plan (hash). Writes <plan_id>/summary.json, experiments.jsonl (registry)
and AUDIT.json; cached per-run series go to the private cache (for analyze_plan.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from hedge_fund.paths import CACHE_DIR
from hedge_fund.trading.data.catalog import Catalog
from hedge_fund.trading.portfolio import audit_portfolio_plan, load_daily, run_portfolio_plan
from hedge_fund.trading.research import ResearchPlan

HERE = Path(__file__).resolve().parent


def main(path: str) -> int:
    doc = json.loads(Path(path).read_text())
    plan = ResearchPlan.model_validate(doc["plan"])
    if plan.plan_hash() != doc["plan_hash"]:
        raise SystemExit("plan hash mismatch: the frozen plan was edited")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    out = HERE / plan.plan_id
    out.mkdir(exist_ok=True)
    data = load_daily(plan.instruments, plan.dev_start, plan.dev_end)
    summary = run_portfolio_plan(plan, CACHE_DIR / "research" / plan.plan_id, registry_path=out / "experiments.jsonl",
                                 code_commit=commit, data=data)
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    report = audit_portfolio_plan(plan, catalog=Catalog(), data=data)
    (out / "AUDIT.json").write_text(report.to_json())
    print(json.dumps({"any_passed": summary["any_passed"], "any_combination_passed": summary["any_combination_passed"],
                      "n_trials": summary["n_trials"], "audit": report.counts()}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
