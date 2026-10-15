"""Strategies: point-in-time decisions (target weights or orders) from closed candles.

``btc_sma_ens`` is the strategy chosen by the independent out-of-sample
evaluation (``research/RESULTS.md``) and the bot's default; ``btc_trend`` (the
old SMA200 placeholder) and ``btc_buy_hold`` stay as comparison books.
"""

from cryptotrader.strategies.base import Decision, Strategy, StrategyContext, TargetWeights
from cryptotrader.strategies.baseline import BtcBuyHold, BtcTrend
from cryptotrader.strategies.sma_ensemble import BtcSmaEnsemble

DEFAULT_STRATEGY = "btc_sma_ens"

REGISTRY = {
    "btc_sma_ens": BtcSmaEnsemble,
    "btc_buy_hold": BtcBuyHold,
    "btc_trend": BtcTrend,
}


def get_strategy(name: str, **params) -> Strategy:
    """A strategy by registry name; KeyError for an unknown one."""
    return REGISTRY[name](**params)
