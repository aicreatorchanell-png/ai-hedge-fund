"""Phase B research governance: a versioned manifest and a tamper-evident ledger.

Two pieces, both deterministic Python:

ResearchManifest
    What a research program runs on, pinned by hashes: the git commit (and
    whether the tree was dirty), a hash of the engine's source files, a hash
    per config file (validation gates, holdout declarations, risk/small
    account), a hash per data snapshot, and the pre-registration hash.
    Manifests are versioned: version n+1 names the hash of version n, so the
    research program's history is a chain too.

ExperimentLedger
    Append-only JSONL. Every entry carries `seq`, the previous entry's hash
    and its own hash, so editing, reordering or deleting an entry breaks the
    chain. A head file next to the ledger pins the last (seq, hash): cutting
    entries off the end is detected as well. (Tamper-*evident*, not
    tamper-proof: commit the ledger and head to git to anchor them.)

    Entry kinds, all validated on write and again on `verify()`:

      manifest      a ResearchManifest (hash recomputed; version chain checked)
      hypothesis    id, statement, family — registered before any trial
      trial_start   hypothesis, parameter set (+ hash), stage, window, and the
                    manifest/code/config/data hashes it ran on
      trial_result  outcome PASS | FAIL | INCONCLUSIVE | ERROR | ABORTED

    A trial is logged *before* it runs (`trial_start`), so a failed, crashed
    or abandoned trial still counts: `n_trials` counts every parameter set
    ever started in a family, whatever its outcome. `trial()` is a context
    manager that records ERROR automatically when the run raises.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
GENESIS = "0" * 64
LEDGER_SCHEMA = 1
MANIFEST_SCHEMA = 1


# -- hashing ------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def hash_json(obj) -> str:
    return sha256_bytes(canonical_json(obj).encode())


def hash_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_tree(root: Path | str, *, suffixes: tuple[str, ...] = (".py",), exclude_tests: bool = True) -> str:
    """Hash of every matching file under *root*: relative path + content, in sorted order."""
    root = Path(root)
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix not in suffixes or "__pycache__" in p.parts:
            continue
        if exclude_tests and (p.name.startswith("test_") or p.name == "conftest.py"):
            continue
        h.update(p.relative_to(root).as_posix().encode() + b"\0" + hash_file(p).encode() + b"\n")
    return h.hexdigest()


def hash_code(repo_root: Path | str = REPO_ROOT) -> str:
    """The engine's source (hedge_fund/**/*.py, tests excluded)."""
    return hash_tree(Path(repo_root) / "hedge_fund")


def hash_panel(panel) -> str:
    """Content hash of a MarketPanel's data (every field, the tradable mask and membership)."""
    import pandas as pd

    h = hashlib.sha256()
    for name in sorted(panel._frames):
        h.update(name.encode())
        h.update(pd.util.hash_pandas_object(panel._frames[name], index=True).values.tobytes())
    h.update(pd.util.hash_pandas_object(panel._tradable.astype(int), index=True).values.tobytes())
    if panel._members is not None:
        h.update(pd.util.hash_pandas_object(panel._members.astype(int), index=True).values.tobytes())
    h.update(canonical_json(panel._splits).encode())
    return h.hexdigest()


def git_state(repo_root: Path | str = REPO_ROOT, paths: tuple[str, ...] = ("hedge_fund", "configs")) -> tuple[str, bool]:
    """(HEAD commit, dirty?) — dirty if tracked or untracked changes exist under *paths*."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True, check=True).stdout

    commit = git("rev-parse", "HEAD").strip()
    dirty = bool(git("status", "--porcelain", "--", *paths).strip())
    return commit, dirty


# -- manifest -----------------------------------------------------------------


class ResearchManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = MANIFEST_SCHEMA
    program: str = Field(min_length=1)
    version: int = Field(ge=1)
    previous_manifest_hash: str | None = None
    created_at: str
    code_commit: str = Field(min_length=7)
    code_dirty: bool
    code_hash: str = Field(min_length=64, max_length=64)
    config_hashes: dict[str, str]
    data_hashes: dict[str, str]
    preregistration_hash: str | None = None
    notes: str = ""

    def manifest_hash(self) -> str:
        return hash_json(self.model_dump(mode="json"))


def build_manifest(program: str, version: int, *, configs: dict[str, Path | str], data: dict[str, str],
                   previous: ResearchManifest | None = None, preregistration_hash: str | None = None,
                   repo_root: Path | str = REPO_ROOT, notes: str = "") -> ResearchManifest:
    """Pin the current code, *configs* (name -> file) and *data* (name -> precomputed hash)."""
    if not configs or not data:
        raise ValueError("a manifest needs at least one config hash and one data hash")
    if any(not v for v in data.values()):
        raise ValueError("every data hash must be non-empty")
    commit, dirty = git_state(repo_root)
    return ResearchManifest(
        program=program, version=version,
        previous_manifest_hash=previous.manifest_hash() if previous else None,
        created_at=datetime.now(timezone.utc).isoformat(), code_commit=commit, code_dirty=dirty,
        code_hash=hash_code(repo_root),
        config_hashes={k: hash_file(Path(repo_root) / v if not Path(v).is_absolute() else v)
                       for k, v in sorted(configs.items())},
        data_hashes=dict(sorted(data.items())), preregistration_hash=preregistration_hash, notes=notes,
    )


# -- ledger -------------------------------------------------------------------


class Outcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"
    ABORTED = "ABORTED"


STAGES = frozenset({"development", "validation", "stress", "holdout", "paper"})


class LedgerTampered(RuntimeError):
    pass


class LedgerViolation(ValueError):
    """An entry was refused: it breaks a governance rule."""


@dataclass
class _Trial:
    start: dict
    result: dict | None = None


class ExperimentLedger:
    def __init__(self, path: Path | str, *, allow_dirty_code: bool = False) -> None:
        self.path = Path(path)
        self.head_path = self.path.with_name(self.path.name + ".head")
        self.allow_dirty_code = allow_dirty_code      # tests only: real research runs on committed code

    # -- reading --------------------------------------------------------------

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def verify(self) -> int:
        """Re-check the chain, the head anchor and every governance rule. Returns the entry count."""
        entries = self.entries()
        prev = GENESIS
        state = _State()
        for i, e in enumerate(entries):
            body = {k: v for k, v in e.items() if k != "entry_hash"}
            if e.get("seq") != i or e.get("prev_hash") != prev or hash_json(body) != e.get("entry_hash"):
                raise LedgerTampered(f"entry {i} does not match the chain")
            try:
                state.check(e["kind"], e["payload"], allow_dirty=e.get("allow_dirty_code", False))
            except LedgerViolation as exc:
                raise LedgerTampered(f"entry {i} breaks a ledger rule: {exc}") from exc
            state.apply(e["kind"], e["payload"], i)
            prev = e["entry_hash"]
        head = self._read_head()
        if entries and head != {"seq": len(entries) - 1, "entry_hash": prev}:
            raise LedgerTampered("ledger head does not match the last entry (truncated or rewritten)")
        if not entries and head is not None:
            raise LedgerTampered("ledger head exists but the ledger is empty")
        return len(entries)

    def _state(self) -> _State:
        state = _State()
        for i, e in enumerate(self.entries()):
            state.apply(e["kind"], e["payload"], i)
        return state

    def manifests(self, program: str | None = None) -> list[ResearchManifest]:
        return [ResearchManifest(**{k: v for k, v in m.items() if k != "manifest_hash"})
                for m in self._state().manifests.values()
                if program is None or m["program"] == program]

    def latest_manifest(self, program: str) -> ResearchManifest | None:
        ms = sorted(self.manifests(program), key=lambda m: m.version)
        return ms[-1] if ms else None

    def hypotheses(self) -> dict[str, dict]:
        return dict(self._state().hypotheses)

    def trials(self, *, hypothesis_id: str | None = None, family: str | None = None,
               stage: str | None = None) -> list[dict]:
        st = self._state()
        out = []
        for tid, t in st.trials.items():
            s = t.start
            fam = st.hypotheses[s["hypothesis_id"]]["family"]
            if (hypothesis_id and s["hypothesis_id"] != hypothesis_id) or (family and fam != family) \
                    or (stage and s["stage"] != stage):
                continue
            out.append({**s, "family": fam, "outcome": t.result["outcome"] if t.result else None,
                        "metrics": t.result["metrics"] if t.result else None})
        return out

    def open_trials(self) -> list[str]:
        return [tid for tid, t in self._state().trials.items() if t.result is None]

    def n_trials(self, family: str) -> int:
        """Distinct parameter sets ever started in *family* — failures, errors and aborts included."""
        return len({(t["hypothesis_id"], t["params_hash"]) for t in self.trials(family=family)})

    # -- writing --------------------------------------------------------------

    def _read_head(self) -> dict | None:
        return json.loads(self.head_path.read_text()) if self.head_path.exists() else None

    def _append(self, kind: str, payload: dict) -> dict:
        self.verify()                                          # never extend a broken chain
        state = self._state()
        state.check(kind, payload, allow_dirty=self.allow_dirty_code)
        entries = self.entries()
        body = {"schema": LEDGER_SCHEMA, "seq": len(entries), "kind": kind, "payload": payload,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "prev_hash": entries[-1]["entry_hash"] if entries else GENESIS}
        if self.allow_dirty_code:
            body["allow_dirty_code"] = True
        body["entry_hash"] = hash_json(body)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as fh:
            fh.write(canonical_json(body) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp = self.head_path.with_suffix(".tmp")
        tmp.write_text(canonical_json({"seq": body["seq"], "entry_hash": body["entry_hash"]}))
        os.replace(tmp, self.head_path)
        return body

    def register_manifest(self, manifest: ResearchManifest) -> str:
        self._append("manifest", {**manifest.model_dump(mode="json"), "manifest_hash": manifest.manifest_hash()})
        return manifest.manifest_hash()

    def register_hypothesis(self, hypothesis_id: str, *, statement: str, family: str, manifest_hash: str,
                            preregistration_hash: str | None = None) -> None:
        self._append("hypothesis", {"hypothesis_id": hypothesis_id, "statement": statement, "family": family,
                                    "manifest_hash": manifest_hash, "preregistration_hash": preregistration_hash})

    def start_trial(self, *, hypothesis_id: str, params: dict, stage: str, window: tuple[str, str],
                    manifest_hash: str, code_hash: str, config_hash: str, data_hash: str) -> str:
        from hedge_fund.validation.holdout_guard import active_book

        active_book().check_trial_window(stage, *window)
        trial_id = f"T{len(self.entries()):06d}"
        self._append("trial_start", {
            "trial_id": trial_id, "hypothesis_id": hypothesis_id, "params": params,
            "params_hash": hash_json(params), "stage": stage, "window": list(window),
            "manifest_hash": manifest_hash, "code_hash": code_hash, "config_hash": config_hash,
            "data_hash": data_hash})
        return trial_id

    def finish_trial(self, trial_id: str, outcome: Outcome | str, metrics: dict | None = None,
                     notes: str = "") -> None:
        self._append("trial_result", {"trial_id": trial_id, "outcome": Outcome(outcome).value,
                                      "metrics": metrics or {}, "notes": notes})

    @contextmanager
    def trial(self, **start_kwargs) -> Iterator[_TrialHandle]:
        """Start a trial; the body sets `handle.finish(outcome, metrics)`.

        An exception records ERROR (and re-raises); leaving without finishing
        records ABORTED — a trial is never silently dropped.
        """
        handle = _TrialHandle(self, self.start_trial(**start_kwargs))
        try:
            yield handle
        except BaseException as exc:
            if not handle.done:
                self.finish_trial(handle.trial_id, Outcome.ERROR, notes=f"{type(exc).__name__}: {exc}"[:500])
            raise
        if not handle.done:
            self.finish_trial(handle.trial_id, Outcome.ABORTED, notes="trial body ended without an outcome")


class _TrialHandle:
    def __init__(self, ledger: ExperimentLedger, trial_id: str) -> None:
        self.ledger, self.trial_id, self.done = ledger, trial_id, False

    def finish(self, outcome: Outcome | str, metrics: dict | None = None, notes: str = "") -> None:
        self.ledger.finish_trial(self.trial_id, outcome, metrics, notes)
        self.done = True


class _State:
    """Replayable ledger state; `check` enforces the rules for the next entry."""

    def __init__(self) -> None:
        self.manifests: dict[str, dict] = {}
        self.hypotheses: dict[str, dict] = {}
        self.trials: dict[str, _Trial] = {}

    def check(self, kind: str, p: dict, *, allow_dirty: bool) -> None:
        if kind == "manifest":
            m = ResearchManifest(**{k: v for k, v in p.items() if k != "manifest_hash"})
            if m.manifest_hash() != p.get("manifest_hash"):
                raise LedgerViolation("manifest hash does not match its content")
            if m.code_dirty and not allow_dirty:
                raise LedgerViolation("manifest built from uncommitted code; commit first")
            if not m.config_hashes or not m.data_hashes or not all(m.data_hashes.values()):
                raise LedgerViolation("manifest needs config and data hashes")
            same = sorted((x for x in self.manifests.values() if x["program"] == m.program),
                          key=lambda x: x["version"])
            expected_prev = same[-1]["manifest_hash"] if same else None
            if m.version != len(same) + 1 or m.previous_manifest_hash != expected_prev:
                raise LedgerViolation(f"manifest {m.program} v{m.version} does not extend the version chain")
        elif kind == "hypothesis":
            if not p.get("hypothesis_id") or not p.get("statement") or not p.get("family"):
                raise LedgerViolation("hypothesis needs id, statement and family")
            if p["hypothesis_id"] in self.hypotheses:
                raise LedgerViolation(f"hypothesis {p['hypothesis_id']} already registered")
            if p.get("manifest_hash") not in self.manifests:
                raise LedgerViolation("hypothesis references an unregistered manifest")
        elif kind == "trial_start":
            m = self.manifests.get(p.get("manifest_hash"))
            if m is None:
                raise LedgerViolation("trial references an unregistered manifest")
            if p.get("hypothesis_id") not in self.hypotheses:
                raise LedgerViolation("trial references an unregistered hypothesis")
            if p.get("stage") not in STAGES:
                raise LedgerViolation(f"unknown stage {p.get('stage')!r}; one of {sorted(STAGES)}")
            for k in ("code_hash", "config_hash", "data_hash", "params_hash"):
                if not p.get(k):
                    raise LedgerViolation(f"trial needs a non-empty {k}")
            if p["code_hash"] != m["code_hash"]:
                raise LedgerViolation("code changed since the manifest was registered; register a new manifest")
            if p["params_hash"] != hash_json(p.get("params")):
                raise LedgerViolation("params hash does not match the params")
            w = p.get("window") or []
            if len(w) != 2 or not w[0] <= w[1]:
                raise LedgerViolation("trial needs a [start, end] window")
            if p.get("trial_id") in self.trials:
                raise LedgerViolation("duplicate trial id")
        elif kind == "trial_result":
            t = self.trials.get(p.get("trial_id"))
            if t is None:
                raise LedgerViolation("result for a trial that was never started")
            if t.result is not None:
                raise LedgerViolation("trial already has a result")
            Outcome(p.get("outcome"))
        else:
            raise LedgerViolation(f"unknown entry kind {kind!r}")

    def apply(self, kind: str, p: dict, seq: int) -> None:
        if kind == "manifest":
            self.manifests[p["manifest_hash"]] = p
        elif kind == "hypothesis":
            self.hypotheses[p["hypothesis_id"]] = p
        elif kind == "trial_start":
            self.trials[p["trial_id"]] = _Trial(start=p)
        elif kind == "trial_result":
            self.trials[p["trial_id"]].result = p
