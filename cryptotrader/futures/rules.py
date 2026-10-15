"""Rules of Binance USDⓈ-M perpetual futures (one-way mode, cross margin, one position per book).

Values the futures book, its backtest and the bot share:

- Costs per fill: taker fee ``TAKER_FEE_PCT`` (0.05% on Binance without BNB or
  VIP) plus ``SLIPPAGE_PCT`` (0.05%) against the trader. Override with
  ``CRYPTO_FUT_TAKER_FEE_PCT`` / ``CRYPTO_FUT_SLIPPAGE_PCT``.
- Funding: paid every 8 hours between longs and shorts. The backtest uses the
  real history (Binance archive). The live paper book does not know the
  coming rates, so it charges longs ``FUNDING_PCT_PER_DAY`` (default 0.03% =
  the usual 0.01% x 3) and pays shorts nothing: on the conservative side of
  the 2020-2026 average (BTC about +0.032%/day).
- Contract filters per symbol (``SPECS``): quantity step and the minimum
  notional value of an order (Binance: 100 USDT for BTCUSDT, 20 for ETHUSDT,
  5 for most others), and the maintenance margin rate of the first tier.
  They change from time to time: check them on Binance (Futures > contract
  info) before trading real money.
- Liquidation: a position is liquidated when the account equity falls to the
  maintenance margin (``liquidation_price``). The book then loses everything
  (the exchange keeps the remaining margin), which is close to what happens
  to a small cross-margin account.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


def _env_pct(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and 0 <= value < 5 else default


DEFAULT_TAKER_FEE_PCT = 0.05
DEFAULT_SLIPPAGE_PCT = 0.05
DEFAULT_FUNDING_PCT_PER_DAY = 0.03


def taker_fee_pct() -> float:
    return _env_pct("CRYPTO_FUT_TAKER_FEE_PCT", DEFAULT_TAKER_FEE_PCT)


def slippage_pct() -> float:
    return _env_pct("CRYPTO_FUT_SLIPPAGE_PCT", DEFAULT_SLIPPAGE_PCT)


def funding_pct_per_day() -> float:
    return _env_pct("FUTURES_FUNDING_PCT_DAY", DEFAULT_FUNDING_PCT_PER_DAY)


@dataclass(frozen=True)
class FuturesSpec:
    symbol: str
    step: float                 # quantity step (LOT_SIZE stepSize)
    min_notional: float         # smallest order value in USDT (MIN_NOTIONAL)
    mmr: float                  # maintenance margin rate, first tier
    known: bool = True          # False: conservative stand-in values, check them on Binance

    def floor_qty(self, qty: float) -> float:
        return _to_step(qty, self.step, ROUND_FLOOR)

    def ceil_qty(self, qty: float) -> float:
        return _to_step(qty, self.step, ROUND_CEILING)

    def min_qty_at(self, price: float) -> float:
        """The smallest quantity whose value reaches ``min_notional`` at ``price``."""
        return self.ceil_qty(self.min_notional / price) if price > 0 else math.inf


def _to_step(qty: float, step: float, rounding) -> float:
    if not math.isfinite(qty) or qty <= 0:
        return 0.0
    d_step = Decimal(str(step))
    return float((Decimal(str(qty)) / d_step).to_integral_value(rounding=rounding) * d_step)


SPECS: dict[str, FuturesSpec] = {
    "BTCUSDT": FuturesSpec("BTCUSDT", 0.001, 100.0, 0.004),
    "ETHUSDT": FuturesSpec("ETHUSDT", 0.001, 20.0, 0.004),
    "BNBUSDT": FuturesSpec("BNBUSDT", 0.01, 5.0, 0.005),
    "XRPUSDT": FuturesSpec("XRPUSDT", 0.1, 5.0, 0.005),
}


def spec_for(symbol: str) -> FuturesSpec:
    """The contract filters of ``symbol``; unknown symbols get conservative stand-ins."""
    symbol = symbol.upper()
    return SPECS.get(symbol) or FuturesSpec(symbol, 0.001, 5.0, 0.01, known=False)


def liquidation_price(cash: float, qty: float, entry: float, mmr: float) -> float | None:
    """The price at which equity (``cash`` + unrealised PnL) equals the maintenance margin.

    One position on a cross-margin account: equity(p) = cash + qty*(p - entry),
    maintenance(p) = |qty|*p*mmr. None for no position or no reachable price."""
    if not qty:
        return None
    side = 1.0 if qty > 0 else -1.0
    denom = qty * (1 - side * mmr)
    if denom == 0:
        return None
    price = (qty * entry - cash) / denom
    return price if price > 0 else None
