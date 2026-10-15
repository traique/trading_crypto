"""The data tools the analysts call.

Each dated tool takes the run's ``trade_date`` from graph state
(``InjectedState``) and never serves data past it, whatever date the model asks
for. A source failure comes back as text the model can report, never as an
exception that would end the run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from cryptotrader.data.errors import NoMarketDataError, VendorError, VendorUnavailableError
from cryptotrader.dataflows import indicators, news, snapshot
from cryptotrader.dataflows.date_window import as_of, as_of_window


def _served(fn: Callable[[], str], what: str) -> str:
    try:
        return fn()
    except VendorUnavailableError as exc:
        return f"<{what} unavailable: the source did not answer ({exc}). This is a gap, not an absence of data.>"
    except NoMarketDataError as exc:
        return f"<{what}: no data ({exc})>"
    except VendorError as exc:
        return f"<{what} failed: {exc}>"
    except ValueError as exc:
        return f"<{what}: {exc}>"


@tool
def get_price_data(
    symbol: Annotated[str, InjectedState("company_of_interest")],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve daily OHLCV candles (UTC days, closed candles only) of the Binance spot USDT pair under
    analysis, prices in USDT.
    Args:
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A Markdown table of the candles in the window.
    """
    try:
        start_date, end_date = as_of_window(start_date, end_date, trade_date)
    except ValueError as exc:
        return str(exc)
    return _served(lambda: snapshot.price_table(symbol, start_date, end_date), "price data")


@tool
def get_indicators(
    symbol: Annotated[str, InjectedState("company_of_interest")],
    indicator: Annotated[str, "technical indicator name, e.g. rsi"],
    curr_date: Annotated[str, "The analysis date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"] = 30,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve one technical indicator for the pair under analysis, computed on closed daily candles.
    Supported: close_10_ema, close_20_ema, close_20_sma, close_50_sma, close_200_sma, macd, macds, macdh,
    rsi, boll, boll_ub, boll_lb, atr, vwma, mfi.
    Args:
        indicator (str): One indicator name, e.g. 'rsi'. Call once per indicator.
        curr_date (str): The analysis date, YYYY-mm-dd
        look_back_days (int): How many days to look back, default 30
    Returns:
        str: The indicator's value for each closed candle in the window.
    """
    curr_date = as_of(curr_date, trade_date)
    names = [i.strip().lower() for i in str(indicator).split(",") if i.strip()]
    return "\n\n".join(
        _served(lambda n=n: indicators.get_indicator_window(symbol, n, curr_date, look_back_days), n)
        for n in names) or "No indicator named."


@tool
def get_verified_market_snapshot(
    symbol: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "the analysis date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "number of recent candles to include"] = 30,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """Deterministic verification snapshot for exact market-data claims.

    Returns the latest closed daily candle on or before curr_date, common technical indicators,
    price changes and recent closes. Call this before making exact claims about price levels or
    indicator values, and treat it as the source of truth.
    """
    return _served(lambda: snapshot.build_verified_market_snapshot(symbol, as_of(curr_date, trade_date),
                                                                   look_back_days), "market snapshot")


@tool
def get_news(
    symbol: Annotated[str, InjectedState("company_of_interest")],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve news about the coin under analysis (Google News in English and Vietnamese, CoinDesk,
    Cointelegraph), published before the close of the analysis date. Items marked ✅ name the coin;
    ⚠️ items are market-wide news.
    Args:
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: The dated headlines.
    """
    try:
        start_date, end_date = as_of_window(start_date, end_date, trade_date)
    except ValueError as exc:
        return str(exc)
    return _served(lambda: news.get_news(symbol, start_date, end_date), "news")


@tool
def get_global_news(
    curr_date: Annotated[str, "The analysis date in yyyy-mm-dd format"],
    look_back_days: Annotated[int | None, "Days to look back; omit for the configured default"] = None,
    limit: Annotated[int | None, "Max articles; omit for the configured default"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve market-wide crypto and macro headlines (ETF flows, Fed and rates, regulation, exchange
    incidents) published before the close of the analysis date.
    Args:
        curr_date (str): The analysis date in yyyy-mm-dd format
        look_back_days (int): Days to look back; omit to inherit config
        limit (int): Maximum number of articles; omit to inherit config
    Returns:
        str: The dated headlines.
    """
    return _served(lambda: news.get_global_news(as_of(curr_date, trade_date), look_back_days, limit),
                   "market news")
