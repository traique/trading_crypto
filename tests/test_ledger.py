import json
import math

import pytest

from cryptotrader.market.rules import SymbolFilters
from cryptotrader.trading.ledger import Bar, ExitRules, Ledger, LedgerLoadError, Order

BTC = "BTCUSDT"
FLT = SymbolFilters(tick_size=0.01, step_size=0.00001, min_qty=0.00001, min_notional=5.0)


def book(cash=10_000.0) -> Ledger:
    led = Ledger(cash=cash, start_cash=cash, fee_pct=0.1, slippage_pct=0.1)
    led.filters[BTC] = FLT
    return led


def bar(day, o, h, low, c, volume=None) -> Bar:
    return Bar(day, o, h, low, c, volume)


def buy(led, qty=0.1, created="2026-01-01", **kw):
    order = led.submit(Order(BTC, "BUY", qty, kw.pop("limit", None), created, **kw))
    assert order is not None
    return order


class TestMarketFills:
    def test_buy_then_sell_exact_fees_and_slippage(self):
        led = book()
        buy(led)
        fills = led.process_day({BTC: bar("2026-01-02", 50_000, 51_000, 49_000, 50_500)})
        assert len(fills) == 1
        f = fills[0]
        assert f.price == pytest.approx(50_050.00)               # open + 0.1%, onto the tick
        assert f.qty == pytest.approx(0.1)
        assert f.fee == pytest.approx(5.005)
        assert led.cash == pytest.approx(10_000 - 5_005 - 5.005)
        pos = led.positions[BTC]
        assert pos.avg_price == pytest.approx(5_010.005 / 0.1)
        led.end_of_day("2026-01-02", benchmark=50_500)
        assert led.history[-1]["nav"] == pytest.approx(led.cash + 0.1 * 50_500, abs=0.01)

        assert led.submit(Order(BTC, "SELL", 1.0, None, "2026-01-02")) is not None   # capped at the holding
        fills = led.process_day({BTC: bar("2026-01-03", 52_000, 52_500, 51_500, 52_100)})
        s = fills[0]
        assert s.price == pytest.approx(51_948.00)
        assert s.fee == pytest.approx(5.1948)
        assert s.pnl == pytest.approx(5_194.8 - 5.1948 - 5_010.005)
        assert led.cash == pytest.approx(10_000 + s.pnl)
        assert BTC not in led.positions

    def test_no_fill_on_the_decision_candle(self):
        led = book()
        buy(led, created="2026-01-02")
        assert led.process_day({BTC: bar("2026-01-02", 50_000, 51_000, 49_000, 50_500)}) == []
        led.end_of_day("2026-01-02")
        assert len(led.orders) == 1                                  # still good for the next candle
        assert len(led.process_day({BTC: bar("2026-01-03", 50_000, 51_000, 49_000, 50_500)})) == 1

    def test_unfilled_order_expires_after_its_day(self):
        led = book()
        buy(led, limit=40_000)
        led.process_day({BTC: bar("2026-01-02", 50_000, 51_000, 49_000, 50_500)})
        led.end_of_day("2026-01-02")
        assert led.orders == []

    def test_slippage_is_clamped_into_the_candle(self):
        led = book()
        buy(led, qty=1)
        f = led.process_day({BTC: bar("2026-01-02", 100.0, 100.05, 99.0, 100.0)})[0]
        assert f.price == pytest.approx(100.05)

    def test_quantity_floored_to_step(self):
        led = book()
        order = buy(led, qty=0.123456789)
        assert order.qty == pytest.approx(0.12345)
        f = led.process_day({BTC: bar("2026-01-02", 50_000, 51_000, 49_000, 50_500)})[0]
        assert f.qty == pytest.approx(0.12345)
        assert math.isclose(f.qty / FLT.step_size, round(f.qty / FLT.step_size), abs_tol=1e-6)

    def test_buy_capped_by_cash(self):
        led = book(cash=1_000)
        buy(led, qty=1)
        f = led.process_day({BTC: bar("2026-01-02", 50_000, 51_000, 49_000, 50_500)})[0]
        assert f.qty == pytest.approx(0.01996)
        assert led.cash >= 0


class TestMinimums:
    def test_min_notional_rejected_at_submit(self):
        led = book()
        assert led.submit(Order(BTC, "BUY", 0.00005, None, "2026-01-01", ref=50_000)) is None   # 2.5 USDT
        assert "USDT" in led.meta["rejections"][-1]["reason"]

    def test_min_notional_rejected_at_fill_when_cash_is_short(self):
        led = book(cash=3)
        buy(led, qty=0.1)
        assert led.process_day({BTC: bar("2026-01-02", 50_000, 51_000, 49_000, 50_500)}) == []
        assert "không đủ tiền" in led.meta["rejections"][-1]["reason"]

    def test_duplicate_orders_refused(self):
        led = book()
        buy(led)
        assert led.submit(Order(BTC, "BUY", 0.2, None, "2026-01-01")) is None
        assert len(led.orders) == 1

    def test_sell_without_position_refused(self):
        assert book().submit(Order(BTC, "SELL", 0.1, None, "2026-01-01")) is None

    def test_partial_sell_leaving_dust_sells_everything(self):
        led = book()
        buy(led, qty=0.001)
        led.process_day({BTC: bar("2026-01-02", 50_000, 51_000, 49_000, 50_000)})
        held = led.positions[BTC].qty
        order = led.submit(Order(BTC, "SELL", held - 0.00005, None, "2026-01-02", ref=50_000))
        assert order.qty == pytest.approx(held)                       # 0.00005 BTC = 2.5 USDT of dust


class TestLimits:
    def test_buy_limit_must_trade_through(self):
        led = book()
        buy(led, limit=49_000)
        assert led.process_day({BTC: bar("2026-01-02", 50_000, 51_000, 49_000, 50_500)}) == []   # touch only

        led = book()
        buy(led, limit=49_000)
        f = led.process_day({BTC: bar("2026-01-02", 50_000, 51_000, 48_999.99, 50_500)})[0]
        assert f.price == 49_000

    def test_marketable_buy_limit_fills_at_the_open_never_above_limit(self):
        led = book()
        buy(led, limit=49_000)
        f = led.process_day({BTC: bar("2026-01-02", 48_000, 49_500, 47_000, 49_000)})[0]
        assert f.price == pytest.approx(48_048.00)
        led = book()
        buy(led, limit=48_010)
        f = led.process_day({BTC: bar("2026-01-02", 48_000, 49_500, 47_000, 49_000)})[0]
        assert f.price == 48_010

    def test_sell_limit_must_trade_through(self):
        led = book()
        buy(led)
        led.process_day({BTC: bar("2026-01-02", 50_000, 51_000, 49_000, 50_000)})
        led.submit(Order(BTC, "SELL", 0.1, 55_000, "2026-01-02"))
        assert led.process_day({BTC: bar("2026-01-03", 50_000, 55_000, 49_000, 54_000)}) == []
        led.submit(Order(BTC, "SELL", 0.1, 55_000, "2026-01-03"))
        assert led.process_day({BTC: bar("2026-01-04", 54_000, 55_000.01, 53_000, 54_000)})[0].price == 55_000


class TestExits:
    def _held(self, **kw):
        led = book()
        buy(led, **kw)
        led.process_day({BTC: bar("2026-01-02", 50_000, 50_100, 49_900, 50_000)})
        return led

    def test_stop_at_the_stop_and_at_the_open_on_a_gap(self):
        led = self._held(stop=45_000)
        f = led.process_day({BTC: bar("2026-01-03", 46_000, 46_500, 44_000, 44_500)})[0]
        assert f.price == pytest.approx(44_955.00) and f.reason == "chạm stop"
        led = self._held(stop=45_000)
        f = led.process_day({BTC: bar("2026-01-03", 43_000, 43_500, 42_000, 43_000)})[0]
        assert f.price == pytest.approx(42_957.00)

    def test_stop_can_trigger_on_the_buy_day(self):
        led = book()
        buy(led, stop_pct=5)
        fills = led.process_day({BTC: bar("2026-01-02", 50_000, 50_500, 47_000, 48_000)})
        assert [f.side for f in fills] == ["BUY", "SELL"]
        assert fills[1].price == pytest.approx(47_499.95)
        assert BTC not in led.positions

    def test_no_target_on_the_buy_day(self):
        led = book()
        buy(led, target1=50_500)
        fills = led.process_day({BTC: bar("2026-01-02", 50_000, 52_000, 49_900, 51_000)})
        assert [f.side for f in fills] == ["BUY"]

    def test_targets_and_breakeven(self):
        led = self._held(stop=45_000, target1=55_000, target2=60_000)
        fills = led.process_day({BTC: bar("2026-01-03", 54_000, 56_000, 53_000, 55_500)})
        assert len(fills) == 1 and fills[0].price == 55_000 and fills[0].qty == pytest.approx(0.05)
        pos = led.positions[BTC]
        assert pos.t1_done and pos.stop == pytest.approx(pos.avg_price)
        fills = led.process_day({BTC: bar("2026-01-04", 61_000, 62_000, 60_500, 61_500)})
        assert fills[0].price == pytest.approx(60_939.00)              # gapped over T2: the open, slipped
        assert BTC not in led.positions

    def test_stop_assumed_before_target_on_the_same_candle(self):
        led = self._held(stop=45_000, target1=55_000)
        fills = led.process_day({BTC: bar("2026-01-03", 50_000, 56_000, 44_000, 50_000)})
        assert len(fills) == 1 and fills[0].reason == "chạm stop"

    def test_trailing_stop_follows_the_highest_close(self):
        led = self._held(trail_pct=10)
        led.process_day({BTC: bar("2026-01-03", 55_000, 60_500, 54_000, 60_000)})
        assert led.positions[BTC].stop == pytest.approx(54_000)
        fills = led.process_day({BTC: bar("2026-01-04", 58_000, 58_500, 53_000, 53_500)})
        assert fills[0].reason == "chạm trailing stop"

    def test_time_stop(self):
        led = self._held()
        rules = ExitRules(max_hold_days=2)
        assert led.process_day({BTC: bar("2026-01-03", 50_000, 50_100, 49_900, 50_000)}, exit_rules=rules) == []
        fills = led.process_day({BTC: bar("2026-01-04", 50_000, 50_100, 49_900, 50_000)}, exit_rules=rules)
        assert fills and "2 ngày" in fills[0].reason

    def test_untradable_candles_are_skipped(self):
        led = self._held(stop=45_000)
        nan = float("nan")
        assert led.process_day({BTC: bar("2026-01-03", nan, nan, nan, nan)}) == []
        assert led.process_day({BTC: bar("2026-01-03", 50_000, 40_000, 45_000, 41_000)}) == []   # high < low
        assert led.process_day({BTC: bar("2026-01-03", 46_000, 46_500, 44_000, 44_500, volume=0)}) == []
        assert BTC in led.positions


class TestTargetWeights:
    def test_rebalance_in_and_out(self):
        led = book()
        orders = led.plan_target_weights("2026-01-01", {BTC: 0.5}, {BTC: 50_000})
        assert len(orders) == 1 and orders[0].side == "BUY"
        assert orders[0].qty == pytest.approx(0.0998)          # 5000 / (50000 * 1.001 * 1.001), floored
        led.process_day({BTC: bar("2026-01-02", 50_000, 50_100, 49_900, 50_000)})
        led.end_of_day("2026-01-02")
        assert led.weights({BTC: 50_000})[BTC] == pytest.approx(0.5, abs=0.002)
        assert led.plan_target_weights("2026-01-02", {BTC: 0.505}, {BTC: 50_000}) == []   # below 1% of NAV
        orders = led.plan_target_weights("2026-01-02", {}, {BTC: 50_000})
        assert orders[0].side == "SELL" and orders[0].qty == pytest.approx(led.positions[BTC].qty)

    def test_weights_are_cleaned(self):
        led = book()
        orders = led.plan_target_weights("2026-01-01", {BTC: 3.0, "ETHUSDT": -1, "XUSDT": float("nan")},
                                         {BTC: 50_000, "ETHUSDT": 3_000, "XUSDT": 1})
        assert [o.symbol for o in orders] == [BTC]
        assert orders[0].qty * 50_000 <= 10_000


class TestRecordFill:
    def test_buy_and_sell(self):
        led = book()
        led.record_fill("buy", "btcusdt", 0.1, 50_000, "2026-01-02", fee=4.0, stop=45_000, target1=55_000)
        assert led.cash == pytest.approx(10_000 - 5_004)
        pos = led.positions[BTC]
        assert pos.avg_price == pytest.approx(50_040) and pos.stop == 45_000
        f = led.record_fill("SELL", BTC, 0.05, 56_000, "2026-01-05")
        assert f.fee == pytest.approx(2.8)
        assert led.positions[BTC].t1_done and led.positions[BTC].stop == pytest.approx(50_040)

    @pytest.mark.parametrize("kwargs,msg", [
        ({"side": "HOLD"}, "MUA"), ({"qty": 0}, "Khối lượng"), ({"qty": float("nan")}, "Khối lượng"),
        ({"qty": True}, "Khối lượng"), ({"price": -1}, "Giá"), ({"day": "15/10/2026"}, "Ngày"),
        ({"qty": 1.0}, "Không đủ USDT"), ({"fee": -1}, "Phí"),
    ])
    def test_invalid(self, kwargs, msg):
        args = {"side": "BUY", "symbol": BTC, "qty": 0.1, "price": 50_000, "day": "2026-01-02", **kwargs}
        fee = args.pop("fee", None)
        with pytest.raises(ValueError, match=msg):
            book().record_fill(**args, fee=fee)

    def test_sell_more_than_held(self):
        led = book()
        led.record_fill("BUY", BTC, 0.1, 50_000, "2026-01-02")
        with pytest.raises(ValueError, match="không có đủ"):
            led.record_fill("SELL", BTC, 0.2, 50_000, "2026-01-03")


class TestStorage:
    def test_round_trip(self, tmp_path):
        led = book()
        buy(led, stop=45_000)
        led.process_day({BTC: bar("2026-01-02", 50_000, 51_000, 49_000, 50_500)})
        led.end_of_day("2026-01-02", 50_500)
        path = tmp_path / "paper.json"
        led.save(path)
        back = Ledger.load(path, 1)
        assert back.cash == pytest.approx(led.cash)
        assert back.positions[BTC].qty == pytest.approx(0.1) and back.history == led.history
        assert back.fee_pct == 0.1 and not list(tmp_path.glob("*.tmp"))

    def test_tolerant_from_dict(self):
        data = {"cash": 100, "positions": {BTC: {"symbol": BTC, "qty": 0.1, "avg_price": 50_000,
                                                 "buy_date": "2026-01-01", "future_field": 1}},
                "unknown": True}
        led = Ledger.from_dict(data)
        assert led.start_cash == 100 and led.positions[BTC].stop == 0.0

    @pytest.mark.parametrize("text", ["{not json", "[]", '{"cash": "nan"}', '{"positions": {}}',
                                      '{"cash": 1, "positions": {"X": {"symbol": "X", "qty": -1, "avg_price": 1, "buy_date": "d"}}}'])
    def test_damaged_file_is_moved_aside_and_raises(self, tmp_path, text):
        path = tmp_path / "real.json"
        path.write_text(text)
        with pytest.raises(LedgerLoadError):
            Ledger.load(path, 1000)
        assert not path.exists()
        aside = list(tmp_path.glob("real.json.corrupt-*"))
        assert len(aside) == 1 and aside[0].read_text() == text

    def test_fresh_on_error(self, tmp_path):
        path = tmp_path / "paper.json"
        path.write_text("{oops")
        led = Ledger.load(path, 500, fresh_on_error=True)
        assert led.cash == 500 and list(tmp_path.glob("paper.json.corrupt-*"))

    def test_missing_file_is_a_new_book(self, tmp_path):
        assert Ledger.load(tmp_path / "none.json", 250).cash == 250

    def test_saved_json_has_no_nan(self, tmp_path):
        led = book()
        led.end_of_day("2026-01-02", float("nan"))
        led.save(tmp_path / "x.json")
        assert json.loads((tmp_path / "x.json").read_text())["history"][0]["benchmark"] is None


def test_stats():
    led = book()
    buy(led)
    led.process_day({BTC: bar("2026-01-02", 50_000, 51_000, 49_000, 50_000)})
    led.end_of_day("2026-01-02", 50_000)
    led.submit(Order(BTC, "SELL", 0.1, None, "2026-01-02"))
    led.process_day({BTC: bar("2026-01-03", 55_000, 55_500, 54_000, 55_000)})
    led.end_of_day("2026-01-03", 55_000)
    s = led.stats()
    assert s["benchmark_pct"] == 10.0 and s["closed"] == 1 and s["win_rate_pct"] == 100.0
    assert s["fees_paid"] == pytest.approx(5.005 + 5.4945, abs=0.01)
    assert s["return_pct"] > 0
