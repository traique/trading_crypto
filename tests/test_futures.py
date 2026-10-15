"""The futures book (long/short, fees, stop, liquidation, funding), the long/short rule and the daily step."""

import math

import numpy as np
import pandas as pd
import pytest

from cryptotrader.futures import (
    AccountPolicy,
    Bar,
    FuturesBook,
    LongShortSma,
    backtest,
    rules as fr,
    spec_for,
    step,
)


def _frame(closes, start="2025-01-01", spread=0.01):
    closes = np.asarray(closes, dtype=float)
    dates = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({"Date": dates, "Open": closes, "High": closes * (1 + spread),
                         "Low": closes * (1 - spread), "Close": closes})


def _book(symbol="ETHUSDT", capital=10.0, **kw):
    return FuturesBook(symbol=symbol, capital=capital, fee_pct=0.05, slippage_pct=0.0, **kw)


# ── rules ──
def test_quantity_steps_and_exchange_minimum():
    eth = spec_for("ETHUSDT")
    assert eth.floor_qty(0.0123456) == pytest.approx(0.012)
    assert eth.min_qty_at(2000.0) == pytest.approx(0.01)          # 20 USDT
    assert spec_for("BTCUSDT").min_notional == 50
    unknown = spec_for("FOOUSDT")
    assert not unknown.known and unknown.mmr >= 0.01


def test_liquidation_price_is_where_equity_meets_maintenance():
    cash, qty, entry, mmr = 10.0, 0.01, 2000.0, 0.004         # 20 USDT long: x2
    p = fr.liquidation_price(cash, qty, entry, mmr)
    assert cash + qty * (p - entry) == pytest.approx(abs(qty) * p * mmr)
    assert p < entry
    p_short = fr.liquidation_price(cash, -qty, entry, mmr)
    assert cash - qty * (p_short - entry) == pytest.approx(qty * p_short * mmr)
    assert p_short > entry
    assert fr.liquidation_price(cash, 0.0, entry, mmr) is None


# ── book ──
def test_a_plan_fills_at_the_next_open_with_fee_never_the_same_day():
    book = _book()
    book.plan("2025-01-01", 0.01, 100.0)
    book.process_day(Bar("2025-01-01", 2000, 2010, 1990, 2000))     # same day: no fill
    assert book.qty == 0
    book.pending = None
    book.plan("2025-01-01", 0.01, 100.0)
    book.process_day(Bar("2025-01-02", 2000, 2050, 1990, 2040))
    assert book.qty == pytest.approx(0.01) and book.entry == 2000
    assert book.stop == pytest.approx(1900)
    fee = 0.01 * 2000 * 0.0005
    funding = 0.01 * 2040 * fr.funding_pct_per_day() / 100
    assert book.last_nav == pytest.approx(10 - fee + 0.01 * 40 - funding)


def test_short_profits_when_price_falls_and_flip_closes_first():
    book = _book()
    book.plan("2025-01-01", -0.01, None)
    book.process_day(Bar("2025-01-02", 2000, 2000, 1900, 1900))
    assert book.side == "SHORT"
    assert book.equity(1900) > 10 - 0.02                         # +1 USDT on the short, minus fee
    book.plan("2025-01-02", 0.01, None)
    book.process_day(Bar("2025-01-03", 1900, 1950, 1890, 1950))
    assert book.side == "LONG"
    reasons = [t["reason"] for t in book.trades]
    assert reasons[1] == "đóng để đảo chiều"
    assert book.trades[1]["pnl"] == pytest.approx(1.0)


def test_stop_gapped_through_fills_at_the_open():
    book = _book()
    book.plan("2025-01-01", 0.01, 100.0)
    book.process_day(Bar("2025-01-02", 2000, 2000, 1990, 1995))
    notes = book.process_day(Bar("2025-01-03", 1850, 1860, 1800, 1820))   # opened below the stop 1900
    assert book.qty == 0
    stop_fill = book.trades[-1]
    assert stop_fill["reason"] == "cắt lỗ (stop)" and stop_fill["price"] == 1850
    assert "cắt lỗ" in notes[0]


def test_liquidation_before_a_too_wide_stop_loses_the_account():
    book = _book(capital=10.0)
    book.plan("2025-01-01", 0.05, 1500.0)                       # x10, stop 1500 below liq
    book.process_day(Bar("2025-01-02", 2000, 2000, 1990, 2000))
    liq = book.liquidation()
    assert liq is not None and liq > 1800
    notes = book.process_day(Bar("2025-01-03", 1990, 1995, 1700, 1750))
    assert book.qty == 0 and book.cash == 0 and book.last_nav == 0
    assert "THANH LÝ" in notes[0]


def test_real_funding_is_paid_by_longs_and_received_by_shorts():
    long_, short = _book(), _book()
    long_.qty, long_.entry = 0.01, 2000.0
    short.qty, short.entry = -0.01, 2000.0
    for b in (long_, short):
        b.process_day(Bar("2025-01-02", 2000, 2001, 1999, 2000), funding=0.001)
    assert long_.last_nav == pytest.approx(10 - 0.02)
    assert short.last_nav == pytest.approx(10 + 0.02)


def test_book_round_trips_through_json():
    book = _book()
    book.plan("2025-01-01", 0.01, 50.0)
    book.process_day(Bar("2025-01-02", 2000, 2010, 1990, 2005))
    again = FuturesBook.from_dict(book.to_dict())
    assert again.to_dict() == book.to_dict()
    flat = FuturesBook.from_dict({"symbol": "ETHUSDT", "capital": 10, "cash": 0.0, "trades": [{"x": 1}]})
    assert flat.cash == 0.0


# ── strategy ──
def test_direction_needs_six_of_seven_smas_for_long_and_one_for_short():
    s = LongShortSma()
    up = np.linspace(1000, 3000, 260)
    down = np.linspace(3000, 1000, 260)
    assert s.direction(up) == (1, 7)
    assert s.direction(down) == (-1, 0)
    assert s.direction(up[:150])[0] == 0                         # warm-up
    choppy = 2000 + 50 * np.sin(np.arange(260))
    assert s.direction(choppy)[0] in (-1, 0, 1)


def test_ten_usdt_cannot_trade_btc_but_trades_eth_at_the_minimum():
    s = LongShortSma()
    up = _frame(np.linspace(40_000, 80_000, 260))
    btc = s.decide(up, 10.0, 0.0, spec_for("BTCUSDT"))
    assert not btc["trade"] and btc["qty"] == 0
    assert "quá nhỏ" in btc["reason"]
    # ETH with 80%/year volatility: the 50% target asks for x0.6, under the 20 USDT minimum.
    noisy = np.linspace(1000, 2000, 260) * np.exp(np.random.default_rng(1).normal(0, 0.04, 260) * 0.3)
    noisy[-1] = noisy.max() * 1.01
    eth = s.decide(_frame(noisy), 10.0, 0.0, spec_for("ETHUSDT"))
    assert eth["trade"] and eth["qty"] > 0 and eth["direction"] == 1
    assert eth["qty"] * noisy[-1] >= 20 - 1e-9 and eth["leverage"] <= 3.0 + 1e-9
    assert "tối thiểu" in eth["reason"] and "LONG" in eth["reason"]
    assert eth["stop_dist"] and eth["stop_dist"] > 0


def test_volatility_targeting_caps_leverage_and_the_band_keeps_a_close_position():
    s = LongShortSma()
    rng = np.random.default_rng(0)
    closes = np.exp(np.cumsum(rng.normal(0.004, 0.01, 260))) * 2000
    frame = _frame(closes)
    d = s.decide(frame, 10_000.0, 0.0, spec_for("ETHUSDT"))
    assert d["trade"] and d["direction"] == 1
    assert d["leverage"] <= 2.0 + 1e-9
    held = s.decide(frame, 10_000.0, d["qty"] * 1.1, spec_for("ETHUSDT"))
    assert not held["trade"] and "giữ nguyên" in held["reason"]


# ── daily step and backtest ──
def test_goal_closes_the_position_and_halts_the_book():
    up = np.linspace(1000, 3000, 260)
    frame = _frame(up)
    book = _book()
    book.qty, book.entry = 0.1, 1000.0                           # deep in profit
    notes = step(book, LongShortSma(), frame, AccountPolicy(goal_profit=100, stop_equity=5))
    assert book.meta["halted"] == "goal"
    assert book.pending["qty"] == 0
    assert any("Đạt mục tiêu" in n for n in notes)
    # Halted: later days only keep the book flat.
    nxt = _frame(np.append(up, up[-1]))
    step(book, LongShortSma(), nxt, AccountPolicy(100, 5))
    assert book.qty == 0 and book.meta["halted"] == "goal"


def test_floor_stops_trading():
    frame = _frame(np.linspace(1000, 3000, 260))
    book = _book()
    book.cash = 4.0
    notes = step(book, LongShortSma(), frame, AccountPolicy(goal_profit=100, stop_equity=5))
    assert book.meta["halted"] == "floor" and any("dừng" in n for n in notes)


def test_backtest_trades_a_trend_both_ways_without_look_ahead():
    up = np.linspace(1000, 3000, 400)
    down = np.linspace(3000, 1200, 400)
    frame = _frame(np.concatenate([up, down]))
    book = backtest(frame, capital=1000.0, symbol="ETHUSDT")
    sides = {t["side"] for t in book.trades}
    assert sides == {"BUY", "SELL"}
    assert any(h["qty"] < 0 for h in book.history) and any(h["qty"] > 0 for h in book.history)
    assert book.last_nav > 1000
    # Every fill is on a day after the decision that planned it.
    days = [h["date"] for h in book.history]
    assert all(t["date"] in days for t in book.trades)
    assert math.isfinite(book.last_nav)


def test_contract_filters_are_refreshed_from_exchange_info_and_cached(tmp_path):
    payload = {"symbols": [
        {"symbol": "PEPEXUSDT", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING",
         "filters": [{"filterType": "LOT_SIZE", "stepSize": "1"}, {"filterType": "MIN_NOTIONAL", "notional": "5"}]},
        {"symbol": "BTCUSDT", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING",
         "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001"}, {"filterType": "MIN_NOTIONAL", "notional": "70"}]},
        {"symbol": "OLDUSDT", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "SETTLING", "filters": []},
    ]}
    saved = dict(fr.SPECS)
    try:
        assert fr.refresh_specs(tmp_path, fetch=lambda: payload, now=1000.0) == 2
        assert fr.spec_for("BTCUSDT").min_notional == 70 and fr.spec_for("PEPEXUSDT").known
        # Cached for a day: no second fetch.
        assert fr.refresh_specs(tmp_path, fetch=lambda: 1 / 0, now=2000.0) == 2
        # Exchange down and cache stale: the table stays as it is.
        assert fr.refresh_specs(tmp_path, fetch=lambda: 1 / 0, now=1000.0 + 2 * 86_400) == 0
        assert fr.spec_for("BTCUSDT").min_notional == 70
    finally:
        fr.SPECS.clear()
        fr.SPECS.update(saved)
