"""The configuration served to the data tools: the run's own when a graph is running, else the process's."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import Context, ContextVar, copy_context
from copy import deepcopy

from cryptotrader import default_config

_config: dict | None = None
# A graph binds its config for the length of a run; LangGraph carries the context into tool calls.
_run_config: ContextVar[dict | None] = ContextVar("crypto_agents_run_config", default=None)


def _merge(base: dict, config: dict) -> dict:
    """``config`` over ``base``: dict values merged one level deep, the rest replaced."""
    for key, value in deepcopy(config).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key].update(value)
        else:
            base[key] = value
    return base


def set_config(config: dict) -> None:
    global _config
    if _config is None:
        _config = default_config.build_default_config()
    _merge(_config, config)


def reset_config() -> None:
    global _config
    _config = None


@contextmanager
def run_config(config: dict):
    """Serve ``config`` (over the defaults) to every read made inside the block."""
    token = _run_config.set(_merge(default_config.build_default_config(), config))
    try:
        yield
    finally:
        _run_config.reset(token)


def run_config_context(config: dict) -> Context:
    """A context serving ``config`` to every read run in it (for a run that yields between steps)."""
    context = copy_context()
    context.run(_run_config.set, _merge(default_config.build_default_config(), config))
    return context


def get_config() -> dict:
    scoped = _run_config.get()
    if scoped is not None:
        return deepcopy(scoped)
    if _config is None:
        return default_config.build_default_config()
    return deepcopy(_config)
