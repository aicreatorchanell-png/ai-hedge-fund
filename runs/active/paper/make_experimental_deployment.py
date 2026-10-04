"""Write runs/active/paper/deployment_experimental.yaml (EXPERIMENTAL / UNVALIDATED forward test).

Candidate: funding_crowding (H-FUNDING-CROWDING), the best out-of-sample result of the plans
run so far (family combination Sharpe 0.59, audit without FAIL) but FAILED validation
(deflated Sharpe, bootstrap). Deployed on the owner's instruction of 2026-10-04 to forward-test
the complete autonomous loop on paper, not as a validated strategy.

Mapping: each Binance perpetual of funding-crowding-v1 -> the Kraken Futures perpetual of the
same coin (PF_*); parameters = the configuration chosen on the training window of the last
walk-forward fold for that coin (no test-window information); funding = Kraken's own hourly
settlements observed from the strategy's start only (forward holdout), with the quantile history
seeded from that coin's development-window Binance settlements converted to per-hour rates.
Health expectations = that coin's walk-forward out-of-sample statistics.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from hedge_fund.paths import CACHE_DIR
from hedge_fund.trading.health import load_thresholds
from hedge_fund.trading.research import ResearchPlan, walk_forward
from hedge_fund.trading.runner import _from_json

ROOT = Path(__file__).resolve().parents[3]
KRAKEN = {"BTCUSDT": "PF_XBTUSD", "ETHUSDT": "PF_ETHUSD", "XRPUSDT": "PF_XRPUSD", "LTCUSDT": "PF_LTCUSD",
          "ADAUSDT": "PF_ADAUSD", "BNBUSDT": "PF_BNBUSD"}
NOTE = ("EXPERIMENTAL / UNVALIDATED forward test of the autonomous paper loop, approved by the owner on "
        "2026-10-04 ('Deploy the best technically safe existing candidate in PAPER/SIMULATION ONLY ... Clearly "
        "label it EXPERIMENTAL / UNVALIDATED'). funding-crowding-v1 verdict: FAIL. Not evidence of profitability.")


def main() -> int:
    plan_doc = json.loads((ROOT / "runs/active/research/plan_funding_crowding_v1.json").read_text())
    plan = ResearchPlan.model_validate(plan_doc["plan"])
    summary = json.loads((ROOT / "runs/active/research/funding-crowding-v1/summary.json").read_text())
    runs = [_from_json(json.loads(f.read_text()))
            for f in (CACHE_DIR / "research" / plan.plan_id / "runs").glob("*.json")]
    out = []
    for line in summary["lines"]:
        coin = line["instrument"].split("-")[0]
        choice = line["choices"][-1]["choice"]
        params = json.loads(choice)["p"]
        group = [r for r in runs if r.instrument == line["instrument"] and r.cost_multiplier == 1.0]
        wf = walk_forward(plan, group)
        by_key = {r.key: r for r in group}
        trades = pd.concat([by_key[c["choice"]].trades[(by_key[c["choice"]].trades.index >= pd.Timestamp(f[2], tz="UTC"))
                                                       & (by_key[c["choice"]].trades.index <= pd.Timestamp(f[3], tz="UTC"))]
                            for f, c in zip(plan.folds(), wf.choices) if c.get("choice")])
        wins, losses = trades[trades > 0], trades[trades <= 0]
        days = (pd.Timestamp(plan.folds()[-1][3]) - pd.Timestamp(plan.folds()[0][2])).days + 1
        v = line["values"]
        exp = {"sharpe_annual": float(v["oos_sharpe_annual"]), "max_drawdown": max(float(v["max_drawdown"]), 0.01),
               "win_rate": float(len(wins) / len(trades)), "avg_win": float(wins.mean() / plan.starting_cash),
               "avg_loss": float(-losses.mean() / plan.starting_cash), "trades_per_day": len(trades) / days,
               "slippage_bps": 3.0, "cost_per_trade": 0.0016, "trades_per_year": len(trades) / days * 365}
        out.append({"family": "funding_crowding", "instrument": f"{KRAKEN[coin]}.KRAKEN",
                    "params": {**{k: v for k, v in params.items()}, "funding_source": "kraken_futures",
                               "funding_symbol": KRAKEN[coin], "funding_seed_symbol": coin},
                    "hypothesis_id": "H-FUNDING-CROWDING",
                    "validation_summary": "runs/active/research/funding-crowding-v1/summary.json",
                    "approved_by": "human:owner", "expectations": {k: round(x, 6) for k, x in exp.items()},
                    "status": "experimental_unvalidated", "note": NOTE})
    doc = {"health_thresholds_version": load_thresholds().version, "strategies": out}
    path = Path(__file__).resolve().parent / "deployment_experimental.yaml"
    path.write_text("# EXPERIMENTAL / UNVALIDATED — PAPER / SIMULATION ONLY — REAL MONEY DISABLED\n"
                    + yaml.safe_dump(doc, sort_keys=False, width=110))
    print(path.name, [(s["instrument"], s["params"]) for s in out])
    _ = np
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
