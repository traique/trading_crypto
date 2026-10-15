from datetime import date

import pandas as pd

from cryptotrader.market.rules import BENCHMARK, SymbolFilters
from cryptotrader.strategies import (
    REGISTRY,
    BtcBuyHold,
    BtcTrend,
    Strategy,
    StrategyContext,
    get_strategy,
)
from cryptotrader.trading.ledger import Bar, Ledger
from tests.helpers import frame


def test_registry_and_protocol():
    for name in REGISTRY:
        assert isinstance(get_strategy(name), Strategy)


def test_context_trims_the_future():
    hist = frame(date(2026, 1, 1), 30)
    ctx = StrategyContext("2026-01-10", {BENCHMARK: hist})
    assert ctx.history[BENCHMARK]["Date"].iloc[-1] == pd.Timestamp("2026-01-10")
    assert ctx.last_close(BENCHMARK) == 109.0
    assert StrategyContext("2026-02-15", {BENCHMARK: hist}).last_close(BENCHMARK) is None


def test_buy_hold():
    hist = frame(date(2026, 1, 1), 5)
    assert BtcBuyHold().decide(StrategyContext("2026-01-03", {BENCHMARK: hist})) == {BENCHMARK: 1.0}
    held = StrategyContext("2026-02-01", {BENCHMARK: hist}, weights={BENCHMARK: 0.9})
    assert BtcBuyHold().decide(held) == {BENCHMARK: 0.9}


def test_trend_filter():
    up = frame(date(2025, 1, 1), 250, 100.0, 1.0)
    down = frame(date(2025, 1, 1), 250, 400.0, -1.0)
    s = BtcTrend()
    assert s.decide(StrategyContext("2025-09-07", {BENCHMARK: up})) == {BENCHMARK: 1.0}
    assert s.decide(StrategyContext("2025-09-07", {BENCHMARK: down})) == {}
    assert s.decide(StrategyContext("2025-03-01", {BENCHMARK: up})) == {}       # warm-up: cash


def test_strategy_drives_the_ledger_without_look_ahead():
    hist = frame(date(2025, 1, 1), 260, 100.0, 1.0)
    led = Ledger(cash=10_000, start_cash=10_000, fee_pct=0.1, slippage_pct=0.1)
    led.filters[BENCHMARK] = SymbolFilters(tick_size=0.01, step_size=0.0001, min_qty=0.0001, min_notional=5)
    strat = BtcTrend(sma_days=50)
    days = [d.strftime("%Y-%m-%d") for d in hist["Date"]]
    for i, day in enumerate(days):
        row = hist.iloc[i]
        led.process_day({BENCHMARK: Bar(day, row.Open, row.High, row.Low, row.Close)})
        led.end_of_day(day, benchmark=row.Close)
        ctx = StrategyContext(day, {BENCHMARK: hist}, weights=led.weights({BENCHMARK: row.Close}), nav=led.nav())
        led.plan_target_weights(day, strat.decide(ctx), {BENCHMARK: row.Close})
    first_buy = next(f for f in led.fills if f.side == "BUY")
    assert first_buy.date == days[50]           # decided on the 50th close, filled at the next open
    assert led.stats()["return_pct"] > 0
