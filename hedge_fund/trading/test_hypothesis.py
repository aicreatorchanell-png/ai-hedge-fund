"""Economic hypothesis layer: no family is researched without an approved economic reason,
approval is human-only, and failed families cannot be re-tested on the same data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hedge_fund.research.pipeline import PROTECTED_ACTIONS, GovernanceViolation
from hedge_fund.trading.hypothesis import (LEGACY_PLANS, EconomicHypothesis, HypothesisRejected, load_hypotheses,
                                           prior_plans, require_hypotheses)
from hedge_fund.trading.research import ResearchPlan, WalkForward

GOOD = dict(
    id="H-LIQ-PREMIUM",
    title="Compensation for absorbing forced liquidation flow",
    families=("liq_rebound",), markets=("crypto",), edge_type="liquidity_provision",
    why_edge_exists=("Leveraged traders hit by margin calls must sell immediately regardless of price; "
                     "absorbing that forced flow ties up capital and inventory risk, so the liquidity "
                     "provider earns a premium as prices partly revert once the forced selling ends."),
    counterparty_type="forced_sellers",
    counterparty="Over-leveraged futures traders whose positions are liquidated by the exchange engine.",
    why_they_pay=("They are price-insensitive at the moment of liquidation: the exchange sells their margin "
                  "into thin books, accepting a concession to obtain immediate liquidity."),
    why_it_persists=("Capital willing to buy during crashes is scarce and itself constrained by risk limits, "
                     "funding costs and drawdown aversion, so the premium is not fully arbitraged away."),
    disappears_when=("Exchange leverage limits are cut so that forced liquidations become rare events.",),
    predictions=("Rebounds are larger after liquidation clusters than after equal-sized organic falls.",),
    drafted_by="ai:claude-opus-5-5",
)


@pytest.fixture(autouse=True)
def placeholder_family(monkeypatch):
    """Registry entry for the hypothetical family name (gating tests only; no new strategy)."""
    from hedge_fund.trading import families
    f = families.FAMILIES["ema_trend"]
    monkeypatch.setitem(families.FAMILIES, "liq_rebound", families.Family("liq_rebound", "test", f.strategy,
                                                                          f.config, f.grid))


def plan(**kw):
    base = dict(plan_id="next-1", families=("liq_rebound",), instruments=("BTCUSDT.BINANCE",),
                dev_start="2019-01-01", dev_end="2021-12-31", walk_forward=WalkForward(train_months=12, test_months=6))
    return ResearchPlan(**{**base, **kw})


def test_a_complete_economic_hypothesis_validates_and_hashes():
    h = EconomicHypothesis(**GOOD)
    assert h.status == "draft" and len(h.content_hash()) == 16
    assert h.content_hash() == EconomicHypothesis(**{**GOOD, "title": GOOD["title"]}).content_hash()


@pytest.mark.parametrize("field,value", [
    ("why_edge_exists", "short"),
    ("counterparty", "someone"),
    ("disappears_when", ()),
    ("predictions", ()),
    ("edge_type", "magic"),
    ("counterparty_type", "the market"),
])
def test_missing_or_vague_answers_are_rejected(field, value):
    with pytest.raises(ValidationError):
        EconomicHypothesis(**{**GOOD, field: value})


def test_indicator_only_justifications_are_rejected():
    bad = ("When the fast EMA crosses above the slow EMA and the RSI leaves oversold while the MACD signal line "
           "turns up and the Bollinger band widens, the crossover indicator gives a buy signal on the chart.")
    with pytest.raises(ValidationError, match="indicator"):
        EconomicHypothesis(**{**GOOD, "why_edge_exists": bad})


def test_approval_is_human_only():
    assert "approve_hypothesis" in PROTECTED_ACTIONS
    h = EconomicHypothesis(**GOOD)
    for actor in ("ai:claude-opus-5-5", "ai:kimi-k3", "system"):
        with pytest.raises(GovernanceViolation):
            h.approve(actor)
    a = h.approve("human:owner", "reviewed")
    assert a.status == "approved" and a.approval.by == "human:owner"
    with pytest.raises(ValidationError):                                  # forged approval record
        EconomicHypothesis(**{**GOOD, "status": "approved", "approval": {"by": "ai:claude", "on": "2026-10-04"}})
    with pytest.raises(ValidationError):                                  # approved without a record
        EconomicHypothesis(**{**GOOD, "status": "approved"})


def test_plans_need_an_approved_hypothesis_for_every_family():
    draft = EconomicHypothesis(**GOOD)
    with pytest.raises(HypothesisRejected, match="no approved economic hypothesis"):
        require_hypotheses(plan(), hypotheses={draft.id: draft}, history=[])
    ok = draft.approve("human:owner")
    assert require_hypotheses(plan(), hypotheses={ok.id: ok}, history=[]) == {"liq_rebound": "H-LIQ-PREMIUM"}


def test_failed_families_cannot_be_retested_on_the_same_development_data():
    ok = EconomicHypothesis(**{**GOOD, "families": ("ema_trend",)}).approve("human:owner")
    history = prior_plans()
    assert any(p["plan_id"] == "crypto-v1" for p in history)
    with pytest.raises(HypothesisRejected, match="re-testing on that data is tuning"):
        require_hypotheses(plan(families=("ema_trend",)), hypotheses={ok.id: ok}, history=history)
    # other instruments, or development dates the old plan never used, are allowed
    require_hypotheses(plan(families=("ema_trend",), instruments=("EURUSD.DUKASCOPY",)),
                       hypotheses={ok.id: ok}, history=history)


def test_legacy_plan_is_never_rerun_and_the_runner_requires_hypotheses(tmp_path):
    from hedge_fund.trading.runner import run_plan
    assert "crypto-v1" in LEGACY_PLANS
    legacy = ResearchPlan.model_validate(json.loads(
        (Path(__file__).resolve().parents[2] / "runs/active/research/plan_crypto_v1.json").read_text())["plan"])
    with pytest.raises(HypothesisRejected, match="legacy"):
        require_hypotheses(legacy)
    with pytest.raises(HypothesisRejected):
        run_plan(plan(), tmp_path / "out", prior_registries=[])            # no approved hypothesis on disk
    cli = (Path(__file__).resolve().parents[2] / "runs/active/research/run_plan.py").read_text()
    assert "require_hypothesis" not in cli                                 # the CLI cannot bypass the rule


def test_hypothesis_files_load_and_must_match_their_name(tmp_path):
    (tmp_path / "H-LIQ-PREMIUM.yaml").write_text(yaml.safe_dump(json.loads(EconomicHypothesis(**GOOD).model_dump_json())))
    assert set(load_hypotheses(tmp_path)) == {"H-LIQ-PREMIUM"}
    (tmp_path / "H-OTHER.yaml").write_text((tmp_path / "H-LIQ-PREMIUM.yaml").read_text())
    with pytest.raises(HypothesisRejected):
        load_hypotheses(tmp_path)
    repo = load_hypotheses()                                               # the AI drafts in research/hypotheses
    assert repo and all(h.status == "draft" and h.approval is None for h in repo.values())  # none approved by AI
