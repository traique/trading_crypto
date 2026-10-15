"""Numbers as the owner reads them: USDT amounts, coin quantities, prices, percentages.

USDT amounts use Binance style (comma thousands, dot decimals). Coin quantities
and prices are printed with a dot decimal point and NO thousands separator, the
way numbers are typed into the bot (``/khop mua BTC 0.0123 65000``), so a
quantity or price read in a report can be copied back into a command unchanged
(``parse_number`` refuses commas on purpose).
"""

from __future__ import annotations

import math
import re


def _ok(value) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def usdt(value, decimals: int = 2) -> str:
    """1234.5 -> "1,234.50"; "-" when unknown."""
    return f"{float(value):,.{decimals}f}" if _ok(value) else "-"


def qty(value) -> str:
    """A coin quantity, up to 8 decimals, trailing zeros removed: 0.01230000 -> "0.0123"."""
    if not _ok(value):
        return "-"
    text = f"{float(value):.8f}".rstrip("0").rstrip(".")
    return text or "0"


def price(value) -> str:
    """A price with the precision its size needs: 65432.10 / 1.2345 / 0.00001234 (no
    thousands separator: it can be typed back into /khop)."""
    if not _ok(value):
        return "-"
    v = float(value)
    if abs(v) >= 100:
        return f"{v:.2f}"
    if abs(v) >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".") or "0"


def pct(value, signed: bool = True, decimals: int = 2) -> str:
    if not _ok(value):
        return "-"
    return f"{float(value):+.{decimals}f}%" if signed else f"{float(value):.{decimals}f}%"


def short_usdt(value) -> str:
    """KPI tiles: 2,345,678,901 -> "2.35B"; 1,234,567 -> "1.23M"; 12,345.6 -> "12,345.60"."""
    if not _ok(value):
        return "-"
    v = float(value)
    if abs(v) >= 1e9:
        return f"{v / 1e9:,.2f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:,.2f}M"
    return usdt(v)


_NUMBER = re.compile(r"^\d+(\.\d+)?$")


def parse_number(text: str, what: str = "Số") -> float:
    """A positive number typed with a dot as the decimal point: 0.0123, 65000, 65000.5.

    Commas and other separators are refused rather than guessed: "65,000" is
    65000 for some and 65.000 (= 65) for others, and a quantity misread by a
    factor of 1000 is worse than a message asking to type it again.
    """
    raw = (text or "").strip()
    if not _NUMBER.match(raw):
        hint = " Dùng dấu chấm cho phần thập phân, không dùng dấu phẩy." if "," in raw else ""
        raise ValueError(f"{what} không hợp lệ: {text!r}.{hint} Ví dụ: 0.0123 hoặc 65000.")
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{what} phải lớn hơn 0.")
    return value
