"""Holdout access protection: declared holdouts, a data fence, one human-opened evaluation.

Declarations live in `configs/holdouts.yaml` (human-owned, hashed into every
research manifest). Each holdout is

    sealed        its window [start - embargo, end] is fenced: no market data
                  in it reaches development, selection, optimization,
                  dashboards or agents
    contaminated  results on it were seen (the Phase A holdout): it can never
                  be declared or evaluated as a holdout again
    consumed      a sealed holdout whose evaluations are finished

The fence is enforced where strategy market data enters the engine:
`MarketPanel.build` (a panel whose sessions reach a fenced window) and
`MarketPanel.as_of` (a view of a fenced session) raise `HoldoutAccessDenied`.
Other surfaces (LLM agent backtests, dashboards, reports) call
`check_access(start, end, purpose=...)` before reading data.

The only way through is `evaluation(...)`: a human actor opens one sealed
holdout for one ledger trial (stage "holdout", window = the holdout) that
has not been evaluated before and whose hypothesis has not already failed
this holdout. While the context is open — and only in that thread of
execution — reads of that holdout are allowed. Nothing else (no env var,
no flag) lifts the fence.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, timedelta
from enum import Enum
from pathlib import Path
from typing import Iterator

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "configs" / "holdouts.yaml"


class HoldoutAccessDenied(PermissionError):
    pass


class HoldoutStatus(str, Enum):
    SEALED = "sealed"
    CONTAMINATED = "contaminated"
    CONSUMED = "consumed"


class Purpose(str, Enum):
    DEVELOPMENT = "development"
    SELECTION = "selection"
    OPTIMIZATION = "optimization"
    DASHBOARD = "dashboard"
    AGENT = "agent"
    PAPER_MONITORING = "paper_monitoring"
    HOLDOUT_EVALUATION = "holdout_evaluation"


class HoldoutDeclaration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    start: str
    end: str
    embargo_days: int = Field(0, ge=0)
    status: HoldoutStatus
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _dates(self):
        if date.fromisoformat(self.start) > date.fromisoformat(self.end):
            raise ValueError(f"holdout {self.id}: start after end")
        return self

    @property
    def fence_start(self) -> str:
        return (date.fromisoformat(self.start) - timedelta(days=self.embargo_days)).isoformat()

    def overlaps(self, start: str, end: str, *, with_embargo: bool = True) -> bool:
        lo = self.fence_start if with_embargo else self.start
        return start <= self.end and end >= lo


_open: ContextVar[str | None] = ContextVar("open_holdout", default=None)


class HoldoutBook:
    def __init__(self, declarations: list[HoldoutDeclaration], *, source_hash: str = "") -> None:
        ids = [d.id for d in declarations]
        if len(ids) != len(set(ids)):
            raise ValueError("holdout ids must be unique")
        self.declarations = list(declarations)
        self.source_hash = source_hash

    def get(self, holdout_id: str) -> HoldoutDeclaration:
        for d in self.declarations:
            if d.id == holdout_id:
                return d
        raise KeyError(holdout_id)

    def with_status(self, status: HoldoutStatus) -> list[HoldoutDeclaration]:
        return [d for d in self.declarations if d.status is status]

    # -- the fence ------------------------------------------------------------

    def check_access(self, start: str, end: str, *, purpose: Purpose | str) -> None:
        """Refuse reading market data dated in [start, end] if it touches a sealed holdout."""
        purpose = Purpose(purpose)
        for d in self.with_status(HoldoutStatus.SEALED):
            if not d.overlaps(start, end):
                continue
            if purpose is Purpose.HOLDOUT_EVALUATION and _open.get() == d.id:
                continue
            raise HoldoutAccessDenied(
                f"{purpose.value} access to {start}..{end} reaches sealed holdout {d.id} "
                f"({d.start}..{d.end}, fenced from {d.fence_start}); only a human-opened holdout "
                f"evaluation may read it")

    def check_new_holdout(self, start: str, end: str) -> None:
        """A holdout may not reuse a contaminated or consumed window."""
        for d in self.declarations:
            if d.status is not HoldoutStatus.SEALED and d.overlaps(start, end, with_embargo=False):
                raise HoldoutAccessDenied(
                    f"{start}..{end} overlaps {d.status.value} holdout {d.id} ({d.start}..{d.end}); "
                    f"it cannot serve as a holdout again")

    def check_trial_window(self, stage: str, start: str, end: str) -> None:
        """Ledger rule: a holdout trial runs on exactly one sealed holdout; no other stage touches one."""
        if stage == "holdout":
            if not any(d.start == start and d.end == end for d in self.with_status(HoldoutStatus.SEALED)):
                self.check_new_holdout(start, end)
                raise HoldoutAccessDenied(f"holdout trial window {start}..{end} is not a sealed declared holdout")
            return
        self.check_access(start, end, purpose=Purpose.DEVELOPMENT)

    # -- the single way through ---------------------------------------------

    @contextmanager
    def evaluation(self, holdout_id: str, *, actor: str, ledger, trial_id: str) -> Iterator[HoldoutDeclaration]:
        """Open one sealed holdout for one pre-registered ledger trial. Human actors only."""
        from hedge_fund.research.pipeline import authorize

        authorize(actor, "open_locked_holdout")
        d = self.get(holdout_id)
        if d.status is not HoldoutStatus.SEALED:
            raise HoldoutAccessDenied(f"holdout {d.id} is {d.status.value}, not sealed")
        if _open.get() is not None:
            raise HoldoutAccessDenied("another holdout evaluation is already open")
        holdout_trials = [t for t in ledger.trials(stage="holdout") if t["window"] == [d.start, d.end]]
        trial = next((t for t in holdout_trials if t["trial_id"] == trial_id), None)
        if trial is None:
            raise HoldoutAccessDenied(f"{trial_id} is not a holdout trial on {d.id} in the ledger")
        if trial["outcome"] is not None:
            raise HoldoutAccessDenied(f"{trial_id} already has an outcome")
        for t in holdout_trials:
            if t["trial_id"] == trial_id:
                continue
            if t["hypothesis_id"] == trial["hypothesis_id"] and t["params_hash"] == trial["params_hash"]:
                raise HoldoutAccessDenied("this candidate was already evaluated on this holdout")
            if t["hypothesis_id"] == trial["hypothesis_id"] and t["outcome"] in ("FAIL", "ERROR", "ABORTED"):
                raise HoldoutAccessDenied("this hypothesis already failed this holdout; it needs a new holdout")
        token = _open.set(d.id)
        try:
            yield d
        finally:
            _open.reset(token)


def load_holdouts(path: Path | str = DEFAULT_PATH) -> HoldoutBook:
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw) or {}
    return HoldoutBook([HoldoutDeclaration(**h) for h in data.get("holdouts", [])],
                       source_hash=hashlib.sha256(raw).hexdigest())


_book: ContextVar[HoldoutBook | None] = ContextVar("holdout_book", default=None)
_default: list[HoldoutBook] = []


def active_book() -> HoldoutBook:
    """The holdout book in force: an explicit `use_holdouts` override, else configs/holdouts.yaml."""
    book = _book.get()
    return book if book is not None else _default_book()


def _default_book() -> HoldoutBook:
    if not _default:
        _default.append(load_holdouts() if DEFAULT_PATH.exists() else HoldoutBook([]))
    return _default[0]


@contextmanager
def use_holdouts(book: HoldoutBook) -> Iterator[HoldoutBook]:
    """Swap the book in force (tests, or a research program with its own reviewed file).

    Swapping can add declarations but never drop or alter one from the
    reviewed file: it is not a way around the fence — `evaluation` is.
    """
    reviewed = _default_book()
    for d in reviewed.declarations:
        if d not in book.declarations:
            raise HoldoutAccessDenied(f"override drops or alters reviewed holdout {d.id}")
    token = _book.set(book)
    try:
        yield book
    finally:
        _book.reset(token)


def check_access(start: str, end: str, *, purpose: Purpose | str) -> None:
    active_book().check_access(start, end, purpose=purpose)


def market_data_fence(start: str, end: str) -> None:
    """Called by MarketPanel: data reads are evaluations only inside an open evaluation."""
    purpose = Purpose.HOLDOUT_EVALUATION if _open.get() else Purpose.DEVELOPMENT
    active_book().check_access(start, end, purpose=purpose)
