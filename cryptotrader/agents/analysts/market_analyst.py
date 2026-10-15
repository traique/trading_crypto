"""Market (technical) analyst: trend, momentum and volatility of the pair from closed daily candles."""

from cryptotrader.agents import crypto_context
from cryptotrader.agents.analysts.common import tool_prompt
from cryptotrader.agents.analysts.turn import take_turn
from cryptotrader.agents.context import get_language_instruction
from cryptotrader.agents.tools import get_indicators, get_price_data, get_verified_market_snapshot

TOOLS = (get_price_data, get_indicators, get_verified_market_snapshot)

SYSTEM_MESSAGE = """You are a technical analyst of crypto spot markets (Binance, USDT pairs). Select the **most relevant indicators** for the current market condition from the list below, up to **8** complementary ones without redundancy:

Moving averages: close_10_ema (fast, noisy in chop), close_20_ema / close_20_sma (short-medium trend), close_50_sma (medium trend, dynamic support/resistance), close_200_sma (long-term regime: price above/below it is the classic bull/bear split in crypto).
MACD: macd, macds (signal), macdh (histogram) - momentum shifts, crossovers and divergence.
Momentum: rsi (70/30; in strong crypto trends RSI can stay extreme for weeks), mfi (volume-weighted RSI, 80/20).
Volatility: boll, boll_ub, boll_lb (20-day bands; squeezes often precede large crypto moves), atr (average true range in USDT; use it to describe how far a normal day moves).
Volume: vwma (volume-weighted average).

Call get_price_data first, then get_indicators with exact indicator names (one per call). Before writing the final report, call get_verified_market_snapshot and treat it as the source of truth for every exact price, change or indicator value; if another tool disagrees, flag the discrepancy instead of reconciling numbers yourself. Do not claim support/resistance bounces or exact percentage moves that the tool output does not show with dates and prices.

Crypto specifics: the market trades 24/7 and the daily candle closes at 00:00 UTC (07:00 Vietnam time); every indicator here is computed on CLOSED daily candles - always state the date of the last closed candle you describe. Volatility is several times that of equities: describe moves relative to ATR rather than in absolute terms, and weight volume confirmation (quote volume in USDT) because thin altcoin books move on small orders.

Write a detailed, nuanced report of the trends you observe, with specific evidence; another agent decides the trade. Append a Markdown table organising the key points."""


def create_market_analyst(llm):
    def market_analyst_node(state):
        system_message = (SYSTEM_MESSAGE + "\n\n" + crypto_context.market_rules() + "\n\n"
                          + crypto_context.GROUNDING_RULES + "\n\n" + crypto_context.NO_LEVERAGE_RULE
                          + get_language_instruction())
        prompt = tool_prompt(system_message, TOOLS, state)
        result, report = take_turn(prompt, llm, TOOLS, state["messages"])
        return {"messages": [result], "market_report": report}

    return market_analyst_node
