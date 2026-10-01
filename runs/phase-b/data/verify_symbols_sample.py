"""Verify, by seeded random sample, that today's SEC ticker map gave the right symbol on old dates.

Population: top-100 pool rows (float rank <= 200) dated before 2019-07-01 whose first symbol
comes from SEC's *current* ticker map. For each sampled (date, company) the company's own
filings filed by that date (cover-page dei:TradingSymbol on the latest four periodic
filings, else the 10-K listing sentence) are read:

    verified      the current-map symbol is among the symbols the company itself stated
    contradicted  the company stated other symbols only (the current symbol was not its
                  ticker then: reassigned or renamed later)
    unverifiable  the company's filings state no symbol

Rule fixed before running: PASS iff no contradiction among >= 60 verifiable rows (then the
one-sided 95% upper bound on the contradiction rate is <= 4.9%). SEC public data only.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from hedge_fund.data.edgar import EdgarClient
from hedge_fund.data.tiingo import TiingoClient, tiingo_symbol
from hedge_fund.universe.builder import UniverseBuilder, _Candidate
from hedge_fund.universe.models import UniverseConfig

HERE = Path(__file__).resolve().parent
SEED, TARGET_VERIFIABLE, MAX_DRAWS = 20261001, 60, 200

snaps = json.loads((HERE / "top100_sec_stage.json").read_text())["snapshots"]
population = [(s["as_of"], r) for s in snaps if s["as_of"] < "2019-07-01" for r in s["ranked"]
              if r["in_pool"] and r["sources"] and r["sources"][0] == "current"]
rng = random.Random(SEED)
order = rng.sample(range(len(population)), min(MAX_DRAWS, len(population)))
rows, verifiable = [], 0
with TiingoClient(offline=True) as t, EdgarClient(price_source=t) as e:
    b = UniverseBuilder(e, t, UniverseConfig(top_n=100))
    for i in order:
        if verifiable >= TARGET_VERIFIABLE:
            break
        as_of, r = population[i]
        store = b._store(r["cik"])
        filings = [f for f in store.filings() if f.filed <= as_of] if store else []
        if not filings:
            continue
        cand = _Candidate(r["cik"], r["name"], store, filings[-1], r["public_float"], r["float_filed"])
        own = [tiingo_symbol(s) for s, _ in b._own_symbols(cand)]
        current = tiingo_symbol(r["symbols"][0])
        status = "unverifiable" if not own else ("verified" if current in own else "contradicted")
        verifiable += status != "unverifiable"
        rows.append({"as_of": as_of, "cik": r["cik"], "name": r["name"], "current_map_symbol": current,
                     "own_filing_symbols": own, "status": status})
counts = {k: sum(1 for x in rows if x["status"] == k) for k in ("verified", "contradicted", "unverifiable")}
n = counts["verified"] + counts["contradicted"]
upper = 1 - 0.05 ** (1 / n) if n and not counts["contradicted"] else None     # exact one-sided bound for 0/n
result = {"population_rows": len(population), "seed": SEED, "sampled": len(rows), "counts": counts,
          "upper95_contradiction_rate_if_zero": upper,
          "pass": n >= TARGET_VERIFIABLE and counts["contradicted"] == 0,
          "contradictions": [x for x in rows if x["status"] == "contradicted"], "rows": rows,
          "sec_requests": e.requests}
(HERE / "symbol_verification_sample.json").write_text(json.dumps(result, indent=1))
print(json.dumps({k: v for k, v in result.items() if k not in ("rows",)}, indent=1)[:3000])
