"""Freeze (pre-register) a research plan before it is run. Refuses to overwrite.

    python runs/active/research/freeze_plan.py funding-crowding-v1
    python runs/active/research/freeze_plan.py vol-trend-v1

Writes plan_<id>.json with the plan, its hash, the folds, the family grids, the linked
approved hypotheses (with content hashes), the gate-config hash, the code commit the
plan was frozen at, and the pre-registered decision rule. Run it, commit the file,
then run run_plan.py on it; nothing in it may change after it is committed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from hedge_fund.trading.data.markets import FUNDING_CROWDING_SET, FX, INDICES, VOL_TREND_CRYPTO_SET
from hedge_fund.trading.families import FAMILIES
from hedge_fund.trading.hypothesis import load_hypotheses, require_hypotheses
from hedge_fund.trading.research import ResearchPlan, WalkForward
from hedge_fund.validation.gates import load_gates

HERE = Path(__file__).resolve().parent

DECISION_RULE = (
    "PASS if and only if (a) at least one instrument line, or the equal-weight family combination, passes "
    "every gate in configs/validation-gates.yaml (hash recorded here) both at cost x1.0 and at cost x2.0, "
    "with the deflated Sharpe computed on the cumulative trial count of every registry under "
    "runs/active/research, and (b) the adversarial audit of this plan reports no FAIL. Otherwise FAIL. "
    "Regime, worst-fold, parameter-stability, concentration and CPCV diagnostics are reported but cannot "
    "turn a FAIL into a PASS. The sealed holdout (from reserve_start) is not read.")

PLANS = {
    "funding-crowding-v1": dict(
        plan=ResearchPlan(
            plan_id="funding-crowding-v1", families=("funding_crowding",),
            instruments=tuple(f"{s}.BINANCE" for s in FUNDING_CROWDING_SET),
            dev_start="2020-01-01", dev_end="2025-08-31", reserve_start="2025-09-01",
            walk_forward=WalkForward(train_months=24, test_months=6, step_months=6),
            cost_multipliers=(1.0, 2.0), periods_per_year=365, min_train_trades=10, bar_minutes=1),
        rationale=(
            "Perpetual futures (shorts allowed, actual funding paid/received) on the perpetuals of the "
            "crypto-v1 universe (same selection rule). Development data starts 2020-01 (first month of "
            "funding and perpetual data for most of the set). min_train_trades is 10, not 20: the family "
            "trades only after rare funding extremes (fixed before any run). 2 x 2 x 2 grid = 8 configs "
            "per instrument: extreme quantile 0.95/0.99, ATR stop 2/4, time stop 24/72 hourly bars "
            "(the hypothesis predicts reversal within one to three days)."),
    ),
    "vol-trend-v1": dict(
        plan=ResearchPlan(
            plan_id="vol-trend-v1", families=("vol_managed_trend",),
            instruments=(*(f"{s}.BINANCE" for s in VOL_TREND_CRYPTO_SET),
                         *(s.instrument_id for s in FX.values()), *(s.instrument_id for s in INDICES.values())),
            dev_start="2020-01-01", dev_end="2025-08-31", reserve_start="2025-09-01",
            walk_forward=WalkForward(train_months=24, test_months=6, step_months=6),
            cost_multipliers=(1.0, 2.0), periods_per_year=365, min_train_trades=6, bar_minutes=1440),
        rationale=(
            "Daily bars, fills at the next daily open. Cross-asset: six crypto perpetuals chosen by rule "
            "(every USD-M perpetual with data in 2020-01, minus the crypto-v1 coins, so no crypto instrument "
            "of crypto-v1 is reused), four FX majors and two index CFDs (Dukascopy). All lines share the "
            "2020-01..2025-08 window. Calendar-day returns (weekends flat) annualized with 365. "
            "min_train_trades is 6: a multi-week trend rule trades a few times a year per instrument. "
            "3 x 2 grid = 6 configs per instrument: lookback 20/60/120 days, volatility stop 2.5/5 x "
            "20-day volatility; risk per trade fixed, so exposure is inversely proportional to volatility. "
            "The hypothesis' prediction concerns the cross-asset portfolio, so the family combination is "
            "the main candidate. Data gap known at freeze: the Dukascopy datafeed refused DEUIDXEUR months "
            "2025-04..2025-08 (HTTP 429), so that line ends 2025-03-31 (recorded in "
            "runs/active/data/dukascopy_daily_quality.json); EOS perpetual data ends 2025-05 (delisting)."),
    ),
}


def main(plan_id: str) -> int:
    spec = PLANS[plan_id]
    plan: ResearchPlan = spec["plan"]
    out = HERE / f"plan_{plan_id.replace('-', '_')}.json"
    if out.exists():
        raise SystemExit(f"{out.name} exists: a frozen plan is never rewritten")
    links = require_hypotheses(plan)
    hyps = load_hypotheses()
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    doc = {
        "plan": plan.model_dump(mode="json"), "plan_hash": plan.plan_hash(), "n_configs": plan.n_configs(),
        "folds": plan.folds(), "grids": {f: FAMILIES[f].grid for f in plan.families},
        "hypotheses": {f: {"id": h, "content_hash": hyps[h].content_hash(), "approval": hyps[h].approval.model_dump()}
                       for f, h in links.items()},
        "gates_hash": load_gates().config_hash(), "code_commit_at_freeze": commit,
        "rationale": spec["rationale"], "decision_rule": DECISION_RULE,
        "frozen_on": date.today().isoformat(), "status": "frozen, not run",
    }
    out.write_text(json.dumps(doc, indent=1))
    print(out.name, plan.plan_hash(), plan.n_configs(), "configs,", len(plan.folds()), "folds")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
