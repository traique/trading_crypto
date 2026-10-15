"""A USDⓈ-M perpetual futures book in USDT: one symbol, one position (long, short or flat).

The same object serves the backtest and the bot's paper book, so a backtest is
what the paper book would have done:

- A decision on day D's close is a *target position* (signed quantity and a
  stop distance), placed as ``pending`` with ``created = D``. It fills at the
  open of the next candle (D+1), moved ``slippage`` against the trader; never
  on D's own candle.
- Fills pay the taker fee on their value. Closing or reducing realises PnL
  into ``cash`` (the wallet balance); flipping long <-> short closes first.
- After the open, the day's candle is checked against the protective stop and
  the liquidation price, worst price first (the low for a long, the high for a
  short). A stop that gapped through fills at the open. When the liquidation
  price comes before the stop (stop too wide for the leverage), the book is
  liquidated: equity drops to 0.
- Longs pay funding on their value at the close (``funding`` of the day, a
  fraction; negative means longs are paid). Shorts receive it.
- Equity (NAV) = cash + unrealised PnL at the close.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from cryptotrader.futures import rules


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float

    @property
    def tradable(self) -> bool:
        return all(math.isfinite(v) and v > 0 for v in (self.open, self.high, self.low, self.close))


@dataclass
class FuturesBook:
    symbol: str
    capital: float                       # starting equity, USDT
    cash: float = 0.0                    # wallet balance (realised PnL and costs included)
    qty: float = 0.0                     # signed: > 0 long, < 0 short
    entry: float = 0.0                   # average entry price of the open position
    stop: float | None = None            # protective stop price of the open position
    pending: dict | None = None          # {"created", "qty", "stop_dist", "note"}
    history: list[dict] = field(default_factory=list)    # {"date", "nav", "close", "qty"}
    trades: list[dict] = field(default_factory=list)     # fills, stops, liquidations
    meta: dict = field(default_factory=dict)
    fee_pct: float | None = None
    slippage_pct: float | None = None

    def __post_init__(self) -> None:
        if not self.cash and not self.history and not self.trades:
            self.cash = float(self.capital)
        if self.fee_pct is None:
            self.fee_pct = rules.taker_fee_pct()
        if self.slippage_pct is None:
            self.slippage_pct = rules.slippage_pct()

    # ── reading ──
    @property
    def spec(self) -> rules.FuturesSpec:
        return rules.spec_for(self.symbol)

    @property
    def side(self) -> str:
        return "LONG" if self.qty > 0 else "SHORT" if self.qty < 0 else "FLAT"

    def equity(self, price: float) -> float:
        return self.cash + (self.qty * (price - self.entry) if self.qty else 0.0)

    def leverage(self, price: float) -> float:
        eq = self.equity(price)
        return abs(self.qty) * price / eq if self.qty and eq > 0 else 0.0

    def liquidation(self) -> float | None:
        return rules.liquidation_price(self.cash, self.qty, self.entry, self.spec.mmr)

    @property
    def last_nav(self) -> float:
        return float(self.history[-1]["nav"]) if self.history else float(self.cash)

    # ── fills ──
    def _fill(self, day: str, delta: float, price: float, reason: str) -> None:
        """Trade ``delta`` (signed) at ``price`` (already slipped)."""
        if not delta:
            return
        fee = abs(delta) * price * self.fee_pct / 100
        pnl = 0.0
        if self.qty and (delta > 0) != (self.qty > 0):          # reducing or closing
            closed = min(abs(delta), abs(self.qty)) * (1 if self.qty > 0 else -1)
            pnl = closed * (price - self.entry)
            self.cash += pnl
            remaining = self.qty + delta
            if abs(remaining) < 1e-12 or (remaining > 0) != (self.qty > 0):
                # Closed (a flip is split by the caller, so nothing is left over here).
                self.qty, self.entry, self.stop = 0.0, 0.0, None
            else:
                self.qty = remaining
        else:                                                   # opening or adding
            new = self.qty + delta
            self.entry = (self.entry * self.qty + price * delta) / new if self.qty else price
            self.qty = new
        self.cash -= fee
        self.trades.append({"date": day, "side": "BUY" if delta > 0 else "SELL", "qty": round(abs(delta), 10),
                            "price": round(price, 8), "fee": round(fee, 8), "pnl": round(pnl, 8),
                            "reason": reason, "position": round(self.qty, 10)})

    def _slipped(self, price: float, delta: float) -> float:
        return price * (1 + self.slippage_pct / 100 * (1 if delta > 0 else -1))

    def _move_to(self, day: str, target: float, price: float, reason: str) -> None:
        if self.qty and target and (target > 0) != (self.qty > 0):     # flip: close, then open
            self._fill(day, -self.qty, self._slipped(price, -self.qty), "đóng để đảo chiều")
        delta = target - self.qty
        if abs(delta) > 1e-12:
            self._fill(day, delta, self._slipped(price, delta), reason)

    def close_now(self, day: str, price: float, reason: str) -> None:
        """Close the whole position at ``price`` (plus slippage), e.g. on a halt."""
        if self.qty:
            self._fill(day, -self.qty, self._slipped(price, -self.qty), reason)
        self.pending = None

    # ── one day ──
    def process_day(self, bar: Bar, funding: float | None = None) -> list[str]:
        """Fill the pending target at the open, check stop/liquidation, charge funding, record NAV.

        ``funding``: the day's total funding rate as a fraction (None: the default of
        ``rules.funding_pct_per_day`` charged to longs only). Returns the notes of the day."""
        notes: list[str] = []
        if not bar.tradable:
            return notes
        pending = self.pending
        if pending and pending.get("created", "") < bar.date:
            self.pending = None
            target = float(pending.get("qty") or 0.0)
            if abs(target - self.qty) > 1e-12:
                self._move_to(bar.date, target, bar.open, pending.get("note") or "theo tín hiệu")
            dist = pending.get("stop_dist")
            if self.qty and dist:
                self.stop = self.entry - math.copysign(float(dist), self.qty)
            elif not self.qty:
                self.stop = None
        if self.qty:
            long = self.qty > 0
            worst = bar.low if long else bar.high
            liq = self.liquidation()
            stop = self.stop
            hit_stop = stop is not None and (worst <= stop if long else worst >= stop)
            hit_liq = liq is not None and (worst <= liq if long else worst >= liq)
            stop_first = hit_stop and (liq is None or (stop > liq if long else stop < liq))
            if stop_first:
                px = min(bar.open, stop) if long else max(bar.open, stop)
                side = self.side
                self._fill(bar.date, -self.qty, self._slipped(px, -self.qty), "cắt lỗ (stop)")
                notes.append(f"{side} {self.symbol} chạm cắt lỗ {stop:.6g}")
            elif hit_liq:
                side, qty = self.side, self.qty
                self.trades.append({"date": bar.date, "side": "SELL" if qty > 0 else "BUY",
                                    "qty": round(abs(qty), 10), "price": round(liq, 8), "fee": 0.0,
                                    "pnl": round(-self.cash, 8), "reason": "THANH LÝ", "position": 0.0})
                self.cash, self.qty, self.entry, self.stop = 0.0, 0.0, 0.0, None
                notes.append(f"⚠️ {side} {self.symbol} BỊ THANH LÝ ở {liq:.6g}")
        if self.qty:
            rate = funding if funding is not None else (rules.funding_pct_per_day() / 100 if self.qty > 0 else 0.0)
            self.cash -= self.qty * bar.close * rate
        nav = self.equity(bar.close)
        self.history.append({"date": bar.date, "nav": round(nav, 8), "close": bar.close, "qty": self.qty})
        return notes

    def plan(self, day: str, target_qty: float, stop_dist: float | None, note: str = "") -> None:
        """The target position for the next candle (replaces any earlier plan)."""
        self.pending = {"created": day, "qty": float(target_qty), "stop_dist": stop_dist, "note": note}

    # ── persistence ──
    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "capital": self.capital, "cash": self.cash, "qty": self.qty,
                "entry": self.entry, "stop": self.stop, "pending": self.pending, "history": self.history,
                "trades": self.trades, "meta": self.meta, "fee_pct": self.fee_pct,
                "slippage_pct": self.slippage_pct}

    @classmethod
    def from_dict(cls, data: dict) -> FuturesBook:
        book = cls(symbol=data["symbol"], capital=float(data["capital"]), cash=float(data.get("cash", 0.0)),
                   qty=float(data.get("qty", 0.0)), entry=float(data.get("entry", 0.0)), stop=data.get("stop"),
                   pending=data.get("pending"), history=list(data.get("history") or []),
                   trades=list(data.get("trades") or []), meta=dict(data.get("meta") or {}),
                   fee_pct=data.get("fee_pct"), slippage_pct=data.get("slippage_pct"))
        if not book.history and not book.trades and "cash" in data:
            book.cash = float(data["cash"])
        return book
