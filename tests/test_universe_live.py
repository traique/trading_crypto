"""The live liquid universe: point-in-time ranking, daily cache, and a loud failure instead of a stale universe."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from cryptotrader.data import universe as U
from cryptotrader.data.errors import NoMarketDataError, VendorUnavailableError

pytestmark = pytest.mark.unit

DAY = "2026-10-14"


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CRYPTO_DATA_DIR", str(tmp_path / "data"))


def info_item(symbol, status="TRADING", spot=True):
    return SimpleNamespace(symbol=symbol, base=symbol[:-4], quote=symbol[-4:], status=status, spot=spot,
                           trading=status == "TRADING" and spot)


def frame(days: int, qv: float, end: str = DAY, zero_tail: int = 0) -> pd.DataFrame:
    dates = pd.date_range(end=end, periods=days, freq="D")
    vol = np.full(days, 10.0)
    if zero_tail:
        vol[-zero_tail:] = 0.0
    return pd.DataFrame({"Date": dates, "Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": vol,
                         "QuoteVolume": np.full(days, float(qv)), "Trades": 1.0})


class Loader:
    def __init__(self, frames):
        self.frames, self.calls = frames, []

    def __call__(self, symbol, end_date=None, *a, **k):
        self.calls.append((symbol, str(end_date)))
        value = self.frames[symbol]
        if isinstance(value, Exception):
            raise value
        return value[value["Date"] <= pd.Timestamp(end_date)].reset_index(drop=True)


def market():
    info = {s: info_item(s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "NEWUSDT", "USDCUSDT", "OLDUSDT",
                                       "HALTUSDT")}
    info["BREAKUSDT"] = info_item("BREAKUSDT", status="BREAK")
    frames = {"BTCUSDT": frame(400, 1e9), "ETHUSDT": frame(400, 5e8), "SOLUSDT": frame(400, 2e8),
              "NEWUSDT": frame(100, 9e9),                       # too young
              "USDCUSDT": frame(400, 9e9),                      # a stablecoin: excluded by rule
              "OLDUSDT": NoMarketDataError("OLDUSDT", "ngừng giao dịch"),
              "HALTUSDT": frame(400, 9e9, zero_tail=5),        # no trading in the last 3 days
              "BREAKUSDT": frame(400, 9e9)}
    return info, Loader(frames)


def test_the_most_liquid_eligible_pairs_first():
    info, loader = market()
    assert U.liquid_universe(DAY, 3, info=info, loader=loader) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert all(end == DAY for _, end in loader.calls)                # nothing after the day is read
    assert "BREAKUSDT" not in {s for s, _ in loader.calls}           # not trading: not even loaded


def test_days_without_trading_count_zero_in_the_median():
    info = {s: info_item(s) for s in ("AAAUSDT", "BBBUSDT")}
    a = frame(400, 1e6)
    a.loc[a.index[-16:-1], "Volume"] = 0.0                         # 15 of the last 30 days without trading
    loader = Loader({"AAAUSDT": a, "BBBUSDT": frame(400, 6e5)})     # AAA's median: (0 + 1e6) / 2
    assert U.liquid_universe(DAY, 2, info=info, loader=loader) == ["BBBUSDT", "AAAUSDT"]


def test_a_source_that_does_not_answer_fails_the_universe_loudly():
    info, loader = market()
    loader.frames["SOLUSDT"] = VendorUnavailableError("down")
    with pytest.raises(U.UniverseUnavailable, match="SOLUSDT"):
        U.liquid_universe(DAY, 3, info=info, loader=loader)
    assert not U.cache_path(DAY, 3).exists()                         # nothing partial is kept


def test_too_few_eligible_pairs_fail_rather_than_shrink():
    info, loader = market()
    with pytest.raises(U.UniverseUnavailable, match="chỉ 3 cặp"):
        U.liquid_universe(DAY, 5, info=info, loader=loader)


def test_cached_for_the_same_day_only():
    info, loader = market()
    first = U.liquid_universe(DAY, 3, info=info, loader=loader)
    n = len(loader.calls)
    assert U.liquid_universe(DAY, 3, info=info, loader=loader) == first and len(loader.calls) == n
    U.liquid_universe("2026-10-13", 3, info=info, loader=loader)     # another day: built, not reused
    assert len(loader.calls) > n and U.cache_path("2026-10-13", 3).exists()


def test_stale_or_missing_exchange_info_is_refused(monkeypatch):
    _, loader = market()
    monkeypatch.setattr(U, "_exchange_info", lambda: ({}, float("inf")))
    with pytest.raises(U.UniverseUnavailable, match="exchangeInfo"):
        U.liquid_universe(DAY, 3, loader=loader)
    info, _ = market()
    monkeypatch.setattr(U, "_exchange_info", lambda: (info, 5 * 86_400))
    with pytest.raises(U.UniverseUnavailable, match="cũ"):
        U.liquid_universe(DAY, 3, loader=loader)
    monkeypatch.setattr(U, "_exchange_info", lambda: (info, 3_600))
    assert U.liquid_universe(DAY, 3, loader=loader) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_the_bot_retries_a_run_whose_universe_is_unavailable(settings, market_fixture, monkeypatch):
    """A strategy with ``universe(day)`` that cannot be built: the run raises, nothing is saved."""
    from bot import books
    from bot.books import BookSpec
    from bot.trader import TradingBot
    from cryptotrader import strategies

    class Basket:
        name, symbols, warmup_days = "basket", (), 0

        def universe(self, day):
            raise U.UniverseUnavailable("nguồn giá không trả lời")

        def decide(self, ctx):
            return {}

    monkeypatch.setitem(strategies.REGISTRY, "basket", Basket)
    monkeypatch.setattr(books, "BOOKS", (BookSpec("basket", "Rổ"),))
    bot = TradingBot(settings)
    with pytest.raises(U.UniverseUnavailable):
        bot.catch_up("2026-10-14")
    assert bot.last_day() == ""


from tests import test_bot_support as support  # noqa: E402

settings, at = support.settings, support.at
market_fixture = support.market
