"""Output Guard: the code-level check that the LLM agents kept the system's decision.

The prompts tell every agent that the action and the numbers are fixed; this
node does not trust them to. It reads the texts that carry the decision - the
Research Manager's assessment, the Trader's plan and the Portfolio Manager's
final decision - and:

- an action label or advice contradicting the system action ("MUA" when the
  system says NO_TRADE, "nên bán" when it says BUY), or any advice to use
  leverage, futures or to short, drops that text's narrative and puts the
  system's own decision summary in its place, with a note;
- a price-like number that appears in none of the deterministic data (the
  engine's report, the market tools' outputs, the sentiment and tokenomics
  figures; not the analysts' prose, not the news) puts a warning and the
  system summary ahead of the text, which is kept;
- only the exact lines the renderers wrote from the engine's decision are
  exempt; a model line that merely starts like one ("**Rating**: SELL") is checked;
- the final decision always ends with exactly one standard disclaimer, and
  ``final_rating`` is always the system action.

The bull, bear and risk debaters argue sides by design and are not checked.
"""

from __future__ import annotations

import logging

from cryptotrader.agents import crypto_context, quant_engine, report_format as rfmt
from cryptotrader.llm_clients.fallback_client import AI_UNAVAILABLE_MARK

logger = logging.getLogger(__name__)

CONFLICT_NOTE = (
    "⚠️ Phần diễn giải của AI nêu hành động/kế hoạch khác với hệ thống (hoặc khuyên dùng đòn bẩy/bán khống) "
    "nên đã bị lược bỏ - chỉ dùng bản quyết định của hệ thống dưới đây."
)
AI_UNAVAILABLE_NOTE = (
    "⚠️ AI không khả dụng lúc chạy (mọi nhà cung cấp đều lỗi hoặc hết hạn mức): không có phần diễn giải - "
    "chỉ dùng bản quyết định của hệ thống dưới đây."
)
UNKNOWN_PRICE_NOTE = (
    "⚠️ Phần diễn giải dưới đây có mốc giá không nằm trong số liệu hệ thống - chỉ dùng các mốc trong bản "
    "quyết định của hệ thống."
)

def system_lines(state) -> set[str]:
    """The exact lines the renderers write from the engine's decision. Only these are
    exempt from the checks: a model line with the same prefix but another value
    ("**Rating**: SELL" under a BUY, "**Target**: 150,000 USDT") is the model's words."""
    from cryptotrader.agents.schemas import _usdt, plan_lines

    decision = crypto_context.quant_decision(state)
    action = crypto_context.action_of(state)
    price = decision.get("price")
    lines = {
        f"**Rating**: {action}",
        f"**Recommendation**: {action} (hành động của hệ thống)",
        f"**Action**: {action} (hành động của hệ thống)",
        f"FINAL TRANSACTION PROPOSAL: **{action}**",
        f"**Reference Price**: {_usdt(price)}",
        *plan_lines({**decision, "action": action}),
    }
    return {line.strip() for line in lines}


def _narrative(text: str, exempt: set[str]) -> str:
    return "\n".join(line for line in (text or "").splitlines() if line.strip() not in exempt)


def _summary(state) -> str:
    decision = crypto_context.quant_decision(state)
    return decision.get("deterministic_summary") or quant_engine.render_summary(decision or {})


def allowed_text(state) -> str:
    """Where a price may come from: deterministic data only - the engine's report and
    summary, the caller's holdings, and the data the analysts were given (the market
    tools' outputs, the sentiment indices, the tokenomics figures; ``source_data``).
    Never the analysts' prose or the news: a price a model invented or a headline
    claimed must not become "known" by being repeated."""
    return "\n".join([
        state.get("quant_report") or "",
        _summary(state),
        state.get("portfolio_context") or "",
        *[str(block) for block in (state.get("source_data") or [])],
    ])


def guard_text(text: str, state, *, label: str) -> tuple[str, str | None]:
    """The text to keep for one agent output, and a note when it was changed."""
    if not text:
        return text, None
    action = crypto_context.action_of(state)
    allowed = quant_engine.COMPATIBLE_ACTIONS.get(action, frozenset({action}))
    exempt = system_lines(state)
    narrative = _narrative(text, exempt)
    if AI_UNAVAILABLE_MARK in narrative:
        head = next((line for line in text.splitlines() if line.strip() in exempt), "")
        kept = f"{head}\n\n" if head else ""
        return (f"{kept}{AI_UNAVAILABLE_NOTE}\n\n{_summary(state)}",
                f"{label}: AI không khả dụng, dùng quyết định của hệ thống ({action})")
    conflicts = rfmt.find_action_conflicts(narrative, allowed) + rfmt.find_leverage_advice(narrative)
    if conflicts:
        logger.warning("%s contradicted the system action %s: %s", label, action, conflicts[:5])
        head = next((line for line in text.splitlines() if line.strip() in exempt), "")
        kept = f"{head}\n\n" if head else ""
        return (f"{kept}{CONFLICT_NOTE}\n\n{_summary(state)}",
                f"{label}: bỏ phần diễn giải vì mâu thuẫn với action {action} ({', '.join(conflicts[:3])})")
    unknown = rfmt.find_unknown_prices(narrative, allowed_text(state), crypto_context.reference_price(state))
    if unknown:
        logger.warning("%s cited prices outside the data: %s", label, unknown[:5])
        return (f"{UNKNOWN_PRICE_NOTE}\n\n{_summary(state)}\n\n{text}",
                f"{label}: có mốc giá ngoài dữ liệu ({', '.join(unknown[:5])})")
    return text, None


def guard_early(node, key: str, label: str):
    """``node`` with its ``key`` output checked at once, before later agents read it."""

    def guarded_node(state) -> dict:
        out = node(state)
        text = (out or {}).get(key)
        if not text:
            return out
        guarded, note = guard_text(text, {**state, **out}, label=label)
        if note is None:
            return out
        notes = [n for n in (state.get("guard_notes") or "").splitlines() if n]
        return {**out, key: guarded, "guard_notes": "\n".join([*notes, note])}

    return guarded_node


def create_output_guard():
    def output_guard_node(state) -> dict:
        notes: list[str] = [n for n in (state.get("guard_notes") or "").splitlines() if n]
        updates: dict = {}
        for key, label in (("investment_plan", "Research Manager"), ("trader_investment_plan", "Trader"),
                           ("final_trade_decision", "Portfolio Manager")):
            guarded, note = guard_text(state.get(key) or "", state, label=label)
            if note:
                notes.append(note)
                updates[key] = guarded
        final = updates.get("final_trade_decision", state.get("final_trade_decision") or "")
        updates["final_trade_decision"] = rfmt.ensure_disclaimer(rfmt.clean_analysis_output(final))
        updates["final_rating"] = crypto_context.action_of(state)
        updates["guard_notes"] = "\n".join(notes)
        return updates

    return output_guard_node
