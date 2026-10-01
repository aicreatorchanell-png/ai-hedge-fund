"""Holdout access protection: the contaminated Phase A holdout and the sealed-holdout fence."""

from __future__ import annotations

import pytest

from hedge_fund.backtesting.engine import BacktestEngine
from hedge_fund.research.governance import ExperimentLedger, ResearchManifest
from hedge_fund.research.pipeline import GovernanceViolation, authorize
from hedge_fund.systematic import MarketPanel
from hedge_fund.systematic.testing import SyntheticMarket, weekdays
from hedge_fund.validation import ExperimentRegistry, HoldoutViolation, LockedHoldout
from hedge_fund.validation.holdout_guard import (
    HoldoutAccessDenied, HoldoutBook, HoldoutDeclaration, HoldoutStatus, Purpose, active_book, check_access,
    load_holdouts, use_holdouts,
)

DAYS = weekdays("2021-01-04", "2022-12-30")
TEST_HOLDOUT = HoldoutDeclaration(id="synthetic", start="2022-07-01", end="2022-12-30", embargo_days=14,
                                  status=HoldoutStatus.SEALED, reason="test holdout on synthetic data")


def book_with(*extra: HoldoutDeclaration) -> HoldoutBook:
    return HoldoutBook(load_holdouts().declarations + list(extra))


def market() -> SyntheticMarket:
    m = SyntheticMarket()
    m.add_series("SPY", DAYS, SyntheticMarket.random_walk(DAYS, seed=1, drift=0.0003, vol=0.01), volume=10_000_000)
    return m


def manifest() -> ResearchManifest:
    return ResearchManifest(program="phase-b", version=1, created_at="2026-10-01T00:00:00+00:00",
                            code_commit="0123456789abcdef", code_dirty=False, code_hash="a" * 64,
                            config_hashes={"holdouts": "h" * 64}, data_hashes={"prices": "d" * 64})


def ledger(tmp_path):
    led = ExperimentLedger(tmp_path / "ledger.jsonl")
    m = manifest()
    led.register_manifest(m)
    led.register_hypothesis("H1", statement="s", family="f", manifest_hash=m.manifest_hash())
    return led, m


def holdout_trial(led, m, params, window=("2022-07-01", "2022-12-30"), hypothesis="H1"):
    return led.start_trial(hypothesis_id=hypothesis, params=params, stage="holdout", window=window,
                           manifest_hash=m.manifest_hash(), code_hash=m.code_hash, config_hash="c" * 64,
                           data_hash="d" * 64)


# -- the reviewed declarations -------------------------------------------------------


def test_reviewed_file_marks_phase_a_contaminated_and_seals_phase_b():
    book = load_holdouts()
    a, b = book.get("phase-a-2023"), book.get("phase-b-prospective")
    assert (a.start, a.end, a.status) == ("2023-02-01", "2026-06-30", HoldoutStatus.CONTAMINATED)
    assert b.status is HoldoutStatus.SEALED and b.fence_start == "2026-09-01"
    assert len(book.source_hash) == 64 and active_book().source_hash == book.source_hash


@pytest.mark.parametrize("window", [("2023-02-01", "2026-06-30"), ("2024-01-01", "2024-12-31"),
                                    ("2022-06-01", "2023-03-01"), ("2026-06-30", "2026-08-15")])
def test_phase_a_holdout_can_never_be_a_holdout_again(tmp_path, window):
    with pytest.raises(HoldoutViolation, match="contaminated"):
        LockedHoldout(*window, ExperimentRegistry(tmp_path / "e"), family="phase-b")
    with pytest.raises(HoldoutAccessDenied):
        active_book().check_new_holdout(*window)


def test_contaminated_window_remains_usable_as_development_data():
    check_access("2023-02-01", "2026-06-30", purpose=Purpose.DEVELOPMENT)


@pytest.mark.parametrize("purpose", [p for p in Purpose])
def test_sealed_phase_b_window_is_fenced_for_every_purpose_outside_an_evaluation(purpose):
    check_access("2016-01-01", "2026-08-31", purpose=purpose)                 # before the embargo: fine
    for window in (("2026-09-01", "2026-09-01"), ("2016-01-01", "2026-09-15"), ("2027-01-01", "2027-03-01"),
                   ("2027-09-30", "2028-01-01")):
        with pytest.raises(HoldoutAccessDenied, match="phase-b-prospective"):
            check_access(*window, purpose=purpose)


def test_overrides_may_add_but_never_drop_or_unseal_reviewed_holdouts():
    reviewed = load_holdouts().declarations
    with pytest.raises(HoldoutAccessDenied, match="drops or alters"):
        with use_holdouts(HoldoutBook([])):
            pass
    unsealed = [d.model_copy(update={"status": HoldoutStatus.CONSUMED}) if d.status is HoldoutStatus.SEALED else d
                for d in reviewed]
    with pytest.raises(HoldoutAccessDenied, match="drops or alters"):
        with use_holdouts(HoldoutBook(unsealed)):
            pass
    with use_holdouts(book_with(TEST_HOLDOUT)) as book:
        assert active_book() is book
    assert active_book() is not book


def test_open_locked_holdout_is_a_human_only_action():
    with pytest.raises(GovernanceViolation):
        authorize("ai:claude", "open_locked_holdout")
    with pytest.raises(GovernanceViolation):
        authorize("system", "open_locked_holdout")
    authorize("human:reviewer", "open_locked_holdout")


# -- the market-data fence ---------------------------------------------------------------


def test_panel_build_and_views_cannot_reach_a_sealed_holdout():
    with use_holdouts(book_with(TEST_HOLDOUT)):
        ok = MarketPanel.build(market(), ["SPY"], DAYS[0], "2022-06-16")       # ends before the embargo
        ok.as_of("2022-06-16")
        with pytest.raises(HoldoutAccessDenied):
            MarketPanel.build(market(), ["SPY"], DAYS[0], DAYS[-1])
        with pytest.raises(HoldoutAccessDenied):
            MarketPanel.build(market(), ["SPY"], "2022-06-20", "2022-06-30")    # embargo only
    MarketPanel.build(market(), ["SPY"], DAYS[0], DAYS[-1])                     # no such holdout in force


def test_agent_backtests_are_fenced():
    with pytest.raises(HoldoutAccessDenied, match="agent"):
        BacktestEngine().run_alpha(None, ["AAPL"], None, "2026-09-01", "2026-09-30")


# -- the single way through ------------------------------------------------------------------


def test_evaluation_opens_one_holdout_for_one_registered_trial(tmp_path):
    with use_holdouts(book_with(TEST_HOLDOUT)) as book:
        led, m = ledger(tmp_path)
        tid = holdout_trial(led, m, {"lookback": 126})
        with pytest.raises(GovernanceViolation):
            with book.evaluation("synthetic", actor="ai:claude", ledger=led, trial_id=tid):
                pass
        with book.evaluation("synthetic", actor="human:reviewer", ledger=led, trial_id=tid):
            panel = MarketPanel.build(market(), ["SPY"], DAYS[0], DAYS[-1])
            panel.as_of(DAYS[-1])
            with pytest.raises(HoldoutAccessDenied):                     # one at a time
                with book.evaluation("synthetic", actor="human:reviewer", ledger=led, trial_id=tid):
                    pass
        with pytest.raises(HoldoutAccessDenied):                         # closed again afterwards
            panel.as_of(DAYS[-1])
        panel.as_of("2022-06-01")
        led.finish_trial(tid, "FAIL", {"sharpe": -0.1})
        with pytest.raises(HoldoutAccessDenied, match="outcome"):
            with book.evaluation("synthetic", actor="human:reviewer", ledger=led, trial_id=tid):
                pass
        # same candidate again, or a retuned variant of the failed hypothesis: refused
        again = holdout_trial(led, m, {"lookback": 126})
        with pytest.raises(HoldoutAccessDenied, match="already evaluated"):
            with book.evaluation("synthetic", actor="human:reviewer", ledger=led, trial_id=again):
                pass
        led.finish_trial(again, "ABORTED")
        variant = holdout_trial(led, m, {"lookback": 100})
        with pytest.raises(HoldoutAccessDenied, match="already failed"):
            with book.evaluation("synthetic", actor="human:reviewer", ledger=led, trial_id=variant):
                pass


def test_ledger_refuses_trials_that_touch_or_misdeclare_holdouts(tmp_path):
    with use_holdouts(book_with(TEST_HOLDOUT)):
        led, m = ledger(tmp_path)
        kw = dict(hypothesis_id="H1", params={"x": 1}, manifest_hash=m.manifest_hash(), code_hash=m.code_hash,
                  config_hash="c" * 64, data_hash="d" * 64)
        led.start_trial(stage="development", window=("2021-01-04", "2022-06-16"), **kw)
        with pytest.raises(HoldoutAccessDenied):                         # development reaching the embargo
            led.start_trial(stage="development", window=("2021-01-04", "2022-06-20"), **kw)
        with pytest.raises(HoldoutAccessDenied, match="contaminated"):   # the Phase A holdout, again
            led.start_trial(stage="holdout", window=("2023-02-01", "2026-06-30"), **kw)
        with pytest.raises(HoldoutAccessDenied, match="not a sealed"):   # an undeclared ad-hoc holdout
            led.start_trial(stage="holdout", window=("2019-01-01", "2019-12-31"), **kw)
        assert led.n_trials("f") == 1
