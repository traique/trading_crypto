"""Research Manager: judges the bull/bear debate against the system action for the trader. It cannot change the action."""

from __future__ import annotations

from cryptotrader.agents import crypto_context
from cryptotrader.agents.context import get_instrument_context_from_state, get_language_instruction
from cryptotrader.agents.schemas import ResearchPlan, render_research_plan
from cryptotrader.agents.structured import NO_EXTERNAL_TOOLS, bind_structured, invoke_structured


def create_research_manager(llm):
    structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")

    def research_manager_node(state) -> dict:
        debate = state["investment_debate_state"]
        action = crypto_context.action_of(state)
        prompt = f"""As the Research Manager and debate facilitator, evaluate this round of the bull/bear debate on a crypto asset and hand the trader a clear assessment.

{get_instrument_context_from_state(state)}

{crypto_context.quant_block(state)}

---

The action {action} is fixed by the system. Your job is to judge the debate, not to pick the action:
- Decide which side argued better on the evidence, independent of who spoke first or last.
- State whether the debate supports the system action: agree, partly_agree or disagree. Disagreement is recorded for the portfolio manager but does not change the action.
- Tell the trader what to monitor: the levels already given, dated catalysts (unlocks, listings, macro events), liquidity, exchange and custody risk.

**Debate History:**
{debate.get("history", "")}

## Output
- **Stance on the system action**: agree / partly_agree / disagree
- **Rationale**: which arguments were strongest and why
- **Strategic Actions**: what the trader must watch while applying the action

{crypto_context.NO_INVENT_RULE}

{crypto_context.NO_LEVERAGE_RULE}

{crypto_context.GROUNDING_RULES}

{NO_EXTERNAL_TOOLS}""" + get_language_instruction()

        result = invoke_structured(structured_llm, prompt, "Research Manager")
        if result is not None:
            plan = render_research_plan(result, action)
        else:
            plan = f"**Recommendation**: {action} (hành động của hệ thống)\n\n{llm.invoke(prompt).content}"
        return {
            "investment_debate_state": {**debate, "current_response": plan},
            "investment_plan": plan,
        }

    return research_manager_node
