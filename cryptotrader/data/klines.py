"""Daily candles as one DataFrame shape, whatever the source.

Every frame leaving the data layer has the columns ``COLUMNS``:

- ``Date``: the candle's UTC day as a tz-naive ``datetime64`` at midnight
  (the candle covers [Date 00:00 UTC, Date+1 00:00 UTC));
- ``Open``/``High``/``Low``/``Close``: USDT prices;
- ``Volume``: base-asset volume; ``QuoteVolume``: USDT volume;
- ``Trades``: number of trades (float, NaN when the source does not report it).
"""

from __future__ import annotations

import io
from datetime import date, datetime

import numpy as np
import pandas as pd

from cryptotrader.market import rules

COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume", "QuoteVolume", "Trades"]
NUMERIC = COLUMNS[1:]

# Binance writes spot archive timestamps in microseconds from 2025-01-01 on,
# milliseconds before; any epoch value above this is microseconds.
_MICROS_THRESHOLD = 10**14


def empty_frame() -> pd.DataFrame:
    frame = pd.DataFrame({c: pd.Series(dtype="float64") for c in COLUMNS})
    frame["Date"] = pd.Series(dtype="datetime64[ns]")
    return frame


def epoch_to_day(values) -> pd.Series:
    """Epoch timestamps (ms or µs, mixed allowed) to UTC dates as tz-naive midnights."""
    raw = pd.to_numeric(pd.Series(values), errors="coerce").astype("float64")
    ms = np.where(raw > _MICROS_THRESHOLD, raw / 1000.0, raw)
    stamps = pd.to_datetime(ms, unit="ms", utc=True, errors="coerce")
    return pd.Series(stamps).dt.tz_convert(None).dt.normalize()


def binance_rows_to_frame(rows: list) -> pd.DataFrame:
    """Binance kline arrays (REST or archive CSV rows) to the standard frame."""
    if not rows:
        return empty_frame()
    table = pd.DataFrame([list(r)[:9] for r in rows if isinstance(r, list | tuple) and len(r) >= 9])
    if table.empty:
        return empty_frame()
    frame = pd.DataFrame({
        "Date": epoch_to_day(table[0]).to_numpy(),
        "Open": table[1], "High": table[2], "Low": table[3], "Close": table[4],
        "Volume": table[5], "QuoteVolume": table[7], "Trades": table[8],
    })
    return clean(frame)


def parse_binance_csv(data: bytes | str) -> pd.DataFrame:
    """A Binance archive kline CSV (with or without a header row) to the standard frame."""
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    table = pd.read_csv(io.StringIO(text), header=None, dtype=str)
    if table.empty:
        return empty_frame()
    first = str(table.iloc[0, 0]).strip()
    if not first.replace(".", "", 1).isdigit():          # a header row ("open_time,...")
        table = table.iloc[1:]
    return binance_rows_to_frame(table.to_numpy().tolist())


def clean(frame: pd.DataFrame) -> pd.DataFrame:
    """Typed, sorted, one row per day; rows without a positive close are dropped."""
    if frame is None or frame.empty:
        return empty_frame()
    frame = frame.copy()
    for col in COLUMNS:
        if col not in frame.columns:
            frame[col] = np.nan
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    if getattr(frame["Date"].dt, "tz", None) is not None:
        frame["Date"] = frame["Date"].dt.tz_convert(None)
    frame["Date"] = frame["Date"].dt.normalize()
    for col in NUMERIC:
        frame[col] = pd.to_numeric(frame[col], errors="coerce").astype("float64")
    frame = frame.dropna(subset=["Date", "Close"])
    frame = frame[frame["Close"] > 0]
    frame = frame.drop_duplicates("Date", keep="last").sort_values("Date")
    frame["Date"] = frame["Date"].astype("datetime64[ns]")
    return frame[COLUMNS].reset_index(drop=True)


def to_day(value) -> date:
    """A date from a date, datetime, Timestamp or 'YYYY-MM-DD' text."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def drop_unfinished(frame: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Only candles that are final at ``now`` (``rules.final_day``): the forming one is dropped."""
    if frame.empty:
        return frame
    last_final = pd.Timestamp(rules.final_day(now))
    return frame[frame["Date"] <= last_final].reset_index(drop=True)


def last_segment(frame: pd.DataFrame, max_gap_days: int = 7) -> pd.DataFrame:
    """The rows after the last gap of more than ``max_gap_days`` days.

    A pair that was delisted and listed again (or halted for a token swap) has a
    hole in its history; prices before it belong to another market and must not
    feed indicators or a backtest that holds through the hole.
    """
    if len(frame) < 2:
        return frame
    gaps = frame["Date"].diff().dt.days
    breaks = np.flatnonzero(gaps.to_numpy() > max_gap_days)
    if breaks.size == 0:
        return frame
    return frame.iloc[int(breaks[-1]):].reset_index(drop=True)


def merge(old: pd.DataFrame | None, new: pd.DataFrame | None) -> pd.DataFrame:
    """``old`` updated by ``new`` (a day in both takes ``new``'s row)."""
    parts = [f for f in (old, new) if f is not None and not f.empty]
    if not parts:
        return empty_frame()
    return clean(pd.concat(parts, ignore_index=True))
