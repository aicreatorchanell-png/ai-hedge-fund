"""Run a frozen research plan (development window only). NOT run in the data phase.

    python runs/active/research/run_plan.py runs/active/research/plan_crypto_v1.json [--workers 4]

Refuses to run if the plan file's hash differs from the hash recorded inside it (the
plan was edited after it was frozen). Results: runs/active/research/<plan_id>/summary.json
(statistics only) and experiments.jsonl (the trial registry); per-run return series are
cached in the private cache.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from hedge_fund.paths import CACHE_DIR
from hedge_fund.trading.research import ResearchPlan
from hedge_fund.trading.runner import run_plan

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    doc = json.loads(Path(a.plan).read_text())
    plan = ResearchPlan.model_validate(doc["plan"])
    if plan.plan_hash() != doc["plan_hash"]:
        raise SystemExit("plan hash mismatch: the frozen plan was edited")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    out = CACHE_DIR / "research" / plan.plan_id
    summary = run_plan(plan, out, workers=a.workers, registry_path=HERE / plan.plan_id / "experiments.jsonl",
                       code_commit=commit)
    (HERE / plan.plan_id).mkdir(exist_ok=True)
    (HERE / plan.plan_id / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps({"any_passed": summary["any_passed"], "n_trials": summary["n_trials"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
