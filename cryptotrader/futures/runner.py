"""One day of a futures book (fills, stop, funding, then the next decision), and the backtest over it.

``step`` is what the bot does every day and what ``backtest`` repeats over
history, so the two cannot drift apart. On top of the strategy it applies the
account rules of a small account:

- **Goal** (``goal_profit``, default +100 USDT): when the equity reaches the
  starting capital plus the goal, the position is closed at the next open and
  the book stops opening new positions (``meta["halted"] = "goal"``): take the
  profit off the exchange. The owner resumes it explicitly.
- **Floor** (``stop_equity``, default 5 USDT = half of 10): when the equity
  falls to it, the position is closed at the next open and the book stops
  (``"floor"``), instead of trading the rest away.
- **Drawdown scaling** (``dd_scale``, 0 = off): below the equity peak the
  position is sized down linearly, to 25% of normal at a drawdown of
  ``dd_scale`` (0.3 = -30%) and beyond; full size again at a new peak. On
  2020-2026 data it roughly halves the maximum drawdown and takes a third
  off the return (``research/ASSESSMENT.md``).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from cryptotrader.futures.book import Bar, FuturesBook
from cryptotrader.futures.strategy import LongShortSma


@dataclass(frozen=True)
class AccountPolicy:
    goal_profit: float = 100.0      # USDT above the starting capital; 0 = no goal
    stop_equity: float = 5.0        # stop trading at or below this equity; 0 = never
    dd_scale: float = 0.0           # drawdown at which exposure is cut to 25% (0.3 = -30%); 0 = off

    def exposure(self, nav: float, peak: float) -> float:
        """The size multiplier for a NAV below its peak (1.0 at or above it)."""
        if not self.dd_scale or peak <= 0 or nav >= peak:
            return 1.0
        return max(0.25, 1.0 + (nav / peak - 1.0) / self.dd_scale)


def bar_of(row) -> Bar:
    return Bar(pd.Timestamp(row["Date"]).strftime("%Y-%m-%d"), float(row["Open"]), float(row["High"]),
               float(row["Low"]), float(row["Close"]))


def step(book: FuturesBook, strategy: LongShortSma, frame: pd.DataFrame, policy: AccountPolicy,
         funding: float | None = None, btc_frame: pd.DataFrame | None = None) -> list[str]:
    """Process the frame's last candle (day D) for ``book``, then plan for D+1.

    ``frame``: closed daily candles up to and including D (Date/Open/High/Low/Close);
    ``btc_frame``: BTCUSDT's up to D, for the strategy's BTC filter."""
    bar = bar_of(frame.iloc[-1])
    notes = book.process_day(bar, funding)
    if not bar.tradable:
        return notes
    nav = book.last_nav
    halted = book.meta.get("halted")
    if halted:
        if book.qty:
            book.plan(bar.date, 0.0, None, "đóng vị thế (đang dừng)")
        return notes
    if policy.goal_profit and nav >= book.capital + policy.goal_profit:
        book.meta["halted"] = "goal"
        book.meta["halted_on"] = bar.date
        if book.qty:
            book.plan(bar.date, 0.0, None, "chốt lời: đạt mục tiêu")
        notes.append(f"🎯 Đạt mục tiêu: vốn {nav:.2f} USDT ≥ {book.capital + policy.goal_profit:.2f}. "
                     "Đóng vị thế ở giá mở cửa tới và dừng mở lệnh mới.")
        return notes
    floor = float(book.meta.get("stop_equity", policy.stop_equity) or 0.0)   # a resumed book has its own
    if floor and nav <= floor:
        book.meta["halted"] = "floor"
        book.meta["halted_on"] = bar.date
        if book.qty:
            book.plan(bar.date, 0.0, None, "dừng lỗ tổng")
        notes.append(f"🛑 Vốn còn {nav:.2f} USDT ≤ mức dừng {floor:g} USDT: đóng vị thế và "
                     "dừng giao dịch.")
        return notes
    peak = max([book.capital, *(float(h["nav"]) for h in book.history)])
    decision = strategy.decide(frame, nav, book.qty, book.spec, btc_frame=btc_frame,
                               exposure=policy.exposure(nav, peak))
    decision["day"] = bar.date
    book.meta["signal"] = {k: v for k, v in decision.items() if k != "vol" or v is None or np.isfinite(v)}
    if decision["trade"]:
        book.plan(bar.date, decision["qty"], decision["stop_dist"], decision["reason"].split(";")[0])
    return notes


def backtest(frame: pd.DataFrame, strategy: LongShortSma | None = None, capital: float = 10.0,
             policy: AccountPolicy | None = None, start: str | None = None, end: str | None = None,
             symbol: str = "BTCUSDT", stop_at_halt: bool = False,
             btc: pd.DataFrame | None = None) -> FuturesBook:
    """Run the daily ``step`` over ``frame`` (with an optional ``funding`` column, a fraction per day).

    The book starts on ``start`` (the strategy still sees the candles before it);
    ``stop_at_halt`` ends the run when the goal or the floor is reached; ``btc``: BTCUSDT's
    daily candles for the BTC filter (without them a filtered altcoin never trades)."""
    strategy = strategy or LongShortSma()
    policy = policy or AccountPolicy(goal_profit=0.0, stop_equity=0.0)
    frame = frame.sort_values("Date").reset_index(drop=True)
    dates = pd.to_datetime(frame["Date"])
    first = int(np.searchsorted(dates.to_numpy(), np.datetime64(pd.Timestamp(start)))) if start else 0
    last = int(np.searchsorted(dates.to_numpy(), np.datetime64(pd.Timestamp(end)), side="right")) if end else len(frame)
    book = FuturesBook(symbol=symbol, capital=capital)
    has_funding = "funding" in frame.columns
    window = strategy.warmup_days
    btc_dates = None
    if btc is not None and getattr(strategy, "btc_filter", False):
        btc = btc.sort_values("Date").reset_index(drop=True)
        btc_dates = pd.to_datetime(btc["Date"]).to_numpy()
    for i in range(first, last):
        lo = max(0, i + 1 - window)
        funding = float(frame["funding"].iloc[i]) if has_funding else None
        btc_frame = None
        if btc_dates is not None:
            j = int(np.searchsorted(btc_dates, dates.iloc[i].to_datetime64(), side="right"))
            btc_frame = btc.iloc[max(0, j - window):j]
        step(book, strategy, frame.iloc[lo:i + 1], policy, funding, btc_frame=btc_frame)
        if stop_at_halt and book.meta.get("halted"):
            break
    return book


def nav_series(book: FuturesBook) -> pd.Series:
    return pd.Series([h["nav"] for h in book.history], index=pd.to_datetime([h["date"] for h in book.history]))


def summary(navs: pd.Series) -> dict:
    """CAGR, max drawdown, Sharpe (daily, 365) and final multiple of a NAV series."""
    if len(navs) < 2 or navs.iloc[0] <= 0:
        return {"cagr": 0.0, "mdd": 0.0, "sharpe": 0.0, "multiple": 1.0, "days": len(navs)}
    rets = navs.pct_change().dropna()
    years = len(navs) / 365
    end = float(navs.iloc[-1])
    cagr = (end / navs.iloc[0]) ** (1 / years) - 1 if end > 0 else -1.0
    mdd = float((navs / navs.cummax() - 1).min())
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365)) if rets.std() > 0 else 0.0
    return {"cagr": float(cagr), "mdd": mdd, "sharpe": sharpe, "multiple": end / float(navs.iloc[0]),
            "days": len(navs)}
