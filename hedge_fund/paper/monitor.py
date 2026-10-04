"""PaperMonitor: a Nautilus Actor that writes the dashboard snapshot every few seconds.

snapshot.json (atomic) holds, per strategy and position: market, strategy, last signal,
open positions with entry, stop-loss and take-profit (from the working bracket orders),
realized and unrealized P&L, fees, average slippage, drawdown, strategy health,
connection health (age of the last execution bar) and rejected/denied order counts.
It also writes heartbeat.json for the supervisor. Node health is the worst of:

    strategy health (HEALTHY/WARNING/DEGRADED/HALT), kill switch (HALT),
    stale market data (DEGRADED), rejected orders in the last hour (WARNING)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig
from nautilus_trader.model.enums import OrderType, PriceType

from hedge_fund.paper.journal import Journal, _atomic_write

ORDER = ["HEALTHY", "WARNING", "DEGRADED", "HALT"]


class PaperMonitorConfig(ActorConfig, frozen=True):
    snapshot_every_secs: int = 10
    stale_after_secs: int = 180


class PaperMonitor(Actor):
    def __init__(self, config: PaperMonitorConfig, *, strategies: list, state_dir: Path | str) -> None:
        super().__init__(config)
        self.strategies = strategies
        self.dir = Path(state_dir)
        self.journal = Journal(self.dir)

    def on_start(self) -> None:
        from datetime import timedelta
        self.clock.set_timer("snapshot", timedelta(seconds=self.config.snapshot_every_secs),
                             callback=lambda _e: self.write_snapshot())

    def on_stop(self) -> None:
        self.write_snapshot()

    def snapshot(self) -> dict:
        now = self.clock.timestamp_ns()
        events = self.journal.tail(2000)
        hour_ago = time.time() - 3600
        rejects = [e for e in events if e["kind"] in ("order_rejected", "order_denied")]
        slips = [e["slippage_bps"] for e in events if e["kind"] == "fill" and e.get("slippage_bps") is not None]
        strategies, worst = [], "HEALTHY"
        for s in self.strategies:
            age = None if s.last_exec_ts is None else (now - s.last_exec_ts) / 1e9
            conn = "NO DATA" if age is None else ("STALE" if age > self.config.stale_after_secs else "OK")
            health = s.health.state.name if s.health is not None else "HEALTHY"
            if s.governor is not None and s.governor.killed:
                health = "HALT"
            g = s.governor
            dd = (1 - s._last_equity / g.peak) if g is not None and g.peak and hasattr(s, "_last_equity") else 0.0
            state = health
            if conn != "OK" and ORDER.index(state) < ORDER.index("DEGRADED"):
                state = "DEGRADED"
            worst = max(worst, state, key=ORDER.index)
            last_signal = next((e for e in reversed(events) if e.get("strategy") == str(s.id)
                                and e["kind"] in ("entry", "refused")), None)
            strategies.append({"strategy": str(s.id), "instrument": str(s.config.instrument_id),
                               "health": state, "connection": conn, "data_age_secs": age,
                               "kill_switch": None if g is None else g.killed,
                               "equity": getattr(s, "_last_equity", None), "drawdown": dd,
                               "last_signal": last_signal})
        positions = []
        for p in self.cache.positions_open():
            orders = [o for o in self.cache.orders_open(instrument_id=p.instrument_id)]
            sl = next((float(o.trigger_price) for o in orders if o.order_type == OrderType.STOP_MARKET), None)
            tp = next((float(o.price) for o in orders if o.order_type == OrderType.LIMIT), None)
            px = self.cache.price(p.instrument_id, PriceType.LAST)
            upnl = p.unrealized_pnl(px) if px is not None else None
            positions.append({"instrument": str(p.instrument_id), "strategy": str(p.strategy_id),
                              "side": p.side.name, "quantity": float(p.quantity), "entry": float(p.avg_px_open),
                              "stop_loss": sl, "take_profit": tp, "last": None if px is None else float(px),
                              "unrealized_pnl": None if upnl is None else float(upnl),
                              "fees": sum(float(c) for c in p.commissions())})
        closed = self.cache.positions_closed()
        realized = sum(float(p.realized_pnl) for p in closed if p.realized_pnl is not None)
        venue_fees = sum(float(c) for p in closed for c in p.commissions()) + sum(x["fees"] for x in positions)
        fills = [e for e in events if e["kind"] == "fill"]
        modeled = sum(e.get("modeled_fee") or 0.0 for e in fills)
        fees = max(venue_fees, modeled)
        if [e for e in rejects if e["wall"] > hour_ago] and worst == "HEALTHY":
            worst = "WARNING"
        return {"ts": now, "wall": time.time(), "node_health": worst, "strategies": strategies,
                "positions": positions, "realized_pnl": realized,
                "realized_pnl_net": realized - (fees if venue_fees == 0 else 0.0), "venue_fees": venue_fees,
                "modeled_fees": modeled,
                "unrealized_pnl": sum(x["unrealized_pnl"] or 0.0 for x in positions), "fees": fees,
                "avg_slippage_bps": (sum(slips) / len(slips)) if slips else None,
                "rejected_orders": len(rejects), "closed_positions": len(closed),
                "recent_events": events[-15:]}

    def write_snapshot(self) -> dict:
        snap = self.snapshot()
        _atomic_write(self.dir / "snapshot.json", json.dumps(snap, default=str, indent=1))
        _atomic_write(self.dir / "heartbeat.json", json.dumps({"wall": snap["wall"], "health": snap["node_health"]}))
        return snap
