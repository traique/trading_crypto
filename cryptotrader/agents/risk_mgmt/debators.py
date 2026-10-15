"""The three risk analysts (aggressive, conservative, neutral): one turn each over the trader's plan."""

from __future__ import annotations

from cryptotrader.agents import crypto_context
from cryptotrader.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
    opponent_argument_or_opening,
    report_or_absent,
)

ROLES = {
    "Aggressive": (
        "As the Aggressive Risk Analyst, champion the upside of executing the plan: the opportunity "
        "cost of standing aside in a fast market, momentum, and catalysts. Counter the conservative and "
        "neutral analysts point by point with data, showing where their caution misses the opportunity.",
        ("Conservative", "Neutral"),
    ),
    "Conservative": (
        "As the Conservative Risk Analyst, protect capital: stress the drawdowns crypto routinely "
        "suffers, exchange and custody risk, liquidity, dilution, regulation and the cost of fees and "
        "slippage on frequent trading. Counter the aggressive and neutral analysts point by point.",
        ("Aggressive", "Neutral"),
    ),
    "Neutral": (
        "As the Neutral Risk Analyst, weigh both sides: where the aggressive view is too optimistic and "
        "where the conservative view is too cautious, and argue for position sizing and diversification "
        "that keep a bad outcome survivable.",
        ("Aggressive", "Conservative"),
    ),
}


def _create(llm, role: str):
    brief, others = ROLES[role]
    key = role.lower()

    def node(state) -> dict:
        debate = state["risk_debate_state"]
        history = debate.get("history", "")
        replies = "\n".join(
            f"Last argument from the {o.lower()} analyst: "
            + opponent_argument_or_opening(debate.get(f"current_{o.lower()}_response", ""), f"{o.lower()} analyst")
            for o in others)
        prompt = f"""{brief}

The system has already fixed the action and every number of the plan in the block below. Debate the risk of executing that plan - and of standing aside - but do not propose different numbers or a different action. Spot only: no leverage, no shorting; the market trades 24/7, so a bad entry can move far overnight.

Here is the trader's plan:
{state.get("trader_investment_plan") or "(no trader plan)"}

{get_instrument_context_from_state(state)}
{get_portfolio_context_from_state(state)}
Technical market report: {report_or_absent(state.get("market_report"), "market")}
Sentiment report: {report_or_absent(state.get("sentiment_report"), "sentiment")}
News report: {report_or_absent(state.get("news_report"), "news")}
Tokenomics report: {report_or_absent(state.get("tokenomics_report"), "tokenomics")}
Conversation history: {history}
{replies}

Speak conversationally, without special formatting.

{crypto_context.quant_block(state)}

{crypto_context.NO_INVENT_RULE}

{crypto_context.NO_LEVERAGE_RULE}""" + get_language_instruction()
        response = llm.invoke(prompt)
        argument = f"{role} Analyst: {response.content}"
        new_state = {
            "history": history + "\n" + argument,
            "aggressive_history": debate.get("aggressive_history", ""),
            "conservative_history": debate.get("conservative_history", ""),
            "neutral_history": debate.get("neutral_history", ""),
            "latest_speaker": role,
            "current_aggressive_response": debate.get("current_aggressive_response", ""),
            "current_conservative_response": debate.get("current_conservative_response", ""),
            "current_neutral_response": debate.get("current_neutral_response", ""),
            "count": debate["count"] + 1,
        }
        new_state[f"{key}_history"] = debate.get(f"{key}_history", "") + "\n" + argument
        new_state[f"current_{key}_response"] = argument
        return {"risk_debate_state": new_state}

    return node


def create_aggressive_debator(llm):
    return _create(llm, "Aggressive")


def create_conservative_debator(llm):
    return _create(llm, "Conservative")


def create_neutral_debator(llm):
    return _create(llm, "Neutral")
