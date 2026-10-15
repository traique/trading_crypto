"""The daily scheduler: when the trading run fires, retries, catch-up at startup, late candles.

The clock (UTC) and the scheduler's own timer are fixed per test; the bot is a stand-in.
"""

import time
from types import SimpleNamespace

import pytest

from bot import service
from bot.service import DailyScheduler
from bot.trader import RunResult
from tests import test_bot_support as support
from tests.test_bot_support import utc

settings, at = support.settings, support.at

pytestmark = pytest.mark.unit


class _FakeBot:
    def __init__(self, failures=0, day="2026-10-14", new=True):
        self.failures, self.day, self.new = failures, day, new
        self.runs = []

    def catch_up(self, day):
        self.runs.append(day)
        if self.failures:
            self.failures -= 1
            raise ConnectionError("BTC candles unavailable")
        return RunResult(self.day, f"report {self.day}", self.new, [self.day] if self.new else [])


@pytest.fixture
def timer(monkeypatch, at):
    t = {"now": 1000.0}
    monkeypatch.setattr(service, "time", SimpleNamespace(time=lambda: t["now"], sleep=time.sleep))
    return t


def _scheduler(settings, bot, notes, started=True):
    sched = DailyScheduler(settings, bot=bot, notify=notes.append)
    sched._started = started
    return sched


def test_the_run_waits_for_the_trade_time(settings, at, timer):
    sched = _scheduler(settings, _FakeBot(), [])
    at["now"] = utc(2026, 10, 15, 0, 4)            # 07:04 Vietnam time
    assert sched._trade_due() is None
    at["now"] = utc(2026, 10, 15, 0, 10)           # 07:10 Vietnam time
    assert sched._trade_due() == "2026-10-15"
    at["now"] = utc(2026, 10, 14, 23, 0)           # 06:00 on the 15th in Vietnam: not yet
    assert sched._trade_due() is None


def test_a_scheduled_run_processes_the_last_closed_day_and_stamps_once(settings, at, timer):
    notes, bot = [], _FakeBot()
    sched = _scheduler(settings, bot, notes)
    at["now"] = utc(2026, 10, 15, 0, 15)
    sched.tick()
    assert bot.runs == ["2026-10-14"] and notes == ["report 2026-10-14"]
    assert sched._trade_stamp.read_text() == "2026-10-15"
    sched.tick()
    assert len(bot.runs) == 1
    at["now"] = utc(2026, 10, 18, 0, 15)           # a Sunday: crypto has no holidays
    bot.day = "2026-10-17"
    sched.tick()
    assert bot.runs[-1] == "2026-10-17"


def test_a_failed_run_is_retried_with_backoff_and_reported_once(settings, at, timer):
    notes, bot = [], _FakeBot(failures=2)
    sched = _scheduler(settings, bot, notes)
    at["now"] = utc(2026, 10, 15, 0, 15)
    sched.tick()
    assert bot.runs == ["2026-10-14"] and not sched._trade_stamp.exists()
    assert len(notes) == 1 and "thử lại" in notes[0]
    timer["now"] += 300
    sched.tick()
    assert len(bot.runs) == 1                                      # still waiting for the retry
    timer["now"] += 301
    sched.tick()
    assert len(bot.runs) == 2 and len(notes) == 1                  # failed again: no second alert
    timer["now"] += 1201
    sched.tick()
    assert len(bot.runs) == 3 and sched._trade_stamp.read_text() == "2026-10-15"
    assert notes[-1] == "report 2026-10-14"


def test_retries_are_bounded_per_day(settings, at, timer):
    notes, bot = [], _FakeBot(failures=100)
    sched = _scheduler(settings, bot, notes)
    at["now"] = utc(2026, 10, 15, 0, 15)
    for _ in range(20):
        sched.tick()
        timer["now"] += 4000
    assert len(bot.runs) == DailyScheduler.MAX_TRADE_ATTEMPTS
    assert sched._trade_stamp.read_text() == "2026-10-15" and "tạm dừng" in notes[-1]


def test_startup_catches_up_without_stamping_the_day(settings, at, timer):
    notes, bot = [], _FakeBot(day="2026-10-13")
    sched = _scheduler(settings, bot, notes, started=False)
    at["now"] = utc(2026, 10, 14, 20, 0)           # a redeploy at 03:00 Vietnam time
    sched.tick()
    assert bot.runs == ["2026-10-13"] and notes == ["report 2026-10-13"]
    assert not sched._trade_stamp.exists()
    at["now"] = utc(2026, 10, 15, 0, 11)
    bot.day = "2026-10-14"
    sched.tick()
    assert bot.runs[-1] == "2026-10-14" and sched._trade_stamp.read_text() == "2026-10-15"


def test_a_late_candle_is_retried_then_left_to_the_next_run(settings, at, timer):
    notes, bot = [], _FakeBot(day="2026-10-13")     # the source has not published 10-14 yet
    sched = _scheduler(settings, bot, notes)
    at["now"] = utc(2026, 10, 15, 0, 15)
    sched.tick()
    assert not sched._trade_stamp.exists()
    timer["now"] += DailyScheduler.LATE_RETRY_SECONDS + 1
    bot.new = False
    for _ in range(DailyScheduler.MAX_LATE_RETRIES + 2):
        sched.tick()
        timer["now"] += DailyScheduler.LATE_RETRY_SECONDS + 1
    assert sched._trade_stamp.read_text() == "2026-10-15"
    assert len(bot.runs) == DailyScheduler.MAX_LATE_RETRIES + 1
    assert "Chưa lấy được nến 2026-10-14" in notes[-1]


def test_a_late_candle_that_arrives_is_processed_and_stamped(settings, at, timer):
    notes, bot = [], _FakeBot(day="2026-10-13")
    sched = _scheduler(settings, bot, notes)
    at["now"] = utc(2026, 10, 15, 0, 15)
    sched.tick()
    bot.day = "2026-10-14"
    timer["now"] += DailyScheduler.LATE_RETRY_SECONDS + 1
    sched.tick()
    assert sched._trade_stamp.read_text() == "2026-10-15" and notes[-1] == "report 2026-10-14"


def test_trade_mode_off_never_runs(settings, at, timer):
    object.__setattr__(settings, "trade_mode", "off")
    bot = _FakeBot()
    sched = _scheduler(settings, bot, [], started=False)
    at["now"] = utc(2026, 10, 15, 1, 0)
    sched.tick()
    assert bot.runs == []


def test_a_day_ended_by_failures_resets_the_late_retry_budget(settings, at, timer):
    notes, bot = [], _FakeBot(day="2026-10-13")     # reaches only 10-13 a few times...
    sched = _scheduler(settings, bot, notes)
    at["now"] = utc(2026, 10, 15, 0, 15)
    for _ in range(5):
        sched.tick()
        timer["now"] += DailyScheduler.LATE_RETRY_SECONDS + 1
    assert sched._late_retries == 5
    bot.failures = 100                               # ...then the source goes down for the day
    for _ in range(DailyScheduler.MAX_TRADE_ATTEMPTS + 2):
        sched.tick()
        timer["now"] += 4000
    assert sched._trade_stamp.read_text() == "2026-10-15"
    assert sched._late_retries == 0                  # tomorrow gets its full budget again
