"""Prompt blocks shared by every agent: the crypto market rules, the grounding rules, the system decision.

The engine (``agents.quant_engine``) decides the action and every number of the
plan before any LLM agent reasons about it. These blocks hand that decision to
the agents with the rules they work under: they explain, argue and stress-test;
they never change the action or invent a price.
"""

from __future__ import annotations

from cryptotrader.market import rules

NO_INVENT_RULE = (
    "TUYỆT ĐỐI không tự bịa hoặc đổi bất kỳ con số giá/vùng mua/stop/mục tiêu/tỷ trọng nào - chỉ dùng "
    "lại ĐÚNG các con số có trong dữ liệu được cấp. Cần nhắc tới một mốc giá thì phải là mốc đã có sẵn, "
    "không tự tính mốc mới."
)

NO_LEVERAGE_RULE = (
    "Chỉ bàn giao dịch SPOT bằng USDT trên Binance: KHÔNG khuyên dùng đòn bẩy, margin, futures/perpetual, "
    "KHÔNG khuyên bán khống (short). Funding và open interest của thị trường phái sinh chỉ được dùng như chỉ "
    "báo tâm lý."
)

GROUNDING_RULES = (
    "Quy tắc trình bày bắt buộc:\n"
    "- Mọi nhận định phải bám vào dữ liệu được cấp; phần nào thiếu thì nói thẳng \"chưa đủ dữ liệu\", gộp mọi "
    "phần thiếu vào ĐÚNG MỘT dòng ngắn ở cuối.\n"
    "- Mỗi mốc giá nhắc tới phải kèm khoảng cách % so với giá đã được tính sẵn trong dữ liệu; dùng lại đúng "
    "con số đó, không tự tính lại.\n"
    "- Chỉ báo kỹ thuật tính trên NẾN NGÀY ĐÃ ĐÓNG (đóng lúc 00:00 UTC, tức 07:00 giờ Việt Nam), không phải "
    "giá đang chạy: khi mô tả phải gắn ngày của nến đó.\n"
    "- Tin tức: chỉ tin đánh dấu ✅ mới là tin của chính coin; tin ⚠️ phải gọi rõ là tin thị trường chung. "
    "Mọi tin nhắc tới phải kèm ngày.\n"
    "- Không tự tính lãi/lỗ vị thế; không hứa hẹn lợi nhuận; không trình bày kết quả quá khứ như lợi nhuận "
    "kỳ vọng.\n"
    "- Số viết kiểu Binance: dấu phẩy phân cách nghìn, dấu chấm thập phân (65,432.10 USDT; -4.76%)."
)


def market_rules() -> str:
    """The trading facts of Binance spot that every plan must respect."""
    fee = rules.TAKER_FEE_PCT
    return (
        "Đặc điểm thị trường (Binance spot, cặp USDT):\n"
        "- Giao dịch 24/7, không có phiên và không có giới hạn biến động trong ngày: giá có thể biến động 10-20% trong vài giờ, "
        "kể cả cuối tuần và ban đêm giờ Việt Nam.\n"
        "- Nến ngày đóng lúc 00:00 UTC (07:00 giờ Việt Nam); hệ thống ra quyết định trên nến đã đóng, lệnh "
        "khớp từ nến sau.\n"
        f"- Phí taker mặc định {fee:g}% mỗi chiều (khứ hồi ~{2 * fee:g}% chưa kể trượt giá); không có thời gian "
        "chờ thanh toán - mua xong có thể bán ngay.\n"
        "- Khối lượng lẻ được phép nhưng phải theo bước LOT_SIZE và giá trị lệnh tối thiểu MIN_NOTIONAL của cặp; "
        "giá theo bước tickSize.\n"
        "- Rủi ro riêng của crypto: sàn bị hack/sập/đóng băng rút tiền, rủi ro lưu ký (giữ coin trên sàn), "
        "stablecoin mất neo, coin bị huỷ niêm yết, rủi ro pháp lý, thanh khoản mỏng ở altcoin nhỏ."
    )


def quant_decision(state) -> dict:
    """The engine's decision in graph state, or {} when it could not run."""
    return state.get("quant_decision") or {}


def action_of(state) -> str:
    return quant_decision(state).get("action") or "NO_TRADE"


def reference_price(state) -> float | None:
    price = quant_decision(state).get("price")
    try:
        return float(price) if price is not None else None
    except (TypeError, ValueError):
        return None


def quant_block(state, *, heading: str = "QUYẾT ĐỊNH CỦA HỆ THỐNG") -> str:
    """The engine report for a prompt, framed as fixed: no agent may change it."""
    report = state.get("quant_report") or ""
    if not report:
        reasons = "; ".join(quant_decision(state).get("reasons") or []) or "thiếu dữ liệu giá"
        return (f"[{heading}]\nHệ thống không chạy được cho lượt này ({reasons}). Hành động mặc định: "
                "NO_TRADE (đứng ngoài). Không agent nào được tự đặt vùng mua, stop, mục tiêu hay tỷ trọng.")
    return (f"[{heading} - action và mọi con số đã CHỐT bằng quy tắc định lượng; các agent chỉ được diễn "
            "giải, phản biện và chỉ ra rủi ro, KHÔNG được đổi]\n" + report)
