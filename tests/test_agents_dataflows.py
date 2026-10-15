"""What the analysts read: indicators without look-ahead, point-in-time news and sentiment, tokenomics."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from cryptotrader.agents import tools
from cryptotrader.dataflows import (
    date_window,
    fmt,
    indicators,
    news,
    sentiment,
    snapshot,
    tokenomics,
)
from tests import helpers

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)


# ── indicators ──
def test_indicators_never_look_ahead():
    frame = helpers.frame(date(2026, 1, 1), 260, 100.0, 0.7)
    for name in indicators.SUPPORTED:
        full = indicators.compute(frame, name)
        cut = indicators.compute(frame.iloc[:200], name)
        assert np.allclose(full.iloc[:200].to_numpy(), cut.to_numpy(), equal_nan=True), name


def test_rsi_and_sma_values():
    close = pd.Series([float(x) for x in range(1, 40)])
    assert indicators.rsi(close).iloc[-1] == pytest.approx(100.0)       # only gains
    assert indicators.sma(close, 5).iloc[-1] == pytest.approx(37.0)
    falling = pd.Series([float(x) for x in range(40, 1, -1)])
    assert indicators.rsi(falling).iloc[-1] == pytest.approx(0.0)


def test_unknown_indicator_is_a_clear_error():
    with pytest.raises(ValueError, match="not supported"):
        indicators.compute(helpers.frame(date(2026, 1, 1), 30), "stochrsi")


def test_price_formatting_spans_the_whole_range():
    assert fmt.price(65432.1) == "65,432.10"
    assert fmt.price(1.23456) == "1.2346"
    assert fmt.price(0.00001234) == "0.00001234"
    assert fmt.price(None) == "N/A"
    assert fmt.usd_short(1.5e12) == "1.50T"


def test_the_snapshot_is_cut_at_the_run_date(monkeypatch):
    frame = helpers.frame(date(2026, 9, 1), 44, 100.0, 1.0)          # through 2026-10-14
    monkeypatch.setattr(indicators, "load_frame", lambda s, d: frame)
    text = snapshot.build_verified_market_snapshot("BTC", "2026-10-10")
    assert "Latest closed daily candle used (UTC): 2026-10-10" in text
    assert "2026-10-11" not in text


# ── tools clamp the dates the model asks for ──
def test_tools_never_serve_past_the_run_date(monkeypatch):
    seen = {}
    monkeypatch.setattr(news, "get_news", lambda s, a, b: seen.update(news=(a, b)) or "ok")
    monkeypatch.setattr(snapshot, "price_table", lambda s, a, b: seen.update(prices=(a, b)) or "ok")
    monkeypatch.setattr(indicators, "get_indicator_window", lambda s, n, d, lb: seen.update(ind=d) or "ok")
    state = {"symbol": "BTCUSDT", "trade_date": "2026-10-01"}
    tools.get_news.func(start_date="2026-10-05", end_date="2026-10-12", **state)
    tools.get_price_data.func(start_date="2026-09-01", end_date="2026-12-31", **state)
    tools.get_indicators.func(indicator="rsi", curr_date="2026-10-15", **state)
    assert seen == {"news": ("2026-09-24", "2026-10-01"), "prices": ("2026-09-01", "2026-10-01"),
                    "ind": "2026-10-01"}
    assert "not a date" in tools.get_news.func(start_date="soon", end_date="x", **state)


def test_a_tool_failure_is_text_not_an_exception(monkeypatch):
    from cryptotrader.data.errors import VendorUnavailableError

    def down(*a, **k):
        raise VendorUnavailableError("timeout")

    monkeypatch.setattr(news, "get_global_news", down)
    out = tools.get_global_news.func(curr_date="2026-10-01", trade_date="2026-10-01")
    assert "unavailable" in out and "gap" in out


# ── news ──
def _rss(*items):
    body = "".join(
        f"<item><title><![CDATA[{t}]]></title><link>https://x/{i}</link><pubDate>{d}</pubDate>"
        f"<source url='https://p'>{s}</source><description>&lt;p&gt;{t} body&lt;/p&gt;</description></item>"
        for i, (t, d, s) in enumerate(items))
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>"


GOOGLE = _rss(
    ("Bitcoin ETF inflows hit record - Reuters", "Tue, 13 Oct 2026 10:00:00 GMT", "Reuters"),
    ("BTC slips as traders book profit - CoinDesk", "Wed, 14 Oct 2026 23:59:00 GMT", "CoinDesk"),
    ("Bitcoin rallies after the close - Bloomberg", "Thu, 15 Oct 2026 00:00:00 GMT", "Bloomberg"),
    ("Old news on bitcoin", "Mon, 01 Jun 2026 08:00:00 GMT", "Old"),
)
FEED = _rss(
    ("Ethereum upgrade date set", "Wed, 14 Oct 2026 12:00:00 +0000", "CoinDesk"),
    ("Exchange hack drains funds", "Wed, 14 Oct 2026 08:00:00 +0000", "CoinDesk"),
    ("Tomorrow's story", "Fri, 16 Oct 2026 08:00:00 +0000", "CoinDesk"),
)


@pytest.fixture
def feeds(monkeypatch):
    calls = []

    def fetch(url, params, source):
        calls.append((url, params))
        return GOOGLE if "google" in url else FEED

    monkeypatch.setattr(news, "FETCH", fetch)
    return calls


def test_news_after_the_run_close_is_never_served(feeds):
    items, failed, gaps = news.collect_coin_news("BTCUSDT", "2026-10-07", "2026-10-14", now=NOW)
    titles = [i.title for i in items]
    assert "BTC slips as traders book profit" in titles               # 23:59 UTC on the run date
    assert "Bitcoin rallies after the close" not in titles            # 00:00 UTC the next day
    assert "Tomorrow's story" not in titles
    assert "Old news on bitcoin" not in titles                        # before the window
    assert failed == []
    # The publisher feeds start after the window start: reported as a gap, not as "no news".
    assert gaps and all("KHÔNG có nghĩa" in g for g in gaps)


def test_coin_items_are_tagged_and_come_first(feeds):
    items, _, _ = news.collect_coin_news("BTC", "2026-10-07", "2026-10-14", now=NOW)
    assert items[0].about_coin and items[0].title.startswith(("BTC", "Bitcoin"))
    assert not next(i for i in items if i.title.startswith("Exchange hack")).about_coin


def test_google_is_searched_inside_the_window(feeds):
    news.collect_coin_news("SOL", "2026-10-07", "2026-10-14", now=NOW)
    google = [p for u, p in feeds if u == news.GOOGLE_NEWS_RSS]
    assert {p["hl"] for p in google} == {"en-US", "vi"}
    assert all("after:2026-10-07" in p["q"] and "Solana" in p["q"] for p in google)


def test_get_news_renders_dated_tagged_items(feeds):
    text = news.get_news("BTCUSDT", "2026-10-07", "2026-10-14", now=NOW)
    assert "### ✅ Bitcoin ETF inflows hit record" in text
    assert "(Reuters, 2026-10-13 10:00 UTC)" in text
    assert "### ⚠️ Exchange hack drains funds" in text


def test_every_source_down_is_unavailable_not_empty(monkeypatch):
    from cryptotrader.data.errors import VendorUnavailableError

    def down(*a):
        raise VendorUnavailableError("down")

    monkeypatch.setattr(news, "FETCH", down)
    with pytest.raises(VendorUnavailableError):
        news.get_news("BTCUSDT", "2026-10-07", "2026-10-14", now=NOW)


def test_the_cutoff_is_the_close_of_the_utc_day_or_now():
    assert date_window.cutoff("2026-10-13", NOW) == datetime(2026, 10, 14, tzinfo=UTC)
    assert date_window.cutoff("2026-10-15", NOW) == NOW
    with pytest.raises(ValueError, match="tương lai"):
        date_window.validate_trade_date("2026-10-16", NOW)
    assert date_window.is_live("2026-10-14", NOW) and not date_window.is_live("2026-10-12", NOW)


def test_keyword_score_reads_negation():
    assert news.sentiment_score("Bitcoin price surges to record high") > 0
    assert news.sentiment_score("Exchange hack triggers sell-off") < 0
    assert news.sentiment_score("SEC does not ban staking") > 0


# ── sentiment ──
def _fng_payload():
    days = pd.date_range("2026-09-01", "2026-10-15", freq="D", tz="UTC")
    return {"data": [{"value": str(20 + i), "value_classification": "Fear",
                      "timestamp": str(int(d.timestamp()))} for i, d in enumerate(days)][::-1]}


def test_fear_and_greed_reads_only_values_before_the_cutoff(monkeypatch):
    monkeypatch.setattr(sentiment, "FETCH_JSON", lambda url, params, source: _fng_payload())
    fng = sentiment.fear_greed("2026-10-10", NOW)
    assert fng["date"] == "2026-10-10" and fng["latest"] == 20 + 39


def test_funding_and_open_interest_are_cut_at_the_run_close(monkeypatch):
    end = datetime(2026, 10, 11, tzinfo=UTC)
    ms = lambda d: str(int(d.timestamp() * 1000))  # noqa: E731
    stamps = [datetime(2026, 10, 10, h, tzinfo=UTC) for h in (0, 8, 16)] + [end]
    funding_rows = [{"fundingTime": ms(t), "fundingRate": "0.0001", "realizedRate": ""} for t in stamps][::-1]
    oi_rows = [[ms(datetime(2026, 10, d, tzinfo=UTC)), str(1e9 + d), "0"] for d in range(1, 14)][::-1]

    def fetch(url, params, source):
        if "funding" in url:
            assert params["after"] == int(end.timestamp() * 1000)
            return {"code": "0", "data": funding_rows}
        return {"code": "0", "data": oi_rows}

    monkeypatch.setattr(sentiment, "FETCH_JSON", fetch)
    fund = sentiment.funding("BTCUSDT", "2026-10-10", NOW)
    assert fund["latest_time"] == "2026-10-10 16:00"            # the 2026-10-11 00:00 settlement is excluded
    oi = sentiment.open_interest("BTCUSDT", "2026-10-10", NOW)
    assert oi["date"] == "2026-10-10"


def test_the_sentiment_block_degrades_source_by_source(monkeypatch):
    from cryptotrader.data.errors import VendorUnavailableError

    def fetch(url, params, source):
        if "alternative" in url:
            return _fng_payload()
        raise VendorUnavailableError("blocked")

    monkeypatch.setattr(sentiment, "FETCH_JSON", fetch)
    text = sentiment.sentiment_block("ARBUSDT", "2026-10-10", NOW)
    assert "Fear & Greed ngày 2026-10-10" in text
    assert "Thiếu dữ liệu" in text and "Funding OKX" in text
    assert "không dùng cho lượt phân tích ngày quá khứ" in text      # dominance withheld


# ── tokenomics ──
COIN = {"id": "solana", "symbol": "sol", "name": "Solana", "categories": ["Layer 1 (L1)"],
        "market_data": {"current_price": {"usd": 150.0}, "market_cap": {"usd": 7.0e10},
                        "fully_diluted_valuation": {"usd": 9.0e10}, "total_volume": {"usd": 3.5e9},
                        "circulating_supply": 4.7e8, "total_supply": 6.0e8, "max_supply": None,
                        "ath": {"usd": 293.0}, "ath_date": {"usd": "2025-01-19T00:00:00Z"},
                        "market_cap_rank": 6}}


@pytest.fixture
def gecko(monkeypatch):
    calls = []

    def fetch(url, params, source):
        calls.append(url)
        if url.endswith("/search"):
            return {"coins": [{"id": "solana", "symbol": "SOL", "market_cap_rank": 6},
                              {"id": "sol-wormhole", "symbol": "SOL", "market_cap_rank": None}]}
        if url.endswith("/history"):
            return {"market_data": {"current_price": {"usd": 120.0}, "market_cap": {"usd": 5e10},
                                    "total_volume": {"usd": 2e9}}}
        return COIN

    monkeypatch.setattr(tokenomics, "FETCH_JSON", fetch)
    return calls


def test_live_tokenomics_and_the_cache(gecko):
    text = tokenomics.tokenomics_report("SOLUSDT", "2026-10-14", NOW)
    assert "Solana" in text and "FDV / market cap: 1.29x" in text and "ngày 2025-01-19" in text
    assert "không có nguồn miễn phí công bố lịch mở khoá" in text
    tokenomics.tokenomics_report("SOLUSDT", "2026-10-14", NOW)
    assert len(gecko) == 2                                            # id and coin data cached
    assert tokenomics.cached_name("SOL") == "Solana"


def test_a_past_run_gets_the_dated_snapshot_and_no_supply(gecko):
    text = tokenomics.tokenomics_report("SOLUSDT", "2026-03-01", NOW)
    assert "120.00 USD" in text and "GIỮ LẠI" in text
    assert "Cung lưu hành:" not in text
    assert any(u.endswith("/solana/history") for u in gecko)


def test_a_throttled_coingecko_is_a_gap(monkeypatch):
    from cryptotrader.data.errors import VendorUnavailableError

    def throttled(*a):
        raise VendorUnavailableError("HTTP 429")

    monkeypatch.setattr(tokenomics, "FETCH_JSON", throttled)
    assert "429" in tokenomics.tokenomics_report("SOLUSDT", "2026-10-14", NOW)


def test_a_run_older_than_the_free_history_window_asks_nothing(gecko):
    text = tokenomics.tokenomics_report("BTCUSDT", "2024-06-01", NOW)
    assert gecko == []                                                # neither the id nor the history
    assert "365 ngày" in text and "429" not in text


def test_a_coingecko_refusal_is_no_data_and_does_not_open_the_breaker(monkeypatch):
    import json as _json

    from cryptotrader.data import net
    from cryptotrader.data.errors import NoMarketDataError

    body = _json.dumps({"error": {"status": {"error_code": 10012,
                                             "error_message": "Public API users are limited to 365 days"}}})
    seen = []

    def http_get(url, params=None, **kwargs):
        seen.append(kwargs.get("allow_status"))
        return net.HttpResult(401, body.encode(), url)

    monkeypatch.setattr(net, "http_get", http_get)
    for _ in range(3):
        with pytest.raises(NoMarketDataError, match="401.*365 days"):
            tokenomics._fetch_json(f"{tokenomics.API}/coins/bitcoin/history", {"date": "01-06-2024"}, "coingecko")
    assert 401 in seen[0] and net.breaker("coingecko").allow()        # live runs keep their tokenomics


def test_a_past_run_notes_the_ticker_mapping_limit(gecko):
    assert "mã bị dùng lại" in tokenomics.tokenomics_report("SOLUSDT", "2026-03-01", NOW)


@pytest.mark.parametrize("title, symbol", [
    ("Bitcoin Cash surges 20% after upgrade", "BTCUSDT"),
    ("Ethereum Classic hit by 51% attack", "ETHUSDT"),
    ("Wrapped Bitcoin custodian change", "BTCUSDT"),
    ("Bitcoin SV delisted from exchange", "BTCUSDT"),
    ("Lido Staked Ether withdrawals pause", "ETHUSDT"),
])
def test_other_assets_named_after_a_coin_are_not_its_news(title, symbol):
    from cryptotrader.dataflows import coins

    assert not coins.mentions(title, symbol)


def test_the_coin_itself_is_still_recognised():
    from cryptotrader.dataflows import coins

    assert coins.mentions("Bitcoin tops $100k as ETF inflows rise", "BTCUSDT")
    assert coins.mentions("Bitcoin Cash and Bitcoin diverge", "BTCUSDT")         # Bitcoin itself, too
    assert coins.mentions("Bitcoin Cash surges 20% after upgrade", "BCHUSDT")
    assert coins.mentions("Ethereum Classic hit by 51% attack", "ETCUSDT")
    assert coins.mentions("ETH staking yields fall", "ETHUSDT")
