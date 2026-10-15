"""USDⓈ-M perpetual futures: long/short paper book, strategy, daily step and backtest."""

from cryptotrader.futures.book import Bar, FuturesBook
from cryptotrader.futures.rules import FuturesSpec, spec_for
from cryptotrader.futures.runner import AccountPolicy, backtest, step, summary
from cryptotrader.futures.strategy import LongShortSma
