from datetime import date

import pytest

from cryptotrader.data import binance
from cryptotrader.data.errors import UnknownSymbolError, VendorUnavailableError
from cryptotrader.data.net import HttpResult
from tests.helpers import kline_rows

INFO = {"symbols": [
    {"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT", "isSpotTradingAllowed": True,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                 {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
                 {"filterType": "NOTIONAL", "minNotional": "5"}]},
    {"symbol": "ETHUSDT", "status": "TRADING", "baseAsset": "ETH", "quoteAsset": "USDT", "filters": []},
    {"symbol": "USDCUSDT", "status": "TRADING", "baseAsset": "USDC", "quoteAsset": "USDT", "filters": []},
    {"symbol": "BTCUPUSDT", "status": "BREAK", "baseAsset": "BTCUP", "quoteAsset": "USDT", "filters": []},
    {"symbol": "OLDUSDT", "status": "BREAK", "baseAsset": "OLD", "quoteAsset": "USDT", "filters": []},
    {"symbol": "ETHBTC", "status": "TRADING", "baseAsset": "ETH", "quoteAsset": "BTC", "filters": []},
    {"broken": True},
]}


def test_parse_exchange_info_and_universe():
    info = binance.parse_exchange_info(INFO)
    assert set(info) == {"BTCUSDT", "ETHUSDT", "USDCUSDT", "BTCUPUSDT", "OLDUSDT", "ETHBTC"}
    assert info["BTCUSDT"].filters.min_notional == 5
    assert binance.tradable_symbols(info) == ["BTCUSDT", "ETHUSDT"]
    tickers = binance.parse_tickers([
        {"symbol": "BTCUSDT", "lastPrice": "80000", "priceChangePercent": "-1", "quoteVolume": "2e9", "count": 5},
        {"symbol": "ETHUSDT", "lastPrice": "3000", "priceChangePercent": "1", "quoteVolume": "3e9", "count": 5},
        {"symbol": "BAD"},
    ])
    assert binance.tradable_symbols(info, tickers) == ["ETHUSDT", "BTCUSDT"]
    assert binance.tradable_symbols(info, tickers, min_quote_volume=2.5e9) == ["ETHUSDT"]


def test_klines_paginate(monkeypatch):
    rows = kline_rows(date(2020, 1, 1), 2500)
    calls = []

    def fake(url, params=None, **kw):
        calls.append(params["startTime"])
        page = [r for r in rows if params["startTime"] <= r[0] <= params["endTime"]][: params["limit"]]
        return HttpResult(200, __import__("json").dumps(page).encode(), url)

    monkeypatch.setattr(binance, "http_get", fake)
    f = binance.fetch_klines("BTCUSDT", None, date(2026, 12, 31))
    assert len(f) == 2500 and len(calls) == 3
    assert f["Date"].is_monotonic_increasing and not f["Date"].duplicated().any()


def test_unknown_symbol(monkeypatch):
    monkeypatch.setattr(binance, "http_get", lambda url, params=None, **kw: HttpResult(
        400, b'{"code":-1121,"msg":"Invalid symbol."}', url))
    with pytest.raises(UnknownSymbolError):
        binance.fetch_klines("LUNAUSDT", None, date(2026, 1, 1))


def test_exchange_info_cache_and_fallback(monkeypatch):
    monkeypatch.setattr(binance, "http_get", lambda url, params=None, **kw: HttpResult(
        200, __import__("json").dumps(INFO).encode(), url))
    info = binance.load_exchange_info()
    assert info["BTCUSDT"].filters.step_size == 0.00001

    def down(*a, **k):
        raise VendorUnavailableError("down")

    monkeypatch.setattr(binance, "http_get", down)
    stale = binance.load_exchange_info(max_age=0)           # cache too old, source down: still served
    assert stale["BTCUSDT"].filters.step_size == 0.00001
    assert binance.symbol_filters("NEWUSDT", stale) == binance.rules.DEFAULT_FILTERS
