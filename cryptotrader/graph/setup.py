"""The agent workflow: analysts and the engine side by side, then debates, trader, risk team, guard."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from cryptotrader.agents import (
    create_aggressive_debator,
    create_bear_researcher,
    create_bull_researcher,
    create_conservative_debator,
    create_market_analyst,
    create_neutral_debator,
    create_news_analyst,
    create_portfolio_manager,
    create_research_manager,
    create_sentiment_analyst,
    create_tokenomics_analyst,
    create_trader,
)
from cryptotrader.agents.analysts.turn import WRAP_UP
from cryptotrader.agents.output_guard import create_output_guard, guard_early
from cryptotrader.agents.quant_engine import create_quant_engine
from cryptotrader.agents.state import AgentState

from .analyst_execution import DEFAULT_ANALYSTS, build_analyst_execution_plan
from .conditional_logic import ConditionalLogic

logger = logging.getLogger(__name__)

# Every target a shared router can return, so a fall-through never hits a missing path.
DEBATE_PATH_MAP = {n: n for n in ("Bull Researcher", "Bear Researcher", "Research Manager")}
RISK_ANALYSIS_PATH_MAP = {n: n for n in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst",
                                         "Portfolio Manager")}


def _tools_or_done(state) -> str:
    return "tools" if state["messages"][-1].tool_calls else END


def _analyst_graph(spec, agent, max_tool_rounds: int):
    """One analyst as its own graph on a private message history, returning only its report.

    After ``max_tool_rounds`` rounds of tool calls it is told to write its report,
    and that turn ends it whatever it answers.
    """
    output = TypedDict(f"{spec.key.capitalize()}Report", {spec.report_key: str, "source_data": list})
    graph = StateGraph(AgentState, output_schema=output)
    graph.add_node("agent", agent)
    graph.add_edge(START, "agent")
    if not spec.tools:
        graph.add_edge("agent", END)
        return graph.compile()

    def calls(messages):
        return [c["name"] for m in messages for c in (getattr(m, "tool_calls", None) or [])]

    def more_or_wrap_up(state) -> str:
        rounds = sum(1 for m in state["messages"] if getattr(m, "tool_calls", None))
        return "wrap_up" if rounds >= max_tool_rounds else "agent"

    def wrap_up(state):
        used = ", ".join(f"{n} x{c}" for n, c in Counter(calls(state["messages"])).most_common())
        logger.warning("%s used its %d tool rounds (%s); asking for its report", spec.agent_node, max_tool_rounds, used)
        return agent({**state, "messages": [*state["messages"], HumanMessage(WRAP_UP)]})

    tool_node = ToolNode(list(spec.tools))

    def tools(state, config=None):
        out = tool_node.invoke(state, config)
        if spec.trusted_tools and isinstance(out, dict):
            data = [m.content for m in out.get("messages", [])
                    if isinstance(m, ToolMessage) and isinstance(m.content, str) and m.content]
            out = {**out, "source_data": data}
        return out

    graph.add_node("tools", tools)
    graph.add_node("wrap_up", wrap_up)
    graph.add_conditional_edges("agent", _tools_or_done, ["tools", END])
    graph.add_conditional_edges("tools", more_or_wrap_up, ["agent", "wrap_up"])
    graph.add_edge("wrap_up", END)
    return graph.compile()


class GraphSetup:
    def __init__(self, quick_thinking_llm: Any, deep_thinking_llm: Any, conditional_logic: ConditionalLogic,
                 max_tool_rounds: int, engine_config: dict | None = None):
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.conditional_logic = conditional_logic
        self.max_tool_rounds = max_tool_rounds
        self.engine_config = engine_config or {}

    def setup_graph(self, selected_analysts=DEFAULT_ANALYSTS) -> StateGraph:
        specs = build_analyst_execution_plan(selected_analysts)
        quick, deep = self.quick_thinking_llm, self.deep_thinking_llm
        factories = {
            "market": lambda: create_market_analyst(quick),
            "sentiment": lambda: create_sentiment_analyst(quick),
            "news": lambda: create_news_analyst(quick),
            "tokenomics": lambda: create_tokenomics_analyst(quick),
        }
        workflow = StateGraph(AgentState)
        for spec in specs:
            workflow.add_node(spec.agent_node, _analyst_graph(spec, factories[spec.key](), self.max_tool_rounds))
        workflow.add_node("Quant Engine", create_quant_engine(self.engine_config))
        workflow.add_node("Bull Researcher", create_bull_researcher(quick))
        workflow.add_node("Bear Researcher", create_bear_researcher(quick))
        workflow.add_node("Research Manager",
                          guard_early(create_research_manager(deep), "investment_plan", "Research Manager"))
        workflow.add_node("Trader", guard_early(create_trader(quick), "trader_investment_plan", "Trader"))
        workflow.add_node("Aggressive Analyst", create_aggressive_debator(quick))
        workflow.add_node("Conservative Analyst", create_conservative_debator(quick))
        workflow.add_node("Neutral Analyst", create_neutral_debator(quick))
        workflow.add_node("Portfolio Manager", create_portfolio_manager(deep))
        workflow.add_node("Output Guard", create_output_guard())

        # The analysts and the engine work at the same time; the debate starts when all are done.
        first_steps = [spec.agent_node for spec in specs] + ["Quant Engine"]
        for node in first_steps:
            workflow.add_edge(START, node)
        workflow.add_edge(first_steps, "Bull Researcher")
        for node in ("Bull Researcher", "Bear Researcher"):
            workflow.add_conditional_edges(node, self.conditional_logic.should_continue_debate, DEBATE_PATH_MAP)
        workflow.add_edge("Research Manager", "Trader")
        workflow.add_edge("Trader", "Aggressive Analyst")
        for node in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
            workflow.add_conditional_edges(node, self.conditional_logic.should_continue_risk_analysis,
                                           RISK_ANALYSIS_PATH_MAP)
        workflow.add_edge("Portfolio Manager", "Output Guard")
        workflow.add_edge("Output Guard", END)
        return workflow
