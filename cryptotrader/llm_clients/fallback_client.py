"""A provider chain as one chat model: 9Router first, then Groq, OpenRouter, Grok.

Port of the VN project's provider chain (``ai/orchestrator.py``,
``ai/provider_state.py``) for the agents of this framework, which expect a
single LangChain chat model they can ``invoke``, ``bind_tools`` and ask for
``with_structured_output``. Each call tries the providers in order:

- a provider that fails to answer (connection, timeout, server error, a 4xx
  such as a request too large for its context) is skipped for this call;
- one that throttles (429) rests for its ``Retry-After`` or
  ``LLM_QUOTA_COOLDOWN_SEC`` (default 300 s), one that refuses the key for an
  hour, and 9Router once down rests ``ROUTER9_RETRY_SEC`` (default 900 s), so
  the next calls go straight to a working provider;
- when every provider is resting, they are all tried anyway, in order, rather
  than failing a call that one of them might now serve.

An answer the provider did give but that does not parse (structured output:
``OutputParserException`` / pydantic ``ValidationError``) is not a provider
failure: it is raised to the agent, which falls back to free text, as it does
on any single provider. A gateway that answers HTTP 200 with an error body, or
with null or missing ``choices`` (the SDK then raises ValueError, TypeError or
KeyError), IS a provider failure: the provider rests briefly and the next one
is tried.

When every provider failed, the agents' model (``degrade=True``, as the
factory builds it) answers with ``AI_UNAVAILABLE_TEXT`` instead of raising, and
skips the chain for ``LLM_DOWN_SKIP_SEC`` (default 300 s): the run completes
with the system's own decision (the Output Guard puts it in place of the AI
text) instead of crashing, and does not walk the dead chain once per node.

Configuration (environment):

    PROVIDER_ORDER       router9,groq,openrouter,xai (default; unset keys are skipped)
    ROUTER9_BASE_URL     9Router endpoint, OpenAI-compatible (…/v1)
    ROUTER9_API_KEY      9Router key
    ROUTER9_MODEL        default model on 9Router (the tier model overrides it)
    GROQ_API_KEY         GROQ_MODEL (default openai/gpt-oss-20b)
    OPENROUTER_API_KEY   OPENROUTER_MODEL (default a ":free" model)
    XAI_API_KEY          XAI_MODEL (default grok-4-fast) - Grok, optional
    LLM_CALL_TIMEOUT_SEC per-call timeout (default 120)
    LLM_BAD_RESPONSE_REST_SEC  rest after an error body / malformed answer (default 60)
    LLM_DOWN_SKIP_SEC    after every provider failed, answer AI_UNAVAILABLE_TEXT this long (default 300)

Every request (each provider tried) first passes the process-wide gate of
``rate_gate``: one request at a time by default, spaced and capped per minute,
hour, day and analysis (LLM_MAX_CONCURRENT, LLM_MIN_INTERVAL_SEC,
LLM_MAX_CALLS_PER_MINUTE / _PER_HOUR / _PER_DAY / _PER_ANALYSIS). A request the
gate refuses ends the call at once (``BudgetRefusedError``, no next provider):
the agents' model then answers AI_UNAVAILABLE_TEXT, without marking the chain
as down.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from .base_client import BaseLLMClient, normalize_content
from .rate_gate import GATE, LLMBudgetExceeded

logger = logging.getLogger(__name__)

DEFAULT_ORDER = ("router9", "groq", "openrouter", "xai")

# What the agents' model answers when no provider can (see the module docstring).
AI_UNAVAILABLE_MARK = "⚠️ AI không khả dụng"
AI_UNAVAILABLE_TEXT = (f"{AI_UNAVAILABLE_MARK}: mọi nhà cung cấp AI đều lỗi hoặc hết hạn mức lúc này, nên không có "
                       "phần diễn giải của AI; chỉ dùng quyết định của hệ thống.")


class AllProvidersFailedError(RuntimeError):
    """Every provider of the chain failed for one call."""


class BudgetRefusedError(AllProvidersFailedError):
    """The rate gate refused the call (a call budget is spent or the gateway is busy too long)."""


@dataclass(frozen=True)
class ProviderEndpoint:
    name: str
    label: str
    base_url_env: str | None
    default_base_url: str | None
    key_env: str
    model_env: str
    default_model: str


ENDPOINTS = {
    "router9": ProviderEndpoint("router9", "9Router", "ROUTER9_BASE_URL", None,
                                "ROUTER9_API_KEY", "ROUTER9_MODEL", "gpt-4o-mini"),
    "groq": ProviderEndpoint("groq", "Groq", "GROQ_BASE_URL", "https://api.groq.com/openai/v1",
                             "GROQ_API_KEY", "GROQ_MODEL", "openai/gpt-oss-20b"),
    "openrouter": ProviderEndpoint("openrouter", "OpenRouter", "OPENROUTER_BASE_URL",
                                   "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                                   "OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"),
    "xai": ProviderEndpoint("xai", "Grok (xAI)", "XAI_BASE_URL", "https://api.x.ai/v1",
                            "XAI_API_KEY", "XAI_MODEL", "grok-4-fast"),
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


# ─── Provider health, shared by every model of the process ───────────────────

_resting_until: dict[str, float] = {}
_last_error: dict[str, str] = {}
_lock = threading.Lock()


def _rest(name: str, seconds: float, reason: str) -> None:
    with _lock:
        _resting_until[name] = time.time() + seconds
        _last_error[name] = reason
    logger.warning("LLM provider %s rests %.0fs: %s", name, seconds, reason)


def _recovered(name: str) -> None:
    with _lock:
        was = _resting_until.pop(name, None)
        _last_error.pop(name, None)
    if was:
        logger.info("LLM provider %s answers again", name)


def is_resting(name: str) -> bool:
    with _lock:
        return time.time() < _resting_until.get(name, 0.0)


def provider_status() -> list[dict]:
    """Each configured provider in order, with whether it rests and why (for a status page)."""
    now = time.time()
    rows = []
    for name in configured_order():
        with _lock:
            until = _resting_until.get(name, 0.0)
            reason = _last_error.get(name, "")
        rows.append({
            "name": name,
            "label": ENDPOINTS[name].label,
            "model": os.environ.get(ENDPOINTS[name].model_env) or ENDPOINTS[name].default_model,
            "resting_seconds": max(0, int(until - now)),
            "last_error": reason,
        })
    return rows


def reset_provider_state() -> None:
    with _lock:
        _resting_until.clear()
        _last_error.clear()


def configured_order() -> list[str]:
    """Providers in PROVIDER_ORDER that have a key (and, for 9Router, an endpoint)."""
    raw = os.environ.get("PROVIDER_ORDER") or ",".join(DEFAULT_ORDER)
    order = []
    for name in (n.strip().lower() for n in raw.split(",")):
        if name == "grok":
            name = "xai"
        endpoint = ENDPOINTS.get(name)
        if endpoint is None or name in order or not os.environ.get(endpoint.key_env):
            continue
        if endpoint.default_base_url is None and not os.environ.get(endpoint.base_url_env or ""):
            continue
        order.append(name)
    return order


def _classify(exc: Exception) -> tuple[bool, float, str]:
    """(is it the provider's failure, how long to rest, a short reason)."""
    import httpx
    import openai

    status = getattr(exc, "status_code", None)
    if isinstance(exc, openai.RateLimitError) or status == 429:
        retry_after = None
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                retry_after = float(response.headers.get("retry-after", ""))
            except (TypeError, ValueError):
                retry_after = None
        return True, retry_after or _env_float("LLM_QUOTA_COOLDOWN_SEC", 300), "rate limited (429)"
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return True, 3600, f"key refused ({status})"
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, httpx.HTTPError, TimeoutError)):
        return True, 0, type(exc).__name__
    if isinstance(exc, openai.APIStatusError):
        return True, 0, f"HTTP {status}"
    if isinstance(exc, openai.APIError):
        return True, 0, type(exc).__name__
    from langchain_core.exceptions import OutputParserException
    from pydantic import ValidationError

    if isinstance(exc, (ValueError, TypeError, KeyError)) and not isinstance(exc, (OutputParserException,
                                                                                   ValidationError)):
        # HTTP 200 with an error body ({"error": {...}}) or null/missing choices: the SDK
        # raises these while building the result. The provider's failure, not the agent's.
        text = str(exc)
        if re.search(r"(?<!\d)429(?!\d)|rate.?limit|quota", text, re.IGNORECASE):
            return True, _env_float("LLM_QUOTA_COOLDOWN_SEC", 300), "rate limited (error body)"
        return True, _env_float("LLM_BAD_RESPONSE_REST_SEC", 60), f"bad response ({type(exc).__name__})"
    return False, 0, ""


def _router9_rest() -> float:
    return _env_float("ROUTER9_RETRY_SEC", 900)


def run_chain(chain: Sequence[tuple[str, Any]], call) -> Any:
    """``call(model)`` on each provider in turn until one answers.

    ``chain`` is ``[(name, model), ...]``. Resting providers go last instead of
    being dropped, so a call never fails while some provider might answer.
    """
    if not chain:
        raise RuntimeError(
            "No LLM provider configured: set ROUTER9_BASE_URL and ROUTER9_API_KEY, "
            "or GROQ_API_KEY / OPENROUTER_API_KEY / XAI_API_KEY."
        )
    ready = [item for item in chain if not is_resting(item[0])]
    ordered = ready + [item for item in chain if item not in ready]
    errors = []
    for name, model in ordered:
        try:
            with GATE.slot(name):
                result = call(model)
        except LLMBudgetExceeded as exc:
            # Not the provider's failure, and the next provider would spend the budget again.
            logger.warning("LLM call refused by the rate gate: %s", exc)
            raise BudgetRefusedError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - classified below
            is_provider_failure, rest, reason = _classify(exc)
            if not is_provider_failure:
                raise
            if name == "router9":
                rest = max(rest, _router9_rest())
            if rest:
                _rest(name, rest, reason)
            else:
                with _lock:
                    _last_error[name] = reason
            errors.append(f"{ENDPOINTS[name].label}: {reason}")
            logger.warning("LLM provider %s failed (%s); trying the next one", name, reason)
            continue
        _recovered(name)
        return result
    raise AllProvidersFailedError("Every LLM provider failed: " + "; ".join(errors))


# ─── The chat model the agents use ──────────────────────────────────────────


class FallbackChatModel(BaseChatModel):
    """One chat model over the provider chain; see the module docstring."""

    chain: list[tuple[str, Any]]
    tool_args: tuple | None = None   # (tools, kwargs) once bind_tools is called
    # Answer AI_UNAVAILABLE_TEXT instead of raising when every provider failed.
    degrade: bool = False
    # Shared by the copies bind_tools makes: when the chain was last found dead.
    outage: dict = Field(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "provider-chain"

    def _down(self) -> bool:
        return self.degrade and time.monotonic() < float(self.outage.get("until", 0.0))

    def _mark_down(self, exc: Exception) -> None:
        self.outage["until"] = time.monotonic() + _env_float("LLM_DOWN_SKIP_SEC", 300)
        self.outage["reason"] = str(exc)
        logger.error("Every LLM provider failed (%s); answering without AI for a while", exc)

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager=None, **kwargs
    ) -> ChatResult:
        def call(model):
            runnable = model
            if self.tool_args is not None:
                tools, tool_kwargs = self.tool_args
                runnable = model.bind_tools(tools, **tool_kwargs)
            return normalize_content(runnable.invoke(messages, stop=stop, **kwargs))

        if self._down():
            message = AIMessage(content=AI_UNAVAILABLE_TEXT)
        else:
            try:
                message = run_chain(self.chain, call)
            except BudgetRefusedError as exc:
                if not self.degrade:
                    raise
                message = AIMessage(content=f"{AI_UNAVAILABLE_TEXT} ({exc})")
            except AllProvidersFailedError as exc:
                if not self.degrade:
                    raise
                self._mark_down(exc)
                message = AIMessage(content=AI_UNAVAILABLE_TEXT)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tool_args": (list(tools), kwargs)})

    def with_structured_output(self, schema, **kwargs):
        chain = self.chain

        def invoke(prompt):
            if self._down():
                # Raised at once: the agent's free-text retry then gets AI_UNAVAILABLE_TEXT
                # without walking the dead chain a second time.
                raise AllProvidersFailedError(self.outage.get("reason") or "every LLM provider failed")
            try:
                return run_chain(chain, lambda model: model.with_structured_output(schema, **kwargs).invoke(prompt))
            except BudgetRefusedError:
                raise
            except AllProvidersFailedError as exc:
                if self.degrade:
                    self._mark_down(exc)
                raise

        return RunnableLambda(invoke)


def build_chain(model_override: str | None = None, **llm_kwargs) -> list[tuple[str, Any]]:
    """The configured providers as chat models, in order.

    ``model_override`` replaces the 9Router model (the tier model of the
    config); the other providers keep their own *_MODEL setting, since a model
    name of one gateway rarely exists on another.
    """
    from .openai_client import LocalCompatibleChatOpenAI

    timeout = _env_float("LLM_CALL_TIMEOUT_SEC", 120)
    chain = []
    for name in configured_order():
        endpoint = ENDPOINTS[name]
        model = os.environ.get(endpoint.model_env) or endpoint.default_model
        if name == "router9" and model_override:
            model = model_override
        base_url = (os.environ.get(endpoint.base_url_env or "") or endpoint.default_base_url or "").rstrip("/")
        kwargs = {k: v for k, v in llm_kwargs.items() if k in ("temperature", "max_tokens", "callbacks")}
        # One retry inside the SDK; the chain is the real fallback.
        chain.append((name, LocalCompatibleChatOpenAI(
            model=model, base_url=base_url, api_key=os.environ[endpoint.key_env],
            timeout=timeout, max_retries=1, **kwargs,
        )))
    return chain


class FallbackClient(BaseLLMClient):
    """``llm_provider: "router9_chain"`` - the provider chain as one client."""

    provider = "router9_chain"

    def get_llm(self) -> Any:
        model = None if self.model in (None, "", "default") else self.model
        return FallbackChatModel(chain=build_chain(model, **self.kwargs), degrade=True)

    def validate_model(self) -> bool:
        return True
