"""/phantich: a small adapter between the bot and the multi-agent graph (``cryptotrader.graph``).

The graph is imported lazily: the bot starts, trades and answers /nhanh
without it (or without any AI key), and /phantich then says clearly why the
AI analysis is unavailable instead of failing.

What the adapter expects from ``cryptotrader.graph`` (either form):

1. a function ``analyze(symbol, date, save_dir=..., portfolio=..., config=...)``
   returning a mapping or an object with ``rating`` and ``final_decision``
   (text), and optionally ``report_markdown`` / ``html_path`` (``portfolio`` and
   ``config`` are passed only when the function accepts them); or
2. a graph class (``CryptoTradingGraph``, ``TradingAgentsGraph`` or
   ``TradingGraph``) whose ``propagate(symbol, date)`` returns
   ``(state, rating)`` with ``state["final_trade_decision"]``, and optionally
   ``save_reports(state, symbol, save_path=...)``.

The AI provider chain is read from ``cryptotrader.llm_clients.fallback_client``
when it exists (``configured_order``, ``provider_status``), else from the usual
key variables in the environment.
"""

from __future__ import annotations

import html
import importlib
import inspect
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

GRAPH_MODULE = "cryptotrader.graph"
FALLBACK_MODULE = "cryptotrader.llm_clients.fallback_client"
GRAPH_CLASSES = ("CryptoTradingGraph", "TradingAgentsGraph", "TradingGraph")

# (label, key variable, extra variable that must also be set)
_ENV_PROVIDERS = (
    ("9Router", "ROUTER9_API_KEY", "ROUTER9_BASE_URL"),
    ("Groq", "GROQ_API_KEY", None),
    ("OpenRouter", "OPENROUTER_API_KEY", None),
    ("Grok (xAI)", "XAI_API_KEY", None),
    ("OpenAI", "OPENAI_API_KEY", None),
    ("Anthropic", "ANTHROPIC_API_KEY", None),
    ("Google", "GOOGLE_API_KEY", None),
    ("DeepSeek", "DEEPSEEK_API_KEY", None),
)


class AnalysisUnavailable(RuntimeError):
    """The AI analysis cannot run here (no graph module, no AI key); the message is for people."""


@dataclass
class Analysis:
    rating: str
    final_decision: str
    report_dir: Path
    html_path: Path | None = None
    guard_notes: str = ""


def _import(name: str):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        logger.info("%s not importable: %s", name, exc)
        return None
    except Exception:  # noqa: BLE001 - a broken optional module must not take the bot down
        logger.exception("%s failed to import", name)
        return None


def provider_status() -> list[dict]:
    """Each configured AI provider in order: {label, model, resting_seconds, last_error}."""
    module = _import(FALLBACK_MODULE)
    if module is not None and hasattr(module, "provider_status"):
        try:
            return list(module.provider_status())
        except Exception:  # noqa: BLE001 - a status page must not fail on it
            logger.exception("provider_status failed")
    rows = []
    for label, key, extra in _ENV_PROVIDERS:
        if os.environ.get(key) and (extra is None or os.environ.get(extra)):
            rows.append({"label": label, "model": "", "resting_seconds": 0, "last_error": ""})
    return rows


def gate_line() -> str:
    """The LLM rate gate (sequential calls, call budgets) as one line, or "" when unavailable."""
    module = _import("cryptotrader.llm_clients.rate_gate")
    if module is None:
        return ""
    try:
        return str(module.status_line())
    except Exception:  # noqa: BLE001 - a status page must not fail on it
        logger.exception("gate status failed")
        return ""


def configured_order() -> list[str]:
    module = _import(FALLBACK_MODULE)
    if module is not None and hasattr(module, "configured_order"):
        try:
            return list(module.configured_order())
        except Exception:  # noqa: BLE001
            logger.exception("configured_order failed")
    return [row["label"] for row in provider_status()]


_CHAIN_REASON = ("chưa cấu hình nhà cung cấp AI nào (ROUTER9_BASE_URL + ROUTER9_API_KEY, GROQ_API_KEY, "
                 "OPENROUTER_API_KEY...)")


def _tier_reason() -> str | None:
    """Why the configured provider of the quick or deep tier cannot run (Vietnamese), or None.

    Reads the same config the graph builds its clients from: each tier's provider
    (CRYPTO_AGENTS_LLM_PROVIDER, or CRYPTO_AGENTS_QUICK/DEEP_THINK_PROVIDER), its key
    variable and its model. Returns "" when that config cannot be read here (the
    adapter then falls back to the provider chain check)."""
    try:
        from cryptotrader.default_config import build_default_config
        from cryptotrader.llm_clients.api_key_env import get_api_key_env
        from cryptotrader.llm_clients.factory import tier_provider
        from cryptotrader.llm_clients.openai_client import OPENAI_COMPATIBLE_PROVIDERS
    except Exception:  # noqa: BLE001 - not this package: only the chain check applies
        return ""
    try:
        config = build_default_config()
    except ValueError as exc:
        return f"cấu hình AI không hợp lệ: {exc}"
    for tier in ("quick", "deep"):
        provider = tier_provider(config, tier)
        if provider == "router9_chain":
            if not configured_order():
                return _CHAIN_REASON
            continue
        spec = OPENAI_COMPATIBLE_PROVIDERS.get(provider)
        if spec is None and provider != "azure":
            return f"nhà cung cấp AI {provider!r} không được hỗ trợ (CRYPTO_AGENTS_LLM_PROVIDER)"
        key_env = get_api_key_env(provider)
        if key_env and not (spec is not None and spec.key_optional) and not os.environ.get(key_env):
            return f"nhà cung cấp AI {provider} cần biến {key_env} (chưa đặt)"
        if str(config.get(f"{tier}_think_llm") or "default").strip().lower() == "default":
            return (f"nhà cung cấp AI {provider} cần tên model: đặt CRYPTO_AGENTS_{tier.upper()}_THINK_LLM "
                    "(model \"default\" chỉ dùng được với chuỗi router9_chain)")
    return None


def unavailable_reason() -> str | None:
    """Why /phantich cannot run now (Vietnamese), or None when it can."""
    if _import(GRAPH_MODULE) is None:
        return "phần phân tích đa agent (cryptotrader.graph) chưa được cài trên máy chủ này"
    reason = _tier_reason()
    if reason == "":
        return None if configured_order() else _CHAIN_REASON
    return reason


def fallback_message(pair: str, reason: str) -> str:
    base = pair[:-4] if pair.endswith("USDT") else pair
    return (f"ℹ️ Phân tích AI chưa dùng được: {reason}. "
            f"Xem nhanh không cần AI: /nhanh {base}.")


def _accepts(fn, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class Analyst:
    """Runs the graph, one analysis at a time (the caller's queue serialises them)."""

    def __init__(self):
        self._graph = None
        self._lock = threading.Lock()

    def _instance(self, module):
        if self._graph is None:
            cls = next((getattr(module, n) for n in GRAPH_CLASSES if hasattr(module, n)), None)
            if cls is None:
                raise AnalysisUnavailable("cryptotrader.graph không có hàm analyze hay lớp graph mà bot dùng được")
            self._graph = cls()
        return self._graph

    def run(self, symbol: str, day: str, report_dir: Path, *, portfolio: dict | None = None,
            config: dict | None = None) -> Analysis:
        """One analysis. ``portfolio`` ({"weights", "nav", "cash"}) lets the system action tell
        BUY from HOLD; ``config`` overrides graph settings (the bot passes its engine strategy).
        Both are passed only to an ``analyze`` that accepts them."""
        reason = unavailable_reason()
        if reason:
            raise AnalysisUnavailable(reason)
        module = _import(GRAPH_MODULE)
        report_dir = Path(report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if hasattr(module, "analyze"):
                extra = {k: v for k, v in (("portfolio", portfolio), ("config", config))
                         if v is not None and _accepts(module.analyze, k)}
                result = module.analyze(symbol, day, save_dir=report_dir, **extra)
                get = (result.get if isinstance(result, dict) else lambda k, d=None: getattr(result, k, d))
                rating = str(get("rating", "") or "")
                decision = str(get("final_decision", "") or get("final_trade_decision", "") or "")
                markdown = str(get("report_markdown", "") or "")
                html_path = get("html_path", None)
                guard = str(get("guard_notes", "") or "")
            else:
                graph = self._instance(module)
                state, rating = graph.propagate(symbol, day)
                state = state or {}
                decision = str(state.get("final_trade_decision", "") or "")
                guard = str(state.get("guard_notes", "") or "")
                markdown, html_path = "", None
                if hasattr(graph, "save_reports"):
                    saved = graph.save_reports(state, symbol, save_path=report_dir)
                    candidate = Path(saved) / "complete_report.html" if saved else None
                    html_path = candidate if candidate and candidate.exists() else None
                rating = str(rating or "")
        html_file = Path(html_path) if html_path else report_dir / "complete_report.html"
        if not html_file.exists():
            html_file = write_html(report_dir, f"{symbol} {day}", markdown or decision)
        return Analysis(rating=rating, final_decision=decision, report_dir=report_dir,
                        html_path=html_file, guard_notes=guard)


def write_html(report_dir: Path, title: str, markdown: str) -> Path:
    """A self-contained HTML page from the report's Markdown (no script, no external file)."""
    try:
        from markdown_it import MarkdownIt

        body = MarkdownIt("commonmark", {"html": False}).render(markdown or "")
    except ImportError:
        body = f"<pre>{html.escape(markdown or '')}</pre>"
    page = ("<!doctype html><html lang=\"vi\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(title)}</title><style>body{{max-width:820px;margin:24px auto;padding:0 16px;"
            "font:15px/1.6 system-ui,sans-serif}}pre{{white-space:pre-wrap}}</style></head><body>"
            f"<h1>{html.escape(title)}</h1>{body}"
            "<p><small>Không phải khuyến nghị đầu tư.</small></p></body></html>")
    path = report_dir / "complete_report.html"
    (report_dir / "report.md").write_text(markdown or "", encoding="utf-8")
    path.write_text(page, encoding="utf-8")
    return path
