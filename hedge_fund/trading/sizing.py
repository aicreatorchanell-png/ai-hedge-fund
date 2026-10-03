"""Position size from the stop distance: lose at most a fixed fraction of equity per trade.

    risk per unit  = |entry - stop| * multiplier + round-trip fees on the entry notional
    quantity       = min(equity * risk_fraction / risk per unit,
                         equity * max_notional_fraction / (entry * multiplier * (1 + 2 * fee))

rounded *down* to the venue's size increment. Returns 0.0 when the result is below
the venue minimum quantity or minimum notional: the trade is skipped, never sized up.
"""

from __future__ import annotations

import math
from decimal import Decimal


def size_for_stop(equity: float, entry: float, stop: float, *, risk_fraction: float,
                  max_notional_fraction: float, size_increment: float, min_quantity: float = 0.0,
                  min_notional: float = 0.0, multiplier: float = 1.0, fee_rate: float = 0.0) -> float:
    for name, v in (("equity", equity), ("entry", entry), ("stop", stop)):
        if not math.isfinite(v):
            raise ValueError(f"{name} must be finite")
    if entry <= 0 or stop <= 0:
        raise ValueError("entry and stop must be positive prices")
    if stop == entry:
        raise ValueError("stop must differ from entry")
    if not 0 < risk_fraction <= 1 or not 0 < max_notional_fraction or size_increment <= 0:
        raise ValueError("risk_fraction in (0, 1], max_notional_fraction > 0, size_increment > 0")
    if equity <= 0:
        return 0.0
    per_unit = abs(entry - stop) * multiplier + 2 * fee_rate * entry * multiplier
    cap = equity * max_notional_fraction / (entry * multiplier * (1 + 2 * fee_rate))
    raw = min(equity * risk_fraction / per_unit, cap)
    step = Decimal(str(size_increment))
    q = float((Decimal(str(raw)) / step).to_integral_value(rounding="ROUND_FLOOR") * step)
    if q <= 0 or q < min_quantity or q * entry * multiplier < min_notional:
        return 0.0
    return q
