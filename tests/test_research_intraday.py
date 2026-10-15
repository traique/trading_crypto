"""The MT5-style backtester: fills at the next open, worst-case order inside a bar, liquidation."""

import numpy as np
import pandas as pd

from research import intraday_study as st


def _frame(rows, start="2024-01-01"):
    f = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"])
    f["funding"] = 0.0
    f.index = pd.date_range(start, periods=len(f), freq="h")
    return f


def _plan(n, entry_at, direction=1, stop=10.0, target=np.nan):
    entry = np.zeros(n, int)
    entry[entry_at] = direction
    none = np.zeros(n, bool)
    return st.Plan(entry, none, none, np.full(n, stop), np.full(n, target), 0)


def test_entry_fills_at_the_next_open_and_the_stop_comes_before_the_target():
    f = _frame([(100, 101, 99, 100), (100, 101, 99, 100), (100, 125, 85, 100)])
    r = st.run_single(f, _plan(3, 0, stop=10.0, target=20.0), "ETHUSDT", 1000.0, f.index[0], f.index[-1])
    assert len(r.trades) == 1 and r.trades[0] < 0          # bar 2 touches both: the stop is assumed first


def test_a_gap_through_the_stop_fills_at_the_open():
    f = _frame([(100, 101, 99, 100), (100, 101, 99, 100), (80, 81, 79, 80)])
    r = st.run_single(f, _plan(3, 0, stop=5.0), "ETHUSDT", 1000.0, f.index[0], f.index[-1])
    qty = 0.02 * 1000 / 5.0                                  # 2% risk at a 5-point stop
    assert r.trades[0] < -qty * 19                           # filled near 80, not at the 95 stop


def test_a_grid_bar_that_fills_a_deeper_level_cannot_also_take_the_profit():
    # Basket opens at 100; bar 1 dips to 98.9 (fills the -1% level) and spikes to 102.
    f = _frame([(100, 100.2, 99.8, 100), (100, 102, 98.9, 100)])
    r = st.run_grid(f, "ETHUSDT", 1000.0, f.index[0], f.index[-1])
    assert r.trades == []                                    # no take-profit on bar 1
    f = _frame([(100, 100.2, 99.8, 100), (100, 102, 98.9, 100), (100, 101, 99.9, 100.4)])
    r = st.run_grid(f, "ETHUSDT", 1000.0, f.index[0], f.index[-1])
    assert len(r.trades) == 1 and r.trades[0] > 0            # it comes on bar 2 (+1% over the average)


def test_a_grid_basket_is_stopped_six_percent_under_its_first_entry():
    f = _frame([(100, 100.1, 99.9, 100), (99, 99, 93, 93.5)])
    r = st.run_grid(f, "ETHUSDT", 1000.0, f.index[0], f.index[-1])
    assert len(r.trades) == 1 and r.trades[0] < 0 and not r.liquidated
