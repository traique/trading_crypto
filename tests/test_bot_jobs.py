"""The analysis queue and the adapter to the multi-agent graph (faked, no network, no AI)."""

import os
import sys
import time
import types

import pytest

from bot import analysis, jobs as jobs_module
from bot.analysis import AnalysisUnavailable, Analyst
from bot.jobs import JobQueue, QueueFull
from tests import test_bot_support as support

settings, at, market = support.settings, support.at, support.market

pytestmark = pytest.mark.unit


class _Analyst:
    def __init__(self, outcome="ok"):
        self.outcome, self.calls = outcome, []

    def run(self, symbol, day, report_dir, **kwargs):
        self.calls.append((symbol, day))
        self.kwargs = kwargs
        if self.outcome == "unavailable":
            raise AnalysisUnavailable("chưa cấu hình nhà cung cấp AI nào")
        if self.outcome == "boom":
            raise RuntimeError("provider exploded")
        report_dir.mkdir(parents=True, exist_ok=True)
        html_path = analysis.write_html(report_dir, f"{symbol} {day}", "# Kết luận\nGiữ")
        return analysis.Analysis("Hold", "Giữ BTC", report_dir, html_path)


@pytest.fixture
def queue(settings, market):
    q = JobQueue(settings)
    q.events = []
    q.add_listener(lambda job, event: q.events.append((job.ticker, event)))
    return q


def test_a_job_sends_the_quick_view_first_then_the_report(queue):
    queue.analyst = _Analyst()
    job = queue.submit("BTCUSDT", "2026-10-14", "telegram", 111)
    queue.process(job)
    assert queue.events == [("BTCUSDT", "quick"), ("BTCUSDT", "done")]
    assert job.status == "done" and job.action == "Hold" and job.quick_summary.startswith("⚡ BTC/USDT")
    assert job.html_path is not None and "Kết luận" in job.html_path.read_text(encoding="utf-8")


def test_without_ai_the_job_ends_with_the_quick_view_and_a_clear_note(queue):
    queue.analyst = _Analyst("unavailable")
    job = queue.submit("ETHUSDT", "2026-10-14", "web")
    queue.process(job)
    assert job.status == "done" and job.error == "" and job.html_path is None
    assert "Phân tích AI chưa dùng được: chưa cấu hình nhà cung cấp AI nào" in job.note and "/nhanh ETH" in job.note


def test_a_failing_analysis_is_reported_and_the_worker_goes_on(queue):
    queue.analyst = _Analyst("boom")
    job = queue.submit("BTCUSDT", "2026-10-14", "web")
    queue.process(job)
    assert job.status == "failed" and "provider exploded" in job.error and queue.events[-1] == ("BTCUSDT", "failed")


def test_a_restart_marks_unfinished_jobs_failed(settings, queue):
    job = queue.submit("BTCUSDT", "2026-10-14", "web")
    again = JobQueue(settings)
    assert again.get(job.id).status == "failed" and "khởi động lại" in again.get(job.id).error


def test_the_same_pair_and_day_join_the_waiting_job(queue):
    first = queue.submit("BTCUSDT", "2026-10-14", "telegram", 1)
    second = queue.submit("BTCUSDT", "2026-10-14", "web")
    third = queue.submit("BTCUSDT", "2026-10-14", "telegram", 2)
    assert first is second is third and first.chat_ids == [1, 2] and first.sources == ["telegram", "web"]
    assert queue.submit("BTCUSDT", "2026-10-13", "web") is not first


def test_people_cannot_queue_more_than_max_pending(settings, queue):
    object.__setattr__(settings, "max_pending", 2)
    queue.submit("BTCUSDT", "2026-10-14", "web")
    queue.submit("ETHUSDT", "2026-10-14", "web")
    with pytest.raises(QueueFull, match="tối đa 2"):
        queue.submit("SOLUSDT", "2026-10-14", "web")


def test_the_history_is_bounded(queue, monkeypatch):
    monkeypatch.setattr(jobs_module, "MAX_JOBS_KEPT", 5)
    for i in range(8):
        job = queue.submit(f"C{i}USDT", "2026-10-14", "web")
        job.status = "done"
    assert len(queue.recent(100)) == 5


def test_old_report_folders_are_pruned(settings, queue):
    object.__setattr__(settings, "keep_reports", 2)
    root = settings.data_dir / "results" / "reports"
    for i in range(4):
        (root / f"old{i}").mkdir(parents=True)
        os.utime(root / f"old{i}", (time.time() - 1000 + i, time.time() - 1000 + i))
    queue._prune_reports()
    assert sorted(p.name for p in root.iterdir()) == ["old2", "old3"]


# ─── the adapter ──────────────────────────────────────────────────────────────


@pytest.fixture
def no_graph(monkeypatch):
    monkeypatch.setitem(sys.modules, analysis.GRAPH_MODULE, None)        # import fails
    monkeypatch.setitem(sys.modules, analysis.FALLBACK_MODULE, None)


def test_without_the_graph_module_the_reason_says_so(no_graph, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert "cryptotrader.graph" in analysis.unavailable_reason()
    with pytest.raises(AnalysisUnavailable):
        Analyst().run("BTCUSDT", "2026-10-14", __import__("pathlib").Path("/nonexistent"))


def _fake_graph(monkeypatch, **attrs):
    module = types.ModuleType(analysis.GRAPH_MODULE)
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, analysis.GRAPH_MODULE, module)
    monkeypatch.setitem(sys.modules, analysis.FALLBACK_MODULE, None)
    return module


def test_without_an_ai_key_the_reason_says_so(monkeypatch):
    _fake_graph(monkeypatch, analyze=lambda *a, **k: {})
    assert "nhà cung cấp AI" in analysis.unavailable_reason()
    monkeypatch.setenv("ROUTER9_API_KEY", "k")                 # 9Router needs its endpoint too
    assert analysis.unavailable_reason() is not None
    monkeypatch.setenv("ROUTER9_BASE_URL", "https://r9.example/v1")
    assert analysis.unavailable_reason() is None and analysis.configured_order() == ["9Router"]


def test_a_graph_function_is_called_and_its_report_saved(monkeypatch, tmp_path):
    seen = {}

    def analyze(symbol, date, save_dir=None):
        seen.update(symbol=symbol, date=date, save_dir=save_dir)
        return {"rating": "Buy", "final_decision": "Mua", "report_markdown": "# Báo cáo\n<script>x</script>"}

    _fake_graph(monkeypatch, analyze=analyze)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    result = Analyst().run("BTCUSDT", "2026-10-14", tmp_path / "r")
    assert seen == {"symbol": "BTCUSDT", "date": "2026-10-14", "save_dir": tmp_path / "r"}
    assert result.rating == "Buy" and result.final_decision == "Mua"
    page = result.html_path.read_text(encoding="utf-8")
    assert "<h1>Báo cáo</h1>" in page and "<script>" not in page


def test_a_graph_class_with_propagate_is_used_once(monkeypatch, tmp_path):
    made = []

    class CryptoTradingGraph:
        def __init__(self):
            made.append(self)

        def propagate(self, symbol, date):
            return {"final_trade_decision": f"Giữ {symbol}"}, "Hold"

    _fake_graph(monkeypatch, CryptoTradingGraph=CryptoTradingGraph)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    analyst = Analyst()
    first = analyst.run("ETHUSDT", "2026-10-14", tmp_path / "a")
    analyst.run("ETHUSDT", "2026-10-13", tmp_path / "b")
    assert first.rating == "Hold" and first.final_decision == "Giữ ETHUSDT" and len(made) == 1
    assert first.html_path.exists()


def test_a_broken_graph_module_does_not_take_the_bot_down(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def broken(name, *a, **k):
        if name == analysis.GRAPH_MODULE:
            raise RuntimeError("bad install")
        return real_import(name, *a, **k)

    monkeypatch.setattr(analysis.importlib, "import_module", broken)
    assert "cryptotrader.graph" in analysis.unavailable_reason()


def test_the_availability_check_follows_the_configured_provider(monkeypatch):
    for name in ("CRYPTO_AGENTS_LLM_PROVIDER", "CRYPTO_AGENTS_QUICK_THINK_LLM", "CRYPTO_AGENTS_DEEP_THINK_LLM",
                 "CRYPTO_AGENTS_QUICK_THINK_PROVIDER", "CRYPTO_AGENTS_DEEP_THINK_PROVIDER", "OPENAI_API_KEY",
                 "GROQ_API_KEY", "ROUTER9_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY", "PROVIDER_ORDER"):
        monkeypatch.delenv(name, raising=False)
    # OpenAI configured with its key and models, no chain keys: available.
    monkeypatch.setenv("CRYPTO_AGENTS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("CRYPTO_AGENTS_QUICK_THINK_LLM", "gpt-4o-mini")
    monkeypatch.setenv("CRYPTO_AGENTS_DEEP_THINK_LLM", "gpt-4o")
    assert analysis.unavailable_reason() is None
    # OpenAI named but only a chain key set: refused up front, naming the variable.
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert "OPENAI_API_KEY" in analysis.unavailable_reason()
    # A key but the model left at "default": refused with the variable to set.
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("CRYPTO_AGENTS_DEEP_THINK_LLM")
    assert "CRYPTO_AGENTS_DEEP_THINK_LLM" in analysis.unavailable_reason()
