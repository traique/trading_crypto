"""The client factory: providers that need no extra package, tier settings, config validation."""

from __future__ import annotations

import pytest

from cryptotrader.default_config import build_default_config
from cryptotrader.llm_clients import (
    build_llm_kwargs,
    create_llm_client,
    create_tier_client,
    tier_provider,
)
from cryptotrader.llm_clients.fallback_client import FallbackClient

pytestmark = pytest.mark.unit


def test_the_default_provider_is_the_chain():
    cfg = build_default_config()
    assert cfg["llm_provider"] == "router9_chain"
    assert isinstance(create_tier_client(cfg, "quick"), FallbackClient)


@pytest.mark.parametrize("provider", ["anthropic", "google", "bedrock", "nonsense"])
def test_providers_needing_other_packages_are_refused_clearly(provider):
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        create_llm_client(provider, "m")


def test_an_openai_compatible_provider_builds_without_network(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    llm = create_llm_client("groq", "llama-3.3-70b-versatile", temperature=0.2).get_llm()
    assert llm.model_name == "llama-3.3-70b-versatile"
    assert str(llm.openai_api_base) == "https://api.groq.com/openai/v1"


def test_a_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        create_llm_client("openrouter", "x/y").get_llm()


def test_kwargs_and_tiers():
    cfg = {"llm_provider": "router9_chain", "temperature": "0.3", "llm_max_retries": "2", "max_tokens": 900,
           "deep_think_provider": "OpenRouter", "quick_think_llm": "a", "deep_think_llm": "b"}
    assert build_llm_kwargs(cfg) == {"temperature": 0.3, "max_retries": 2, "max_tokens": 900}
    assert tier_provider(cfg, "deep") == "openrouter" and tier_provider(cfg, "quick") == "router9_chain"
    with pytest.raises(ValueError):
        build_llm_kwargs({"llm_max_retries": True})
    with pytest.raises(ValueError):
        build_llm_kwargs({"max_tokens": 0})


def test_env_overrides_are_typed_and_strict(monkeypatch):
    monkeypatch.setenv("CRYPTO_AGENTS_MAX_DEBATE_ROUNDS", "2")
    monkeypatch.setenv("CRYPTO_ENGINE_STRATEGY", "btc_buy_hold")
    cfg = build_default_config()
    assert cfg["max_debate_rounds"] == 2 and cfg["engine_strategy"] == "btc_buy_hold"
    monkeypatch.setenv("CRYPTO_AGENTS_MAX_TOOL_ROUNDS", "many")
    with pytest.raises(ValueError, match="CRYPTO_AGENTS_MAX_TOOL_ROUNDS"):
        build_default_config()


def test_the_results_dir_follows_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CRYPTO_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.delenv("CRYPTO_AGENTS_HOME", raising=False)
    assert build_default_config()["results_dir"] == str(tmp_path / "d" / "agents" / "results")
    assert not (tmp_path / "d").exists()                  # reading the config creates nothing


def test_a_non_chain_provider_refuses_the_default_model_name(monkeypatch):
    import pytest

    monkeypatch.setenv("CRYPTO_AGENTS_LLM_PROVIDER", "openai")
    monkeypatch.delenv("CRYPTO_AGENTS_QUICK_THINK_LLM", raising=False)
    cfg = build_default_config()
    with pytest.raises(ValueError, match="CRYPTO_AGENTS_QUICK_THINK_LLM"):
        create_tier_client(cfg, "quick")
    monkeypatch.setenv("CRYPTO_AGENTS_QUICK_THINK_LLM", "gpt-4o-mini")
    assert create_tier_client(build_default_config(), "quick").model == "gpt-4o-mini"
