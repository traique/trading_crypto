"""The LLM agents of the analysis graph, around a deterministic system decision."""

from .analysts.market_analyst import create_market_analyst
from .analysts.news_analyst import create_news_analyst
from .analysts.sentiment_analyst import create_sentiment_analyst
from .analysts.tokenomics_analyst import create_tokenomics_analyst
from .managers.portfolio_manager import create_portfolio_manager
from .managers.research_manager import create_research_manager
from .researchers.debate import create_bear_researcher, create_bull_researcher
from .risk_mgmt.debators import (
    create_aggressive_debator,
    create_conservative_debator,
    create_neutral_debator,
)
from .state import AgentState, InvestDebateState, RiskDebateState
from .trader.trader import create_trader

__all__ = [
    "AgentState",
    "InvestDebateState",
    "RiskDebateState",
    "create_aggressive_debator",
    "create_bear_researcher",
    "create_bull_researcher",
    "create_conservative_debator",
    "create_market_analyst",
    "create_neutral_debator",
    "create_news_analyst",
    "create_portfolio_manager",
    "create_research_manager",
    "create_sentiment_analyst",
    "create_tokenomics_analyst",
    "create_trader",
]
