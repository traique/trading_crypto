"""Backtester that drives the project's REAL ledger (``cryptotrader.trading.ledger``).

Each UTC day D of [start, end]:

1. Pairs that stopped trading: a held pair without a candle for more than
   ``config.DELIST_GAP_DAYS`` days is sold at its LAST CLOSE (minus slippage and
   the fee) on D, with a warning (see "Delisted pairs" below).
2. ``ledger.process_day(candles of D)`` fills the orders decided at D-1's close
   (market at the open + slippage on the tick grid, limits that trade through,
   fee in USDT, minQty/minNotional, stops/targets): the ledger's own code,
   exactly what the paper account runs.
3. ``ledger.end_of_day(D, BTC close)`` expires stale orders and records the NAV.
4. The strategy decides at D's close. Its context only exposes candles up to D
   (every frame is sliced ``iloc[:i+1]``). It returns orders for D+1:
   - ``None``: nothing to do;
   - ``{pair: weight}``: target weights of the NAV (the rest in USDT), turned
     into orders by ``ledger.plan_target_weights`` - as the bot does;
   - a list of ``Order`` (``ctx.buy`` / ``ctx.sell``), for limits, stops, targets.
   A strategy may also submit orders to ``ctx.ledger`` itself.
   A ``cryptotrader.strategies`` object (``decide(StrategyContext)``) is run
   through the same ``StrategyContext`` the bot builds.
5. Optional liquidity cap: each new BUY is cut to ``max_adv_pct`` x the pair's
   average daily USDT volume of the last 30 days (days without trading count 0).

Delisted pairs: the archive keeps their candles, so a strategy can hold one.
When a pair has no candle for more than ``DELIST_GAP_DAYS`` days the position is
liquidated at its last close (no look-ahead: the gap is observed first; the cash
comes back then). Binance announces delistings about a week ahead, so a real
holder could sell around that close - but delisted coins usually crash on the
announcement, and that crash IS in the candles a strategy holds through. A
position below the exchange minimum cannot be sold and is written off (0).

Exchange filters: today's exchangeInfo filters do not describe 2018 (tick and
step sizes change with the price), so the ledger uses ``rules.DEFAULT_FILTERS``
(fine grid, minNotional 10 USDT) for every pair.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from cryptotrader.data import klines as kl
from cryptotrader.market import rules
from cryptotrader.trading.ledger import Bar, ExitRules, Ledger, Order
from research import config, metrics as M
from research.data import Panel, TestPeriodLocked, check_locked, load_panel
from research.universe import universe as _universe

__all__ = ["Context", "Result", "TestPeriodLocked", "run", "run_price", "ADV_DAYS"]

ADV_DAYS = 30
BENCH = rules.BENCHMARK


def check_dates(end, unlock_test: bool) -> None:
    if pd.Timestamp(end) > pd.Timestamp(config.LOCK_AFTER) and not unlock_test:
        raise TestPeriodLocked(f"end {end} is in the locked TEST period (after {config.LOCK_AFTER}); "
                               "only the evaluator may pass unlock_test=True")


class _Arrays:
    """numpy views of a panel for fast candle construction."""

    def __init__(self, panel: Panel):
        self.cols = {s: j for j, s in enumerate(panel.close.columns)}
        self.o = panel.open.to_numpy()
        self.h = panel.high.to_numpy()
        self.l = panel.low.to_numpy()  # noqa: E741
        self.c = panel.close.to_numpy()
        self.v = panel.volume.to_numpy()
        has = ~np.isnan(self.c)
        rows = np.where(has, np.arange(len(panel.dates))[:, None], np.nan)
        self.last_row = pd.DataFrame(rows).ffill().to_numpy()       # last row <= i with a candle
        self.dates = [d.strftime("%Y-%m-%d") for d in panel.dates]
        qv = panel.quote_volume.where(has, 0.0).fillna(0.0)
        self.adv = qv.rolling(ADV_DAYS, min_periods=1).mean().to_numpy()
        j = self.cols.get(BENCH)
        self.bench = (pd.Series(self.c[:, j]).ffill().to_numpy() if j is not None
                      else np.full(len(panel.dates), np.nan))

    def bar(self, sym: str, i: int) -> Bar | None:
        j = self.cols.get(sym)
        if j is None or math.isnan(self.c[i, j]):
            return None
        vol = self.v[i, j]
        return Bar(self.dates[i], float(self.o[i, j]), float(self.h[i, j]), float(self.l[i, j]),
                   float(self.c[i, j]), None if math.isnan(vol) else float(vol))


class Context:
    """What a strategy sees at the close of day ``date`` (no later data)."""

    def __init__(self, panel: Panel, arrays: _Arrays, i: int, ledger: Ledger, universe_kind: str, state: dict,
                 frames: dict):
        self._panel, self._arrays, self.i = panel, arrays, i
        self.date = panel.dates[i]
        self.day = arrays.dates[i]
        self.ledger = ledger
        self.universe_kind = universe_kind
        self.state = state                       # free per-run storage for the strategy
        self._frames = frames                    # per-pair candle frames (shared cache)

    # -- point-in-time data (sliced to days <= date) --
    def _cut(self, frame):
        return frame.iloc[: self.i + 1]

    @property
    def open(self) -> pd.DataFrame: return self._cut(self._panel.open)
    @property
    def high(self) -> pd.DataFrame: return self._cut(self._panel.high)
    @property
    def low(self) -> pd.DataFrame: return self._cut(self._panel.low)
    @property
    def close(self) -> pd.DataFrame: return self._cut(self._panel.close)
    @property
    def volume(self) -> pd.DataFrame: return self._cut(self._panel.volume)
    @property
    def quote_volume(self) -> pd.DataFrame: return self._cut(self._panel.quote_volume)
    @property
    def trades(self) -> pd.DataFrame: return self._cut(self._panel.trades)

    @property
    def btc(self) -> pd.Series:
        """BTCUSDT closes up to today."""
        return self.close[BENCH] if BENCH in self._panel.close.columns else pd.Series(dtype=float)

    def history(self, field: str, symbols=None, n: int | None = None) -> pd.DataFrame:
        """The last ``n`` days (<= date) of a field, optionally for some pairs."""
        frame = getattr(self, field)
        if symbols is not None:
            frame = frame.reindex(columns=list(symbols))
        return frame if n is None else frame.iloc[-n:]

    def candles(self, symbol: str) -> pd.DataFrame:
        """``symbol``'s candles up to today in ``cryptotrader.data.klines`` columns (what the bot loads)."""
        full = self._frames.get(symbol)
        if full is None:
            j = self._arrays.cols.get(symbol)
            if j is None:
                full = kl.empty_frame()
            else:
                p = self._panel
                full = pd.DataFrame({"Date": p.dates, "Open": p.open.iloc[:, j].to_numpy(),
                                     "High": p.high.iloc[:, j].to_numpy(), "Low": p.low.iloc[:, j].to_numpy(),
                                     "Close": p.close.iloc[:, j].to_numpy(),
                                     "Volume": p.volume.iloc[:, j].to_numpy(),
                                     "QuoteVolume": p.quote_volume.iloc[:, j].to_numpy(),
                                     "Trades": p.trades.iloc[:, j].to_numpy()})
                full = full.dropna(subset=["Close"]).reset_index(drop=True)
            self._frames[symbol] = full
        k = int(full["Date"].searchsorted(self.date, side="right"))
        return full.iloc[:k]

    def universe(self, kind: str | None = None) -> list[str]:
        """The universe for tomorrow (built from candles up to today), most liquid first."""
        return _universe(self.date + pd.Timedelta(days=1), kind or self.universe_kind, self._panel)

    def has_bar(self, symbol: str) -> bool:
        return self.last_close(symbol) is not None

    def last_close(self, symbol: str) -> float | None:
        """Today's close of ``symbol``; None when it did not trade today."""
        j = self._arrays.cols.get(symbol)
        if j is None:
            return None
        v = self._arrays.c[self.i, j]
        return None if math.isnan(v) else float(v)

    def adv(self, symbol: str) -> float:
        """Average daily USDT volume of the last 30 days (days without trading count 0)."""
        j = self._arrays.cols.get(symbol)
        return 0.0 if j is None else float(self._arrays.adv[self.i, j])

    # -- book --
    @property
    def nav(self) -> float: return self.ledger.nav()
    @property
    def cash(self) -> float: return self.ledger.cash
    @property
    def positions(self) -> dict: return self.ledger.positions

    def prices(self, symbols=()) -> dict[str, float]:
        """Today's closes of ``symbols`` and of every holding (a holding without a candle
        today keeps its last mark)."""
        out = {}
        for s in set(symbols) | set(self.ledger.positions):
            px = self.last_close(s)
            if px is None and s in self.ledger.positions:
                px = self.ledger.positions[s].last_price or None
            if px:
                out[s] = px
        return out

    def weights(self) -> dict[str, float]:
        return self.ledger.weights(self.prices())

    def rebalance(self, weights: dict[str, float], *, min_trade_pct: float = 1.0, reason: str = "") -> list[Order]:
        """Orders for tomorrow's open that move the book to ``weights`` (``ledger.plan_target_weights``)."""
        return self.ledger.plan_target_weights(self.day, weights, self.prices(weights), min_trade_pct=min_trade_pct,
                                               reason=reason)

    def strategy_context(self, symbols, state: dict):
        """The ``cryptotrader.strategies.StrategyContext`` the bot would build today."""
        from cryptotrader.strategies.base import StrategyContext

        return StrategyContext(date=self.day, history={s: self.candles(s) for s in symbols},
                               weights=self.weights(), nav=self.nav, cash=self.cash, state=state)

    # -- order helpers --
    def buy(self, symbol: str, *, qty: float | None = None, value: float | None = None, weight: float | None = None,
            limit: float | None = None, stop: float | None = None, stop_pct: float = 0.0,
            target1: float | None = None, target2: float | None = None, trail_pct: float = 0.0,
            reason: str = "") -> Order | None:
        """A BUY for tomorrow: at the open (``limit`` None) or a one-day limit. Sized by
        ``qty`` coins, ``value`` USDT or ``weight`` of NAV (room left for slippage and fee)."""
        ref = self.last_close(symbol)
        price = limit or ref
        if not price:
            return None
        if qty is None:
            budget = value if value is not None else (weight or 0.0) * self.ledger.nav()
            slip = 1.0 if limit else 1 + self.ledger.slippage_pct / 100
            qty = budget / (price * slip * (1 + self.ledger.fee_pct / 100))
        if not qty or qty <= 0:
            return None
        return Order(symbol, "BUY", float(qty), limit, self.day, reason or "harness buy", stop, target1, target2,
                     stop_pct=stop_pct, trail_pct=trail_pct, ref=ref)

    def sell(self, symbol: str, qty: float | None = None, *, limit: float | None = None,
             reason: str = "") -> Order | None:
        """Sell ``qty`` (default: the whole holding) tomorrow, at the open or a limit."""
        pos = self.ledger.positions.get(symbol)
        if pos is None:
            return None
        return Order(symbol, "SELL", float(qty or pos.qty), limit, self.day, reason or "harness sell",
                     ref=self.last_close(symbol) or pos.last_price)


@dataclass
class Result:
    name: str
    start: str
    end: str
    universe_kind: str
    ledger: Ledger
    daily: pd.DataFrame              # day-indexed: nav, cash, invested, exposure, bench (BTC close)
    fills: pd.DataFrame
    metrics: dict
    benchmark: dict                  # BTC buy & hold (price, no costs) over the same days
    yearly: pd.DataFrame
    warnings: list[str] = field(default_factory=list)
    options: dict = field(default_factory=dict)

    def summary(self) -> str:
        m, b = self.metrics, self.benchmark
        return (f"{self.name} [{self.universe_kind}] {self.start}..{self.end}: "
                f"ret {m['total_return_pct']:+.1f}% CAGR {m['cagr_pct']:+.1f}% MDD {m['max_drawdown_pct']:.1f}% "
                f"Sharpe {m['sharpe']:.2f} | BTC {b.get('total_return_pct', float('nan')):+.1f}% "
                f"CAGR {b.get('cagr_pct', float('nan')):+.1f}% MDD {b.get('max_drawdown_pct', float('nan')):.1f}%")


def _apply_liquidity_cap(ctx: Context, orders: list[Order], max_adv_pct: float) -> None:
    """Cut each new BUY to ``max_adv_pct`` x 30-day average USDT volume; drop it when nothing is left."""
    led = ctx.ledger
    for o in orders:
        if o.side != "BUY":
            continue
        price = o.limit or o.ref or ctx.last_close(o.symbol)
        flt = led.filter_for(o.symbol)
        cap = rules.floor_to_step(max_adv_pct * ctx.adv(o.symbol) / price, flt.step_size) if price else 0.0
        o.qty = min(o.qty, cap)
        if not price or o.qty <= 0 or rules.order_rejection(o.qty, price, flt):
            led.orders.remove(o)


def _liquidate_gone(ledger: Ledger, arrays: _Arrays, i: int, bars: dict[str, Bar], warnings: list[str],
                    gone: set[str]) -> None:
    """Sell, at its last close, every holding whose pair has had no candle for > DELIST_GAP_DAYS days."""
    day = arrays.dates[i]
    for sym in list(ledger.positions):
        j = arrays.cols.get(sym)
        if j is None or not math.isnan(arrays.c[i, j]):
            continue
        last = arrays.last_row[i, j]
        if math.isnan(last) or i - int(last) <= config.DELIST_GAP_DAYS:
            continue
        last = int(last)
        close = float(arrays.c[last, j])
        pos = ledger.positions[sym]
        ledger.orders = [o for o in ledger.orders if o.symbol != sym]
        exit_px = rules.slipped(close, "SELL", ledger.filter_for(sym), ledger.slippage_pct)
        order = Order(sym, "SELL", pos.qty, None, arrays.dates[i - 1],
                      f"thanh lý: ngừng giao dịch từ {arrays.dates[last]}", ref=close)
        if ledger.submit(order) is None:
            ledger.positions.pop(sym, None)
            warnings.append(f"{sym}: no candle since {arrays.dates[last]}; holding {pos.qty:g} below the exchange "
                            f"minimum at {close:g}, written off on {day}")
            continue
        # A one-price candle at the last close (low = the slipped exit) for the ledger to fill.
        bars[sym] = Bar(day, close, close, min(exit_px, close), min(exit_px, close), None)
        gone.add(sym)
        warnings.append(f"{sym}: no candle since {arrays.dates[last]} (delisted/halted); "
                        f"liquidated {pos.qty:g} at its last close {close:g} on {day}")


def _is_live_strategy(strategy) -> bool:
    return callable(getattr(strategy, "decide", None)) and hasattr(strategy, "symbols")


def run(strategy, start: str, end: str, *, capital: float = 10_000, universe_kind: str = "liq_top20",
        slippage_mult: float = 1.0, fee_pct: float | None = None, max_adv_pct: float | None = None,
        unlock_test: bool = False, panel: Panel | None = None, name: str | None = None,
        exit_rules: ExitRules | None = None) -> Result:
    """Backtest ``strategy`` over the days [start, end] on the project's ledger.

    ``strategy`` is one of:
    - an object with ``on_day(ctx)`` (optional ``name``, ``on_start(ctx)``,
      ``exit_rules``, ``universe_label``), or a plain function ``f(ctx)``;
    - a ``cryptotrader.strategies`` object (``decide(StrategyContext)``, ``symbols``),
      run as the bot runs it (target weights through ``plan_target_weights``).

    Costs: ``fee_pct`` per fill (default ``rules.TAKER_FEE_PCT``, 0.1%),
    slippage ``rules.SLIPPAGE_PCT`` x ``slippage_mult`` per market fill.
    """
    check_dates(end, unlock_test)
    if pd.Timestamp(start) > pd.Timestamp(end):
        raise ValueError(f"start {start} is after end {end}")
    panel = panel if panel is not None else load_panel(unlock_test=unlock_test)
    check_locked(panel, unlock_test)
    arrays = _Arrays(panel)
    idx = np.where((panel.dates >= pd.Timestamp(start)) & (panel.dates <= pd.Timestamp(end)))[0]
    if len(idx) == 0:
        raise ValueError(f"no days between {start} and {end} in the data (ends {panel.dates[-1].date()})")
    live = _is_live_strategy(strategy)
    sname = name or getattr(strategy, "name", None) or getattr(strategy, "__name__", type(strategy).__name__)
    if live:
        live_state: dict = {}
        min_trade = float(getattr(strategy, "min_trade_pct", 1.0))
        symbols = tuple(strategy.symbols)

        def call(ctx):
            return strategy.decide(ctx.strategy_context(symbols, live_state))
    else:
        call: Callable = getattr(strategy, "on_day", None) or strategy
        min_trade = float(getattr(strategy, "min_trade_pct", 1.0))
    exit_rules = exit_rules or getattr(strategy, "exit_rules", None) or ExitRules()
    ledger = Ledger(cash=float(capital), start_cash=float(capital),
                    fee_pct=rules.TAKER_FEE_PCT if fee_pct is None else float(fee_pct),
                    slippage_pct=rules.SLIPPAGE_PCT * float(slippage_mult))
    state: dict = {}
    frames: dict = {}
    rows = []
    warnings: list[str] = []
    gone: set[str] = set()
    for k, i in enumerate(idx):
        day = arrays.dates[i]
        bars: dict[str, Bar] = {}
        if k > 0:
            _liquidate_gone(ledger, arrays, i, bars, warnings, gone)
        for s in {o.symbol for o in ledger.orders} | set(ledger.positions):
            if s not in bars and (b := arrays.bar(s, i)) is not None:
                bars[s] = b
        ledger.process_day(bars, exit_rules=exit_rules)
        for sym in [s for s in gone if s in ledger.positions and bars.get(s) and bars[s].volume is None]:
            pos = ledger.positions.pop(sym)       # the exit sale was refused (below the minimum)
            warnings.append(f"{sym}: liquidation refused by the exchange minimums; {pos.qty:g} written off on {day}")
        bench = arrays.bench[i]
        ledger.end_of_day(day, None if math.isnan(bench) else float(bench))
        ctx = Context(panel, arrays, i, ledger, universe_kind, state, frames)
        if k == 0 and hasattr(strategy, "on_start"):
            strategy.on_start(ctx)
        before = len(ledger.orders)
        out = call(ctx)
        if isinstance(out, dict):
            ctx.rebalance(out, min_trade_pct=min_trade)
        elif out:
            for o in out:
                if o is not None:
                    o.created = day
                    ledger.submit(o)
        new = ledger.orders[before:]
        for o in new:
            o.created = day
        if max_adv_pct is not None:
            _apply_liquidity_cap(ctx, list(new), max_adv_pct)
        nav = ledger.nav()
        rows.append((panel.dates[i], nav, ledger.cash, nav - ledger.cash, bench))
    daily = pd.DataFrame(rows, columns=["date", "nav", "cash", "invested", "bench"]).set_index("date")
    daily["exposure"] = (daily["invested"] / daily["nav"]).clip(lower=0)
    last_i = idx[-1]
    for s, pos in ledger.positions.items():
        j = arrays.cols.get(s)
        if j is not None and math.isnan(arrays.c[last_i, j]):
            warnings.append(f"{s}: held at the end without a candle on the last day (marked at {pos.last_date})")
    fills = pd.DataFrame([f.__dict__ for f in ledger.fills])
    met = M.compute(daily["nav"], capital, daily["exposure"], fills)
    bmet = _bench_metrics(daily["bench"], capital)
    yearly = M.yearly(daily["nav"], capital, daily["exposure"], daily["bench"])
    label = getattr(strategy, "universe_label", None) or universe_kind
    options = {"capital": capital, "slippage_pct": ledger.slippage_pct, "fee_pct": ledger.fee_pct,
               "max_adv_pct": max_adv_pct, "liquidated": sorted(gone)}
    return Result(sname, str(panel.dates[idx[0]].date()), str(panel.dates[last_i].date()), label, ledger,
                  daily, fills, met, bmet, yearly, warnings, options)


def _bench_metrics(bench: pd.Series, capital: float) -> dict:
    b = bench.ffill().dropna()
    if b.empty:
        return {}
    nav = b / b.iloc[0] * capital
    return M.compute(nav, capital, pd.Series(1.0, index=nav.index), None)


def run_price(symbol: str, start: str, end: str, *, capital: float = 10_000, unlock_test: bool = False,
              panel: Panel | None = None) -> Result:
    """``symbol``'s price over [start, end] as a NAV (no costs, no ledger): the yardstick
    ``Result.benchmark`` reports for BTC. NAV = capital x close / the first day's close."""
    check_dates(end, unlock_test)
    panel = panel if panel is not None else load_panel(unlock_test=unlock_test)
    check_locked(panel, unlock_test)
    if symbol not in panel.close.columns:
        raise ValueError(f"unknown pair {symbol!r}: not in the research data")
    s = panel.close[symbol][(panel.dates >= pd.Timestamp(start)) & (panel.dates <= pd.Timestamp(end))].ffill().dropna()
    if s.empty:
        raise ValueError(f"no {symbol} candles between {start} and {end}")
    nav = s / s.iloc[0] * capital
    exposure = pd.Series(1.0, index=nav.index)
    bench = panel.close[BENCH].reindex(nav.index) if BENCH in panel.close.columns else None
    daily = pd.DataFrame({"nav": nav, "cash": 0.0, "invested": nav, "exposure": exposure,
                          "bench": bench if bench is not None else np.nan})
    met = M.compute(nav, capital, exposure, None)
    yearly = M.yearly(nav, capital, exposure, bench)
    return Result(f"{symbol}_price", str(s.index[0].date()), str(s.index[-1].date()), "-",
                  Ledger(cash=capital, start_cash=capital), daily, pd.DataFrame(), met,
                  _bench_metrics(daily["bench"], capital) if bench is not None else {}, yearly,
                  options={"capital": capital, "costs": "none (price only)"})
