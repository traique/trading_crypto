"""Where the project keeps its runtime files."""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR_ENV = "CRYPTO_DATA_DIR"


def data_dir() -> Path:
    """The data directory (env ``CRYPTO_DATA_DIR``, default ``./data``), created on first use."""
    path = Path(os.environ.get(DATA_DIR_ENV) or "data").expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path
