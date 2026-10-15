"""The graph state: the run's inputs, the engine's decision, every agent's output."""

import operator
from typing import Annotated

from langgraph.graph import MessagesState
from typing_extensions import TypedDict


class InvestDebateState(TypedDict):
    bull_history: Annotated[str, "Bullish conversation history"]
    bear_history: Annotated[str, "Bearish conversation history"]
    history: Annotated[str, "Conversation history"]
    current_response: Annotated[str, "Latest response"]
    count: Annotated[int, "Length of the current conversation"]


class RiskDebateState(TypedDict):
    aggressive_history: Annotated[str, "Aggressive analyst's conversation history"]
    conservative_history: Annotated[str, "Conservative analyst's conversation history"]
    neutral_history: Annotated[str, "Neutral analyst's conversation history"]
    history: Annotated[str, "Conversation history"]
    latest_speaker: Annotated[str, "Analyst that spoke last"]
    current_aggressive_response: Annotated[str, "Latest response by the aggressive analyst"]
    current_conservative_response: Annotated[str, "Latest response by the conservative analyst"]
    current_neutral_response: Annotated[str, "Latest response by the neutral analyst"]
    count: Annotated[int, "Length of the current conversation"]


class AgentState(MessagesState):
    # Kept under the upstream name so the shared pieces read the same: the Binance pair (BTCUSDT).
    company_of_interest: Annotated[str, "The Binance USDT pair under analysis, e.g. BTCUSDT"]
    asset_type: Annotated[str, "Always 'crypto' (Binance spot, USDT pairs)"]
    instrument_context: Annotated[str, "Deterministic coin identity resolved at run start"]
    trade_date: Annotated[str, "The analysis date (UTC day); data is served as of its close"]

    quant_decision: Annotated[dict, "The engine view (EngineView.to_state()) every agent explains and none may change"]
    quant_report: Annotated[str, "The engine report with every level and its % distance"]

    market_report: Annotated[str, "Report from the Market (technical) Analyst"]
    sentiment_report: Annotated[str, "Report from the Sentiment Analyst"]
    news_report: Annotated[str, "Report from the News Analyst"]
    tokenomics_report: Annotated[str, "Report from the Tokenomics Analyst"]

    investment_debate_state: Annotated[InvestDebateState, "The bull/bear debate"]
    investment_plan: Annotated[str, "Assessment from the Research Manager"]
    trader_investment_plan: Annotated[str, "Execution plan from the Trader"]

    risk_debate_state: Annotated[RiskDebateState, "The risk debate"]
    final_trade_decision: Annotated[str, "Final decision from the Portfolio Manager, checked by the Output Guard"]
    final_rating: Annotated[str, "The system action (BUY/HOLD/SELL/NO_TRADE)"]
    guard_notes: Annotated[str, "What the Output Guard dropped or flagged; empty when nothing"]
    portfolio_context: Annotated[str, "Caller-supplied holdings, rendered; empty when not provided"]
    portfolio: Annotated[dict, "The caller's book as data ({'weights': {pair: w}, 'nav', 'cash'}); empty when none"]
    # Deterministic data the analysts were given (market tool outputs, sentiment indices,
    # tokenomics figures): where the Output Guard lets a price come from. Appended by
    # the analysts running side by side.
    source_data: Annotated[list[str], operator.add]
