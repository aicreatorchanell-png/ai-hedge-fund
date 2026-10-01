"""PART 5 — delisting / survivorship audit (offline, no download, no invented returns).

For each cached quarterly SEC public-float frame, take the top-N filers (the
universe's discovery proxy) and ask whether each company ever appears again
in any later cached frame. A company that disappears from every later frame
stopped filing periodic reports: acquired, taken private, bankrupt, or
deregistered — i.e. it left the listed universe.

Also measured: which disappeared companies have a stored Tiingo history, its
last bar (= the session the backtester would liquidate at, at that close),
and which have a curated outcome in hedge_fund/data/security_events.csv.

No delisting return is estimated from data we do not have. The bias table is
a *scenario* bound: if a share s of disappearances were performance-related
and their true delisting return were r (Shumway 1997 reports about -30% on
average for performance-related NYSE/AMEX delistings), the backtest that
liquidates at the last close overstates returns by about s * |r| per
disappearing holding.
"""

from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

from hedge_fund.paths import cache_dir
from hedge_fund.validation.holdout_guard import visible

OUT = Path(__file__).resolve().parent
EDGAR, TIINGO = cache_dir("edgar"), cache_dir("tiingo")
FRAMES = EDGAR / "frames" / "dei" / "EntityPublicFloat" / "USD"
EVENTS = Path(__file__).resolve().parents[3] / "hedge_fund" / "data" / "security_events.csv"


def load(p):
    with gzip.open(p, "rt") as fh:
        doc = json.load(fh)
    if isinstance(doc, dict) and "rows" in doc:                  # Tiingo: never look inside a sealed window
        doc["rows"] = [r for r in doc["rows"] if visible(r["date"])]
    return doc


keys = sorted(p.name.split(".")[0] for p in FRAMES.glob("CY*.json.gz"))
ranked = {}
for k in keys:
    rows = [r for r in load(FRAMES / f"{k}.json.gz")["data"] if r.get("val") and 0 < r["val"] < 1e13 and r.get("cik")]
    rows.sort(key=lambda r: (-r["val"], r["cik"]))
    ranked[k] = rows
present = {k: {int(r["cik"]) for r in ranked[k]} for k in keys}

ticker_map = load(EDGAR / "company_tickers.json.gz")["data"]
cik_tickers: dict[int, list[str]] = {}
for t, c in ticker_map.items():
    cik_tickers.setdefault(int(c), []).append(t)
stored = {p.name.split(".json")[0]: p for p in TIINGO.glob("*.json.gz")}
curated = {r["ticker"]: r for r in csv.DictReader(open(EVENTS))}

# Universe proxy = the builder's discovery: at each quarterly date D, every filer in the top
# `per_frame` (4 x top_n) of each frame ending before D (years D-2..D), ranked by its latest
# float; the top_n are the members. Single frames are fiscal-year cohorts, so they are never
# ranked alone. A member "disappears" when it is absent from every one of the last TAIL frames
# (no public float filed in the cache's final year): it stopped filing.
TAIL = 4
QE = {1: "-03-31", 2: "-06-30", 3: "-09-30", 4: "-12-31"}


def quarterly(start, end):
    out, y, m = [], int(start[:4]), int(start[5:7])
    while f"{y:04d}-{m:02d}-01" <= end:
        out.append(f"{y:04d}-{m:02d}-01")
        m += 3
        if m > 12:
            y, m = y + 1, m - 12
    return out


def proxy_members(d, top_n):
    year, latest = int(d[:4]), {}
    for y in range(year - 2, year + 1):
        for q in (1, 2, 3, 4):
            k, end = f"CY{y}Q{q}I", f"{y}{QE[q]}"
            if end >= d or k not in ranked:
                continue
            for r in ranked[k][:4 * top_n]:
                cik = int(r["cik"])
                if end >= latest.get(cik, ("", 0, ""))[0]:
                    latest[cik] = (end, r["val"], r.get("entityName"))
    top = sorted(latest.items(), key=lambda kv: (-kv[1][1], kv[0]))[:top_n]
    return {cik: v[2] for cik, v in top}


tail_present = set().union(*(present[k] for k in keys[-TAIL:]))
dates = [d for d in quarterly("2016-01-01", "2024-07-01")]          # discovery fully cached, tail kept clear
results = {}
for top_n in (100, 200, 500):
    members, gone = {}, {}
    for d in dates:
        for cik, name in proxy_members(d, top_n).items():
            members.setdefault(cik, (name, d))
    for cik, (name, first) in members.items():
        if cik not in tail_present:
            last_seen = max((x for x in keys if cik in present[x]), default=None)
            gone[cik] = {"name": name, "first_member_date": first, "last_frame": last_seen}
    with_quotes = in_current_map = curated_hits = 0
    for cik, g in gone.items():
        tickers = [t.replace(".", "-") for t in cik_tickers.get(cik, [])]
        g["current_sec_tickers"] = tickers
        in_current_map += bool(tickers)
        hist = [stored[t] for t in tickers if t in stored]
        if hist:
            with_quotes += 1
            g["tiingo_last_bar"] = load(hist[0])["rows"][-1]["date"]
        curated_hits += any(t in curated for t in tickers)
    years = len(dates) / 4
    results[f"top{top_n}"] = {
        "quarterly_dates": len(dates), "span": [dates[0], dates[-1]],
        "distinct_members": len(members), "disappeared": len(gone),
        "disappeared_per_year": round(len(gone) / years, 1),
        "annual_disappearance_rate_of_members": round(len(gone) / years / top_n, 4),
        "disappeared_still_in_current_sec_ticker_map": in_current_map,
        "disappeared_with_stored_tiingo_history": with_quotes,
        "disappeared_with_curated_outcome": curated_hits,
        "disappeared_without_any_known_outcome": len(gone) - curated_hits,
        "would_be_liquidated_at_last_close": len(gone) - curated_hits,
        "examples": dict(sorted(gone.items(), key=lambda kv: str(kv[1]["last_frame"]))[:12]),
    }

scenarios = {}
for k, r in results.items():
    rate = r["annual_disappearance_rate_of_members"]
    scenarios[k] = {f"perf_share_{int(s * 100)}pct": round(rate * s * 0.30, 5) for s in (0.1, 0.25, 0.5)}
audit = {"frames": [keys[0], keys[-1]], "tail_rule_frames": TAIL, "results": results,
         "bias_scenarios_annual_return_overstatement_equal_weight": scenarios,
         "curated_security_events": len(curated),
         "note": "scenario bounds only; no delisting return was estimated or invented"}
(OUT / "delisting_audit.json").write_text(json.dumps(audit, indent=1, default=str))
print(json.dumps({k: {kk: v for kk, v in r.items() if kk != "examples"} for k, r in results.items()}, indent=1))
print(json.dumps(scenarios, indent=1))
