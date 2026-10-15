"""Sentiment analyst: one sentiment report from data fetched before the model is called.

Sources, each windowed to the run's cutoff (the close of its UTC day):

  1. News about the coin over the past 7 days, ✅/⚠️ tagged.
  2. A deterministic keyword score of those headlines, a baseline to check the model's read against.
  3. Fear & Greed, perpetual funding and open interest (OKX), BTC dominance for a live run.

The model reports on data it was given rather than inventing posts. Output is a
``SentimentReport`` through structured output where the provider supports it,
free text otherwise.
"""

from __future__ import annotations

from datetime import timedelta

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from cryptotrader.agents import crypto_context
from cryptotrader.agents.context import get_instrument_context_from_state, get_language_instruction
from cryptotrader.agents.schemas import SentimentReport, render_sentiment_report
from cryptotrader.agents.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)
from cryptotrader.data.symbols import base_asset, normalize_symbol
from cryptotrader.dataflows import date_window, news, sentiment


def _days_back(trade_date: str, days: int) -> str:
    return f"{date_window.parse_day(trade_date) - timedelta(days=days):%Y-%m-%d}"


def _news_block(pair: str, start_date: str, end_date: str) -> tuple[str, str]:
    """(rendered headlines, keyword baseline) for the window; each degrades to a note."""
    try:
        items, failed, gaps = news.collect_coin_news(pair, start_date, end_date, limit=25)
    except Exception as exc:  # noqa: BLE001 - the sentiment report goes on without news
        return f"<news unavailable: {exc}>", "<keyword score unavailable>"
    head = []
    if failed:
        head.append(f"(Không truy cập được nguồn: {', '.join(failed)})")
    head += gaps
    rendered = "\n".join(head + (news.render(items) if items else ["Không có tin nào trong khoảng này."]))
    about = [i for i in items if i.about_coin]
    scores = [news.sentiment_score(i.title) for i in about]
    positive = sum(1 for s in scores if s > 0.2)
    negative = sum(1 for s in scores if s < -0.2)
    mean = sum(scores) / len(scores) if scores else 0.0
    keyword = (f"Tin đúng coin (✅): {len(about)} | tích cực: {positive} | tiêu cực: {negative} | "
               f"trung tính: {len(about) - positive - negative} | tin chung (⚠️): {len(items) - len(about)}\n"
               f"Điểm từ khoá trung bình của tin ✅ (-1..+1): {mean:+.2f}")
    return rendered, keyword


def _sentiment_block(pair: str, trade_date: str) -> str:
    try:
        return sentiment.sentiment_block(pair, trade_date)
    except Exception as exc:  # noqa: BLE001
        return f"<sentiment indices unavailable: {exc}>"


def create_sentiment_analyst(llm):
    structured_llm = bind_structured(llm, SentimentReport, "Sentiment Analyst")

    def sentiment_analyst_node(state):
        pair = normalize_symbol(state["company_of_interest"])
        end_date = state["trade_date"]
        start_date = _days_back(end_date, 7)
        news_text, keyword_text = _news_block(pair, start_date, end_date)
        indices = _sentiment_block(pair, end_date)
        system_message = build_system_message(
            pair=pair, start_date=start_date, end_date=end_date, news_block=news_text,
            keyword_block=keyword_text, indices_block=indices)
        prompt = ChatPromptTemplate.from_messages([
            ("system",
             "You are a helpful AI assistant, collaborating with other assistants."
             " Report what your data supports; another agent decides the trade."
             " Today's date is {current_date} (UTC); treat it as 'now' for all analysis. {instrument_context} "
             + NO_EXTERNAL_TOOLS + "\n{system_message}"),
            MessagesPlaceholder("messages"),
        ]).partial(system_message=system_message, current_date=end_date,
                   instrument_context=get_instrument_context_from_state(state))
        messages = prompt.format_messages(messages=state["messages"])
        text = invoke_structured_or_freetext(structured_llm, llm, messages, render_sentiment_report,
                                             "Sentiment Analyst")
        return {"messages": [AIMessage(content=text)], "sentiment_report": text, "source_data": [indices]}

    return sentiment_analyst_node


def build_system_message(*, pair: str, start_date: str, end_date: str, news_block: str,
                         keyword_block: str, indices_block: str) -> str:
    coin = base_asset(pair)
    return f"""You are a market sentiment analyst for crypto. Produce a sentiment report for {coin} ({pair}) covering {start_date} to the close of {end_date} (UTC), from the data below only.

## Data (pre-fetched, in this prompt)

### News about {coin}, past 7 days
Items marked ✅ name {coin}; ⚠️ items are market-wide news and must not be read as events of {coin}.

<start_of_news>
{news_block}
<end_of_news>

### Keyword baseline (deterministic, not a verdict)
<start_of_keyword_score>
{keyword_block}
<end_of_keyword_score>

### Market sentiment indices
Fear & Greed is market-wide (mostly driven by BTC). Funding and open interest come from perpetual futures on OKX and are used as sentiment indicators only: high positive funding with rising open interest means crowded leveraged longs (liquidation risk on a drop); negative funding means crowded shorts.

<start_of_indices>
{indices_block}
<end_of_indices>

## How to analyze

1. Separate events (listings, hacks, unlocks, ETF flows, regulatory actions) from opinion and price commentary.
2. Only ✅ items speak about {coin}; ⚠️ items are backdrop.
3. Crypto media amplifies moves: one story repeated by many outlets is one story; "price prediction" articles are noise.
4. Extreme fear or greed is a contrarian context, not a timing signal; say so rather than calling a turn.
5. Look for divergences between news tone, the keyword baseline, Fear & Greed and derivatives positioning.
6. Be honest about data limits: few ✅ items or a missing source means low confidence.
7. Past sentiment is not predictive; frame conclusions as context for the trader, not a price call.

## Output fields
- overall_band: Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish
- overall_score: 0 (maximally bearish) to 10 (maximally bullish), consistent with the band
- confidence: low / medium / high
- narrative: source-by-source breakdown, divergences, themes, catalysts and risks, and a Markdown summary table (signal, direction, source, date)

{crypto_context.GROUNDING_RULES}

{crypto_context.NO_LEVERAGE_RULE}
{get_language_instruction()}"""
