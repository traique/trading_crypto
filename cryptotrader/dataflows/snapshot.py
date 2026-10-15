"""Deterministic market data for the agents: a price table and a verified snapshot.

The market analyst is an LLM that can confabulate exact numbers. The snapshot
(latest closed daily candle on or before the run date, a fixed indicator set,
recent closes) is computed here, with no LLM, and the analyst is told to treat
it as the source of truth for any exact figure.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from cryptotrader.data.errors import NoMarketDataError
from cryptotrader.data.symbols import normalize_symbol
from cryptotrader.dataflows import fmt, indicators

DEFAULT_SNAPSHOT_INDICATORS: tuple[str, ...] = (
    "close_10_ema", "close_50_sma", "close_200_sma",
    "rsi", "boll", "boll_ub", "boll_lb",
    "macd", "macds", "macdh", "atr",
)


def _rows(symbol: str, as_of_date: str) -> pd.DataFrame:
    """Closed candles on or before ``as_of_date``, re-cut here: a verification path trusts no input."""
    pair = normalize_symbol(symbol)
    data = indicators.load_frame(pair, as_of_date)
    if data is None or data.empty:
        raise NoMarketDataError(pair, "no candles")
    df = data.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df[df["Date"] <= pd.Timestamp(as_of_date)].sort_values("Date").reset_index(drop=True)
    if df.empty:
        raise NoMarketDataError(pair, f"no candles on or before {as_of_date}")
    return df


def price_table(symbol: str, start_date: str, end_date: str) -> str:
    """Daily OHLCV of ``symbol`` in ``[start_date, end_date]`` as a Markdown table."""
    pair = normalize_symbol(symbol)
    df = _rows(pair, end_date)
    window = df[df["Date"] >= pd.Timestamp(start_date)]
    if window.empty:
        return f"Không có nến ngày nào của {pair} từ {start_date} đến {end_date}."
    lines = [f"## Nến ngày {pair} (UTC, đã đóng) từ {start_date} đến {end_date}", "",
             "| Ngày | Open | High | Low | Close | Volume | Quote volume (USDT) |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for _, row in window.iterrows():
        qv = row.get("QuoteVolume")
        lines.append(f"| {row['Date']:%Y-%m-%d} | {fmt.price(row['Open'])} | {fmt.price(row['High'])} | "
                     f"{fmt.price(row['Low'])} | {fmt.price(row['Close'])} | {fmt.number(row['Volume'], 2)} | "
                     f"{fmt.usd_short(qv)} |")
    return "\n".join(lines)


def build_verified_market_snapshot(symbol: str, as_of_date: str, look_back_days: int = 30,
                                   names: Iterable[str] | None = None) -> str:
    """Ground truth: latest closed candle, indicators at that candle, recent closes."""
    pair = normalize_symbol(symbol)
    df = _rows(pair, as_of_date)
    values: dict[str, str] = {}
    for name in tuple(names or DEFAULT_SNAPSHOT_INDICATORS):
        try:
            values[name] = fmt.price(indicators.compute(df, name).iloc[-1])
        except Exception as exc:  # noqa: BLE001 - one bad indicator must not sink the snapshot
            values[name] = f"N/A ({type(exc).__name__})"
    latest = df.iloc[-1]
    recent = df.tail(max(1, min(int(look_back_days), 30)))
    closes = df["Close"].astype(float)
    lines = [
        f"## Verified market data snapshot for {pair}",
        "",
        f"- Requested analysis date: {as_of_date}",
        f"- Latest closed daily candle used (UTC): {latest['Date']:%Y-%m-%d}",
        "- Price unit: USDT. Candles close at 00:00 UTC; a candle still forming is excluded.",
        "",
        "### Latest verified candle",
        "",
        "| Field | Value |",
        "|---|---:|",
    ]
    for field in ("Open", "High", "Low", "Close"):
        lines.append(f"| {field} | {fmt.price(latest.get(field))} |")
    lines.append(f"| Volume | {fmt.number(latest.get('Volume'), 2)} |")
    lines.append(f"| Quote volume (USDT) | {fmt.usd_short(latest.get('QuoteVolume'))} |")
    changes = []
    for days in (1, 7, 30, 90):
        if len(closes) > days and closes.iloc[-1 - days] > 0:
            changes.append(f"| {days}d change | {fmt.signed_pct((closes.iloc[-1] / closes.iloc[-1 - days] - 1) * 100)} |")
    lines += changes
    lines += ["", "### Verified technical indicators (latest candle)", "",
              "| Indicator | Value |", "|---|---:|"]
    lines += [f"| {name} | {value} |" for name, value in values.items()]
    lines += ["", f"### Recent verified closes (last {len(recent)} candles)", "",
              "| Date | Close |", "|---|---:|"]
    lines += [f"| {row['Date']:%Y-%m-%d} | {fmt.price(row['Close'])} |" for _, row in recent.iterrows()]
    lines += ["", "Use this snapshot as the source of truth for exact OHLCV, price-level and "
              "indicator-value claims. If another tool output conflicts with it, flag the "
              "discrepancy rather than inventing a reconciled number."]
    return "\n".join(lines)
