"""Paper deployment manifest: which strategies may run in paper, with which evidence.

    runs/active/paper/deployment.yaml
      health_thresholds_version: "1.0.0"
      strategies:
        - family: <registered family>
          params: {...}                    # the validated configuration, unchanged
          instrument: "BTC/USD.KRAKEN"
          hypothesis_id: H-...              # approved economic hypothesis
          validation_summary: runs/active/research/<plan>/summary.json
          approved_by: human:<name>         # human approval for PAPER
          expectations: {...}               # BacktestExpectations for the health monitor

`load_deployment` refuses a strategy unless: the approval is by a human, its hypothesis
is approved, its (family, instrument) line PASSED in the referenced validation summary
including cost stress, and the health thresholds version matches the versioned file.
No strategy passes today; the only runnable strategy is the sandbox plumbing check.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from hedge_fund.trading.health import BacktestExpectations

ROOT = Path(__file__).resolve().parents[2]


class DeploymentRejected(PermissionError):
    pass


class DeployedStrategy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    family: str
    params: dict
    instrument: str
    hypothesis_id: str
    validation_summary: str
    approved_by: str
    expectations: BacktestExpectations

    @field_validator("approved_by")
    @classmethod
    def _human(cls, v: str) -> str:
        if not v.startswith("human:") or len(v) <= 6:
            raise ValueError("paper deployment needs a human approval (human:<name>)")
        return v


class Deployment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    health_thresholds_version: str
    strategies: tuple[DeployedStrategy, ...] = ()


def load_deployment(path: Path | str, *, hypotheses=None, thresholds=None) -> Deployment:
    from hedge_fund.trading.health import load_thresholds
    from hedge_fund.trading.hypothesis import load_hypotheses

    d = Deployment(**(yaml.safe_load(Path(path).read_text()) or {}))
    t = thresholds or load_thresholds()
    if d.health_thresholds_version != t.version:
        raise DeploymentRejected(f"health thresholds {d.health_thresholds_version} != versioned {t.version}")
    hyps = load_hypotheses() if hypotheses is None else hypotheses
    for s in d.strategies:
        h = hyps.get(s.hypothesis_id)
        if h is None or h.status != "approved" or s.family not in h.families:
            raise DeploymentRejected(f"{s.family}: hypothesis {s.hypothesis_id} missing, not approved or unrelated")
        summary = ROOT / s.validation_summary
        if not summary.exists():
            raise DeploymentRejected(f"{s.family}: validation summary {s.validation_summary} not found")
        lines = json.loads(summary.read_text()).get("lines", [])
        line = next((x for x in lines if x["family"] == s.family and x["instrument"].split(".")[0]
                     == s.instrument.split(".")[0].replace("/", "")), None)
        if line is None or not line.get("passed"):
            raise DeploymentRejected(f"{s.family} on {s.instrument}: no PASSED validation line (gates + cost stress)")
    return d
