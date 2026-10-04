"""Event journal (append-only JSONL) and atomic per-strategy state snapshots.

The journal is the audit trail of a paper run: signals, entries, refusals, fills (price,
commission, slippage vs. the decision reference), rejections, positions, kill switch,
restarts. Snapshots hold the risk state needed after a restart (governor latches, health,
peak equity) and the dashboard snapshot (positions, P&L, health, connection).
Writes are atomic (temp file + rename) and fsync'd; a crash never leaves a torn file.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class Journal:
    def __init__(self, state_dir: Path | str) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "journal.jsonl"

    def event(self, kind: str, **fields) -> None:
        rec = {"kind": kind, "wall": time.time(), **fields}
        with open(self.path, "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def tail(self, n: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]


class StateStore:
    """One JSON document per strategy id, plus the node-level dashboard snapshot."""

    def __init__(self, state_dir: Path | str) -> None:
        self.dir = Path(state_dir) / "state"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        return self.dir / (key.replace("/", "_").replace(":", "_") + ".json")

    def save(self, key: str, state: dict) -> None:
        _atomic_write(self._p(key), json.dumps(state, default=str, indent=1))

    def load(self, key: str) -> dict | None:
        p = self._p(key)
        return json.loads(p.read_text()) if p.exists() else None

    def keys(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))
