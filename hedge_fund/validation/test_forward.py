"""Forward paper evaluation vs research mode — including attempts to cross the line."""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

import pytest

from hedge_fund.research.governance import ExperimentLedger, ResearchManifest
from hedge_fund.research.pipeline import GovernanceViolation
from hedge_fund.systematic import MarketPanel
from hedge_fund.systematic.backtest import BacktestConfig
from hedge_fund.systematic.execution import CostModel, FillTiming
from hedge_fund.systematic.risk import RiskConfig
from hedge_fund.systematic.strategies import REGISTRY, StrategyConfig, SystematicStrategy, TimeSeriesMomentum
from hedge_fund.systematic.testing import SyntheticMarket, weekdays
from hedge_fund.validation.forward import ForwardEvaluationError, ForwardEvaluator, ForwardLedgerTampered
from hedge_fund.validation.holdout_guard import (
    EvaluationMode, HoldoutAccessDenied, HoldoutBook, HoldoutDeclaration, HoldoutStatus, Purpose, check_access,
    load_holdouts, use_holdouts,
)

DAYS = weekdays("2021-01-04", "2022-12-30")
NAMES = ["SPY", "A", "B", "C"]
FWD = HoldoutDeclaration(id="fwd-test", start="2022-07-01", end="2022-12-30", embargo_days=14,
                         status=HoldoutStatus.SEALED, reason="synthetic forward holdout",
                         evaluation_mode=EvaluationMode.FORWARD)
REPO = Path(__file__).resolve().parents[2]


def market() -> SyntheticMarket:
    m = SyntheticMarket()
    m.add_series("SPY", DAYS, SyntheticMarket.random_walk(DAYS, seed=0, drift=0.0003, vol=0.01), volume=5e7)
    for i, s in enumerate("ABC"):
        m.add_series(s, DAYS, SyntheticMarket.random_walk(DAYS, seed=i + 1, drift=0.0005, vol=0.015), volume=2e6)
    return m


def config() -> BacktestConfig:
    return BacktestConfig(start="2000-01-03", end="2000-01-04", capital=10_000.0, rebalance="weekly",
                          timing=FillTiming.NEXT_OPEN, costs=CostModel(commission_bps=1.0, half_spread_bps=2.0),
                          risk=RiskConfig(max_position=0.5, vol_target=None, max_turnover=None,
                                          max_cluster_weight=1.0, max_drawdown=1.0, max_daily_loss=1.0))


class Clock:
    def __init__(self, day: str) -> None:
        self.day = day

    def __call__(self) -> str:
        return self.day


@pytest.fixture
def env(tmp_path):
    book = HoldoutBook(load_holdouts().declarations + [FWD])
    with use_holdouts(book):
        clock = Clock("2022-07-01")
        ev = ForwardEvaluator(tmp_path / "fwd.jsonl", "fwd-test", data_client=market(), universe=NAMES,
                              history_start=DAYS[0], clock=clock)
        yield ev, clock


def freeze(ev, cid="C1", family="trend", parent=None, strategy=None):
    s = strategy or TimeSeriesMomentum(lookback=60, skip=0, vol_lookback=20, exclude=("SPY",))
    return ev.freeze(cid, family=family, strategy_specs=[s.spec()], backtest_config=config(),
                     actor="human:reviewer", parent_id=parent)


# -- research mode: zero access ---------------------------------------------------------------


@pytest.mark.parametrize("purpose", [p for p in Purpose if p is not Purpose.FORWARD_PAPER_EVALUATION])
def test_research_purposes_have_zero_access(env, purpose):
    with pytest.raises(HoldoutAccessDenied):
        check_access("2022-07-01", "2022-07-01", purpose=purpose)
    with pytest.raises(HoldoutAccessDenied):
        check_access("2021-01-04", "2022-06-20", purpose=purpose)                     # embargo


def test_forward_purpose_without_a_running_step_has_no_access(env):
    with pytest.raises(HoldoutAccessDenied):
        check_access("2022-07-01", "2022-07-01", purpose=Purpose.FORWARD_PAPER_EVALUATION)
    with pytest.raises(HoldoutAccessDenied):
        MarketPanel.build(market(), NAMES, DAYS[0], "2022-07-05")


def test_forward_holdout_has_no_one_shot_window_read(env, tmp_path):
    led = ExperimentLedger(tmp_path / "l.jsonl")
    m = ResearchManifest(program="p", version=1, created_at="x", code_commit="0123456789abcdef", code_dirty=False,
                         code_hash="a" * 64, config_hashes={"g": "g"}, data_hashes={"d": "d"})
    led.register_manifest(m)
    led.register_hypothesis("H1", statement="s", family="f", manifest_hash=m.manifest_hash())
    with pytest.raises(HoldoutAccessDenied, match="forward holdout"):
        led.start_trial(hypothesis_id="H1", params={}, stage="holdout", window=(FWD.start, FWD.end),
                        manifest_hash=m.manifest_hash(), code_hash=m.code_hash, config_hash="c", data_hash="d")
    from hedge_fund.validation.holdout_guard import active_book
    with pytest.raises(HoldoutAccessDenied, match="forward holdout"):
        with active_book().evaluation("fwd-test", actor="human:reviewer", ledger=led, trial_id="T000002"):
            pass


def test_reviewed_phase_b_holdout_is_forward_and_real_time_only(tmp_path):
    d = load_holdouts().get("phase-b-prospective")
    assert (d.start, d.end, d.fence_start, d.evaluation_mode) == ("2026-10-01", "2027-09-30", "2026-09-01",
                                                                  EvaluationMode.FORWARD)
    with pytest.raises(HoldoutAccessDenied, match="real time"):
        ForwardEvaluator(tmp_path / "f.jsonl", "phase-b-prospective", data_client=None, universe=[],
                         history_start="2020-01-02", clock=Clock("2027-12-31"))
    with pytest.raises(HoldoutAccessDenied, match="not a sealed forward"):
        ForwardEvaluator(tmp_path / "f.jsonl", "phase-a-2023", data_client=None, universe=[],
                         history_start="2020-01-02")


# -- forward mode: sequential, frozen, append-only ----------------------------------------------


def test_steps_follow_real_time_one_session_at_a_time(env):
    ev, clock = env
    frz = freeze(ev)
    assert frz["start_after"] == "2022-06-30" and frz["generation"] == 1
    assert ev.step("C1") is None                                # 2022-07-01 not completed yet
    clock.day = "2022-07-06"                                    # completed through 07-05
    assert [ev.step("C1")["session"] for _ in range(3)] == ["2022-07-01", "2022-07-04", "2022-07-05"]
    assert ev.step("C1") is None                                # no peeking past the clock
    recs = ev.review(actor="human:reviewer", purpose="human_review")
    obs = [r for r in recs if "session" in r]
    assert [o["session"] for o in obs] == ["2022-07-01", "2022-07-04", "2022-07-05"]
    assert all(o["spec_hash"] == frz["spec_hash"] and o["clock"] == "injected" for o in obs)
    assert all(t["fill_session"] == o["session"] for o in obs for t in o["trades"])


def test_freeze_and_review_are_human_only_and_purpose_bound(env):
    ev, _ = env
    with pytest.raises(GovernanceViolation):
        ev.freeze("X", family="f", strategy_specs=[], backtest_config=config(), actor="ai:claude")
    freeze(ev)
    for purpose in ("optimization", "selection", "feature_discovery", "llm_candidate_generation", "dashboard"):
        with pytest.raises(HoldoutAccessDenied):
            ev.review(actor="human:reviewer", purpose=purpose)
    with pytest.raises(GovernanceViolation):
        ev.review(actor="ai:claude", purpose="human_review")
    with pytest.raises(GovernanceViolation):
        ev.review(actor="system", purpose="human_review")


def test_no_in_place_change(env, monkeypatch):
    ev, clock = env
    freeze(ev)
    with pytest.raises(ForwardEvaluationError, match="already frozen"):
        freeze(ev)                                              # same id, any spec: refused
    clock.day = "2022-07-06"
    ev.step("C1")
    ev.step("C1")
    # data changed under an observed session -> the replay check refuses
    ev.data_client.bars["A"][DAYS.index("2022-07-01")] = ev.data_client.bars["A"][DAYS.index("2022-07-01")].__class__(
        "2022-07-01", 1.0, 1.0, 1.0, 1.0, 2e6)
    ev.data_client.bars["SPY"][DAYS.index("2022-07-01")] = ev.data_client.bars["SPY"][
        DAYS.index("2022-07-01")].__class__("2022-07-01", 1.0, 1.0, 1.0, 1.0, 5e7)
    with pytest.raises(ForwardEvaluationError, match="replay differs"):
        ev.step("C1")
    monkeypatch.setattr(ev, "_code_hash", lambda: "f" * 64)
    with pytest.raises(ForwardEvaluationError, match="code changed"):
        ev.step("C1")


def test_records_are_tamper_evident(env):
    ev, clock = env
    freeze(ev)
    clock.day = "2022-07-06"
    ev.step("C1")
    ev.step("C1")
    lines = ev.chain.path.read_text().splitlines()
    e = json.loads(lines[-1])
    e["payload"]["session"] = "2022-07-01"                     # try to "rewind"
    ev.chain.path.write_text("\n".join(lines[:-1] + [json.dumps(e)]) + "\n")
    with pytest.raises(ForwardLedgerTampered):
        ev.step("C1")
    ev.chain.path.write_text("\n".join(lines[:-1]) + "\n")      # drop the last observation
    with pytest.raises(ForwardLedgerTampered, match="head"):
        ev.step("C1")


def test_modified_candidate_is_a_new_generation_that_cannot_reuse_observed_sessions(env):
    ev, clock = env
    freeze(ev)
    clock.day = "2022-07-08"
    for _ in range(4):
        ev.step("C1")                                           # observed through 07-07
    ev.review(actor="human:reviewer", purpose="human_review")
    clock.day = "2022-07-08"
    tweaked = TimeSeriesMomentum(lookback=40, skip=0, vol_lookback=20, exclude=("SPY",))
    with pytest.raises(ForwardEvaluationError, match="lineage"):
        freeze(ev, cid="C2", strategy=tweaked)                  # no silent sibling
    with pytest.raises(ForwardEvaluationError, match="parent"):
        freeze(ev, cid="C2", strategy=tweaked, parent="nope")
    child = freeze(ev, cid="C2", strategy=tweaked, parent="C1")
    assert child["generation"] == 2 and child["start_after"] >= "2022-07-07"
    assert ev.step("C2") is None                                # nothing unseen is due yet
    clock.day = "2022-07-12"
    first = ev.step("C2")["session"]
    assert first > "2022-07-07"


# -- adversarial: code running inside a step --------------------------------------------------------

PROBE: list[str] = []


class ProbeConfig(StrategyConfig):
    pass


class Probe(SystematicStrategy):
    """A strategy that tries to read beyond what forward evaluation allows."""

    name, version, Config = "probe", "test", ProbeConfig

    def _generate(self, view, names):
        def attempt(label, fn):
            try:
                fn()
                PROBE.append(f"{label}:ALLOWED")
            except (HoldoutAccessDenied, LookupError, ValueError):
                PROBE.append(f"{label}:denied")

        session = view.session
        attempt("future_view", lambda: view.bars("close", end="2022-12-30"))
        attempt("future_panel", lambda: MarketPanel.build(market(), NAMES, DAYS[0], "2022-12-30"))
        attempt("research_purpose", lambda: check_access(session, session, purpose=Purpose.OPTIMIZATION))
        result: list[str] = []
        from hedge_fund.validation.holdout_guard import active_book
        book = active_book()                                     # same declarations, but a fresh context

        def in_thread():
            try:
                with use_holdouts(book):
                    MarketPanel.build(market(), NAMES, DAYS[0], session)
                result.append("ALLOWED")
            except HoldoutAccessDenied:
                result.append("denied")
        t = threading.Thread(target=in_thread)
        t.start()
        t.join()
        PROBE.append(f"thread:{result[0]}")
        return [self._signal(view, t, 0.0, {}) for t in names]


def test_code_inside_a_step_cannot_read_beyond_the_session_or_for_research(env, monkeypatch):
    ev, clock = env
    monkeypatch.setitem(REGISTRY, "probe", Probe)
    PROBE.clear()
    ev.freeze("P1", family="probe", strategy_specs=[Probe().spec()], backtest_config=config(),
              actor="human:reviewer")
    clock.day = "2022-07-20"
    while ev.step("P1") is not None:                            # through several rebalance decisions
        pass
    assert PROBE and all(p.endswith("denied") for p in PROBE), PROBE
    PROBE.clear()
    with pytest.raises(HoldoutAccessDenied):                    # and nothing leaks after the step
        MarketPanel.build(market(), NAMES, DAYS[0], "2022-07-01")


# -- static: research code never touches the forward path or the guard's tokens ----------------------

ALLOWED = {"hedge_fund/validation/holdout_guard.py", "hedge_fund/validation/forward.py"}


def _python_files():
    for root in ("hedge_fund", "runs"):
        for p in (REPO / root).rglob("*.py"):
            rel = p.relative_to(REPO).as_posix()
            if "__pycache__" in rel or p.name.startswith("test_") or rel in ALLOWED:
                continue
            yield rel, p


def test_no_research_code_imports_the_forward_evaluator_or_guard_internals():
    offenders = []
    for rel, p in _python_files():
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.endswith("validation.forward"):
                    offenders.append(f"{rel}: imports the forward evaluator")
                if node.module.endswith("holdout_guard") and any(a.name.startswith("_") for a in node.names):
                    offenders.append(f"{rel}: imports guard internals")
            if isinstance(node, ast.Import) and any(a.name.endswith("validation.forward") for a in node.names):
                offenders.append(f"{rel}: imports the forward evaluator")
            if isinstance(node, ast.Attribute) and node.attr in ("_forward", "_open", "_book", "_default"):
                offenders.append(f"{rel}: touches {node.attr}")
    assert offenders == []
