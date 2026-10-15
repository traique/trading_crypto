"""Provider -> API-key environment variable: the one place that says which variable holds which key."""

from __future__ import annotations

PROVIDER_API_KEY_ENV: dict[str, str | None] = {
    "openai": "OPENAI_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "qwen-cn": "DASHSCOPE_CN_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "glm-cn": "ZHIPU_CN_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "groq": "GROQ_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "ollama": None,
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
    # The provider chain reads its own keys (ROUTER9_API_KEY, GROQ_API_KEY, ...): see fallback_client.
    "router9_chain": None,
}


def get_api_key_env(provider: str) -> str | None:
    """The env var of ``provider``'s key, or None (no key, or unknown provider)."""
    return PROVIDER_API_KEY_ENV.get(provider.lower())
