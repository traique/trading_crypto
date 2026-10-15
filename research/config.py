"""Paths and the fixed research splits.

The data directory defaults to ``research/data`` next to this file and can be
moved with the environment variable ``CRYPTO_RESEARCH_DATA``. The path is read
on every call (not frozen at import), so a test or a script can point it
elsewhere at any time. It is never committed (``.gitignore``);
``python -m research download`` fills it.
"""

from __future__ import annotations

import os
from pathlib import Path

RESEARCH_DIR = Path(__file__).resolve().parent
DATA_ENV = "CRYPTO_RESEARCH_DATA"
# Default output directory of ``python -m research baselines`` (ignored by git).
OUT_DIR = RESEARCH_DIR / "out"

# Binance's first USDT pairs (BTCUSDT, ETHUSDT) start on 2017-08-17.
HISTORY_START = "2017-08-01"

# Fixed splits. Tune on TRAIN, check on VALID; TEST is run once, at the end, by the evaluator.
TRAIN = ("2018-01-01", "2021-12-31")
VALID = ("2022-01-01", "2023-12-31")
TEST = ("2024-01-01", "2099-12-31")
TRAIN_VALID = (TRAIN[0], VALID[1])
ALL = (TRAIN[0], TEST[1])
LOCK_AFTER = VALID[1]       # run() / load_panel() refuse anything after this unless unlock_test=True

SPLITS = {"TRAIN": TRAIN, "VALID": VALID, "TRAIN+VALID": TRAIN_VALID}

# A pair without a candle for more than this many days is gone (delisted, or
# halted for a swap): the backtest liquidates a holding at its last close, and
# history before such a gap is another market (``klines.last_segment`` uses 7 too).
DELIST_GAP_DAYS = 7


def data_dir() -> Path:
    return Path(os.environ.get(DATA_ENV) or RESEARCH_DIR / "data").expanduser().resolve()


def klines_dir() -> Path:
    """Daily candles, one ``{SYMBOL}.csv`` (+ ``{SYMBOL}.json`` manifest) per pair."""
    return data_dir() / "klines"


def meta_file() -> Path:
    return data_dir() / "download_meta.json"


def symbols_file() -> Path:
    """The archive listing: every USDT pair that ever had spot daily klines."""
    return data_dir() / "archive_symbols.json"


def exchange_info_file() -> Path:
    """Today's pair statuses from exchangeInfo (for the coverage report only)."""
    return data_dir() / "exchange_status.json"


def log_file() -> Path:
    return data_dir() / "download.log"


def panel_cache() -> Path:
    return data_dir() / "research_panel.pkl"


def data_ready() -> bool:
    """Whether the data directory holds a downloaded data set (BTCUSDT at least)."""
    return (klines_dir() / "BTCUSDT.csv").exists()
