"""The LLM gate in front of 9Router: one request at a time, spaced, capped per minute/day/analysis."""

import threading
import time

import pytest
from langchain_core.messages import HumanMessage

from cryptotrader.llm_clients import fallback_client as fc, rate_gate as rg
from tests.test_llm_fallback_client import Scripted


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.t += seconds


def _gate(clock, day="2026-10-15"):
    return rg.RateGate(clock=clock, sleep=clock.sleep, today=lambda: day)


def test_requests_are_spaced_by_the_min_interval(monkeypatch):
    monkeypatch.setenv("LLM_MIN_INTERVAL_SEC", "2")
    monkeypatch.setenv("LLM_MAX_INTERVAL_SEC", "2")
    clock = FakeClock()
    gate = _gate(clock)
    for _ in range(3):
        with gate.slot():
            pass
    assert clock.slept == [2.0, 2.0]
    assert gate.status()["today"] == 3


def test_the_pause_is_random_between_min_and_max_and_counts_from_the_end_of_the_call(monkeypatch):
    monkeypatch.setenv("LLM_MIN_INTERVAL_SEC", "10")
    monkeypatch.setenv("LLM_MAX_INTERVAL_SEC", "20")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_MINUTE", "0")
    clock = FakeClock()
    gate = rg.RateGate(clock=clock, sleep=clock.sleep, today=lambda: "2026-10-15")
    pauses = []
    for _ in range(20):
        with gate.slot():
            clock.t += 7.0                 # the call itself takes 7 s
        before = len(clock.slept)
        with gate.slot():
            pass
        pauses += clock.slept[before:]
    assert all(10.0 <= p <= 20.0 for p in pauses)
    assert len({round(p, 1) for p in pauses}) > 5      # not a fixed rhythm


def test_defaults_fit_a_small_gateway(monkeypatch):
    for name in ("LLM_MIN_INTERVAL_SEC", "LLM_MAX_INTERVAL_SEC", "LLM_MAX_CALLS_PER_ANALYSIS"):
        monkeypatch.delenv(name, raising=False)
    lim = rg.limits()
    assert (lim["min_interval"], lim["max_interval"], lim["per_analysis"], lim["max_concurrent"]) == (10, 20, 10, 1)


def test_per_minute_cap_waits_for_a_free_slot(monkeypatch):
    monkeypatch.setenv("LLM_MIN_INTERVAL_SEC", "0")
    monkeypatch.setenv("LLM_MAX_INTERVAL_SEC", "0")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_MINUTE", "3")
    clock = FakeClock()
    gate = _gate(clock)
    for _ in range(4):
        with gate.slot():
            pass
    # The 4th waits until the first leaves the sliding minute.
    assert clock.slept == [60.0]


def test_wait_longer_than_max_wait_is_refused(monkeypatch):
    monkeypatch.setenv("LLM_MIN_INTERVAL_SEC", "0")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_MINUTE", "1")
    monkeypatch.setenv("LLM_MAX_WAIT_SEC", "10")
    clock = FakeClock()
    gate = _gate(clock)
    with gate.slot():
        pass
    with pytest.raises(rg.LLMBudgetExceeded, match="bận"):
        gate.acquire()
    assert gate.status()["refused"] == 1


def test_daily_cap_refuses_and_resets_next_day(monkeypatch):
    monkeypatch.setenv("LLM_MIN_INTERVAL_SEC", "0")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_DAY", "2")
    clock = FakeClock()
    day = {"v": "2026-10-15"}
    gate = rg.RateGate(clock=clock, sleep=clock.sleep, today=lambda: day["v"])
    for _ in range(2):
        with gate.slot():
            pass
    with pytest.raises(rg.LLMBudgetExceeded, match="hôm nay"):
        gate.acquire()
    day["v"] = "2026-10-16"
    with gate.slot():
        pass
    assert gate.status()["today"] == 1


def test_analysis_budget_caps_one_analysis_only(monkeypatch):
    monkeypatch.setenv("LLM_MIN_INTERVAL_SEC", "0")
    clock = FakeClock()
    gate = _gate(clock)
    with rg.analysis_budget("BTCUSDT 2026-10-14", limit=2) as budget:
        for _ in range(2):
            with gate.slot():
                pass
        with pytest.raises(rg.LLMBudgetExceeded, match="lượt phân tích"):
            gate.acquire()
        assert budget.used == 2
    with gate.slot():          # outside the analysis: no budget
        pass


def test_one_request_at_a_time_across_threads(monkeypatch):
    monkeypatch.setenv("LLM_MIN_INTERVAL_SEC", "0")
    monkeypatch.setenv("LLM_MAX_CONCURRENT", "1")
    gate = rg.RateGate()
    running, peak = [0], [0]
    lock = threading.Lock()

    def work():
        with gate.slot():
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.02)
            with lock:
                running[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1
    assert gate.status()["total"] == 6


def test_refused_call_does_not_try_the_next_provider(monkeypatch):
    monkeypatch.setenv("LLM_MAX_CALLS_PER_DAY", "1")
    router9 = Scripted(name_="router9")
    groq = Scripted(name_="groq")
    model = fc.FallbackChatModel(chain=[("router9", router9), ("groq", groq)], degrade=True)
    first = model.invoke([HumanMessage(content="hi")])
    assert first.content == "router9:ok"
    second = model.invoke([HumanMessage(content="hi")])
    assert second.content.startswith(fc.AI_UNAVAILABLE_MARK)
    assert "hôm nay" in second.content
    assert len(router9.calls) == 1 and not groq.calls
    # A refused budget does not mark the chain down: tomorrow's (or a raised limit's) call goes through.
    monkeypatch.setenv("LLM_MAX_CALLS_PER_DAY", "0")
    assert model.invoke([HumanMessage(content="hi")]).content == "router9:ok"


def test_status_line_is_readable(monkeypatch):
    monkeypatch.setenv("LLM_MAX_CALLS_PER_DAY", "200")
    monkeypatch.setenv("LLM_MIN_INTERVAL_SEC", "10")
    monkeypatch.setenv("LLM_MAX_INTERVAL_SEC", "20")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_ANALYSIS", "10")
    line = rg.status_line()
    assert "tuần tự" in line and "/200" in line and "10-20s" in line and "10 lượt/phân tích" in line
