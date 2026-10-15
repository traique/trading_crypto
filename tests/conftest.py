"""Shared fixtures: an isolated data dir, fresh breakers and no network in the default suite."""

from __future__ import annotations

import pytest
import requests

from cryptotrader.data import net
from cryptotrader.llm_clients.rate_gate import GATE


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch, request):
    monkeypatch.setenv("CRYPTO_DATA_DIR", str(tmp_path / "data"))
    # The LLM gate spaces real calls; the scripted models of the tests need no spacing.
    monkeypatch.setenv("LLM_MIN_INTERVAL_SEC", "0")
    monkeypatch.setenv("LLM_MAX_INTERVAL_SEC", "0")
    # The existing graph tests exercise the full analysis (debates, risk team); lite has its own tests.
    monkeypatch.setenv("CRYPTO_AGENTS_PROFILE", "full")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_ANALYSIS", "60")
    net.reset_breakers()
    GATE.reset()
    if request.node.get_closest_marker("integration") is None:
        def _no_network(self, method, url, *args, **kwargs):
            raise AssertionError(f"network access in a unit test: {method} {url}")

        monkeypatch.setattr(requests.Session, "request", _no_network)
    yield
    net.reset_breakers()
    GATE.reset()
