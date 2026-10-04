"""Causal market-regime classification for diagnostics (not for trading rules).

Labels each day from information available at that day's close, then shifts the label
by one day before it is matched with returns: the return of day d+1 is attributed to
the regime known at the close of day d.

    trend      t-statistic of the OLS slope of log price over the trailing `trend_window`
               days. bull if t > +t_crit, bear if t < -t_crit, sideways otherwise. Using the
               slope's t-statistic instead of a single moving-average cross scales the
               evidence by the noise in the window, so a drift inside a volatile range is
               not called a trend.
    volatility realized volatility over `vol_window` days, ranked against its own *past*
               values only (expanding window, at least `vol_min_history` days): high above
               the `high_q` percentile, low below `low_q`, normal in between.

Before enough history exists the labels are "unknown". Nothing here reads data: the
caller passes a close series, and `guard_research_dates` refuses series that reach
into a sealed holdout window for the market.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from hedge_fund.validation.holdout_guard import HoldoutAccessDenied, sealed_windows_in_force

TRENDS = ("bull", "bear", "sideways")
VOLS = ("high", "low", "normal")


class RegimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trend_window: int = Field(90, ge=20)
    t_crit: float = Field(2.0, gt=0)
    vol_window: int = Field(30, ge=5)
    vol_min_history: int = Field(180, ge=30)
    high_q: float = Field(0.70, gt=0.5, lt=1)
    low_q: float = Field(0.30, gt=0, lt=0.5)


def guard_research_dates(index: pd.DatetimeIndex, market: str = "*") -> None:
    for lo, hi in sealed_windows_in_force(market):
        lo_t, hi_t = pd.Timestamp(lo, tz="UTC"), pd.Timestamp(hi, tz="UTC") + pd.Timedelta(days=1)
        idx = index.tz_convert("UTC") if index.tz is not None else index.tz_localize("UTC")
        if ((idx >= lo_t) & (idx < hi_t)).any():
            raise HoldoutAccessDenied(f"series reaches sealed window {lo}..{hi} ({market})")


def _slope_t(y: np.ndarray) -> float:
    n = len(y)
    x = np.arange(n, dtype=float)
    x -= x.mean()
    b = (x * (y - y.mean())).sum() / (x * x).sum()
    resid = y - y.mean() - b * x
    s2 = (resid ** 2).sum() / (n - 2)
    se = np.sqrt(s2 / (x * x).sum())
    return float(b / se) if se > 0 else 0.0


def classify(close: pd.Series, config: RegimeConfig = RegimeConfig(), *, market: str = "*") -> pd.DataFrame:
    """Daily close series -> DataFrame[trend, vol, t_stat, vol_pct], label as of each close."""
    guard_research_dates(close.index, market)
    c = close.astype(float).dropna()
    logp = np.log(c.to_numpy())
    rets = np.diff(logp, prepend=np.nan)
    n = len(c)
    t_stat = np.full(n, np.nan)
    w = config.trend_window
    for i in range(w - 1, n):
        t_stat[i] = _slope_t(logp[i - w + 1:i + 1])
    rv = pd.Series(rets, index=c.index).rolling(config.vol_window).std().to_numpy()
    pct = np.full(n, np.nan)
    hist: list[float] = []
    for i in range(n):
        if np.isnan(rv[i]):
            continue
        if len(hist) >= config.vol_min_history:
            pct[i] = (np.searchsorted(np.sort(hist), rv[i], side="right")) / len(hist)
        hist.append(rv[i])
    trend = np.where(np.isnan(t_stat), "unknown",
                     np.where(t_stat > config.t_crit, "bull", np.where(t_stat < -config.t_crit, "bear", "sideways")))
    vol = np.where(np.isnan(pct), "unknown",
                   np.where(pct > config.high_q, "high", np.where(pct < config.low_q, "low", "normal")))
    return pd.DataFrame({"trend": trend, "vol": vol, "t_stat": t_stat, "vol_pct": pct}, index=c.index)


def labels_for_returns(regimes: pd.DataFrame, returns: pd.Series) -> pd.DataFrame:
    """Attach to each return the regime known at the *previous* close."""
    r = regimes.copy()
    r.index = r.index.normalize()
    shifted = r[["trend", "vol"]].shift(1)
    idx = returns.index.normalize()
    out = shifted.reindex(idx).fillna("unknown")
    out.index = returns.index
    return out


def performance_by_regime(returns: pd.Series, regimes: pd.DataFrame, *, periods_per_year: int = 365,
                          dominance: float = 0.70) -> dict:
    """Per trend and per volatility regime: days, summed return, share of profits, Sharpe,
    hit rate. Flags a strategy whose profits come mostly from one regime."""
    lab = labels_for_returns(regimes, returns)
    out: dict = {}
    total = float(returns.sum())
    for dim in ("trend", "vol"):
        rows = {}
        for name, grp in returns.groupby(lab[dim]):
            g = grp.to_numpy()
            sd = g.std(ddof=1) if len(g) > 1 else 0.0
            rows[name] = {"days": int(len(g)), "sum_return": float(g.sum()),
                          "sharpe_annual": float(g.mean() / sd * np.sqrt(periods_per_year)) if sd > 0 else 0.0,
                          "hit_rate": float((g > 0).mean()) if len(g) else 0.0}
        gains = {k: max(v["sum_return"], 0.0) for k, v in rows.items() if k != "unknown"}
        pos = sum(gains.values())
        for k, v in rows.items():
            v["share_of_profit"] = (gains.get(k, 0.0) / pos) if pos > 0 else 0.0
        top = max(gains, key=gains.get) if gains else None
        out[dim] = {"by_regime": rows, "top_regime": top,
                    "dominated": bool(total > 0 and top is not None and rows[top]["share_of_profit"] > dominance),
                    "losing_regimes": sorted(k for k, v in rows.items() if k != "unknown" and v["sum_return"] < 0)}
    out["total_return_sum"] = total
    return out
