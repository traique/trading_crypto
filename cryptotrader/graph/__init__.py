"""The multi-agent analysis graph. ``analyze`` is the bot's entry point; ``CryptoTradingGraph`` the full API.

Importing this package needs no API key: models are built when a graph is.
"""

from .conditional_logic import ConditionalLogic
from .propagation import Propagator
from .setup import GraphSetup
from .trading_graph import CryptoTradingGraph, analyze

__all__ = ["ConditionalLogic", "CryptoTradingGraph", "GraphSetup", "Propagator", "analyze"]
