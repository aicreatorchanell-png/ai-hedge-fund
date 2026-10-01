"""PART 4 — point-in-time data audit of the five candidates (no backtest, no download).

Measures, on every SEC companyfacts file in the local cache (offline):
  - reporting lag: filing date minus period end, for 10-Q and 10-K
  - restatement frequency: how often a later filing reports a different value
    for an already-reported period (why as-filed matters)
  - gross-profitability inputs: GrossProfit tag, or revenue and cost of revenue,
    plus total assets, available as of a date
  - share-count measurement: dei:EntityCommonStockSharesOutstanding per filing;
    filings carrying more than one value (multi-class, undimensioned) are ambiguous
and reads the code paths that would feed each candidate.
"""

from __future__ import annotations

import gzip
import json
import statistics
from collections import Counter
from pathlib import Path

from hedge_fund.data.edgar.concepts import CONCEPTS, COVER_SHARES_TAG
from hedge_fund.data.edgar.facts import FactStore
from hedge_fund.paths import cache_dir

OUT = Path(__file__).resolve().parent
EDGAR = cache_dir("edgar")


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


lags = {"10-Q": [], "10-K": []}
restated_periods = total_periods = 0
gp_direct = gp_derived = gp_none = assets_ok = 0
multi_class = single_class = no_cover = 0
companies = 0
files = sorted((EDGAR / "companyfacts").glob("*.json.gz"))
tags = {c: set(CONCEPTS[c].tags) for c in ("revenue", "cost_of_revenue", "gross_profit", "total_assets")}
for p in files:
    with gzip.open(p, "rt") as fh:
        doc = json.load(fh)
    store = FactStore.from_companyfacts(doc.get("data", doc))
    if not store.facts:
        continue
    companies += 1
    for f in store.filings():
        if f.form in lags:
            from datetime import date
            lags[f.form].append((date.fromisoformat(f.filed) - date.fromisoformat(f.report_period)).days)
    # restatements: same tag+period reported with different values across filings
    seen: dict[tuple, set] = {}
    for f in store.facts:
        if f.tag in tags["revenue"] | tags["total_assets"]:
            seen.setdefault((f.tag, f.start, f.end), set()).add(round(f.val))
    total_periods += len(seen)
    restated_periods += sum(len(v) > 1 for v in seen.values())
    view = store.view("2020-06-30")
    filings = [x for x in store.filings() if x.filed <= "2020-06-30"]
    if filings:
        end = filings[-1].report_period
        gp = view.ttm("gross_profit", end)
        rev, cogs = view.ttm("revenue", end), view.ttm("cost_of_revenue", end)
        gp_direct += gp is not None
        gp_derived += gp is None and rev is not None and cogs is not None
        gp_none += gp is None and (rev is None or cogs is None)
        assets_ok += view.instant("total_assets", end) is not None
    per_accn = Counter(f.accn for f in store.facts if f.tag == COVER_SHARES_TAG)
    if not per_accn:
        no_cover += 1
    elif max(per_accn.values()) > 1:
        multi_class += 1
    else:
        single_class += 1

with_filings_2020 = gp_direct + gp_derived + gp_none
audit = {
    "sample": {"companyfacts_files": len(files), "companies_with_facts": companies,
               "note": "the cache holds the companies screened by the Phase A top-10 universe build "
                       "(large caps); coverage for smaller names must be re-measured on the pilot universe"},
    "reporting_lag_days": {form: {"n": len(v), "median": statistics.median(v) if v else None,
                                  "p90": pct(v, 0.9), "max": max(v) if v else None} for form, v in lags.items()},
    "restatement": {"tag_periods": total_periods, "periods_with_more_than_one_reported_value": restated_periods,
                    "share": round(restated_periods / total_periods, 4) if total_periods else None},
    "gross_profitability_inputs_as_of_2020_06_30": {
        "companies_with_a_filing": with_filings_2020, "gross_profit_tag": gp_direct,
        "derived_revenue_minus_cost_of_revenue": gp_derived, "not_available": gp_none,
        "total_assets_available": assets_ok},
    "share_count_measurement": {"single_value_per_filing": single_class,
                                "multiple_values_per_filing_ambiguous": multi_class, "no_cover_shares": no_cover},
}
(OUT / "candidate_data_measurements.json").write_text(json.dumps(audit, indent=1))
print(json.dumps(audit, indent=1))
