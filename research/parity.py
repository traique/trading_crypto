"""Parity check: the live strategy module against the frozen evaluated code, on the research data.

    python -m research.parity                       # TRAIN, VALID, TRAIN+VALID
    python -m research.parity --json out.json

Runs ``btc_sma_ens_eval`` (``research/evaluated/trend_majors.py``, the code the
evaluator ran) and ``btc_sma_ens`` (``cryptotrader.strategies.BtcSmaEnsemble``,
run through ``StrategyContext`` exactly as the bot runs it) on the same panel
and compares every fill (day, pair, side, quantity, price, fee) and the daily
NAV. The TEST period stays locked: this never unlocks it.
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np

from research import config
from research.backtest import run
from research.data import load_panel
from research.strategies import evaluated_c2, make

FILL_COLUMNS = ["date", "symbol", "side", "qty", "price", "fee"]


def compare(start: str, end: str, panel=None, *, bot_min_trade_pct: float | None = None) -> dict:
    """Both forms over [start, end]: their metrics, and whether fills and NAV are identical."""
    panel = panel if panel is not None else load_panel()
    live = make("btc_sma_ens")
    if bot_min_trade_pct is not None:
        live.min_trade_pct = float(bot_min_trade_pct)
    a = run(evaluated_c2(), start, end, panel=panel)
    b = run(live, start, end, panel=panel)
    fa = a.fills[FILL_COLUMNS].reset_index(drop=True) if len(a.fills) else a.fills
    fb = b.fills[FILL_COLUMNS].reset_index(drop=True) if len(b.fills) else b.fills

    def summary(res) -> dict:
        m = res.metrics
        return {"cagr_pct": round(m["cagr_pct"], 2), "max_drawdown_pct": round(m["max_drawdown_pct"], 2),
                "sharpe": round(m["sharpe"], 3), "fills": int(len(res.fills)), "final_nav": round(m["final_nav"], 2)}

    return {"start": a.start, "end": a.end, "evaluated": summary(a), "live": summary(b),
            "identical_fills": bool(fa.equals(fb)),
            "max_abs_nav_diff": float(np.max(np.abs(a.daily["nav"].to_numpy() - b.daily["nav"].to_numpy())))}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.parity", description=__doc__.splitlines()[0])
    parser.add_argument("--json", help="write the comparison to this file")
    args = parser.parse_args(argv)
    if not config.data_ready():
        print(f"no research data in {config.data_dir()} (python -m research download)", file=sys.stderr)
        return 2
    panel = load_panel()
    out = {}
    for split, (start, end) in config.SPLITS.items():
        out[split] = compare(start, end, panel)
        out[split]["identical_with_bot_min_trade_1pct"] = compare(start, end, panel,
                                                                  bot_min_trade_pct=1.0)["identical_fills"]
        r = out[split]
        print(f"{split:12s} {r['start']}..{r['end']} fills {r['live']['fills']:4d} identical={r['identical_fills']} "
              f"(1% min trade: {r['identical_with_bot_min_trade_1pct']}) max|ΔNAV|={r['max_abs_nav_diff']:.6f} "
              f"CAGR {r['live']['cagr_pct']} MDD {r['live']['max_drawdown_pct']} Sharpe {r['live']['sharpe']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1)
    return 0 if all(r["identical_fills"] and r["identical_with_bot_min_trade_1pct"] for r in out.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
