"""The paper books the bot trades: one per strategy of ``cryptotrader.strategies``.

Each book is a ``BookSpec``: the strategy's registry name, a Vietnamese title
and a one-line note for the report. The first book is the evaluated strategy
(``btc_sma_ens``, the default for real-account signals); the others are
comparison books. Adding a strategy of ``cryptotrader.strategies.REGISTRY`` is
ONE line in ``BOOKS``; it then gets its own paper book, a line on the NAV chart and can be
chosen for the real-account signals with ``SIGNAL_STRATEGY=<name>``.

A strategy listed here but missing from the registry is skipped with a warning,
so the bot still starts while another part of the project is being written.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_NAME = re.compile(r"^[a-z0-9_]{1,40}$")


@dataclass(frozen=True)
class BookSpec:
    name: str                    # key in cryptotrader.strategies.REGISTRY (also the book's file name)
    title: str                   # shown in reports and on the web page
    note: str = ""               # what it does and how far it is tested (honest, one line)
    params: tuple = ()           # (key, value) pairs passed to the strategy's constructor
    benchmark: bool = False      # a yardstick, never the default for real signals

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"book name must be lowercase letters, digits or _: {self.name!r}")

    def strategy(self):
        from cryptotrader.strategies import get_strategy

        return get_strategy(self.name, **dict(self.params))

    @property
    def file_name(self) -> str:
        return f"paper_{self.name}.json"


# Report order. One line per book.
BOOKS: tuple[BookSpec, ...] = (
    BookSpec("btc_sma_ens", "BTC theo 7 đường SMA",
             "Chiến lược chính (đã kiểm định ngoài mẫu): tỷ trọng BTC = số đường SMA 50..200 ngày mà giá "
             "nằm trên, chia 7; phần còn lại giữ USDT. TEST 2024-2026 sau phí: CAGR 25,4%, sụt giảm tối đa "
             "-29,6% (BTC: 24,7%, -53,0%) - không phải lời hứa lợi nhuận."),
    BookSpec("btc_trend", "Xu hướng BTC (SMA200)",
             "Mốc so sánh: giữ BTC khi giá trên SMA200, ngược lại USDT (chiến lược giữ chỗ cũ, không qua kiểm định).",
             benchmark=True),
    BookSpec("btc_buy_hold", "Mua & giữ BTC", "Mốc so sánh: luôn giữ BTC bằng toàn bộ vốn.", benchmark=True),
)

DEFAULT_SIGNAL_STRATEGY = "btc_sma_ens"


def active_books() -> list[BookSpec]:
    """The books whose strategy exists in the registry, in report order."""
    from cryptotrader.strategies import REGISTRY

    out = []
    for spec in BOOKS:
        if spec.name in REGISTRY:
            out.append(spec)
        else:
            logger.warning("Book %s skipped: no strategy %r in cryptotrader.strategies.REGISTRY",
                           spec.title, spec.name)
    return out


def book_names() -> tuple[str, ...]:
    return tuple(spec.name for spec in BOOKS)


def get_book(name: str) -> BookSpec | None:
    return next((spec for spec in BOOKS if spec.name == name), None)


def title_of(name: str | None) -> str:
    spec = get_book(name or "")
    return spec.title if spec else (name or "")
