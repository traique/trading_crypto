"""The Telegram bot: commands, and the hardening of the polling loop.

Edited messages never re-run a command, a batch is never handled twice (not
after an error, not after a restart), the bot token never reaches a log or a
chat, a burst of messages runs on a bounded set of threads, strangers have a
budget of their own. No network: the Bot API, the trader and the market are faked.
"""

import logging
import threading
import time
from types import SimpleNamespace

import pytest
import requests

from bot import fmt, telegram_bot
from bot.jobs import JobQueue
from bot.telegram_bot import TelegramBot, TelegramNetworkError, chunks, plain, redact
from tests import test_bot_support as support

settings, at, market = support.settings, support.at, support.market

pytestmark = pytest.mark.unit

TOKEN = "123456789:AAHxYz0123456789abcdefghijklmnopqrstu"


class _Stop(BaseException):
    """Ends the endless polling loop from inside a fake API call."""


def _msg(text, user=111, message_id=1):
    return {"message_id": message_id, "chat": {"id": user}, "from": {"id": user}, "text": text}


@pytest.fixture
def jobs(settings):
    return JobQueue(settings)


@pytest.fixture
def tg(settings, jobs, monkeypatch):
    object.__setattr__(settings, "telegram_token", TOKEN)
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda s: None)
    bot = TelegramBot(settings, jobs)
    bot.sent = []
    bot._call = lambda method, http_timeout=30, **params: bot.sent.append((method, params)) or {}
    return bot


def _texts(bot):
    return [p.get("text", "") for m, p in bot.sent if m == "sendMessage"]


def _wait(cond, timeout=5.0):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.01)


class _FakeTrader:
    def __init__(self, settings):
        self.settings = settings
        self.fills, self.capital = [], []

    def record_fill(self, side, symbol, qty, price, day=None, fee_in=None):
        self.fills.append((side, symbol, qty, price, day) + ((fee_in,) if fee_in else ()))
        return f"✅ Đã ghi {side} {symbol}"

    def set_capital(self, cash):
        self.capital.append(cash)
        return f"✅ Vốn {cash}"

    def books(self):
        return []

    def portfolio_text(self):
        return "danh mục"

    def orders_text(self):
        return "lệnh"

    def start_run(self, notify=None, report=None):
        self.run_callbacks = (notify, report)
        return "▶️ chạy"

    def next_run_text(self):
        return "Bot: chế độ signal"


@pytest.fixture
def signal_bot(tg, settings):
    object.__setattr__(settings, "trade_mode", "signal")
    tg.trader = _FakeTrader(settings)
    return tg


# ─── 1. commands ──────────────────────────────────────────────────────────────


def test_an_unknown_user_is_told_their_id(tg):
    tg._handle(_msg("/danhmuc", user=999))
    tg._handle(_msg("/id", user=999))
    assert "Thêm 999 vào TELEGRAM_ALLOWED_IDS" in _texts(tg)[0] and _texts(tg)[1] == "Telegram id của bạn: 999"


def test_help_says_when_the_bot_runs_and_that_it_never_trades(tg):
    tg._handle(_msg("/help"))
    text = _texts(tg)[0]
    assert "07:10" in text and "KHÔNG đặt lệnh lên sàn" in text and "/khop mua BTC 0.0123 65000" in text


def test_nhanh_answers_from_closed_candles(tg, market):
    tg._handle(_msg("/nhanh btc"))
    text = _texts(tg)[0]
    assert text.startswith("⚡ BTC/USDT - nến 2026-10-14") and "Xu hướng: TĂNG" in text
    tg._handle(_msg("/nhanh USDC"))
    assert "stablecoin" in _texts(tg)[1] and _texts(tg)[1].startswith("⚠️")


def test_phantich_without_the_graph_answers_with_the_quick_view_and_why(tg, jobs, market, monkeypatch):
    monkeypatch.setattr(telegram_bot, "unavailable_reason", lambda: "chưa cài cryptotrader.graph")
    tg._handle(_msg("/phantich ETH"))
    (text,) = _texts(tg)
    assert text.startswith("⚡ ETH/USDT") and "Phân tích AI chưa dùng được: chưa cài cryptotrader.graph" in text
    assert "/nhanh ETH" in text and jobs.recent() == []


def test_phantich_queues_and_the_results_reach_the_chat(tg, jobs, market, monkeypatch):
    monkeypatch.setattr(telegram_bot, "unavailable_reason", lambda: None)
    tg._handle(_msg("/phantich BTC"))
    (job,) = jobs.recent()
    assert job.ticker == "BTCUSDT" and job.date == "2026-10-14" and job.chat_ids == [111]
    assert "Đã nhận BTCUSDT ngày 2026-10-14" in _texts(tg)[0]
    tg._handle(_msg("/phantich btcusdt", user=222))
    assert jobs.recent() == [job] and job.chat_ids == [111, 222]          # joined, not queued twice
    job.quick_summary, job.final_decision, job.action = "số liệu", "**Kết luận**: giữ", "HOLD"
    tg.on_job_event(job, "quick")
    tg.on_job_event(job, "done")
    texts = _texts(tg)
    assert texts.count("số liệu") == 2 and any(t.startswith("✅ BTCUSDT 2026-10-14 - HOLD") and "**" not in t
                                               for t in texts)


def test_a_bad_request_gets_a_reason_not_a_crash(tg, market):
    tg._handle(_msg("/phantich"))
    tg._handle(_msg("/phantich BTC 2030-01-01"))
    tg._handle(_msg("/nhanh ETH/BTC"))
    assert all(t.startswith("⚠️") for t in _texts(tg)) and len(_texts(tg)) == 3


def test_khop_parses_coin_quantities_with_a_dot(signal_bot):
    signal_bot._handle(_msg("/khop mua BTC 0.0123 65000"))
    signal_bot._handle(_msg("/khop ban eth 1.5 3200.5 2026-10-13"))
    assert signal_bot.trader.fills == [("BUY", "BTC", 0.0123, 65000.0, None),
                                       ("SELL", "eth", 1.5, 3200.5, "2026-10-13")]


def test_khop_refuses_ambiguous_numbers_and_bad_syntax(signal_bot):
    for text in ("/khop mua BTC 0,0123 65000", "/khop mua BTC 0.0123 65,000", "/khop mua BTC 0 65000",
                 "/khop mua BTC -1 65000", "/khop giu BTC 1 65000", "/khop mua BTC 1"):
        signal_bot._handle(_msg(text))
    assert signal_bot.trader.fills == []
    assert all(t.startswith("⚠️") for t in _texts(signal_bot)) and len(_texts(signal_bot)) == 6
    assert "dấu chấm" in _texts(signal_bot)[0]


def test_von_and_the_trading_commands(signal_bot):
    for text in ("/von 1000.5", "/danhmuc", "/lenh", "/chay", "/trangthai"):
        signal_bot._handle(_msg(text))
    assert signal_bot.trader.capital == [1000.5]
    texts = _texts(signal_bot)
    assert texts[:4] == ["✅ Vốn 1000.5", "danh mục", "lệnh", "▶️ chạy"] and "Bot: chế độ signal" in texts[4]


def test_khop_and_von_only_in_signal_mode_and_nothing_when_off(tg, settings):
    tg.trader = _FakeTrader(settings)
    tg._handle(_msg("/khop mua BTC 0.01 65000"))
    tg._handle(_msg("/von 1000"))
    object.__setattr__(settings, "trade_mode", "off")
    tg._handle(_msg("/danhmuc"))
    texts = _texts(tg)
    assert "TRADE_MODE=signal" in texts[0] and "TRADE_MODE=signal" in texts[1] and "TRADE_MODE=off" in texts[2]
    assert tg.trader.fills == [] and tg.trader.capital == []


def test_numbers_must_use_a_dot_for_decimals():
    assert fmt.parse_number("0.0123") == 0.0123 and fmt.parse_number("65000") == 65000
    for bad in ("65,000", "0,5", "", "abc", "-5", ".5", "1e3", "0", "1 000"):
        with pytest.raises(ValueError):
            fmt.parse_number(bad)


def test_long_text_is_split_and_markdown_stripped():
    parts = chunks("a" * 9000)
    assert [len(p) for p in parts] == [3900, 3900, 1200]
    assert plain("## Tiêu đề\n**đậm** và __gạch__") == "Tiêu đề\nđậm và gạch"


def test_flood_control_is_waited_out(settings, jobs, monkeypatch):
    object.__setattr__(settings, "telegram_token", TOKEN)
    bot = TelegramBot(settings, jobs)
    waits, replies = [], [{"ok": False, "error_code": 429, "parameters": {"retry_after": 3}},
                          {"ok": True, "result": {"message_id": 1}}]
    monkeypatch.setattr(telegram_bot.time, "sleep", waits.append)
    bot._session = SimpleNamespace(post=lambda url, **kw: SimpleNamespace(json=lambda: replies.pop(0),
                                                                          status_code=200))
    assert bot._call("sendMessage", chat_id=1, text="x") == {"message_id": 1}
    assert waits == [3.5]


# ─── 2. edited messages, offsets, restarts ───────────────────────────────────


def test_polling_asks_only_for_new_messages_and_ignores_edits(tg):
    calls, dispatched = [], []
    batch = [
        {"update_id": 10, "message": _msg("/khop mua BTC 0.01 65000")},
        {"update_id": 11, "edited_message": _msg("/khop mua BTC 0.01 65000")},
    ]

    def fake_call(method, http_timeout=30, **params):
        calls.append(params)
        if len(calls) == 1:
            return batch
        raise _Stop

    tg._call = fake_call
    tg._dispatch = dispatched.append
    with pytest.raises(_Stop):
        tg._poll()
    assert "edited_message" not in calls[0]["allowed_updates"]
    assert [m["text"] for m in dispatched] == ["/khop mua BTC 0.01 65000"]   # the edit is not a command
    assert calls[1]["offset"] == 12                                         # the whole batch confirmed


def test_editing_khop_does_not_record_the_fill_twice(signal_bot):
    _, messages = signal_bot._accept([{"update_id": 1, "message": _msg("/khop mua BTC 0.01 65000")}])
    for message in messages:
        signal_bot._handle(message)
    _, messages = signal_bot._accept([{"update_id": 2, "edited_message": _msg("/khop mua BTC 0.02 65000")}])
    for message in messages:
        signal_bot._handle(message)
    assert signal_bot.trader.fills == [("BUY", "BTC", 0.01, 65000.0, None)]


def test_an_error_while_queueing_does_not_replay_the_batch(tg):
    calls, handled = [], []
    batch = [{"update_id": 5, "message": _msg("/von 1")}, {"update_id": 6, "message": _msg("/von 2")}]

    def fake_call(method, http_timeout=30, **params):
        calls.append(params)
        if len(calls) <= 2:
            return batch            # Telegram would not repeat it, but a replay must still be refused
        raise _Stop

    def dispatch(message):
        handled.append(message["text"])
        raise RuntimeError("can't start new thread")

    tg._call, tg._dispatch = fake_call, dispatch
    with pytest.raises(_Stop):
        tg._poll()
    assert calls[1]["offset"] == 7 and calls[2]["offset"] == 7
    assert handled == ["/von 1", "/von 2"]


def test_a_restart_does_not_rerun_updates_already_handed_out(tg, settings, jobs):
    batch = [{"update_id": 42, "message": _msg("/von 1000")}]
    offset, messages = tg._accept(batch)
    assert offset == 43 and len(messages) == 1
    # The process dies before the next getUpdates confirms the batch; Telegram
    # hands it out again to the new process.
    again = TelegramBot(settings, jobs)
    offset, messages = again._accept(batch)
    assert offset == 43 and messages == []
    _, messages = again._accept([{"update_id": 43, "message": _msg("/danhmuc")}])
    assert [m["text"] for m in messages] == ["/danhmuc"]


def test_a_malformed_update_is_skipped_not_fatal(tg):
    offset, messages = tg._accept([{"message": _msg("/id")}, {"update_id": 3, "message": {"text": "/id"}},
                                   {"update_id": 4, "message": _msg("/id")}, "garbage"])
    assert offset == 5 and [m["text"] for m in messages] == ["/id"]


# ─── 3. the token never reaches a log or a chat ──────────────────────────────


def test_network_errors_lose_the_token(settings, jobs):
    object.__setattr__(settings, "telegram_token", TOKEN)
    bot = TelegramBot(settings, jobs)

    def post(url, **kwargs):
        raise requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")

    bot._session = SimpleNamespace(post=post)
    with pytest.raises(TelegramNetworkError) as info:
        bot._call("getUpdates")
    assert TOKEN not in str(info.value) and "<TOKEN>" in str(info.value)
    assert info.value.__suppress_context__          # the original (with the URL) is not printed
    assert isinstance(info.value, requests.RequestException)


def test_polling_logs_never_contain_the_token(tg, caplog):
    calls = []

    def fake_call(method, http_timeout=30, **params):
        calls.append(method)
        if len(calls) == 1:
            raise requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/getUpdates")
        if len(calls) == 2:
            raise ValueError(f"boom https://api.telegram.org/bot{TOKEN}/getUpdates")
        raise _Stop

    tg._call = fake_call
    with caplog.at_level(logging.DEBUG), pytest.raises(_Stop):
        tg._poll()
    assert "Telegram polling" in caplog.text
    assert TOKEN not in caplog.text
    assert all(TOKEN not in (r.exc_text or "") for r in caplog.records)


def test_logger_exception_traceback_is_redacted(tg, caplog):
    with caplog.at_level(logging.INFO):
        try:
            raise RuntimeError(f"url /bot{TOKEN}/sendMessage")
        except RuntimeError:
            telegram_bot.logger.exception("send failed for %s", f"bot{TOKEN}")
    assert "send failed" in caplog.text and "RuntimeError" in caplog.text
    assert TOKEN not in caplog.text


def test_a_failing_command_does_not_echo_the_token_to_the_chat(tg, monkeypatch):
    def broken():
        raise RuntimeError(f"HTTPSConnectionPool url: /bot{TOKEN}/getMe")

    monkeypatch.setattr(tg, "_status", broken)
    tg._handle(_msg("/trangthai"))
    assert _texts(tg) and TOKEN not in _texts(tg)[0] and _texts(tg)[0].startswith("⚠️ Lỗi")


def test_redact_handles_any_bot_token_shape():
    assert redact(f"x /bot{TOKEN}/y") == "x /bot<TOKEN>/y"
    assert redact("price 65000:300 at 10:30") == "price 65000:300 at 10:30"


# ─── 4. bounded command threads ──────────────────────────────────────────────


def test_a_burst_runs_on_a_few_threads_and_the_excess_is_dropped(tg, monkeypatch):
    release, started = threading.Event(), []
    names = set()

    def slow(message):
        names.add(threading.current_thread().name)
        started.append(message["text"])
        release.wait(5)

    monkeypatch.setattr(tg, "_handle", slow)
    before = threading.active_count()
    for i in range(telegram_bot.MAX_QUEUED_COMMANDS + 20):
        tg._dispatch(_msg(f"/nhanh {i}", user=(111, 222)[i % 2]))
    assert tg._queued == telegram_bot.MAX_QUEUED_COMMANDS
    assert threading.active_count() - before <= telegram_bot.COMMAND_LANES
    release.set()
    _wait(lambda: tg._queued == 0)
    assert tg._queued == 0 and len(started) == telegram_bot.MAX_QUEUED_COMMANDS
    assert len(names) <= telegram_bot.COMMAND_LANES


def test_a_strangers_flood_cannot_make_the_bot_drop_the_owners_fill(tg, monkeypatch):
    release, handled = threading.Event(), []

    def slow(message):
        handled.append(message["text"])
        if message["from"]["id"] == 999:
            release.wait(5)

    monkeypatch.setattr(tg, "_handle", slow)
    for i in range(60):
        tg._dispatch(_msg(f"spam {i}", user=999))
    tg._dispatch(_msg("/khop mua BTC 0.01 65000", user=111))
    assert tg._queued == 1                     # the owner's command is queued
    release.set()
    _wait(lambda: not (tg._queued or tg._strangers_queued))
    assert "/khop mua BTC 0.01 65000" in handled
    assert sum(t.startswith("spam") for t in handled) == 1      # one reply per chat per window


def test_a_stranger_still_gets_its_id_once_per_window(tg, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(telegram_bot.time, "monotonic", lambda: clock["now"])
    tg._dispatch(_msg("/id", user=999))
    tg._dispatch(_msg("/id", user=999))
    _wait(lambda: not tg._strangers_queued)
    assert _texts(tg) == ["Telegram id của bạn: 999"]
    clock["now"] += telegram_bot.STRANGER_REPLY_SECONDS + 1
    tg._dispatch(_msg("/id", user=999))
    _wait(lambda: len(_texts(tg)) == 2)
    assert len(_texts(tg)) == 2


def test_the_owner_is_told_when_a_command_is_dropped(tg, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(tg, "_handle", lambda message: release.wait(5))
    for i in range(telegram_bot.MAX_QUEUED_COMMANDS):
        tg._dispatch(_msg(f"/nhanh {i}", user=222))
    tg._dispatch(_msg("/khop mua BTC 0.01 65000", user=111))
    release.set()
    _wait(lambda: bool(_texts(tg)))
    assert any("Bot đang bận" in t and "/khop mua BTC" in t for t in _texts(tg))


def test_one_chats_commands_run_in_order(tg, monkeypatch):
    done = []
    monkeypatch.setattr(tg, "_handle", lambda message: done.append(message["text"]))
    for i in range(20):
        tg._dispatch(_msg(f"/von {i}"))
    _wait(lambda: tg._queued == 0)
    assert done == [f"/von {i}" for i in range(20)]


def test_a_crashing_handler_frees_its_slot(tg, monkeypatch):
    monkeypatch.setattr(tg, "_handle", lambda message: 1 / 0)
    tg._dispatch(_msg("/id"))
    _wait(lambda: tg._queued == 0)
    assert tg._queued == 0


# ─── regressions ─────────────────────────────────────────────────────────────


def test_khop_takes_an_optional_fee_asset_and_date(signal_bot):
    signal_bot._handle(_msg("/khop mua BTC 0.0123 65000 phi=bnb"))
    signal_bot._handle(_msg("/khop mua BTC 0.0123 65000 2026-10-13 phi=usdt"))
    signal_bot._handle(_msg("/khop mua BTC 0.0123 65000 phi=bnb phi=usdt"))
    assert signal_bot.trader.fills == [("BUY", "BTC", 0.0123, 65000.0, None, "bnb"),
                                       ("BUY", "BTC", 0.0123, 65000.0, "2026-10-13", "usdt")]
    assert _texts(signal_bot)[-1].startswith("⚠️")


def test_reported_quantities_and_prices_can_be_typed_back_into_khop():
    for value in (1234.5, 65432.1, 0.00122877, 152.3, 0.5):
        assert fmt.parse_number(fmt.qty(value)) == pytest.approx(value)
        assert fmt.parse_number(fmt.price(value)) == pytest.approx(value, rel=1e-4)
    assert fmt.qty(1234.5) == "1234.5" and fmt.price(65432.1) == "65432.10"


def test_chay_report_reaches_the_report_chats_once_each(signal_bot, settings):
    object.__setattr__(settings, "telegram_report_chat_ids", frozenset({111, -100}))
    signal_bot._handle(_msg("/chay", user=111))
    notify, report = signal_bot.trader.run_callbacks
    signal_bot.sent.clear()
    report("BÁO CÁO NGÀY")
    chats = sorted(p["chat_id"] for m, p in signal_bot.sent if p.get("text") == "BÁO CÁO NGÀY")
    assert chats == [-100, 111]                     # the asker is a report chat: not sent twice
    signal_bot.sent.clear()
    notify("✅ đã chạy rồi")
    assert [p["chat_id"] for _, p in signal_bot.sent] == [111]


def test_a_transient_network_error_is_retried(settings, jobs, monkeypatch):
    object.__setattr__(settings, "telegram_token", TOKEN)
    bot = TelegramBot(settings, jobs)
    waits = []
    monkeypatch.setattr(telegram_bot.time, "sleep", waits.append)
    outcomes = [requests.ConnectionError("reset"), SimpleNamespace(status_code=502, json=lambda: {}),
                SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 7}})]

    def post(url, **kwargs):
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    bot._session = SimpleNamespace(post=post)
    assert bot._call("sendMessage", chat_id=1, text="x") == {"message_id": 7}
    assert waits == list(telegram_bot.NETWORK_RETRY_DELAYS)
    # Bounded: a source that stays down fails after the retries.
    bot._session = SimpleNamespace(post=lambda url, **kw: (_ for _ in ()).throw(requests.ConnectionError("down")))
    with pytest.raises(TelegramNetworkError):
        bot._call("sendMessage", chat_id=1, text="x")


def test_an_undelivered_broadcast_waits_in_the_outbox(settings, jobs, monkeypatch):
    object.__setattr__(settings, "telegram_token", TOKEN)
    bot = TelegramBot(settings, jobs)
    up = {"ok": False}
    delivered = []

    def call(method, http_timeout=30, **params):
        if not up["ok"]:
            raise TelegramNetworkError("down")
        delivered.append((params["chat_id"], params["text"]))
        return {}

    bot._call = call
    bot.broadcast("Báo cáo 2026-10-14")
    assert bot._outbox_path.exists() and delivered == []
    up["ok"] = True
    assert bot.flush_outbox() == 2                  # both allowed ids are report chats
    assert sorted(delivered) == [(111, "Báo cáo 2026-10-14"), (222, "Báo cáo 2026-10-14")]
    assert not bot._outbox_path.exists() and bot.flush_outbox() == 0
