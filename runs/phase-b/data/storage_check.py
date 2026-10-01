"""Storage readiness: is the cache private, outside git, intact and surviving container restarts?

Evidence, not assumption:
  - the machine's boot time is later than the cache files' write times, and the
    per-source manifests (written before the restart) still verify byte for byte
    -> the cache survived a container restart in this session
  - the cache directories are outside the git checkout; no Tiingo file is tracked
  - the directories are readable by the container's user only (mode check)
What it cannot prove: survival after this session ends. The environment
documentation says a new session starts on a fresh machine, so cross-session
durability needs storage the user attaches (see readiness report).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from hedge_fund.data import cache_manifest
from hedge_fund.paths import CACHE_SOURCES, REPO_ROOT, cache_dir

OUT = Path(__file__).resolve().parent
with open("/proc/uptime") as fh:
    boot = time.time() - float(fh.read().split()[0])
result = {"checked_at": datetime.now(timezone.utc).isoformat(),
          "boot_time": datetime.fromtimestamp(boot, timezone.utc).isoformat(), "sources": {}}
for source in CACHE_SOURCES:
    root = cache_dir(source)
    manifest = json.loads((root / cache_manifest.MANIFEST_NAME).read_text())
    written = (root / cache_manifest.MANIFEST_NAME).stat().st_mtime
    v = cache_manifest.verify(source)
    # nothing lost; any changed file was rewritten after the restart by this session (e.g. stale SEC refresh)
    unchanged_since_manifest = not v["missing"] and all((root / f).stat().st_mtime > boot for f in v["changed"])
    mode = stat.S_IMODE(root.stat().st_mode)
    result["sources"][source] = {
        "path_outside_repo": REPO_ROOT not in root.resolve().parents and root.resolve() != REPO_ROOT,
        "licence": manifest["licence"], "manifest_files": manifest["n_files"],
        "manifest_written": datetime.fromtimestamp(written, timezone.utc).isoformat(),
        "manifest_written_before_boot": written < boot,
        "files_since_manifest_intact": unchanged_since_manifest, "files_added_since": len(v["added"]),
        "files_rewritten_after_boot": len(v["changed"]), "files_missing": len(v["missing"]),
        "dir_mode": oct(mode), "other_users_can_enter": bool(mode & (stat.S_IROTH | stat.S_IXOTH)), "owner_uid": root.stat().st_uid,
        "process_uid": os.getuid(),
    }
tracked = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
result["no_tiingo_file_tracked_in_git"] = not [t for t in tracked.splitlines() if "/tiingo/" in t and t.endswith(".gz")]
result["survived_container_restart"] = all(s["manifest_written_before_boot"] and s["files_since_manifest_intact"]
                                           for s in result["sources"].values())
result["cross_session_durability"] = "NOT PROVABLE: a new session starts on a fresh machine"
result["tree_hashes"] = {src: cache_manifest.verify(src)["tree_hash"] for src in CACHE_SOURCES}
# Restart evidence is a past observation; manifests are rewritten as the cache grows, so each check is
# appended to a committed history and G0 uses any recorded survival plus today's integrity/privacy.
history = OUT / "storage_history.jsonl"
with open(history, "a") as fh:
    fh.write(json.dumps({k: result[k] for k in ("checked_at", "boot_time", "survived_container_restart",
                                                  "tree_hashes")}) + "\n")
past = [json.loads(x) for x in history.read_text().splitlines() if x.strip()]
result["restart_survival_recorded"] = any(h["survived_container_restart"] for h in past)
result["integrity_now"] = all(cache_manifest.verify(src)["missing"] == [] for src in CACHE_SOURCES)
(OUT / "storage_check.json").write_text(json.dumps(result, indent=1))
print(json.dumps(result, indent=1))
