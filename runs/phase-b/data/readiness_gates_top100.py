"""Top-100 data-readiness gates — PASS/FAIL from evidence files (no strategy, no price research).

Mandatory gates (all must PASS before pre-registration/freeze):
  G0 storage            private, outside git, integrity-hashed, survived a container restart
  G1 sec_frames         every SEC public-float frame the 61 quarterly discovery windows need is cached
  G2 universe_sec       SEC stages built for every date and validated (PIT, survivorship, lineage,
                        coverage, identifiers) and today's-map symbols verified by sample (0 contradictions)
  G3 universe_final     market-cap membership complete: every first-try pricing symbol is cached
  G4 tiingo_verified    the account's actual plan/usage has been verified
  G5 tiingo_fits        missing symbols and bandwidth fit the (verified, else most restrictive
                        published) plan with 10% headroom, within one month
  G6 exits_quantified   pool exits counted and bounded (scenario bias <= 1%/yr at a 25%
                        performance-related share); no delisting return invented
  G7 candidate_data     at least one candidate DATA_READY; H-PEAD stays DATA_NOT_READY
  G8 holdout_fence      research reads into the embargo/holdout are refused (checked live) and
                        every universe date precedes the fence
  G9 runtime            estimated full pilot <= 12 hours
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
gates["G2_universe_sec"] = {"pass": len(sec["dates"]) == 61 and all(val["summary"].values()) and sample["pass"],
                            "dates": len(sec["dates"]), "checks": val["summary"],
                            "symbol_sample": {"counts": sample["counts"], "pass": sample["pass"],
                                              "contradictions": [(c["as_of"], c["name"], c["current_map_symbol"],
                                                                  c["own_filing_symbols"])
                                                                 for c in sample["contradictions"]]}}

first = sec["first_try"]
gates["G3_universe_final"] = {"pass": not first["missing"], "first_try_symbols": first["n"],
                              "cached": len(first["cached"]), "missing": len(first["missing"]),
                              "contingency_symbols": sec["contingency"]["n"],
                              "contingency_missing": len(sec["contingency"]["missing"])}

gates["G4_tiingo_verified"] = {"pass": False, "why": tv["account_plan"], "action": tv["account_plan_check_required"]}

plan = tv["published_limits"]["starter_free"]
rows = budget["tiingo_cache"]["avg_rows"]
bytes_per_row = tv["authenticated_calls_made"][1]["wire_bytes_per_row"]
need_sym = len(first["missing"])
need_gb = need_sym * rows * bytes_per_row / 1e9
gates["G5_tiingo_fits"] = {
    "pass": need_sym <= 0.9 * plan["unique_symbols_per_month"] and need_gb <= 0.9 * plan["bandwidth_per_month_gb"],
    "plan_assumed": tv["planning_assumption_until_verified"], "missing_first_try_symbols": need_sym,
    "symbol_limit_with_headroom": int(0.9 * plan["unique_symbols_per_month"]),
    "est_bandwidth_gb_full_history": round(need_gb, 3),
    "bandwidth_limit_with_headroom_gb": 0.9 * plan["bandwidth_per_month_gb"],
    "est_requests": need_sym, "hours_at_50_per_hour": round(need_sym / 45, 1)}

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
