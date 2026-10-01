"""PART 3 — dry-run Tiingo request/symbol budget for quarterly PIT top-100/200/500.

Offline: reads only local cache files; makes no network request.

It replays the call structure of hedge_fund.universe.builder exactly where
the cached SEC data allows, and states the model where it does not:

  discovery (exact)  for each quarterly date D: the top `per_frame` (= 4 x top_n)
                     filers by public float of every cached quarterly frame
                     that ended before D, years D-2..D (UniverseBuilder.discover)
  screen (model)     every discovered company gets an SEC companyfacts read; the
                     pass rate is measured from the real top-10 build (cached
                     snapshot exclusion reasons)
  pricing (model)    the builder prices screened companies in float order until
                     `pool_size` (= 2 x top_n) are priced; each attempted symbol
                     not already stored costs exactly one Tiingo request
                     (TiingoClient._download: whole history in one GET), 404s
                     included. The failure rate per slot and the symbols tried
                     per company are measured from the same real build.
  turnover           the union over all quarterly dates of the companies the
                     pricing step reaches — measured from SEC frames, not assumed
  uncovered quarters dates whose discovery frames are not cached (2011-07..2015-10
                     needs CY2009-2013 frames; 2025-10..2026-07 needs CY2025Q3+)
                     are extrapolated from the covered span's new-names-per-quarter
                     rate and reported separately

Backtests afterwards cost 0 Tiingo requests with TiingoClient(offline=True)
(Phase A ran that way); a non-offline client refreshes each stale symbol once
per process (max_age 20h) — 1 request per symbol per run, not included here.
"""

from __future__ import annotations

import gzip
import json
import math
from datetime import date
from pathlib import Path

from hedge_fund.paths import cache_dir

OUT = Path(__file__).resolve().parent
EDGAR, TIINGO = cache_dir("edgar"), cache_dir("tiingo")
UNIVERSE_DIR = EDGAR.parent / "universe" / "e38c27babb40d16d"
FRAMES = EDGAR / "frames" / "dei" / "EntityPublicFloat" / "USD"
ABSURD = 1e13
DEV = ("2011-07-01", "2026-07-31")
FREE = {"requests_per_hour": 50, "requests_per_day": 1000, "unique_symbols_per_month": 500}
HEADROOM = 0.10


def load(p: Path):
    with gzip.open(p, "rt") as fh:
        return json.load(fh)


def quarterly_dates(start: str, end: str) -> list[str]:
    s, out, k = date.fromisoformat(start), [], 0
    while True:
        m = s.month - 1 + 3 * k
        d = date(s.year + m // 12, m % 12 + 1, 1).isoformat()
        if d > end:
            return out
        out.append(d)
        k += 1


def quarter_end(y, q):
    return {1: f"{y}-03-31", 2: f"{y}-06-30", 3: f"{y}-09-30", 4: f"{y}-12-31"}[q]


frames: dict[str, list[dict]] = {}
for p in sorted(FRAMES.glob("CY*.json.gz")):
    rows = load(p)["data"]
    rows = [r for r in rows if r.get("val") and 0 < r["val"] < ABSURD and r.get("cik")]
    rows.sort(key=lambda r: (-r["val"], r["cik"]))
    frames[p.name.split(".")[0]] = rows


def discover(d: str, per_frame: int) -> tuple[dict[int, float], bool]:
    """CIK -> latest frame float before d; complete=False if a needed frame is not cached."""
    year, found, latest_end, complete = date.fromisoformat(d).year, {}, {}, True
    for y in range(year - 2, year + 1):
        for q in (1, 2, 3, 4):
            end = quarter_end(y, q)
            if end >= d:
                continue
            key = f"CY{y}Q{q}I"
            if key not in frames:
                complete = False
                continue
            for r in frames[key][:per_frame]:
                cik = int(r["cik"])
                if end >= latest_end.get(cik, ""):
                    found[cik], latest_end[cik] = r["val"], end
    return found, complete


# -- rates measured from the real top-10 build ---------------------------------------
SCREEN = {"implausible_float", "stopped_filing", "insufficient_filings", "not_yet_filing",
          "excluded_security_type", "no_public_float", "predecessor", "successor_not_yet", "no_sec_facts"}
PRICE_FAIL = {"no_price", "float_market_cap_mismatch", "no_symbol", "no_shares", "recently_listed",
              "duplicate_symbol"}
cand = screened_out = slots = fails = 0
priced_ciks, attempted_ciks = set(), set()
for f in sorted(UNIVERSE_DIR.glob("*.json")):
    s = json.loads(f.read_text())
    cand += s["candidates"]
    for e in s["excluded"]:
        if e["reason"] in SCREEN:
            screened_out += 1
        if e["reason"] in PRICE_FAIL:
            fails += 1
            attempted_ciks.add(e["cik"])
        if e["reason"] == "below_top_n":
            priced_ciks.add(e["cik"])
    priced_ciks |= {m["cik"] for m in s["members"]}
    slots += len(s["members"]) + sum(e["reason"] == "below_top_n" for e in s["excluded"])
attempted_ciks |= priced_ciks
tiingo_files = sorted(p.name for p in TIINGO.glob("*.json.gz"))
symbols_from_build = len([t for t in tiingo_files if t != "SPY.json.gz"])
rates = {
    "screen_pass_rate": 1 - screened_out / cand,
    "price_fail_per_slot": fails / slots,
    "symbols_tried_per_attempted_company": symbols_from_build / len(attempted_ciks),
    "source": f"{UNIVERSE_DIR.name}: {len(list(UNIVERSE_DIR.glob('*.json')))} annual top-10 snapshots, "
              f"{cand} candidates, {slots} priced slots, {fails} pricing failures, "
              f"{len(attempted_ciks)} companies attempted, {symbols_from_build} Tiingo files",
    "caveat": "measured on megacaps; deeper (smaller) names fail pricing more often, so the true "
              "request count for top-200/500 is likely higher",
}

ticker_map = load(EDGAR / "company_tickers.json.gz")["data"]           # ticker -> cik (current SEC map)
cik_to_tickers: dict[int, list[str]] = {}
for t, c in ticker_map.items():
    cik_to_tickers.setdefault(int(c), []).append(t)
cached_symbols = {t.split(".json")[0] for t in tiingo_files}
avg_file = sum((TIINGO / t).stat().st_size for t in tiingo_files) / len(tiingo_files)
sample = load(TIINGO / "AAPL.json.gz")
bytes_per_row_raw = len(json.dumps(sample["rows"])) / len(sample["rows"])
# Tiingo's response carries 13 fields per row vs 8 stored (adj* fields): ~1.6x the stored JSON
avg_rows = sum(len(load(TIINGO / t)["rows"]) for t in tiingo_files) / len(tiingo_files)
resp_bytes = avg_rows * bytes_per_row_raw * 13 / 8

dates = quarterly_dates(*DEV)
results = {}
for top_n in (100, 200, 500):
    per_frame, pool = 4 * top_n, 2 * top_n
    reach = math.ceil(pool * (1 + rates["price_fail_per_slot"]) / rates["screen_pass_rate"])
    union, discovered_union, covered, uncovered, new_per_q = set(), set(), [], [], []
    for d in dates:
        found, complete = discover(d, per_frame)
        if not complete:
            uncovered.append(d)
            continue
        covered.append(d)
        discovered_union |= set(found)
        ranked = sorted(found, key=lambda c: (-found[c], c))[:reach]
        new_per_q.append(len(set(ranked) - union))
        union |= set(ranked)
    steady = new_per_q[1:]                                  # the first covered quarter seeds the set
    rate = sum(steady) / len(steady)
    extra = round(rate * len(uncovered))
    # screened-out companies never reach pricing (no Tiingo request); exclusions are mostly
    # persistent per company (float tagging, filer type), so scale the union by the pass rate
    companies = round((len(union) + extra) * rates["screen_pass_rate"])
    cached = sum(1 for c in union if any(t.replace(".", "-") in cached_symbols for t in cik_to_tickers.get(c, [])))
    symbols = math.ceil(companies * rates["symbols_tried_per_attempted_company"])
    cached_sym = cached                                     # one stored file per cached company
    missing = max(symbols - cached_sym, 0)
    requests = missing + 1                                  # + SPY benchmark/calendar refresh is 0 if cached;
    requests -= 1 if "SPY" in cached_symbols else 0
    limit_month = FREE["unique_symbols_per_month"] * (1 - HEADROOM)
    results[f"top{top_n}"] = {
        "per_frame": per_frame, "pool_size": pool, "companies_reached_per_quarter": reach,
        "quarterly_dates": len(dates), "dates_exact_from_cached_frames": len(covered),
        "dates_extrapolated": len(uncovered), "first_last_covered": [covered[0], covered[-1]],
        "unique_companies_reached_covered_span": len(union), "new_companies_per_quarter_mean": round(rate, 1),
        "unique_companies_priced_attempts_est": companies, "unique_symbols_needed_est": symbols,
        "already_cached": cached_sym, "missing_symbols_est": missing, "tiingo_requests_est": requests,
        "sec_companyfacts_reads_est": len(discovered_union) + round(rate * len(uncovered) * per_frame / reach),
        "bandwidth_download_mb_est": round(missing * resp_bytes / 1e6, 1),
        "bandwidth_stored_mb_est": round((cached_sym + missing) * avg_file / 1e6, 1),
        "free_tier_hours_at_50_per_hour": round(requests / FREE["requests_per_hour"] / (1 - HEADROOM), 1),
        "free_tier_days_at_1000_per_day": round(requests / FREE["requests_per_day"] / (1 - HEADROOM), 2),
        "months_at_500_unique_symbols": math.ceil(missing / limit_month),
        "exceeds_500_unique_symbols_in_one_month": missing > FREE["unique_symbols_per_month"],
        "free_tier_feasible_in_one_month_with_10pct_headroom": missing <= limit_month,
    }

report = {"dev_period": DEV, "free_tier": FREE, "headroom": HEADROOM, "rates_measured": rates,
          "tiingo_cache": {"files": len(tiingo_files), "avg_stored_kb": round(avg_file / 1e3, 1),
                           "avg_rows": round(avg_rows), "est_response_kb": round(resp_bytes / 1e3, 1)},
          "cached_frames": sorted(frames), "results": results,
          "note": "no request was made to produce this file"}
(OUT / "tiingo_budget.json").write_text(json.dumps(report, indent=1, sort_keys=True))
print(json.dumps({k: {kk: v[kk] for kk in ("unique_symbols_needed_est", "already_cached", "missing_symbols_est",
                                           "tiingo_requests_est", "months_at_500_unique_symbols",
                                           "free_tier_feasible_in_one_month_with_10pct_headroom")}
                  for k, v in results.items()}, indent=1))
print(json.dumps(rates, indent=1))
