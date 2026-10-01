"""Where user data lives: ~/.hedge-fund/.

Everything the user owns — mandates, run/backtest receipts, API caches, and
the .env key file — lives under one home directory, outside the package. The
package directory stays read-only code, so a pipx install behaves exactly
like a checkout.

Textual-free and import-light on purpose: every layer (CLI, TUI, caches)
anchors its paths here, and nothing here may import them back.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

USER_DIR = Path.home() / ".hedge-fund"
MANDATES_DIR = USER_DIR / "mandates"
# Data caches. AIHF_CACHE_DIR moves the whole cache root (e.g. to a private
# persistent volume); AIHF_TIINGO_CACHE_DIR / AIHF_SEC_CACHE_DIR move one source
# so licensed vendor data and public SEC filings can live apart.
CACHE_DIR = Path(os.environ.get("AIHF_CACHE_DIR") or USER_DIR / "cache").expanduser()
REPO_ROOT = Path(__file__).resolve().parents[1]

# source -> (env override, subdirectory, licence class)
CACHE_SOURCES = {
    "tiingo": ("AIHF_TIINGO_CACHE_DIR", "tiingo", "licensed"),     # Tiingo terms: no redistribution
    "edgar": ("AIHF_SEC_CACHE_DIR", "edgar", "public"),             # SEC EDGAR public filings
}


class CacheLocationError(ValueError):
    pass


def cache_dir(source: str) -> Path:
    """The cache directory for *source*; licensed data may never sit inside the git checkout."""
    env, sub, licence = CACHE_SOURCES[source]
    path = Path(os.environ.get(env) or CACHE_DIR / sub).expanduser().resolve()
    if licence == "licensed" and (path == REPO_ROOT or REPO_ROOT in path.parents):
        raise CacheLocationError(f"{source} cache {path} is inside the repository; licensed data must stay out of git")
    return path
ENV_PATH = USER_DIR / ".env"

# The example mandate ships inside the package; it is copied out (never read
# in place) so users edit their copy, not the install.
EXAMPLE_MANDATE = Path(__file__).resolve().parent / "fund" / "example.yaml"


def ensure_mandates_dir() -> Path:
    """Create the mandates dir on first use, seeded with the example."""
    if not MANDATES_DIR.exists():
        MANDATES_DIR.mkdir(parents=True)
        shutil.copy(EXAMPLE_MANDATE, MANDATES_DIR / "example.yaml")
    return MANDATES_DIR
