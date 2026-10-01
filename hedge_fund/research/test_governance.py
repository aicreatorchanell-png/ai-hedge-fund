"""Research manifest and tamper-evident experiment ledger."""

from __future__ import annotations

import json
import subprocess

import pandas as pd
import pytest

from hedge_fund.research.governance import (
    ExperimentLedger, LedgerTampered, LedgerViolation, Outcome, ResearchManifest, build_manifest, git_state,
    hash_code, hash_json, hash_panel, hash_tree,
)
from hedge_fund.systematic import MarketPanel
from hedge_fund.systematic.testing import SyntheticMarket, weekdays

CODE = "a" * 64


def manifest(version=1, previous=None, code_hash=CODE, dirty=False, program="phase-b") -> ResearchManifest:
    return ResearchManifest(program=program, version=version,
                            previous_manifest_hash=previous.manifest_hash() if previous else None,
                            created_at="2026-10-01T00:00:00+00:00", code_commit="0123456789abcdef",
                            code_dirty=dirty, code_hash=code_hash, config_hashes={"gates": "g" * 64},
                            data_hashes={"prices": "d" * 64}, preregistration_hash="p" * 64)


def start_kw(m: ResearchManifest, **kw) -> dict:
    base = dict(hypothesis_id="H1", params={"lookback": 126}, stage="development",
                window=("2010-01-01", "2018-12-31"), manifest_hash=m.manifest_hash(), code_hash=m.code_hash,
                config_hash="c" * 64, data_hash="d" * 64)
    return {**base, **kw}


@pytest.fixture
def ledger(tmp_path):
    led = ExperimentLedger(tmp_path / "ledger.jsonl")
    m = manifest()
    led.register_manifest(m)
    led.register_hypothesis("H1", statement="trend persists", family="trend", manifest_hash=m.manifest_hash())
    return led, m


# -- hashing ----------------------------------------------------------------------


def test_hashes_are_deterministic_and_content_sensitive(tmp_path):
    assert hash_json({"b": 1, "a": 2}) == hash_json({"a": 2, "b": 1})
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "test_m.py").write_text("assert True\n")
    h1 = hash_tree(tmp_path / "pkg")
    (tmp_path / "pkg" / "test_m.py").write_text("assert 1\n")         # tests excluded
    assert hash_tree(tmp_path / "pkg") == h1
    (tmp_path / "pkg" / "m.py").write_text("x = 2\n")
    assert hash_tree(tmp_path / "pkg") != h1
    assert len(hash_code()) == 64


def test_panel_hash_changes_with_the_data():
    days = weekdays("2022-01-03", "2022-03-31")
    m = SyntheticMarket()
    m.add_series("SPY", days, [100.0 + i for i in range(len(days))], volume=1_000_000)
    p1 = MarketPanel.build(m, ["SPY"], days[0], days[-1])
    p2 = MarketPanel.build(m, ["SPY"], days[0], days[-1])
    assert hash_panel(p1) == hash_panel(p2)
    p2._frames["close"].iloc[5, 0] += 0.01
    assert hash_panel(p1) != hash_panel(p2)
    assert isinstance(p1._frames["close"], pd.DataFrame)


def test_build_manifest_pins_commit_code_configs_and_data(tmp_path):
    def git(*a):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=tmp_path, check=True,
                       capture_output=True)
    git("init", "-q")
    (tmp_path / "hedge_fund").mkdir()
    (tmp_path / "hedge_fund" / "engine.py").write_text("RULE = 1\n")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "gates.yaml").write_text("min_trades: 30\n")
    git("add", ".")
    git("commit", "-qm", "init")
    m1 = build_manifest("phase-b", 1, configs={"gates": "configs/gates.yaml"}, data={"prices": "d" * 64},
                        repo_root=tmp_path)
    assert m1.code_dirty is False and len(m1.code_commit) == 40 and m1.previous_manifest_hash is None
    (tmp_path / "configs" / "gates.yaml").write_text("min_trades: 10\n")   # an edit is visible
    m2 = build_manifest("phase-b", 2, configs={"gates": "configs/gates.yaml"}, data={"prices": "d" * 64},
                        previous=m1, repo_root=tmp_path)
    assert m2.code_dirty is True and m2.config_hashes["gates"] != m1.config_hashes["gates"]
    assert m2.previous_manifest_hash == m1.manifest_hash()
    assert git_state(tmp_path)[1] is True
    with pytest.raises(ValueError):
        build_manifest("phase-b", 1, configs={"gates": "configs/gates.yaml"}, data={"prices": ""},
                       repo_root=tmp_path)


# -- ledger rules ---------------------------------------------------------------------


def test_every_trial_is_logged_including_failures(ledger):
    led, m = ledger
    for lb, outcome in [(63, Outcome.FAIL), (126, Outcome.PASS), (252, Outcome.INCONCLUSIVE)]:
        tid = led.start_trial(**start_kw(m, params={"lookback": lb}))
        led.finish_trial(tid, outcome, {"sharpe": 0.1})
    assert led.n_trials("trend") == 3
    assert [t["outcome"] for t in led.trials(hypothesis_id="H1")] == ["FAIL", "PASS", "INCONCLUSIVE"]
    assert led.verify() == 2 + 6


def test_started_but_unfinished_trial_still_counts(ledger):
    led, m = ledger
    tid = led.start_trial(**start_kw(m))
    assert led.open_trials() == [tid] and led.n_trials("trend") == 1


def test_trial_context_records_error_and_abort(ledger):
    led, m = ledger
    with pytest.raises(ZeroDivisionError):
        with led.trial(**start_kw(m, params={"lookback": 1})):
            1 / 0
    with led.trial(**start_kw(m, params={"lookback": 2})):
        pass
    with led.trial(**start_kw(m, params={"lookback": 3})) as t:
        t.finish("FAIL", {"sharpe": -0.2})
    assert [t["outcome"] for t in led.trials()] == ["ERROR", "ABORTED", "FAIL"]
    assert led.n_trials("trend") == 3 and led.open_trials() == []


@pytest.mark.parametrize("bad, match", [
    ({"hypothesis_id": "H9"}, "unregistered hypothesis"),
    ({"manifest_hash": "x" * 64}, "unregistered manifest"),
    ({"stage": "tuning"}, "unknown stage"),
    ({"data_hash": ""}, "data_hash"),
    ({"config_hash": ""}, "config_hash"),
    ({"code_hash": "b" * 64}, "code changed"),
    ({"window": ("2019-01-01", "2010-01-01")}, "window"),
])
def test_trial_start_rules(ledger, bad, match):
    led, m = ledger
    with pytest.raises(LedgerViolation, match=match):
        led.start_trial(**start_kw(m, **bad))


def test_result_rules(ledger):
    led, m = ledger
    with pytest.raises(LedgerViolation, match="never started"):
        led.finish_trial("T999999", "PASS")
    tid = led.start_trial(**start_kw(m))
    led.finish_trial(tid, "FAIL")
    with pytest.raises(LedgerViolation, match="already has a result"):
        led.finish_trial(tid, "PASS")                         # no rewriting a failure into a pass
    with pytest.raises(ValueError):
        led.finish_trial(led.start_trial(**start_kw(m, params={"x": 1})), "GREAT")


def test_manifest_version_chain_and_dirty_code(tmp_path):
    led = ExperimentLedger(tmp_path / "l.jsonl")
    m1 = manifest()
    led.register_manifest(m1)
    with pytest.raises(LedgerViolation, match="version chain"):
        led.register_manifest(manifest(version=3, previous=m1))
    with pytest.raises(LedgerViolation, match="version chain"):
        led.register_manifest(manifest(version=2, previous=None))
    with pytest.raises(LedgerViolation, match="uncommitted"):
        led.register_manifest(manifest(version=2, previous=m1, dirty=True))
    m2 = manifest(version=2, previous=m1, code_hash="b" * 64)
    led.register_manifest(m2)
    assert led.latest_manifest("phase-b") == m2
    with pytest.raises(LedgerViolation, match="already registered"):
        led.register_hypothesis("H", statement="s", family="f", manifest_hash=m2.manifest_hash())
        led.register_hypothesis("H", statement="s", family="f", manifest_hash=m2.manifest_hash())


# -- tamper evidence ---------------------------------------------------------------------


def _filled(ledger):
    led, m = ledger
    for lb in (63, 126):
        tid = led.start_trial(**start_kw(m, params={"lookback": lb}))
        led.finish_trial(tid, "FAIL", {"sharpe": -0.1})
    return led


def test_editing_an_entry_is_detected(ledger):
    led = _filled(ledger)
    lines = led.path.read_text().splitlines()
    e = json.loads(lines[-1])
    e["payload"]["outcome"] = "PASS"                         # rewrite a failure as a pass
    lines[-1] = json.dumps(e, sort_keys=True, separators=(",", ":"))
    led.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(LedgerTampered):
        led.verify()
    with pytest.raises(LedgerTampered):                       # and nothing more can be appended
        led.register_hypothesis("H2", statement="s", family="f", manifest_hash=ledger[1].manifest_hash())


def test_deleting_or_truncating_entries_is_detected(ledger):
    led = _filled(ledger)
    lines = led.path.read_text().splitlines()
    led.path.write_text("\n".join(lines[:3] + lines[4:]) + "\n")          # delete from the middle
    with pytest.raises(LedgerTampered):
        led.verify()
    led.path.write_text("\n".join(lines[:-2]) + "\n")                     # drop the last failed trial
    with pytest.raises(LedgerTampered, match="head"):
        led.verify()


def test_rehashed_forgery_that_breaks_a_rule_is_detected(ledger):
    """Recomputing the hashes is not enough: replay re-applies the governance rules."""
    led, m = ledger
    tid = led.start_trial(**start_kw(m))
    led.finish_trial(tid, "FAIL")
    entries = led.entries()
    forged = dict(entries[-1])
    forged["seq"], forged["prev_hash"] = len(entries), entries[-1]["entry_hash"]
    forged["payload"] = {**forged["payload"], "outcome": "PASS"}       # second result for the same trial
    body = {k: v for k, v in forged.items() if k != "entry_hash"}
    forged["entry_hash"] = hash_json(body)
    with open(led.path, "a") as fh:
        fh.write(json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n")
    led.head_path.write_text(json.dumps({"seq": forged["seq"], "entry_hash": forged["entry_hash"]}))
    with pytest.raises(LedgerTampered, match="rule"):
        led.verify()
