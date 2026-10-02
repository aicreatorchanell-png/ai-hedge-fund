"""Completeness of the PIT top-100: how many of the largest companies could not be priced?

For each date, the top 100 screened companies by point-in-time public float (a proxy
for the top 100 by market cap that needs no price) are joined with the built
schedule: each is a member, priced but below the top 100, or unpriced with a reason.
Unpriced names among the largest are a survivorship gap: the universe would silently
lack them. Causes:

  vendor_history_unavailable  Tiingo lists the company's old ticker, but the ticker was
                              reused and the API serves only the newer listing
  not_listed_at_vendor        no Tiingo listing of the ticker spans the date
  no_symbol                   no symbol from SEC data (history, current map, own filings)
  price_download_deferred     priceable, not yet downloaded (budget)
  duplicate_symbol            lost a ticker conflict to another company
  other                       float/market-cap mismatch, no shares, recently listed, ...

Rule fixed before running: PASS iff on every date at most 3 of the top 100 by float are
unpriced for reasons other than price_download_deferred, and no download is deferred.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from hedge_fund.data.edgar import EdgarClient
from hedge_fund.data.tiingo import TiingoClient
from hedge_fund.paths import CACHE_DIR
from hedge_fund.universe.builder import UniverseBuilder
from hedge_fund.universe.models import UniverseConfig

HERE = Path(__file__).resolve().parent
full = json.loads((CACHE_DIR / "universe_runs" / "top100_schedule_full.json").read_text())
MAX_GAP = 3

per_date, causes, names = [], Counter(), Counter()
with TiingoClient(offline=True) as t, EdgarClient(offline=True, price_source=t, max_age_hours=None) as e:
    b = UniverseBuilder(e, t, UniverseConfig(top_n=100))
    for snap in full["snapshots"]:
        _, _, screened = b.screened(snap["as_of"])
        top = screened[:100]
        members = {m["cik"] for m in snap["members"]}
        reason = {x["cik"]: x["reason"] for x in snap["excluded"]}
        gap, deferred = [], 0
        for c in top:
            if c.cik in members or reason.get(c.cik) == "below_top_n":
                continue
            r = reason.get(c.cik, "other")
            r = r if r in ("vendor_history_unavailable", "not_listed_at_vendor", "no_symbol",
                           "price_download_deferred", "duplicate_symbol") else "other"
            if r == "price_download_deferred":
                deferred += 1
                continue
            gap.append((c.cik, c.name, r))
            causes[r] += 1
            names[(c.cik, c.name, r)] += 1
        per_date.append({"as_of": snap["as_of"], "unpriced_in_top100_by_float": len(gap), "deferred": deferred,
                         "unpriced": gap})
worst = max(per_date, key=lambda x: x["unpriced_in_top100_by_float"])
result = {
    "pass": all(d["unpriced_in_top100_by_float"] <= MAX_GAP and d["deferred"] == 0 for d in per_date),
    "max_gap_allowed": MAX_GAP,
    "gap_per_date": {d["as_of"]: d["unpriced_in_top100_by_float"] for d in per_date},
    "deferred_per_date": {d["as_of"]: d["deferred"] for d in per_date},
    "causes_total_member_dates": dict(causes),
    "most_frequent": [{"cik": k[0], "name": k[1], "cause": k[2], "dates": n} for k, n in names.most_common(30)],
    "worst_date": worst,
}
(HERE / "top100_completeness.json").write_text(json.dumps(result, indent=1, default=str))
g = list(result["gap_per_date"].values())
print(json.dumps({"pass": result["pass"], "gap_min_median_max": [min(g), sorted(g)[len(g) // 2], max(g)],
                  "deferred_total": sum(result["deferred_per_date"].values()), "causes": result["causes_total_member_dates"]},
                 indent=1))
for x in result["most_frequent"][:20]:
    print(x)
