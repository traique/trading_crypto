"""Trading rules of Binance spot (USDT pairs), in one place.

Every part of the project (the ledger, the backtest, the bot, the agents) reads
these values here, so a backtest charges what the paper account charges.

- The market trades 24/7: no sessions, no holidays, no price bands.
- The daily candle is a UTC day: it opens at 00:00 UTC and closes at the next
  00:00 UTC (07:00 Vietnam time). A candle is treated as final from
  ``FINAL_AFTER`` (00:05 UTC) of the next day, so a run just after midnight does
  not read a candle the exchange may still be closing.
- Costs: the taker fee (0.1% per side without BNB discount or VIP tier) on every
  fill, plus ``SLIPPAGE_PCT`` against the trader on every fill at a market price.
  Both can be overridden by the environment (``CRYPTO_TAKER_FEE_PCT``,
  ``CRYPTO_SLIPPAGE_PCT``) or per ledger.
- No settlement delay: what is bought can be sold on the same day, and the
  proceeds of a sale are cash at once.
- Quantities are fractional, floored to the pair's LOT_SIZE ``stepSize`` and
  bounded below by ``minQty`` and by the NOTIONAL/MIN_NOTIONAL filter
  (``minNotional``, in USDT). Prices sit on the PRICE_FILTER ``tickSize`` grid.
  The filters come from exchangeInfo (``SymbolFilters.from_exchange_info``);
  ``DEFAULT_FILTERS`` are conservative stand-ins when it is unavailable.
- The tradable universe is USDT-quoted pairs whose base is a real crypto asset:
  stablecoins and fiat (as base), leveraged tokens and wrapped/pegged duplicates
  are excluded (``exclusion_reason``).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

QUOTE = "USDT"
BENCHMARK = "BTCUSDT"


def _env_pct(name: str, default: float) -> float:
    """A non-negative percentage from the environment, or ``default`` when unset or invalid."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and 0 <= value < 10 else default


# ── costs (percent of traded value, per fill) ──
DEFAULT_TAKER_FEE_PCT = 0.1
DEFAULT_SLIPPAGE_PCT = 0.1
TAKER_FEE_PCT = _env_pct("CRYPTO_TAKER_FEE_PCT", DEFAULT_TAKER_FEE_PCT)
SLIPPAGE_PCT = _env_pct("CRYPTO_SLIPPAGE_PCT", DEFAULT_SLIPPAGE_PCT)


def reload_costs() -> tuple[float, float]:
    """Read ``CRYPTO_TAKER_FEE_PCT`` / ``CRYPTO_SLIPPAGE_PCT`` again (after a .env file
    was loaded into the environment, which may happen after this module was imported).
    Returns (fee, slippage)."""
    global TAKER_FEE_PCT, SLIPPAGE_PCT
    TAKER_FEE_PCT = _env_pct("CRYPTO_TAKER_FEE_PCT", DEFAULT_TAKER_FEE_PCT)
    SLIPPAGE_PCT = _env_pct("CRYPTO_SLIPPAGE_PCT", DEFAULT_SLIPPAGE_PCT)
    return TAKER_FEE_PCT, SLIPPAGE_PCT


def round_trip_cost_pct(fee_pct: float | None = None, slippage_pct: float | None = None) -> float:
    """Fees and slippage of one buy and one sell, in percent of the traded value."""
    fee = TAKER_FEE_PCT if fee_pct is None else fee_pct
    slip = SLIPPAGE_PCT if slippage_pct is None else slippage_pct
    return 2 * (fee + slip)


# ── the daily candle ──
DAY_CLOSE_UTC = time(0, 0)
# A daily candle is final from this time of the next UTC day on.
FINAL_AFTER = time(0, 5)


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(now: datetime | None) -> datetime:
    if now is None:
        return utc_now()
    if now.tzinfo is None:
        # A naive datetime is taken as UTC, never as the host's local time.
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def final_day(now: datetime | None = None) -> date:
    """The UTC date of the last fully closed daily candle at ``now``.

    The candle of day D closes at 00:00 UTC of D+1 and is final from 00:05 UTC
    of D+1 (``FINAL_AFTER``). So at 2026-10-15 00:04 UTC the last final candle is
    2026-10-13's, and from 00:05 UTC on it is 2026-10-14's.
    """
    now = as_utc(now)
    day = now.date()
    if now.time() >= FINAL_AFTER:
        return day - timedelta(days=1)
    return day - timedelta(days=2)


def candle_close_time(day: date) -> datetime:
    """When the daily candle of ``day`` closes (00:00 UTC of the next day)."""
    return datetime.combine(day + timedelta(days=1), DAY_CLOSE_UTC, tzinfo=UTC)


# ── symbol filters ──
def _dec(value: float | str) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class SymbolFilters:
    """The exchange filters of one pair (PRICE_FILTER, LOT_SIZE, NOTIONAL)."""

    tick_size: float = 1e-8
    step_size: float = 1e-8
    min_qty: float = 1e-8
    min_notional: float = 10.0
    max_qty: float | None = None

    @classmethod
    def from_exchange_info(cls, symbol_info: dict) -> SymbolFilters:
        """Filters from one entry of exchangeInfo's ``symbols``; missing ones keep the defaults."""
        values: dict = {}
        for flt in symbol_info.get("filters") or []:
            kind = flt.get("filterType")
            if kind == "PRICE_FILTER":
                values["tick_size"] = _positive_or_none(flt.get("tickSize"))
            elif kind == "LOT_SIZE":
                values["step_size"] = _positive_or_none(flt.get("stepSize"))
                values["min_qty"] = _positive_or_none(flt.get("minQty"))
                values["max_qty"] = _positive_or_none(flt.get("maxQty"))
            elif kind in ("NOTIONAL", "MIN_NOTIONAL"):
                values["min_notional"] = _positive_or_none(flt.get("minNotional"))
        return cls(**{k: v for k, v in values.items() if v is not None})

    def to_dict(self) -> dict:
        return {"tick_size": self.tick_size, "step_size": self.step_size, "min_qty": self.min_qty,
                "min_notional": self.min_notional, "max_qty": self.max_qty}

    @classmethod
    def from_dict(cls, data: dict) -> SymbolFilters:
        known = {"tick_size", "step_size", "min_qty", "min_notional", "max_qty"}
        return cls(**{k: (float(v) if v is not None else None) for k, v in data.items() if k in known})


def _positive_or_none(raw) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


# Used when exchangeInfo cannot be read: a fine grid (no made-up rounding loss)
# and the strictest minimum order value seen on USDT pairs, so an order the
# defaults accept is not one the exchange would reject for being too small.
DEFAULT_FILTERS = SymbolFilters()


def floor_to_step(qty: float, step: float) -> float:
    """The largest multiple of ``step`` at or below ``qty`` (a quantity the exchange accepts).

    Decimal arithmetic avoids binary noise (0.3 / 0.1 = 2.9999999999999996); a
    relative 1e-12 tolerance keeps a quantity that is a multiple up to float
    noise from losing a whole step.
    """
    if not (math.isfinite(qty) and qty > 0):
        return 0.0
    if not (math.isfinite(step) and step > 0):
        return float(qty)
    step_d = _dec(step)
    units = ((_dec(qty) * (1 + Decimal("1e-12"))) / step_d).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * step_d)


def _to_tick(price: float, tick: float, rounding: str) -> float:
    if not (math.isfinite(price) and price > 0):
        return price
    if not (math.isfinite(tick) and tick > 0):
        return float(price)
    tick_d = _dec(tick)
    return float((_dec(price) / tick_d).to_integral_value(rounding=rounding) * tick_d)


def floor_to_tick(price: float, tick: float) -> float:
    """The highest price on the tick grid at or below ``price``."""
    return _to_tick(price, tick, ROUND_FLOOR)


def ceil_to_tick(price: float, tick: float) -> float:
    """The lowest price on the tick grid at or above ``price``."""
    return _to_tick(price, tick, ROUND_CEILING)


def round_price_against(price: float, side: str, tick: float) -> float:
    """``price`` onto the tick grid in the direction that hurts the trader:
    a BUY pays the tick above, a SELL receives the tick below."""
    return ceil_to_tick(price, tick) if side.upper() == "BUY" else floor_to_tick(price, tick)


def slipped(price: float, side: str, filters: SymbolFilters | None = None,
            slippage_pct: float | None = None) -> float:
    """``price`` moved ``slippage_pct`` against the trader, onto the tick grid in that direction."""
    filters = filters or DEFAULT_FILTERS
    slip = SLIPPAGE_PCT if slippage_pct is None else slippage_pct
    factor = 1 + slip / 100 if side.upper() == "BUY" else 1 - slip / 100
    return round_price_against(price * factor, side, filters.tick_size)


def order_rejection(qty: float, price: float, filters: SymbolFilters | None = None) -> str | None:
    """Why the exchange would refuse an order of ``qty`` at ``price`` (in Vietnamese), or None."""
    filters = filters or DEFAULT_FILTERS
    if not (math.isfinite(qty) and qty > 0):
        return "khối lượng phải dương"
    if not (math.isfinite(price) and price > 0):
        return "giá không hợp lệ"
    if qty + 1e-15 < filters.min_qty:
        return f"khối lượng {qty:g} dưới mức tối thiểu {filters.min_qty:g}"
    if filters.max_qty and qty > filters.max_qty * (1 + 1e-12):
        return f"khối lượng {qty:g} vượt mức tối đa {filters.max_qty:g}"
    if qty * price + 1e-9 < filters.min_notional:
        return f"giá trị lệnh {qty * price:.2f} USDT dưới mức tối thiểu {filters.min_notional:g} USDT"
    return None


# ── the tradable universe ──
# Stablecoins, fiat and fiat-like tokens: as a base they are cash, not a trade.
STABLE_BASES = frozenset({
    "USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "USDS", "USDSB", "SUSD", "UST",
    "USTC", "USDD", "PYUSD", "USD1", "RLUSD", "USDE", "XUSD", "BFUSD", "AEUR", "EURI", "EUR", "GBP",
    "AUD", "TRY", "BRL", "RUB", "UAH", "NGN", "ZAR", "IDRT", "BIDR", "BVND", "BKRW", "ARS", "PLN",
    "RON", "JPY", "MXN", "COP", "CZK", "VAI", "FRAX", "LUSD", "GUSD", "EURT", "EURC", "USD",
})
# Wrapped, staked or pegged duplicates of another asset (and gold): same risk as the original.
PEGGED_BASES = frozenset({
    "WBTC", "WETH", "WBETH", "BETH", "STETH", "WSTETH", "CBBTC", "BTCB", "BNSOL", "WBNB",
    "PAXG", "XAUT", "RENBTC", "HBTC", "BBTC",
})
FIAT_QUOTES = frozenset({
    "EUR", "GBP", "AUD", "TRY", "BRL", "RUB", "UAH", "NGN", "ZAR", "IDRT", "BIDR", "BVND", "BKRW",
    "ARS", "PLN", "RON", "JPY", "MXN", "COP", "CZK", "USD",
})
# Underlyings that had Binance leveraged tokens (<X>UP / <X>DOWN). "UP" alone is
# a common ending (JUP, SUP...), so only these count; DOWN/BULL/BEAR always do.
LEVERAGED_UNDERLYINGS = frozenset({
    "BTC", "ETH", "BNB", "XRP", "ADA", "LINK", "DOT", "TRX", "XTZ", "EOS", "LTC", "BCH", "YFI",
    "SXP", "FIL", "AAVE", "SUSHI", "UNI", "XLM", "1INCH",
})


def split_pair(symbol: str) -> tuple[str, str]:
    """(base, quote) of a Binance pair name such as BTCUSDT; quote "" when not recognised."""
    symbol = symbol.strip().upper()
    for quote in ("FDUSD", "USDT", "USDC", "TUSD", "BUSD", "BTC", "ETH", "BNB", *sorted(FIAT_QUOTES, key=len, reverse=True)):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, ""


def is_leveraged_token(base: str) -> bool:
    base = base.upper()
    if base in ("BULL", "BEAR") or base.endswith(("BULL", "BEAR")):
        return True
    if base.endswith("DOWN") and len(base) > 4:
        return True
    return base.endswith("UP") and base[:-2] in LEVERAGED_UNDERLYINGS


def exclusion_reason(symbol: str, base: str | None = None, quote: str | None = None) -> str | None:
    """Why a pair is not in the tradable universe, or None when it is.

    ``base``/``quote`` (from exchangeInfo) are used when given; otherwise they are
    split from the symbol name.
    """
    if base is None or quote is None:
        base, quote = split_pair(symbol)
    base, quote = base.upper(), quote.upper()
    if quote in FIAT_QUOTES:
        return "cặp niêm yết bằng tiền pháp định"
    if quote != QUOTE:
        return f"không phải cặp {QUOTE}"
    if base in STABLE_BASES:
        return "stablecoin/tiền pháp định"
    if is_leveraged_token(base):
        return "token đòn bẩy"
    if base in PEGGED_BASES:
        return "token bọc/neo giá trùng tài sản khác"
    return None


def is_tradable_pair(symbol: str, base: str | None = None, quote: str | None = None) -> bool:
    return exclusion_reason(symbol, base, quote) is None
