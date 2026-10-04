"""Sandbox-only plumbing check: proves the live loop end to end with simulated fills.

market stream -> 1-minute bars -> GuardedStrategy -> risk checks -> bracket order ->
sandbox fill -> position -> stop-loss / take-profit at the venue -> time-stop exit ->
journal -> snapshot. It is not a trading strategy and has no research status: it enters
once, with the venue's minimum size, after `wait_bars` bars, and exits after
`hold_bars` bars (or at its stop / target). It refuses to run outside the sandbox.
"""

from __future__ import annotations

from nautilus_trader.model.enums import OrderSide

from hedge_fund.trading.strategy import GuardedConfig, GuardedStrategy


class PlumbingConfig(GuardedConfig, frozen=True):
    wait_bars: int = 2
    stop_pct: float = 0.01
    target_pct: float = 0.01


class PlumbingCheck(GuardedStrategy):
    def on_start(self) -> None:
        acct = self.portfolio.account(self.config.instrument_id.venue)
        inst = self.cache.instrument(self.config.instrument_id)
        self._journal("plumbing_start", venue_account=type(acct).__name__ if acct else "pending",
                      balances=[str(b) for b in acct.balances_total().values()] if acct else [],
                      quote=str(inst.quote_currency) if inst else None,
                      base=str(getattr(inst, "base_currency", None)) if inst else None)
        self._bars = 0
        self._done = False
        super().on_start()

    def on_signal(self, bar) -> None:
        self._bars += 1
        if self._done or self._bars < self.config.wait_bars or not self.is_flat():
            return
        c = float(bar.close)
        self._done = self.enter(OrderSide.BUY, c * (1 - self.config.stop_pct), c * (1 + self.config.target_pct), bar)
