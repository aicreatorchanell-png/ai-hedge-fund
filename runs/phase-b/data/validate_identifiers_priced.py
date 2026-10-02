"""Identifier validation with the acquired Tiingo prices (no strategy, no returns analysis).

Reads the private full schedule (members with market cap = PIT filed shares x the
symbol's raw close, and the PIT public float) and checks, per member-date:

  ratio      public float / market cap in [0.2, 5]. Float is the non-affiliate value at a
             past date (up to ~15 months stale), so it differs from market cap by price
             moves and insider holdings, but a symbol pointing at another company's price
             series is normally far outside this band. (The builder accepts [0.05, 20].)
  unique     no symbol held by two companies on one date
  changes    a company whose symbol changes between dates: listed for review with the
             ratio on both sides (renames such as BBT->TFC should keep a sane ratio)
  sample     the five (company, date) rows where SEC's current map contradicted the
             company's own filings: is the chosen symbol now priced with a sane ratio?

Rule fixed before running: PASS iff every member-date is inside the band and no symbol
is shared; flagged rows are written out for review.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from hedge_fund.paths import CACHE_DIR

HERE = Path(__file__).resolve().parent
full = json.loads((CACHE_DIR / "universe_runs" / "top100_schedule_full.json").read_text())
LO, HI = 0.2, 5.0

out_of_band, shared, by_cik = [], [], defaultdict(list)
for snap in full["snapshots"]:
    seen = {}
    for m in snap["members"]:
        ratio = m["public_float"] / m["market_cap"] if m["market_cap"] else float("inf")
        by_cik[m["cik"]].append((snap["as_of"], m["ticker"], round(ratio, 3), m["symbol_source"]))
        if not LO <= ratio <= HI:
            out_of_band.append({"as_of": snap["as_of"], "cik": m["cik"], "name": m["name"], "ticker": m["ticker"],
                                "ratio": round(ratio, 3), "source": m["symbol_source"]})
        if m["ticker"] in seen:
            shared.append((snap["as_of"], m["ticker"], seen[m["ticker"]], m["cik"]))
        seen[m["ticker"]] = m["cik"]
changes = {cik: rows for cik, rows in by_cik.items() if len({t for _, t, _, _ in rows}) > 1}

sample = json.loads((HERE / "symbol_verification_sample.json").read_text())["contradictions"]
sample_check = []
for c in sample:
    rows = [r for r in by_cik.get(c["cik"], []) if r[0] == c["as_of"]]
    sample_check.append({"as_of": c["as_of"], "name": c["name"], "sec_current_map": c["current_map_symbol"],
                         "own_filings": c["own_filing_symbols"],
                         "member_with": rows[0][1] if rows else None, "ratio": rows[0][2] if rows else None,
                         "status": ("member, ratio in band" if rows and LO <= rows[0][2] <= HI else
                                    "member, ratio OUT of band" if rows else "not a top-100 member that date")})

ratios = sorted(r for rows in by_cik.values() for _, _, r, _ in rows)
result = {
    "pass": not out_of_band and not shared, "band": [LO, HI],
    "member_dates": len(ratios), "ratio_quantiles": {q: ratios[int(q * (len(ratios) - 1))] for q in (0.01, 0.5, 0.99)}
    if ratios else {},
    "out_of_band": out_of_band, "shared_symbols": shared,
    "symbol_changes": {str(k): v for k, v in changes.items()}, "n_companies_with_symbol_change": len(changes),
    "sample_contradictions_now": sample_check,
}
(HERE / "identifier_validation_priced.json").write_text(json.dumps(result, indent=1))
print(json.dumps({k: v for k, v in result.items() if k not in ("symbol_changes",)}, indent=1)[:4000])
