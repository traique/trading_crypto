"""Walk-forward optimisation of the futures long/short rule (``cryptotrader.futures``), on many coins at once.

    python -m research.optimize_futures [--workers 4] [--top 20]

Protocol, fixed before running:

1. Data: daily Binance perpetual candles and real funding (``research/data/futures``,
   from ``research.futures_study``) of the 14 pairs listed since 2020.
2. Periods: TRAIN 2020-07-20 .. 2022-12-31 (search), VALID 2023 (choose),
   TEST 2024-01-01 .. data end (run ONCE, for the chosen rule and the current one).
3. Score: the MEDIAN over the coins of the daily Sharpe ratio, 1000 USDT
   account (the edge of the rule, without the exchange-minimum distortion of
   a 10 USDT account). One rule for every coin: no per-coin tuning.
4. Choice: the 20 best rules on TRAIN; among them the best on VALID; it must
   have a positive VALID median Sharpe and a TRAIN median drawdown better than
   -70%, else the current rule stays.
5. The grid (2 x 9 x 3 x 3 x 4 x 2 x 2 = 2592 rules): SMA set (50..200 or
   20..80), long_min x short_max, target volatility, leverage cap, ATR stop
   (0 = none), resize band, and a BTC filter (an altcoin trades only in the
   direction of BTC's own signal).

The fast simulator below repeats ``cryptotrader.futures.runner.backtest``
arithmetic on precomputed indicators; ``tests/test_research_optimize.py``
checks that both give the same fills and NAV.
"""

from __future__ import annotations

import argparse
import itertools
import math
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from cryptotrader.futures.rules import spec_for

DATA = Path(__file__).resolve().parent / "data" / "futures"
COINS = ("BTC", "ETH", "BNB", "XRP", "ADA", "LINK", "LTC", "DOGE", "TRX", "DOT", "SOL", "AVAX", "UNI", "NEAR")
START, TRAIN_END, VALID_END = "2020-07-20", "2022-12-31", "2023-12-31"
SLOW, FAST = (50, 75, 100, 125, 150, 175, 200), (20, 30, 40, 50, 60, 70, 80)
FEE, SLIP = 0.0005, 0.0005


@dataclass(frozen=True)
class Rule:
    lookbacks: str = "slow"       # "slow" (50..200) or "fast" (20..80)
    long_min: int = 6
    short_max: int = 1
    target_vol: float = 0.5
    max_leverage: float = 2.0
    stop_atr: float = 3.0         # 0 = no stop
    band: float = 0.25
    btc_filter: bool = False


CURRENT = Rule()


def grid() -> list[Rule]:
    return [Rule(lb, lm, sm, tv, ml, sa, b, bf) for lb, lm, sm, tv, ml, sa, b, bf in itertools.product(
        ("slow", "fast"), (5, 6, 7), (0, 1, 2), (0.3, 0.5, 0.8), (1.0, 2.0, 3.0), (0.0, 2.0, 3.0, 4.0),
        (0.1, 0.25), (False, True))]


# ── indicators (exactly as cryptotrader.futures.strategy computes them) ──
def prepare(coin: str) -> dict:
    f = pd.read_csv(DATA / f"{coin}USDT.csv", parse_dates=["Date"]).sort_values("Date").reset_index(drop=True)
    c = f["Close"].to_numpy(float)
    n = len(c)
    out = {"dates": f["Date"].dt.strftime("%Y-%m-%d").to_numpy(), "open": f["Open"].to_numpy(float),
           "high": f["High"].to_numpy(float), "low": f["Low"].to_numpy(float), "close": c,
           "funding": f["funding"].to_numpy(float) if "funding" in f else np.zeros(n)}
    for name, lbs in (("slow", SLOW), ("fast", FAST)):
        above = np.full(n, -1)
        need = max(lbs) + 1
        for i in range(need - 1, n):
            window = c[max(0, i + 1 - (max(SLOW) + 3)): i + 1]
            above[i] = sum(1 for k in lbs if window[-1] > window[-k:].mean())
        out[f"above_{name}"] = above
    vol = np.full(n, np.nan)
    logc = np.log(c)
    for i in range(31, n):
        r = np.diff(logc[i - 30: i + 1])
        vol[i] = np.std(r, ddof=1) * math.sqrt(365)
    tr = np.maximum.reduce([f["High"].to_numpy() - f["Low"].to_numpy(),
                            np.abs(f["High"].to_numpy() - np.r_[np.nan, c[:-1]]),
                            np.abs(f["Low"].to_numpy() - np.r_[np.nan, c[:-1]])])
    atr = np.full(n, np.nan)
    for i in range(15, n):
        atr[i] = np.mean(tr[i - 13: i + 1])
    out["vol"], out["atr"] = vol, atr
    return out


def _direction(above: int, rule: Rule) -> int:
    if above < 0:
        return 0
    return 1 if above >= rule.long_min else -1 if above <= rule.short_max else 0


def simulate(d: dict, rule: Rule, symbol: str, capital: float = 1000.0, start: str = START,
             btc: dict | None = None, min_order_leverage: float = 3.0) -> tuple[np.ndarray, np.ndarray, int]:
    """Daily NAV (from ``start``), its dates, and the number of fills."""
    spec = spec_for(symbol)
    o, h, low, c, fund = d["open"], d["high"], d["low"], d["close"], d["funding"]
    above, vol, atr, dates = d[f"above_{rule.lookbacks}"], d["vol"], d["atr"], d["dates"]
    btc_dir = None
    if rule.btc_filter and btc is not None and symbol != "BTCUSDT":
        idx = {day: i for i, day in enumerate(btc["dates"])}
        b_above = btc[f"above_{rule.lookbacks}"]
        btc_dir = np.array([_direction(b_above[idx[day]], rule) if day in idx else 0 for day in dates])
    first = int(np.searchsorted(dates, start))
    cash, qty, entry, stop = float(capital), 0.0, 0.0, None
    pend = None                                  # (target qty, stop dist)
    navs, fills = np.empty(len(c) - first), 0
    mmr = spec.mmr
    for j, i in enumerate(range(first, len(c))):
        if pend is not None:
            target, dist = pend
            pend = None
            if abs(target - qty) > 1e-12:
                if qty and target and (target > 0) != (qty > 0):           # flip: close first
                    px = o[i] * (1 - SLIP * (1 if qty > 0 else -1))
                    cash += qty * (px - entry) - abs(qty) * px * FEE
                    qty, entry, fills = 0.0, 0.0, fills + 1
                delta = target - qty
                if abs(delta) > 1e-12:
                    px = o[i] * (1 + SLIP * (1 if delta > 0 else -1))
                    if qty and (delta > 0) != (qty > 0):
                        closed = min(abs(delta), abs(qty)) * (1 if qty > 0 else -1)
                        cash += closed * (px - entry)
                        rem = qty + delta
                        qty = 0.0 if abs(rem) < 1e-12 or (rem > 0) != (qty > 0) else rem
                        if not qty:
                            entry = 0.0
                    else:
                        new = qty + delta
                        entry = (entry * qty + px * delta) / new if qty else px
                        qty = new
                    cash -= abs(delta) * px * FEE
                    fills += 1
            if qty and dist:
                stop = entry - math.copysign(dist, qty)
            elif not qty:
                stop = None
        if qty:
            lg = qty > 0
            worst = low[i] if lg else h[i]
            side = 1.0 if lg else -1.0
            liq = (qty * entry - cash) / (qty * (1 - side * mmr))
            liq = liq if liq > 0 else None
            hit_stop = stop is not None and (worst <= stop if lg else worst >= stop)
            hit_liq = liq is not None and (worst <= liq if lg else worst >= liq)
            if hit_stop and (liq is None or (stop > liq if lg else stop < liq)):
                px = (min(o[i], stop) if lg else max(o[i], stop)) * (1 - SLIP * side)
                cash += qty * (px - entry) - abs(qty) * px * FEE
                qty, entry, stop, fills = 0.0, 0.0, None, fills + 1
            elif hit_liq:
                cash, qty, entry, stop = 0.0, 0.0, 0.0, None
        if qty:
            cash -= qty * c[i] * fund[i]
        nav = cash + (qty * (c[i] - entry) if qty else 0.0)
        navs[j] = nav
        # decision at the close
        direction = _direction(above[i], rule)
        if btc_dir is not None and direction and btc_dir[i] != direction:
            direction = 0
        if above[i] < 0 or direction == 0 or nav <= 0:
            target = 0.0
            dist = None
        else:
            v = vol[i]
            lev = min(rule.max_leverage, rule.target_vol / v) if v == v and v > 0 else 0.0
            q = spec.floor_qty(lev * nav / c[i])
            if q * c[i] < spec.min_notional:
                need = spec.min_qty_at(c[i])
                q = need if need * c[i] / nav <= max(rule.max_leverage, min_order_leverage) + 1e-9 else 0.0
            target = direction * q
            a = atr[i]
            dist = rule.stop_atr * a if rule.stop_atr and a == a else None
        if qty and target and (qty > 0) == (target > 0) and abs(target - qty) <= rule.band * abs(qty):
            continue
        if abs(target - qty) < 1e-12:
            continue
        pend = (target, dist)
    return navs, dates[first:], fills


def metrics(navs: np.ndarray) -> dict:
    if len(navs) < 30 or navs[0] <= 0:
        return {"sharpe": 0.0, "cagr": 0.0, "mdd": 0.0}
    safe = np.maximum(navs, 1e-9)
    rets = np.diff(safe) / safe[:-1]
    sd = rets.std()
    end = navs[-1] / navs[0]
    years = len(navs) / 365
    return {"sharpe": float(rets.mean() / sd * math.sqrt(365)) if sd > 0 else 0.0,
            "cagr": float(end ** (1 / years) - 1) if end > 0 else -1.0,
            "mdd": float((safe / np.maximum.accumulate(safe) - 1).min())}


def periods(navs, dates) -> dict:
    cut1, cut2 = np.searchsorted(dates, "2023-01-01"), np.searchsorted(dates, "2024-01-01")
    return {"train": metrics(navs[:cut1]), "valid": metrics(navs[cut1 - 1:cut2]), "test": metrics(navs[cut2 - 1:])}


_DATA: dict = {}


def _init() -> None:
    for coin in COINS:
        _DATA[coin] = prepare(coin)


def evaluate(rule: Rule) -> dict:
    per = {}
    for coin in COINS:
        navs, dates, fills = simulate(_DATA[coin], rule, f"{coin}USDT", btc=_DATA["BTC"])
        per[coin] = {**periods(navs, dates), "fills": fills}
    summary = {}
    for p in ("train", "valid", "test"):
        for m in ("sharpe", "cagr", "mdd"):
            summary[f"{p}_{m}"] = float(np.median([per[c][p][m] for c in COINS]))
    return {"rule": rule, "summary": summary, "per": per}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="walk-forward optimisation of ls_sma")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args(argv)
    rules = grid()
    with ProcessPoolExecutor(args.workers, initializer=_init) as pool:
        results = list(pool.map(evaluate, rules, chunksize=16))
    by_rule = {r["rule"]: r for r in results}
    ranked = sorted(results, key=lambda r: -r["summary"]["train_sharpe"])
    top = ranked[:args.top]

    def show(r, label=""):
        s, rule = r["summary"], r["rule"]
        print(f"{label:10s} {rule.lookbacks:4s} L>={rule.long_min} S<={rule.short_max} vol {rule.target_vol:.1f} "
              f"x{rule.max_leverage:.0f} stop {rule.stop_atr:.0f}ATR band {rule.band:.2f} btc {'on ' if rule.btc_filter else 'off'}"
              f" | TRAIN Sh {s['train_sharpe']:5.2f} CAGR {s['train_cagr'] * 100:6.1f}% MDD {s['train_mdd'] * 100:6.1f}%"
              f" | VALID Sh {s['valid_sharpe']:5.2f} CAGR {s['valid_cagr'] * 100:6.1f}%")

    print(f"{len(rules)} rules x {len(COINS)} coins; medians over coins, 1000 USDT account.\n")
    print("Top by TRAIN:")
    for r in top:
        show(r)
    eligible = [r for r in top if r["summary"]["valid_sharpe"] > 0 and r["summary"]["train_mdd"] > -0.70]
    chosen = max(eligible, key=lambda r: r["summary"]["valid_sharpe"]) if eligible else by_rule[CURRENT]
    print("\nChosen (best VALID among the top):")
    show(chosen, "CHOSEN")
    show(by_rule[CURRENT], "CURRENT")
    print("\nTEST (run once):")
    for label, r in (("CHOSEN", chosen), ("CURRENT", by_rule[CURRENT])):
        s = r["summary"]
        print(f"  {label:8s} median Sharpe {s['test_sharpe']:5.2f}  CAGR {s['test_cagr'] * 100:6.1f}%  "
              f"MDD {s['test_mdd'] * 100:6.1f}%")
        print("   " + "  ".join(f"{c} {r['per'][c]['test']['cagr'] * 100:+.0f}%" for c in COINS))
    print("\nchosen rule:", asdict(chosen["rule"]))


if __name__ == "__main__":
    main()
