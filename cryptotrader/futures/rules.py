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
  notional value of an order (Binance: 50 USDT for BTCUSDT, 20 for ETHUSDT,
  LTC, LINK, BCH, 5 for most others), and the maintenance margin rate of the
  first tier (0.4% BTC/ETH; 1% assumed for altcoins, on the safe side).
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


# From Binance's exchangeInfo (www.binance.com/fapi/v1/exchangeInfo, 2026-10-15): LOT_SIZE
# stepSize and MIN_NOTIONAL. ``load_specs`` refreshes them from the exchange when it answers.
_TABLE = {
    "BTCUSDT": (0.001, 50), "ETHUSDT": (0.001, 20), "BCHUSDT": (0.001, 20), "LTCUSDT": (0.001, 20),
    "LINKUSDT": (0.01, 20), "BNBUSDT": (0.01, 5), "SOLUSDT": (0.01, 5), "XRPUSDT": (0.1, 5),
    "DOTUSDT": (0.1, 5), "OPUSDT": (0.1, 5), "APTUSDT": (0.1, 5), "ARBUSDT": (0.1, 5), "SUIUSDT": (0.1, 5),
    "TRXUSDT": (1, 5), "ADAUSDT": (1, 5), "DOGEUSDT": (1, 5), "UNIUSDT": (1, 5), "AVAXUSDT": (1, 5),
    "NEARUSDT": (1, 5),
}
SPECS: dict[str, FuturesSpec] = {
    sym: FuturesSpec(sym, step, notional, 0.004 if sym in ("BTCUSDT", "ETHUSDT") else 0.01)
    for sym, (step, notional) in _TABLE.items()
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


EXCHANGE_INFO_URL = "https://www.binance.com/fapi/v1/exchangeInfo"
EXCHANGE_INFO_MAX_AGE = 86_400.0


def parse_exchange_info(payload: dict) -> dict[str, tuple[float, float]]:
    """{symbol: (stepSize, minNotional)} of the USDT perpetuals that are trading."""
    out = {}
    for item in payload.get("symbols") or []:
        if (item.get("quoteAsset") != "USDT" or item.get("contractType") != "PERPETUAL"
                or item.get("status") != "TRADING"):
            continue
        filters = {f.get("filterType"): f for f in item.get("filters") or []}
        try:
            step = float(filters["LOT_SIZE"]["stepSize"])
            notional = float(filters["MIN_NOTIONAL"]["notional"])
        except (KeyError, TypeError, ValueError):
            continue
        if step > 0 and notional > 0:
            out[item["symbol"]] = (step, notional)
    return out


def refresh_specs(cache_dir=None, fetch=None, now=None) -> int:
    """Update ``SPECS`` from Binance's futures exchangeInfo (cached a day); returns how many.

    Best effort: when the exchange does not answer, the cache or the built-in
    table stays in use. ``fetch`` (a callable returning the JSON) is for tests."""
    import json
    import time
    from pathlib import Path

    now = time.time() if now is None else now
    cache = Path(cache_dir or os.environ.get("CRYPTO_DATA_DIR") or "data") / "futures_exchange_info.json"
    table = None
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if now - float(cached.get("fetched", 0)) < EXCHANGE_INFO_MAX_AGE:
            table = {k: tuple(v) for k, v in cached["specs"].items()}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if table is None:
        try:
            if fetch is None:
                import requests

                response = requests.get(EXCHANGE_INFO_URL, timeout=20)
                response.raise_for_status()
                payload = response.json()
            else:
                payload = fetch()
            table = parse_exchange_info(payload)
            if table:
                from cryptotrader.data.files import write_text_atomic

                cache.parent.mkdir(parents=True, exist_ok=True)
                write_text_atomic(cache, json.dumps({"fetched": now, "specs": table}))
        except Exception:  # noqa: BLE001 - keep the built-in table
            return 0
    for symbol, (step, notional) in (table or {}).items():
        old = SPECS.get(symbol)
        mmr = old.mmr if old else (0.004 if symbol in ("BTCUSDT", "ETHUSDT") else 0.01)
        SPECS[symbol] = FuturesSpec(symbol, float(step), float(notional), mmr)
    return len(table or {})
