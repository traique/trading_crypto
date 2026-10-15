"""Build the LLM client of a provider, or of a model tier (quick / deep) from a config."""

from __future__ import annotations

from typing import Any

from .base_client import BaseLLMClient


def create_llm_client(provider: str, model: str, base_url: str | None = None, **kwargs) -> BaseLLMClient:
    """The client for ``provider``; modules are imported lazily so importing the factory costs nothing.

    Providers: ``router9_chain`` (the fallback chain, the default), ``azure``, and
    every OpenAI-compatible provider of ``openai_client.OPENAI_COMPATIBLE_PROVIDERS``.
    Native Anthropic, Google and Bedrock clients are not shipped: they need
    packages this project does not depend on; reach those models through
    OpenRouter or 9Router instead.
    """
    provider_lower = provider.lower()
    if provider_lower == "router9_chain":
        from .fallback_client import FallbackClient

        return FallbackClient(model, base_url, **kwargs)
    if provider_lower == "azure":
        from .azure_client import AzureOpenAIClient

        return AzureOpenAIClient(model, base_url, **kwargs)
    from .openai_client import OpenAIClient, is_openai_compatible

    if is_openai_compatible(provider_lower):
        return OpenAIClient(model, base_url, provider=provider_lower, **kwargs)
    raise ValueError(f"Unsupported LLM provider: {provider}")


def _non_negative_int(value, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, not a boolean: {value!r}")
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc
    if n < 0:
        raise ValueError(f"{name} must be >= 0, got {n}")
    return n


def build_llm_kwargs(config: dict) -> dict[str, Any]:
    """Keyword arguments for ``create_llm_client`` from a config."""
    kwargs: dict[str, Any] = {}
    if str(config.get("llm_provider", "")).lower() == "openai" and config.get("openai_reasoning_effort"):
        kwargs["reasoning_effort"] = config["openai_reasoning_effort"]
    if config.get("temperature") not in (None, ""):
        kwargs["temperature"] = float(config["temperature"])
    if config.get("llm_max_retries") not in (None, ""):
        kwargs["max_retries"] = _non_negative_int(config["llm_max_retries"], "llm_max_retries")
    if config.get("max_tokens") not in (None, ""):
        n = _non_negative_int(config["max_tokens"], "max_tokens")
        if n == 0:
            raise ValueError("max_tokens must be > 0")
        kwargs["max_tokens"] = n
    return kwargs


def tier_provider(config: dict, tier: str) -> str:
    """The provider of the ``quick`` or ``deep`` tier: its own, else ``llm_provider``."""
    return str(config.get(f"{tier}_think_provider") or config["llm_provider"]).lower()


def create_tier_client(config: dict, tier: str, **extra) -> BaseLLMClient:
    """The client of the ``quick`` or ``deep`` tier; ``backend_url`` belongs to ``llm_provider``."""
    provider = tier_provider(config, tier)
    if provider == str(config["llm_provider"]).lower():
        base_url = config.get(f"{tier}_think_backend_url") or config.get("backend_url")
    else:
        base_url = config.get(f"{tier}_think_backend_url")
    kwargs = build_llm_kwargs({**config, "llm_provider": provider})
    model = config[f"{tier}_think_llm"]
    if provider != "router9_chain" and str(model or "default").strip().lower() == "default":
        # "default" lets each provider of the chain use its own *_MODEL; another
        # provider would be sent the model name "default" and answer 404 on every call.
        raise ValueError(f"Provider {provider!r} needs a model name: set CRYPTO_AGENTS_{tier.upper()}_THINK_LLM "
                         f"(or {tier}_think_llm in the config); 'default' only works with router9_chain.")
    return create_llm_client(provider=provider, model=model, base_url=base_url, **kwargs, **extra)
