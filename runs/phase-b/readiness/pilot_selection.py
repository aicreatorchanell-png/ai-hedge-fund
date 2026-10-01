"""PART 7 — mechanical pilot-universe selection (no strategy result is read or exists).

Rules fixed here, before evaluation; the largest universe passing ALL wins:

  G1 reliable PIT membership   a survivorship-aware schedule can be built for the
                               whole development period from available data:
                               every quarterly date's discovery frames are cached
                               AND the top-N schedule has been built
  G2 delisting coverage        disappearances quantified (delisting_audit.json) and
                               the scenario overstatement at a 25% performance-
                               related share is <= 1.0% a year
  G3 candidate data quality    at least one candidate is DATA_READY for the universe
  G4 Tiingo free tier          missing symbols <= 90% of 500 unique symbols/month
                               (one month, 10% headroom) and requests <= 90% of 1000/day
                               spread over at most 2 days
  G5 runtime                   estimated full pilot <= 12 hours
  G0 persistent private storage for the licensed cache (Part 2) — required for
                               any universe that needs new downloads
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
budget = json.loads((HERE / "tiingo_budget.json").read_text())["results"]
delist = json.loads((HERE / "delisting_audit.json").read_text())
SECONDS_PER_100_NAMES_PER_YEAR = 4.5        # measured: 100 names, 2 years, monthly tsmom, 9.0s (no profiler)
DEV_YEARS = 15.1
RUNS_PER_PILOT = 40                          # 8 DATA_READY parameter sets x (dev + 3 cost stresses + EUR 200)
PERSISTENT_STORAGE = False                   # storage_audit: none in this environment
SCHEDULES_BUILT = {10}                       # only the Phase A/Buffett top-10 annual schedule exists
DATA_READY = ["H-RESMOM", "H-LOWVOL", "H-GPROF", "H-ISSUE"]   # candidate_data_audit; H-PEAD not ready

rows = {}
for key, n in (("top100", 100), ("top200", 200), ("top500", 500)):
    b = budget[key]
    runtime_h = SECONDS_PER_100_NAMES_PER_YEAR * n / 100 * DEV_YEARS * RUNS_PER_PILOT / 3600
    g = {
        "G0_persistent_storage": PERSISTENT_STORAGE or b["missing_symbols_est"] == 0,
        "G1_pit_membership": b["dates_extrapolated"] == 0 and n in SCHEDULES_BUILT,
        "G2_delisting_quantified_and_bounded":
            delist["bias_scenarios_annual_return_overstatement_equal_weight"][key]["perf_share_25pct"] <= 0.01,
        "G3_candidate_data": bool(DATA_READY),
        "G4_free_tier_headroom": b["missing_symbols_est"] <= 450 and b["tiingo_requests_est"] <= 2 * 900,
        "G5_runtime": runtime_h <= 12,
    }
    rows[key] = {"gates": g, "passes_all": all(g.values()), "runtime_hours_est": round(runtime_h, 2),
                 "missing_symbols_est": b["missing_symbols_est"], "requests_est": b["tiingo_requests_est"]}

passing = [k for k, r in rows.items() if r["passes_all"]]
selection = {
    "rules": __doc__, "evaluation": rows,
    "selected": passing[-1] if passing else None,
    "decision": ("PILOT_UNIVERSE_SELECTED" if passing else
                 "NO_UNIVERSE_QUALIFIES — stop before freezing and before any Phase B backtest"),
    "blocking_gates": sorted({g for r in rows.values() for g, ok in r["gates"].items() if not ok}),
}
(HERE / "pilot_selection.json").write_text(json.dumps(selection, indent=1))
print(json.dumps({k: (r["passes_all"], [g for g, ok in r["gates"].items() if not ok]) for k, r in rows.items()},
                 indent=1))
print(selection["decision"])
