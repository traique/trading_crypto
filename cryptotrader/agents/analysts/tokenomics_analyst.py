"""Tokenomics analyst (replaces the equity fundamentals analyst): supply, valuation and dilution risk.

The data (CoinGecko market cap, FDV, circulating/total/max supply, ATH) is
fetched before the model is called and is point-in-time: a past run gets only
the dated snapshot, never today's supply figures.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from cryptotrader.agents import crypto_context
from cryptotrader.agents.context import get_instrument_context_from_state, get_language_instruction
from cryptotrader.agents.structured import NO_EXTERNAL_TOOLS
from cryptotrader.data.symbols import normalize_symbol
from cryptotrader.dataflows import tokenomics


def _tokenomics_block(pair: str, trade_date: str) -> str:
    try:
        return tokenomics.tokenomics_report(pair, trade_date)
    except Exception as exc:  # noqa: BLE001 - the report goes on without tokenomics
        return f"<tokenomics unavailable: {exc}>"


SYSTEM_MESSAGE = """You are a crypto tokenomics analyst. From the CoinGecko data below, write a report on the token's supply and valuation for the traders:

- Market cap vs fully diluted valuation (FDV): a high FDV/market-cap ratio means most tokens are not circulating yet, so future unlocks and emissions dilute holders.
- Circulating share of max (or total) supply; whether supply is capped, inflationary or unknown.
- Liquidity: 24h volume relative to market cap; thin liquidity means large slippage and easier manipulation.
- Distance from the all-time high, as context only (not a target).
- Unlock schedules: the free data has none. Do NOT invent unlock dates or amounts; say what the supply figures imply and that the schedule must be checked in the project's own documents.
- For BTC and other coins with no team or VC allocation, say so instead of forcing a dilution story.

Equity metrics (P/E, revenue multiples) do not apply. Provide specific insights with the figures given; another agent decides the trade. Append a Markdown table of the key figures."""


def create_tokenomics_analyst(llm):
    def tokenomics_analyst_node(state):
        pair = normalize_symbol(state["company_of_interest"])
        trade_date = state["trade_date"]
        block = _tokenomics_block(pair, trade_date)
        prompt = (
            f"Today's date is {trade_date} (UTC). {get_instrument_context_from_state(state)} {NO_EXTERNAL_TOOLS}\n\n"
            f"{SYSTEM_MESSAGE}\n\n<start_of_tokenomics>\n{block}\n<end_of_tokenomics>\n\n"
            f"{crypto_context.GROUNDING_RULES}\n\n{crypto_context.NO_LEVERAGE_RULE}{get_language_instruction()}"
        )
        text = llm.invoke(prompt).content
        return {"messages": [AIMessage(content=text)], "tokenomics_report": text, "source_data": [block]}

    return tokenomics_analyst_node
