"""Forward paper evaluation of a sealed *forward* holdout — one session at a time.

Research mode has zero access to a sealed holdout (holdout_guard). This module
is the only reader of a forward holdout, and it reads it the way time does:

    freeze   a human freezes a candidate: strategy specs + backtest config,
             their hash, the engine code hash, and `start_after` — the last
             session already completed (or already observed by its lineage)
             when it was frozen. Nothing on or before `start_after` is ever
             "unseen" data for this candidate.
    step     evaluates exactly one more session: the next panel session after
             the last recorded one, and only if real calendar time has
             completed it. No argument names a date, so there is no rewind and
             no arbitrary historical read. The run is replayed from frozen
             specs; every previously recorded session must reproduce exactly,
             so a strategy, config or code change in place is refused.
    review   a human reads the records (logged as a review). AI actors and
             research purposes cannot read them.

Records are an append-only hash chain with a head anchor (as the experiment
ledger). A candidate modified after a human saw results is a *new generation*
with an explicit parent; its `start_after` is at least the last session its
lineage observed, so already-observed sessions are never reused as unseen.

While a step runs, a context-local token lets the market-data fence admit
reads of that one holdout up to that one session, for the forward-evaluation
purpose only; research code in any other context still gets
HoldoutAccessDenied. The clock is the system clock; an injected clock is
accepted only for holdouts that are not in the reviewed configs/holdouts.yaml
(tests), and is written into the record.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from hedge_fund.research.governance import GENESIS, canonical_json, hash_code, hash_json
from hedge_fund.validation.holdout_guard import (
    EvaluationMode, HoldoutAccessDenied, HoldoutStatus, _forward, active_book, load_holdouts,
)

NEW_YORK_LAG_DAYS = 1           # a session is complete the day after (sessions.completed_through)
_REVIEW_PURPOSES = frozenset({"human_review"})


class ForwardEvaluationError(RuntimeError):
    pass


class ForwardLedgerTampered(ForwardEvaluationError):
    pass


def _system_today() -> str:
    from hedge_fund.data.sessions import NEW_YORK
    return datetime.now(NEW_YORK).date().isoformat()


class _Chain:
    """Append-only JSONL with a hash chain and a head anchor."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.head_path = self.path.with_name(self.path.name + ".head")

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]

    def verify(self) -> list[dict]:
        entries, prev = self.entries(), GENESIS
        for i, e in enumerate(entries):
            body = {k: v for k, v in e.items() if k != "entry_hash"}
            if e.get("seq") != i or e.get("prev_hash") != prev or hash_json(body) != e.get("entry_hash"):
                raise ForwardLedgerTampered(f"forward record {i} does not match the chain")
            prev = e["entry_hash"]
        head = json.loads(self.head_path.read_text()) if self.head_path.exists() else None
        if entries and head != {"seq": len(entries) - 1, "entry_hash": prev}:
            raise ForwardLedgerTampered("forward ledger head does not match (truncated or rewritten)")
        if not entries and head is not None:
            raise ForwardLedgerTampered("forward ledger head exists but the ledger is empty")
        return entries

    def append(self, kind: str, payload: dict) -> dict:
        entries = self.verify()
        body = {"seq": len(entries), "kind": kind, "payload": payload,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "prev_hash": entries[-1]["entry_hash"] if entries else GENESIS}
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


class ForwardEvaluator:
    """Forward paper evaluation of candidates on one sealed forward holdout."""

    def __init__(self, ledger_path: Path | str, holdout_id: str, *, data_client, universe,
                 history_start: str, clock: Callable[[], str] | None = None,
                 repo_root: Path | str | None = None) -> None:
        book = active_book()
        self.holdout = book.get(holdout_id)
        if self.holdout.status is not HoldoutStatus.SEALED or self.holdout.evaluation_mode is not EvaluationMode.FORWARD:
            raise HoldoutAccessDenied(f"{holdout_id} is not a sealed forward holdout")
        reviewed_ids = {d.id for d in load_holdouts().declarations}
        if clock is not None and holdout_id in reviewed_ids:
            raise HoldoutAccessDenied("an injected clock cannot evaluate a reviewed holdout; real time only")
        self.clock_kind = "system" if clock is None else "injected"
        self._today = clock or _system_today
        self.chain = _Chain(ledger_path)
        self.data_client, self.universe, self.history_start = data_client, universe, history_start
        self._code_hash = lambda: hash_code(repo_root) if repo_root else hash_code()

    # -- clock --------------------------------------------------------------

    def completed_through(self) -> str:
        return (date.fromisoformat(self._today()) - timedelta(days=NEW_YORK_LAG_DAYS)).isoformat()

    # -- state (internal; never exposes market data) ---------------------------

    def _state(self) -> dict:
        cands: dict[str, dict] = {}
        for e in self.chain.verify():
            p = e["payload"]
            if e["kind"] == "freeze":
                cands[p["candidate_id"]] = {"freeze": p, "observations": []}
            elif e["kind"] == "observation":
                cands[p["candidate_id"]]["observations"].append(p)
        return cands

    def _lineage_last_observed(self, cands: dict, family: str) -> str | None:
        seen = [o["session"] for c in cands.values() if c["freeze"]["family"] == family for o in c["observations"]]
        return max(seen) if seen else None

    # -- freeze ---------------------------------------------------------------

    def freeze(self, candidate_id: str, *, family: str, strategy_specs: list[dict], backtest_config,
               actor: str, parent_id: str | None = None, universe_id: str = "") -> dict:
        from hedge_fund.research.pipeline import authorize

        authorize(actor, "open_locked_holdout")
        cands = self._state()
        if candidate_id in cands:
            raise ForwardEvaluationError(f"{candidate_id} is already frozen; a change is a new generation")
        family_members = [c for c in cands.values() if c["freeze"]["family"] == family]
        if family_members and parent_id is None:
            raise ForwardEvaluationError(f"family {family} already has forward candidates; declare the parent "
                                         f"(lineage must be explicit)")
        generation = 1
        if parent_id is not None:
            if parent_id not in cands or cands[parent_id]["freeze"]["family"] != family:
                raise ForwardEvaluationError(f"parent {parent_id} is not a forward candidate of family {family}")
            generation = cands[parent_id]["freeze"]["generation"] + 1
        cfg = backtest_config.model_dump(mode="json", exclude={"start", "end"})
        spec = {"strategies": strategy_specs, "backtest": cfg, "universe_id": universe_id}
        day_before_holdout = (date.fromisoformat(self.holdout.start) - timedelta(days=1)).isoformat()
        start_after = max(x for x in (day_before_holdout, self.completed_through(),
                                      self._lineage_last_observed(cands, family)) if x)
        payload = {"candidate_id": candidate_id, "family": family, "generation": generation,
                   "parent_id": parent_id, "holdout_id": self.holdout.id, "spec": spec,
                   "spec_hash": hash_json(spec), "code_hash": self._code_hash(), "start_after": start_after,
                   "frozen_on": self._today(), "clock": self.clock_kind, "actor": actor}
        self.chain.append("freeze", payload)
        return payload

    # -- step -----------------------------------------------------------------

    def step(self, candidate_id: str) -> dict | None:
        """Evaluate the next completed session for *candidate_id*; None if none is due yet."""
        from hedge_fund.systematic.backtest import BacktestConfig, SystematicBacktester
        from hedge_fund.systematic.panel import MarketPanel
        from hedge_fund.systematic.strategies import strategy_from_spec

        cands = self._state()
        if candidate_id not in cands:
            raise ForwardEvaluationError(f"{candidate_id} is not frozen")
        frz, obs = cands[candidate_id]["freeze"], cands[candidate_id]["observations"]
        if hash_json(frz["spec"]) != frz["spec_hash"]:
            raise ForwardLedgerTampered("frozen spec does not match its hash")
        if self._code_hash() != frz["code_hash"]:
            raise ForwardEvaluationError("engine code changed since the freeze; freeze a new generation")
        last = obs[-1]["session"] if obs else frz["start_after"]
        limit = min(self.completed_through(), self.holdout.end)
        if limit <= last:
            return None
        cfg = frz["spec"]["backtest"]
        benchmark = cfg.get("benchmark", "SPY")
        token = _forward.set((self.holdout.id, limit))      # calendar first: no data beyond what time completed
        try:
            calendar = MarketPanel.build(self.data_client, [benchmark], self.history_start, limit)
            nxt = next((s for s in calendar.sessions_through(limit) if s > last), None)
        finally:
            _forward.reset(token)
        if nxt is None:
            return None
        anchor = [s for s in calendar.sessions_through(frz["start_after"])]
        if not anchor:
            raise ForwardEvaluationError("no session on or before start_after inside the history window")
        token = _forward.set((self.holdout.id, nxt))         # this step may read through `nxt` only
        try:
            tickers = self.universe if isinstance(self.universe, list) else None
            schedule = None if isinstance(self.universe, list) else self.universe
            panel = MarketPanel.build(self.data_client, tickers, self.history_start, nxt, benchmark=benchmark,
                                      schedule=schedule)
            strategies = [strategy_from_spec(s) for s in frz["spec"]["strategies"]]
            if [s.spec() for s in strategies] != frz["spec"]["strategies"]:
                raise ForwardEvaluationError("strategy specs do not round-trip; refusing to evaluate")
            config = BacktestConfig(**{**cfg, "start": anchor[-1], "end": nxt})
            result = SystematicBacktester(panel, strategies, config).run()
        finally:
            _forward.reset(token)
        equity = {s: float(v) for s, v in result.equity.items()}
        for o in obs:                                        # replay must reproduce every recorded session
            if equity.get(o["session"]) != o["equity"]:
                raise ForwardEvaluationError(f"replay differs at {o['session']}: config, code or data changed "
                                             f"in place; freeze a new generation")
        record = {
            "candidate_id": candidate_id, "session": nxt, "equity": equity[nxt],
            "cash": float(result.cash[nxt]), "positions": dict(sorted(result.final_positions.items())),
            "trades": [t for t in result.trades if t["fill_session"] == nxt],
            "decisions": [d for d in result.decisions if d["session"] == nxt],
            "spec_hash": frz["spec_hash"], "clock": self.clock_kind, "evaluated_on": self._today(),
        }
        record = json.loads(canonical_json(record))
        self.chain.append("observation", record)
        return {"candidate_id": candidate_id, "session": nxt}        # results are read through review()

    # -- review -----------------------------------------------------------------

    def review(self, *, actor: str, purpose: str) -> list[dict]:
        """A human reads the forward records; the read itself is recorded."""
        from hedge_fund.research.pipeline import GovernanceViolation, _kind

        if purpose not in _REVIEW_PURPOSES:
            raise HoldoutAccessDenied(f"forward results are not available for purpose {purpose!r}")
        if _kind(actor) != "human":
            raise GovernanceViolation(f"{actor} may not read forward holdout results; human review only")
        entries = self.chain.verify()
        self.chain.append("review", {"actor": actor, "through_seq": len(entries) - 1, "on": self._today()})
        return [e["payload"] for e in entries if e["kind"] in ("freeze", "observation")]
