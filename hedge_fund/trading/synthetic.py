"""Seeded synthetic OHLCV bars for engine tests (no market data, no research value).

A random walk in log price whose drift switches between regimes, so breakouts and
reversals both occur. Each bar is stamped at its close: ts_event = ts_init = close
time, which is when the bar becomes known.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
from nautilus_trader.model.data import Bar, BarType


def synthetic_bars(instrument, bar_type: BarType, n: int, *, start: str = "2020-01-01", minutes: int = 1,
                   price: float = 100.0, vol: float = 0.002, seed: int = 0) -> list[Bar]:
    rng = np.random.default_rng(seed)
    regime = np.repeat(rng.choice([-1.0, 0.0, 1.0], size=n // 200 + 1), 200)[:n]
    rets = rng.normal(regime * vol * 0.15, vol, size=n)
    closes = price * np.exp(np.cumsum(rets))
    opens = np.concatenate([[price], closes[:-1]])
    wick = np.abs(rng.normal(0, vol * 0.6, size=(2, n)))
    highs = np.maximum(opens, closes) * (1 + wick[0])
    lows = np.minimum(opens, closes) * (1 - wick[1])
    vols = rng.uniform(5, 50, size=n)
    t0 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1e9)
    step = minutes * 60 * 1_000_000_000
    out = []
    for i in range(n):
        ts = t0 + (i + 1) * step
        o, h, lo, c = (instrument.make_price(x) for x in (opens[i], highs[i], lows[i], closes[i]))
        h = max(h, o, c)
        lo = min(lo, o, c)
        out.append(Bar(bar_type, o, h, lo, c, instrument.make_qty(vols[i]), ts, ts))
    return out


def synthetic_funding(start: str = "2020-01-01", n: int = 400, *, every_minutes: int = 60, seed: int = 0,
                      lead: int = 60) -> tuple[tuple[int, float], ...]:
    """((settlement_ns, rate), ...) for tests and audit probes: mostly small positive rates with
    occasional positive and negative extremes. `lead` settlements fall before *start* so a
    strategy has history at the first bar. Settlements land 1 ms after the interval boundary,
    like Binance's calc_time."""
    rng = np.random.default_rng(seed)
    rates = rng.normal(1e-4, 5e-5, size=n)
    spikes = rng.random(n)
    rates[spikes > 0.95] += 2e-3
    rates[spikes < 0.05] -= 2e-3
    t0 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1e9)
    step = every_minutes * 60 * 1_000_000_000
    return tuple((t0 + (i - lead) * step + 1_000_000, float(r)) for i, r in enumerate(rates))
