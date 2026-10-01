"""The Phase B candidate hypotheses are well-formed proposals — and only proposals."""

from __future__ import annotations

from pathlib import Path

import yaml

from hedge_fund.research.preregistration import Hypothesis, PreRegistration, validate_against_environment

PHASE_B = Path(__file__).resolve().parents[2] / "runs" / "phase-b"


def candidates() -> list[Hypothesis]:
    return [Hypothesis(**h) for h in yaml.safe_load((PHASE_B / "candidate_hypotheses.yaml").read_text())["hypotheses"]]


def test_candidates_validate_and_are_long_only_with_small_grids():
    hs = candidates()
    assert len({h.id for h in hs}) == len(hs) == 5
    assert sum(h.n_parameter_sets for h in hs) == 10
    assert all(h.n_parameter_sets <= 4 for h in hs)
    assert all(h.expected_sign == "positive" for h in hs)


def test_candidates_fit_a_template_preregistration_without_changing_any_gate():
    base = yaml.safe_load((PHASE_B / "preregistration.template.yaml").read_text())
    base["hypotheses"] = [h.model_dump() for h in candidates()]
    base["multiple_testing"]["planned_trials"] = 10
    base["budget"]["max_trials_total"] = 10
    pr = PreRegistration(**base)
    assert pr.status == "draft"
    assert validate_against_environment(pr) == []


def test_no_candidate_was_registered_or_run():
    assert not list(PHASE_B.glob("*.jsonl")) and not list(PHASE_B.glob("*.head"))
    assert not (PHASE_B / "results.json").exists()
