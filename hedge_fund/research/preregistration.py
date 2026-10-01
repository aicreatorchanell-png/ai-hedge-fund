"""Phase B pre-registration: everything about a test fixed before any result exists.

A `PreRegistration` is a frozen, hashed document with one section per
decision a researcher could otherwise make after seeing results:

    hypotheses        statement, rationale, null, expected sign, falsifier
    universe          point-in-time definition, filters, reconstitution
    periods           development window, inner validation scheme, the sealed
                      holdout (by id from configs/holdouts.yaml), embargo
    benchmark         primary (total return) and secondary benchmarks
    execution         fill timing (next session only), order type
    costs             commission, half-spread, impact/slippage, participation,
                      stress multipliers
    borrow            shorting, borrow fee, locate rule
    sizing            method, capital, max position, vol target
    exposure          gross / net limits and the beta band
    statistics        primary/secondary metrics, minimum sample
    multiple_testing  correction method, alpha, planned trials
    gates             the validation-gates file and hash, plus Phase B gates
                      that may only be *stricter*
    budget            maximum trials, LLM spend, data requests, wall clock

`validate_against_environment` checks the document against the repository as
it is: the holdout must be sealed and the development window must end before
its fence; the gates hash must match configs/validation-gates.yaml and no
Phase B gate may be weaker; costs may not be zero; shorting needs a borrow
fee *and* a simulator that charges it. `lock` adds a human approval
(protected action "approve_preregistration") and the content hash; a locked
document cannot be edited — any change is a new version.

`BudgetGuard` enforces the research budget against the experiment ledger:
it refuses to start a trial that would exceed the pre-registered limits.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hedge_fund.research.governance import canonical_json, hash_json
from hedge_fund.systematic.execution import FillTiming

PREREG_SCHEMA = 1


class PreregistrationError(ValueError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Hypothesis(_Frozen):
    id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{1,31}$")
    family: str = Field(min_length=1)
    statement: str = Field(min_length=20)
    economic_rationale: str = Field(min_length=20)
    null_hypothesis: str = Field(min_length=10)
    expected_sign: Literal["positive", "negative"]
    signal_definition: str = Field(min_length=20)
    parameter_grid: dict[str, list] = Field(description="every value that will be tried; nothing else")
    falsified_if: str = Field(min_length=10)
    known_risks: list[str] = Field(default_factory=list)

    @property
    def n_parameter_sets(self) -> int:
        n = 1
        for v in self.parameter_grid.values():
            n *= max(len(v), 1)
        return n


class Universe(_Frozen):
    definition: str = Field(min_length=20)
    point_in_time_source: str = Field(min_length=5)
    selection_rule: str = Field(min_length=10)
    reconstitution: Literal["monthly", "quarterly", "annual"]
    target_size: int = Field(ge=1)
    min_price: float = Field(ge=0)
    min_adv_usd: float = Field(ge=0)
    survivorship: str = Field(min_length=10, description="how delisted names enter and leave")
    exclusions: list[str] = Field(default_factory=list)


class Periods(_Frozen):
    development: tuple[str, str]
    validation_scheme: str = Field(min_length=10, description="inner walk-forward / purged CV inside development")
    holdout_id: str
    embargo_days: int = Field(ge=0)

    @model_validator(mode="after")
    def _order(self):
        if not self.development[0] < self.development[1]:
            raise ValueError("development start must precede its end")
        return self


class Benchmark(_Frozen):
    primary: str
    total_return: bool = True
    secondary: list[str] = Field(default_factory=list)
    risk_free: str = Field(min_length=1)


class Execution(_Frozen):
    timing: FillTiming
    order_type: Literal["market_on_open", "market_on_close"]
    decision_time: str = Field(min_length=5)
    rebalance: Literal["daily", "weekly", "monthly", "quarterly"]


class Costs(_Frozen):
    commission_per_share: float = Field(ge=0)
    commission_bps: float = Field(ge=0)
    commission_min: float = Field(ge=0)
    half_spread_bps: float = Field(ge=0)
    impact_coef: float = Field(ge=0)
    max_participation: float = Field(gt=0, le=1)
    stress_multipliers: list[float] = Field(min_length=1)

    @model_validator(mode="after")
    def _not_free(self):
        if self.half_spread_bps <= 0 and self.commission_bps <= 0 and self.commission_per_share <= 0 \
                and self.commission_min <= 0:
            raise ValueError("costs may not be zero: trading is never free")
        if not any(m >= 2.0 for m in self.stress_multipliers):
            raise ValueError("cost stress must include at least a 2x multiplier")
        return self


class Borrow(_Frozen):
    allow_short: bool
    borrow_fee_bps_annual: float = Field(ge=0)
    hard_to_borrow_policy: str = Field(min_length=5)
    locate_required: bool = True

    @model_validator(mode="after")
    def _fee(self):
        if self.allow_short and self.borrow_fee_bps_annual <= 0:
            raise ValueError("shorting needs a positive borrow fee assumption")
        return self


class Sizing(_Frozen):
    method: str = Field(min_length=5)
    capital: float = Field(gt=0)
    max_position: float = Field(gt=0, le=1)
    vol_target_annual: float | None = Field(None, gt=0)


class Exposure(_Frozen):
    max_gross: float = Field(gt=0)
    max_net: float
    min_net: float
    beta_band: tuple[float, float] = Field(description="allowed ex-post beta to the primary benchmark")

    @model_validator(mode="after")
    def _consistent(self):
        if self.min_net > self.max_net or self.beta_band[0] > self.beta_band[1]:
            raise ValueError("exposure bounds are inverted")
        if self.max_gross > 1.0:
            raise ValueError("leverage (max_gross > 1) is out of scope for Phase B")
        return self


class Statistics(_Frozen):
    primary_metric: str = Field(min_length=5)
    secondary_metrics: list[str]
    min_daily_observations: int = Field(ge=252)
    min_round_trips: int = Field(ge=30)


class MultipleTesting(_Frozen):
    methods: list[Literal["deflated_sharpe", "pbo", "holm", "benjamini_hochberg", "white_reality_check"]]
    family_alpha: float = Field(gt=0, le=0.1)
    planned_trials: int = Field(ge=1)
    trial_count_source: Literal["experiment_ledger"] = "experiment_ledger"

    @model_validator(mode="after")
    def _methods(self):
        if "deflated_sharpe" not in self.methods:
            raise ValueError("the deflated Sharpe ratio (with all ledger trials) is required")
        if not {"holm", "benjamini_hochberg", "white_reality_check"} & set(self.methods):
            raise ValueError("a family-wise or FDR correction across hypotheses is required")
        return self


class Gates(_Frozen):
    gates_file: str
    gates_hash: str
    phase_b_overrides: dict[str, float] = Field(default_factory=dict,
                                                description="stricter-only thresholds on top of the gates file")
    holdout_pass: dict[str, float]


class Budget(_Frozen):
    max_trials_total: int = Field(ge=1)
    max_trials_per_hypothesis: int = Field(ge=1)
    max_llm_usd: float = Field(ge=0)
    max_data_requests: int = Field(ge=0)
    max_wall_clock_hours: float = Field(gt=0)
    stop_rule: str = Field(min_length=10)


class PreRegistration(_Frozen):
    schema_version: int = PREREG_SCHEMA
    program: str
    version: int = Field(ge=1)
    title: str = Field(min_length=10)
    status: Literal["draft", "locked"] = "draft"
    hypotheses: list[Hypothesis] = Field(min_length=1)
    universe: Universe
    periods: Periods
    benchmark: Benchmark
    execution: Execution
    costs: Costs
    borrow: Borrow
    sizing: Sizing
    exposure: Exposure
    statistics: Statistics
    multiple_testing: MultipleTesting
    gates: Gates
    budget: Budget
    approved_by: str | None = None
    approved_at: str | None = None
    preregistration_hash: str | None = None

    @model_validator(mode="after")
    def _internal(self):
        ids = [h.id for h in self.hypotheses]
        if len(ids) != len(set(ids)):
            raise ValueError("hypothesis ids must be unique")
        planned = sum(h.n_parameter_sets for h in self.hypotheses)
        if planned > self.multiple_testing.planned_trials:
            raise ValueError(f"parameter grids hold {planned} sets but only {self.multiple_testing.planned_trials} "
                             f"trials are planned")
        if self.multiple_testing.planned_trials > self.budget.max_trials_total:
            raise ValueError("planned trials exceed the trial budget")
        if any(h.n_parameter_sets > self.budget.max_trials_per_hypothesis for h in self.hypotheses):
            raise ValueError("a hypothesis grid exceeds the per-hypothesis trial budget")
        if self.borrow.allow_short != (self.exposure.min_net < 0):
            raise ValueError("borrow.allow_short must match a negative min_net")
        if self.status == "locked" and (not self.approved_by or self.preregistration_hash != self.content_hash()):
            raise ValueError("a locked pre-registration needs its approval and a matching hash")
        return self

    def content_hash(self) -> str:
        return hash_json(self.model_dump(mode="json", exclude={"status", "approved_by", "approved_at",
                                                               "preregistration_hash"}))

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, width=110)


def load_preregistration(path: Path | str) -> PreRegistration:
    return PreRegistration(**yaml.safe_load(Path(path).read_text()))


# Things the deterministic engine cannot simulate yet; a document needing them cannot be locked.
SUPPORTED_RECONSTITUTION = frozenset({"annual", "quarterly"})   # hedge_fund.universe.builder.reconstitution_dates
# SEC XBRL phase-in finished for all filers in mid-2011 and discovery reads two
# years of public-float frames, so a survivorship-aware PIT universe needs D >= this.
XBRL_DISCOVERY_FROM = "2011-07-01"

SIMULATOR_LIMITS = {
    "borrow_fees": "the backtester charges no borrow fee on short positions",
}


def validate_against_environment(pr: PreRegistration, *, holdouts=None, gates_path: Path | str | None = None,
                                 ) -> list[str]:
    """Problems that block locking, given the repository as it is. Empty list = ready to lock."""
    from hedge_fund.validation.gates import DEFAULT_PATH, load_gates
    from hedge_fund.validation.holdout_guard import HoldoutStatus, active_book

    problems: list[str] = []
    book = holdouts or active_book()
    try:
        h = book.get(pr.periods.holdout_id)
    except KeyError:
        problems.append(f"holdout {pr.periods.holdout_id!r} is not declared in configs/holdouts.yaml")
    else:
        if h.status is not HoldoutStatus.SEALED:
            problems.append(f"holdout {h.id} is {h.status.value}; a pre-registration needs a sealed holdout")
        if pr.periods.development[1] >= h.fence_start:
            problems.append(f"development ends {pr.periods.development[1]}, inside the fence of {h.id} "
                            f"(from {h.fence_start})")
        if pr.periods.embargo_days < h.embargo_days:
            problems.append("embargo shorter than the declared holdout embargo")
    try:
        book.check_access(*pr.periods.development, purpose="development")
    except PermissionError as exc:
        problems.append(str(exc))

    gates = load_gates(gates_path or DEFAULT_PATH)
    if pr.gates.gates_hash != gates.config_hash():
        problems.append("gates hash does not match configs/validation-gates.yaml")
    current = gates.model_dump()
    higher_is_stricter = {"min_trades", "min_oos_sharpe_annual", "min_deflated_sharpe",
                          "min_walk_forward_positive_share"}
    lower_is_stricter = {"max_pbo", "max_drawdown"}
    for k, v in pr.gates.phase_b_overrides.items():
        if k in higher_is_stricter and v < current[k] or k in lower_is_stricter and v > current[k]:
            problems.append(f"gate {k}={v} is weaker than the gates file ({current[k]})")
        elif k not in higher_is_stricter | lower_is_stricter:
            problems.append(f"unknown gate {k}")
    if pr.universe.reconstitution not in SUPPORTED_RECONSTITUTION:
        problems.append(f"universe reconstitution {pr.universe.reconstitution!r} is not supported by the "
                        f"point-in-time universe builder ({', '.join(sorted(SUPPORTED_RECONSTITUTION))})")
    if pr.periods.development[0] < XBRL_DISCOVERY_FROM:
        problems.append(f"development starts {pr.periods.development[0]}, before point-in-time universe "
                        f"discovery is possible (SEC XBRL public-float frames: from {XBRL_DISCOVERY_FROM})")
    if pr.borrow.allow_short:
        problems.append(f"shorting needs borrow fees in the simulator: {SIMULATOR_LIMITS['borrow_fees']}")
    if pr.execution.timing is FillTiming.NEXT_OPEN and pr.execution.order_type != "market_on_open" or \
            pr.execution.timing is FillTiming.NEXT_CLOSE and pr.execution.order_type != "market_on_close":
        problems.append("order type does not match the fill timing")
    return problems


def lock(pr: PreRegistration, *, approver: str, **env) -> PreRegistration:
    """Human approval; refuses while any environment problem remains."""
    from hedge_fund.research.pipeline import authorize

    authorize(approver, "approve_preregistration")
    if pr.status == "locked":
        raise PreregistrationError("already locked; changes need a new version")
    problems = validate_against_environment(pr, **env)
    if problems:
        raise PreregistrationError("cannot lock: " + "; ".join(problems))
    data = pr.model_dump(mode="json")
    data.update(status="locked", approved_by=approver, approved_at=datetime.now(timezone.utc).isoformat(),
                preregistration_hash=pr.content_hash())
    return PreRegistration(**data)


class BudgetGuard:
    """Refuses trials beyond the pre-registered budget, counted from the experiment ledger."""

    def __init__(self, pr: PreRegistration) -> None:
        if pr.status != "locked":
            raise PreregistrationError("research runs only under a locked pre-registration")
        self.pr = pr
        self.ids = {h.id for h in pr.hypotheses}

    def check_can_start(self, ledger, hypothesis_id: str, params: dict) -> None:
        if hypothesis_id not in self.ids:
            raise BudgetExceeded(f"{hypothesis_id} is not pre-registered")
        grid = next(h for h in self.pr.hypotheses if h.id == hypothesis_id).parameter_grid
        for k, v in params.items():
            if k not in grid or v not in grid[k]:
                raise BudgetExceeded(f"{k}={v!r} is outside the pre-registered grid of {hypothesis_id}")
        trials = [t for t in ledger.trials() if t["hypothesis_id"] in self.ids]
        if len({(t["hypothesis_id"], t["params_hash"]) for t in trials}) >= self.pr.budget.max_trials_total:
            raise BudgetExceeded("total trial budget exhausted")
        mine = {t["params_hash"] for t in trials if t["hypothesis_id"] == hypothesis_id}
        if hash_json(params) not in mine and len(mine) >= self.pr.budget.max_trials_per_hypothesis:
            raise BudgetExceeded(f"trial budget for {hypothesis_id} exhausted")

    def check_spend(self, spent_usd: float, next_estimate_usd: float) -> None:
        if spent_usd + next_estimate_usd > self.pr.budget.max_llm_usd:
            raise BudgetExceeded(f"LLM budget: {spent_usd:.2f} + {next_estimate_usd:.2f} > "
                                 f"{self.pr.budget.max_llm_usd:.2f}")


__all__ = ["BudgetExceeded", "BudgetGuard", "PreRegistration", "PreregistrationError", "SIMULATOR_LIMITS",
           "canonical_json", "load_preregistration", "lock", "validate_against_environment"]
