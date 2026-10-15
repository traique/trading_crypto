"""News analyst: the coin's own news and the market and macro backdrop, point-in-time."""

from cryptotrader.agents import crypto_context
from cryptotrader.agents.analysts.common import tool_prompt
from cryptotrader.agents.analysts.turn import take_turn
from cryptotrader.agents.context import get_language_instruction
from cryptotrader.agents.tools import get_global_news, get_news

TOOLS = (get_news, get_global_news)

SYSTEM_MESSAGE = (
    "You are a news researcher covering crypto markets. Write a comprehensive report of the news of the "
    "past week that matters for this coin. Use get_news(start_date, end_date) for news about the coin "
    "(Google News in English and Vietnamese, CoinDesk, Cointelegraph) and get_global_news(curr_date, "
    "look_back_days, limit) for the market and macro backdrop (spot ETF flows, Fed and interest rates, "
    "the US dollar, regulation and enforcement, stablecoins, exchange hacks, outages and insolvencies). "
    "Separate news of the coin itself (marked ✅) from market-wide news (marked ⚠️), and date every item. "
    "Weigh what moves crypto: listings and delistings, token unlocks, protocol upgrades and forks, "
    "hacks and exploits, regulatory actions, large holder and ETF flows. One story repeated by several "
    "outlets is one story. Tools only return items published before the close of the analysis date: "
    "never describe later events. Provide specific insights with supporting evidence; another agent "
    "decides the trade. Append a Markdown table organising the key points."
)


def create_news_analyst(llm):
    def news_analyst_node(state):
        system_message = (SYSTEM_MESSAGE + "\n\n" + crypto_context.GROUNDING_RULES + "\n\n"
                          + crypto_context.NO_LEVERAGE_RULE + get_language_instruction())
        prompt = tool_prompt(system_message, TOOLS, state)
        result, report = take_turn(prompt, llm, TOOLS, state["messages"])
        return {"messages": [result], "news_report": report}

    return news_analyst_node
