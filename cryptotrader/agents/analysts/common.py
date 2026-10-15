"""The prompt every tool-using analyst runs under."""

from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from cryptotrader.agents.context import get_instrument_context_from_state

TOOL_ANALYST_SYSTEM = (
    "You are a helpful AI assistant, collaborating with other assistants."
    " Use the provided tools to progress towards answering the question."
    " If you are unable to fully answer, that's OK; another assistant with different tools"
    " will help where you left off. Execute what you can to make progress."
    " Report what your tools support; another agent decides the trade."
    " You have access to the following tools: {tool_names}."
    " Today's date is {current_date} (UTC); treat it as 'now' for all analysis and tool-call date ranges."
    " {instrument_context}\n{system_message}"
)


def tool_prompt(system_message: str, tools, state) -> ChatPromptTemplate:
    prompt = ChatPromptTemplate.from_messages([("system", TOOL_ANALYST_SYSTEM), MessagesPlaceholder("messages")])
    return prompt.partial(
        system_message=system_message,
        tool_names=", ".join(t.name for t in tools),
        current_date=state["trade_date"],
        instrument_context=get_instrument_context_from_state(state),
    )
