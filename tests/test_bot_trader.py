"""The trading bot: what a run processes, how it resumes, the real book's commands.

No network: candles and exchange filters are faked (tests/test_bot_support.py);
the clock (UTC) is fixed per test.
"""

import json
import math
import threading
import time

import pytest

from bot import trader as trader_module
from bot.books import BookSpec
from tests import test_bot_support as support
from tests.test_bot_support import BTC, ETH, utc

# The shared fixtures, used by name below.
settings, at, market, trader, signal_trader = (support.settings, support.at, support.market, support.trader,
                                               support.signal_trader)

pytestmark = pytest.mark.unit


def _book(bot, name):
    spec = next(s for s in bot.specs if s.name == name)
    return bot.book(spec)


# ─── 1. What a run processes ─────────────────────────────────────────────────


def test_a_new_book_starts_at_the_last_closed_candle_and_fills_at_the_next_open(trader, market, at):
    first = trader.catch_up("2026-10-14")
    assert first.new and first.day == "2026-10-14" and first.days == ["2026-10-14"]
    hold = _book(trader, "btc_buy_hold")
    assert [h["date"] for h in hold.history] == ["2026-10-14"]
    assert hold.meta["target"] == {BTC: 1.0}
    (order,) = hold.orders
    assert order.side == "BUY" and order.created == "2026-10-14" and order.limit is None
    assert "MUA BTC" in first.text and "nến 2026-10-14 (UTC)" in first.text
    assert hold.history[-1]["benchmark"] == market.close(BTC, "2026-10-14")

    at["now"] = utc(2026, 10, 16, 0, 30)
    second = trader.catch_up("2026-10-15")
    hold = _book(trader, "btc_buy_hold")
    (fill,) = hold.fills
    open_ = market.rows[(BTC, "2026-10-15")][0]
    assert fill.date == "2026-10-15" and fill.price == pytest.approx(math.ceil(open_ * 1.001 * 100) / 100)
    assert fill.fee == pytest.approx(fill.qty * fill.price * 0.001, rel=1e-6)
    assert round(fill.qty, 5) == fill.qty                      # floored to the pair's step size
    assert hold.cash >= 0 and hold.nav() < 10_000              # fees and slippage were paid
    assert "Khớp MUA BTC" in second.text and "So sánh (sau phí và trượt giá giả định)" in second.text


def test_only_closed_candles_are_processed(trader, market, at):
    at["now"] = utc(2026, 10, 15, 0, 4)          # 07:04 Vietnam time: the 10-14 candle is not final yet
    result = trader.catch_up("2026-10-15")
    assert result.day == "2026-10-13"
    assert all(end <= "2026-10-13" for _, end in market.loads)
    assert trader.last_day() == "2026-10-13"


def test_a_finished_day_is_not_processed_or_reported_again(trader, market):
    first = trader.catch_up("2026-10-14")
    again = trader.catch_up("2026-10-14")
    assert not again.new and again.text == first.text
    assert len(_book(trader, "btc_buy_hold").history) == 1
    assert trader.is_done("2026-10-14")


def test_missed_days_are_replayed_in_order_with_each_days_data(trader, market, at):
    trader.catch_up("2026-10-14")
    at["now"] = utc(2026, 10, 18, 3, 0)          # three days of downtime
    result = trader.catch_up("2026-10-17")
    assert result.days == ["2026-10-15", "2026-10-16", "2026-10-17"]
    hold = _book(trader, "btc_buy_hold")
    assert [h["date"] for h in hold.history] == ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17"]
    assert [h["benchmark"] for h in hold.history] == [market.close(BTC, d) for d in
                                                      ("2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17")]
    assert result.text.startswith("⏪ Chạy bù 2 ngày bị lỡ: 2026-10-15, 2026-10-16")
    assert "2026-10-15 Mua & giữ BTC: MUA BTC" in result.text


def test_a_long_outage_is_capped_and_stale_orders_lapse(trader, market, at, monkeypatch):
    monkeypatch.setattr(trader_module, "MAX_CATCH_UP", 2)
    trader.catch_up("2026-10-14")
    at["now"] = utc(2026, 10, 20, 1, 0)
    result = trader.catch_up("2026-10-19")
    assert result.days == ["2026-10-18", "2026-10-19"]
    assert "Bỏ qua 3 ngày cũ hơn" in result.text
    hold = _book(trader, "btc_buy_hold")
    assert all(f.date >= "2026-10-19" for f in hold.fills)     # the 10-14 order lapsed, the 10-18 one filled


def test_orders_whose_day_passed_unprocessed_are_dropped():
    from cryptotrader.trading.ledger import Ledger, Order

    book = Ledger(cash=1000, start_cash=1000)
    book.orders = [Order(BTC, "BUY", 0.01, None, "2026-10-10"), Order(ETH, "BUY", 0.1, None, "2026-10-12")]
    stale = trader_module._expire(book, "2026-10-12")
    assert [o.created for o in stale] == ["2026-10-10"] and [o.created for o in book.orders] == ["2026-10-12"]
    assert trader_module._expire(book, None) == []


def test_a_day_without_a_candle_or_with_bad_prices_is_a_day_without_trading(trader, market, at):
    trader.catch_up("2026-10-14")
    market.rows[(BTC, "2026-10-15")] = (float("nan"),) * 4 + (0.0,)
    at["now"] = utc(2026, 10, 16, 0, 30)
    trader.catch_up("2026-10-15")
    hold = _book(trader, "btc_buy_hold")
    assert hold.fills == [] and hold.positions == {}
    # No price on 10-15 either to size a new order: the next one is placed on 10-16.
    at["now"] = utc(2026, 10, 18, 0, 30)
    trader.catch_up("2026-10-17")
    assert [f.date for f in _book(trader, "btc_buy_hold").fills] == ["2026-10-17"]


def test_a_day_is_not_saved_when_the_source_of_a_held_pair_does_not_answer(trader, market, at):
    trader.catch_up("2026-10-14")
    at["now"] = utc(2026, 10, 16, 0, 30)
    trader.catch_up("2026-10-15")                     # BTC bought on 10-15
    at["now"] = utc(2026, 10, 17, 0, 30)
    market.down.add(BTC)
    with pytest.raises(Exception):  # noqa: B017 - the source's error, retried by the scheduler
        trader.catch_up("2026-10-16")
    assert trader.last_day() == "2026-10-15"
    market.down.clear()
    assert trader.catch_up("2026-10-16").days == ["2026-10-16"]


def test_a_failure_mid_run_is_resumed_without_replaying_any_book(trader, market, at, monkeypatch):
    trader.catch_up("2026-10-14")
    at["now"] = utc(2026, 10, 16, 0, 30)
    trend = next(s for s in trader.specs if s.name == "btc_trend")
    real_strategy = BookSpec.strategy
    calls = {"n": 0}

    def flaky(spec):
        strategy = real_strategy(spec)
        if spec.name == "btc_buy_hold" and calls["n"] == 0:
            calls["n"] += 1

            def boom(ctx):
                raise RuntimeError("strategy crashed")
            strategy.decide = boom
        return strategy

    monkeypatch.setattr(BookSpec, "strategy", flaky)
    with pytest.raises(RuntimeError, match="strategy crashed"):
        trader.catch_up("2026-10-15")
    assert trader.book(trend).meta["last_day"] == "2026-10-15"           # saved before the failure
    assert _book(trader, "btc_buy_hold").meta["last_day"] == "2026-10-14"
    trend_fills = len(trader.book(trend).fills)
    result = trader.catch_up("2026-10-15")
    assert result.new and len(trader.book(trend).fills) == trend_fills    # not replayed
    assert _book(trader, "btc_buy_hold").meta["last_day"] == "2026-10-15"


def test_a_lost_report_is_rebuilt_without_processing_again(trader, market):
    trader.catch_up("2026-10-14")
    (trader.dir / "last_report.json").unlink()
    result = trader.catch_up("2026-10-14")
    assert result.new and result.days == [] and "MỤC TIÊU" not in result.text and "Mục tiêu: BTC 100%" in result.text
    assert len(_book(trader, "btc_buy_hold").history) == 1


def test_state_files_are_written_atomically(trader, market):
    trader.catch_up("2026-10-14")
    leftovers = [p.name for p in trader.dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    report = json.loads((trader.dir / "last_report.json").read_text(encoding="utf-8"))
    assert report["day"] == "2026-10-14" and report["at"] == "2026-10-15T07:30+07:00"   # Vietnam time


# ─── 2. One run at a time ────────────────────────────────────────────────────


def test_concurrent_runs_process_a_day_once(trader, market):
    market.gate = threading.Event()
    results = []
    threads = [threading.Thread(target=lambda: results.append(trader.catch_up("2026-10-14"))) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    assert trader.running
    assert "đang chạy" in trader.start_run(notify=lambda t: None)
    market.gate.set()
    for t in threads:
        t.join(10)
    assert sum(r.new for r in results) == 1
    assert [h["date"] for h in _book(trader, "btc_buy_hold").history] == ["2026-10-14"]


def test_chay_runs_the_last_closed_day_once(trader, market):
    notes, done = [], threading.Event()
    trader.notify = lambda text: (notes.append(text), done.set())
    reply = trader.start_run()
    assert "2026-10-14" in reply
    assert done.wait(10)
    assert "MUA BTC" in notes[0]
    deadline = time.time() + 5
    while trader.running and time.time() < deadline:
        time.sleep(0.01)
    assert "đã chạy rồi" in trader.start_run()


def test_a_reported_fill_during_a_run_is_neither_blocked_nor_overwritten(signal_trader, market):
    bot = signal_trader
    bot.set_capital(1000)
    market.gate = threading.Event()
    worker = threading.Thread(target=bot.run_day, args=("2026-10-14",))
    worker.start()
    time.sleep(0.1)
    started = time.monotonic()
    bot.record_fill("BUY", "BTC", 0.01, market.close(BTC, "2026-10-14"))
    assert time.monotonic() - started < 2
    market.gate.set()
    worker.join(10)
    assert bot.real().positions[BTC].qty == pytest.approx(0.00999)      # the buy fee taken in BTC


# ─── 3. Signals for the real account ─────────────────────────────────────────


def test_signal_orders_are_sized_on_the_real_book_and_respect_the_filters(signal_trader, market):
    bot = signal_trader
    bot.set_capital(1000)
    text = bot.run_day("2026-10-14")
    assert "📣 TÍN HIỆU THẬT - BTC THEO 7 ĐƯỜNG SMA" in text
    line = next(ln for ln in text.splitlines() if ln.startswith("- MUA BTC"))
    qty = float(line.split()[3])
    close = market.close(BTC, "2026-10-14")
    assert round(qty, 5) == qty                                    # the pair's step size
    assert 0.98 * 1000 / close < qty <= 1000 / close               # the whole book, after costs
    assert bot.real().cash == 1000                                 # the bot never writes the real book


def test_an_order_below_the_minimum_notional_is_left_out_with_the_reason(signal_trader, market):
    bot = signal_trader
    bot.set_capital(4)
    text = bot.run_day("2026-10-14")
    assert "- MUA BTC" not in text
    assert "Bỏ qua MUA BTC" in text and "dưới mức tối thiểu 5 USDT" in text


def test_without_capital_the_signal_report_asks_for_it(signal_trader, market):
    assert "/von 1000" in signal_trader.run_day("2026-10-14")


def test_a_changed_signal_strategy_is_announced_once(signal_trader, market, at):
    bot = signal_trader
    bot.set_capital(1000)
    bot.record_fill("BUY", "BTC", 0.01, market.close(BTC, "2026-10-14"), "2026-10-14")
    bot.run_day("2026-10-14")
    object.__setattr__(bot.settings, "signal_strategy", "btc_buy_hold")
    at["now"] = utc(2026, 10, 16, 0, 30)
    text = bot.run_day("2026-10-15")
    assert "đổi chiến lược: BTC theo 7 đường SMA → Mua & giữ BTC" in text
    at["now"] = utc(2026, 10, 17, 0, 30)
    assert "đổi chiến lược" not in bot.run_day("2026-10-16")


# ─── 4. The real book: /khop and /von ────────────────────────────────────────


def test_reported_fills_are_checked_before_they_touch_the_book(signal_trader, market, at):
    bot = signal_trader
    bot.set_capital(1000)
    bot.run_day("2026-10-14")                      # the closes of the run are the reference prices
    close = market.close(BTC, "2026-10-14")
    with pytest.raises(ValueError, match="tương lai"):
        bot.record_fill("BUY", "BTC", 0.01, close, "2026-10-16")
    with pytest.raises(ValueError, match="quá xa giá đóng cửa"):
        bot.record_fill("BUY", "BTC", 0.01, close / 10)            # 65000 typed 6500
    with pytest.raises(ValueError, match="Không đủ USDT"):
        bot.record_fill("BUY", "BTC", 1, close)
    with pytest.raises(ValueError, match="stablecoin"):
        bot.record_fill("BUY", "USDC", 10, 1.0)
    with pytest.raises(ValueError, match="không có đủ"):
        bot.record_fill("SELL", "ETH", 1, market.close(ETH, "2026-10-14"))
    assert bot.real().fills == [] and bot.real().cash == 1000

    reply = bot.record_fill("mua", "btc", 0.01, close, "2026-10-14")
    assert reply.startswith("✅ Đã ghi MUA BTC 0.01") and "ngày 2026-10-14 (UTC)" in reply
    with pytest.raises(ValueError, match="trước lệnh đã ghi gần nhất"):
        bot.record_fill("SELL", "BTC", 0.005, close, "2026-10-13")
    reply = bot.record_fill("ban", "BTCUSDT", 0.005, close * 1.01)
    assert "lãi/lỗ" in reply
    real = bot.real()
    assert real.positions[BTC].qty == pytest.approx(0.00499) and [f.date for f in real.fills] == ["2026-10-14", "2026-10-15"]


def test_the_price_check_reads_the_candle_cache_and_never_downloads(signal_trader, market):
    from datetime import date

    from cryptotrader.data import ohlcv
    from tests.helpers import frame

    bot = signal_trader
    bot.set_capital(1000)
    ohlcv.write_cache(BTC, frame(date(2026, 10, 1), 14, 60_000.0, 10.0), {})
    with pytest.raises(ValueError, match="quá xa giá đóng cửa"):
        bot.record_fill("BUY", "BTC", 0.001, 6_000.0)
    assert bot.record_fill("BUY", "BTC", 0.001, 60_100.0).startswith("✅")
    assert market.loads == []


def test_capital_is_validated_and_a_damaged_real_book_is_never_emptied(signal_trader):
    bot = signal_trader
    for bad in (0, -5, float("nan"), True, 1e11):
        with pytest.raises(ValueError):
            bot.set_capital(bad)
    assert bot.set_capital(1500) == "✅ Tiền mặt tài khoản thật: 1,500.00 USDT."
    bot.real_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="hỏng"):
        bot.set_capital(2000)
    assert not bot.real_path.exists() and list(bot.dir.glob("real.json.corrupt-*"))
    with pytest.raises(ValueError, match="real.json.corrupt-"):
        bot.set_capital(2000)                       # still refused, never a fresh empty book
    assert "⚠️" in bot.portfolio_text()


def test_portfolio_and_orders_text(signal_trader, market):
    bot = signal_trader
    bot.set_capital(1000)
    bot.run_day("2026-10-14")
    text = bot.portfolio_text()
    assert "Xu hướng BTC" in text and "Mua & giữ BTC" in text and "💼 Thật: USDT 1,000.00" in text
    orders = bot.orders_text()
    assert "MUA BTC" in orders and "đặt sau nến 2026-10-14" in orders


def test_a_strategy_missing_from_the_registry_is_skipped(settings, market, monkeypatch):
    from bot import books

    monkeypatch.setattr(books, "BOOKS", books.BOOKS + (BookSpec("not_there", "Chưa có"),))
    bot = trader_module.TradingBot(settings)
    assert [s.name for s in bot.specs] == ["btc_sma_ens", "btc_trend", "btc_buy_hold"]
    assert bot.catch_up("2026-10-14").new


# ─── regressions ─────────────────────────────────────────────────────────────


def test_a_buy_fee_taken_in_the_coin_is_booked_once(signal_trader, market):
    """Binance without BNB takes a buy's fee from the coin: the book holds the net coin
    and pays the executed value in USDT, with no second fee in USDT."""
    bot = signal_trader
    bot.set_capital(1000)
    close = market.close(BTC, "2026-10-14")
    reply = bot.record_fill("BUY", "BTC", 0.00123, close, "2026-10-14")
    real = bot.real()
    assert real.positions[BTC].qty == pytest.approx(0.00122877, abs=1e-12)
    assert real.cash == pytest.approx(1000 - 0.00123 * close)
    assert "trừ vào coin" in reply and "0.00122877" in reply
    # Paid with BNB: the whole quantity, no fee in the book.
    bot.record_fill("BUY", "ETH", 0.1, market.close(ETH, "2026-10-14"), "2026-10-14", fee_in="bnb")
    real = bot.real()
    assert real.positions[ETH].qty == pytest.approx(0.1)
    assert real.cash == pytest.approx(1000 - 0.00123 * close - 0.1 * market.close(ETH, "2026-10-14"))
    with pytest.raises(ValueError, match="không trừ vào coin"):
        bot.record_fill("SELL", "ETH", 0.05, market.close(ETH, "2026-10-14"), fee_in="coin")
    with pytest.raises(ValueError, match="coin, usdt hoặc bnb"):
        bot.record_fill("BUY", "ETH", 0.05, market.close(ETH, "2026-10-14"), fee_in="eth")


def test_a_full_exit_of_a_net_holding_is_on_the_step_grid_and_dust_is_not_nagged():
    from cryptotrader.market import rules
    from cryptotrader.trading.ledger import Ledger
    from tests.test_bot_support import FILTERS

    book = Ledger(cash=1000.0, start_cash=1000.0)
    book.filters = {BTC: FILTERS}
    book.record_fill("BUY", BTC, 0.00122877, 60_000.0, "2026-10-14", fee=(0.00123 - 0.00122877) * 60_000)
    orders = book.plan_target_weights("2026-10-14", {}, {BTC: 60_000.0})
    assert [(o.side, o.qty) for o in orders] == [("SELL", 0.00122)]
    assert rules.floor_to_step(orders[0].qty, FILTERS.step_size) == orders[0].qty
    book.orders = []
    book.record_fill("SELL", BTC, 0.00122, 60_000.0, "2026-10-15")
    assert book.positions[BTC].qty == pytest.approx(0.00000877)
    assert book.plan_target_weights("2026-10-15", {}, {BTC: 60_000.0}) == []
    assert not book.meta.get("rejections")                              # no daily "Bỏ qua BÁN" line


def test_the_closes_cover_coins_held_only_in_the_real_book(settings, market, at):
    from bot.trader import TradingBot

    object.__setattr__(settings, "trade_mode", "signal")
    object.__setattr__(settings, "signal_strategy", "btc_buy_hold")
    bot = TradingBot(settings)
    bot.set_capital(1000)
    bot.record_fill("BUY", "ETH", 0.1, market.close(ETH, "2026-10-14"), "2026-10-14")
    at["now"] = utc(2026, 10, 16, 0, 30)
    bot.run_day("2026-10-15")
    closes = bot.last_prices()["closes"]
    assert closes.get(ETH) == market.close(ETH, "2026-10-15")           # not the fill price forever


def test_a_finishing_run_now_does_not_clear_the_flag_of_a_run_still_going(trader, market):
    calls, orig = [], trader._catch_up
    other_waiting, second_inside, release = threading.Event(), threading.Event(), threading.Event()

    def slow(day):
        calls.append(day)
        if len(calls) == 1:
            other_waiting.wait(5)
        else:
            second_inside.set()
            release.wait(5)
        return orig(day)

    trader._catch_up = slow
    notified = threading.Event()
    trader.start_run(notify=lambda text: notified.set())
    other = threading.Thread(target=trader.catch_up, args=("2026-10-14",))
    other.start()
    time.sleep(0.1)                                    # the scheduler's run waits on the lock
    other_waiting.set()
    assert notified.wait(10) and second_inside.wait(10)
    assert trader.running                              # the scheduler's run is still going
    assert "đang chạy" in trader.start_run(notify=lambda text: None)
    release.set()
    other.join(10)
    assert not trader.running


def test_run_now_sends_a_new_report_to_the_report_callback(trader, market):
    notes, reports, done = [], [], threading.Event()
    trader.start_run(notify=notes.append, report=lambda text: (reports.append(text), done.set()))
    assert done.wait(10)
    assert reports and "MUA BTC" in reports[0] and notes == []
