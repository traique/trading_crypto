"""One gate in front of every LLM request of the process: sequential calls and call budgets.

A small gateway such as 9Router is easily overloaded: one /phantich runs a
few dozen model calls (analysts with tool rounds, the debate, the risk round,
the managers), and a burst of them can trip its own rate limits. Every request
the provider chain sends (``fallback_client.run_chain``, once per provider
tried) first passes this gate, which:

- lets at most ``LLM_MAX_CONCURRENT`` requests run at once (default 1: strictly
  one after the other);
- spaces the starts of two requests by at least ``LLM_MIN_INTERVAL_SEC``
  (default 1.5 s);
- keeps at most ``LLM_MAX_CALLS_PER_MINUTE`` / ``_PER_HOUR`` requests in any
  sliding minute / hour (defaults 20 / 0 = off): a request over the limit
  WAITS for a free slot, at most ``LLM_MAX_WAIT_SEC`` (default 300 s);
- allows at most ``LLM_MAX_CALLS_PER_DAY`` requests per Vietnam calendar day
  (default 0 = off), and at most ``LLM_MAX_CALLS_PER_ANALYSIS`` per analysis
  (default 60, see ``analysis_budget``): over these the request is REFUSED at
  once with ``LLMBudgetExceeded``.

A refused request is not a provider failure: the chain stops (it does not try
the next provider, which would spend the budget again) and the agents' model
answers with the "AI unavailable" text, so the analysis still completes with
the system's own decision. Every value 0 means "no limit" (for
``LLM_MAX_CONCURRENT``, 0 is read as 1).

The counters live in memory (a restart clears them) and are shared by every
model of the process; ``gate_status()`` reports them for /trangthai and the web.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_VN = timezone(timedelta(hours=7))   # Vietnam time has no daylight saving


class LLMBudgetExceeded(RuntimeError):
    """A request refused by a call budget (per day or per analysis); the message is for people."""


def _env_num(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    return value if value >= 0 and value == value and value != float("inf") else default


def limits() -> dict:
    """The gate's limits, read from the environment on every call (so a .env loaded late counts)."""
    return {
        "max_concurrent": max(1, int(_env_num("LLM_MAX_CONCURRENT", 1))),
        "min_interval": _env_num("LLM_MIN_INTERVAL_SEC", 1.5),
        "per_minute": int(_env_num("LLM_MAX_CALLS_PER_MINUTE", 20)),
        "per_hour": int(_env_num("LLM_MAX_CALLS_PER_HOUR", 0)),
        "per_day": int(_env_num("LLM_MAX_CALLS_PER_DAY", 0)),
        "per_analysis": int(_env_num("LLM_MAX_CALLS_PER_ANALYSIS", 60)),
        "max_wait": _env_num("LLM_MAX_WAIT_SEC", 300),
    }


class _Budget:
    """Calls left for one analysis (shared by every thread the analysis runs in)."""

    def __init__(self, limit: int, label: str):
        self.limit, self.label, self.used = int(limit), label, 0
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            if self.limit and self.used >= self.limit:
                raise LLMBudgetExceeded(
                    f"đã dùng hết {self.limit} lượt gọi AI cho lượt phân tích {self.label} "
                    "(LLM_MAX_CALLS_PER_ANALYSIS)")
            self.used += 1

    def give_back(self) -> None:
        with self._lock:
            self.used = max(0, self.used - 1)


_current_budget: ContextVar[_Budget | None] = ContextVar("llm_analysis_budget", default=None)


class RateGate:
    """The process-wide gate; see the module docstring. ``clock``/``sleep`` are for tests."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep, today=None):
        self._clock, self._sleep = clock, sleep
        self._today = today or (lambda: datetime.now(_VN).strftime("%Y-%m-%d"))
        self._cond = threading.Condition()
        self._running = 0
        self._waiting = 0
        self._last_start = -1e18
        self._starts: deque[float] = deque()      # start times within the last hour
        self._day = ""
        self._day_count = 0
        self._total = 0
        self._refused = 0

    # ── bookkeeping (under self._cond) ──
    def _prune(self, now: float) -> None:
        while self._starts and now - self._starts[0] >= 3600:
            self._starts.popleft()

    def _in_window(self, now: float, seconds: float) -> int:
        return sum(1 for t in self._starts if now - t < seconds)

    def _wait_needed(self, now: float, lim: dict) -> float:
        """Seconds until a new request may start (0 = now)."""
        if self._running >= lim["max_concurrent"]:
            return 0.25                                  # woken by release(); poll as a fallback
        wait = max(0.0, self._last_start + lim["min_interval"] - now)
        for seconds, cap in ((60, lim["per_minute"]), (3600, lim["per_hour"])):
            if cap and self._in_window(now, seconds) >= cap:
                inside = [t for t in self._starts if now - t < seconds]
                wait = max(wait, inside[-cap] + seconds - now)
        return wait

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._day_count = today, 0

    # ── the gate ──
    def acquire(self, label: str = "") -> None:
        lim = limits()
        budget = _current_budget.get()
        with self._cond:
            self._roll_day()
            if lim["per_day"] and self._day_count >= lim["per_day"]:
                self._refused += 1
                raise LLMBudgetExceeded(f"đã dùng hết {lim['per_day']} lượt gọi AI hôm nay "
                                        "(LLM_MAX_CALLS_PER_DAY); hạn mức mở lại lúc 00:00 giờ VN")
            if budget is not None:
                try:
                    budget.take()
                except LLMBudgetExceeded:
                    self._refused += 1
                    raise
            deadline = self._clock() + lim["max_wait"]
            self._waiting += 1
            try:
                while True:
                    now = self._clock()
                    self._prune(now)
                    wait = self._wait_needed(now, lim)
                    if wait <= 0:
                        break
                    if now + wait > deadline:
                        self._refused += 1
                        if budget is not None:
                            budget.give_back()               # not spent: it never ran
                        raise LLMBudgetExceeded(
                            f"gateway AI đang bận quá {lim['max_wait']:.0f}s (giới hạn lượt gọi mỗi phút/giờ)")
                    if wait > 1 and label:
                        logger.info("LLM gate: %s waits %.1fs", label, wait)
                    # Condition.wait releases the lock; with a fake clock (tests) sleep instead.
                    if self._sleep is time.sleep:
                        self._cond.wait(timeout=min(wait, 5.0))
                    else:
                        self._cond.release()
                        try:
                            self._sleep(wait)
                        finally:
                            self._cond.acquire()
            finally:
                self._waiting -= 1
            now = self._clock()
            self._running += 1
            self._last_start = now
            self._starts.append(now)
            self._day_count += 1
            self._total += 1

    def release(self) -> None:
        with self._cond:
            self._running = max(0, self._running - 1)
            self._cond.notify_all()

    @contextlib.contextmanager
    def slot(self, label: str = "") -> Iterator[None]:
        self.acquire(label)
        try:
            yield
        finally:
            self.release()

    def status(self) -> dict:
        lim = limits()
        with self._cond:
            self._roll_day()
            now = self._clock()
            self._prune(now)
            return {**lim, "running": self._running, "waiting": self._waiting,
                    "last_minute": self._in_window(now, 60), "last_hour": self._in_window(now, 3600),
                    "today": self._day_count, "total": self._total, "refused": self._refused}

    def reset(self) -> None:
        with self._cond:
            self._running = self._waiting = 0
            self._last_start = -1e18
            self._starts.clear()
            self._day, self._day_count, self._total, self._refused = "", 0, 0, 0
            self._cond.notify_all()


GATE = RateGate()


def gate_status() -> dict:
    return GATE.status()


def status_line() -> str:
    """One Vietnamese line for /trangthai and the web page."""
    s = GATE.status()
    day = f"{s['today']}/{s['per_day']}" if s["per_day"] else f"{s['today']} (không giới hạn)"
    minute = f"{s['last_minute']}/{s['per_minute']}" if s["per_minute"] else str(s["last_minute"])
    mode = "tuần tự" if s["max_concurrent"] == 1 else f"tối đa {s['max_concurrent']} cùng lúc"
    return (f"Giới hạn gọi AI: {mode}, cách nhau ≥{s['min_interval']:g}s; phút này {minute}; hôm nay {day}; "
            f"đang chạy {s['running']}, đang chờ {s['waiting']}; bị từ chối {s['refused']}")


@contextlib.contextmanager
def analysis_budget(label: str = "", limit: int | None = None) -> Iterator[_Budget]:
    """Count the LLM requests made inside the block against one analysis' budget.

    The budget follows the context (threads started with a copied context, as
    LangGraph does, share it)."""
    n = limits()["per_analysis"] if limit is None else int(limit)
    budget = _Budget(n, label)
    token = _current_budget.set(budget)
    try:
        yield budget
    finally:
        _current_budget.reset(token)
