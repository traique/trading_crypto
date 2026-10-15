"""Portfolio Manager: writes the final decision around the system action; the rating is the engine's."""

from __future__ import annotations

from cryptotrader.agents import crypto_context
from cryptotrader.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
)
from cryptotrader.agents.schemas import PortfolioDecision, render_pm_decision
from cryptotrader.agents.structured import NO_EXTERNAL_TOOLS, bind_structured, invoke_structured
from cryptotrader.dataflows import fmt


def create_portfolio_manager(llm):
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        risk = state["risk_debate_state"]
        decision = crypto_context.quant_decision(state)
        action = crypto_context.action_of(state)
        prompt = f"""As the Portfolio Manager, write the final decision on this crypto asset for the investor.

{get_instrument_context_from_state(state)}

{get_portfolio_context_from_state(state)}

{crypto_context.quant_block(state)}

---

The action {action} and every number of the plan are fixed by the system; you explain them, you do not choose them. Ground every statement in specific evidence from the analysts and the risk debate. Where the debate disagrees with the system, say so plainly as a risk - do not soften it and do not change the action. Be honest about uncertainty: no backtest or past performance is a promise of future returns, and fees (0.1% per side) and slippage reduce every trade.

**Context:**
- Research Manager's assessment: **{state.get("investment_plan", "")}**
- Trader's execution plan: **{state.get("trader_investment_plan", "")}**

**Risk Analysts Debate History:**
{risk.get("history", "")}

---

## Output
- **Executive Summary**: the system action and how to act on it with the levels and sizing already given (or, for NO_TRADE/SELL, why there is no entry)
- **Investment Thesis**: the evidence that supports or strains the action, and what would change it
- **Key Risks**: two or three risks that would make the scenario wrong (market, exchange/custody, supply, regulation)
- **Time Horizon**: optional

{crypto_context.NO_INVENT_RULE}

{crypto_context.NO_LEVERAGE_RULE}

{crypto_context.GROUNDING_RULES}

{NO_EXTERNAL_TOOLS}{get_language_instruction()}"""

        result = invoke_structured(structured_llm, prompt, "Portfolio Manager")
        if result is not None:
            final = render_pm_decision(result, decision)
        else:
            price = decision.get("price")
            final = "\n".join([
                f"**Rating**: {action}", "", llm.invoke(prompt).content, "",
                f"**Reference Price**: {fmt.price(price) + ' USDT' if price is not None else 'not provided'}",
            ])
        return {
            "risk_debate_state": {**risk, "latest_speaker": "Judge"},
            "final_trade_decision": final,
            "final_rating": action,
        }

    return portfolio_manager_node
