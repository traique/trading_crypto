"""Configuration of the multi-agent analysis: the built-in defaults plus the CRYPTO_AGENTS_* environment.

The LLM provider defaults to ``router9_chain``: the OpenAI-compatible provider
chain (9Router -> Groq -> OpenRouter -> Grok) configured by the key variables
in ``llm_clients.fallback_client``. Any OpenAI-compatible provider of
``llm_clients.openai_client`` can be named instead; it then needs its key
variable and real model names (CRYPTO_AGENTS_QUICK_THINK_LLM /
CRYPTO_AGENTS_DEEP_THINK_LLM): "default" only means something to the chain.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptotrader.config import DATA_DIR_ENV

# Env var -> config key. Coercion follows the type of the default value.
_ENV_OVERRIDES = {
    "CRYPTO_AGENTS_LLM_PROVIDER": "llm_provider",
    "CRYPTO_AGENTS_DEEP_THINK_LLM": "deep_think_llm",
    "CRYPTO_AGENTS_QUICK_THINK_LLM": "quick_think_llm",
    "CRYPTO_AGENTS_DEEP_THINK_PROVIDER": "deep_think_provider",
    "CRYPTO_AGENTS_QUICK_THINK_PROVIDER": "quick_think_provider",
    "CRYPTO_AGENTS_LLM_BACKEND_URL": "backend_url",
    "CRYPTO_AGENTS_OUTPUT_LANGUAGE": "output_language",
    "CRYPTO_AGENTS_MAX_DEBATE_ROUNDS": "max_debate_rounds",
    "CRYPTO_AGENTS_MAX_RISK_ROUNDS": "max_risk_discuss_rounds",
    "CRYPTO_AGENTS_MAX_TOOL_ROUNDS": "max_tool_rounds",
    "CRYPTO_AGENTS_TEMPERATURE": "temperature",
    "CRYPTO_AGENTS_LLM_MAX_RETRIES": "llm_max_retries",
    "CRYPTO_AGENTS_MAX_TOKENS": "max_tokens",
    "CRYPTO_AGENTS_NEWS_LIMIT": "news_article_limit",
    "CRYPTO_ENGINE_STRATEGY": "engine_strategy",
}

_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")


def _coerce(value: str, reference):
    """``value`` as the type of ``reference``; ValueError on a value that does not parse."""
    if isinstance(reference, bool):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE:
            return True
        if normalized in _BOOL_FALSE:
            return False
        raise ValueError(f"expected a boolean ({'/'.join(_BOOL_TRUE + _BOOL_FALSE)}), got {value!r}")
    if isinstance(reference, int):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    return value


def _apply_env_overrides(config: dict) -> dict:
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        try:
            config[key] = _coerce(raw, config.get(key))
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_var}: {exc}") from exc
    return config


def build_default_config() -> dict:
    """The built-in defaults with the environment folded in (read again on every call)."""
    # Not created here: importing the package must not touch the disk.
    home = os.environ.get("CRYPTO_AGENTS_HOME") or str(
        Path(os.environ.get(DATA_DIR_ENV) or "data").expanduser() / "agents")
    return _apply_env_overrides({
        "results_dir": os.environ.get("CRYPTO_AGENTS_RESULTS_DIR") or os.path.join(home, "results"),
        "data_cache_dir": os.environ.get("CRYPTO_AGENTS_CACHE_DIR") or os.path.join(home, "cache"),
        # LLM settings. "default" lets each provider of the chain use its own *_MODEL.
        "llm_provider": "router9_chain",
        "deep_think_llm": "default",
        "quick_think_llm": "default",
        "backend_url": None,
        "quick_think_provider": None,
        "deep_think_provider": None,
        "quick_think_backend_url": None,
        "deep_think_backend_url": None,
        "openai_reasoning_effort": None,
        "temperature": None,
        "llm_max_retries": None,
        "max_tokens": None,
        # Reports are written in Vietnamese; the machine-read labels stay in English.
        "output_language": "Vietnamese",
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "max_recur_limit": 100,
        "max_tool_rounds": 12,
        # Full state logs (results_dir/<PAIR>/logs): the newest this many per pair are
        # kept (the host disk is small); 0 turns the logs off.
        "state_logs_kept": 30,
        # News
        "news_article_limit": 20,
        "global_news_article_limit": 12,
        "news_lookback_days": 7,
        "global_news_queries": [
            "crypto market bitcoin ETF flows",
            "Fed interest rates crypto",
            "crypto regulation SEC stablecoin",
            "crypto exchange hack outage",
            "thị trường tiền mã hoá bitcoin",
        ],
        # The strategy whose engine view decides the system action (cryptotrader.strategies.REGISTRY).
        "engine_strategy": "btc_sma_ens",
    })


DEFAULT_CONFIG = build_default_config()
