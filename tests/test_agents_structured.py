"""Ratings, schemas and the structured-output fallback of the deciding agents."""

from __future__ import annotations

import pytest
from langchain_core.runnables import RunnableLambda

from cryptotrader.agents import rating, schemas, structured
from cryptotrader.agents.managers.portfolio_manager import create_portfolio_manager
from cryptotrader.agents.quant_engine import EngineView, render_report

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("text, expected", [
    ("**Rating**: BUY\n\nText", "BUY"),
    ("## Quyết định\n**Rating**: NO_TRADE", "NO_TRADE"),
    ("Rating: Bán", "SELL"),
    ("Consensus rating: Buy\nOur rating: Sell", "SELL"),     # our own line, not someone else's
    ("Intro\n**Rating**: BUY\n**Rating**: SELL", None),       # disagreeing own lines are no call
    ("Không có nhãn nào", None),
])
def test_extract_rating(text, expected):
    assert rating.extract_rating(text) == expected


def test_run_rating_prefers_the_stored_system_action():
    assert rating.run_rating({"final_rating": "HOLD", "final_trade_decision": "**Rating**: BUY"}) == "HOLD"
    assert rating.run_rating({"final_trade_decision": "nothing"}) == rating.RATING_REVIEW


def test_plan_lines_give_no_entry_for_sell_or_no_trade():
    lines = schemas.plan_lines({"action": "SELL", "price": 1.2345, "last_candle_date": "2026-10-14",
                                "target_weight": 0.0, "current_weight": 0.25})
    assert "**Entry Zone**: not provided" in lines
    assert "**Reference Price**: 1.2345 USDT (đóng cửa nến 2026-10-14)" in lines
    assert any("đang giữ 25.0% NAV" in line for line in lines)


def test_plan_lines_of_a_buy_carry_the_engines_levels():
    lines = schemas.plan_lines({"action": "BUY", "price": 65432.1, "stop_price": 60000.0, "target_weight": 1.0})
    assert "**Stop Loss**: 60,000.00 USDT" in lines and "**Position Sizing**: 100.0% NAV (mục tiêu của chiến lược)" in lines


class _Model:
    """A chat model whose structured call fails, so the agent falls back to free text."""

    def __init__(self):
        self.prompts = []

    def with_structured_output(self, schema):
        return RunnableLambda(lambda _: (_ for _ in ()).throw(ValueError("bad json")))

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return type("R", (), {"content": "Nhận định của quản lý danh mục."})()


def _state():
    decision = EngineView(symbol="BTCUSDT", as_of_date="2026-10-14", action="HOLD", price=65432.1).to_state()
    return {"company_of_interest": "BTCUSDT", "trade_date": "2026-10-14", "quant_decision": decision,
            "quant_report": render_report(decision), "investment_plan": "p", "trader_investment_plan": "t",
            "risk_debate_state": {"history": "h", "count": 3}}


def test_a_failed_structured_call_falls_back_to_free_text_under_the_system_rating():
    model = _Model()
    out = create_portfolio_manager(model)(_state())
    assert out["final_trade_decision"].startswith("**Rating**: HOLD")
    assert "Nhận định của quản lý danh mục." in out["final_trade_decision"]
    assert "**Reference Price**: 65,432.10 USDT" in out["final_trade_decision"]
    assert out["final_rating"] == "HOLD"


def test_bind_structured_tolerates_providers_without_it():
    class Plain:
        def with_structured_output(self, schema):
            raise NotImplementedError

    assert structured.bind_structured(Plain(), schemas.ResearchPlan, "x") is None
