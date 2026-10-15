"""The engine view: the strategy registry's decision as the system action, and its plug-in points."""

from __future__ import annotations

from datetime import date

import pytest

from cryptotrader.agents import quant_engine
from cryptotrader.agents.quant_engine import EngineView
from tests import helpers

pytestmark = pytest.mark.unit

DAY = "2026-10-14"


@pytest.fixture
def frames(monkeypatch):
    """BTC rising 300 days to DAY (above its SMA200), ETH falling."""
    first = date(2025, 12, 19)
    data = {"BTCUSDT": helpers.frame(first, 300, 50_000.0, 50.0),
            "ETHUSDT": helpers.frame(first, 300, 4_000.0, -5.0)}
    assert f"{data['BTCUSDT']['Date'].iloc[-1]:%Y-%m-%d}" == DAY
    monkeypatch.setattr(quant_engine, "_load", lambda symbols, end, warmup: {s: data[s] for s in symbols if s in data})
    yield data
    quant_engine.set_engine(None)


def test_a_wanted_coin_not_held_is_buy(frames):
    view = quant_engine.analyze("BTC", DAY, portfolio={"weights": {}}, config={"engine_strategy": "btc_trend"})
    assert (view.action, view.in_basket, view.target_weight, view.trend_state) == ("BUY", True, 1.0, "TĂNG")
    assert view.price == pytest.approx(float(frames["BTCUSDT"]["Close"].iloc[-1]))
    assert {"SMA50", "SMA200", "ATR14"} <= set(view.levels)


def test_a_wanted_coin_held_is_hold(frames):
    assert quant_engine.analyze("BTCUSDT", DAY, portfolio={"weights": {"BTCUSDT": 0.9}}).action == "HOLD"


def test_without_a_portfolio_a_wanted_coin_reads_buy_and_says_why(frames):
    view = quant_engine.analyze("BTCUSDT", DAY)
    assert view.action == "BUY" and view.current_weight is None
    assert any("không biết danh mục" in r for r in view.reasons)


def test_a_coin_outside_the_strategy_is_no_trade_or_sell(frames):
    assert quant_engine.analyze("ETH", DAY).action == "NO_TRADE"
    held = quant_engine.analyze("ETH", DAY, portfolio={"weights": {"ETHUSDT": 0.2}})
    assert held.action == "SELL"
    assert any("không nằm trong" in r for r in held.reasons)


def test_a_plugged_in_engine_replaces_the_default(frames):
    quant_engine.set_engine(lambda symbol, trade_date, portfolio=None, config=None: EngineView(
        symbol="SOLUSDT", as_of_date=trade_date, action="SELL", price=150.0, source="bot"))
    assert quant_engine.analyze("SOL", DAY).source == "bot"


def test_a_strategy_engine_view_is_used_when_it_has_one(frames, monkeypatch):
    from cryptotrader import strategies

    class Winner:
        name = "winner"
        symbols = ("BTCUSDT",)
        warmup_days = 0

        def decide(self, ctx):
            raise AssertionError("engine_view takes precedence")

        def engine_view(self, ctx, pair):
            return {"action": "HOLD", "stop_price": 60_000.0, "reasons": ["rổ momentum tuần"]}

    monkeypatch.setitem(strategies.REGISTRY, "winner", Winner)
    view = quant_engine.analyze("BTC", DAY, config={"engine_strategy": "winner"})
    assert (view.action, view.stop_price, view.strategy) == ("HOLD", 60_000.0, "winner")


def test_an_unknown_action_is_refused():
    with pytest.raises(ValueError):
        EngineView(symbol="BTCUSDT", as_of_date=DAY, action="WATCH")


def test_the_report_gives_every_level_with_its_distance(frames):
    state = quant_engine.analyze("BTC", DAY).to_state()
    report = quant_engine.render_report(state)
    assert "BTC/USDT" in report and "BUY (MUA)" in report
    assert "SMA200" in report and "so với giá" in report
    assert "không đòn bẩy" in report
    assert state["deterministic_summary"].startswith("Tín hiệu hệ thống cho BTC/USDT: BUY")


def test_the_graph_node_degrades_to_no_trade_on_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no data")

    quant_engine.set_engine(boom)
    try:
        out = quant_engine.create_quant_engine()({"company_of_interest": "BTCUSDT", "trade_date": DAY})
    finally:
        quant_engine.set_engine(None)
    assert out["quant_decision"]["action"] == "NO_TRADE"
    assert out["quant_report"] == ""
    assert "no data" in out["quant_decision"]["reasons"][0]
