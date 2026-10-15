"""Prompt context shared by the agents: coin identity, output language, portfolio, debate openings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cryptotrader.data.symbols import base_asset, normalize_symbol
from cryptotrader.dataflows import coins


def get_language_instruction() -> str:
    """The output-language instruction; the machine-read labels keep their English label and value."""
    from cryptotrader.dataflows.config import get_config

    lang = str(get_config().get("output_language", "Vietnamese"))
    if lang.strip().lower() == "english":
        return ""
    return (
        f" Write your entire response in {lang}, except the labelled lines the format asks for "
        "(the \"**Rating**:\" line, \"FINAL TRANSACTION PROPOSAL:\"): keep their label and value in "
        "English, exactly as specified."
    )


def opponent_argument_or_opening(text: str, opponent: str) -> str:
    """The opponent's latest argument, or a marker that they have not spoken (so none is invented)."""
    text = (text or "").strip()
    return text or f"(The {opponent} has not spoken yet — open the debate with your own case.)"


def report_or_absent(text: str, source: str) -> str:
    """An analyst's report, or a marker that it was not produced (an absence is not a finding)."""
    text = (text or "").strip()
    return text or f"(No {source} report in this run: it is not available, not an empty finding.)"


def build_instrument_context(pair: str, name: str | None = None) -> str:
    """The exact instrument, so no agent drifts to another coin or to a futures contract."""
    pair = normalize_symbol(pair)
    ticker = base_asset(pair)
    name = name or coins.coin_name(pair)
    who = f"{name} ({ticker})" if name and name.upper() != ticker else ticker
    return (
        f"The instrument to analyze is {who}, traded as the Binance SPOT pair `{pair}` "
        f"({ticker}/USDT), priced in USDT. Refer to it by this exact ticker in every report. "
        "It is a spot pair: no leverage, no shorting, no futures."
    )


def get_instrument_context_from_state(state: Mapping[str, Any]) -> str:
    context = state.get("instrument_context")
    if isinstance(context, str) and context.strip():
        return context
    return build_instrument_context(str(state["company_of_interest"]))


def render_portfolio(portfolio: Mapping[str, Any] | None, pair: str) -> str:
    """The caller's book as prompt text ('' when none was given)."""
    if not portfolio:
        return ""
    weights = portfolio.get("weights") or {}
    lines = ["Portfolio context (caller's paper/real book, weights as fraction of NAV):"]
    if portfolio.get("nav") is not None:
        lines.append(f"- NAV: {float(portfolio['nav']):,.2f} USDT")
    if portfolio.get("cash") is not None:
        lines.append(f"- Cash: {float(portfolio['cash']):,.2f} USDT")
    held = {k: float(v) for k, v in weights.items() if float(v or 0) > 0}
    lines.append("- Holdings: " + (", ".join(f"{base_asset(k)} {v * 100:.1f}%" for k, v in held.items())
                                   if held else "none (all cash)"))
    lines.append(f"- {base_asset(pair)} held: {'yes' if held.get(normalize_symbol(pair)) else 'no'}")
    return "\n".join(lines)


def get_portfolio_context_from_state(state: Mapping[str, Any]) -> str:
    context = state.get("portfolio_context")
    if isinstance(context, str) and context.strip():
        return context
    return ("Portfolio context: not provided. You do not know the caller's holdings or cash, so do not "
            "assume a flat book; give guidance the caller can apply to their own position.")
