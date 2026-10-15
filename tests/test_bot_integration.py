"""The pieces together: the three books, their reports on Telegram and the web, the real-account
band, /nhanh, and /phantich through the real graph with a scripted model (no network, no AI)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bot import telegram_bot
from bot.jobs import JobQueue
from bot.trader import TradingBot
from bot.web import create_app
from cryptotrader.dataflows import news, sentiment, tokenomics
from cryptotrader.graph import trading_graph
from tests import test_bot_support as support
from tests.test_graph_end_to_end import RSS, ScriptedModel, _Client

settings, at, market = support.settings, support.at, support.market
BTC, utc = support.BTC, support.utc

pytestmark = pytest.mark.unit

TITLES = ("BTC theo 7 đường SMA", "Xu hướng BTC (SMA200)", "Mua & giữ BTC")


def _two_days(bot, at):
    bot.catch_up("2026-10-14")                        # new books: decide on 10-14
    at["now"] = utc(2026, 10, 16, 0, 30)
    return bot.catch_up("2026-10-15")                 # the orders fill at 10-15's open


def test_the_daily_report_shows_every_book_and_the_signal(settings, market, at):
    bot = TradingBot(settings)
    assert [s.name for s in bot.specs] == ["btc_sma_ens", "btc_trend", "btc_buy_hold"]
    text = _two_days(bot, at).text
    for title in TITLES:
        assert title.upper() in text and title in text.split("📊 So sánh")[1]
    # The steady rise of the fake market: the close is above all 7 SMAs.
    assert "Tín hiệu 2026-10-15: giá trên 7/7 đường SMA" in text and "giữ nguyên" in text
    book = bot.book(bot.specs[0])
    assert book.positions[BTC].qty > 0 and book.meta["strategy_state"]["signal"]["ideal"] == 1.0
    assert all(title in bot.portfolio_text() for title in TITLES)


def test_the_web_dashboard_and_telegram_show_every_book(settings, market, at, monkeypatch):
    bot = TradingBot(settings)
    _two_days(bot, at)
    object.__setattr__(settings, "web_password", "pw")
    jobs = JobQueue(settings, trader=bot)
    client = TestClient(create_app(settings, jobs, trader=bot))
    page = client.get("/", auth=("u", "pw"))
    assert page.status_code == 200
    for title in TITLES:
        assert title.replace("&", "&amp;") in page.text
    object.__setattr__(settings, "telegram_token", "123456789:AAHxYz0123456789abcdefghijklmnopqrstu")
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda s: None)
    tg = telegram_bot.TelegramBot(settings, jobs)
    tg.trader, sent = bot, []
    tg._call = lambda method, http_timeout=30, **params: sent.append(params.get("text", "")) or {}
    tg._handle({"message_id": 1, "chat": {"id": 111}, "from": {"id": 111}, "text": "/danhmuc"})
    assert all(title in "\n".join(sent) for title in TITLES)


def test_the_real_account_follows_the_strategys_band(settings, market, at):
    object.__setattr__(settings, "trade_mode", "signal")
    bot = TradingBot(settings)
    bot.set_capital(1000)
    close = market.close(BTC, "2026-10-14")
    # 93% of the account in BTC while the strategy's ideal is 100%: inside the 10% band.
    bot.record_fill("BUY", "BTC", round(930 / close, 5), close, "2026-10-14")
    bot.set_capital(70)
    text = bot.run_day("2026-10-14")
    real = text.split("📣 TÍN HIỆU THẬT")[1]
    assert "giữ nguyên" in real and "- MUA BTC" not in real
    # Most of it in cash: outside the band, buy up to the ideal.
    at["now"] = utc(2026, 10, 16, 0, 30)
    bot.record_fill("SELL", "BTC", round(700 / close, 5), close, "2026-10-15")
    real = bot.run_day("2026-10-15").split("📣 TÍN HIỆU THẬT")[1]
    assert "Mục tiêu: BTC 100%" in real and "- MUA BTC" in real


def test_nhanh_shows_the_strategys_reading(settings, market):
    from bot.quick import quick_view

    text = quick_view("BTC", None, trader=TradingBot(settings))
    assert "Chiến lược chính (btc_sma_ens): giá trên 7/7 SMA → mục tiêu BTC 100% NAV" in text
    assert "chỉ giao dịch BTC" in quick_view("ETH", None, trader=None)


@pytest.fixture
def scripted_graph(monkeypatch):
    """The real cryptotrader.graph with a scripted model and offline news/sentiment/tokenomics."""
    model = ScriptedModel()
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(model))
    monkeypatch.setattr(news, "FETCH", lambda url, params, source: RSS)
    monkeypatch.setattr(sentiment, "FETCH_JSON", lambda url, params, source: {"code": "1", "data": []})
    monkeypatch.setattr(tokenomics, "FETCH_JSON", lambda url, params, source: {"coins": []})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return model


def _state_log(settings_dir, pair: str, day: str) -> dict:
    found = sorted(settings_dir.rglob(f"full_states_log_{day}.json"))
    found = [p for p in found if pair in str(p)]
    assert found, f"no state log for {pair} {day}"
    return json.loads(found[-1].read_text(encoding="utf-8"))


def test_phantich_runs_the_graph_with_the_bots_book(settings, market, at, scripted_graph, tmp_path):
    bot = TradingBot(settings)
    _two_days(bot, at)                                  # the main book holds BTC since 10-15
    jobs = JobQueue(settings, trader=bot)
    events = []
    jobs.add_listener(lambda job, event: events.append((job.ticker, event)))

    held = jobs.submit("BTCUSDT", "2026-10-15", "telegram", 111)
    jobs.process(held)
    assert held.status == "done" and held.error == "", held.error
    assert held.action == "HOLD" and held.html_path is not None
    assert held.quick_summary.startswith("⚡ BTC/USDT") and events[-1] == ("BTCUSDT", "done")
    log = _state_log(tmp_path, "BTCUSDT", "2026-10-15")
    decision = log["quant_decision"]
    assert decision["strategy"] == "btc_sma_ens" and decision["current_weight"] > 0.9
    # Inside the 10% band of the ideal 100%: the target is to keep the held weight (as the daily signal).
    assert decision["target_weight"] == pytest.approx(decision["current_weight"])

    # A past day: today's holdings are not shown to it, so a wanted coin reads BUY ("unknown book").
    past = jobs.submit("BTCUSDT", "2026-10-10", "web")
    jobs.process(past)
    assert past.action == "BUY"
    assert _state_log(tmp_path, "BTCUSDT", "2026-10-10")["quant_decision"]["current_weight"] is None

    eth = jobs.submit("ETHUSDT", "2026-10-15", "web")
    jobs.process(eth)
    assert eth.action == "NO_TRADE" and "MUA ngay" not in (eth.final_decision or "")
