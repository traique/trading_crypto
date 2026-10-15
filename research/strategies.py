"""Baseline strategies of the harness, and strategies by name for the command line.

None of these is tuned; they are the yardsticks a researched strategy must beat
out of sample, after costs.
"""

from __future__ import annotations

import contextlib

import pandas as pd

from cryptotrader.market.rules import BENCHMARK


def _month_end(ctx) -> bool:
    """Whether today is the last day of its month (orders then fill on the 1st)."""
    return (ctx.date + pd.Timedelta(days=1)).month != ctx.date.month


class BuyHold:
    """Put ``weight`` of the NAV in ``symbol`` at the first chance and keep it there."""

    def __init__(self, symbol: str, weight: float = 1.0, name: str | None = None):
        self.symbol, self.weight = symbol, weight
        self.name = name or f"buyhold_{symbol}"
        self.universe_label = "-"

    def on_day(self, ctx):
        if ctx.last_close(self.symbol) is None:
            return None
        return {self.symbol: self.weight}


class EqualWeightMonthly:
    """Equal weight over the point-in-time universe, rebalanced at each month end
    (and on the first day of the run). The ledger resizes held pairs to the new weight."""

    def __init__(self, name: str = "ew_monthly", universe: str | None = None):
        self.name = name
        self.universe = universe
        self.universe_label = universe

    def on_day(self, ctx):
        if ctx.state.get("started") and not _month_end(ctx):
            return None
        ctx.state["started"] = True
        syms = [s for s in ctx.universe(self.universe) if ctx.last_close(s)]
        return dict.fromkeys(syms, 1.0 / len(syms)) if syms else {}


class CrossSectionalMomentum:
    """Naive cross-sectional momentum: at each month end, the ``top`` pairs of the
    universe with the highest ``lookback``-day return, equal weight (no market
    filter, no positive-return requirement)."""

    def __init__(self, top: int = 5, lookback: int = 90, universe: str = "liq_top20", name: str = "xs_momentum"):
        self.top, self.lookback, self.universe, self.name = top, lookback, universe, name
        self.universe_label = universe

    def on_day(self, ctx):
        if ctx.state.get("started") and not _month_end(ctx):
            return None
        ctx.state["started"] = True
        syms = ctx.universe(self.universe)
        if not syms:
            return {}
        px = ctx.history("close", syms, self.lookback + 1)
        if len(px) < self.lookback + 1:
            return {}
        ret = (px.iloc[-1] / px.iloc[0] - 1).dropna()
        picks = list(ret.nlargest(self.top).index)
        return dict.fromkeys(picks, 1.0 / len(picks)) if picks else {}


def live(name: str, **params):
    """A strategy of ``cryptotrader.strategies`` (run exactly as the bot runs it)."""
    from cryptotrader.strategies import get_strategy

    strat = get_strategy(name, **params)
    with contextlib.suppress(AttributeError):
        strat.universe_label = "-"
    return strat


def evaluated_c2():
    """``C2_btc_sma_ens`` in its research form, from the frozen evaluated code."""
    from research.evaluated import trend_majors

    return trend_majors.candidate("C2_btc_sma_ens")


# ── strategies by name (``python -m research run <name>``) ───────────────────
STRATEGIES = {
    "btc_sma_ens": "cryptotrader.strategies.BtcSmaEnsemble: the bot's strategy (the evaluated C2_btc_sma_ens), "
                   "run through the LIVE module exactly as the bot runs it",
    "btc_sma_ens_eval": "C2_btc_sma_ens from the frozen evaluated code (research/evaluated), for parity checks",
    "btc_buy_hold": "cryptotrader.strategies.BtcBuyHold: 100% BTC (the bot's benchmark strategy, with costs)",
    "btc_trend": "cryptotrader.strategies.BtcTrend: BTC above its SMA200, else USDT (former placeholder, "
                 "comparison book)",
    "ew": "equal weight over --universe, rebalanced at each month end",
    "xs_momentum": "top 5 of --universe (default liq_top20) by 90-day return, equal weight, monthly",
    "buyhold:SYM": "buy one pair (e.g. buyhold:ETHUSDT) on the first day and hold it, with costs",
    "price:SYM": "a pair's price as NAV (no costs, no ledger), e.g. price:BTCUSDT",
}


def make(name: str, *, universe: str | None = None, panel=None):
    """A strategy object by name (see ``STRATEGIES``); ``price:SYM`` is handled by the CLI."""
    key = name.strip().lower()
    if key in ("btc_sma_ens", "btc_buy_hold", "btc_trend"):
        return live(key)
    if key == "btc_sma_ens_eval":
        return evaluated_c2()
    if key in ("ew", "ew_monthly"):
        return EqualWeightMonthly(universe=universe)
    if key in ("xs_momentum", "momentum"):
        return CrossSectionalMomentum(universe=universe or "liq_top20")
    if key.startswith("buyhold:"):
        symbol = name.split(":", 1)[1].strip().upper()
        if not symbol:
            raise ValueError("buyhold needs a pair: buyhold:ETHUSDT")
        if panel is not None and symbol not in panel.close.columns:
            raise ValueError(f"unknown pair {symbol!r}: not in the research data "
                             f"(`python -m research download --symbols {symbol}` fetches it)")
        return BuyHold(symbol)
    raise ValueError(f"unknown strategy {name!r}; known: {', '.join(STRATEGIES)}")


__all__ = ["BENCHMARK", "BuyHold", "CrossSectionalMomentum", "EqualWeightMonthly", "STRATEGIES", "evaluated_c2",
           "live", "make"]
