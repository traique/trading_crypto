"""The analysts' real sources (network): run with ``pytest -m integration``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cryptotrader.dataflows import date_window, news, sentiment, tokenomics

pytestmark = pytest.mark.integration


def _yesterday() -> str:
    return f"{datetime.now(UTC).date() - timedelta(days=1):%Y-%m-%d}"


def test_news_is_dated_and_before_the_cutoff():
    day = _yesterday()
    start = f"{date_window.parse_day(day) - timedelta(days=7):%Y-%m-%d}"
    items, failed, _ = news.collect_coin_news("BTCUSDT", start, day)
    assert len(failed) < 4 and items
    cut = date_window.cutoff(day)
    assert all(i.published is not None and i.published < cut for i in items)


def test_sentiment_sources_answer_point_in_time():
    day = _yesterday()
    fng = sentiment.fear_greed(day)
    assert fng and fng["date"] <= day
    fund = sentiment.funding("BTCUSDT", day)
    assert fund and fund["latest_time"][:10] <= day


def test_tokenomics_degrades_or_answers():
    text = tokenomics.tokenomics_report("ETHUSDT", _yesterday())
    assert text.startswith("## Tokenomics ETH")
