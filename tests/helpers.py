"""Synthetic market data for the tests (no network)."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from cryptotrader.data import klines as kl

DAY_MS = 86_400_000


def day_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def kline_row(day: date, close: float = 100.0, *, micros: bool = False) -> list:
    """One Binance kline array (strings as the API sends them)."""
    open_ms = day_ms(day)
    scale = 1000 if micros else 1
    o, h, low = close * 0.99, close * 1.02, close * 0.97
    return [open_ms * scale, f"{o:.8f}", f"{h:.8f}", f"{low:.8f}", f"{close:.8f}", "10.5",
            (open_ms + DAY_MS) * scale - 1, f"{10.5 * close:.8f}", 1234, "5.0", "500.0", "0"]


def kline_rows(first: date, days: int, start_price: float = 100.0, step: float = 1.0, **kw) -> list:
    return [kline_row(first + timedelta(days=i), start_price + i * step, **kw) for i in range(days)]


def csv_bytes(rows: list, header: bool = False) -> bytes:
    lines = []
    if header:
        lines.append("open_time,open,high,low,close,volume,close_time,quote_volume,count,"
                     "taker_buy_volume,taker_buy_quote_volume,ignore")
    lines += [",".join(str(v) for v in r) for r in rows]
    return ("\n".join(lines) + "\n").encode()


def zip_bytes(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(name, payload)
    return buf.getvalue()


def frame(first: date, days: int, start_price: float = 100.0, step: float = 1.0) -> pd.DataFrame:
    return kl.binance_rows_to_frame(kline_rows(first, days, start_price, step))
