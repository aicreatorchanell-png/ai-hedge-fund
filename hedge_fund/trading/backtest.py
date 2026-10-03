"""Deterministic backtest runner on the Nautilus engine, with the project's guards.

    fence         the bars' date range passes hedge_fund.validation.holdout_guard first:
                  a sealed holdout's window cannot be backtested in research mode
    bar checks    sorted, unique, stamped at close (ts_init >= ts_event)
    completeness  an engine that stops early (negative balance) raises BacktestAborted
                  instead of returning a truncated result
    determinism   fixed trader id, seeded fill model; the same inputs give the same fills
    audit         `ambiguity_audit`: exits on bars where both the stop and the target
                  were reachable, and the P&L if every such exit had hit the stop instead

The result exposes equity, per-bar returns and closed-trade P&L for the validation
stack (hedge_fund.validation.stats, gates).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.identifiers import TraderId

from hedge_fund.trading.strategy import GuardedStrategy
from hedge_fund.trading.venue import VenueSpec, add_venue
from hedge_fund.validation.holdout_guard import market_data_fence


class BacktestAborted(RuntimeError):
    """The engine stopped before the last bar (e.g. a negative account balance)."""


@dataclass
class BacktestResult:
    equity: pd.Series
    orders: pd.DataFrame
    positions: pd.DataFrame
    brackets: list[dict]
    refusals: list[dict]
    risk_events: list[dict]
    audit: dict
    meta: dict = field(default_factory=dict)

    def returns(self) -> pd.Series:
        return self.equity.pct_change().dropna()

    def trade_pnls(self) -> list[float]:
        if self.positions.empty:
            return []
        closed = self.positions[self.positions["ts_closed"].notna()]
        return [float(str(x).split()[0]) for x in closed["realized_pnl"]]


def _day(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).date().isoformat()


def check_bars(bars) -> None:
    if not bars:
        raise ValueError("no bars")
    prev = None
    for b in bars:
        if b.ts_init < b.ts_event:
            raise ValueError("bar visible before it closed (ts_init < ts_event)")
        if prev is not None and b.ts_event <= prev:
            raise ValueError("bars must be sorted with unique timestamps")
        prev = b.ts_event


def run_backtest(instrument, bars, strategy: GuardedStrategy, venue: VenueSpec) -> BacktestResult:
    check_bars(bars)
    market_data_fence(_day(bars[0].ts_event), _day(bars[-1].ts_event))
    engine = BacktestEngine(BacktestEngineConfig(trader_id=TraderId("AIHF-001"),
                                                 logging=LoggingConfig(log_level="ERROR")))
    try:
        add_venue(engine, venue)
        engine.add_instrument(instrument)
        engine.add_data(list(bars))
        engine.add_strategy(strategy)
        engine.run()
        orders = engine.trader.generate_orders_report()
        positions = engine.trader.generate_positions_report()
    finally:
        engine.dispose()
    seen = strategy.last_exec_ts
    if seen != bars[-1].ts_event:
        raise BacktestAborted(f"engine stopped at {seen} before the last bar {bars[-1].ts_event}")
    eq = pd.Series({pd.Timestamp(t, unit="ns", tz="UTC"): v for t, v in strategy.equity_curve}, dtype=float)
    return BacktestResult(
        equity=eq, orders=orders, positions=positions, brackets=list(strategy.brackets),
        refusals=list(strategy.refusals), risk_events=list(strategy.governor.events if strategy.governor else []),
        audit=ambiguity_audit(strategy.brackets, orders, bars, float(instrument.multiplier)),
        meta={"risk_config_hash": strategy.risk.config_hash(), "venue": venue.model_dump(mode="json"),
              "instrument": str(instrument.id), "bars": len(bars)})


def ambiguity_audit(brackets: list[dict], orders: pd.DataFrame, bars, multiplier: float = 1.0) -> dict:
    """Exits on a bar whose range contained both the stop and the target.

    Bars carry no intrabar order of prices, so for such an exit the engine's
    open-high-low-close path decides the outcome. The pessimistic adjustment books
    each ambiguous take-profit exit at its stop instead.
    """
    by_ts = {b.ts_event: b for b in bars}
    ts_sorted = sorted(by_ts)
    status = orders["status"].to_dict() if not orders.empty else {}
    exits = ambiguous = 0
    adjustment = 0.0
    rows = []
    for br in brackets:
        hit = "tp" if status.get(br["tp_id"]) == "FILLED" else "sl" if status.get(br["sl_id"]) == "FILLED" else None
        if hit is None:
            continue
        exits += 1
        ts_last = orders.loc[br[f"{hit}_id"], "ts_last"]
        ts = int(pd.Timestamp(ts_last).value)
        bar = by_ts.get(ts) or by_ts[ts_sorted[max(0, _bisect(ts_sorted, ts) - 1)]]
        if float(bar.low) <= min(br["stop"], br["take_profit"]) and float(bar.high) >= max(br["stop"], br["take_profit"]):
            ambiguous += 1
            if hit == "tp":
                adjustment -= abs(br["take_profit"] - br["stop"]) * br["quantity"] * multiplier
            rows.append({"entry_id": br["entry_id"], "exit": hit, "bar_ts": ts})
    return {"exits": exits, "ambiguous_exits": ambiguous,
            "ambiguous_share": ambiguous / exits if exits else 0.0,
            "pessimistic_pnl_adjustment": adjustment, "ambiguous": rows}


def _bisect(xs: list[int], x: int) -> int:
    import bisect
    return bisect.bisect_right(xs, x)
