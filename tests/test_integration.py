"""Real-network checks of the data sources. Run with: pytest -m integration"""

from datetime import date, timedelta

import pandas as pd
import pytest

from cryptotrader.data import archive, binance, ohlcv, okx
from cryptotrader.market import rules

pytestmark = pytest.mark.integration


def test_load_btc_from_binance():
    f = ohlcv.load_ohlcv("BTC")
    last = rules.final_day()
    assert f["Date"].iloc[-1] == pd.Timestamp(last)                # closed candles only, up to date
    assert f["Date"].iloc[0] <= pd.Timestamp("2017-09-01")          # full history (listed 2017-08-17)
    assert (f["High"] >= f["Low"]).all() and (f["Close"] > 0).all()
    assert f.attrs["source"] == "binance"
    info = binance.fetch_exchange_info(["BTCUSDT"])
    assert info["BTCUSDT"].filters.step_size > 0 and info["BTCUSDT"].filters.min_notional > 0


def test_delisted_pair_from_the_archive():
    assert "LUNAUSDT" in archive.list_symbols()
    f = archive.fetch_klines("LUNAUSDT", date(2022, 4, 1), date(2022, 6, 30))
    assert f["Date"].iloc[0] == pd.Timestamp("2022-04-01")
    assert f["Close"].min() < 1                                     # the May 2022 collapse is in the data


def test_okx_matches_binance_closely():
    end = rules.final_day()
    start = end - timedelta(days=10)
    o = okx.fetch_klines("BTCUSDT", start, end).set_index("Date")["Close"]
    b = binance.fetch_klines("BTCUSDT", start, end).set_index("Date")["Close"]
    both = pd.concat([o, b], axis=1, keys=["okx", "binance"]).dropna()
    assert len(both) >= 9
    assert ((both["okx"] / both["binance"] - 1).abs() < 0.01).all()
