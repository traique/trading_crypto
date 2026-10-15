"""Shared fixtures of the bot tests: settings, a fixed clock, a fake market (no network).

The clock is fixed per test (UTC); the candles come from a table the test can
edit (``market.rows``), served through a stand-in for ``ohlcv.load_ohlcv`` that
honours ``end_date`` and drops days after it, like the real loader.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from bot import clock, settings as settings_module
from cryptotrader.data.errors import NoMarketDataError, VendorUnavailableError
from cryptotrader.market.rules import SymbolFilters

BTC = "BTCUSDT"
ETH = "ETHUSDT"
FILTERS = SymbolFilters(tick_size=0.01, step_size=0.00001, min_qty=0.00001, min_notional=5.0)
FIRST = date(2026, 1, 1)
LAST = date(2026, 10, 14)       # the last closed candle at the default clock

ENV = ("TELEGRAM_TOKEN", "TELEGRAM_ALLOWED_IDS", "ALLOWED_USER_ID", "WEB_PASSWORD", "PUBLIC_URL",
       "TELEGRAM_REPORT_CHAT_IDS", "TRADE_TIME", "TRADE_MODE", "SIGNAL_STRATEGY", "PAPER_CAPITAL",
       "DATA_DIR", "MAX_PENDING", "KEEP_REPORTS", "ROUTER9_API_KEY", "ROUTER9_BASE_URL", "GROQ_API_KEY",
       "OPENROUTER_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
       "DEEPSEEK_API_KEY")


def utc(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def days(first: date, last: date) -> list[str]:
    n = (last - first).days + 1
    return [(first + timedelta(days=i)).isoformat() for i in range(n)]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)          # no .env of the developer's in the way
    monkeypatch.setenv("CRYPTO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TELEGRAM_ALLOWED_IDS", "111,222")
    return settings_module.load()


@pytest.fixture
def at(monkeypatch):
    """The clock: 2026-10-15 00:30 UTC (07:30 Vietnam time); the last closed candle is 2026-10-14."""
    state = {"now": utc(2026, 10, 15, 0, 30)}
    monkeypatch.setattr(clock, "now_utc", lambda: state["now"])
    return state


class FakeMarket:
    """Candles per (symbol, day) as (open, high, low, close, volume)."""

    def __init__(self):
        self.rows: dict[tuple[str, str], tuple] = {}
        self.loads: list[tuple[str, str]] = []
        self.down: set[str] = set()        # symbols whose source does not answer
        self.gate: threading.Event | None = None
        for i, day in enumerate(days(FIRST, LAST + timedelta(days=10))):
            c = 20_000.0 + 100 * i                        # a steady rise: above any moving average
            self.rows[(BTC, day)] = (c, c + 300, c - 300, c, 1000.0)
            e = 1_000.0 + i
            self.rows[(ETH, day)] = (e, e + 10, e - 10, e, 5000.0)

    def close(self, symbol: str, day: str) -> float:
        return self.rows[(symbol, day)][3]

    def load(self, symbol, end_date=None, start=None, *, now=None, **kwargs):
        if self.gate is not None:
            self.gate.wait(5)
        end = str(end_date)[:10] if end_date is not None else clock.final_day()
        end = min(end, clock.final_day())
        self.loads.append((symbol, end))
        if symbol in self.down:
            raise VendorUnavailableError(f"{symbol}: source down")
        out = [{"Date": pd.Timestamp(d), "Open": v[0], "High": v[1], "Low": v[2], "Close": v[3], "Volume": v[4],
                "QuoteVolume": v[3] * v[4], "Trades": 10.0}
               for (s, d), v in sorted(self.rows.items()) if s == symbol and d <= end]
        if not out:
            raise NoMarketDataError(symbol, "không có dữ liệu")
        frame = pd.DataFrame(out)
        frame.attrs["symbol"] = symbol
        return frame


@pytest.fixture
def market(monkeypatch, at):
    from cryptotrader.data import binance, ohlcv

    fake = FakeMarket()
    monkeypatch.setattr(ohlcv, "load_ohlcv", fake.load)
    info = {s: SimpleNamespace(filters=FILTERS) for s in (BTC, ETH)}
    monkeypatch.setattr(binance, "load_exchange_info", lambda *a, **k: info)
    return fake


@pytest.fixture
def trader(settings, market):
    from bot.trader import TradingBot

    return TradingBot(settings)


@pytest.fixture
def signal_trader(settings, market):
    from bot.trader import TradingBot

    object.__setattr__(settings, "trade_mode", "signal")
    return TradingBot(settings)
