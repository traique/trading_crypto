import pytest

from cryptotrader.data.symbols import base_asset, normalize_symbol, safe_file_component


@pytest.mark.parametrize("raw", ["BTC", "btc", "BTCUSDT", "btcusdt", "BTC/USDT", "btc-usdt", "BTC_USDT", " BTC / USDT "])
def test_normalize(raw):
    assert normalize_symbol(raw) == "BTCUSDT"


def test_numeric_prefix():
    assert normalize_symbol("1000sats") == "1000SATSUSDT"


@pytest.mark.parametrize("raw", ["", "USDT", "ETH/BTC", "BTC/USDT/X", "BT$C", "../etc", None])
def test_rejected(raw):
    with pytest.raises(ValueError):
        normalize_symbol(raw)


def test_base_and_file_component():
    assert base_asset("eth/usdt") == "ETH"
    assert safe_file_component("BTCUSDT") == "BTCUSDT"
    with pytest.raises(ValueError):
        safe_file_component("../x")
