"""Which analysts run, and the node, report key and tools of each."""

from collections.abc import Iterable
from dataclasses import dataclass

from cryptotrader.agents.analysts import market_analyst, news_analyst


@dataclass(frozen=True)
class AnalystNodeSpec:
    key: str
    agent_node: str
    report_key: str
    tools: tuple = ()
    # The tools return deterministic market data: their outputs are recorded in
    # ``source_data`` (where the Output Guard lets a price come from). Not the news.
    trusted_tools: bool = False


ANALYST_NODE_SPECS: dict[str, AnalystNodeSpec] = {
    "market": AnalystNodeSpec("market", "Market Analyst", "market_report", market_analyst.TOOLS, trusted_tools=True),
    # The sentiment and tokenomics analysts fetch their data before calling the model: no tools.
    "sentiment": AnalystNodeSpec("sentiment", "Sentiment Analyst", "sentiment_report"),
    "news": AnalystNodeSpec("news", "News Analyst", "news_report", news_analyst.TOOLS),
    "tokenomics": AnalystNodeSpec("tokenomics", "Tokenomics Analyst", "tokenomics_report"),
}
DEFAULT_ANALYSTS = ("market", "sentiment", "news", "tokenomics")
# Names users of the upstream project know.
_ALIASES = {"social": "sentiment", "fundamentals": "tokenomics"}


def build_analyst_execution_plan(selected_analysts: Iterable[str]) -> list[AnalystNodeSpec]:
    specs: list[AnalystNodeSpec] = []
    for key in selected_analysts:
        spec = ANALYST_NODE_SPECS.get(_ALIASES.get(key, key))
        if spec is None:
            raise ValueError(f"unknown analyst key: {key}")
        if spec not in specs:
            specs.append(spec)
    if not specs:
        raise ValueError("at least one analyst must be selected")
    return specs
