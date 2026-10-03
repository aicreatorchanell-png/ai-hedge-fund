"""The trading layer is backtest-only: no live node, no live broker adapter, no live flag."""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORBIDDEN = re.compile(r"TradingNode|nautilus_trader\.live|nautilus_trader\.adapters|LiveExecClient|"
                       r"LiveDataClient|LIVE_TRADING_ENABLED\s*=\s*True")


def test_no_live_trading_code_in_trading_layer():
    hits = [f"{p.name}:{i}" for p in HERE.glob("*.py") if not p.name.startswith("test_")
            for i, line in enumerate(p.read_text().splitlines(), 1) if FORBIDDEN.search(line)]
    assert hits == []


def test_live_lock_unchanged():
    from hedge_fund.brokers import adapters
    assert adapters.LIVE_TRADING_ENABLED is False
