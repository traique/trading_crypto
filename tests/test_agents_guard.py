"""The Output Guard and its text checks: actions, leverage advice, invented prices, the disclaimer."""

from __future__ import annotations

import pytest

from cryptotrader.agents import output_guard, report_format as rfmt
from cryptotrader.agents.quant_engine import EngineView, render_report

pytestmark = pytest.mark.unit


def _state(action="NO_TRADE", price=65432.10, **extra):
    view = EngineView(symbol="BTCUSDT", as_of_date="2026-10-14", action=action, last_candle_date="2026-10-14",
                      price=price, strategy="btc_trend", trend_state="TĂNG", in_basket=action in ("BUY", "HOLD"),
                      levels={"SMA50": 61000.0, "SMA200": 58250.5, "Đỉnh 30 ngày": 67100.0})
    decision = view.to_state()
    return {"quant_decision": decision, "quant_report": render_report(decision), **extra}


# ── action conflicts ──
@pytest.mark.parametrize("text", [
    "Kết luận: MUA ngay khi có thể.",
    "Nhà đầu tư nên mua thêm ở vùng hiện tại.",
    "Khuyến nghị: tích lũy dần.",
])
def test_buy_advice_contradicts_no_trade(text):
    assert rfmt.find_action_conflicts(text, {"NO_TRADE"})


@pytest.mark.parametrize("text", [
    "Chưa nên mua lúc này, đứng ngoài quan sát.",
    "Không MUA đuổi.",
    "Nếu giá đóng cửa trên SMA50 thì có thể mua lại.",
    "Cá voi mua vào mạnh trong tuần, quỹ ETF mua ròng.",
    "Áp lực BÁN từ thợ đào giảm dần.",
])
def test_negated_conditional_and_third_party_mentions_are_not_conflicts(text):
    assert rfmt.find_action_conflicts(text, {"NO_TRADE"}) == []


def test_sell_label_contradicts_buy():
    assert rfmt.find_action_conflicts("Hành động đề xuất: BÁN toàn bộ.", {"BUY"})


# ── leverage ──
@pytest.mark.parametrize("text", [
    "Có thể mở long với đòn bẩy x5.",
    "Nên dùng đòn bẩy để tối ưu lợi nhuận.",
    "Hãy short khi giá chạm kháng cự.",
    "leverage 10x là hợp lý",
])
def test_leverage_and_short_advice_is_found(text):
    assert rfmt.find_leverage_advice(text)


@pytest.mark.parametrize("text", [
    "Không dùng đòn bẩy, chỉ giao dịch spot.",
    "Funding dương cho thấy phe long đòn bẩy đông.",
    "Tránh mở long trên futures.",
])
def test_warnings_about_leverage_are_not_advice(text):
    assert rfmt.find_leverage_advice(text) == []


# ── prices ──
ALLOWED = "Giá đóng cửa 65,432.10 USDT. SMA50: 61,000.00 USDT (-6.77%). SMA200 58,250.50."


@pytest.mark.parametrize("text", [
    "Giá đang ở 65,432.10 USDT.",
    "Giá quanh 65,432 USDT.",            # rounded
    "Giá khoảng 65,000 USDT.",           # rounded to thousands
    "Hỗ trợ ở SMA50 61.000 USDT.",       # Vietnamese thousands separator
    "SMA200 58,250.5",
    "RSI 72, giảm 4.76%, khối lượng 1,234 BTC, ngày 2026-10-14 lúc 07:00.",
])
def test_known_or_non_price_numbers_pass(text):
    assert rfmt.find_unknown_prices(text, ALLOWED, 65432.10) == []


@pytest.mark.parametrize("text, token", [
    ("Mục tiêu 72,500 USDT.", "72,500"),
    ("Stop đặt ở 59.800 USDT.", "59.800"),
    ("Kháng cự 69800.", "69800"),
])
def test_invented_prices_are_found(text, token):
    assert rfmt.find_unknown_prices(text, ALLOWED, 65432.10) == [token]


def test_small_prices_are_checked_with_their_own_precision():
    allowed = "Giá đóng cửa 0.00001234 USDT; SMA20 0.00001190."
    assert rfmt.find_unknown_prices("Giá 0.00001234, hỗ trợ 0.0000119", allowed, 0.00001234) == []
    assert rfmt.find_unknown_prices("Mục tiêu 0.00001500", allowed, 0.00001234) == ["0.00001500"]


def test_no_reference_price_means_no_price_check():
    assert rfmt.find_unknown_prices("Mục tiêu 72,500", ALLOWED, None) == []


# ── disclaimer ──
def test_exactly_one_standard_disclaimer_ends_the_text():
    text = "Phân tích.\n\nĐây chỉ mang tính tham khảo, không phải lời khuyên đầu tư.\n\nKết luận."
    once = rfmt.ensure_disclaimer(rfmt.clean_analysis_output(text))
    assert once.endswith(rfmt.STANDARD_DISCLAIMER)
    assert once.count("tham khảo") == 1
    assert rfmt.ensure_disclaimer(once) == once


# ── the guard node ──
def test_a_contradicting_final_decision_is_replaced_by_the_system_summary():
    state = _state("NO_TRADE", final_trade_decision="**Rating**: NO_TRADE\n\nTôi khuyên MUA ngay.",
                   investment_plan="", trader_investment_plan="")
    out = output_guard.create_output_guard()(state)
    assert out["final_rating"] == "NO_TRADE"
    assert "MUA ngay" not in out["final_trade_decision"]
    assert output_guard.CONFLICT_NOTE in out["final_trade_decision"]
    assert out["final_trade_decision"].startswith("**Rating**: NO_TRADE")
    assert "Portfolio Manager" in out["guard_notes"]
    assert out["final_trade_decision"].endswith(rfmt.STANDARD_DISCLAIMER)


def test_leverage_advice_in_the_trader_plan_is_dropped():
    state = _state("BUY", trader_investment_plan="**Action**: BUY\n\nMở long x3 để tối ưu.",
                   investment_plan="", final_trade_decision="**Rating**: BUY\n\nỔn.")
    out = output_guard.create_output_guard()(state)
    assert "x3" not in out["trader_investment_plan"]
    assert "Trader" in out["guard_notes"]


def test_an_invented_price_keeps_the_text_behind_a_warning():
    state = _state("BUY", final_trade_decision="**Rating**: BUY\n\nMục tiêu 80,000 USDT.",
                   investment_plan="", trader_investment_plan="")
    out = output_guard.create_output_guard()(state)
    assert out["final_trade_decision"].startswith(output_guard.UNKNOWN_PRICE_NOTE)
    assert "80,000" in out["final_trade_decision"]


def test_a_clean_decision_passes_and_the_rating_is_always_the_system_action():
    state = _state("HOLD", final_trade_decision="**Rating**: HOLD\n\nTiếp tục nắm giữ, SMA50 61,000 USDT.",
                   investment_plan="", trader_investment_plan="")
    out = output_guard.create_output_guard()(state)
    assert out["guard_notes"] == ""
    assert out["final_rating"] == "HOLD"


def test_the_system_lines_are_not_read_as_the_models_words():
    # The renderer writes "**Rating**: SELL" and plan lines itself; a SELL decision is not a conflict
    # with itself, and the reference price line is the engine's.
    state = _state("SELL", final_trade_decision="**Rating**: SELL\n\n**Reference Price**: 65,432.10 USDT\n\nThoát vị thế.",
                   investment_plan="", trader_investment_plan="")
    assert output_guard.create_output_guard()(state)["guard_notes"] == ""


def test_guard_early_records_its_note_for_later_agents():
    node = output_guard.guard_early(lambda s: {"investment_plan": "**Recommendation**: NO_TRADE\n\nNên mua."},
                                    "investment_plan", "Research Manager")
    out = node(_state("NO_TRADE"))
    assert "Nên mua" not in out["investment_plan"]
    assert "Research Manager" in out["guard_notes"]


# ── regressions: normal wording is not a conflict ──
@pytest.mark.parametrize("text, action", [
    ("Phe Bear cho rằng nên bán vì funding quá cao.", "BUY"),
    ("Phe Bull cho rằng nên mua vì dòng tiền ETF.", "NO_TRADE"),
    ("Hệ thống sẽ chuyển sang BÁN nếu đa số SMA bị thủng.", "BUY"),
    ("Chỉ mua vào khi đa số SMA được lấy lại.", "NO_TRADE"),
    ("Không có lý do để bán ra lúc này.", "HOLD"),
    ("Hệ thống chỉ BUY, sau đó HOLD cho tới khi tín hiệu đổi.", "BUY"),
])
def test_debate_summaries_conditions_and_negations_are_not_conflicts(text, action):
    from cryptotrader.agents.quant_engine import COMPATIBLE_ACTIONS

    assert rfmt.find_action_conflicts(text, COMPATIBLE_ACTIONS[action]) == []


def test_real_contradictions_are_still_found():
    assert rfmt.find_action_conflicts("Kết luận: MUA ngay khi có thể.", {"NO_TRADE"})
    assert rfmt.find_action_conflicts("Tôi cho rằng nên bán ngay.", {"BUY", "HOLD"})
    assert rfmt.find_action_conflicts("Giá không giảm nữa nên mua vào.", {"NO_TRADE"})


def test_a_no_leverage_sentence_is_not_leverage_advice():
    assert rfmt.find_leverage_advice("Không nên dùng đòn bẩy x10.") == []
    assert rfmt.find_leverage_advice("Có thể dùng đòn bẩy x10.")


@pytest.mark.parametrize("text, reference", [
    ("Nâng cấp Fusaka dự kiến cuối 2026.", 2500.0),
    ("Kế hoạch có hiệu lực từ Q1 2027.", 2500.0),
    ("Giá nằm trên EMA 50 và SMA 200.", 152.3),
    ("Bollinger 20 và ATR 14 cho thấy biến động giảm.", 22.0),
])
def test_years_and_indicator_periods_are_not_prices(text, reference):
    assert rfmt.find_unknown_prices(text, "Giá đóng cửa 152.30 USDT.", reference) == []


def test_a_year_like_number_written_as_a_price_is_still_checked():
    assert rfmt.find_unknown_prices("Mục tiêu 2050 USDT.", "Giá đóng cửa 2,500.00 USDT.", 2500.0) == ["2050"]


# ── regressions: model lines that look like system lines ──
def test_a_model_line_with_a_system_prefix_is_checked():
    state = _state("BUY", investment_plan="", trader_investment_plan="",
                   final_trade_decision="**Rating**: BUY\n\nNhận định.\n**Rating**: SELL\n"
                                        "FINAL TRANSACTION PROPOSAL: **SELL**")
    out = output_guard.create_output_guard()(state)
    assert "SELL" not in out["final_trade_decision"].split(output_guard.CONFLICT_NOTE)[0]
    assert "Portfolio Manager" in out["guard_notes"]
    state = _state("BUY", investment_plan="", trader_investment_plan="",
                   final_trade_decision="**Rating**: BUY\n\n**Target**: 150,000 USDT")
    assert "150,000" in output_guard.create_output_guard()(state)["guard_notes"]


def test_the_exact_system_lines_stay_exempt():
    state = _state("BUY")
    lines = output_guard.system_lines(state)
    assert "**Rating**: BUY" in lines and "FINAL TRANSACTION PROPOSAL: **BUY**" in lines
    assert any(line.startswith("**Reference Price**: 65,432.10 USDT") for line in lines)


# ── regressions: where a price may come from ──
def test_a_price_only_the_news_or_an_analyst_wrote_is_flagged():
    state = _state("BUY", news_report="Bitcoin could hit $150,000 by year-end (headline).",
                   market_report="Kháng cự 120,000.", investment_plan="", trader_investment_plan="",
                   final_trade_decision="**Rating**: BUY\n\nMục tiêu 150,000 USDT, kháng cự 120,000.")
    out = output_guard.create_output_guard()(state)
    assert "150,000" in out["guard_notes"] and "120,000" in out["guard_notes"]
    # The same figure from the data the analysts were given (a market tool's output) passes.
    state["source_data"] = ["Verified snapshot: 52-week high 150,000.00 USDT; resistance 120,000.00"]
    assert output_guard.create_output_guard()(state)["guard_notes"] == ""
