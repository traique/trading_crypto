"""How numbers are written in every report the agents read: Binance style, the same as the bot.

Comma thousands, dot decimals (65,432.10 USDT; -4.76%), and a price precision
that follows its size, since USDT prices span 0.00000123 to 100,000+.
"""

from __future__ import annotations

import math


def _ok(value) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def price(value) -> str:
    """65,432.10 / 123.45 / 1.2345 / 0.00001234 ; "N/A" when unknown."""
    if not _ok(value):
        return "N/A"
    v = float(value)
    a = abs(v)
    if a >= 100:
        return f"{v:,.2f}"
    if a >= 1:
        return f"{v:,.4f}"
    if a == 0:
        return "0"
    decimals = min(12, max(4, -math.floor(math.log10(a)) + 3))   # four significant digits
    return f"{v:.{decimals}f}"


def number(value, decimals: int = 2) -> str:
    return f"{float(value):,.{decimals}f}" if _ok(value) else "N/A"


def pct(value, decimals: int = 2) -> str:
    return f"{float(value):.{decimals}f}%" if _ok(value) else "N/A"


def signed_pct(value, decimals: int = 2) -> str:
    return f"{float(value):+.{decimals}f}%" if _ok(value) else "N/A"


def usd_short(value) -> str:
    """1.23T / 4.56B / 7.89M / 12,345 ; "N/A" when unknown."""
    if not _ok(value):
        return "N/A"
    v = float(value)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(v) >= limit:
            return f"{v / limit:,.2f}{suffix}"
    return f"{v:,.0f}"


def distance_pct(reference, level) -> float | None:
    """How far ``level`` is from ``reference`` in percent (positive = above)."""
    if not _ok(reference) or not _ok(level) or float(reference) <= 0:
        return None
    return (float(level) - float(reference)) / float(reference) * 100.0
