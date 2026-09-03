"""Load repo-level environment variables once for all scripts and providers."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


@lru_cache(maxsize=1)
def load_repo_env() -> Path:
    """Load `.env` from the repo root. Safe to call multiple times."""
    load_dotenv(ENV_FILE, override=False)
    return ENV_FILE


def require_serpapi_key() -> str:
    load_repo_env()
    key = os.getenv("SERPAPI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            f"SERPAPI_API_KEY is not set. Add it to {ENV_FILE} "
            "(see .env.example)."
        )
    return key
