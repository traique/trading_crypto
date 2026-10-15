"""The optimiser's fast simulator gives the same NAV as the real futures backtest (same rule)."""

import numpy as np
import pandas as pd
import pytest

from cryptotrader.futures import LongShortSma, backtest
from research import optimize_futures as opt


def _write(tmp_path, monkeypatch, coin, closes, funding=0.0003):
    rng = np.random.default_rng(len(coin))
    closes = np.asarray(closes, float)
    opens = closes * (1 + rng.normal(0, 0.005, len(closes)))
    f = pd.DataFrame({"Date": pd.date_range("2020-01-01", periods=len(closes), freq="D"),
                      "Open": opens, "High": np.maximum(opens, closes) * 1.02,
                      "Low": np.minimum(opens, closes) * 0.98, "Close": closes, "Volume": 1.0,
                      "funding": funding})
    monkeypatch.setattr(opt, "DATA", tmp_path)
    f.to_csv(tmp_path / f"{coin}USDT.csv", index=False)
    return f


@pytest.mark.parametrize("rule", [opt.Rule(), opt.Rule(stop_atr=2.0, band=0.1, max_leverage=3.0),
                                  opt.Rule(long_min=5, short_max=2, target_vol=0.8, stop_atr=0.0),
                                  opt.Rule("fast", 6, 2, 0.3, 2.0, 4.0, 0.25, True)])
def test_fast_simulator_matches_the_futures_backtest(tmp_path, monkeypatch, rule):
    rng = np.random.default_rng(7)
    t = np.arange(900)
    closes = 2000 * np.exp(0.6 * np.sin(t / 90) + np.cumsum(rng.normal(0, 0.03, 900)))
    f = _write(tmp_path, monkeypatch, "ETH", closes)
    btc_f = _write(tmp_path, monkeypatch, "BTC", 30_000 * np.exp(0.5 * np.sin(t / 70 + 0.5)))
    d, btc = opt.prepare("ETH"), opt.prepare("BTC")
    navs, dates, fills = opt.simulate(d, rule, "ETHUSDT", capital=1000.0, start="2020-08-01", btc=btc)
    strategy = LongShortSma(lookbacks=opt.SLOW if rule.lookbacks == "slow" else opt.FAST,
                            long_min=rule.long_min, short_max=rule.short_max, target_vol=rule.target_vol,
                            max_leverage=rule.max_leverage, stop_atr=rule.stop_atr, band=rule.band,
                            btc_filter=rule.btc_filter)
    book = backtest(f, strategy, 1000.0, start="2020-08-01", symbol="ETHUSDT", btc=btc_f)
    ref = np.array([h["nav"] for h in book.history])
    assert fills > 10 and len(navs) == len(ref)
    assert np.allclose(navs, ref, rtol=1e-6, atol=1e-6)
