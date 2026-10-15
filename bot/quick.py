"""/nhanh: the strategy engine's quick view of one coin, from closed daily candles, no AI.

Price and changes, the trend state (close against SMA50/SMA200), 30-day
volatility, the distance from the one-year high, what the main strategy reads
from the same closes (``explain_closes``: for ``btc_sma_ens`` the 7 SMA votes and
the target weight), and whether the bot's books hold or target the coin as of
their last run. Seconds, no LLM quota.
"""

from __future__ import annotations

import math

from bot import clock, fmt
from bot.checks import check_request
from cryptotrader.market import rules


def _change(closes: list[float], days: int) -> float | None:
    if len(closes) <= days or closes[-1 - days] <= 0:
        return None
    return (closes[-1] / closes[-1 - days] - 1) * 100


def _sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def trend_state(closes: list[float]) -> tuple[str, str]:
    """("TĂNG" | "GIẢM" | "TRUNG TÍNH" | "CHƯA ĐỦ DỮ LIỆU", explanation)."""
    last, sma50, sma200 = closes[-1], _sma(closes, 50), _sma(closes, 200)
    if sma200 is None:
        return "CHƯA ĐỦ DỮ LIỆU", f"mới có {len(closes)} nến ngày, cần 200 để tính SMA200"
    detail = f"giá {fmt.price(last)}, SMA50 {fmt.price(sma50)}, SMA200 {fmt.price(sma200)}"
    if last > sma200 and sma50 > sma200:
        return "TĂNG", detail
    if last < sma200 and sma50 < sma200:
        return "GIẢM", detail
    return "TRUNG TÍNH", detail


def _volatility(closes: list[float], n: int = 30) -> float | None:
    """Annualised volatility of the last ``n`` daily log returns, in percent (365 days a year)."""
    window = closes[-(n + 1):]
    if len(window) < n + 1 or any(c <= 0 for c in window):
        return None
    rets = [math.log(b / a) for a, b in zip(window, window[1:], strict=False)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(365) * 100


def basket_lines(pair: str, trader) -> list[str]:
    """Which paper books hold or target ``pair`` as of their last run."""
    if trader is None or trader.settings.trade_mode == "off":
        return []
    parts = []
    for spec, book in trader.books():
        target = (book.meta.get("target") or {}).get(pair)
        held = book.positions.get(pair)
        if target or held:
            state = f"mục tiêu {target * 100:.0f}%" if target else "mục tiêu 0% (sẽ bán)"
            if held:
                state += f", đang giữ {fmt.qty(held.qty)}"
            parts.append(f"{spec.title}: {state}")
    if not parts:
        return ["Rổ của bot: không tài khoản ảo nào giữ hay nhắm tới coin này."]
    return ["Rổ của bot (lần chạy gần nhất): " + " | ".join(parts)]


def strategy_lines(pair: str, closes: list[float], trader=None) -> list[str]:
    """What the main strategy (the real-signal strategy, else the default) reads from these closes."""
    from cryptotrader.strategies import DEFAULT_STRATEGY, REGISTRY, get_strategy

    name = getattr(getattr(trader, "settings", None), "signal_strategy", None) or DEFAULT_STRATEGY
    if name not in REGISTRY:
        return []
    explain = getattr(get_strategy(name), "explain_closes", None)
    if not callable(explain):
        return []
    try:
        return [str(line) for line in explain(pair, closes)]
    except Exception:  # noqa: BLE001 - an extra line must not break /nhanh
        return []


def quick_view(symbol: str, day: str | None = None, trader=None) -> str:
    from cryptotrader.data import ohlcv

    pair, day = check_request(symbol, day)
    frame = ohlcv.load_ohlcv(pair, day, now=clock.now_utc())
    closes = [float(c) for c in frame["Close"] if math.isfinite(float(c))]
    if not closes:
        raise ValueError(f"Không có giá đóng cửa của {pair}.")
    last_day = frame["Date"].iloc[-1].strftime("%Y-%m-%d")
    base = rules.split_pair(pair)[0]
    state, detail = trend_state(closes)
    lines = [f"⚡ {base}/USDT - nến {last_day} (UTC, đóng lúc 07:00 giờ VN hôm sau)",
             f"Giá đóng cửa {fmt.price(closes[-1])} USDT | 1 ngày {fmt.pct(_change(closes, 1))} | "
             f"7 ngày {fmt.pct(_change(closes, 7))} | 30 ngày {fmt.pct(_change(closes, 30))}",
             f"Xu hướng: {state} ({detail})"]
    vol = _volatility(closes)
    year = closes[-365:]
    off_high = (closes[-1] / max(year) - 1) * 100 if year else None
    extra = []
    if vol is not None:
        extra.append(f"biến động 30 ngày {vol:.0f}%/năm")
    if off_high is not None:
        extra.append(f"cách đỉnh 365 ngày {fmt.pct(off_high)}")
    quote_volume = frame["QuoteVolume"].iloc[-1] if "QuoteVolume" in frame else None
    if quote_volume is not None and math.isfinite(float(quote_volume)):
        extra.append(f"khối lượng ngày {fmt.short_usdt(float(quote_volume))} USDT")
    if extra:
        text = " | ".join(extra)
        lines.append(text[0].upper() + text[1:])
    lines += strategy_lines(pair, closes, trader)
    lines += basket_lines(pair, trader)
    lines.append("Chỉ là số liệu kỹ thuật trên nến đã đóng, không phải khuyến nghị đầu tư.")
    return "\n".join(lines)
