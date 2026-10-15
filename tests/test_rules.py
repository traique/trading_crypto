from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from cryptotrader.market import rules
from cryptotrader.market.rules import SymbolFilters


class TestFinalDay:
    def test_before_and_after_the_grace_minutes(self):
        assert rules.final_day(datetime(2026, 10, 15, 0, 4, tzinfo=UTC)) == date(2026, 10, 13)
        assert rules.final_day(datetime(2026, 10, 15, 0, 5, tzinfo=UTC)) == date(2026, 10, 14)
        assert rules.final_day(datetime(2026, 10, 15, 23, 59, tzinfo=UTC)) == date(2026, 10, 14)

    def test_other_timezones_are_converted_to_utc(self):
        # 07:30 in Vietnam (UTC+7) is 00:30 UTC: yesterday's candle is final.
        vn = timezone(timedelta(hours=7))
        assert rules.final_day(datetime(2026, 10, 15, 7, 30, tzinfo=vn)) == date(2026, 10, 14)
        # 06:59 in Vietnam is 23:59 UTC of the previous day.
        assert rules.final_day(datetime(2026, 10, 15, 6, 59, tzinfo=vn)) == date(2026, 10, 13)

    def test_naive_datetime_is_utc_not_local(self):
        assert rules.final_day(datetime(2026, 10, 15, 0, 10)) == date(2026, 10, 14)

    def test_candle_close_time(self):
        assert rules.candle_close_time(date(2026, 10, 14)) == datetime(2026, 10, 15, tzinfo=UTC)


class TestRounding:
    def test_floor_to_step_is_exact(self):
        assert rules.floor_to_step(0.3, 0.1) == pytest.approx(0.3)
        assert rules.floor_to_step(0.1 + 0.2, 0.1) == pytest.approx(0.3)
        assert rules.floor_to_step(1.23456789, 0.001) == 1.234
        assert rules.floor_to_step(123.9, 1) == 123
        assert rules.floor_to_step(0.000019, 0.00001) == pytest.approx(0.00001)

    def test_floor_to_step_degenerate(self):
        assert rules.floor_to_step(-1, 0.1) == 0.0
        assert rules.floor_to_step(float("nan"), 0.1) == 0.0
        assert rules.floor_to_step(1.5, 0) == 1.5

    def test_price_rounds_against_the_trader(self):
        assert rules.round_price_against(100.004, "BUY", 0.01) == pytest.approx(100.01)
        assert rules.round_price_against(100.004, "SELL", 0.01) == pytest.approx(100.0)
        assert rules.round_price_against(100.0, "BUY", 0.01) == 100.0

    def test_slipped(self):
        flt = SymbolFilters(tick_size=0.01)
        assert rules.slipped(100.0, "BUY", flt, 0.1) == pytest.approx(100.10)
        assert rules.slipped(100.0, "SELL", flt, 0.1) == pytest.approx(99.90)
        assert rules.slipped(100.005, "BUY", flt, 0.0) == pytest.approx(100.01)


class TestFilters:
    INFO = {"symbol": "BTCUSDT", "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01000000"},
        {"filterType": "LOT_SIZE", "minQty": "0.00001000", "maxQty": "9000.0", "stepSize": "0.00001000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
    ]}

    def test_from_exchange_info(self):
        f = SymbolFilters.from_exchange_info(self.INFO)
        assert (f.tick_size, f.step_size, f.min_qty, f.min_notional, f.max_qty) == (0.01, 0.00001, 0.00001, 5.0, 9000.0)

    def test_old_min_notional_filter_and_missing_filters(self):
        f = SymbolFilters.from_exchange_info({"filters": [{"filterType": "MIN_NOTIONAL", "minNotional": "10"}]})
        assert f.min_notional == 10.0
        assert f.step_size == rules.DEFAULT_FILTERS.step_size

    def test_round_trip(self):
        f = SymbolFilters.from_exchange_info(self.INFO)
        assert SymbolFilters.from_dict(f.to_dict()) == f

    def test_order_rejection(self):
        f = SymbolFilters.from_exchange_info(self.INFO)
        assert rules.order_rejection(0.0001, 60000, f) is None
        assert "tối thiểu" in rules.order_rejection(0.000001, 60000, f)
        assert "USDT" in rules.order_rejection(0.00005, 60000, f)      # 3 USDT < 5
        assert rules.order_rejection(0, 60000, f)
        assert rules.order_rejection(1, float("nan"), f)

    def test_defaults_are_conservative(self):
        assert rules.DEFAULT_FILTERS.min_notional >= 5.0


class TestCosts:
    def test_defaults(self):
        assert rules.DEFAULT_TAKER_FEE_PCT == 0.1
        assert rules.DEFAULT_SLIPPAGE_PCT == 0.1
        assert rules.round_trip_cost_pct(0.1, 0.1) == pytest.approx(0.4)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("X_FEE", "0.075")
        assert rules._env_pct("X_FEE", 0.1) == 0.075
        monkeypatch.setenv("X_FEE", "junk")
        assert rules._env_pct("X_FEE", 0.1) == 0.1
        monkeypatch.setenv("X_FEE", "-1")
        assert rules._env_pct("X_FEE", 0.1) == 0.1


class TestUniverse:
    @pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT", "JUPUSDT", "SUPERUSDT", "1000SATSUSDT", "PEPEUSDT"])
    def test_tradable(self, symbol):
        assert rules.exclusion_reason(symbol) is None

    @pytest.mark.parametrize("symbol,word", [
        ("USDCUSDT", "stablecoin"), ("FDUSDUSDT", "stablecoin"), ("TUSDUSDT", "stablecoin"),
        ("BUSDUSDT", "stablecoin"), ("DAIUSDT", "stablecoin"), ("EURUSDT", "stablecoin"),
        ("TRYUSDT", "stablecoin"), ("USDPUSDT", "stablecoin"),
        ("BTCUPUSDT", "đòn bẩy"), ("ETHDOWNUSDT", "đòn bẩy"), ("BULLUSDT", "đòn bẩy"),
        ("ETHBEARUSDT", "đòn bẩy"), ("WBTCUSDT", "bọc"), ("BETHUSDT", "bọc"), ("PAXGUSDT", "bọc"),
        ("BTCEUR", "pháp định"), ("BTCTRY", "pháp định"), ("ETHBTC", "USDT"), ("BTCFDUSD", "USDT"),
    ])
    def test_excluded(self, symbol, word):
        reason = rules.exclusion_reason(symbol)
        assert reason is not None and word in reason

    def test_exchange_info_assets_take_precedence(self):
        assert rules.is_tradable_pair("XUSDT", "X", "USDT")
        assert not rules.is_tradable_pair("WHATEVER", "USDC", "USDT")

    def test_split_pair(self):
        assert rules.split_pair("BTCUSDT") == ("BTC", "USDT")
        assert rules.split_pair("BTCFDUSD") == ("BTC", "FDUSD")
        assert rules.split_pair("ethbtc") == ("ETH", "BTC")
