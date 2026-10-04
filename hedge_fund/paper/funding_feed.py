"""Live funding settlements for paper trading (Kraken Futures public API, no credentials).

    https://futures.kraken.com/derivatives/api/v4/historicalfundingrates?symbol=PF_XBTUSD

Kraken settles funding every hour; `relativeFundingRate` is the settled rate per hour
(positive: longs pay shorts).

Holdouts. Every live read falls inside sealed windows (the crypto final holdout and the
prospective forward holdout from 2026-09-01). A forward holdout may be observed only as
real time reaches it, by a configuration frozen before its first observation. So `parse`
keeps a settlement inside a sealed window only if it is at or after `observe_from_ns`
(the moment the paper strategy started); everything earlier in a sealed window is dropped
and never seen. The quantile history is seeded instead from development-window Binance
settlements (`seed`), converted to per-hour rates.
"""

from __future__ import annotations

import pandas as pd
import requests

from hedge_fund.validation.holdout_guard import sealed_windows_in_force

URL = "https://futures.kraken.com/derivatives/api/v4/historicalfundingrates"


def _sealed() -> list[tuple[int, int]]:
    return [(pd.Timestamp(a, tz="UTC").value, (pd.Timestamp(b, tz="UTC") + pd.Timedelta(days=1)).value)
            for a, b in sealed_windows_in_force("crypto")]


def _ts(x: str) -> int:
    t = pd.Timestamp(x)
    return int((t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")).value)


def parse(payload: dict, observe_from_ns: int) -> tuple[tuple[int, float], ...]:
    windows = _sealed()
    out = []
    for r in payload.get("rates", []):
        ts = _ts(r["timestamp"])
        if ts < observe_from_ns and any(a <= ts < z for a, z in windows):
            continue                                       # sealed past: never observed
        out.append((ts, float(r["relativeFundingRate"])))
    return tuple(sorted(set(out)))


def fetch(symbol: str, observe_from_ns: int, *, session=None, timeout: float = 15.0) -> tuple[tuple[int, float], ...]:
    s = session or requests.Session()
    s.trust_env = True
    r = s.get(URL, params={"symbol": symbol}, timeout=timeout)
    r.raise_for_status()
    return parse(r.json(), observe_from_ns)


def seed(binance_symbol: str) -> tuple[tuple[int, float], ...]:
    """Development-window Binance settlements as per-hour rates (downloads missing months from
    the approved public archive; months reaching a sealed crypto window are refused)."""
    from hedge_fund.trading.data import binance, funding
    from hedge_fund.trading.data.fence import last_research_day
    months = [m for m in binance.months("2020-01", last_research_day("crypto")) if binance.month_allowed(m)]
    for m in months:
        if not funding.archive_path(binance_symbol, m).exists():
            funding.download_month(binance_symbol, m)
    df = funding.load(binance_symbol, months)
    per_hour = df["rate"] / df["interval_hours"].clip(lower=1)
    return tuple(zip(df.index.as_unit("ns").asi8.tolist(), per_hour.astype(float).tolist()))
