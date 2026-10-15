"""Command line of the research harness: ``python -m research <command>``.

    python -m research download [--workers 8] [--refresh] [--symbols BTCUSDT,ETHUSDT]
    python -m research baselines [--out research/out]
    python -m research list
    python -m research run btc_trend --split TRAIN
    python -m research run xs_momentum --split VALID --universe liq_top30 --slippage-mult 2
    python -m research run ew --start 2019-01-01 --end 2020-12-31 --out research/out/ew_2019
    python -m research run btc_trend --split TEST --unlock-test      # evaluator only, once
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

from research import config

SPLIT_CHOICES = ("TRAIN", "VALID", "TRAIN+VALID", "TEST", "ALL")

METRIC_ROWS = [
    ("final_nav", "Final NAV (USDT)"),
    ("total_return_pct", "Total return %"),
    ("cagr_pct", "CAGR %"),
    ("max_drawdown_pct", "Max drawdown %"),
    ("sharpe", "Sharpe (daily, rf=0, x sqrt 365)"),
    ("sortino", "Sortino"),
    ("calmar", "Calmar"),
    ("vol_ann_pct", "Volatility %/yr"),
    ("avg_exposure_pct", "Avg exposure %"),
    ("pct_time_invested", "Time invested %"),
    ("turnover_per_year", "Turnover /yr (one-way)"),
    ("buys", "Buys"),
    ("sells", "Sells"),
    ("trades_per_year", "Trades /yr"),
    ("win_rate_pct", "Win rate % (sells)"),
    ("fees_paid", "Fees paid (USDT)"),
    ("fees_pct_capital", "Fees % of capital"),
]


def split_dates(split: str) -> tuple[str, str]:
    split = split.upper()
    if split == "TEST":
        return config.TEST
    if split == "ALL":
        return config.ALL
    return config.SPLITS[split]


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def metrics_table(res) -> pd.DataFrame:
    rows = []
    for key, label in METRIC_ROWS:
        if key not in res.metrics:
            continue
        rows.append({"metric": label, res.name: _fmt(res.metrics.get(key)),
                     "BTC buy&hold (no costs)": _fmt(res.benchmark.get(key)) if key in res.benchmark else "-"})
    return pd.DataFrame(rows).set_index("metric")


def report(res, *, options: dict) -> str:
    opts = ", ".join(f"{k}={v}" for k, v in options.items() if v is not None)
    lines = [f"{res.name} [{res.universe_kind}] {res.start}..{res.end} ({res.metrics['days']} days"
             f"{', ' + opts if opts else ''})", "",
             metrics_table(res).to_string(), "", "Calendar-year returns (%):", res.yearly.to_string()]
    if res.warnings:
        lines += ["", f"Warnings ({len(res.warnings)}):"] + [f"- {w}" for w in res.warnings[:40]]
        if len(res.warnings) > 40:
            lines.append(f"- ... {len(res.warnings) - 40} more (see summary.json with --out)")
    lines += ["", res.summary()]
    return "\n".join(lines)


def write_outputs(res, out: Path, options: dict) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / "summary.json", out / "nav.csv", out / "fills.csv", out / "yearly.csv"]
    summary = {"strategy": res.name, "universe": res.universe_kind, "start": res.start, "end": res.end,
               "options": {**options, **res.options}, "metrics": res.metrics, "btc_buy_hold": res.benchmark,
               "yearly": res.yearly.reset_index().to_dict("records"), "warnings": res.warnings}
    paths[0].write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    res.daily.to_csv(paths[1], index_label="date")
    res.fills.to_csv(paths[2], index=False)
    res.yearly.to_csv(paths[3])
    return paths


def cmd_list(_args) -> int:
    from research.strategies import STRATEGIES

    width = max(len(k) for k in STRATEGIES)
    for name, text in STRATEGIES.items():
        print(f"{name:<{width}}  {text}")
    return 0


def cmd_run(args) -> int:
    from research.backtest import TestPeriodLocked, run, run_price
    from research.data import load_panel
    from research.strategies import make

    start, end = split_dates(args.split)
    start, end = args.start or start, args.end or end
    unlock = bool(args.unlock_test)
    if pd.Timestamp(end) > pd.Timestamp(config.LOCK_AFTER) and not unlock:
        print(f"TEST is locked: the period ends {end}, after {config.LOCK_AFTER}. Tune on TRAIN, check on VALID; "
              "the evaluator runs TEST once at the end with --unlock-test.", file=sys.stderr)
        return 2
    options = {"universe": args.universe, "slippage_mult": args.slippage_mult, "fee_pct": args.fee_pct,
               "max_adv_pct": args.max_adv_pct, "unlock_test": unlock or None}
    t0 = time.time()
    try:
        panel = load_panel(unlock_test=unlock)
        if args.strategy.lower().startswith("price:"):
            symbol = args.strategy.split(":", 1)[1].strip().upper()
            options = {"unlock_test": unlock or None}
            res = run_price(symbol, start, end, capital=args.capital, unlock_test=unlock, panel=panel)
        else:
            strategy = make(args.strategy, universe=args.universe, panel=panel)
            if getattr(strategy, "universe_label", None):
                options["universe"] = None          # the strategy's own universe (in the header)
            res = run(strategy, start, end, capital=args.capital, universe_kind=args.universe or "liq_top20",
                      slippage_mult=args.slippage_mult, fee_pct=args.fee_pct, max_adv_pct=args.max_adv_pct,
                      unlock_test=unlock, panel=panel)
    except TestPeriodLocked as exc:
        print(f"TEST is locked: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(report(res, options=options))
    print(f"({time.time() - t0:.0f}s)")
    if args.out:
        for path in write_outputs(res, Path(args.out), options):
            print(f"wrote {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m research",
                                 description="Research harness for Binance spot USDT pairs "
                                             f"(data: {config.data_dir()}; set {config.DATA_ENV} to move it).")
    sub = ap.add_subparsers(dest="command", required=True)

    from research import baselines, download

    download.build_parser(sub.add_parser("download", help="fetch / update the data set (incremental, resumable)"))
    baselines.build_parser(sub.add_parser("baselines", help="baseline runs on TRAIN, VALID, TRAIN+VALID"))
    sub.add_parser("list", help="the strategies `run` knows")

    r = sub.add_parser("run", help="backtest one strategy and print its metrics and yearly returns")
    r.add_argument("strategy", help="btc_buy_hold | btc_trend | ew | xs_momentum | buyhold:SYM | price:SYM")
    r.add_argument("--split", default="VALID", type=str.upper, choices=SPLIT_CHOICES,
                   help="fixed period (default VALID); TEST and ALL need --unlock-test")
    r.add_argument("--start", help="first day (YYYY-MM-DD), overrides --split")
    r.add_argument("--end", help="last day (YYYY-MM-DD), overrides --split")
    r.add_argument("--unlock-test", action="store_true",
                   help=f"allow days after {config.LOCK_AFTER} (evaluator only)")
    r.add_argument("--capital", type=float, default=10_000, help="starting USDT (default 10000)")
    r.add_argument("--slippage-mult", type=float, default=1.0, help="scale the 0.1%% slippage (stress test)")
    r.add_argument("--fee-pct", type=float, default=None, help="fee per fill in %% (default 0.1)")
    r.add_argument("--max-adv-pct", type=float, default=None,
                   help="cap each buy at this fraction of the 30-day average USDT volume (e.g. 0.01)")
    r.add_argument("--universe", default=None,
                   help="liq_top10 | liq_top20 | liq_top30 | liq_top50 (default liq_top20)")
    r.add_argument("--out", help="directory for summary.json, nav.csv, fills.csv, yearly.csv")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "download":
        from research import download

        return download.main(args=args)
    if args.command == "baselines":
        from research import baselines

        return baselines.main(args=args)
    if args.command == "list":
        return cmd_list(args)
    return cmd_run(args)
