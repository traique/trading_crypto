"""A Binance spot book in USDT, and the order/exit rules of the bot.

One set of rules serves the paper account and the backtest, so a backtest
result is what the paper account would have done:

- Decisions are taken on a day's close (00:00 UTC); orders are placed with that
  day as ``created`` and can only fill on a LATER day's candle (no same-candle
  look-ahead). An order not filled on the next candle of its pair expires at
  ``end_of_day`` (good for one day).
- Market orders fill at the open, moved ``slippage_pct`` against the trader and
  onto the tick grid in that direction, never outside the candle's [low, high].
- Limit orders fill only when the candle trades THROUGH the limit (a BUY needs
  ``low < limit``, a SELL ``high > limit``): touching the limit is not enough,
  as the queue at that price may never reach the order. A limit that is already
  marketable at the open fills at the open plus slippage, never worse than the
  limit.
- Quantities are fractional, floored to the pair's ``stepSize``; an order or a
  fill below ``minQty`` or ``minNotional`` is rejected (as the exchange would).
  A partial sale that would leave dust below those minimums sells everything.
- The taker fee (``fee_pct``, 0.1% by default) is charged in USDT on every
  fill; the buy fee is part of the cost basis. (On Binance without BNB the buy
  fee is taken in the bought coin; for a USDT book the cost is the same.)
- No settlement delay: a coin bought at the open can be stopped out the same
  day, and cash from a sale at the open pays for that open's buys. Cash from a
  stop or target hit during the day is not used for that day's buys, which
  are modelled at the open.
- Exits each day, in order: SELL orders at the open; the stop (at the open if it
  gapped below); ``t1_fraction`` at target 1 (the stop then moves to break-even)
  and the rest at target 2; a time stop at the close after ``max_hold_days``.
  When a candle touches both the stop and a target, the stop is assumed first.
  On the day of a buy only the stop is checked (the high may have come before
  the fill). A trailing stop (``trail_pct``) follows the highest close.
- A candle with a missing, non-finite or non-positive price, or an impossible
  range, is a day without trading for that pair: no fill, no stop.
- The NAV is in USDT; ``end_of_day`` records it with the benchmark (BTC close).
- A saved book that cannot be read is never replaced by an empty one behind the
  owner's back: ``Ledger.load`` moves it aside and raises (see there).
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from datetime import date as _date, datetime
from pathlib import Path

from cryptotrader.market import rules
from cryptotrader.market.rules import SymbolFilters

logger = logging.getLogger(__name__)

# A position smaller than this is gone (float noise after a full sale).
_QTY_EPS = 1e-12
MAX_SAVED_FILLS = 2000
MAX_SAVED_HISTORY = 4000


class LedgerLoadError(RuntimeError):
    """A saved book exists but cannot be used; it was not overwritten."""


def finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _positive(value) -> bool:
    return finite(value) and float(value) > 0


@dataclass
class Bar:
    date: str                       # the candle's UTC day, YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None     # when given, a candle with no volume is a day without trading

    @property
    def tradable(self) -> bool:
        """Whether the candle traded at believable prices (forward-filled NaN rows do not)."""
        prices = (self.open, self.high, self.low, self.close)
        if not all(_positive(p) for p in prices):
            return False
        if self.low > self.high:
            return False
        tol = 1e-9 * self.high
        if not (self.low - tol <= self.open <= self.high + tol and self.low - tol <= self.close <= self.high + tol):
            return False
        return self.volume is None or _positive(self.volume)


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float                # cost per unit, buy fee included
    buy_date: str                   # the first buy
    stop: float = 0.0
    target1: float | None = None
    target2: float | None = None
    trail_pct: float = 0.0          # trailing stop this % under the highest close (0 = off)
    t1_done: bool = False
    last_price: float = 0.0
    last_date: str = ""
    peak: float = 0.0               # highest close since the buy
    days: int = 0                   # candles traded since the buy day

    def value(self, price: float | None = None) -> float:
        return self.qty * (price if _positive(price) else (self.last_price or self.avg_price))


@dataclass
class Order:
    symbol: str
    side: str                       # BUY | SELL
    qty: float
    limit: float | None             # None = market at the open
    created: str                    # the day whose close produced it
    reason: str = ""
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    stop_pct: float = 0.0           # stop this % under the fill when no absolute stop is given
    trail_pct: float = 0.0
    ref: float | None = None        # price the size was computed at (close of ``created``)


@dataclass
class Fill:
    date: str
    symbol: str
    side: str
    qty: float
    price: float
    fee: float                      # USDT
    reason: str
    pnl: float | None = None        # realised, sells only (after both fees)


@dataclass(frozen=True)
class ExitRules:
    t1_fraction: float = 0.5
    use_t2: bool = True
    breakeven_after_t1: bool = True
    max_hold_days: int | None = None


def _round8(x: float) -> float:
    return float(f"{x:.10g}") if finite(x) else x


@dataclass
class Ledger:
    cash: float
    start_cash: float
    start_date: str = ""
    # Read when a ledger is created (not at import), so a .env loaded later counts.
    fee_pct: float = field(default_factory=lambda: rules.TAKER_FEE_PCT)
    slippage_pct: float = field(default_factory=lambda: rules.SLIPPAGE_PCT)
    positions: dict[str, Position] = field(default_factory=dict)
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)    # {date, nav, benchmark}
    meta: dict = field(default_factory=dict)             # strategy state, rejections
    # Runtime only (not saved): the pairs' exchange filters; defaults when unknown.
    filters: dict[str, SymbolFilters] = field(default_factory=dict, repr=False, compare=False)

    # ── helpers ──
    def filter_for(self, symbol: str) -> SymbolFilters:
        return self.filters.get(symbol) or rules.DEFAULT_FILTERS

    def _fee(self, value: float) -> float:
        return value * self.fee_pct / 100

    def _slipped(self, raw: float, side: str, bar: Bar, symbol: str) -> float:
        """A market fill at ``raw``: slipped against the trader, kept inside the candle."""
        price = rules.slipped(raw, side, self.filter_for(symbol), self.slippage_pct)
        return min(price, bar.high) if side == "BUY" else max(price, bar.low)

    def _reject(self, day: str, symbol: str, side: str, reason: str) -> None:
        logger.info("%s %s %s rejected: %s", day, side, symbol, reason)
        notes = self.meta.setdefault("rejections", [])
        notes.append({"date": day, "symbol": symbol, "side": side, "reason": reason})
        del notes[:-50]

    # ── valuation ──
    def nav(self, prices: dict[str, float] | None = None) -> float:
        prices = prices or {}
        return self.cash + sum(p.value(prices.get(s)) for s, p in self.positions.items())

    def weights(self, prices: dict[str, float] | None = None) -> dict[str, float]:
        nav = self.nav(prices)
        prices = prices or {}
        return {s: p.value(prices.get(s)) / nav for s, p in self.positions.items()} if nav > 0 else {}

    def reserved(self) -> float:
        """Cash the open BUY orders may need (at their limit, or reference price plus slippage)."""
        slip = 1 + self.slippage_pct / 100
        fee = 1 + self.fee_pct / 100
        return sum(o.qty * (o.limit or (o.ref or 0.0) * slip) * fee for o in self.orders if o.side == "BUY")

    # ── orders ──
    def submit(self, order: Order) -> Order | None:
        """Queue ``order`` for the next candle; None (and a logged rejection) when it is invalid.

        One open order per pair and side: a second one is refused, so a day planned
        twice does not double the orders. The quantity is floored to the step; a SELL
        is capped at the holding; an order below the pair's minimums is refused.
        """
        order.symbol = order.symbol.strip().upper()
        order.side = order.side.strip().upper()
        if order.side not in ("BUY", "SELL"):
            self._reject(order.created, order.symbol, order.side, "lệnh phải là BUY hoặc SELL")
            return None
        if any(o.symbol == order.symbol and o.side == order.side for o in self.orders):
            self._reject(order.created, order.symbol, order.side, "đã có lệnh cùng chiều đang chờ")
            return None
        if order.limit is not None and not _positive(order.limit):
            self._reject(order.created, order.symbol, order.side, "giá giới hạn không hợp lệ")
            return None
        flt = self.filter_for(order.symbol)
        qty = rules.floor_to_step(float(order.qty), flt.step_size) if finite(order.qty) else 0.0
        if order.side == "SELL":
            pos = self.positions.get(order.symbol)
            if pos is None:
                self._reject(order.created, order.symbol, "SELL", "không có vị thế để bán")
                return None
            # A whole exit sells what the exchange accepts: the holding floored to the step
            # (a real holding net of a fee taken in the coin can be off the step grid; the
            # sub-step remainder cannot be sold and stays as dust).
            whole = self.sellable_qty(pos)
            qty = whole if qty >= whole or self._dust_after(pos, qty, order.limit or order.ref) else qty
        price = order.limit or order.ref
        why = "khối lượng bằng 0" if qty <= 0 else (rules.order_rejection(qty, price, flt) if _positive(price) else None)
        if why:
            self._reject(order.created, order.symbol, order.side, why)
            return None
        order.qty = qty
        self.orders.append(order)
        return order

    def sellable_qty(self, pos: Position) -> float:
        """The part of a holding an order can sell: its quantity floored to the step."""
        return min(rules.floor_to_step(pos.qty, self.filter_for(pos.symbol).step_size), pos.qty)

    def is_dust(self, pos: Position, price: float | None) -> bool:
        """Whether the whole holding is too small for the exchange to accept a SELL of it."""
        price = price if _positive(price) else (pos.last_price or pos.avg_price)
        qty = self.sellable_qty(pos)
        return qty <= 0 or not _positive(price) or rules.order_rejection(qty, price, self.filter_for(pos.symbol)) is not None

    def _dust_after(self, pos: Position, qty: float, price: float | None) -> bool:
        """Whether selling ``qty`` would leave a remainder the exchange cannot sell."""
        left = pos.qty - qty
        if left <= _QTY_EPS:
            return True
        price = price if _positive(price) else (pos.last_price or pos.avg_price)
        return rules.order_rejection(left, price, self.filter_for(pos.symbol)) is not None

    # ── one day ──
    def process_day(self, bars: dict[str, Bar], *, exit_rules: ExitRules | None = None) -> list[Fill]:
        """Apply one day's candles to the whole book: SELL orders at the open, BUY orders
        (cash from those sales included), then the stops and targets, then the marks."""
        exit_rules = exit_rules or ExitRules()
        out: list[Fill] = []
        bought_today: set[str] = set()
        due = [o for o in self.orders if (bar := bars.get(o.symbol)) is not None and o.created < bar.date]
        for order in [o for o in due if o.side == "SELL"]:
            self.orders.remove(order)
            bar = bars[order.symbol]
            pos = self.positions.get(order.symbol)
            if pos is None or not bar.tradable:
                continue
            fill = self._sell_order(pos, order, bar)
            if fill:
                out.append(fill)
        for order in [o for o in due if o.side == "BUY"]:
            self.orders.remove(order)
            bar = bars[order.symbol]
            if not bar.tradable:
                continue
            fill = self._buy(order, bar)
            if fill:
                out.append(fill)
                bought_today.add(order.symbol)
        for symbol in list(self.positions):
            bar = bars.get(symbol)
            pos = self.positions[symbol]
            if bar is None or not bar.tradable or bar.date < pos.buy_date:
                continue
            out += self._exits(pos, bar, exit_rules, fresh=symbol in bought_today and bar.date == pos.buy_date)
        for symbol, pos in self.positions.items():
            bar = bars.get(symbol)
            if bar is not None and bar.tradable:
                self._mark(pos, bar)
        return out

    def end_of_day(self, day: str, benchmark: float | None = None) -> None:
        """Expire the orders that had their day and record the NAV (USDT) and the benchmark."""
        self.orders = [o for o in self.orders if o.created >= day]
        if not self.start_date:
            self.start_date = day
        if benchmark is not None and not _positive(benchmark):
            benchmark = None
        self.history = [h for h in self.history if h["date"] != day]
        self.history.append({"date": day, "nav": round(self.nav(), 2), "benchmark": benchmark})

    def _mark(self, pos: Position, bar: Bar) -> None:
        if bar.date > pos.last_date and bar.date > pos.buy_date:
            pos.days += 1
        pos.last_price = bar.close
        pos.last_date = bar.date
        pos.peak = max(pos.peak, bar.close)
        if pos.trail_pct > 0:
            trail = rules.floor_to_tick(pos.peak * (1 - pos.trail_pct / 100), self.filter_for(pos.symbol).tick_size)
            pos.stop = max(pos.stop or 0.0, trail)

    def _buy(self, order: Order, bar: Bar) -> Fill | None:
        if order.limit is None:
            price = self._slipped(bar.open, "BUY", bar, order.symbol)
        elif bar.low < order.limit:
            price = min(self._slipped(bar.open, "BUY", bar, order.symbol), order.limit) if bar.open < order.limit else order.limit
        else:
            return None
        flt = self.filter_for(order.symbol)
        affordable = rules.floor_to_step(max(self.cash, 0.0) / (price * (1 + self.fee_pct / 100)), flt.step_size)
        qty = min(order.qty, affordable)
        why = rules.order_rejection(qty, price, flt)
        if why:
            self._reject(bar.date, order.symbol, "BUY", why + (" (không đủ tiền)" if affordable < order.qty else ""))
            return None
        value = qty * price
        fee = self._fee(value)
        self.cash -= value + fee
        stop = order.stop or (rules.floor_to_tick(price * (1 - order.stop_pct / 100), flt.tick_size)
                              if order.stop_pct else 0.0)
        self._add(order.symbol, qty, value + fee, bar.date, stop=stop, target1=order.target1,
                  target2=order.target2, trail_pct=order.trail_pct, last_price=bar.close)
        fill = Fill(bar.date, order.symbol, "BUY", qty, price, _round8(fee), order.reason or "mua theo tín hiệu")
        self.fills.append(fill)
        return fill

    def _add(self, symbol: str, qty: float, cost: float, day: str, *, stop: float | None,
             target1: float | None, target2: float | None, trail_pct: float, last_price: float) -> Position:
        pos = self.positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol, qty=qty, avg_price=cost / qty, buy_date=day, stop=float(stop or 0.0),
                           target1=target1, target2=target2, trail_pct=float(trail_pct or 0.0),
                           last_price=last_price, last_date=day, peak=last_price)
            self.positions[symbol] = pos
            return pos
        # Adding to a holding: the average cost moves; levels already set stay, missing ones fill in.
        total = pos.qty + qty
        pos.avg_price = (pos.avg_price * pos.qty + cost) / total
        pos.qty = total
        pos.stop = pos.stop or float(stop or 0.0)
        pos.target1 = pos.target1 if pos.target1 is not None else target1
        pos.target2 = pos.target2 if pos.target2 is not None else target2
        pos.trail_pct = pos.trail_pct or float(trail_pct or 0.0)
        return pos

    def _sell(self, pos: Position, qty: float, price: float, day: str, reason: str) -> Fill | None:
        """Sell ``qty`` (everything when the rest would be dust) at ``price``; None when the
        exchange would refuse the order (below the minimums)."""
        flt = self.filter_for(pos.symbol)
        qty = min(rules.floor_to_step(qty, flt.step_size), pos.qty)
        if qty < pos.qty and self._dust_after(pos, qty, price):
            qty = pos.qty
        why = rules.order_rejection(qty, price, flt)
        if why:
            self._reject(day, pos.symbol, "SELL", why)
            return None
        value = qty * price
        fee = self._fee(value)
        self.cash += value - fee
        pnl = value - fee - pos.avg_price * qty
        pos.qty -= qty
        if pos.qty <= _QTY_EPS * max(1.0, qty):
            self.positions.pop(pos.symbol, None)
        fill = Fill(day, pos.symbol, "SELL", qty, price, _round8(fee), reason, _round8(pnl))
        self.fills.append(fill)
        return fill

    def _sell_order(self, pos: Position, order: Order, bar: Bar) -> Fill | None:
        symbol = pos.symbol
        if order.limit is None:
            price = self._slipped(bar.open, "SELL", bar, symbol)
        elif bar.high > order.limit:
            price = max(self._slipped(bar.open, "SELL", bar, symbol), order.limit) if bar.open > order.limit else order.limit
        else:
            return None
        return self._sell(pos, min(order.qty, pos.qty), price, bar.date, order.reason or "bán theo tín hiệu")

    def _target_fill(self, target: float, bar: Bar, symbol: str) -> float | None:
        """Where a resting sell limit at ``target`` fills on this candle, or None."""
        if bar.open > target:
            return max(self._slipped(bar.open, "SELL", bar, symbol), target)
        if bar.high > target:
            return target
        return None

    def _exits(self, pos: Position, bar: Bar, exit_rules: ExitRules, *, fresh: bool) -> list[Fill]:
        """Stops, targets and the time stop of one candle. ``fresh``: bought at this candle,
        so only the stop is checked (its high may have come before the fill)."""
        if pos.stop and bar.low <= pos.stop:
            raw = min(bar.open, pos.stop)
            label = "chạm stop hoà vốn sau T1" if pos.t1_done else ("chạm trailing stop" if pos.trail_pct else "chạm stop")
            fill = self._sell(pos, pos.qty, self._slipped(raw, "SELL", bar, pos.symbol), bar.date, label)
            return [fill] if fill else []
        if fresh:
            return []
        out: list[Fill] = []
        if pos.target1 and not pos.t1_done:
            price = self._target_fill(pos.target1, bar, pos.symbol)
            if price is not None:
                part = pos.qty if exit_rules.t1_fraction >= 1 else pos.qty * exit_rules.t1_fraction
                fill = self._sell(pos, part, price, bar.date, f"chốt {exit_rules.t1_fraction:.0%} tại T1")
                if fill:
                    out.append(fill)
                if pos.symbol in self.positions:
                    pos.t1_done = True
                    if exit_rules.breakeven_after_t1:
                        pos.stop = max(pos.stop or 0.0, pos.avg_price)
        if pos.symbol in self.positions and pos.t1_done and exit_rules.use_t2 and pos.target2:
            price = self._target_fill(pos.target2, bar, pos.symbol)
            if price is not None:
                fill = self._sell(pos, pos.qty, price, bar.date, "chốt phần còn lại tại T2")
                if fill:
                    out.append(fill)
        if (pos.symbol in self.positions and exit_rules.max_hold_days
                and pos.days + 1 >= exit_rules.max_hold_days):
            fill = self._sell(pos, pos.qty, self._slipped(bar.close, "SELL", bar, pos.symbol), bar.date,
                              f"hết {exit_rules.max_hold_days} ngày nắm giữ")
            if fill:
                out.append(fill)
        return out

    # ── target weights (the strategies' output) ──
    def plan_target_weights(self, day: str, weights: dict[str, float], prices: dict[str, float], *,
                            min_trade_pct: float = 1.0, reason: str = "") -> list[Order]:
        """Market orders for the candle after ``day`` that move the book to ``weights``
        (fractions of NAV at ``prices``, the closes of ``day``; the rest is cash).

        Negative or non-finite weights count as 0 (spot: no shorting); weights summing
        above 1 are scaled down. A change smaller than ``min_trade_pct`` % of NAV is
        skipped to save fees, except a full exit. Buys are sized so the fill with
        slippage and fee fits; the cash at the fill caps them again.
        """
        clean = {s.upper(): (float(w) if finite(w) and float(w) > 0 else 0.0) for s, w in weights.items()}
        total = sum(clean.values())
        if total > 1:
            clean = {s: w / total for s, w in clean.items()}
        nav = self.nav(prices)
        if nav <= 0:
            return []
        planned: list[Order] = []
        sells, buys = [], []
        for symbol in sorted(set(clean) | set(self.positions)):
            price = prices.get(symbol)
            if not _positive(price):
                logger.warning("%s: no price for %s; not rebalanced", day, symbol)
                continue
            pos = self.positions.get(symbol)
            current = pos.qty * price if pos else 0.0
            target = clean.get(symbol, 0.0) * nav
            diff = target - current
            if pos and target <= 0:
                if self.is_dust(pos, price):
                    # Unsellable dust (e.g. the remainder of a fee taken in the coin): no
                    # order the exchange would refuse, day after day.
                    logger.info("%s: %s %g is dust below the exchange minimums; not sold", day, symbol, pos.qty)
                    continue
                sells.append(Order(symbol, "SELL", pos.qty, None, day, reason or "thoát theo tỷ trọng mục tiêu", ref=price))
            elif abs(diff) < nav * min_trade_pct / 100:
                continue
            elif diff < 0 and pos:
                sells.append(Order(symbol, "SELL", -diff / price, None, day, reason or "giảm tỷ trọng", ref=price))
            elif diff > 0:
                unit = price * (1 + self.slippage_pct / 100) * (1 + self.fee_pct / 100)
                buys.append(Order(symbol, "BUY", diff / unit, None, day, reason or "tăng tỷ trọng", ref=price))
        for order in sells + buys:
            placed = self.submit(order)
            if placed:
                planned.append(placed)
        return planned

    # ── fills reported by the owner of a real account ──
    def record_fill(self, side: str, symbol: str, qty: float, price: float, day: str, *,
                    fee: float | None = None, stop: float | None = None, target1: float | None = None,
                    target2: float | None = None, trail_pct: float = 0.0, reason: str = "báo khớp") -> Fill:
        """Book a fill the owner made on the exchange. Raises ValueError (Vietnamese message)
        when it cannot have happened: a non-positive quantity or price, a bad date, a
        negative fee, not enough USDT for a BUY or not enough coins for a SELL.

        ``fee`` is the fee actually paid, in USDT (default: ``fee_pct`` of the value).
        The quantity is not checked against the step size: a real fill may be net of a
        fee taken in the coin.
        """
        side = str(side).strip().upper()
        symbol = str(symbol).strip().upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("Lệnh phải là MUA (BUY) hoặc BÁN (SELL).")
        if isinstance(qty, bool) or not _positive(qty):
            raise ValueError("Khối lượng phải là số dương.")
        if isinstance(price, bool) or not _positive(price):
            raise ValueError("Giá khớp không hợp lệ.")
        qty, price = float(qty), float(price)
        try:
            _date.fromisoformat(str(day)[:10])
        except ValueError:
            raise ValueError(f"Ngày khớp không hợp lệ: {day!r} (cần YYYY-MM-DD).") from None
        day = str(day)[:10]
        value = qty * price
        if fee is None:
            fee = self._fee(value)
        elif not finite(fee) or float(fee) < 0:
            raise ValueError("Phí không hợp lệ.")
        fee = float(fee)
        if side == "BUY":
            if value + fee > self.cash + 1e-6:
                raise ValueError(f"Không đủ USDT: lệnh cần {value + fee:,.2f} USDT (gồm phí), "
                                 f"tiền mặt còn {self.cash:,.2f} USDT. Cập nhật số dư nếu đã nạp thêm.")
            self.cash -= value + fee
            self._add(symbol, qty, value + fee, day, stop=stop, target1=target1, target2=target2,
                      trail_pct=trail_pct, last_price=price)
            fill = Fill(day, symbol, "BUY", qty, price, _round8(fee), reason)
            self.fills.append(fill)
            return fill
        pos = self.positions.get(symbol)
        held = pos.qty if pos else 0.0
        if pos is None or qty > held * (1 + 1e-9):
            raise ValueError(f"Tài khoản không có đủ {symbol} để bán (đang có {held:g}).")
        qty = min(qty, pos.qty)
        self.cash += qty * price - fee
        pnl = qty * price - fee - pos.avg_price * qty
        pos.qty -= qty
        if pos.qty <= _QTY_EPS * max(1.0, qty):
            self.positions.pop(symbol, None)
        elif pos.target1 and not pos.t1_done and price >= pos.target1:
            pos.t1_done = True
            pos.stop = max(pos.stop or 0.0, pos.avg_price)
        fill = Fill(day, symbol, "SELL", qty, price, _round8(fee), reason, _round8(pnl))
        self.fills.append(fill)
        return fill

    # ── stats ──
    def stats(self) -> dict:
        sells = [f for f in self.fills if f.side == "SELL" and f.pnl is not None]
        navs = [h["nav"] for h in self.history] or [self.start_cash]
        peak, max_dd = navs[0], 0.0
        for v in navs:
            peak = max(peak, v)
            max_dd = max(max_dd, (peak - v) / peak * 100 if peak else 0.0)
        bench = [h["benchmark"] for h in self.history if h.get("benchmark")]
        bench_ret = (bench[-1] / bench[0] - 1) * 100 if len(bench) >= 2 else None
        nav = self.nav()
        return {
            "nav": round(nav, 2),
            "return_pct": round((nav / self.start_cash - 1) * 100, 2) if self.start_cash else 0.0,
            "benchmark_pct": round(bench_ret, 2) if bench_ret is not None else None,
            "max_drawdown_pct": round(max_dd, 2),
            "days": len(self.history),
            "buys": sum(1 for f in self.fills if f.side == "BUY"),
            "closed": len(sells),
            "win_rate_pct": round(sum(1 for f in sells if f.pnl > 0) / len(sells) * 100, 1) if sells else None,
            "realised_pnl": round(sum(f.pnl for f in sells), 2),
            "fees_paid": round(sum(f.fee for f in self.fills), 2),
        }

    # ── storage ──
    def to_dict(self) -> dict:
        return {
            "cash": self.cash, "start_cash": self.start_cash, "start_date": self.start_date,
            "fee_pct": self.fee_pct, "slippage_pct": self.slippage_pct,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "orders": [asdict(o) for o in self.orders],
            "fills": [asdict(f) for f in self.fills[-MAX_SAVED_FILLS:]],
            "history": self.history[-MAX_SAVED_HISTORY:],
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Ledger:
        """A book from ``to_dict`` output. Fields added since a save take their defaults and
        unknown ones are ignored; a missing or non-finite ``cash``, a non-finite position,
        or a record without its essential fields is damage (KeyError/TypeError/ValueError)."""
        if not isinstance(data, dict):
            raise TypeError(f"a ledger is a JSON object, not {type(data).__name__}")
        cash = float(data["cash"])
        start_cash = float(data.get("start_cash", cash))
        if not (finite(cash) and finite(start_cash)):
            raise ValueError("non-finite cash")
        positions = data.get("positions") or {}
        if not isinstance(positions, dict):
            raise TypeError("positions must be an object")
        built = {k: _build(Position, v) for k, v in positions.items()}
        for p in built.values():
            if not (_positive(p.qty) and _positive(p.avg_price)):
                raise ValueError(f"position {p.symbol} has a non-positive quantity or cost")
            p.qty, p.avg_price = float(p.qty), float(p.avg_price)
        fee_pct = float(data.get("fee_pct", rules.TAKER_FEE_PCT))
        slippage_pct = float(data.get("slippage_pct", rules.SLIPPAGE_PCT))
        if not (finite(fee_pct) and finite(slippage_pct)) or fee_pct < 0 or slippage_pct < 0:
            raise ValueError("invalid fee or slippage")
        return cls(
            cash=cash, start_cash=start_cash, start_date=str(data.get("start_date") or ""),
            fee_pct=fee_pct, slippage_pct=slippage_pct, positions=built,
            orders=[_build(Order, o) for o in data.get("orders") or []],
            fills=[_build(Fill, f) for f in data.get("fills") or []],
            history=[h for h in data.get("history") or [] if isinstance(h, dict) and "date" in h and "nav" in h],
            meta=dict(data.get("meta") or {}),
        )

    def save(self, path: Path) -> None:
        """Write atomically: a temp file in the same folder, fsync'd, then renamed over."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError, AttributeError):
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    @classmethod
    def load(cls, path: Path, start_cash: float, *, fresh_on_error: bool = False) -> Ledger:
        """The book saved at ``path``; a new one with ``start_cash`` only when there is no file.

        A file that exists but cannot be parsed is moved aside to
        ``<name>.corrupt-<timestamp>`` (never deleted) and ``LedgerLoadError`` is raised,
        so a damaged real account is never silently replaced by an empty book on the next
        save. ``fresh_on_error=True`` (for a throwaway paper book) returns a new book
        instead, once the damaged file is safely aside. A file that cannot be read at all
        (permissions, I/O) is left in place and always raises.
        """
        path = Path(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return cls(cash=start_cash, start_cash=start_cash)
        except OSError as exc:
            logger.error("Ledger %s cannot be read: %s", path, exc)
            raise LedgerLoadError(f"Không đọc được sổ giao dịch {path.name}: {exc}") from exc
        try:
            return cls.from_dict(json.loads(raw.decode("utf-8")))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            try:
                aside = _quarantine(path)
            except OSError as move_exc:
                logger.error("Ledger %s is damaged (%s) and cannot be moved aside: %s", path, exc, move_exc)
                raise LedgerLoadError(f"Sổ giao dịch {path.name} bị hỏng ({exc}) và không chuyển sang bên được; "
                                      "giữ nguyên file, cần kiểm tra thủ công.") from exc
            logger.error("Ledger %s is damaged (%s: %s); moved to %s", path, type(exc).__name__, exc, aside)
            if fresh_on_error:
                return cls(cash=start_cash, start_cash=start_cash)
            raise LedgerLoadError(f"Sổ giao dịch {path.name} bị hỏng ({type(exc).__name__}: {exc}); "
                                  f"đã chuyển sang {aside.name}, không xoá. Khôi phục file đó rồi chạy lại.") from exc


def _build(kind, raw):
    """A dataclass from a saved record: unknown keys dropped, missing ones defaulted."""
    if not isinstance(raw, dict):
        raise TypeError(f"{kind.__name__} record must be an object")
    names = {f.name for f in fields(kind)}
    return kind(**{k: v for k, v in raw.items() if k in names})


def _quarantine(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    aside = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 1
    while aside.exists():
        aside = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
        n += 1
    path.rename(aside)
    return aside
