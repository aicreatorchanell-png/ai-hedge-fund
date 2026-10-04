"""Adversarial audit of a frozen research plan's methodology (read-only).

    python runs/active/research/audit_plan.py runs/active/research/plan_crypto_v1.json

Probes the plan's families on synthetic data, checks costs, fills, leakage, holdout
sealing and survivorship, samples development-window catalog data (bar convention,
timezone, liquidity) and reads the stored per-run ambiguity counts. It neither changes
nor re-runs the experiment. Writes <plan_id>/AUDIT.json next to the results.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from hedge_fund.paths import CACHE_DIR
from hedge_fund.trading.audit import audit_plan
from hedge_fund.trading.data.catalog import Catalog
from hedge_fund.trading.research import ResearchPlan

HERE = Path(__file__).resolve().parent


def main(path: str) -> int:
    doc = json.loads(Path(path).read_text())
    plan = ResearchPlan.model_validate(doc["plan"])
    runs = CACHE_DIR / "research" / plan.plan_id / "runs"
    audits = [json.loads(f.read_text())["audit"] for f in sorted(runs.glob("*.json"))] if runs.exists() else None
    report = audit_plan(plan, catalog=Catalog(), run_audits=audits)
    out = HERE / plan.plan_id / "AUDIT.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(report.to_json())
    print(json.dumps({"passed": report.passed, "counts": report.counts()}))
    for c in report.checks:
        if c.status != "PASS":
            print(c.status, c.name, "|", c.detail)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
