"""Backtest of the futures long/short rule on Binance perpetual data, and the "10 -> 110 USDT" question.

    python -m research.futures_study [--symbols BTCUSDT,ETHUSDT] [--capital 10] [--goal 100] [--floor 5]

Data: monthly archives of the Binance USDⓈ-M perpetuals (data.binance.vision,
``futures/um/monthly/klines/<SYM>/1d`` and ``fundingRate``), cached under
``research/data/futures``. The real funding of each day is charged (longs pay
a positive rate, shorts receive it); fees 0.05% + slippage 0.05% per fill.

It prints, per symbol:

1. TRAIN (2020-07-20 .. 2023-12-31) and TEST (2024-01-01 .. data end) for a
   small (``--capital``) and a large (1000 USDT) account: with a small one the
   exchange minimum order decides the leverage (or forbids the trade);
2. the goal study: starting on the first day of every month, run at most
   ``--years`` years and count how often the account reaches capital + goal
   before falling to the floor, and the median end equity.

The parameters of the rule were fixed beforehand (``cryptotrader.futures.strategy``);
this script measures, it does not tune.
"""

from __future__ import annotations

import argparse
import io
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from cryptotrader.futures import AccountPolicy, LongShortSma, backtest, summary
from cryptotrader.futures.runner import nav_series

ARCHIVE = "https://data.binance.vision/data/futures/um/monthly"
CACHE = Path(__file__).resolve().parent / "data" / "futures"
TRAIN = ("2020-07-20", "2023-12-31")
TEST_START = "2024-01-01"


def _zip_csv(url: str) -> list[list[str]] | None:
    response = requests.get(url, timeout=60)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        text = archive.read(archive.namelist()[0]).decode()
    return [line.split(",") for line in text.strip().splitlines() if line[:1].isdigit()]


def load_perp(symbol: str, refresh: bool = False) -> pd.DataFrame:
    """Daily perpetual candles of ``symbol`` with a ``funding`` column (sum of the day's rates)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{symbol}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, parse_dates=["Date"])
    candles, funding = [], []
    for month in pd.period_range("2019-09", pd.Timestamp.utcnow().strftime("%Y-%m"), freq="M"):
        rows = _zip_csv(f"{ARCHIVE}/klines/{symbol}/1d/{symbol}-1d-{month}.zip")
        candles += [(int(r[0]), *map(float, r[1:6])) for r in rows or []]
        rows = _zip_csv(f"{ARCHIVE}/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip")
        funding += [(int(r[0]), float(r[2])) for r in rows or []]
    frame = pd.DataFrame(candles, columns=["t", "Open", "High", "Low", "Close", "Volume"])
    frame["Date"] = pd.to_datetime(frame["t"], unit="ms").dt.normalize()
    frame = frame.drop(columns="t").drop_duplicates("Date").sort_values("Date")
    rates = pd.DataFrame(funding, columns=["t", "rate"])
    rates["Date"] = pd.to_datetime(rates["t"], unit="ms").dt.normalize()
    daily = rates.groupby("Date")["rate"].sum().rename("funding")
    frame = frame.merge(daily, left_on="Date", right_index=True, how="left").fillna({"funding": 0.0})
    frame.to_csv(path, index=False)
    return frame


def _fmt(stats: dict) -> str:
    return (f"CAGR {stats['cagr'] * 100:6.1f}%  MDD {stats['mdd'] * 100:6.1f}%  Sharpe {stats['sharpe']:5.2f}  "
            f"x{stats['multiple']:.2f}")


def goal_study(frame: pd.DataFrame, symbol: str, capital: float, goal: float, floor: float, years: float,
               strategy: LongShortSma) -> dict:
    end = pd.Timestamp(frame["Date"].max())
    starts = pd.date_range("2020-08-01", end - pd.Timedelta(days=365), freq="MS")
    out = []
    for start in starts:
        book = backtest(frame, strategy, capital, AccountPolicy(goal, floor), start=str(start.date()),
                        end=str((start + pd.Timedelta(days=int(365 * years))).date()), symbol=symbol,
                        stop_at_halt=True)
        out.append((book.meta.get("halted"), len(book.history), book.last_nav))
    reached = [r for r in out if r[0] == "goal"]
    return {"starts": len(out), "goal": len(reached), "floor": sum(r[0] == "floor" for r in out),
            "median_days_to_goal": float(np.median([r[1] for r in reached])) if reached else None,
            "median_end": float(np.median([r[2] for r in out])) if out else None}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--capital", type=float, default=10.0)
    parser.add_argument("--goal", type=float, default=100.0)
    parser.add_argument("--floor", type=float, default=5.0)
    parser.add_argument("--years", type=float, default=3.0)
    parser.add_argument("--target-vol", type=float, default=0.5, help="biến động mục tiêu (0.5 = 50%%/năm)")
    parser.add_argument("--max-leverage", type=float, default=2.0)
    parser.add_argument("--min-order-leverage", type=float, default=3.0)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    strategy = LongShortSma(target_vol=args.target_vol, max_leverage=args.max_leverage,
                            min_order_max_leverage=args.min_order_leverage)
    print(f"ls_sma: target_vol {args.target_vol:g}, max_leverage {args.max_leverage:g}, "
          f"min_order_leverage {args.min_order_leverage:g}")
    for symbol in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        frame = load_perp(symbol, args.refresh)
        print(f"\n== {symbol}  ({frame['Date'].min().date()} .. {frame['Date'].max().date()})")
        for capital in (args.capital, 1000.0):
            for label, (start, end) in (("TRAIN", TRAIN), ("TEST ", (TEST_START, None))):
                book = backtest(frame, strategy, capital, start=start, end=end, symbol=symbol)
                liq = sum(t["reason"] == "THANH LÝ" for t in book.trades)
                print(f"  vốn {capital:>6g}  {label} {_fmt(summary(nav_series(book)))}  "
                      f"lệnh {len(book.trades)}  thanh lý {liq}")
        hold = frame.set_index("Date")["Close"].loc[TEST_START:]
        print(f"  giữ {symbol} (spot, TEST): {_fmt(summary(hold))}")
        g = goal_study(frame, symbol, args.capital, args.goal, args.floor, args.years, strategy)
        print(f"  mục tiêu {args.capital:g} -> {args.capital + args.goal:g} USDT trong {args.years:g} năm, "
              f"{g['starts']} điểm bắt đầu: đạt {g['goal']}, chạm mức dừng {args.floor:g}: {g['floor']}, "
              f"vốn cuối trung vị {g['median_end']:.2f}"
              + (f", số ngày tới mục tiêu (trung vị) {g['median_days_to_goal']:.0f}" if g["goal"] else ""))


if __name__ == "__main__":
    main()
