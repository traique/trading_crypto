"""Trader: turns the system decision into an execution plan on Binance spot.

The action and every level and size are the engine's; the trader explains them
and plans the execution (order type, fees and slippage, LOT_SIZE/MIN_NOTIONAL,
24/7 market, custody). The rendered proposal carries the engine's numbers, never
the model's.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from cryptotrader.agents import crypto_context
from cryptotrader.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
)
from cryptotrader.agents.schemas import TraderProposal, plan_lines, render_trader_proposal
from cryptotrader.agents.structured import NO_EXTERNAL_TOOLS, bind_structured, invoke_structured


def create_trader(llm):
    structured_llm = bind_structured(llm, TraderProposal, "Trader")

    def trader_node(state):
        decision = crypto_context.quant_decision(state)
        action = crypto_context.action_of(state)
        market_report = (state.get("market_report") or "").strip()
        report_section = f"Technical Market Report:\n{market_report}\n\n" if market_report else ""
        messages = [
            {"role": "system", "content": (
                "You are the trader of a crypto spot desk. The system has already decided the action "
                f"({action}) and every number of the plan; you explain the plan and how to execute it, and "
                "you may not change the action or any number. "
                + crypto_context.market_rules()
                + "\n\n" + crypto_context.NO_INVENT_RULE
                + "\n\n" + crypto_context.NO_LEVERAGE_RULE
                + "\n\n" + crypto_context.GROUNDING_RULES
                + "\n" + NO_EXTERNAL_TOOLS + get_language_instruction())},
            {"role": "user", "content": (
                f"Here is the research team's assessment for {state['company_of_interest']}. "
                f"{get_instrument_context_from_state(state)}\n\n"
                f"{crypto_context.quant_block(state)}\n\n{report_section}"
                f"{get_portfolio_context_from_state(state)}\n\n"
                f"Research Manager's assessment:\n{state.get('investment_plan', '')}\n\n"
                "## Output\n\n"
                "- **Reasoning**: why the evidence supports (or strains) the system action\n"
                "- **Execution Notes**: how to execute it on Binance spot - limit vs market order, the 0.1% "
                "taker fee per side and slippage, LOT_SIZE step and MIN_NOTIONAL, splitting the order, the 24/7 "
                "market (alerts), custody. If the action has no entry, say what would have to change for one.")},
        ]
        proposal = invoke_structured(structured_llm, messages, "Trader")
        if proposal is not None:
            plan = render_trader_proposal(proposal, decision)
        else:
            plan = "\n".join([
                f"**Action**: {action} (hành động của hệ thống)", "", llm.invoke(messages).content, "",
                *plan_lines(decision), "", f"FINAL TRANSACTION PROPOSAL: **{action}**",
            ])
        return {"messages": [AIMessage(content=plan)], "trader_investment_plan": plan}

    return trader_node
