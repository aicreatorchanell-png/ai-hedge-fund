"""Identifier validation with the acquired Tiingo prices (no strategy, no returns analysis).

Reads the private full schedule (members with market cap = PIT filed shares x the
symbol's raw close, and the PIT public float) and checks, per member-date:

  ratio      public float / market cap *at the float's own measurement date*, in [0.2, 5].
             The filed float values the non-affiliate shares at a past date (up to ~15
             months before the reconstitution date); the market cap at the reconstitution
             date is rolled back to that date with the same symbol's split-adjusted
             closes, so a genuine price move (Tesla 2020) cancels out while a symbol that
             points at another company's series does not. Raw ratios are reported too.
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

from hedge_fund.data.edgar import EdgarClient  # noqa: E402
from hedge_fund.data.tiingo import TiingoClient  # noqa: E402

tiingo = TiingoClient(offline=True)
edgar = EdgarClient(offline=True, price_source=tiingo, max_age_hours=None)
_float_end: dict[tuple[int, str], str | None] = {}


def float_end(cik: int, as_of: str, filed: str) -> str | None:
    key = (cik, as_of)
    if key not in _float_end:
        store = edgar.store_for_cik(cik)
        fl = [f for f in store.public_floats(as_of) if f.filed == filed] if store else []
        _float_end[key] = max(f.end for f in fl) if fl else None
    return _float_end[key]


def adj_close(ticker: str, day: str) -> float | None:
    from datetime import date, timedelta
    start = (date.fromisoformat(day) - timedelta(days=10)).isoformat()
    bars = [b for b in tiingo.get_prices(ticker, start, day) if b.time[:10] <= day]
    return bars[-1].close if bars else None


out_of_band, shared, by_cik, raw_ratios = [], [], defaultdict(list), []
for snap in full["snapshots"]:
    seen = {}
    for m in snap["members"]:
        raw = m["public_float"] / m["market_cap"] if m["market_cap"] else float("inf")
        raw_ratios.append(raw)
        end = float_end(m["cik"], snap["as_of"], m["float_filed"])
        then, now = (adj_close(m["ticker"], end) if end else None), adj_close(m["ticker"], m["price_date"])
        ratio = m["public_float"] / (m["market_cap"] * then / now) if then and now else raw
        by_cik[m["cik"]].append((snap["as_of"], m["ticker"], round(ratio, 3), m["symbol_source"]))
        if not LO <= ratio <= HI:
            out_of_band.append({"as_of": snap["as_of"], "cik": m["cik"], "name": m["name"], "ticker": m["ticker"],
                                "ratio_at_float_date": round(ratio, 3), "raw_ratio": round(raw, 3),
                                "float_end": end, "source": m["symbol_source"]})
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
    "raw_ratio_quantiles": {q: sorted(raw_ratios)[int(q * (len(raw_ratios) - 1))] for q in (0.01, 0.5, 0.99)},
    "out_of_band": out_of_band, "shared_symbols": shared,
    "symbol_changes": {str(k): v for k, v in changes.items()}, "n_companies_with_symbol_change": len(changes),
    "sample_contradictions_now": sample_check,
}
(HERE / "identifier_validation_priced.json").write_text(json.dumps(result, indent=1))
print(json.dumps({k: v for k, v in result.items() if k not in ("symbol_changes",)}, indent=1)[:4000])
