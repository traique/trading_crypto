"""The futures side of the bot: daily catch-up of the paper books, signals, goal/resume, Telegram, web.

No network: the candles are the fake market of tests/test_bot_support.py
(ETH rises 1 USDT a day from 1000, BTC 100 a day from 20000).
"""

import pytest

from bot.futures_bot import FuturesTrader
from bot.jobs import JobQueue
from bot.service import FuturesScheduler
from bot.telegram_bot import TelegramBot
from tests import test_bot_support as support
from tests.test_bot_support import BTC, ETH, utc

settings, at, market = support.settings, support.at, support.market

pytestmark = pytest.mark.unit


@pytest.fixture
def futures(settings, market):
    # The fake market has BTC and ETH only.
    object.__setattr__(settings, "futures_symbols", (ETH,))
    return FuturesTrader(settings)


def test_a_pair_without_candles_does_not_hold_the_others_back(settings, market):
    object.__setattr__(settings, "futures_symbols", (ETH, "SOLUSDT"))
    result = FuturesTrader(settings).catch_up("2026-10-14")
    assert result.day == "2026-10-14" and "SOL: không có nến" in result.text


def _set(settings, **values):
    for key, value in values.items():
        object.__setattr__(settings, key, value)


def test_a_new_book_starts_at_the_last_closed_candle_and_goes_long_on_the_uptrend(futures, at):
    result = futures.catch_up("2026-10-14")
    assert result.new and result.day == "2026-10-14" and result.days == ["2026-10-14"]
    book = futures.load_book(ETH)
    assert book.qty == 0 and book.pending["qty"] > 0          # fills at the next open, not today
    assert book.pending["qty"] * 1287 >= 20                   # the ETH minimum order (20 USDT)
    assert "FUTURES" in result.text and "LONG" in result.text
    again = futures.catch_up("2026-10-14")
    assert not again.new
    at["now"] = utc(2026, 10, 16, 0, 30)
    nxt = futures.catch_up("2026-10-15")
    assert nxt.days == ["2026-10-15"]
    book = futures.load_book(ETH)
    assert book.side == "LONG" and book.entry > 1287 and book.stop is not None
    assert "Vị thế: LONG" in nxt.text


def test_ten_usdt_is_too_small_for_btc_and_the_report_says_so(settings, market):
    _set(settings, futures_symbols=(BTC, ETH))
    futures = FuturesTrader(settings)
    text = futures.catch_up("2026-10-14").text
    assert futures.load_book(BTC).pending is None
    assert "quá nhỏ cho BTCUSDT" in text


def test_signal_mode_sizes_the_real_target_on_the_reported_equity(settings, market):
    assert settings.futures_signal_symbol == "BNBUSDT"            # the default (not in the fake market)
    _set(settings, futures_mode="signal", futures_signal_symbol=ETH)
    futures = FuturesTrader(settings)
    assert "/fvon" in futures.catch_up("2026-10-14").text
    assert "vốn gốc 10" in futures.set_real_equity(10)
    small = "\n".join(futures._real_signal(support_market(), "2026-10-14"))
    assert "Vị thế mục tiêu: LONG (mua) 0.016 ETH" in small and "STOP_MARKET" in small
    futures.set_real_equity(100)                               # the start stays 10
    big = "\n".join(futures._real_signal(support_market(), "2026-10-14"))
    assert "LONG (mua) 0.155 ETH" in big
    futures.set_real_equity(120)
    done = "\n".join(futures._real_signal(support_market(), "2026-10-14"))
    assert "ĐÃ ĐẠT MỤC TIÊU" in done
    assert "Đặt lại vốn gốc" in futures.reset_real_start(120)
    assert "Vị thế mục tiêu" in "\n".join(futures._real_signal(support_market(), "2026-10-14"))
    futures.set_real_equity(4)
    assert "NGỪNG" in "\n".join(futures._real_signal(support_market(), "2026-10-14"))


def support_market():
    from bot.trader import _Market

    return _Market("2026-10-14")


def test_a_halted_book_resumes_with_the_goal_and_floor_counted_from_its_equity(futures):
    futures.catch_up("2026-10-14")
    book = futures.load_book(ETH)
    book.meta["halted"], book.meta["halted_on"] = "goal", "2026-10-14"
    book.cash = 112.0
    book.history[-1]["nav"] = 112.0
    futures.save_book(book)
    assert "đã đạt mục tiêu" in futures.status_text()
    assert "chạy lại" in futures.resume("eth")
    book = futures.load_book(ETH)
    assert "halted" not in book.meta and book.capital == 112.0
    assert book.meta["stop_equity"] == pytest.approx(56.0) and book.meta["start_capital"] == 10.0
    assert "đang chạy bình thường" in futures.resume("ETH")
    with pytest.raises(ValueError):
        futures.resume("PEPE")


def test_the_futures_scheduler_has_its_own_stamp_and_switch(settings, futures):
    sched = FuturesScheduler(settings, bot=futures)
    assert sched.enabled and sched._trade_stamp.name == "last_futures_run.txt"
    _set(settings, futures_mode="off")
    assert not sched.enabled


def test_telegram_futures_commands(settings, market, monkeypatch):
    _set(settings, telegram_token="123456789:AAHxYz0123456789abcdefghijklmnopqrstu", futures_mode="signal")
    bot = TelegramBot(settings, JobQueue(settings))
    sent = []
    bot._call = lambda method, http_timeout=30, **params: sent.append(params.get("text", "")) or {}
    bot.futures = FuturesTrader(settings)

    def say(text):
        bot._handle({"message_id": 1, "chat": {"id": 111}, "from": {"id": 111}, "text": text})
        return sent[-1]

    assert "FUTURES" in say("/futures")
    assert "vốn gốc 10" in say("/fvon 10")
    assert "Dùng: /fvon" in say("/fvon")
    assert "Đặt lại vốn gốc" in say("/fvon 25 moi")
    assert "Dùng: /futures" in say("/futures abc")
    assert "/futures" in say("/help")
    _set(settings, futures_mode="paper")
    assert "chỉ dùng ở chế độ tín hiệu" in say("/fvon 10")
    bot.futures = None
    assert "đang tắt" in say("/futures")


def test_the_futures_settings_are_read_from_the_environment(monkeypatch, tmp_path):
    from bot import settings as settings_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRYPTO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FUTURES_SYMBOLS", "eth, bnb/usdt")
    monkeypatch.setenv("FUTURES_SIGNAL_SYMBOL", "BNB")
    monkeypatch.setenv("FUTURES_CAPITAL", "20")
    monkeypatch.setenv("FUTURES_GOAL_PROFIT", "100")
    monkeypatch.setenv("FUTURES_MODE", "signal")
    s = settings_module.load()
    assert s.futures_symbols == ("ETHUSDT", "BNBUSDT") and s.futures_signal_symbol == "BNBUSDT"
    assert s.futures_capital == 20 and s.futures_stop_equity == 10 and s.futures_mode == "signal"


def test_the_web_page_shows_the_futures_books(settings, futures):
    from fastapi.testclient import TestClient

    from bot.web import create_app

    _set(settings, web_password="pw")
    futures.catch_up("2026-10-14")
    client = TestClient(create_app(settings, JobQueue(settings), futures=futures))
    page = client.get("/futures", auth=("x", "pw"))
    assert page.status_code == 200 and "FUTURES long/short" in page.text and "ETH" in page.text
    assert client.get("/futures").status_code == 401
