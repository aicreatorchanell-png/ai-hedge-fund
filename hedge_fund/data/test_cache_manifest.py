"""Cache location rules and integrity manifests."""

from __future__ import annotations

import subprocess
import sys

import pytest

from hedge_fund import paths
from hedge_fund.data import cache_manifest as cm


def test_licensed_cache_may_not_live_inside_the_repository(monkeypatch, tmp_path):
    monkeypatch.setenv("AIHF_TIINGO_CACHE_DIR", str(paths.REPO_ROOT / "runs" / "tiingo"))
    with pytest.raises(paths.CacheLocationError):
        paths.cache_dir("tiingo")
    monkeypatch.setenv("AIHF_TIINGO_CACHE_DIR", str(tmp_path / "private" / "tiingo"))
    assert paths.cache_dir("tiingo") == (tmp_path / "private" / "tiingo").resolve()
    monkeypatch.setenv("AIHF_SEC_CACHE_DIR", str(tmp_path / "sec"))
    assert paths.cache_dir("edgar") != paths.cache_dir("tiingo")          # sources stay separable


def test_cache_root_is_configurable_at_import(tmp_path):
    code = "from hedge_fund.data.tiingo import DEFAULT_CACHE_DIR as T; from hedge_fund.data.edgar.client import " \
           "DEFAULT_CACHE_DIR as E; print(T); print(E)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={"AIHF_CACHE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
                         cwd=paths.REPO_ROOT).stdout.split()
    assert out == [str((tmp_path / "tiingo").resolve()), str((tmp_path / "edgar").resolve())]


def test_manifest_detects_changes_and_is_written_atomically(tmp_path):
    root = tmp_path / "tiingo"
    root.mkdir()
    (root / "AAA.json.gz").write_bytes(b"one")
    (root / "BBB.json.gz").write_bytes(b"two")
    (root / "partial.123.tmp").write_bytes(b"ignored")
    m = cm.write("tiingo", root)
    assert m["licence"] == "licensed" and m["n_files"] == 2 and not list(root.glob("MANIFEST.json.*.tmp"))
    assert cm.verify("tiingo", root)["ok"]
    (root / "AAA.json.gz").write_bytes(b"ONE")
    (root / "BBB.json.gz").unlink()
    (root / "CCC.json.gz").write_bytes(b"three")
    r = cm.verify("tiingo", root)
    assert (r["ok"], r["changed"], r["missing"], r["added"]) == (False, ["AAA.json.gz"], ["BBB.json.gz"],
                                                                 ["CCC.json.gz"])
    with pytest.raises(ValueError, match="source"):
        cm.verify("edgar", root)


def test_no_tiingo_raw_bars_are_tracked_in_git():
    tracked = subprocess.run(["git", "ls-files"], cwd=paths.REPO_ROOT, capture_output=True, text=True,
                             check=True).stdout.split("\n")
    assert not [t for t in tracked if "/tiingo/" in t and t.endswith(".json.gz")]
    assert not [t for t in tracked if t.endswith("MANIFEST.json") and "tiingo" in t]
