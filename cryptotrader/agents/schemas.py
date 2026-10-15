"""Pydantic schemas of the agents that produce structured output, and their Markdown renderers.

The action and every price level come from the engine; the schemas ask the
model for the explanation only, and the renderers write the engine's action and
numbers themselves, so a model can neither change nor misquote them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from cryptotrader.dataflows import fmt


class SystemAction(StrEnum):
    """The engine's four actions. SELL only exits a holding: this is spot, nothing is shorted."""

    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    NO_TRADE = "NO_TRADE"


class Stance(StrEnum):
    AGREE = "agree"
    PARTLY_AGREE = "partly_agree"
    DISAGREE = "disagree"


# ── Research Manager ──
class ResearchPlan(BaseModel):
    """The Research Manager's assessment of the bull/bear debate against the system action."""

    stance: Stance = Field(description=(
        "Whether the debate supports the system action: agree, partly_agree or disagree. "
        "Disagreeing is allowed and recorded; it does not change the action."))
    rationale: str = Field(description=(
        "Which arguments of the bull and bear sides were strongest and why, anchored in specific "
        "data from the reports."))
    strategic_actions: str = Field(description=(
        "What the trader should monitor while applying the system action: the levels already given, "
        "dated catalysts (unlocks, listings, macro events), liquidity, exchange and custody risk. "
        "Use only price levels already given."))


def render_research_plan(plan: ResearchPlan, action: str = "NO_TRADE") -> str:
    return "\n".join([
        f"**Recommendation**: {action} (hành động của hệ thống)",
        "",
        f"**Stance on the system action**: {plan.stance.value}",
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
    ])


# ── Trader ──
class TraderProposal(BaseModel):
    """The Trader's execution note; the action and every number are filled in from the engine."""

    reasoning: str = Field(description=(
        "The case for the system action, anchored in the analysts' reports and the research plan. "
        "Two to four sentences."))
    execution_notes: str = Field(description=(
        "How to execute the system plan on Binance spot: limit or market order, the 0.1% taker fee per "
        "side and slippage, the pair's LOT_SIZE step and MIN_NOTIONAL, splitting the order, the 24/7 "
        "market (alerts instead of watching), withdrawing to self-custody when holding long. Spot only: "
        "no leverage, no shorting. Use only price levels already given; if the action has no plan, say "
        "what would have to change for one."))


def _usdt(value) -> str:
    return f"{fmt.price(value)} USDT" if value is not None else "not provided"


def plan_lines(decision: dict) -> list[str]:
    """The engine's plan as labelled Markdown lines (always all named)."""
    action = decision.get("action") or "NO_TRADE"
    if decision.get("entry_low") is not None:
        entry = f"{_usdt(decision['entry_low'])} - {_usdt(decision.get('entry_high'))}"
    else:
        entry = "not provided"
    target_w, current_w = decision.get("target_weight"), decision.get("current_weight")
    sizing = f"{fmt.number(target_w * 100, 1)}% NAV (mục tiêu của chiến lược)" if target_w is not None else "not provided"
    if current_w is not None:
        sizing += f"; đang giữ {fmt.number(current_w * 100, 1)}% NAV"
    if action in ("SELL", "NO_TRADE"):
        lines = [f"**{label}**: not provided" for label in ("Entry Zone", "Stop Loss", "Target")]
    else:
        lines = [f"**Entry Zone**: {entry}", f"**Stop Loss**: {_usdt(decision.get('stop_price'))}",
                 f"**Target**: {_usdt(decision.get('target_price'))}"]
    lines.append(f"**Reference Price**: {_usdt(decision.get('price'))} (đóng cửa nến {decision.get('last_candle_date') or '?'})")
    lines.append(f"**Position Sizing**: {sizing}")
    return lines


def render_trader_proposal(proposal: TraderProposal, decision: dict | None = None) -> str:
    decision = decision or {}
    action = decision.get("action") or "NO_TRADE"
    return "\n".join([
        f"**Action**: {action} (hành động của hệ thống)",
        "",
        f"**Reasoning**: {proposal.reasoning}",
        "",
        f"**Execution Notes**: {proposal.execution_notes}",
        "",
        *plan_lines(decision),
        "",
        f"FINAL TRANSACTION PROPOSAL: **{action}**",
    ])


# ── Portfolio Manager ──
class PortfolioDecision(BaseModel):
    """The Portfolio Manager's write-up; the rating is the engine's and is filled in by the node."""

    executive_summary: str = Field(description=(
        "A concise statement of the system action and how to act on it with the levels and sizing "
        "already given, and the time horizon. Two to four sentences."))
    investment_thesis: str = Field(description=(
        "Detailed reasoning anchored in specific evidence from the analysts and the risk debate."))
    key_risks: str = Field(description=(
        "Two or three risks that would make the scenario wrong (market, exchange/custody, token "
        "supply, regulation), with a level already given where there is one."))
    time_horizon: str | None = Field(default=None, description="Optional holding period, e.g. '2-6 tuần'.")


def render_pm_decision(decision: PortfolioDecision, system: dict | None = None) -> str:
    system = system or {}
    return "\n".join([
        f"**Rating**: {system.get('action') or 'NO_TRADE'}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
        "",
        f"**Key Risks**: {decision.key_risks}",
        "",
        f"**Reference Price**: {_usdt(system.get('price'))}",
        "",
        f"**Time Horizon**: {decision.time_horizon or 'not provided'}",
    ])


# ── Sentiment Analyst ──
class SentimentBand(StrEnum):
    BULLISH = "Bullish"
    MILDLY_BULLISH = "Mildly Bullish"
    NEUTRAL = "Neutral"
    MIXED = "Mixed"
    MILDLY_BEARISH = "Mildly Bearish"
    BEARISH = "Bearish"


class SentimentReport(BaseModel):
    """Structured sentiment report: a band, a score, a confidence and the narrative."""

    overall_band: SentimentBand = Field(description=(
        "Overall sentiment direction. Exactly one of: Bullish / Mildly Bullish / Neutral / Mixed / "
        "Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions."))
    overall_score: float = Field(ge=0.0, le=10.0, description=(
        "Sentiment intensity 0-10: 0 maximally bearish, 5 neutral, 10 maximally bullish; consistent "
        "with overall_band."))
    confidence: Literal["low", "medium", "high"] = Field(description=(
        "low when a source was unavailable or there are fewer than 3 items about the coin itself; "
        "medium when data is present but sparse; high when news, Fear & Greed and derivatives data "
        "all returned substantive data."))
    narrative: str = Field(description=(
        "Source-by-source breakdown with dated evidence, divergences, dominant themes, catalysts and "
        "risks, and a Markdown table summarising the key signals."))


def render_sentiment_report(report: SentimentReport) -> str:
    return "\n".join([
        f"**Overall Sentiment:** **{report.overall_band.value}** (Score: {report.overall_score:.1f}/10)",
        f"**Confidence:** {report.confidence.capitalize()}",
        "",
        report.narrative,
    ])
