import pytest
import requests

from cryptotrader.data import net
from cryptotrader.data.errors import NoMarketDataError, VendorUnavailableError


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestBreaker:
    def test_opens_after_threshold_and_half_opens_after_cooldown(self):
        clock = Clock()
        b = net.CircuitBreaker("x", failure_threshold=2, cooldown=60, clock=clock)
        fail = lambda: (_ for _ in ()).throw(VendorUnavailableError("down"))  # noqa: E731
        for _ in range(2):
            with pytest.raises(VendorUnavailableError):
                b.call(fail)
        assert b.state == "open"
        calls = []
        with pytest.raises(VendorUnavailableError, match="tạm bỏ qua"):
            b.call(lambda: calls.append(1))
        assert calls == []                              # not even attempted while open
        clock.t += 61
        assert b.state == "half_open"
        assert b.allow() is True                        # the one trial call
        assert b.allow() is False                       # others wait for its result
        b.record_success()
        assert b.state == "closed"

    def test_failed_trial_reopens(self):
        clock = Clock()
        b = net.CircuitBreaker("x", failure_threshold=1, cooldown=10, clock=clock)
        b.record_failure()
        clock.t += 11
        with pytest.raises(VendorUnavailableError):
            b.call(lambda: (_ for _ in ()).throw(VendorUnavailableError("still down")))
        assert b.state == "open"

    def test_an_answer_without_data_is_not_a_failure(self):
        b = net.CircuitBreaker("x", failure_threshold=1)

        def no_data():
            raise NoMarketDataError("ABCUSDT")

        with pytest.raises(NoMarketDataError):
            b.call(no_data)
        assert b.state == "closed"

    def test_registry(self):
        assert net.breaker("a") is net.breaker("a")
        net.reset_breakers()


class FakeResponse:
    def __init__(self, status, chunks=(b"{}",), headers=None, delay=None):
        self.status_code = status
        self._chunks = chunks
        self.headers = headers or {}
        self.url = "https://example.test/x"
        self._delay = delay

    def iter_content(self, chunk_size):
        for chunk in self._chunks:
            if self._delay:
                self._delay()
            yield chunk

    def close(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, stream=None, timeout=None):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def use(monkeypatch, session):
    monkeypatch.setattr(net, "_session", lambda: session)
    monkeypatch.setattr(net.time, "sleep", lambda s: None)


def test_ok_and_json(monkeypatch):
    use(monkeypatch, FakeSession([FakeResponse(200, (b'{"a":', b" 1}"))]))
    assert net.http_get("https://example.test/x").json() == {"a": 1}


def test_retries_throttling_then_succeeds(monkeypatch):
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(200)])
    use(monkeypatch, session)
    assert net.http_get("https://example.test/x").status == 200
    assert session.calls == 2


def test_geo_block_is_unavailable_without_retry(monkeypatch):
    session = FakeSession([FakeResponse(451), FakeResponse(200)])
    use(monkeypatch, session)
    with pytest.raises(VendorUnavailableError, match="451"):
        net.http_get("https://example.test/x")
    assert session.calls == 1


def test_allowed_status_is_returned(monkeypatch):
    use(monkeypatch, FakeSession([FakeResponse(404, (b"nope",))]))
    assert net.http_get("https://example.test/x", allow_status=(404,)).status == 404


def test_network_errors_become_unavailable(monkeypatch):
    use(monkeypatch, FakeSession([requests.ConnectionError("boom"), requests.Timeout("slow")]))
    with pytest.raises(VendorUnavailableError, match="Timeout"):
        net.http_get("https://example.test/x")


def test_a_dripping_body_is_cut_at_the_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(net.time, "monotonic", lambda: now[0])

    def tick():
        now[0] += 4.0

    use(monkeypatch, FakeSession([FakeResponse(200, chunks=[b"x"] * 100, delay=tick)]))
    monkeypatch.setattr(net.time, "monotonic", lambda: now[0])
    with pytest.raises(VendorUnavailableError, match="quá thời hạn"):
        net.http_get("https://example.test/x", deadline=10, retries=0)


def test_body_size_cap(monkeypatch):
    use(monkeypatch, FakeSession([FakeResponse(200, chunks=[b"x" * 10] * 5)]))
    with pytest.raises(VendorUnavailableError, match="quá lớn"):
        net.http_get("https://example.test/x", max_bytes=20, retries=0)
