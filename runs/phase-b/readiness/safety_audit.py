"""FINAL SAFETY AUDIT — each claim is checked from evidence, not asserted."""

from __future__ import annotations

import gzip
import json
import os
import subprocess
from pathlib import Path

from hedge_fund.brokers import adapters
from hedge_fund.data.policy import financial_datasets_allowed
from hedge_fund.paths import CACHE_DIR, cache_dir

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
MAIN_AT_START = "5d2c7ca2d02c6501692a58bb363dfab1916890ba"


def git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()


checks = {}
checks["main_unchanged"] = git("rev-parse", "origin/main") == MAIN_AT_START and git("rev-parse", "main") == MAIN_AT_START
checks["financial_datasets_disabled"] = (not financial_datasets_allowed()
                                         and not os.environ.get("FINANCIAL_DATASETS_API_KEY"))
fd_cache = CACHE_DIR / "data"
checks["no_financial_datasets_cache_writes_today"] = not any(
    p.stat().st_mtime > 1790812800 for p in fd_cache.rglob("*") if p.is_file()) if fd_cache.exists() else True
checks["live_trading_disabled"] = adapters.LIVE_TRADING_ENABLED is False
tiingo_last = max((json.load(gzip.open(p, "rt"))["rows"] or [{"date": ""}])[-1]["date"]
                  for p in cache_dir("tiingo").glob("*.json.gz"))
checks["no_holdout_bars_exist_locally"] = tiingo_last < "2026-10-01"
checks["phase_b_has_no_trials_or_results"] = (not list((REPO / "runs" / "phase-b").rglob("*.jsonl"))
                                              and not list((REPO / "runs" / "phase-b").rglob("results*.json")))
tracked = git("ls-files").splitlines()
checks["no_tiingo_raw_data_in_git"] = not [t for t in tracked if "/tiingo/" in t and t.endswith(".gz")]
checks["working_tree_clean"] = not [line for line in git("status", "--porcelain").splitlines()
                                     if not line.endswith("safety_audit.json")]   # its own output
audit = {"checks": checks, "all_pass": all(checks.values()), "tiingo_cache_last_bar": tiingo_last,
         "notes": {
             "no_holdout_bars_exist_locally": "embargo bars (2026-09-01..09-29) exist in the raw cache; research "
                                              "reads of them are fenced since 037a19d (raw client) and the "
                                              "readiness scripts drop them",
             "secrets": "environment variables were only ever reported PRESENT/ABSENT",
             "trials_pre_logged": "vacuous: no Phase B trial was started (NO_UNIVERSE_QUALIFIES)"}}
(HERE / "safety_audit.json").write_text(json.dumps(audit, indent=1))
print(json.dumps(audit, indent=1))
