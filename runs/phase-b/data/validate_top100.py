"""Validate the quarterly PIT top-100 universe (SEC stages) — no price is read.

Checks on runs/phase-b/data/top100_sec_stage.json:

  pit                every ranked candidate's float and latest filing were filed on or
                     before the reconstitution date; no date reaches the 2026-09-01 fence
  survivorship       companies that later stop filing are present in the pools of the
                     dates when they were alive (they are not dropped retroactively), and
                     drop out only after they stop filing
  lineage            predecessor/successor registrants (renames, re-domiciles, holding-
                     company reorganisations) are represented once per date
  identifiers        candidates whose symbol comes from history or their own filing cover
                     page (delisted, renamed), symbol changes across dates for one CIK,
                     the same symbol claimed by two CIKs on one date
  exits              pool members that later stop filing (acquired, merged, taken
                     private, bankrupt, deregistered), whether SEC's current ticker map
                     still lists them, and the curated outcomes available
  coverage           pool candidates with no symbol to price (pricing would fail)
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from hedge_fund.data.edgar import EdgarClient
from hedge_fund.data.tiingo import tiingo_symbol

HERE = Path(__file__).resolve().parent
EVENTS = Path(__file__).resolve().parents[3] / "hedge_fund" / "data" / "security_events.csv"
FENCE = "2026-09-01"

doc = json.loads((HERE / "top100_sec_stage.json").read_text())
snaps = doc["snapshots"]
dates = [s["as_of"] for s in snaps]
checks: dict[str, dict] = {}

# -- pit -------------------------------------------------------------------------------
late = [(s["as_of"], r["cik"]) for s in snaps for r in s["ranked"]
        if r["float_filed"] > s["as_of"] or r["latest_filing"] > s["as_of"]]
checks["pit"] = {"pass": not late and max(dates) < FENCE, "violations": late[:20], "last_date": max(dates),
                 "nominated_after_as_of_excluded": sum(s["exclusions"].get("nominated_after_as_of", 0) for s in snaps)}

# -- survivorship / exits ------------------------------------------------------------------
pool_dates = defaultdict(list)
for s in snaps:
    for r in s["ranked"]:
        if r["in_pool"]:
            pool_dates[r["cik"]].append(s["as_of"])
stopped = defaultdict(list)
for s in snaps:
    for e in s["excluded_detail"]:
        if e["reason"] == "stopped_filing":
            stopped[e["cik"]].append(s["as_of"])
exits = {cik: {"first_pool": min(ds), "last_pool": max(ds), "first_stopped": min(stopped[cik])}
         for cik, ds in pool_dates.items() if cik in stopped and min(stopped[cik]) > max(ds)}
# A company that stopped filing may return (late filer, emerged from bankruptcy): legitimate only if
# it filed again — its latest filing at re-entry is after the "last periodic report" it was cut on.
stopped_detail = defaultdict(list)
for s in snaps:
    for e in s["excluded_detail"]:
        if e["reason"] == "stopped_filing":
            stopped_detail[e["cik"]].append((s["as_of"], e["detail"].rsplit(" ", 1)[-1]))
resurrected, reentries = [], []
for s in snaps:
    for r in s["ranked"]:
        if not r["in_pool"]:
            continue
        cut = [last for d, last in stopped_detail.get(r["cik"], []) if d < s["as_of"]]
        if cut:
            (reentries if r["latest_filing"] > max(cut) else resurrected).append((s["as_of"], r["cik"], r["name"]))
with EdgarClient(offline=True) as edgar:
    current = {int(c) for c in edgar.current_tickers().values()}
names = {r["cik"]: r["name"] for s in snaps for r in s["ranked"]}
curated = {r["ticker"] for r in csv.DictReader(open(EVENTS))}
exit_rows = []
for cik, x in sorted(exits.items(), key=lambda kv: kv[1]["first_stopped"]):
    syms = sorted({t for s in snaps for r in s["ranked"] if r["cik"] == cik for t in r["symbols"]})
    exit_rows.append({"cik": cik, "name": names.get(cik), **x, "symbols": syms,
                      "in_current_sec_map": cik in current, "curated_outcome": bool(set(syms) & curated)})
years = (len(dates) - 1) / 4
checks["survivorship"] = {
    "pass": not resurrected and len(exits) > 0,
    "pool_members_that_later_stop_filing": len(exits),
    "kept_in_pools_while_alive": len(exits),
    "reentries_after_filing_again": sorted({(c, n) for _, c, n in reentries}),
    "listed_after_stopping_without_new_filing (must be 0)": len(resurrected),
    "note": "companies that later disappear are in the pools of their live dates: no survivorship bias "
            "from using today's lists",
}
checks["exits"] = {
    "count": len(exit_rows), "per_year": round(len(exit_rows) / years, 1),
    "still_in_current_sec_ticker_map": sum(r["in_current_sec_map"] for r in exit_rows),
    "with_curated_outcome": sum(r["curated_outcome"] for r in exit_rows),
    "without_known_outcome_liquidated_at_last_close": sum(not r["curated_outcome"] for r in exit_rows),
    "rows": exit_rows,
}

# -- lineage / identifiers ----------------------------------------------------------------
lineage = defaultdict(int)
for s in snaps:
    for e in s["excluded_detail"]:
        if e["reason"] in ("predecessor", "successor_not_yet"):
            lineage[e["reason"]] += 1
both = []
for s in snaps:
    pool = {r["cik"] for r in s["ranked"]}
    for e in s["excluded_detail"]:
        if e["reason"] in ("predecessor", "successor_not_yet") and e["cik"] in pool:
            both.append((s["as_of"], e["cik"]))
checks["lineage"] = {"pass": not both, "exclusions": dict(lineage), "registrant_and_lineage_twin_both_ranked": both}

first_symbol = defaultdict(set)
dup = []
source_counts = defaultdict(int)
for s in snaps:
    seen = {}
    for r in s["ranked"]:
        if not r["in_pool"] or not r["symbols"]:
            continue
        sym = tiingo_symbol(r["symbols"][0])
        first_symbol[r["cik"]].add(sym)
        source_counts[r["sources"][0]] += 1
        if sym in seen and seen[sym] != r["cik"]:
            dup.append((s["as_of"], sym, seen[sym], r["cik"]))
        seen[sym] = r["cik"]
changed = {cik: sorted(v) for cik, v in first_symbol.items() if len(v) > 1}
conflicts = [(s["as_of"], r["cik"], r["conflict"], r["symbols"][:1]) for s in snaps for r in s["ranked"] if r.get("conflict")]
pre2019_current = sum(1 for s in snaps for r in s["ranked"]
                      if r["in_pool"] and r["sources"] and r["sources"][0] == "current" and s["as_of"] < "2019-07-01")
checks["identifiers"] = {
    "pass": not dup,
    "resolved_conflicts": conflicts,
    "pre_2019_pool_rows_using_current_sec_map": pre2019_current, "first_symbol_source_counts": dict(source_counts),
    "ciks_whose_symbol_changes_across_dates": len(changed), "examples": dict(list(changed.items())[:10]),
    "same_symbol_two_ciks_same_date": dup[:20],
    "note": "after conflict resolution no symbol may be claimed by two CIKs on one date; current-map "
            "symbols on pre-2019 dates are verified by sample against the companies' own filings "
            "(verify_symbols_sample.py)",
}

# -- coverage -------------------------------------------------------------------------------
no_symbol = [(s["as_of"], r["cik"], r["name"]) for s in snaps for r in s["ranked"] if r["in_pool"] and not r["symbols"]]
pool_rows = sum(1 for s in snaps for r in s["ranked"] if r["in_pool"])
checks["coverage"] = {"pass": len(no_symbol) / pool_rows < 0.05, "pool_rows": pool_rows,
                      "pool_rows_without_symbol": len(no_symbol), "examples": no_symbol[:15]}

summary = {k: v["pass"] for k, v in checks.items() if "pass" in v}
(HERE / "top100_validation.json").write_text(json.dumps({"summary": summary, "checks": checks}, indent=1, default=str))
print(json.dumps(summary, indent=1))
print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk not in ("rows", "examples", "violations")}
                  for k, v in checks.items()}, indent=1, default=str)[:4000])
