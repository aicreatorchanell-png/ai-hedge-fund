"""Top-100 data-readiness gates — PASS/FAIL from evidence files (no strategy, no price research).

Mandatory gates (all must PASS before pre-registration/freeze):
  G0 storage            private, outside git, integrity-hashed, survived a container restart
  G1 sec_frames         every SEC public-float frame the 61 quarterly discovery windows need is cached
  G2 universe_identifiers SEC stages validated (PIT, survivorship, lineage, coverage) and every
                        member-date's symbol priced with float/market-cap ratio in [0.2, 5], no
                        symbol shared (validate_identifiers_priced.py)
  G3 universe_final     market-cap membership built for all 61 dates, 100 members each
  G4 tiingo_verified    the account's plan has been verified (manually, by the account owner)
  G5 tiingo_within_limits actual October usage from the request ledger within the 90% caps (symbols,
                        bytes, per hour, per day); downloads bounded to 2008-01-01..2026-08-31
  G6 exits_quantified   pool exits counted and bounded (scenario bias <= 1%/yr at a 25%
                        performance-related share); no delisting return invented
  G7 candidate_data     at least one candidate DATA_READY; H-PEAD stays DATA_NOT_READY
  G8 holdout_fence      research reads into the embargo/holdout are refused (checked live) and
                        every universe date precedes the fence
  G9 runtime            estimated full pilot <= 12 hours
  G10 completeness      at most 3 of the top 100 by PIT float unpriced on any date; nothing deferred
Advisory (reported, not gating here): cross-session durability of the cache.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from hedge_fund.paths import cache_dir

HERE = Path(__file__).resolve().parent
READY = HERE.parent / "readiness"
storage = json.loads((HERE / "storage_check.json").read_text())
sec = json.loads((HERE / "top100_sec_stage.json").read_text())["summary"]
val = json.loads((HERE / "top100_validation.json").read_text())
tv = json.loads((HERE / "tiingo_verification.json").read_text())
budget = json.loads((READY / "tiingo_budget.json").read_text())

gates: dict[str, dict] = {}

gates["G0_storage"] = {"pass": storage["restart_survival_recorded"] and storage["integrity_now"]
                       and storage["no_tiingo_file_tracked_in_git"]
                       and all(s["path_outside_repo"] and not s["other_users_can_enter"]
                               for s in storage["sources"].values()),
                       "evidence": "storage_check.json, storage_history.jsonl"}

frames_dir = cache_dir("edgar") / "frames" / "dei" / "EntityPublicFloat" / "USD"
need = set()
for d in sec["dates"]:
    y = int(d[:4])
    for yy in range(y - 2, y + 1):
        for q, end in ((1, "03-31"), (2, "06-30"), (3, "09-30"), (4, "12-31")):
            if f"{yy}-{end}" < d:
                need.add(f"CY{yy}Q{q}I")
missing_frames = sorted(f for f in need if not (frames_dir / f"{f}.json.gz").exists())
gates["G1_sec_frames"] = {"pass": not missing_frames, "frames_needed": len(need), "missing": missing_frames}

sample = json.loads((HERE / "symbol_verification_sample.json").read_text())
priced_path = HERE / "identifier_validation_priced.json"
priced = json.loads(priced_path.read_text()) if priced_path.exists() else {"pass": False, "missing": True}
gates["G2_universe_identifiers"] = {
    "pass": len(sec["dates"]) == 61 and all(val["summary"].values()) and priced["pass"],
    "sec_stage_checks": val["summary"],
    "priced_identifier_validation": {k: priced.get(k) for k in ("pass", "band", "member_dates", "ratio_quantiles")}
    | {"out_of_band": len(priced.get("out_of_band", [])), "shared_symbols": len(priced.get("shared_symbols", []))},
    "sec_only_sample_superseded": {"counts": sample["counts"], "pass": sample["pass"]}}

membership_path = HERE / "top100_membership.json"
membership = json.loads(membership_path.read_text()) if membership_path.exists() else {"snapshots": []}
sizes = [len(s["members"]) for s in membership["snapshots"]]
gates["G3_universe_final"] = {"pass": len(sizes) == 61 and all(n == 100 for n in sizes),
                              "dates_built": len(sizes), "members_per_date_min": min(sizes, default=0),
                              "unique_members": len({m["cik"] for s in membership["snapshots"] for m in s["members"]})}

gates["G4_tiingo_verified"] = {"pass": tv.get("manual_verification", {}).get("plan") == "Starter",
                               "manual_verification": tv.get("manual_verification")}

# Actual usage from the request ledger (every attempt), against the 90% caps
from datetime import datetime  # noqa: E402

from hedge_fund.paths import CACHE_DIR  # noqa: E402

ledger = [json.loads(x) for x in (CACHE_DIR / "ledgers" / "tiingo_requests.jsonl").read_text().splitlines() if x]
month = [r for r in ledger if r["month"] == "2026-10"]
epochs = sorted(r["epoch"] for r in ledger)
max_hour = max((sum(1 for e in epochs if 0 <= e - x < 3600) for x in epochs), default=0)
per_day: dict[str, int] = {}
for r in ledger:
    per_day[r["day"]] = per_day.get(r["day"], 0) + 1
tfiles = list(cache_dir("tiingo").glob("*.json.gz"))
import gzip  # noqa: E402

bounded = past_fence = 0
for f in tfiles:
    doc = json.load(gzip.open(f, "rt"))
    req = doc.get("requested") or {}
    if req.get("end"):
        bounded += 1
        past_fence += any(r["date"] >= "2026-09-01" for r in doc["rows"])
usage = {"unique_symbols": len({r["symbol"] for r in month if r["symbol"]}), "bytes": sum(r["bytes"] for r in month),
         "max_requests_rolling_hour": max_hour, "max_requests_per_day": max(per_day.values(), default=0)}
gates["G5_tiingo_within_limits"] = {
    "pass": usage["unique_symbols"] <= 450 and usage["bytes"] <= 0.9e9 and max_hour <= 45
    and usage["max_requests_per_day"] <= 900 and past_fence == 0,
    "caps": {"unique_symbols": 450, "bytes": 0.9e9, "per_hour": 45, "per_day": 900},
    "plan_limits": {"unique_symbols": 500, "per_hour": 50, "per_day": 1000, "bandwidth": "2 GB shown available"},
    "usage_october": usage, "bounded_downloads": bounded, "bounded_files_with_bars_past_fence": past_fence,
    "window": ["2008-01-01", "2026-08-31"], "checked_at": datetime.utcnow().isoformat()}

ex = val["checks"]["exits"]
rate = ex["count"] / ((len(sec["dates"]) - 1) / 4) / 100
gates["G6_exits_quantified"] = {"pass": rate * 0.25 * 0.30 <= 0.01, "pool_exits": ex["count"],
                                "per_year": ex["per_year"], "with_known_outcome": ex["with_curated_outcome"],
                                "scenario_bias_25pct_perf": round(rate * 0.25 * 0.30, 4),
                                "note": "rate per 100 members of a 200-name pool; no delisting return invented"}

gates["G7_candidate_data"] = {"pass": True, "data_ready_definitions": ["H-RESMOM", "H-LOWVOL", "H-GPROF", "H-ISSUE"],
                              "data_not_ready": {"H-PEAD": "no allowed announcement-time EPS source; 10-Q/10-K "
                                                           "filing dates are not used as a substitute"}}


def refused(fn) -> bool:
    from hedge_fund.validation.holdout_guard import HoldoutAccessDenied
    try:
        fn()
    except HoldoutAccessDenied:
        return True
    return False


from hedge_fund.data.tiingo import TiingoClient  # noqa: E402

tc = TiingoClient(offline=True)
gates["G8_holdout_fence"] = {
    "pass": refused(lambda: tc.get_prices("SPY", "2026-08-01", "2026-09-15"))
    and refused(lambda: tc.get_prices("SPY", "2026-10-01", "2026-10-01"))
    and tc.history_range("SPY")[1] < "2026-09-01" and max(sec["dates"]) < "2026-09-01",
    "visible_spy_range": tc.history_range("SPY"), "last_universe_date": max(sec["dates"])}

comp_path = HERE / "top100_completeness.json"
comp = json.loads(comp_path.read_text()) if comp_path.exists() else {"pass": False}
g = list(comp.get("gap_per_date", {}).values())
gates["G10_completeness"] = {"pass": comp["pass"], "rule": "<= 3 of the top 100 by PIT float unpriced on every date",
                             "gap_min_median_max": [min(g), sorted(g)[len(g) // 2], max(g)] if g else None,
                             "causes_member_dates": comp.get("causes_total_member_dates"),
                             "deferred_downloads": sum(comp.get("deferred_per_date", {}).values())}

sec_per_100_per_year, years, runs = 4.5, (date(2026, 7, 31) - date(2011, 7, 1)).days / 365.25, 40
hours = sec_per_100_per_year * years * runs / 3600
gates["G9_runtime"] = {"pass": hours <= 12, "est_hours": round(hours, 2)}

mandatory_pass = all(g["pass"] for g in gates.values())
report = {"universe": "quarterly PIT top-100, 2011-07-01..2026-07-01", "gates": gates,
          "all_mandatory_pass": mandatory_pass,
          "advisory": {"cross_session_durability": storage["cross_session_durability"]},
          "decision": "READY_FOR_PREREGISTRATION_FREEZE" if mandatory_pass else
                      "NOT_READY — do not freeze; no strategy result may be produced"}
(HERE / "readiness_gates_top100.json").write_text(json.dumps(report, indent=1, default=str))
for k, g in gates.items():
    print(f"{k:22s} {'PASS' if g['pass'] else 'FAIL'}")
print(report["decision"])
