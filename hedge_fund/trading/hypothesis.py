"""Economic hypothesis layer: no strategy family is researched without a written,
human-approved explanation of why its edge should exist.

A hypothesis (research/hypotheses/<ID>.yaml) must answer:

    why_edge_exists       the mechanism, in economic terms
    counterparty          who is on the other side (type from COUNTERPARTIES + description)
    why_they_pay          why that counterparty systematically loses or pays us
    why_it_persists       why arbitrage has not removed it (limits to arbitrage, risk, constraints)
    disappears_when       conditions under which the edge should vanish (at least one)
    predictions           testable consequences beyond "the backtest is profitable" (at least one)

It also states its `edge_type` (EDGE_TYPES). Justifications built only from indicator
vocabulary ("the RSI crosses...") are rejected: an indicator is a measurement, not a
reason. Only a human can approve a hypothesis (protected action "approve_hypothesis");
AI may draft one.

`require_hypotheses(plan)` is called before any research plan runs:

    - every family in the plan must be linked to an APPROVED hypothesis
    - a family already tested on overlapping instruments and development dates by an
      earlier plan cannot be tested there again (no re-tuning on seen development data)
    - plans frozen before this rule (LEGACY_PLANS) keep their recorded status and are
      never re-run
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HYPOTHESIS_DIR = Path(__file__).resolve().parents[2] / "research" / "hypotheses"
RESEARCH_DIR = Path(__file__).resolve().parents[2] / "runs" / "active" / "research"
LEGACY_PLANS = frozenset({"crypto-v1"})          # frozen and run before the hypothesis rule (2026-10-04)

EDGE_TYPES = ("risk_premium", "behavioral", "structural_flow", "liquidity_provision", "information", "carry")
COUNTERPARTIES = ("hedgers", "liquidity_demanders", "forced_sellers", "index_or_mandate_rebalancers",
                  "retail_or_sentiment_traders", "risk_averse_investors", "market_makers", "arbitrage_capital",
                  "other")
INDICATOR_WORDS = re.compile(r"\b(rsi|macd|ema|sma|moving average|bollinger|crossover|cross|stochastic|"
                             r"indicator|oscillator|donchian|atr|signal line|overbought|oversold)\b", re.I)
ECONOMIC_WORDS = re.compile(r"\b(risk|premium|compensat|insurance|hedg|liquidity|inventory|flow|forced|"
                            r"constraint|mandate|behavio|underreact|overreact|attention|funding|leverage|"
                            r"margin|liquidat|information|arbitrage|capital|demand|supply|cost)\w*", re.I)


class HypothesisRejected(ValueError):
    pass


class Approval(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    by: str
    on: str
    note: str = ""

    @field_validator("by")
    @classmethod
    def _human(cls, v: str) -> str:
        if not v.startswith("human:") or len(v) <= len("human:"):
            raise ValueError("only a human (human:<name>) can approve a hypothesis")
        return v


class EconomicHypothesis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^H-[A-Z0-9-]{2,40}$")
    title: str = Field(min_length=10)
    families: tuple[str, ...] = Field(min_length=1)
    markets: tuple[str, ...] = Field(min_length=1)
    edge_type: Literal[EDGE_TYPES]                       # type: ignore[valid-type]
    why_edge_exists: str = Field(min_length=120)
    counterparty_type: Literal[COUNTERPARTIES]           # type: ignore[valid-type]
    counterparty: str = Field(min_length=40)
    why_they_pay: str = Field(min_length=80)
    why_it_persists: str = Field(min_length=80)
    disappears_when: tuple[str, ...] = Field(min_length=1)
    predictions: tuple[str, ...] = Field(min_length=1)
    references: tuple[str, ...] = ()
    signal_sketch: str = ""                   # how the edge would be measured; parameters come later
    data_required: tuple[str, ...] = ()       # and whether each is available under the data policy
    feasibility: Literal["ready", "needs_data", "needs_infrastructure", "blocked"] = "needs_data"
    drafted_by: str
    status: Literal["draft", "approved", "rejected", "retired"] = "draft"
    approval: Approval | None = None

    @model_validator(mode="after")
    def _economic(self) -> EconomicHypothesis:
        for name in ("why_edge_exists", "why_they_pay", "why_it_persists"):
            text = getattr(self, name)
            ind = len(INDICATOR_WORDS.findall(text))
            eco = len(ECONOMIC_WORDS.findall(text))
            if eco == 0 or ind > eco:
                raise ValueError(f"{name}: reads as an indicator description, not an economic reason "
                                 f"({ind} indicator terms, {eco} economic terms)")
        for d in self.disappears_when + self.predictions:
            if len(d) < 20:
                raise ValueError("each condition and prediction must be a full, testable sentence")
        if self.status == "approved" and self.approval is None:
            raise ValueError("an approved hypothesis needs a human approval record")
        if self.approval is not None and self.status == "draft":
            raise ValueError("a draft cannot carry an approval")
        return self

    def content_hash(self) -> str:
        body = self.model_dump(mode="json", exclude={"status", "approval"})
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]

    def approve(self, actor: str, note: str = "") -> EconomicHypothesis:
        from hedge_fund.research.pipeline import authorize

        authorize(actor, "approve_hypothesis")
        return self.model_copy(update={"status": "approved",
                                       "approval": Approval(by=actor, on=date.today().isoformat(), note=note)})


def load_hypotheses(root: Path = HYPOTHESIS_DIR) -> dict[str, EconomicHypothesis]:
    out = {}
    for p in sorted(Path(root).glob("H-*.yaml")):
        h = EconomicHypothesis(**yaml.safe_load(p.read_text()))
        if h.id != p.stem:
            raise HypothesisRejected(f"{p.name}: id {h.id} does not match the file name")
        out[h.id] = h
    return out


def prior_plans(root: Path = RESEARCH_DIR) -> list[dict]:
    """Every frozen plan file: its families, instruments and development window."""
    out = []
    for p in sorted(Path(root).glob("plan_*.json")):
        plan = json.loads(p.read_text())["plan"]
        out.append({"plan_id": plan["plan_id"], "families": set(plan["families"]),
                    "instruments": set(plan["instruments"]), "dev": (plan["dev_start"], plan["dev_end"])})
    return out


def require_hypotheses(plan, *, hypotheses: dict[str, EconomicHypothesis] | None = None,
                       history: list[dict] | None = None) -> dict[str, str]:
    """Refuse a plan whose families lack an approved hypothesis or that re-tests a family on
    development data it has already been tested on. Returns family -> hypothesis id."""
    if plan.plan_id in LEGACY_PLANS:
        raise HypothesisRejected(f"{plan.plan_id} is a completed legacy plan; it is never re-run")
    hyps = load_hypotheses() if hypotheses is None else hypotheses
    history = prior_plans() if history is None else history
    links: dict[str, str] = {}
    problems = []
    for fam in plan.families:
        approved = [h for h in hyps.values() if fam in h.families and h.status == "approved"]
        if not approved:
            problems.append(f"family {fam!r} has no approved economic hypothesis")
            continue
        links[fam] = approved[0].id
        for prev in history:
            if prev["plan_id"] == plan.plan_id or fam not in prev["families"]:
                continue
            same_inst = prev["instruments"] & set(plan.instruments)
            overlap = plan.dev_start <= prev["dev"][1] and plan.dev_end >= prev["dev"][0]
            if same_inst and overlap:
                problems.append(f"family {fam!r} was already tested by {prev['plan_id']} on {sorted(same_inst)} "
                                f"over {prev['dev'][0]}..{prev['dev'][1]}; re-testing on that data is tuning")
    if problems:
        raise HypothesisRejected("; ".join(problems))
    return links
