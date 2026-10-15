"""Checks on what people type: a coin and a day, before anything is queued or computed."""

from __future__ import annotations

from datetime import date

from bot import clock
from cryptotrader.market import rules


def check_symbol(symbol: str) -> str:
    """The Binance USDT pair for ``symbol`` (BTC, btc, BTCUSDT, BTC/USDT...); ValueError otherwise."""
    from cryptotrader.data.symbols import normalize_symbol

    try:
        pair = normalize_symbol((symbol or "").strip())
    except ValueError as exc:
        raise ValueError(f"{exc} Ví dụ: BTC, ETH, SOL.") from None
    why = rules.exclusion_reason(pair)
    if why:
        raise ValueError(f"{pair}: {why}, không có quyết định giao dịch. Hãy nhập một coin, ví dụ BTC.")
    return pair


def check_request(symbol: str, day: str | None = None) -> tuple[str, str]:
    """A pair and a UTC day (YYYY-MM-DD) whose candle is closed; ValueError otherwise.

    No day means the last closed candle. Today's day (its candle is still
    forming) is moved back to the last closed one; a future day is refused.
    """
    pair = check_symbol(symbol)
    raw = (day or "").strip()
    final = clock.final_day()
    if not raw:
        return pair, final
    try:
        parsed = date.fromisoformat(raw).isoformat() if len(raw) == 10 else None
    except ValueError:
        parsed = None
    if parsed is None:
        raise ValueError(f"Ngày không hợp lệ: {raw!r}. Dùng dạng YYYY-MM-DD.")
    if parsed > clock.today_utc():
        raise ValueError("Không phân tích được ngày trong tương lai.")
    return pair, min(parsed, final)
