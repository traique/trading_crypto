"""The analysis graph of one coin on one day, and ``analyze``, the one call the bot needs."""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import cryptotrader
from cryptotrader.agents.context import build_instrument_context, render_portfolio
from cryptotrader.agents.rating import run_rating
from cryptotrader.data.symbols import normalize_symbol, safe_file_component
from cryptotrader.dataflows import tokenomics
from cryptotrader.dataflows.config import run_config, run_config_context, set_config
from cryptotrader.dataflows.date_window import validate_trade_date
from cryptotrader.default_config import apply_profile, build_default_config
from cryptotrader.llm_clients import create_tier_client, tier_provider
from cryptotrader.llm_clients.rate_gate import analysis_budget
from cryptotrader.reporting import write_report_tree

from .analyst_execution import DEFAULT_ANALYSTS
from .conditional_logic import ConditionalLogic
from .propagation import Propagator
from .setup import GraphSetup

logger = logging.getLogger(__name__)


class CryptoTradingGraph:
    """Analysts, engine, debates, trader, risk team, portfolio manager and Output Guard for one pair."""

    def __init__(self, selected_analysts=DEFAULT_ANALYSTS, debug: bool = False,
                 config: dict[str, Any] | None = None, callbacks: list | None = None):
        self.debug = debug
        # The profile (lite/full) of the merged config sets the rounds the caller did not set itself.
        self.config = apply_profile({**build_default_config(), **(config or {})}, explicit=set(config or {}))
        self.callbacks = callbacks or []
        set_config(self.config)
        Path(self.config["results_dir"]).mkdir(parents=True, exist_ok=True)

        extra = {"callbacks": self.callbacks} if self.callbacks else {}
        self.deep_thinking_llm = create_tier_client(self.config, "deep", **extra).get_llm()
        self.quick_thinking_llm = create_tier_client(self.config, "quick", **extra).get_llm()

        max_tool_rounds, max_recur_limit = int(self.config["max_tool_rounds"]), int(self.config["max_recur_limit"])
        if 2 * max_tool_rounds + 2 >= max_recur_limit:
            raise ValueError(f"max_tool_rounds={max_tool_rounds} needs max_recur_limit above {2 * max_tool_rounds + 2}")
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=int(self.config["max_debate_rounds"]),
            max_risk_discuss_rounds=int(self.config["max_risk_discuss_rounds"]),
        )
        self.selected_analysts = tuple(selected_analysts)
        self.graph_setup = GraphSetup(self.quick_thinking_llm, self.deep_thinking_llm, self.conditional_logic,
                                      max_tool_rounds, engine_config=self.config)
        self.propagator = Propagator(max_recur_limit=max_recur_limit)
        self.workflow = self.graph_setup.setup_graph(self.selected_analysts)
        self.graph = self.workflow.compile()

    # ── a run ──
    def create_run_state(self, pair: str, trade_date: str, portfolio: dict | None = None) -> dict:
        return self.propagator.create_initial_state(
            pair, trade_date,
            instrument_context=build_instrument_context(pair, tokenomics.cached_name(pair)),
            portfolio_context=render_portfolio(portfolio, pair),
            portfolio=portfolio,
        )

    def propagate(self, symbol: str, trade_date, portfolio: dict | None = None):
        """Run the graph for ``symbol`` (BTC, BTCUSDT, BTC/USDT...) on ``trade_date`` (UTC day).

        Returns ``(final_state, rating)``; the rating is the system action
        (BUY / HOLD / SELL / NO_TRADE), never the model's.
        """
        trade_date = validate_trade_date(trade_date)
        pair = normalize_symbol(symbol)
        init = self.create_run_state(pair, trade_date, portfolio)
        args = self.propagator.get_graph_args()
        # One budget of LLM calls per analysis (LLM_MAX_CALLS_PER_ANALYSIS, see llm_clients.rate_gate).
        with run_config(self.config), analysis_budget(f"{pair} {trade_date}"):
            if self.debug:
                final_state: dict = {}
                for messages, state in self.stream_run(init, **args):
                    for msg in messages:
                        msg.pretty_print()
                    if state is not None:
                        final_state.update(state)
            else:
                final_state = self.graph.invoke(init, **args)
        self._log_state(trade_date, final_state)
        return final_state, run_rating(final_state)

    def stream_run(self, graph_input, **args):
        """Stream a run as ``(messages, state)`` pairs (an analyst's report once filed, or the run's state)."""
        args = {**args, "stream_mode": ["values", "tasks"]}
        context = run_config_context(self.config)
        stream = context.run(self.graph.stream, graph_input, subgraphs=True, **args)
        try:
            while (step := context.run(next, stream, None)) is not None:
                namespace, mode, chunk = step
                if namespace:
                    result = chunk.get("result") if mode == "tasks" else None
                    if isinstance(result, dict):
                        report = {k: v for k, v in result.items() if k != "messages" and v}
                        if result.get("messages") or report:
                            yield result.get("messages", []), report or None
                elif mode == "values":
                    yield chunk.get("messages", []), chunk
        finally:
            context.run(stream.close)

    # ── output ──
    def run_settings(self) -> dict:
        """What produced the run, for the report (no endpoints, keys or paths)."""
        cfg = self.config
        return {
            "version": cryptotrader.__version__,
            "llm_provider": cfg.get("llm_provider"),
            "deep_think_provider": tier_provider(cfg, "deep"),
            "deep_think_llm": cfg.get("deep_think_llm"),
            "quick_think_provider": tier_provider(cfg, "quick"),
            "quick_think_llm": cfg.get("quick_think_llm"),
            "analysts": list(self.selected_analysts),
            "max_debate_rounds": cfg.get("max_debate_rounds"),
            "max_risk_discuss_rounds": cfg.get("max_risk_discuss_rounds"),
            "output_language": cfg.get("output_language"),
            "engine_strategy": cfg.get("engine_strategy"),
        }

    def default_report_path(self, symbol: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return Path(self.config["results_dir"]) / "reports" / f"{safe_file_component(normalize_symbol(symbol))}_{stamp}"

    def save_reports(self, final_state: dict, symbol: str, save_path=None, html: bool = True) -> Path:
        """Write the report tree (Markdown per section, complete_report.md and .html); returns the .md path."""
        if save_path is None:
            save_path = self.default_report_path(symbol)
        return write_report_tree(final_state, normalize_symbol(symbol), save_path,
                                 settings=self.run_settings(), html=html)

    def _log_state(self, trade_date: str, final_state: dict) -> None:
        """Write the run's full state for auditing; only the newest ``state_logs_kept``
        files per pair are kept (0: no log), so months of use cannot fill the disk."""
        try:
            kept = int(self.config.get("state_logs_kept", 30))
        except (TypeError, ValueError):
            kept = 30
        if kept <= 0:
            return
        keys = ("company_of_interest", "trade_date", "market_report", "sentiment_report", "news_report",
                "tokenomics_report", "investment_plan", "trader_investment_plan", "final_trade_decision",
                "quant_decision", "quant_report", "guard_notes")
        entry = {k: final_state.get(k) for k in keys}
        entry["investment_debate_state"] = {k: (final_state.get("investment_debate_state") or {}).get(k)
                                            for k in ("bull_history", "bear_history", "history")}
        entry["risk_debate_state"] = {k: (final_state.get("risk_debate_state") or {}).get(k)
                                      for k in ("aggressive_history", "conservative_history", "neutral_history",
                                                "history")}
        entry["final_rating"] = run_rating(final_state)
        entry["run_settings"] = self.run_settings()
        pair = safe_file_component(normalize_symbol(final_state["company_of_interest"]))
        directory = Path(self.config["results_dir"]) / pair / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"full_states_log_{trade_date}.json").write_text(
            json.dumps(entry, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        prune_state_logs(directory, kept)


def prune_state_logs(directory: Path, kept: int) -> None:
    """Delete all but the ``kept`` most recently written state logs in ``directory``."""
    try:
        logs = sorted(directory.glob("full_states_log_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in logs[max(kept, 0):]:
        with contextlib.suppress(OSError):
            old.unlink()


def analyze(symbol: str, date: str, save_dir=None, portfolio: dict | None = None,
            config: dict | None = None) -> dict:
    """One analysis for the bot: run the graph, save the reports, return what the bot shows.

    Returns {rating, final_decision, report_markdown, html_path, guard_notes, report_dir}.
    """
    graph = CryptoTradingGraph(config=config)
    state, rating = graph.propagate(symbol, date, portfolio=portfolio)
    md_path = graph.save_reports(state, symbol, save_path=save_dir)
    html_path = md_path.parent / "complete_report.html"
    return {
        "rating": rating,
        "final_decision": state.get("final_trade_decision", ""),
        "report_markdown": md_path.read_text(encoding="utf-8"),
        "html_path": html_path if html_path.exists() else None,
        "guard_notes": state.get("guard_notes", ""),
        "report_dir": md_path.parent,
    }
