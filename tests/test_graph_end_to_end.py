"""The whole graph, end to end, with scripted models and no network.

Every tool-using analyst calls each of its tools once, the pre-fetching
analysts read their (faked) sources, the engine decides, the debates and
managers run, the Output Guard checks, and the reports are written. This pins
the wiring: a restructure that drops a node, a tool or an edge fails here.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from cryptotrader.agents import quant_engine, schemas
from cryptotrader.agents.report_format import STANDARD_DISCLAIMER
from cryptotrader.dataflows import indicators, news, sentiment, tokenomics
from cryptotrader.graph import trading_graph
from tests import helpers

pytestmark = pytest.mark.unit

DAY = "2026-10-14"
TEXT = "Báo cáo dựa trên dữ liệu.\n\n**Rating**: BUY\n\nFINAL TRANSACTION PROPOSAL: **BUY**"

STRUCTURED = {
    schemas.ResearchPlan: schemas.ResearchPlan(stance=schemas.Stance.AGREE, rationale="r", strategic_actions="a"),
    schemas.TraderProposal: schemas.TraderProposal(reasoning="r", execution_notes="lệnh limit, phí 0.1%"),
    schemas.PortfolioDecision: schemas.PortfolioDecision(executive_summary="s", key_risks="k",
                                                         investment_thesis="Xu hướng tăng."),
    schemas.SentimentReport: schemas.SentimentReport(overall_band=schemas.SentimentBand.NEUTRAL, overall_score=5.0,
                                                     confidence="low", narrative="n"),
}

ARGS = {"start_date": "2026-10-07", "end_date": DAY, "curr_date": DAY, "indicator": "rsi"}


class ScriptedModel(BaseChatModel):
    """Calls every bound tool once, then answers with ``reply``."""

    structured: bool = False
    reply: str = TEXT
    tools: tuple = ()
    calls: list = Field(default_factory=list)
    tool_results: list = Field(default_factory=list)
    threads: set = Field(default_factory=set)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tools": tuple(tools)})

    def with_structured_output(self, schema, **kwargs):
        if not self.structured:
            raise NotImplementedError
        return RunnableLambda(lambda _: self.calls.append(schema.__name__) or STRUCTURED[schema])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append("text")
        if self.tools:
            self.threads.add(threading.current_thread().name)
            time.sleep(0.05)
        if self.tools and not isinstance(messages[-1], ToolMessage):
            calls = [{"name": t.name, "id": f"call_{i}",
                      "args": {k: v for k, v in ARGS.items()
                               if k in t.tool_call_schema.model_json_schema()["properties"]}}
                     for i, t in enumerate(self.tools)]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])
        self.tool_results += [m.content for m in messages if isinstance(m, ToolMessage)]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


class _Client:
    def __init__(self, model):
        self.model = model

    def get_llm(self):
        return self.model


RSS = ("<rss><channel><item><title>Bitcoin ETF inflows rise - Reuters</title>"
       "<pubDate>Tue, 13 Oct 2026 10:00:00 GMT</pubDate><source>Reuters</source></item></channel></rss>")


@pytest.fixture
def offline(monkeypatch):
    """Every source answers offline; returns the set of sources used."""
    used: set[str] = set()
    first = date(2025, 12, 19)
    frames = {"BTCUSDT": helpers.frame(first, 300, 50_000.0, 50.0),
              "ETHUSDT": helpers.frame(first, 300, 4_000.0, -5.0)}
    monkeypatch.setattr(indicators, "load_frame", lambda s, d: used.add("candles") or frames[s])
    monkeypatch.setattr(quant_engine, "_load",
                        lambda symbols, end, warmup: used.add("engine") or {s: frames[s] for s in symbols})
    monkeypatch.setattr(news, "FETCH", lambda url, params, source: used.add("news") or RSS)
    monkeypatch.setattr(sentiment, "FETCH_JSON",
                        lambda url, params, source: used.add("sentiment") or {"code": "1", "data": []})
    monkeypatch.setattr(tokenomics, "FETCH_JSON", lambda url, params, source: used.add("tokenomics") or {"coins": []})
    return used


def _graph(tmp_path, monkeypatch, model, **config):
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(model))
    return trading_graph.CryptoTradingGraph(config={"results_dir": str(tmp_path / "results"), **config})


@pytest.mark.parametrize("structured", [False, True], ids=["free-text", "structured"])
def test_a_full_run_reaches_a_guarded_decision(tmp_path, monkeypatch, offline, structured):
    model = ScriptedModel(structured=structured)
    graph = _graph(tmp_path, monkeypatch, model)

    state, rating = graph.propagate("btc", DAY)

    assert rating == state["final_rating"] == "BUY"
    for key in ("market_report", "sentiment_report", "news_report", "tokenomics_report", "quant_report",
                "investment_plan", "trader_investment_plan", "final_trade_decision"):
        assert state[key].strip(), key
    assert state["final_trade_decision"].startswith("**Rating**: BUY")
    assert state["final_trade_decision"].endswith(STANDARD_DISCLAIMER)
    assert state["guard_notes"] == ""
    assert offline == {"candles", "engine", "news", "sentiment", "tokenomics"}
    # Every tool answered with data, none with an error.
    assert model.tool_results and not [r for r in model.tool_results if r.startswith("<")]
    if structured:
        assert {"ResearchPlan", "TraderProposal", "PortfolioDecision", "SentimentReport"} <= set(model.calls)
    log = json.loads((tmp_path / "results/BTCUSDT/logs/full_states_log_2026-10-14.json").read_text())
    assert log["final_rating"] == "BUY" and log["quant_decision"]["strategy"] == "btc_sma_ens"


def test_the_analysts_run_at_the_same_time(tmp_path, monkeypatch, offline):
    model = ScriptedModel()
    _graph(tmp_path, monkeypatch, model).propagate("BTCUSDT", DAY)
    assert len(model.threads) > 1


def test_a_model_contradicting_the_system_is_overruled(tmp_path, monkeypatch, offline):
    # ETH is outside btc_sma_ens (BTC only): the system says NO_TRADE; every agent says "MUA".
    model = ScriptedModel(reply="Kết luận: MUA ngay, mục tiêu 9,999 USDT.")
    state, rating = _graph(tmp_path, monkeypatch, model).propagate("ETHUSDT", DAY)
    assert rating == "NO_TRADE"
    assert "MUA ngay" not in state["final_trade_decision"]
    assert "MUA ngay" not in state["trader_investment_plan"]
    assert {"Research Manager", "Trader", "Portfolio Manager"} <= {
        line.split(":")[0] for line in state["guard_notes"].splitlines()}


def test_reports_are_written_safely(tmp_path, monkeypatch, offline):
    model = ScriptedModel(reply="Báo cáo <script>alert(1)</script> ![x](https://evil/x.png)\n\n**Rating**: BUY")
    graph = _graph(tmp_path, monkeypatch, model)
    state, _ = graph.propagate("BTCUSDT", DAY)
    md = graph.save_reports(state, "BTCUSDT", save_path=tmp_path / "rep")
    page = (tmp_path / "rep" / "complete_report.html").read_text(encoding="utf-8")
    assert md.name == "complete_report.md" and (tmp_path / "rep/0_system/system.md").exists()
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "<img" not in page
    assert "default-src 'none'" in page and 'lang="vi"' in page
    assert "BTC/USDT" in page and "BUY (MUA)" in page


def test_analyze_is_what_the_bot_calls(tmp_path, monkeypatch, offline):
    model = ScriptedModel()
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(model))
    monkeypatch.setenv("CRYPTO_AGENTS_RESULTS_DIR", str(tmp_path / "results"))
    out = trading_graph.analyze("BTC", DAY, save_dir=tmp_path / "bot")
    assert out["rating"] == "BUY"
    assert out["html_path"] == tmp_path / "bot" / "complete_report.html"
    assert "Báo cáo phân tích BTCUSDT" in out["report_markdown"]


def test_the_bot_adapter_runs_the_graph(tmp_path, monkeypatch, offline):
    bot_analysis = pytest.importorskip("bot.analysis")
    model = ScriptedModel()
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(model))
    monkeypatch.setenv("CRYPTO_AGENTS_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert bot_analysis.unavailable_reason() is None
    result = bot_analysis.Analyst().run("BTCUSDT", DAY, tmp_path / "bot")
    assert result.rating == "BUY" and result.html_path.exists()


def test_a_future_or_malformed_date_and_a_non_usdt_pair_are_refused(tmp_path, monkeypatch, offline):
    graph = _graph(tmp_path, monkeypatch, ScriptedModel())
    with pytest.raises(ValueError):
        graph.propagate("BTCUSDT", "2999-01-01")
    with pytest.raises(ValueError):
        graph.propagate("BTCUSDT", "14/10/2026")
    with pytest.raises(ValueError):
        graph.propagate("ETH/BTC", DAY)


def test_too_many_tool_rounds_for_the_recursion_limit_is_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="max_recur_limit"):
        _graph(tmp_path, monkeypatch, ScriptedModel(), max_tool_rounds=60, max_recur_limit=100)


def test_state_logs_are_pruned_to_the_newest_few(tmp_path):
    import os

    directory = tmp_path / "BTCUSDT" / "logs"
    directory.mkdir(parents=True)
    for i in range(6):
        path = directory / f"full_states_log_2026-10-0{i + 1}.json"
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (1000 + i, 1000 + i))
    (directory / "other.txt").write_text("keep", encoding="utf-8")
    trading_graph.prune_state_logs(directory, 2)
    assert sorted(p.name for p in directory.iterdir()) == [
        "full_states_log_2026-10-05.json", "full_states_log_2026-10-06.json", "other.txt"]


def test_the_guard_reads_prices_from_the_analysts_data_not_their_prose(tmp_path, monkeypatch, offline):
    model = ScriptedModel()
    graph = _graph(tmp_path, monkeypatch, model)
    state, _ = graph.propagate("btc", DAY)
    data = state.get("source_data") or []
    assert data, "the market tools' outputs and the pre-fetched blocks are recorded"
    assert any(r in data for r in model.tool_results)                  # a market tool's output
    assert not any("Bitcoin ETF inflows rise" in block for block in data)   # never the news


class _Down(BaseChatModel):
    """A provider that never answers."""

    calls: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "down"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        import httpx
        import openai

        self.calls.append(1)
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://x"))

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        return RunnableLambda(lambda prompt: self._generate([]))


def test_a_run_with_every_ai_provider_down_still_reports_the_system_decision(tmp_path, monkeypatch, offline):
    from cryptotrader.llm_clients import fallback_client as fc

    fc.reset_provider_state()
    down = _Down()
    model = fc.FallbackChatModel(chain=[("groq", down)], degrade=True)
    try:
        graph = _graph(tmp_path, monkeypatch, model)
        state, rating = graph.propagate("btc", DAY)
    finally:
        fc.reset_provider_state()
    assert rating == state["quant_decision"]["action"] == "BUY"
    assert "AI không khả dụng" in state["guard_notes"]
    assert state["final_trade_decision"].startswith("**Rating**: BUY")
    assert "Tín hiệu hệ thống cho BTC/USDT: BUY" in state["final_trade_decision"]
    assert len(down.calls) <= 5      # at most the parallel analysts race it; not walked once per node
    graph.save_reports(state, "BTCUSDT", save_path=tmp_path / "rep")
    assert (tmp_path / "rep" / "complete_report.md").exists()


def test_an_analysis_over_its_llm_call_budget_still_completes(tmp_path, monkeypatch, offline):
    """LLM_MAX_CALLS_PER_ANALYSIS caps the requests of one run, the parallel analysts included
    (the budget follows the context into LangGraph's threads); the run still decides."""
    from cryptotrader.llm_clients import fallback_client as fc
    from cryptotrader.llm_clients.rate_gate import GATE

    monkeypatch.setenv("LLM_MAX_CALLS_PER_ANALYSIS", "3")
    fc.reset_provider_state()
    scripted = ScriptedModel()
    model = fc.FallbackChatModel(chain=[("groq", scripted)], degrade=True)
    graph = _graph(tmp_path, monkeypatch, model)
    state, rating = graph.propagate("btc", DAY)
    assert rating == state["quant_decision"]["action"] == "BUY"
    assert GATE.status()["total"] == 3          # requests that reached a provider
    assert GATE.status()["refused"] >= 1
    # The next analysis has its own budget.
    graph.propagate("btc", DAY)
    assert GATE.status()["total"] == 6


def test_the_lite_profile_fits_about_ten_llm_calls(tmp_path, monkeypatch, offline):
    """CRYPTO_AGENTS_PROFILE=lite: no debate, no risk team, one tool round per analyst; the
    managers still decide and the run ends with the system's rating."""
    from cryptotrader.llm_clients import fallback_client as fc
    from cryptotrader.llm_clients.rate_gate import GATE

    monkeypatch.setenv("CRYPTO_AGENTS_PROFILE", "lite")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_ANALYSIS", "10")
    fc.reset_provider_state()
    model = fc.FallbackChatModel(chain=[("groq", ScriptedModel())], degrade=True)
    graph = _graph(tmp_path, monkeypatch, model)
    assert graph.config["max_debate_rounds"] == 0 and graph.config["max_tool_rounds"] == 1
    assert "Bull Researcher" not in graph.workflow.nodes and "Aggressive Analyst" not in graph.workflow.nodes
    state, rating = graph.propagate("btc", DAY)
    assert rating == state["quant_decision"]["action"] == "BUY"
    for key in ("market_report", "investment_plan", "trader_investment_plan", "final_trade_decision"):
        assert state[key].strip(), key
    assert GATE.status()["total"] <= 10 and GATE.status()["refused"] == 0
    assert "AI không khả dụng" not in state["guard_notes"]
    graph.save_reports(state, "BTCUSDT", save_path=tmp_path / "rep")


def test_a_caller_config_beats_the_profile(tmp_path, monkeypatch, offline):
    monkeypatch.setenv("CRYPTO_AGENTS_PROFILE", "lite")
    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), max_debate_rounds=2)
    assert graph.config["max_debate_rounds"] == 2 and graph.config["max_risk_discuss_rounds"] == 0
    full = _graph(tmp_path, monkeypatch, ScriptedModel(), analysis_profile="full")
    assert full.config["max_risk_discuss_rounds"] == 1 and "Bull Researcher" in full.workflow.nodes
