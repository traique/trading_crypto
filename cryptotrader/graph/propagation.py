"""The initial state of a run and the arguments of its invocation."""

from typing import Any

from cryptotrader.agents.state import InvestDebateState, RiskDebateState


class Propagator:
    def __init__(self, max_recur_limit: int = 100):
        self.max_recur_limit = max_recur_limit

    def create_initial_state(self, pair: str, trade_date: str, instrument_context: str = "",
                             portfolio_context: str = "", portfolio: dict | None = None) -> dict[str, Any]:
        return {
            "messages": [("human", pair)],
            "company_of_interest": pair,
            "asset_type": "crypto",
            "instrument_context": instrument_context,
            "trade_date": str(trade_date),
            "portfolio_context": portfolio_context,
            "portfolio": portfolio or {},
            "quant_decision": {},
            "quant_report": "",
            "guard_notes": "",
            "investment_debate_state": InvestDebateState(
                bull_history="", bear_history="", history="", current_response="", count=0),
            "risk_debate_state": RiskDebateState(
                aggressive_history="", conservative_history="", neutral_history="", history="",
                latest_speaker="", current_aggressive_response="", current_conservative_response="",
                current_neutral_response="", count=0),
            "market_report": "",
            "tokenomics_report": "",
            "sentiment_report": "",
            "news_report": "",
            "investment_plan": "",
            "trader_investment_plan": "",
            "final_trade_decision": "",
            "final_rating": "",
        }

    def get_graph_args(self, callbacks: list | None = None) -> dict[str, Any]:
        config: dict[str, Any] = {"recursion_limit": self.max_recur_limit}
        if callbacks:
            config["callbacks"] = callbacks
        return {"stream_mode": "values", "config": config}
