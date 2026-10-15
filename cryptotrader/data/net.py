"""HTTP for the data sources: hard deadlines, bounded retries and a per-source circuit breaker.

A source that hangs must not freeze the bot. ``http_get`` bounds every request
three ways: a connect timeout, a read timeout per socket operation, and a total
``deadline`` for the whole body (a server that drips bytes forever is cut off).
A source that keeps failing is skipped for a while by its ``CircuitBreaker``
instead of being waited on again on every call.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

import requests

from cryptotrader import __version__
from cryptotrader.data.errors import VendorUnavailableError

logger = logging.getLogger(__name__)

T = TypeVar("T")

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0
DEADLINE_SECONDS = 30.0
MAX_BODY_BYTES = 64 * 1024 * 1024
RETRY_STATUSES = frozenset({418, 429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 5.0
USER_AGENT = f"trading-crypto/{__version__} (+research; python-requests)"


# ── circuit breaker ──
class CircuitBreaker:
    """Skip a source after ``failure_threshold`` failures in a row, for ``cooldown`` seconds.

    After the cooldown one trial call is let through (half-open); while it runs
    other callers are still refused. Its success closes the breaker, its failure
    opens it for another cooldown. Thread-safe.
    """

    def __init__(self, name: str, failure_threshold: int = 2, cooldown: float = 300.0,
                 clock: Callable[[], float] = time.monotonic):
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown = float(cooldown)
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0

    @property
    def state(self) -> str:
        with self._lock:
            if self._failures < self.failure_threshold:
                return "closed"
            return "open" if self._clock() < self._open_until else "half_open"

    def allow(self) -> bool:
        """Whether a call may go out now (a half-open breaker lets exactly one through)."""
        with self._lock:
            if self._failures < self.failure_threshold:
                return True
            now = self._clock()
            if now < self._open_until:
                return False
            self._open_until = now + self.cooldown     # the trial call; others wait for its result
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._open_until = self._clock() + self.cooldown

    def reset(self) -> None:
        self.record_success()

    def call(self, fn: Callable[..., T], *args, **kwargs) -> T:
        """``fn(*args, **kwargs)`` through the breaker.

        Only ``VendorUnavailableError`` (the source did not answer) counts as a
        failure; any other outcome, an empty answer included, means the source is up.
        """
        if not self.allow():
            raise VendorUnavailableError(f"{self.name}: lỗi liên tiếp gần đây, tạm bỏ qua nguồn này")
        try:
            result = fn(*args, **kwargs)
        except VendorUnavailableError:
            self.record_failure()
            raise
        except Exception:
            self.record_success()
            raise
        self.record_success()
        return result


_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def breaker(name: str) -> CircuitBreaker:
    """The process-wide breaker of source ``name``."""
    with _breakers_lock:
        found = _breakers.get(name)
        if found is None:
            found = _breakers[name] = CircuitBreaker(name)
        return found


def reset_breakers() -> None:
    with _breakers_lock:
        _breakers.clear()


# ── HTTP ──
@dataclass
class HttpResult:
    status: int
    content: bytes
    url: str
    headers: dict = field(default_factory=dict)

    def json(self):
        try:
            return json.loads(self.content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise VendorUnavailableError(f"{self.url}: phản hồi không phải JSON ({exc})") from None

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


_local = threading.local()


def _session() -> requests.Session:
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        _local.session = session
    return session


def _retry_after(response: requests.Response, attempt: int) -> float:
    raw = response.headers.get("Retry-After") if response is not None else None
    try:
        wait = float(raw) if raw is not None else 0.5 * (2 ** attempt)
    except ValueError:
        wait = 0.5 * (2 ** attempt)
    return max(0.0, min(wait, MAX_BACKOFF_SECONDS))


def http_get(url: str, params: dict | None = None, *, allow_status: tuple[int, ...] = (),
             retries: int = 1, deadline: float = DEADLINE_SECONDS,
             timeout: tuple[float, float] = (CONNECT_TIMEOUT, READ_TIMEOUT),
             max_bytes: int = MAX_BODY_BYTES) -> HttpResult:
    """GET ``url`` within ``deadline`` seconds in total, retrying throttling and 5xx answers.

    Returns the result for a 2xx status or one in ``allow_status`` (e.g. 404 for
    an archive file that does not exist). Raises ``VendorUnavailableError`` for
    anything else: network errors, timeouts, the deadline, 451/403 (blocked
    region), throttling or 5xx after the retries.
    """
    start = time.monotonic()
    attempt = 0
    while True:
        remaining = deadline - (time.monotonic() - start)
        if remaining <= 0:
            raise VendorUnavailableError(f"{url}: quá thời hạn {deadline:.0f}s")
        response = None
        try:
            response = _session().get(url, params=params, stream=True,
                                      timeout=(min(timeout[0], remaining), min(timeout[1], remaining)))
            status = response.status_code
            if status < 400 or status in allow_status:
                body = _read_body(response, start, deadline, max_bytes, url)
                return HttpResult(status, body, response.url, dict(response.headers))
            if status in RETRY_STATUSES and attempt < retries:
                wait = _retry_after(response, attempt)
                if time.monotonic() - start + wait < deadline:
                    logger.info("%s answered %s; retrying in %.1fs", url, status, wait)
                    time.sleep(wait)
                    attempt += 1
                    continue
            raise VendorUnavailableError(f"{url}: HTTP {status}")
        except requests.RequestException as exc:
            if attempt < retries and time.monotonic() - start + 0.5 < deadline:
                time.sleep(0.5)
                attempt += 1
                continue
            raise VendorUnavailableError(f"{url}: {type(exc).__name__}: {exc}") from None
        finally:
            if response is not None:
                response.close()


def _read_body(response: requests.Response, start: float, deadline: float, max_bytes: int,
               url: str) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=65536):
        if time.monotonic() - start > deadline:
            raise VendorUnavailableError(f"{url}: quá thời hạn {deadline:.0f}s khi đang tải")
        size += len(chunk)
        if size > max_bytes:
            raise VendorUnavailableError(f"{url}: phản hồi quá lớn (> {max_bytes} byte)")
        chunks.append(chunk)
    return b"".join(chunks)
