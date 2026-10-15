"""The popular MetaTrader 5 Expert Advisor styles, rebuilt and tested on Binance perpetual futures.

    python -m research.intraday_study [--symbols BTCUSDT,ETHUSDT,...] [--download] [--workers 8]

Seven EA styles, with the textbook settings found in most free/commercial EAs,
fixed BEFORE testing (nothing here is tuned to the data):

========================  =====  ==================================================================
style                     bars   rule
========================  =====  ==================================================================
breakout_h1               H1     close above the 24-bar high -> long, below the 24-bar low -> short;
                                 stop 1.5 ATR(14), take profit 3 ATR (2R), out after 48 bars
turtle_h4                 H4     close above the 20-bar high / below the 20-bar low; exit on the
                                 10-bar opposite channel; stop 2 ATR(14)
ma_cross_h1               H1     EMA20 crosses EMA50: long above / short below (always in, reverses);
                                 stop 2 ATR(14)
rsi_reversion_h1          H1     RSI(14) < 30 above EMA200 -> long; > 70 below EMA200 -> short;
                                 exit when RSI crosses 50; stop 2 ATR; out after 48 bars
bollinger_reversion_h1    H1     close below the lower band (20, 2) -> long, above the upper -> short;
                                 exit at the middle band; stop 2 ATR; out after 48 bars
grid_h1                   H1     long grid: buy at the open, then every 1% lower (5 entries of equal
                                 size, 0.4x equity each); close the basket at +1% over its average
                                 price; stop the basket 6% under the first entry
martingale_h1             H1     the same grid, each entry twice the previous (0.1, 0.2, 0.4, 0.8,
                                 1.6x equity) - the classic MT5 martingale basket
========================  =====  ==================================================================

Money management of the single-position styles: risk 2% of equity per trade
(the distance to the stop), at most 3x leverage; an order under the exchange
minimum is raised to it when that needs at most 3x, else skipped. Grid entries
under the minimum are raised to it; no basket exceeds 10x.

Costs and mechanics: taker fee 0.05% + slippage 0.05% per market fill (limit
fills of the grids pay the fee, no slippage), the real 8-hourly funding,
fills at the next bar's open, stops before targets when a bar touches both,
gaps fill at the open, liquidation when equity falls to the maintenance
margin (the account is then lost). Data: Binance USDⓈ-M 1h archives
(``data.binance.vision``), cached under ``research/data/futures_1h``.
"""

from __future__ import annotations

import argparse
import io
import math
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from cryptotrader.futures.rules import spec_for

CACHE = Path(__file__).resolve().parent / "data" / "futures_1h"
ARCHIVE = "https://data.binance.vision/data/futures/um/monthly"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")
TRAIN = ("2021-01-01", "2023-12-31 23:00")
TEST = ("2024-01-01", "2026-09-30 23:00")
FEE, SLIP = 0.0005, 0.0005
RISK, LEV_CAP, MIN_ORDER_LEV, GRID_LEV_CAP = 0.02, 3.0, 3.0, 10.0


# ── data ──
def _rows(text: str | None) -> list[list[str]]:
    return [line.split(",") for line in (text or "").strip().splitlines() if line[:1].isdigit()]


def _get(session, url: str) -> str | None:
    for attempt in range(6):
        try:
            response = session.get(url, timeout=60)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                return archive.read(archive.namelist()[0]).decode()
        except Exception:  # noqa: BLE001 - retried, then raised
            if attempt == 5:
                raise
            time.sleep(2 * (attempt + 1))
    return None


def download(symbol: str) -> pd.DataFrame:
    """Hourly candles and funding of ``symbol`` from the Binance archive, cached as CSV."""
    import requests

    session = requests.Session()
    months = [str(m) for m in pd.period_range("2020-01", pd.Timestamp.utcnow().strftime("%Y-%m"), freq="M")]
    candles, funding = [], []
    for month in months:
        candles += _rows(_get(session, f"{ARCHIVE}/klines/{symbol}/1h/{symbol}-1h-{month}.zip"))
        funding += _rows(_get(session, f"{ARCHIVE}/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip"))
    frame = pd.DataFrame([(int(r[0]) // 3_600_000 * 3_600_000, *map(float, r[1:6])) for r in candles],
                         columns=["t", "Open", "High", "Low", "Close", "Volume"]).drop_duplicates("t").sort_values("t")
    rates = pd.DataFrame([(int(r[0]) // 3_600_000 * 3_600_000, float(r[2])) for r in funding],
                         columns=["t", "funding"]).groupby("t").sum()
    frame = frame.merge(rates, left_on="t", right_index=True, how="left").fillna({"funding": 0.0})
    CACHE.mkdir(parents=True, exist_ok=True)
    frame.to_csv(CACHE / f"{symbol}.csv", index=False)
    return frame


def load(symbol: str, bars: str = "1h") -> pd.DataFrame:
    frame = pd.read_csv(CACHE / f"{symbol}.csv")
    frame.index = pd.to_datetime(frame["t"], unit="ms")
    frame = frame.drop(columns="t")
    if bars == "1h":
        return frame
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum", "funding": "sum"}
    return frame.resample(bars).agg(agg).dropna(subset=["Open"])


# ── indicators ──
def atr(f: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = f["Close"].shift()
    tr = pd.concat([f["High"] - f["Low"], (f["High"] - prev).abs(), (f["Low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


@dataclass
class Plan:
    """What a single-position style asks for at a bar's close (arrays over the bars)."""
    entry: np.ndarray        # +1 / -1 / 0: open this direction at the next open (when flat)
    exit_long: np.ndarray    # bool: close a long at the next open
    exit_short: np.ndarray   # bool: close a short at the next open
    stop: np.ndarray         # stop distance (price) for an entry on this bar
    target: np.ndarray       # take-profit distance, nan = none
    max_bars: int            # time exit, 0 = none
    reverse: bool = False    # an opposite entry closes and reverses


def plan_breakout_h1(f):
    c, a = f["Close"], atr(f)
    hi, lo = f["High"].rolling(24).max().shift(), f["Low"].rolling(24).min().shift()
    entry = np.where(c > hi, 1, np.where(c < lo, -1, 0))
    none = np.zeros(len(f), bool)
    return Plan(entry, none, none, (1.5 * a).to_numpy(), (3 * a).to_numpy(), 48)


def plan_turtle_h4(f):
    c, a = f["Close"], atr(f)
    hi, lo = f["High"].rolling(20).max().shift(), f["Low"].rolling(20).min().shift()
    xhi, xlo = f["High"].rolling(10).max().shift(), f["Low"].rolling(10).min().shift()
    entry = np.where(c > hi, 1, np.where(c < lo, -1, 0))
    return Plan(entry, (c < xlo).to_numpy(), (c > xhi).to_numpy(), (2 * a).to_numpy(),
                np.full(len(f), np.nan), 0)


def plan_ma_cross_h1(f):
    c, a = f["Close"], atr(f)
    fast, slow = c.ewm(span=20, adjust=False).mean(), c.ewm(span=50, adjust=False).mean()
    up = (fast > slow) & (fast.shift() <= slow.shift())
    down = (fast < slow) & (fast.shift() >= slow.shift())
    entry = np.where(up, 1, np.where(down, -1, 0))
    return Plan(entry, down.to_numpy(), up.to_numpy(), (2 * a).to_numpy(), np.full(len(f), np.nan), 0, reverse=True)


def plan_rsi_h1(f):
    c, a, r = f["Close"], atr(f), rsi(f["Close"])
    trend = c.ewm(span=200, adjust=False).mean()
    entry = np.where((r < 30) & (c > trend), 1, np.where((r > 70) & (c < trend), -1, 0))
    return Plan(entry, (r > 50).to_numpy(), (r < 50).to_numpy(), (2 * a).to_numpy(),
                np.full(len(f), np.nan), 48)


def plan_bollinger_h1(f):
    c, a = f["Close"], atr(f)
    mid, sd = c.rolling(20).mean(), c.rolling(20).std()
    entry = np.where(c < mid - 2 * sd, 1, np.where(c > mid + 2 * sd, -1, 0))
    return Plan(entry, (c >= mid).to_numpy(), (c <= mid).to_numpy(), (2 * a).to_numpy(),
                np.full(len(f), np.nan), 48)


STYLES = {
    "breakout_h1": ("1h", plan_breakout_h1),
    "turtle_h4": ("4h", plan_turtle_h4),
    "ma_cross_h1": ("1h", plan_ma_cross_h1),
    "rsi_reversion_h1": ("1h", plan_rsi_h1),
    "bollinger_reversion_h1": ("1h", plan_bollinger_h1),
    "grid_h1": ("1h", None),
    "martingale_h1": ("1h", None),
}


# ── the account ──
@dataclass
class Result:
    navs: np.ndarray
    times: np.ndarray
    trades: list           # realised PnL per closed trade / basket
    liquidated: bool
    halted: str | None


def _size(equity, price, stop_dist, spec):
    """Quantity for a 2% risk at ``stop_dist``, capped at 3x, raised to the exchange minimum up to 3x."""
    if equity <= 0 or not (stop_dist > 0) or not math.isfinite(stop_dist):
        return 0.0
    qty = min(RISK * equity / stop_dist, LEV_CAP * equity / price)
    qty = spec.floor_qty(qty)
    if qty * price < spec.min_notional:
        need = spec.min_qty_at(price)
        qty = need if need * price <= MIN_ORDER_LEV * equity + 1e-9 else 0.0
    return qty


def _liq_price(cash, qty, entry, mmr):
    if not qty:
        return None
    side = 1.0 if qty > 0 else -1.0
    p = (qty * entry - cash) / (qty * (1 - side * mmr))
    return p if p > 0 else None


def run_single(f: pd.DataFrame, plan: Plan, symbol: str, capital: float, start, end,
               goal: float | None = None, floor: float | None = None) -> Result:
    spec = spec_for(symbol)
    idx = f.index
    lo_i, hi_i = idx.searchsorted(pd.Timestamp(start)), idx.searchsorted(pd.Timestamp(end), side="right")
    o, h, low, c, fund = (f[k].to_numpy() for k in ("Open", "High", "Low", "Close", "funding"))
    cash, qty, entry, stop, target, held = float(capital), 0.0, 0.0, None, None, 0
    pending = None              # ("open", dir) / ("close",) / ("reverse", dir)
    navs, trades, liquidated, halted = [], [], False, None
    for i in range(lo_i, hi_i):
        # 1. orders at the open
        if pending is not None:
            kind = pending[0]
            if qty and kind in ("close", "reverse"):
                px = o[i] * (1 - SLIP * np.sign(qty))
                pnl = qty * (px - entry) - abs(qty) * px * FEE
                cash += pnl
                trades.append(pnl)
                qty = 0.0
            if kind in ("open", "reverse") and not qty:
                d = pending[1]
                k = i - 1
                q = _size(cash, o[i], plan.stop[k], spec)
                if q:
                    px = o[i] * (1 + SLIP * d)
                    qty, entry, held = d * q, px, 0
                    cash -= q * px * FEE
                    stop = px - d * plan.stop[k]
                    target = px + d * plan.target[k] if np.isfinite(plan.target[k]) else None
            pending = None
        # 2. stop / target / liquidation inside the bar
        if qty:
            d = 1 if qty > 0 else -1
            worst, best = (low[i], h[i]) if d > 0 else (h[i], low[i])
            liq = _liq_price(cash, qty, entry, spec.mmr)
            hit_stop = (worst - stop) * d <= 0
            hit_liq = liq is not None and (worst - liq) * d <= 0
            if hit_stop and (liq is None or (stop - liq) * d > 0):
                px = (min(o[i], stop) if d > 0 else max(o[i], stop)) * (1 - SLIP * d)
                pnl = qty * (px - entry) - abs(qty) * px * FEE
                cash += pnl
                trades.append(pnl)
                qty = 0.0
            elif hit_liq:
                trades.append(-cash)
                cash, qty, liquidated = 0.0, 0.0, True
            elif target is not None and (best - target) * d >= 0:
                px = max(o[i], target) if d > 0 else min(o[i], target)
                pnl = qty * (px - entry) - abs(qty) * px * FEE
                cash += pnl
                trades.append(pnl)
                qty = 0.0
        # 3. funding, NAV
        if qty:
            cash -= qty * c[i] * fund[i]
            held += 1
        nav = cash + (qty * (c[i] - entry) if qty else 0.0)
        navs.append(nav)
        if goal and nav >= goal:
            halted = "goal"
            break
        if (floor is not None and nav <= floor) or nav <= 0:
            halted = "floor"
            break
        # 4. decisions at the close
        sig = plan.entry[i]
        if qty:
            d = 1 if qty > 0 else -1
            if plan.reverse and sig == -d:
                pending = ("reverse", -d)
            elif (d > 0 and plan.exit_long[i]) or (d < 0 and plan.exit_short[i]) or \
                    (plan.max_bars and held >= plan.max_bars):
                pending = ("close",)
        elif sig and np.isfinite(plan.stop[i]):
            pending = ("open", int(sig))
    return Result(np.array(navs), idx[lo_i:lo_i + len(navs)].to_numpy(), trades, liquidated, halted)


def run_grid(f: pd.DataFrame, symbol: str, capital: float, start, end, sizes=(0.4,) * 5,
             step=0.01, take=0.01, stop=0.06, goal=None, floor=None) -> Result:
    """Long grid / martingale basket: entries at the open, then every ``step`` lower; basket TP / stop."""
    spec = spec_for(symbol)
    idx = f.index
    lo_i, hi_i = idx.searchsorted(pd.Timestamp(start)), idx.searchsorted(pd.Timestamp(end), side="right")
    o, h, low, c, fund = (f[k].to_numpy() for k in ("Open", "High", "Low", "Close", "funding"))
    cash, qty, cost, anchor, filled, base_eq = float(capital), 0.0, 0.0, None, 0, 0.0
    navs, trades, liquidated, halted = [], [], False, None

    def level_qty(k, price):
        q = spec.floor_qty(sizes[k] * base_eq / price)
        if q * price < spec.min_notional:
            q = spec.min_qty_at(price)
        if (qty + q) * price > GRID_LEV_CAP * base_eq:
            return 0.0
        return q

    for i in range(lo_i, hi_i):
        if anchor is None and cash > 0:                       # start a basket at the open
            anchor, filled, base_eq = o[i], 0, cash
            q = level_qty(0, o[i])
            if q:
                px = o[i] * (1 + SLIP)
                qty, cost, filled = q, q * px, 1
                cash -= q * px * FEE
            else:
                anchor = None
        if qty:
            # Order inside the bar is unknown: assume the worst for the basket. The stop is checked
            # first; a bar that fills deeper levels cannot also take the profit (that would assume
            # it went down first and up after); only the open of a new basket is known to come first.
            opened_now = filled == 1 and cost == qty * o[i] * (1 + SLIP)
            avg = cost / qty
            stop_px = anchor * (1 - stop)
            new_fill = False
            while filled < len(sizes) and low[i] <= anchor * (1 - step * filled) and stop_px < anchor * (1 - step * filled):
                px = min(o[i], anchor * (1 - step * filled))
                q = level_qty(filled, px)
                filled += 1
                if q:
                    qty += q
                    cost += q * px
                    cash -= q * px * FEE
                    new_fill = True
            avg = cost / qty
            liq = _liq_price(cash, qty, avg, spec.mmr)
            if low[i] <= stop_px and (liq is None or stop_px > liq):
                px = min(o[i], stop_px) * (1 - SLIP)
                pnl = qty * (px - avg) - qty * px * FEE
                cash += pnl
                trades.append(pnl)
                qty, cost, anchor = 0.0, 0.0, None
            elif liq is not None and low[i] <= liq:
                trades.append(-cash)
                cash, qty, cost, anchor, liquidated = 0.0, 0.0, 0.0, None, True
            elif h[i] >= avg * (1 + take) and (not new_fill or opened_now and filled == 1):
                px = max(o[i], avg * (1 + take))
                pnl = qty * (px - avg) - qty * px * FEE
                cash += pnl
                trades.append(pnl)
                qty, cost, anchor = 0.0, 0.0, None
        if qty:
            cash -= qty * c[i] * fund[i]
        nav = cash + (qty * c[i] - cost if qty else 0.0)
        navs.append(nav)
        if goal and nav >= goal:
            halted = "goal"
            break
        if (floor is not None and nav <= floor) or nav <= 0:
            halted = "floor"
            break
    return Result(np.array(navs), idx[lo_i:lo_i + len(navs)].to_numpy(), trades, liquidated, halted)


def load_minutes(symbol: str, months=None) -> pd.DataFrame:
    """1-minute candles of the TEST period (cached), with the hourly funding on each hour's first minute.

    The grids depend on the order of the prices inside a bar, which an hourly bar does not
    tell; on 1-minute bars the question all but disappears. Large: ~1.4 million rows per pair."""
    import requests

    path = CACHE / f"{symbol}_1m.csv"
    if not path.exists():
        session = requests.Session()
        parts = []
        for month in months or [str(m) for m in pd.period_range(TEST[0][:7], TEST[1][:7], freq="M")]:
            rows = _rows(_get(session, f"{ARCHIVE}/klines/{symbol}/1m/{symbol}-1m-{month}.zip"))
            parts += [(int(r[0]) // 60_000 * 60_000, *map(float, r[1:5])) for r in rows]
        frame = pd.DataFrame(parts, columns=["t", "Open", "High", "Low", "Close"]).drop_duplicates("t").sort_values("t")
        CACHE.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False)
    frame = pd.read_csv(path)
    frame.index = pd.to_datetime(frame["t"], unit="ms")
    frame["funding"] = load(symbol)["funding"].reindex(frame.index).fillna(0.0)
    return frame.drop(columns="t")


def run(style: str, frame: pd.DataFrame, symbol: str, capital: float, start, end, **kw) -> Result:
    if style == "grid_h1":
        return run_grid(frame, symbol, capital, start, end, **kw)
    if style == "martingale_h1":
        return run_grid(frame, symbol, capital, start, end, sizes=(0.1, 0.2, 0.4, 0.8, 1.6), **kw)
    plan = STYLES[style][1](frame)
    return run_single(frame, plan, symbol, capital, start, end, **kw)


def stats(result: Result, capital: float) -> dict:
    navs = pd.Series(result.navs, index=pd.to_datetime(result.times)).resample("1D").last().dropna()
    if len(navs) < 2:
        return {"cagr": 0.0, "mdd": 0.0, "trades": len(result.trades), "win": 0.0, "pf": 0.0,
                "end": float(result.navs[-1]) if len(result.navs) else capital, "liq": result.liquidated}
    navs = pd.concat([pd.Series([capital], index=[navs.index[0] - pd.Timedelta(days=1)]), navs])
    years = max(len(navs) / 365, 1e-9)
    end = float(navs.iloc[-1])
    trades = np.array(result.trades)
    gains, losses = trades[trades > 0].sum(), -trades[trades < 0].sum()
    return {"cagr": (end / capital) ** (1 / years) - 1 if end > 0 else -1.0,
            "mdd": float((navs / navs.cummax() - 1).min()), "trades": len(trades),
            "win": float((trades > 0).mean()) if len(trades) else 0.0,
            "pf": float(gains / losses) if losses > 0 else float("inf"), "end": end, "liq": result.liquidated}


def _job(args):
    style, symbol = args
    bars, _ = STYLES[style]
    frame = load(symbol, bars)
    out = {"style": style, "symbol": symbol}
    for label, (start, end) in (("train", TRAIN), ("test", TEST)):
        for capital in (10.0, 1000.0):
            out[f"{label}_{capital:g}"] = stats(run(style, frame, symbol, capital, start, end), capital)
    goal = []
    for s0 in pd.date_range("2021-01-01", "2025-07-01", freq="QS"):
        r = run(style, frame, symbol, 10.0, s0, s0 + pd.Timedelta(days=1095), goal=110.0, floor=5.0)
        goal.append(r.halted)
    out["goal"] = (len(goal), goal.count("goal"), goal.count("floor"))
    return out


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="MT5 EA styles on Binance perpetuals")
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--styles", default=",".join(STYLES))
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--grid-1m", action="store_true",
                        help="also run grid/martingale on 1-minute bars of TEST (downloads ~60 MB per pair)")
    args = parser.parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    styles = [s.strip() for s in args.styles.split(",") if s.strip()]
    for symbol in symbols:
        if args.download or not (CACHE / f"{symbol}.csv").exists():
            download(symbol)
    jobs = [(style, symbol) for style in styles for symbol in symbols]
    with ProcessPoolExecutor(args.workers) as pool:
        rows = list(pool.map(_job, jobs))
    print("style                   coin  | TEST 10 USDT: CAGR   MDD    lệnh  thắng  PF   | TEST 1000: CAGR   MDD  "
          "| TRAIN 1000: CAGR | 10->110 trong 3 năm (đạt/dừng)")
    for r in rows:
        t, t1, tr = r["test_10"], r["test_1000"], r["train_1000"]
        n, g, fl = r["goal"]
        print(f"{r['style']:23s} {r['symbol'][:-4]:5s} | {t['cagr'] * 100:8.1f}% {t['mdd'] * 100:6.1f}% {t['trades']:5d} "
              f"{t['win'] * 100:4.0f}% {t['pf']:5.2f}{' LIQ' if t['liq'] else '    '} | {t1['cagr'] * 100:7.1f}% "
              f"{t1['mdd'] * 100:6.1f}% | {tr['cagr'] * 100:8.1f}% | {g}/{n} đạt, {fl}/{n} dừng")
    if args.grid_1m:
        print("\nGrid / martingale trên nến 1 phút (TEST):")
        for symbol in symbols:
            minutes = load_minutes(symbol)
            for style in ("grid_h1", "martingale_h1"):
                for capital in (10.0, 1000.0):
                    t = stats(run(style, minutes, symbol, capital, *TEST), capital)
                    print(f"  {style:14s} {symbol[:-4]:5s} vốn {capital:>6g}: CAGR {t['cagr'] * 100:6.1f}%  "
                          f"MDD {t['mdd'] * 100:6.1f}%  lệnh {t['trades']}  thắng {t['win'] * 100:.0f}%  PF {t['pf']:.2f}")


if __name__ == "__main__":
    main()
