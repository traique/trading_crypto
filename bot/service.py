"""What the web page and the Telegram bot share: the quick view and the daily trading run."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from bot import clock
from bot.settings import Settings
from cryptotrader.data.files import write_text_atomic

logger = logging.getLogger(__name__)


def quick_text(symbol: str, day: str | None = None, trader=None) -> str:
    """The quick view of one coin (closed candles, no LLM), as text."""
    from bot.quick import quick_view

    return quick_view(symbol, day, trader=trader)


class DailyScheduler:
    """The daily trading run (Vietnam time on the clock, UTC days in the books).

    From ``trade_time`` (default 07:10, never before 07:05 = 00:05 UTC, when the
    candle of the UTC day before is final) the bot catches up every closed day it
    has not processed yet. The market trades 24/7: there are no holidays, every
    UTC day has a candle. A failed run is retried with a growing delay
    (``MAX_TRADE_ATTEMPTS`` per day, the owner is told once); what is still
    missing is caught up by the next run. The bot also catches up once at
    startup, so a redeploy loses no day. When a run succeeds but the sources do
    not have the last closed candle yet (it reached only an earlier day), the day
    is not stamped: it is tried again every ``LATE_RETRY_SECONDS``, at most
    ``MAX_LATE_RETRIES`` times, then left to the next day's run (which catches up).
    """

    RETRY_SECONDS = 600            # first retry; doubles per failure up to MAX_RETRY_SECONDS
    MAX_RETRY_SECONDS = 3600
    MAX_TRADE_ATTEMPTS = 6
    LATE_RETRY_SECONDS = 600
    MAX_LATE_RETRIES = 12
    TICK_SECONDS = 30

    STAMP = "last_trade_run.txt"   # holds the Vietnam date of the last scheduled run
    THREAD = "daily-scheduler"

    def __init__(self, settings: Settings, bot=None, notify=None):
        self.settings = settings
        self.bot = bot
        self.notify = notify or (lambda text: None)
        self._trade_stamp = settings.data_dir / self.STAMP
        self._trade_next_try = 0.0
        self._trade_failures = 0
        self._late_retries = 0
        self._alerted = ""          # the day whose failure the owner was already told about
        self._reached: str | None = None
        self._started = False
        self._thread = threading.Thread(target=self._loop, name=self.THREAD, daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def enabled(self) -> bool:
        return self.bot is not None and self.settings.trade_mode != "off"

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _trade_due(self) -> str | None:
        """Today's Vietnam date, once per day from the run time."""
        now = clock.now_vn()
        if now.strftime("%H:%M") < self.settings.run_time:
            return None
        day = now.strftime("%Y-%m-%d")
        return None if self._read(self._trade_stamp) == day else day

    def _trade(self, final: str) -> bool:
        """Bring the books up to ``final`` (the last closed UTC day); True when done."""
        self._reached = None
        try:
            result = self.bot.catch_up(final)
            self._reached = result.day
            if result.new:
                self.notify(result.text)
        except Exception as exc:  # noqa: BLE001 - retried with backoff
            logger.exception("Trading bot run up to %s failed", final)
            if self._alerted != final:
                self._alerted = final
                self.notify(f"❌ Bot giao dịch lỗi khi xử lý nến {final}: {exc}. Bot sẽ tự thử lại "
                            f"(tối đa {self.MAX_TRADE_ATTEMPTS} lần hôm nay) và chạy bù ngày bị lỡ.")
            return False
        return True

    def _run_trading(self, day: str, *, scheduled: bool) -> None:
        """One attempt: on success the stamp (scheduled runs); on failure a later retry."""
        final = clock.final_day()
        if self._trade(final):
            self._trade_failures, self._trade_next_try = 0, 0.0
            late = (self._reached or "") < final
            if scheduled and late and self._late_retries < self.MAX_LATE_RETRIES:
                # The last closed candle is not published yet (a lagging source): try again soon.
                self._late_retries += 1
                logger.info("Trading run reached only %s, candle %s not available yet; retrying in %ds",
                            self._reached, final, self.LATE_RETRY_SECONDS)
                self._trade_next_try = time.time() + self.LATE_RETRY_SECONDS
                return
            if scheduled and late:
                self.notify(f"⚠️ Chưa lấy được nến {final} sau {self._late_retries} lần thử; "
                            f"sổ đang ở ngày {self._reached or '-'}. Lần chạy sau sẽ chạy bù.")
            if scheduled:
                self._late_retries = 0
                write_text_atomic(self._trade_stamp, day)
            return
        self._trade_failures += 1
        if self._trade_failures >= self.MAX_TRADE_ATTEMPTS:
            logger.error("Trading run %s failed %d times; next attempt at the next run", day, self._trade_failures)
            if scheduled:
                write_text_atomic(self._trade_stamp, day)
            self.notify(f"⚠️ Bot giao dịch lỗi {self._trade_failures} lần ngày {day}; tạm dừng tới lần chạy sau "
                        "(các ngày bị lỡ sẽ được chạy bù), hoặc gửi /chay khi dữ liệu ổn định.")
            self._trade_failures, self._trade_next_try, self._late_retries = 0, 0.0, 0
            return
        delay = min(self.RETRY_SECONDS * 2 ** (self._trade_failures - 1), self.MAX_RETRY_SECONDS)
        logger.warning("Trading run %s failed (attempt %d); retrying in %ds", day, self._trade_failures, delay)
        self._trade_next_try = time.time() + delay

    def tick(self) -> None:
        if not self.enabled:
            return
        if not self._started:
            self._started = True
            # Catch up the days missed while the server was down (closed candles only).
            self._run_trading(clock.now_vn().strftime("%Y-%m-%d"), scheduled=False)
        day = self._trade_due()
        if day and time.time() >= self._trade_next_try:
            self._run_trading(day, scheduled=True)

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the scheduler must keep ticking
                logger.exception("Daily scheduler tick failed")
            time.sleep(self.TICK_SECONDS)


class FuturesScheduler(DailyScheduler):
    """The same daily schedule, retries and catch-up for the futures books (``bot.futures_bot``)."""

    STAMP = "last_futures_run.txt"
    THREAD = "futures-scheduler"

    @property
    def enabled(self) -> bool:
        return self.bot is not None and self.settings.futures_mode != "off"
