"""Robustness diagnostics: worst-fold, parameter stability, concentration, diversification.

Diagnostics describe; they never choose parameters and never create trading rules.
Every function takes returns/trades already produced on development data;
`regimes.guard_research_dates` refuses any series that reaches a sealed holdout.

    fold_report        every walk-forward fold: return, Sharpe, max drawdown, trades,
                       regime mix; worst / median fold, dispersion, fragility flags
    neighbourhood      the chosen configuration vs. its one-step grid neighbours:
                       sharp peaks and unstable neighbours are flagged
    concentration      share of profit from the top trades, one asset, one regime
    diversification    return and drawdown correlation, overlapping exposure, shared
                       regime dependence, concentration by asset and family
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hedge_fund.trading.regimes import guard_research_dates, labels_for_returns


def _max_dd(r: pd.Series) -> float:
    eq = (1 + r).cumprod()
    return float(-(eq / eq.cummax() - 1).min()) if len(eq) else 0.0


def _sharpe(r: pd.Series, ppy: int) -> float:
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return float(r.mean() / sd * np.sqrt(ppy)) if sd > 0 else 0.0


# -- 3. worst-fold analysis ----------------------------------------------------

def fold_report(plan, wf, runs: dict, *, regimes: pd.DataFrame | None = None, market: str = "*",
                one_fold_share: float = 0.5) -> dict:
    """wf: research.WalkForwardResult; runs: config key -> ConfigRun (for trade counts)."""
    guard_research_dates(wf.oos.index, market)
    ppy = plan.periods_per_year
    folds = []
    for (tr_s, tr_e, te_s, te_e), choice in zip(plan.folds(), wf.choices):
        r = wf.oos[(wf.oos.index >= pd.Timestamp(te_s, tz="UTC")) & (wf.oos.index <= pd.Timestamp(te_e, tz="UTC"))]
        key = choice.get("choice")
        row = {"test_start": te_s, "test_end": te_e, "choice": key,
               "return": float((1 + r).prod() - 1), "sharpe_annual": _sharpe(r, ppy), "max_drawdown": _max_dd(r),
               "trades": runs[key].trades_in(te_s, te_e) if key in runs else 0}
        if regimes is not None and len(r):
            lab = labels_for_returns(regimes, r)
            row["regime_mix"] = {d: lab[d].value_counts(normalize=True).round(3).to_dict() for d in ("trend", "vol")}
        folds.append(row)
    rets = np.array([f["return"] for f in folds])
    total = float(np.prod(1 + rets) - 1) if len(rets) else 0.0
    best = int(np.argmax(rets)) if len(rets) else None
    without_best = float(np.prod(1 + np.delete(rets, best)) - 1) if best is not None else 0.0
    gains = rets[rets > 0]
    best_share = float(rets[best] / gains.sum()) if best is not None and gains.sum() > 0 else 0.0
    q1, q3 = (np.percentile(rets, [25, 75]) if len(rets) else (0.0, 0.0))
    summary = {
        "folds": len(folds), "positive": int((rets > 0).sum()), "negative": int((rets <= 0).sum()),
        "worst_fold": folds[int(np.argmin(rets))] if folds else None,
        "median_return": float(np.median(rets)) if len(rets) else 0.0,
        "dispersion_std": float(rets.std(ddof=1)) if len(rets) > 1 else 0.0,
        "dispersion_iqr": float(q3 - q1),
        "total_return": total, "total_without_best_fold": without_best, "best_fold_share_of_gains": best_share,
        "max_fold_drawdown": max((f["max_drawdown"] for f in folds), default=0.0),
    }
    summary["fragile"] = bool(total > 0 and (without_best <= 0 or best_share > one_fold_share))
    return {"folds": folds, "summary": summary}


# -- 4. stability --------------------------------------------------------------

def neighbours(grid: dict, params: dict) -> list[dict]:
    """Grid points that differ from *params* by one step in exactly one parameter."""
    out = []
    for k, values in grid.items():
        if k not in params or params[k] not in values:
            continue
        i = values.index(params[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(values):
                out.append({**params, k: values[j]})
    return out


def neighbourhood(grid: dict, params: dict, metric: dict, *, peak_ratio: float = 0.5) -> dict:
    """metric: frozenset(params.items()) -> score (e.g. training Sharpe). Flags a sharp peak
    (neighbour median below peak_ratio x chosen) and unstable neighbours (any below zero
    while the chosen one is positive)."""
    key = lambda p: frozenset(p.items())  # noqa: E731
    chosen = metric[key(params)]
    nb = [metric[key(p)] for p in neighbours(grid, params) if key(p) in metric]
    med = float(np.median(nb)) if nb else float("nan")
    return {"chosen": chosen, "neighbours": len(nb), "neighbour_median": med,
            "neighbour_min": float(min(nb)) if nb else float("nan"),
            "sharp_peak": bool(nb and chosen > 0 and med < peak_ratio * chosen),
            "unstable_neighbours": bool(nb and chosen > 0 and min(nb) < 0),
            "isolated": not nb}


def concentration(trade_pnls, *, by_asset: dict | None = None, regime_perf: dict | None = None,
                  top_trade_frac: float = 0.05, top_trade_share: float = 0.5, asset_share: float = 0.6,
                  min_trades: int = 30) -> dict:
    p = np.sort(np.asarray(trade_pnls, dtype=float))[::-1]
    net = float(p.sum()) if len(p) else 0.0
    k = max(1, int(np.ceil(len(p) * top_trade_frac))) if len(p) else 0
    top = float(p[:k].sum()) if k else 0.0
    out = {"trades": int(len(p)), "net_pnl": net, "top_trades": k,
           "top_trades_share": (top / net) if net > 0 else float("inf") if top > 0 else 0.0}
    out["few_trades"] = bool(len(p) < min_trades)
    out["dominated_by_few_trades"] = bool(net > 0 and top / net > top_trade_share) or (net <= 0 < top)
    if by_asset:
        gains = {a: max(v, 0.0) for a, v in by_asset.items()}
        pos = sum(gains.values())
        share = {a: g / pos for a, g in gains.items()} if pos > 0 else {a: 0.0 for a in gains}
        out["asset_shares"] = share
        out["dominated_by_one_asset"] = bool(pos > 0 and max(share.values()) > asset_share)
    if regime_perf:
        out["dominated_by_one_regime"] = bool(regime_perf.get("trend", {}).get("dominated")
                                              or regime_perf.get("vol", {}).get("dominated"))
    return out


# -- 6. diversification --------------------------------------------------------

def _drawdowns(r: pd.Series) -> pd.Series:
    eq = (1 + r).cumprod()
    return eq / eq.cummax() - 1


def diversification(returns: dict[str, pd.Series], *, exposure: dict[str, pd.Series] | None = None,
                    family: dict[str, str] | None = None, asset: dict[str, str] | None = None,
                    regime_perf: dict[str, dict] | None = None, weights: dict[str, float] | None = None,
                    same_source_corr: float = 0.7, market: str = "*") -> dict:
    """Describes how different the return sources are. It is not evidence of profitability."""
    for s in returns.values():
        guard_research_dates(s.index, market)
    names = sorted(returns)
    df = pd.concat([returns[n].rename(n) for n in names], axis=1).fillna(0.0)
    corr = df.corr()
    dd = pd.concat([_drawdowns(df[n]).rename(n) for n in names], axis=1).corr()
    out = {"return_corr": corr.round(3).to_dict(), "drawdown_corr": dd.round(3).to_dict()}
    pairs = [(a, b, float(corr.loc[a, b])) for i, a in enumerate(names) for b in names[i + 1:]]
    out["same_source_pairs"] = [(a, b, round(c, 3)) for a, b, c in pairs if c > same_source_corr]
    if exposure:
        ex = pd.concat([(exposure[n].abs() > 0).rename(n) for n in names if n in exposure], axis=1).fillna(False)
        out["overlap"] = {a: {b: float((ex[a] & ex[b]).mean()) for b in ex} for a in ex}
    if regime_perf:
        keys = sorted({k for p in regime_perf.values() for k in p.get("trend", {}).get("by_regime", {})} - {"unknown"})
        prof = pd.DataFrame({n: [regime_perf[n]["trend"]["by_regime"].get(k, {}).get("sharpe_annual", 0.0)
                                 for k in keys] for n in names if n in regime_perf}, index=keys)
        out["regime_profile_corr"] = prof.corr().round(3).to_dict() if prof.shape[1] > 1 and len(keys) > 1 else {}
    w = weights or {n: 1 / len(names) for n in names}
    for label, groups in (("asset", asset), ("family", family)):
        if groups:
            share: dict[str, float] = {}
            for n in names:
                share[groups[n]] = share.get(groups[n], 0.0) + w[n]
            out[f"by_{label}"] = share
            out[f"hhi_{label}"] = float(sum(v * v for v in share.values()))
    eig = np.linalg.eigvalsh(corr.fillna(0).to_numpy()) if len(names) > 1 else np.array([1.0])
    eig = np.clip(eig, 0, None)
    out["effective_independent_sources"] = float(eig.sum() ** 2 / (eig ** 2).sum()) if eig.sum() > 0 else 0.0
    out["note"] = "diversification describes overlap between return sources; it is not evidence of profitability"
    return out
