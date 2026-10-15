"""Shared fixtures: an isolated data dir, fresh breakers and no network in the default suite."""

from __future__ import annotations

import pytest
import requests

from cryptotrader.data import net


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch, request):
    monkeypatch.setenv("CRYPTO_DATA_DIR", str(tmp_path / "data"))
    net.reset_breakers()
    if request.node.get_closest_marker("integration") is None:
        def _no_network(self, method, url, *args, **kwargs):
            raise AssertionError(f"network access in a unit test: {method} {url}")

        monkeypatch.setattr(requests.Session, "request", _no_network)
    yield
    net.reset_breakers()
