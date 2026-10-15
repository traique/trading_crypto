"""Data-source error taxonomy: callers react to the kind of failure, not to the source.

    VendorError
    ├── NoMarketDataError        the source answered, but has no usable rows (empty, stale)
    │   └── UnknownSymbolError   the source does not know the pair (e.g. delisted from REST)
    ├── VendorUnavailableError   the source did not answer (timeout, 5xx, 429, 451, breaker open)
    └── ArchiveChecksumError     a downloaded archive does not match its published checksum
"""

from __future__ import annotations


class VendorError(Exception):
    """Base for any condition where a source could not return usable data."""


class NoMarketDataError(VendorError):
    """A source answered but returned no usable rows for ``symbol``."""

    def __init__(self, symbol: str, detail: str = ""):
        self.symbol = symbol
        self.detail = detail
        super().__init__(f"No market data for {symbol!r}" + (f": {detail}" if detail else ""))


class UnknownSymbolError(NoMarketDataError):
    """The source does not list the pair (Binance REST answers -1121 for a delisted pair)."""


class VendorUnavailableError(VendorError):
    """A source could not answer (network, timeout, throttling, outage, open breaker)."""


class ArchiveChecksumError(VendorError):
    """An archive file's SHA-256 differs from its .CHECKSUM file; the file is not used."""
