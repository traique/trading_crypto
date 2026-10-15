"""What a strategy is: a point-in-time decision from closed candles.

A strategy sees ONLY candles up to and including the decision day (its close
is known at 00:00 UTC of the next day); ``StrategyContext`` trims every frame
it is given, so a caller that passes a longer history cannot leak the future.
The decision is either target weights (fraction of NAV per pair, the rest in
USDT) or explicit orders; the ledger turns weights into orders that fill at the
next candle's open.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import pandas as pd

from cryptotrader.trading.ledger import Order

# {pair: fraction of NAV}; missing pairs are 0, the rest of the NAV is cash.
TargetWeights = dict[str, float]
Decision = TargetWeights | list[Order]


@dataclass
class StrategyContext:
    """Everything a strategy may look at on decision day ``date`` (YYYY-MM-DD, UTC)."""

    date: str
    history: Mapping[str, pd.DataFrame]          # closed daily candles per pair (klines.COLUMNS)
    weights: Mapping[str, float] = field(default_factory=dict)   # current weights of the book
    nav: float = 0.0
    cash: float = 0.0
    state: dict = field(default_factory=dict)    # the strategy's own memory between days

    def __post_init__(self) -> None:
        cutoff = pd.Timestamp(self.date).normalize()
        trimmed = {}
        for symbol, frame in self.history.items():
            if frame is None or frame.empty:
                continue
            trimmed[symbol] = frame[frame["Date"] <= cutoff].reset_index(drop=True)
        self.history = trimmed

    def closes(self, symbol: str) -> pd.Series:
        """Closing prices of ``symbol`` through ``date``, indexed by day (empty when unknown)."""
        frame = self.history.get(symbol)
        if frame is None or frame.empty:
            return pd.Series(dtype="float64")
        return pd.Series(frame["Close"].to_numpy(), index=pd.DatetimeIndex(frame["Date"]), name=symbol)

    def last_close(self, symbol: str) -> float | None:
        closes = self.closes(symbol)
        if closes.empty or pd.Timestamp(closes.index[-1]) != pd.Timestamp(self.date).normalize():
            return None                           # no candle on the decision day itself
        return float(closes.iloc[-1])


@runtime_checkable
class Strategy(Protocol):
    """A strategy: a name, the pairs it needs, how much history, and a decision."""

    name: str
    symbols: tuple[str, ...]
    warmup_days: int

    def decide(self, ctx: StrategyContext) -> Decision:
        ...
