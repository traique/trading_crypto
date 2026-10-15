"""Binance spot market data over the public data mirror (data-api.binance.vision).

api.binance.com answers 451 from some regions, so nothing here depends on it;
data-api.binance.vision serves the same market-data endpoints without a key.

- ``fetch_klines``: daily candles, paginated (1000 per request), under a total deadline.
- ``fetch_exchange_info`` / ``load_exchange_info``: pair status and filters, cached a day.
- ``fetch_tickers_24h``: last price and 24h volume of every pair.
- ``tradable_symbols``: the USDT universe after the exclusion rules of ``market.rules``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from cryptotrader.config import data_dir
from cryptotrader.data import klines as kl
from cryptotrader.data.errors import (
    NoMarketDataError,
    UnknownSymbolError,
    VendorError,
    VendorUnavailableError,
)
from cryptotrader.data.files import write_text_atomic
from cryptotrader.data.net import breaker, http_get
from cryptotrader.market import rules

logger = logging.getLogger(__name__)

SOURCE = "binance"
BASE_URL = os.environ.get("CRYPTO_BINANCE_DATA_URL", "https://data-api.binance.vision").rstrip("/")


def reload_base_url() -> str:
    """Read ``CRYPTO_BINANCE_DATA_URL`` again (after a .env file was loaded)."""
    global BASE_URL
    BASE_URL = (os.environ.get("CRYPTO_BINANCE_DATA_URL") or "https://data-api.binance.vision").rstrip("/")
    return BASE_URL
KLINES_LIMIT = 1000
MAX_PAGES = 20                       # 20 000 days: far more than any pair's history
PAGINATION_DEADLINE = 90.0           # seconds for a whole paginated download
EXCHANGE_INFO_MAX_AGE = 24 * 3600
_DAY_MS = 86_400_000


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def _get(path: str, params: dict | None = None):
    """JSON from the data mirror, through the Binance breaker."""
    def call():
        result = http_get(f"{BASE_URL}{path}", params=params, allow_status=(400,))
        payload = result.json()
        if result.status == 400:
            code = payload.get("code") if isinstance(payload, dict) else None
            msg = payload.get("msg") if isinstance(payload, dict) else result.text[:200]
            symbol = (params or {}).get("symbol", "?")
            if code == -1121:
                raise UnknownSymbolError(symbol, f"Binance không có cặp này ({msg})")
            raise NoMarketDataError(symbol, f"Binance từ chối yêu cầu: {code} {msg}")
        return payload

    return breaker(SOURCE).call(call)


def fetch_klines(symbol: str, start: date | None = None, end: date | None = None, *,
                 interval: str = "1d") -> pd.DataFrame:
    """Daily candles of ``symbol`` from ``start`` (the listing when None) through ``end``.

    The result may hold the forming candle of today; ``klines.drop_unfinished``
    removes it. Raises ``UnknownSymbolError`` when Binance does not list the pair.
    """
    end = end or datetime.now(UTC).date()
    start_ms = _ms(start) if start else 0
    end_ms = _ms(end + timedelta(days=1)) - 1
    rows: list = []
    began = time.monotonic()
    for _ in range(MAX_PAGES):
        if time.monotonic() - began > PAGINATION_DEADLINE:
            raise VendorUnavailableError(f"{SOURCE}: tải nến {symbol} quá {PAGINATION_DEADLINE:.0f}s")
        page = _get("/api/v3/klines", {"symbol": symbol, "interval": interval, "startTime": start_ms,
                                        "endTime": end_ms, "limit": KLINES_LIMIT})
        if not isinstance(page, list):
            raise VendorUnavailableError(f"{SOURCE}: phản hồi nến không hợp lệ cho {symbol}")
        rows.extend(page)
        if len(page) < KLINES_LIMIT:
            break
        try:
            next_ms = int(page[-1][0]) + _DAY_MS
        except (TypeError, ValueError, IndexError):
            break
        if next_ms <= start_ms or next_ms > end_ms:
            break
        start_ms = next_ms
    frame = kl.binance_rows_to_frame(rows)
    frame.attrs["source"] = SOURCE
    return frame


# ── exchangeInfo ──
@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    base: str
    quote: str
    status: str
    spot: bool
    filters: rules.SymbolFilters

    @property
    def trading(self) -> bool:
        return self.status == "TRADING" and self.spot

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "base": self.base, "quote": self.quote, "status": self.status,
                "spot": self.spot, "filters": self.filters.to_dict()}

    @classmethod
    def from_dict(cls, data: dict) -> SymbolInfo:
        return cls(str(data["symbol"]), str(data["base"]), str(data["quote"]), str(data["status"]),
                   bool(data.get("spot", True)), rules.SymbolFilters.from_dict(data.get("filters") or {}))


def parse_exchange_info(payload: dict) -> dict[str, SymbolInfo]:
    """exchangeInfo's ``symbols`` as {symbol: SymbolInfo}; malformed entries are skipped."""
    out: dict[str, SymbolInfo] = {}
    for entry in (payload or {}).get("symbols") or []:
        try:
            symbol = str(entry["symbol"]).upper()
            spot = bool(entry.get("isSpotTradingAllowed", True))
            out[symbol] = SymbolInfo(symbol, str(entry["baseAsset"]).upper(), str(entry["quoteAsset"]).upper(),
                                     str(entry.get("status", "")), spot,
                                     rules.SymbolFilters.from_exchange_info(entry))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_exchange_info(symbols: list[str] | None = None) -> dict[str, SymbolInfo]:
    params = {"symbols": json.dumps(symbols, separators=(",", ":"))} if symbols else None
    return parse_exchange_info(_get("/api/v3/exchangeInfo", params))


def _info_cache_path():
    return data_dir() / "exchange_info.json"


def load_exchange_info(max_age: float = EXCHANGE_INFO_MAX_AGE) -> dict[str, SymbolInfo]:
    """All pairs' info, from a cache younger than ``max_age`` seconds or downloaded.

    When Binance cannot answer, an older cache is served; with no cache at all
    the result is empty (callers fall back to ``rules.DEFAULT_FILTERS``).
    """
    path = _info_cache_path()
    cached: dict[str, SymbolInfo] | None = None
    fetched_at = 0.0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = float(raw.get("fetched_at", 0))
        cached = {k: SymbolInfo.from_dict(v) for k, v in (raw.get("symbols") or {}).items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        cached = None
    if cached and time.time() - fetched_at < max_age:
        return cached
    try:
        info = fetch_exchange_info()
    except VendorError as exc:
        logger.warning("exchangeInfo unavailable (%s); using %s", exc, "the older cache" if cached else "defaults")
        return cached or {}
    if info:
        write_text_atomic(path, json.dumps({"fetched_at": time.time(),
                                            "symbols": {k: v.to_dict() for k, v in info.items()}}))
    return info or cached or {}


def symbol_filters(symbol: str, info: dict[str, SymbolInfo] | None = None) -> rules.SymbolFilters:
    """The filters of ``symbol``; the conservative defaults when they are not known."""
    info = load_exchange_info() if info is None else info
    found = info.get(symbol)
    return found.filters if found else rules.DEFAULT_FILTERS


# ── 24h tickers ──
def parse_tickers(payload) -> pd.DataFrame:
    """The 24h ticker array as a frame indexed by symbol: last, change_pct, quote_volume, trades."""
    rows = []
    for t in payload if isinstance(payload, list) else []:
        try:
            rows.append({"symbol": str(t["symbol"]).upper(), "last": float(t["lastPrice"]),
                         "change_pct": float(t.get("priceChangePercent", "nan")),
                         "quote_volume": float(t.get("quoteVolume", "nan")),
                         "trades": float(t.get("count", "nan"))})
        except (KeyError, TypeError, ValueError):
            continue
    frame = pd.DataFrame(rows, columns=["symbol", "last", "change_pct", "quote_volume", "trades"])
    return frame.set_index("symbol")


def fetch_tickers_24h(symbols: list[str] | None = None) -> pd.DataFrame:
    params = {"symbols": json.dumps(symbols, separators=(",", ":"))} if symbols else None
    payload = _get("/api/v3/ticker/24hr", params)
    return parse_tickers(payload if isinstance(payload, list) else [payload])


def tradable_symbols(info: dict[str, SymbolInfo], tickers: pd.DataFrame | None = None, *,
                     min_quote_volume: float = 0.0) -> list[str]:
    """Trading USDT pairs that pass ``rules.exclusion_reason``, by 24h USDT volume (largest first)
    when ``tickers`` are given. Point-in-time only for today: for a backtest universe use
    the archive listing (delisted pairs included) and volume as of each date."""
    names = [s for s, i in info.items() if i.trading and rules.is_tradable_pair(s, i.base, i.quote)]
    if tickers is None or tickers.empty:
        return sorted(names)
    volume = {s: float(tickers["quote_volume"].get(s, 0.0) or 0.0) for s in names}
    names = [s for s in names if volume[s] >= min_quote_volume]
    return sorted(names, key=lambda s: (-volume[s], s))
