"""OKX daily candles: the fallback when Binance cannot answer.

OKX is another exchange: its prices track Binance's closely for liquid pairs,
but its volumes are OKX's own (much smaller for most pairs) and it reports no
trade count. So OKX rows only fill the missing tail of a Binance history for
a run that cannot wait; they are never written into the Binance cache.

``bar=1Dutc`` gives candles aligned on 00:00 UTC like Binance's. Rows come
newest first; ``confirm`` is "0" for the forming candle, which is dropped.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, date, datetime

import pandas as pd

from cryptotrader.data import klines as kl
from cryptotrader.data.errors import NoMarketDataError, UnknownSymbolError, VendorUnavailableError
from cryptotrader.data.net import breaker, http_get
from cryptotrader.market.rules import QUOTE

SOURCE = "okx"
BASE_URL = os.environ.get("CRYPTO_OKX_URL", "https://www.okx.com").rstrip("/")


def reload_base_url() -> str:
    """Read ``CRYPTO_OKX_URL`` again (after a .env file was loaded)."""
    global BASE_URL
    BASE_URL = (os.environ.get("CRYPTO_OKX_URL") or "https://www.okx.com").rstrip("/")
    return BASE_URL
PAGE_LIMIT = 100
MAX_PAGES = 40
PAGINATION_DEADLINE = 60.0


def inst_id(symbol: str) -> str:
    """BTCUSDT -> BTC-USDT."""
    if not symbol.endswith(QUOTE):
        raise ValueError(f"not a {QUOTE} pair: {symbol!r}")
    return f"{symbol[: -len(QUOTE)]}-{QUOTE}"


def rows_to_frame(rows: list) -> pd.DataFrame:
    """OKX candle arrays to the standard frame; unconfirmed (forming) candles are dropped."""
    kept = [r for r in rows if isinstance(r, list) and len(r) >= 6 and (len(r) < 9 or str(r[8]) != "0")]
    if not kept:
        return kl.empty_frame()
    table = pd.DataFrame([r[:9] + [None] * (9 - len(r[:9])) for r in kept])
    frame = pd.DataFrame({
        "Date": kl.epoch_to_day(table[0]).to_numpy(),
        "Open": table[1], "High": table[2], "Low": table[3], "Close": table[4],
        "Volume": table[5], "QuoteVolume": table[7], "Trades": float("nan"),
    })
    return kl.clean(frame)


def _page(symbol: str, after_ms: int | None) -> list:
    params = {"instId": inst_id(symbol), "bar": "1Dutc", "limit": PAGE_LIMIT}
    if after_ms is not None:
        params["after"] = after_ms
    result = http_get(f"{BASE_URL}/api/v5/market/history-candles", params=params, allow_status=(400,))
    payload = result.json()
    code = str(payload.get("code")) if isinstance(payload, dict) else "?"
    if code == "51001":
        raise UnknownSymbolError(symbol, "OKX không có cặp này")
    if code != "0" or not isinstance(payload.get("data"), list):
        raise VendorUnavailableError(f"{SOURCE}: lỗi {code} {payload.get('msg', '') if isinstance(payload, dict) else ''}")
    return payload["data"]


def fetch_klines(symbol: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Confirmed daily candles of ``symbol`` from ``start`` (or as far back as the pages
    allow) through ``end``, through the OKX breaker."""
    end = end or datetime.now(UTC).date()

    def call():
        rows: list = []
        after = int(datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp() * 1000) + 86_400_000
        began = time.monotonic()
        for _ in range(MAX_PAGES):
            if time.monotonic() - began > PAGINATION_DEADLINE:
                raise VendorUnavailableError(f"{SOURCE}: tải nến {symbol} quá {PAGINATION_DEADLINE:.0f}s")
            page = _page(symbol, after)
            if not page:
                break
            rows.extend(page)
            oldest = min(int(r[0]) for r in page)
            if len(page) < PAGE_LIMIT or (start and oldest <= _ms(start)) or oldest >= after:
                break
            after = oldest
        return rows

    frame = rows_to_frame(breaker(SOURCE).call(call))
    if start is not None:
        frame = frame[frame["Date"] >= pd.Timestamp(start)]
    frame = frame[frame["Date"] <= pd.Timestamp(end)].reset_index(drop=True)
    if frame.empty:
        raise NoMarketDataError(symbol, "OKX không có nến trong khoảng này")
    frame.attrs["source"] = SOURCE
    return frame


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)

