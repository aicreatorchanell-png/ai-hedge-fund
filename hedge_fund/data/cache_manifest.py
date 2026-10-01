"""Integrity manifests for the local data caches (Tiingo licensed, SEC public).

    python -m hedge_fund.data.cache_manifest write   # hash every cached file, per source
    python -m hedge_fund.data.cache_manifest verify  # re-hash and compare

Each source keeps its own `MANIFEST.json` inside its own cache directory
(paths.cache_dir), so licensed vendor data and public filings stay logically
separate. A manifest records the schema version, the source and its licence
class, every file's size and sha256, and a tree hash over all of them — the
value a research manifest pins as its data hash. Manifests are written
atomically (temp file + rename). Nothing here uploads or copies data anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from hedge_fund.paths import CACHE_SOURCES, cache_dir

SCHEMA_VERSION = 1
MANIFEST_NAME = "MANIFEST.json"
LICENCE_NOTE = {
    "licensed": "Tiingo end-of-day data under the account's Tiingo terms; do not commit or redistribute",
    "public": "SEC EDGAR public filings",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(root: Path) -> dict[str, dict]:
    files = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != MANIFEST_NAME and not p.name.endswith(".tmp") and ".tmp" not in p.suffixes:
            files[p.relative_to(root).as_posix()] = {"bytes": p.stat().st_size, "sha256": _sha256(p)}
    return files


def tree_hash(files: dict[str, dict]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\0{files[rel]['sha256']}\n".encode())
    return h.hexdigest()


def build(source: str, root: Path | None = None) -> dict:
    root = Path(root) if root else cache_dir(source)
    licence = CACHE_SOURCES[source][2]
    files = scan(root) if root.exists() else {}
    return {"schema_version": SCHEMA_VERSION, "source": source, "licence": licence,
            "licence_note": LICENCE_NOTE[licence], "created_at": datetime.now(timezone.utc).isoformat(),
            "n_files": len(files), "bytes": sum(f["bytes"] for f in files.values()),
            "tree_hash": tree_hash(files), "files": files}


def write(source: str, root: Path | None = None) -> dict:
    root = Path(root) if root else cache_dir(source)
    manifest = build(source, root)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"{MANIFEST_NAME}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    os.replace(tmp, root / MANIFEST_NAME)
    return manifest


def verify(source: str, root: Path | None = None) -> dict:
    """{'ok', 'missing', 'changed', 'added', 'tree_hash'} against the stored manifest."""
    root = Path(root) if root else cache_dir(source)
    stored = json.loads((root / MANIFEST_NAME).read_text())
    if stored.get("schema_version") != SCHEMA_VERSION or stored.get("source") != source:
        raise ValueError(f"{root / MANIFEST_NAME}: wrong schema or source")
    now = scan(root)
    old = stored["files"]
    missing = sorted(set(old) - set(now))
    added = sorted(set(now) - set(old))
    changed = sorted(k for k in set(old) & set(now) if old[k]["sha256"] != now[k]["sha256"])
    return {"ok": not (missing or added or changed), "missing": missing, "changed": changed, "added": added,
            "tree_hash": tree_hash(now), "stored_tree_hash": stored["tree_hash"]}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "verify"
    for source in CACHE_SOURCES:
        if cmd == "write":
            m = write(source)
            print(f"{source}: {m['n_files']} files, {m['bytes']:,} bytes, tree {m['tree_hash'][:16]}")
        else:
            r = verify(source)
            print(f"{source}: {'OK' if r['ok'] else 'MISMATCH'} missing={len(r['missing'])} "
                  f"changed={len(r['changed'])} added={len(r['added'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
