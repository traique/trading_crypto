"""Baseline strategies: the yardsticks the bot's strategy is compared with, after costs.

``btc_trend`` was the placeholder before the evaluation; it is kept as a
comparison book. On the evaluator's TEST period (2024-01-01..2026-10-14) it made
CAGR 19.7%, max drawdown -32.3%, Sharpe 0.68 after costs, below BTC's Sharpe
(0.70), and its TRAIN drawdown (-64.7%) failed the protocol's -50% limit
(``research/RESULTS.md``).
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptotrader.market.rules import BENCHMARK
from cryptotrader.strategies.base import Decision, StrategyContext


@dataclass
class BtcBuyHold:
    """Hold BTC with the whole book, always (the benchmark)."""

    name: str = "btc_buy_hold"
    symbols: tuple[str, ...] = (BENCHMARK,)
    warmup_days: int = 0

    def decide(self, ctx: StrategyContext) -> Decision:
        if ctx.last_close(BENCHMARK) is None:
            return dict(ctx.weights)           # no candle today: change nothing
        return {BENCHMARK: 1.0}


@dataclass
class BtcTrend:
    """Hold BTC while its close is above its ``sma_days``-day simple moving average, else cash.

    The former placeholder (a classic trend filter, no parameter tuned on this
    project's data), now a comparison book. With fewer than ``sma_days`` closes
    it stays in cash.
    """

    sma_days: int = 200
    name: str = "btc_trend"
    symbols: tuple[str, ...] = (BENCHMARK,)

    @property
    def warmup_days(self) -> int:
        return self.sma_days

    def decide(self, ctx: StrategyContext) -> Decision:
        closes = ctx.closes(BENCHMARK)
        if ctx.last_close(BENCHMARK) is None:
            return dict(ctx.weights)
        if len(closes) < self.sma_days:
            return {}
        sma = float(closes.iloc[-self.sma_days:].mean())
        return {BENCHMARK: 1.0} if float(closes.iloc[-1]) > sma else {}
