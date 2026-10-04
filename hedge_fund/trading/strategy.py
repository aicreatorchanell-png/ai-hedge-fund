"""GuardedStrategy: the base for every strategy on the Nautilus engine.

A subclass implements `on_signal(bar)` and calls `enter(side, stop, take_profit, bar)`.

Two bar streams: `bar_type` is the execution stream (1-minute external bars; fills,
stops and targets are simulated on it) and `signal_minutes` > 1 adds a signal stream
aggregated inside Nautilus from it (`{n}-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL`).
A signal bar closing at T contains exactly the execution bars closing in (T - n, T]
and arrives after the execution bar closing at T (tested), so decisions never see
an unfinished bar. Indicators registered in `register_indicators` should use
`self.signal_bar_type`.

The base class:

    - marks equity on every bar and feeds the RiskGovernor; once the kill switch is
      engaged it cancels open orders, closes positions and ignores further signals
    - refuses an entry the governor does not approve (daily loss, caps, kill switch)
    - sizes it from the stop distance (sizing.size_for_stop) and the venue minimums, capped
      at `max_volume_participation` of the median volume of the last `volume_lookback`
      execution bars (no entry without volume history)
    - submits it as a bracket: market entry + stop-market stop-loss + limit take-profit
      (one cancels the other), so every position has its exits at the venue from the start
    - records every bracket and every refusal for the audit

Paper/testnet additions (off by default, required by hedge_fund.paper):

    max_data_age_secs            no entry when the last execution bar is older than this
                                 (stale or disconnected market data)
    max_total_exposure_fraction  no entry that would lift gross exposure across all open
                                 positions on the venue above this fraction of equity
    journal                      receives signal / order / fill / reject / position events
    state_store                  persists governor and health state (kill switch, HALT,
                                 peak equity, day start) and restores it after a restart,
                                 so a latched halt survives a process restart
"""

from __future__ import annotations

from pathlib import Path

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OrderSide, OrderType, PriceType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from hedge_fund.trading.governor import RiskGovernor, TradeRiskConfig
from hedge_fund.trading.sizing import size_for_stop


DEFAULT_VOLUME_PARTICIPATION = 0.10


class GuardedConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    signal_minutes: int = 1
    allow_short: bool = False
    max_hold_bars: int = 0                  # time stop in signal bars; 0 = exits only by stop/target
    segment_starts_ns: tuple[int, ...] = ()  # research: walk-forward segment starts (risk state resets)
    max_volume_participation: float = DEFAULT_VOLUME_PARTICIPATION  # an entry may not exceed this share of median recent bar volume
    volume_lookback: int = 60               # execution bars in that median
    mark_every_minutes: int = 60
    max_data_age_secs: int = 0              # 0 = off (backtests); paper requires > 0
    max_total_exposure_fraction: float = 0.0  # 0 = off; paper requires a cap
    max_order_notional: float = 0.0         # 0 = off; paper sets it below the risk engine's per-order limit
    protect_on_fill: bool = False           # live/paper: SL/TP sized to the actual (partially filled) position
    modeled_taker_fee: float = 0.0          # paper: fee charged in the journal when the venue reports none
    modeled_maker_fee: float = 0.0

class GuardedStrategy(Strategy):
    def __init__(self, config: GuardedConfig, risk: TradeRiskConfig, *, kill_dir: Path | str | None = None,
                 health=None, journal=None, state_store=None) -> None:
        super().__init__(config)
        self.risk = risk
        self.journal = journal
        self.state_store = state_store
        self.health = health                     # HealthMonitor (paper/live); HALT blocks new entries
        self.kill_dir = kill_dir
        self.governor: RiskGovernor | None = None
        self.instrument = None
        self.brackets: list[dict] = []
        self.refusals: list[dict] = []
        self.equity_curve: list[tuple[int, float]] = []
        self.last_exec_ts: int | None = None
        self.exits: list[dict] = []
        self._held = 0
        self._flattened = False
        self._mark_ns = config.mark_every_minutes * 60_000_000_000
        n = config.signal_minutes
        if n < 1:
            raise ValueError("signal_minutes must be >= 1")
        spec = f"{n // 60}-HOUR" if n % 60 == 0 else f"{n}-MINUTE"     # Nautilus wants 1-HOUR, not 60-MINUTE
        if not 0 < config.max_volume_participation <= 0.25:
            raise ValueError("max_volume_participation must be in (0, 0.25]")
        from collections import deque
        self._volumes = deque(maxlen=max(1, config.volume_lookback))
        src = "INTERNAL" if config.bar_type.is_internally_aggregated() else "EXTERNAL"
        self.signal_bar_type = (config.bar_type if n == 1 else
                                BarType.from_str(f"{config.instrument_id}-{spec}-LAST-INTERNAL@"
                                                 f"{config.bar_type.spec.step}-MINUTE-{src}"))

    # -- lifecycle --------------------------------------------------------

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            raise RuntimeError(f"instrument {self.config.instrument_id} not in cache")
        self.register_indicators()
        self.subscribe_bars(self.config.bar_type)
        if self.signal_bar_type != self.config.bar_type:
            self.subscribe_bars(self.signal_bar_type)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.config.instrument_id)
        self.save_state("stop")
        if self.last_exec_ts is not None and self.equity_curve[-1][0] != self.last_exec_ts:
            self.equity_curve.append((self.last_exec_ts, self._last_equity))

    def on_bar(self, bar: Bar) -> None:
        if bar.bar_type.spec != self.config.bar_type.spec:          # a signal bar (n-minute aggregate)
            if self.governor is not None and not self.governor.killed:
                self._signal(bar)
            return
        self._on_exec_bar(bar)
        if self.config.signal_minutes == 1 and not self.governor.killed:
            self._signal(bar)

    def _signal(self, bar: Bar) -> None:
        iid = self.config.instrument_id
        if self.config.max_hold_bars and self.cache.positions_open(instrument_id=iid):
            self._held += 1
            if self._held >= self.config.max_hold_bars:
                self.flatten("time stop")
                return
        else:
            self._held = 0
        self.on_signal(bar)

    def flatten(self, reason: str) -> None:
        """Cancel working orders and close the position at the next execution bar."""
        iid = self.config.instrument_id
        self.cancel_all_orders(iid)
        self.close_all_positions(iid)
        self.exits.append({"ts": self.clock.timestamp_ns(), "reason": reason})

    def _on_exec_bar(self, bar: Bar) -> None:
        equity = self.equity(float(bar.close))
        if self.governor is None:
            self.governor = RiskGovernor(self.risk, equity, kill_dir=self.kill_dir)
            self._segments = sorted(self.config.segment_starts_ns)
            self.restore_state()
        if self._segments and bar.ts_event >= self._segments[0]:
            while self._segments and bar.ts_event >= self._segments[0]:
                self._segments.pop(0)
            self.governor.start_segment(bar.ts_event, equity)
            self._flattened = False
        else:
            self.governor.update(bar.ts_event, equity)
        if not self.equity_curve or bar.ts_event // self._mark_ns != self.equity_curve[-1][0] // self._mark_ns:
            self.equity_curve.append((bar.ts_event, equity))
        self.last_exec_ts = bar.ts_event
        self._last_equity = equity
        self._volumes.append(float(bar.volume))
        if self.health is not None and (not self.health.equity or
                                        bar.ts_event // 86_400_000_000_000 != self.health.equity[-1][0] // 86_400_000_000_000):
            self.health.record_equity(bar.ts_event, equity)
        if self.governor.killed and not self._flattened:
            self.cancel_all_orders(self.config.instrument_id)
            self.close_all_positions(self.config.instrument_id)
            self._flattened = True
            self._journal("kill_switch", reason=self.governor.killed)
            self.save_state("kill")
        if self.state_store is not None and len(self.equity_curve) != getattr(self, "_saved_marks", -1):
            self._saved_marks = len(self.equity_curve)
            self.save_state("mark")

    # -- subclass hooks ---------------------------------------------------

    def register_indicators(self) -> None:
        """Register indicators with self.register_indicator_for_bars (optional)."""

    def on_signal(self, bar: Bar) -> None:
        raise NotImplementedError

    # -- helpers ----------------------------------------------------------

    def equity(self, mark: float) -> float:
        """Account equity in the quote currency, marking any base holdings at *mark*."""
        account = self.portfolio.account(self.config.instrument_id.venue)
        quote = self.instrument.quote_currency
        cash = account.balance_total(quote)
        total = float(cash) if cash is not None else 0.0
        if account.type == AccountType.CASH:
            base = getattr(self.instrument, "base_currency", None)
            held = account.balance_total(base) if base is not None else None
            total += float(held) * mark if held is not None else 0.0
        else:
            pnl = self.portfolio.unrealized_pnl(self.config.instrument_id)
            total += float(pnl) if pnl is not None else 0.0
        return total

    def is_flat(self) -> bool:
        """No open position and no order that is not closed (in-flight orders count:
        a just-filled entry's stop and target are in flight before they are open)."""
        iid = self.config.instrument_id
        return not self.cache.positions_open(instrument_id=iid) and self._live_orders() == 0

    def _live_orders(self) -> int:
        return sum(1 for o in self.cache.orders(instrument_id=self.config.instrument_id) if not o.is_closed)

    def enter(self, side: OrderSide, stop: float, take_profit: float, bar: Bar) -> bool:
        ref = float(bar.close)
        if stop <= 0 or take_profit <= 0:            # the venue would deny it and strand the bracket
            return self._refuse(bar, "non-positive stop or target price", stop=stop, take_profit=take_profit)
        if side == OrderSide.BUY and not stop < ref < take_profit:
            raise ValueError(f"long bracket needs stop < {ref} < take_profit, got {stop}, {take_profit}")
        if side == OrderSide.SELL:
            if not self.config.allow_short:
                return self._refuse(bar, "short selling disabled")
            if self.portfolio.account(self.config.instrument_id.venue).type == AccountType.CASH:
                return self._refuse(bar, "cash account cannot short")
            if not take_profit < ref < stop:
                raise ValueError(f"short bracket needs take_profit < {ref} < stop, got {take_profit}, {stop}")
        if self.health is not None and not self.health.allows_entries():
            return self._refuse(bar, "health HALT")
        age = self.data_age_secs()
        if self.config.max_data_age_secs and age is not None and age > self.config.max_data_age_secs:
            return self._refuse(bar, "stale data", data_age_secs=age)
        equity = self.equity(ref)
        iid = self.config.instrument_id
        ok, why = self.governor.can_enter(equity, len(self.cache.positions_open(instrument_id=iid)))
        if not ok:
            return self._refuse(bar, why)
        if self._live_orders():
            return self._refuse(bar, "orders pending")
        inst = self.instrument
        qty = size_for_stop(
            equity, ref, stop, risk_fraction=self.risk.risk_per_trade,
            max_notional_fraction=self.risk.max_notional_fraction,
            size_increment=float(inst.size_increment),
            min_quantity=float(inst.min_quantity) if inst.min_quantity is not None else 0.0,
            min_notional=float(inst.min_notional) if inst.min_notional is not None else 0.0,
            multiplier=float(inst.multiplier), fee_rate=float(inst.taker_fee))
        cap = self.volume_cap(float(inst.size_increment))
        if cap is None:
            return self._refuse(bar, "no volume history")
        risk_qty, qty = qty, min(qty, cap)
        if self.config.max_order_notional:
            from decimal import Decimal
            st = Decimal(str(float(inst.size_increment)))
            lim = Decimal(str(self.config.max_order_notional / (ref * float(inst.multiplier))))
            qty = min(qty, float((lim / st).to_integral_value(rounding="ROUND_FLOOR") * st))
        if qty <= 0 or (inst.min_quantity is not None and qty < float(inst.min_quantity)) or \
                (inst.min_notional is not None and qty * ref * float(inst.multiplier) < float(inst.min_notional)):
            return self._refuse(bar, "size below venue minimum", risk_qty=risk_qty, volume_cap=cap,
                                min_quantity=None if inst.min_quantity is None else float(inst.min_quantity),
                                min_notional=None if inst.min_notional is None else float(inst.min_notional),
                                equity=equity)
        if self.config.max_total_exposure_fraction:
            gross = self.gross_exposure()
            if gross + qty * ref * float(inst.multiplier) > self.config.max_total_exposure_fraction * equity:
                return self._refuse(bar, "portfolio exposure cap")
        if self.config.protect_on_fill:
            entry = self.order_factory.market(iid, side, inst.make_qty(qty))
            self._protect = {"side": side, "stop": float(inst.make_price(stop)),
                             "take_profit": float(inst.make_price(take_profit))}
            self.brackets.append({
                "ts": bar.ts_event, "side": side.name, "quantity": qty, "reference": ref,
                "stop": self._protect["stop"], "take_profit": self._protect["take_profit"],
                "entry_id": entry.client_order_id.value, "sl_id": None, "tp_id": None})
            self.governor.record_entry()
            self._journal("entry", **{("decision_ts" if k == "ts" else k): v for k, v in self.brackets[-1].items()})
            self.submit_order(entry)
            return True
        orders = self.order_factory.bracket(
            iid, side, inst.make_qty(qty),
            sl_trigger_price=inst.make_price(stop), tp_price=inst.make_price(take_profit))
        entry = orders.first
        sl = next(o for o in orders.orders if o.order_type == OrderType.STOP_MARKET)
        tp = next(o for o in orders.orders if o.order_type == OrderType.LIMIT)
        self.brackets.append({
            "ts": bar.ts_event, "side": side.name, "quantity": qty, "reference": ref,
            "stop": float(inst.make_price(stop)), "take_profit": float(inst.make_price(take_profit)),
            "entry_id": entry.client_order_id.value, "sl_id": sl.client_order_id.value, "tp_id": tp.client_order_id.value})
        self.governor.record_entry()
        self._journal("entry", **{("decision_ts" if k == "ts" else k): v for k, v in self.brackets[-1].items()})
        self.submit_order_list(orders)
        return True

    # -- live/paper position protection --------------------------------------

    def sync_protection(self) -> None:
        """Keep exactly one reduce-only stop-loss and one reduce-only take-profit, each sized to
        the current position. Called whenever the position opens or changes (partial fills)."""
        protect = getattr(self, "_protect", None)
        iid = self.config.instrument_id
        positions = self.cache.positions_open(instrument_id=iid, strategy_id=self.id)
        working = [o for o in self.cache.orders_open(instrument_id=iid, strategy_id=self.id) if o.is_reduce_only]
        if not positions or protect is None:
            for o in working:
                self.cancel_order(o)
            return
        qty = sum(float(p.quantity) for p in positions)
        if working and all(abs(float(o.quantity) - qty) < 1e-12 for o in working) and len(working) == 2:
            return
        for o in working:
            self.cancel_order(o)
        inst = self.instrument
        exit_side = OrderSide.SELL if protect["side"] == OrderSide.BUY else OrderSide.BUY
        q = inst.make_qty(qty)
        sl = self.order_factory.stop_market(iid, exit_side, q, trigger_price=inst.make_price(protect["stop"]),
                                            reduce_only=True)
        tp = self.order_factory.limit(iid, exit_side, q, price=inst.make_price(protect["take_profit"]),
                                      reduce_only=True)
        self.submit_order(sl)
        self.submit_order(tp)
        if self.brackets:
            self.brackets[-1]["sl_id"], self.brackets[-1]["tp_id"] = sl.client_order_id.value, tp.client_order_id.value
        self._journal("protection", quantity=qty, stop=protect["stop"], take_profit=protect["take_profit"])

    def on_position_changed(self, event) -> None:
        if self.config.protect_on_fill:
            self.sync_protection()

    def data_age_secs(self) -> float | None:
        """Seconds since the last execution bar closed (None before the first bar)."""
        if self.last_exec_ts is None:
            return None
        return (self.clock.timestamp_ns() - self.last_exec_ts) / 1e9

    def gross_exposure(self) -> float:
        """Sum of |quantity| x mark x multiplier over every open position on this venue."""
        total = 0.0
        for p in self.cache.positions_open(venue=self.config.instrument_id.venue):
            inst = self.cache.instrument(p.instrument_id)
            px = self.cache.price(p.instrument_id, PriceType.LAST)
            mark = float(px) if px is not None else float(p.avg_px_open)
            total += abs(float(p.signed_qty)) * mark * (float(inst.multiplier) if inst is not None else 1.0)
        return total

    def volume_cap(self, step: float) -> float | None:
        """Largest entry allowed by liquidity: participation x median volume of recent execution
        bars, rounded down to the size step. None without volume history."""
        if not self._volumes:
            return None
        from decimal import Decimal
        med = sorted(self._volumes)[len(self._volumes) // 2]
        raw = self.config.max_volume_participation * med
        st = Decimal(str(step))
        return float((Decimal(str(raw)) / st).to_integral_value(rounding="ROUND_FLOOR") * st)

    def on_position_closed(self, event) -> None:
        if self.config.protect_on_fill:
            self._protect = None
            self.sync_protection()                       # cancel the leftover stop or target
        pnl = float(event.realized_pnl) if event.realized_pnl is not None else 0.0
        if self.health is not None:
            self.health.record_trade(event.ts_event, pnl)
        self._journal("position_closed", position_id=str(event.position_id), realized_pnl=pnl)
        self.save_state("position_closed")

    def on_position_opened(self, event) -> None:
        if self.config.protect_on_fill:
            self.sync_protection()
        self._journal("position_opened", position_id=str(event.position_id), side=str(event.entry),
                      quantity=float(event.quantity), avg_px=float(event.avg_px_open))

    def on_order_filled(self, event) -> None:
        ref = next((b["reference"] for b in reversed(self.brackets) if b["entry_id"] == event.client_order_id.value),
                   None)
        px = float(event.last_px)
        slip = None if ref is None else (px / ref - 1) * 1e4 * (1 if event.order_side == OrderSide.BUY else -1)
        maker = str(getattr(event.liquidity_side, "name", event.liquidity_side)) == "MAKER"
        rate = self.config.modeled_maker_fee if maker else self.config.modeled_taker_fee
        self._journal("fill", order_id=event.client_order_id.value, side=event.order_side.name,
                      qty=float(event.last_qty), price=px, commission=float(event.commission),
                      modeled_fee=float(event.last_qty) * px * rate, liquidity="MAKER" if maker else "TAKER",
                      slippage_bps=slip)

    def on_order_rejected(self, event) -> None:
        self._journal("order_rejected", order_id=event.client_order_id.value, reason=str(event.reason))
        order = self.cache.order(event.client_order_id)
        if self.config.protect_on_fill and order is not None and order.is_reduce_only and \
                self.cache.positions_open(instrument_id=self.config.instrument_id, strategy_id=self.id):
            # a position must never stay unprotected: if a stop or target is refused
            # (e.g. the price already gapped through it), exit at market now
            self._protect = None
            self.flatten("protection rejected")
            self._journal("protection_rejected_flatten", order_id=event.client_order_id.value)

    def on_order_denied(self, event) -> None:
        self._journal("order_denied", order_id=event.client_order_id.value, reason=str(event.reason))

    def _refuse(self, bar: Bar, reason: str, **detail) -> bool:
        self.refusals.append({"ts": bar.ts_event, "reason": reason})
        self._journal("refused", reason=reason, **detail)
        return False

    # -- journal and persisted state ----------------------------------------

    def _journal(self, kind: str, **fields) -> None:
        """Monitoring must never break trading or risk logic: a journal failure is logged only."""
        if self.journal is None:
            return
        try:
            base = {"ts": self.clock.timestamp_ns(), "strategy": str(self.id), "instrument": str(self.config.instrument_id)}
            self.journal.event(kind, **{**base, **fields})
        except Exception as exc:                                       # noqa: BLE001
            self.log.error(f"journal write failed ({kind}): {exc!r}")

    def state(self) -> dict:
        g, h = self.governor, self.health
        return {
            "governor": None if g is None else {"peak": g.peak, "day": g.day, "day_start": g.day_start,
                                                "entries_today": g.entries_today, "killed": g.killed,
                                                "events": g.events[-50:]},
            "health": None if h is None else {"latched": h.latched,
                                              "trades": [vars(t) for t in h.trades[-500:]],
                                              "equity": h.equity[-400:]},
            "risk_config_hash": self.risk.config_hash(),
        }

    def save_state(self, reason: str) -> None:
        if self.state_store is not None and self.governor is not None:
            self.state_store.save(str(self.id), {**self.state(), "saved_reason": reason,
                                                 "saved_at": self.clock.timestamp_ns()})

    def restore_state(self) -> None:
        """Restore latches and risk state after a restart. A kill switch or HALT that was
        engaged before the restart stays engaged; limits come from the frozen config."""
        if self.state_store is None:
            return
        st = self.state_store.load(str(self.id))
        if not st:
            return
        g = st.get("governor") or {}
        if g:
            self.governor.peak = max(self.governor.peak, g.get("peak") or 0.0)
            self.governor.day, self.governor.day_start = g.get("day"), g.get("day_start") or self.governor.day_start
            self.governor.entries_today = g.get("entries_today", 0)
            self.governor.killed = g.get("killed")
            self.governor.events = list(g.get("events", []))
        h = st.get("health") or {}
        if self.health is not None and h:
            from hedge_fund.trading.health import TradeRecord
            self.health.trades = [TradeRecord(**t) for t in h.get("trades", [])]
            self.health.equity = [tuple(x) for x in h.get("equity", [])]
            self.health.latched = h.get("latched")
        self._journal("state_restored", killed=self.governor.killed,
                      health_latched=None if self.health is None else self.health.latched)
