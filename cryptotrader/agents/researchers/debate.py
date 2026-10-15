"""The bull and bear researchers: one debate turn each, over the analysts' reports and the system decision."""

from __future__ import annotations

from cryptotrader.agents import crypto_context
from cryptotrader.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    opponent_argument_or_opening,
    report_or_absent,
)

BULL_BRIEF = """You are the Bull Analyst in an internal debate on a crypto asset. Build the strongest upside case the data can defend: adoption and network activity, catalysts, favourable supply dynamics, positive trend and sentiment readings. The system has already fixed the action ({action}); you do not need to agree with it, but you cannot change it. If the action is not BUY, frame your case as "the scenario to watch", not as advice to buy.

Focus on:
- Catalysts and adoption: upgrades, listings, ETF or institutional flows, ecosystem growth - only those in the reports.
- Positive indicators: trend, momentum, sentiment and supply data from the reports.
- Bear counterpoints: answer the bear's points with specific data.
- Engage conversationally with the bear's arguments rather than listing facts."""

BEAR_BRIEF = """You are the Bear Analyst in an internal debate on a crypto asset. Make the strongest case against it: risks, weaknesses and negative indicators, and rebut the bull directly. The system has already fixed the action ({action}); you may argue it is too optimistic, but you cannot change it. This is spot only: a bearish view means "do not buy" or "exit a holding", never "short it".

Focus on:
- Crypto-specific risks: volatility and drawdowns of 50-80%, token unlocks and dilution, exchange and custody failure, hacks, regulation, delisting, stablecoin de-pegs, thin liquidity.
- Negative indicators from the reports: trend, momentum, crowded leverage (funding/open interest), news.
- Bull counterpoints: expose over-optimistic assumptions with specific data.
- Engage conversationally with the bull's arguments rather than listing facts."""


def _prompt(brief: str, state, last_label: str, last: str, history: str) -> str:
    return f"""{brief.format(action=crypto_context.action_of(state))}

Resources available:
{get_instrument_context_from_state(state)}
{crypto_context.quant_block(state)}

Technical market report: {report_or_absent(state.get("market_report"), "market")}
Sentiment report: {report_or_absent(state.get("sentiment_report"), "sentiment")}
News report: {report_or_absent(state.get("news_report"), "news")}
Tokenomics report: {report_or_absent(state.get("tokenomics_report"), "tokenomics")}
Conversation history of the debate: {history}
{last_label}: {last}

{crypto_context.NO_INVENT_RULE}

{crypto_context.NO_LEVERAGE_RULE}

{crypto_context.GROUNDING_RULES}
""" + get_language_instruction()


def _create(llm, side: str):
    brief, opponent, mine = ((BULL_BRIEF, "bear analyst", "bull_history") if side == "Bull"
                             else (BEAR_BRIEF, "bull analyst", "bear_history"))
    other = "bear_history" if side == "Bull" else "bull_history"

    def node(state) -> dict:
        debate = state["investment_debate_state"]
        history = debate.get("history", "")
        last = opponent_argument_or_opening(debate.get("current_response", ""), opponent)
        response = llm.invoke(_prompt(brief, state, f"Last {opponent} argument", last, history))
        argument = f"{side} Analyst: {response.content}"
        return {"investment_debate_state": {
            "history": history + "\n" + argument,
            mine: debate.get(mine, "") + "\n" + argument,
            other: debate.get(other, ""),
            "current_response": argument,
            "count": debate["count"] + 1,
        }}

    return node


def create_bull_researcher(llm):
    return _create(llm, "Bull")


def create_bear_researcher(llm):
    return _create(llm, "Bear")
