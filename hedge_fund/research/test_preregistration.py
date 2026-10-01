"""Phase B pre-registration framework."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hedge_fund.research.governance import ExperimentLedger, ResearchManifest
from hedge_fund.research.pipeline import GovernanceViolation
from hedge_fund.research.preregistration import (
    BudgetExceeded, BudgetGuard, PreRegistration, PreregistrationError, load_preregistration, lock,
    validate_against_environment,
)

TEMPLATE = Path(__file__).resolve().parents[2] / "runs" / "phase-b" / "preregistration.template.yaml"


def raw() -> dict:
    return yaml.safe_load(TEMPLATE.read_text())


def variant(**sections) -> PreRegistration:
    d = raw()
    for k, v in sections.items():
        d[k] = {**d[k], **v} if isinstance(v, dict) and isinstance(d.get(k), dict) else v
    return PreRegistration(**d)


def test_template_is_complete_and_ready_to_lock():
    pr = load_preregistration(TEMPLATE)
    assert pr.status == "draft" and pr.preregistration_hash is None
    assert validate_against_environment(pr) == []
    required = {"hypotheses", "universe", "periods", "benchmark", "execution", "costs", "borrow", "sizing",
                "exposure", "statistics", "multiple_testing", "gates", "budget"}
    assert required <= set(PreRegistration.model_fields)


def test_lock_is_human_only_and_freezes_the_hash():
    pr = load_preregistration(TEMPLATE)
    with pytest.raises(GovernanceViolation):
        lock(pr, approver="ai:claude")
    locked = lock(pr, approver="human:reviewer")
    assert locked.status == "locked" and locked.preregistration_hash == pr.content_hash()
    with pytest.raises(PreregistrationError, match="already locked"):
        lock(locked, approver="human:reviewer")
    with pytest.raises(ValidationError, match="matching hash"):              # edits break the locked hash
        PreRegistration(**{**locked.model_dump(mode="json"), "title": "edited after locking ..."})
    reloaded = PreRegistration(**yaml.safe_load(locked.to_yaml()))
    assert reloaded == locked


@pytest.mark.parametrize("change, match", [
    ({"periods": {"holdout_id": "phase-a-2023"}}, "contaminated"),
    ({"periods": {"holdout_id": "nope"}}, "not declared"),
    ({"periods": {"development": ["2006-01-03", "2026-09-15"]}}, "fence"),
    ({"periods": {"embargo_days": 5}}, "embargo"),
    ({"gates": {"gates_hash": "0" * 16}}, "gates hash"),
    ({"gates": {"phase_b_overrides": {"min_deflated_sharpe": 0.9}}}, "weaker"),
    ({"gates": {"phase_b_overrides": {"max_pbo": 0.6}}}, "weaker"),
    ({"execution": {"order_type": "market_on_close"}}, "order type"),
    ({"universe": {"reconstitution": "monthly"}}, "not supported"),
    ({"periods": {"development": ["2006-01-03", "2026-07-31"]}}, "XBRL"),
])
def test_environment_problems_block_locking(change, match):
    pr = variant(**change)
    problems = validate_against_environment(pr)
    assert any(match in p for p in problems), problems
    with pytest.raises(PreregistrationError):
        lock(pr, approver="human:reviewer")


def test_stricter_gates_are_allowed():
    pr = variant(gates={"phase_b_overrides": {"min_deflated_sharpe": 0.97, "max_drawdown": 0.2}})
    assert validate_against_environment(pr) == []


def test_shorting_needs_a_fee_and_a_simulator_that_charges_it():
    with pytest.raises(ValidationError, match="borrow fee"):
        variant(borrow={"allow_short": True}, exposure={"min_net": -0.5})
    pr = variant(borrow={"allow_short": True, "borrow_fee_bps_annual": 50.0}, exposure={"min_net": -0.5})
    assert any("borrow fees in the simulator" in p for p in validate_against_environment(pr))


@pytest.mark.parametrize("change, match", [
    ({"costs": {"commission_bps": 0.0, "half_spread_bps": 0.0}}, "zero"),
    ({"costs": {"stress_multipliers": [1.0, 1.5]}}, "2x"),
    ({"multiple_testing": {"methods": ["pbo", "holm"]}}, "deflated"),
    ({"multiple_testing": {"methods": ["deflated_sharpe"]}}, "correction"),
    ({"multiple_testing": {"planned_trials": 1}}, "parameter grids"),
    ({"budget": {"max_trials_total": 1}}, "budget"),
    ({"exposure": {"max_gross": 1.5}}, "leverage"),
    ({"statistics": {"min_round_trips": 10}}, "min_round_trips"),
    ({"status": "locked"}, "approval"),
])
def test_internal_rules(change, match):
    with pytest.raises(ValidationError, match=match):
        variant(**change)


def test_budget_guard_counts_every_ledger_trial(tmp_path):
    pr = variant(budget={"max_trials_total": 3, "max_trials_per_hypothesis": 2},
                 multiple_testing={"planned_trials": 2})
    with pytest.raises(PreregistrationError, match="locked"):
        BudgetGuard(pr)
    guard = BudgetGuard(lock(pr, approver="human:reviewer"))
    led = ExperimentLedger(tmp_path / "l.jsonl")
    m = ResearchManifest(program="phase-b", version=1, created_at="2026-10-01T00:00:00+00:00",
                         code_commit="0123456789abcdef", code_dirty=False, code_hash="a" * 64,
                         config_hashes={"g": "g" * 64}, data_hashes={"p": "d" * 64})
    led.register_manifest(m)
    led.register_hypothesis("H-EXAMPLE", statement="s", family="f", manifest_hash=m.manifest_hash())
    kw = dict(hypothesis_id="H-EXAMPLE", stage="development", window=("2011-07-01", "2020-12-31"),
              manifest_hash=m.manifest_hash(), code_hash=m.code_hash, config_hash="c" * 64, data_hash="d" * 64)
    with pytest.raises(BudgetExceeded, match="outside the pre-registered grid"):
        guard.check_can_start(led, "H-EXAMPLE", {"lookback": 100})
    with pytest.raises(BudgetExceeded, match="not pre-registered"):
        guard.check_can_start(led, "H-OTHER", {"lookback": 126})
    for lb in (126, 252):
        guard.check_can_start(led, "H-EXAMPLE", {"lookback": lb})
        led.finish_trial(led.start_trial(params={"lookback": lb}, **kw), "FAIL")
    guard.check_can_start(led, "H-EXAMPLE", {"lookback": 126})            # re-run of a logged set: no new trial
    with pytest.raises(BudgetExceeded):
        guard.check_spend(0.0, 0.01)                                       # template budgets $0 of LLM spend
