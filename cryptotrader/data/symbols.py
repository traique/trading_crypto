"""Pair names: whatever the user types (BTC, btc, BTCUSDT, BTC/USDT, BTC-USDT) to BTCUSDT."""

from __future__ import annotations

import re

from cryptotrader.market.rules import QUOTE

_VALID = re.compile(r"^[A-Z0-9]{1,20}$")
_SEPARATORS = re.compile(r"[/\-_: ]+")
_FILE_SAFE = re.compile(r"^[A-Z0-9]{1,24}$")


def normalize_symbol(symbol: str) -> str:
    """The Binance USDT pair for ``symbol``; ValueError for anything else.

    ``BTC``, ``btc``, ``BTCUSDT``, ``BTC/USDT``, ``btc-usdt`` and ``BTC_USDT`` all
    give ``BTCUSDT``. A pair quoted in anything but USDT (``ETH/BTC``) is refused:
    this project trades USDT pairs only.
    """
    if not isinstance(symbol, str):
        raise ValueError(f"symbol must be text, not {type(symbol).__name__}")
    text = symbol.strip().upper()
    parts = [p for p in _SEPARATORS.split(text) if p]
    if len(parts) == 2:
        base, quote = parts
        if quote != QUOTE:
            raise ValueError(f"Chỉ hỗ trợ cặp {QUOTE}, không hỗ trợ {symbol!r}.")
    elif len(parts) == 1:
        base = parts[0]
        if base.endswith(QUOTE) and len(base) > len(QUOTE):
            base = base[: -len(QUOTE)]
    else:
        raise ValueError(f"Mã không hợp lệ: {symbol!r}.")
    if base == QUOTE or not _VALID.match(base):
        raise ValueError(f"Mã không hợp lệ: {symbol!r}.")
    pair = base + QUOTE
    if len(pair) > 24:
        raise ValueError(f"Mã không hợp lệ: {symbol!r}.")
    return pair


def base_asset(symbol: str) -> str:
    """BTC for BTCUSDT (any accepted spelling)."""
    return normalize_symbol(symbol)[: -len(QUOTE)]


def safe_file_component(symbol: str) -> str:
    """``symbol`` checked to be safe inside a file name (letters and digits only)."""
    if not isinstance(symbol, str) or not _FILE_SAFE.match(symbol):
        raise ValueError(f"unsafe symbol for a file name: {symbol!r}")
    return symbol
