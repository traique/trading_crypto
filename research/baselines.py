"""Baseline runs on TRAIN, VALID and TRAIN+VALID -> baselines.json and baselines.md.

    python -m research baselines [--out research/out]

Never touches TEST (the panel is loaded locked).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

from cryptotrader.market import rules
from research import config
from research.backtest import run, run_price
from research.data import load_panel
from research.strategies import BuyHold, CrossSectionalMomentum, EqualWeightMonthly, live

CAPITAL = 10_000


def jobs(split: str) -> list[tuple[object, str, dict]]:
    """(strategy, universe kind, run options) of one split."""
    out = [
        (live("btc_buy_hold"), "-", {}),
        (BuyHold("ETHUSDT", name="eth_buy_hold"), "-", {}),
        (EqualWeightMonthly(universe="liq_top10"), "liq_top10", {}),
        (live("btc_trend"), "-", {}),
        (CrossSectionalMomentum(universe="liq_top20"), "liq_top20", {}),
    ]
    if split == "TRAIN+VALID":
        out += [
            (EqualWeightMonthly(universe="liq_top10", name="ew_monthly_slip2x"), "liq_top10", {"slippage_mult": 2.0}),
            (CrossSectionalMomentum(universe="liq_top20", name="xs_momentum_slip2x"), "liq_top20",
             {"slippage_mult": 2.0}),
            (CrossSectionalMomentum(universe="liq_top50", name="xs_momentum"), "liq_top50", {}),
            (CrossSectionalMomentum(universe="liq_top20", name="xs_momentum_1M_adv1pct"), "liq_top20",
             {"capital": 1_000_000, "max_adv_pct": 0.01}),
        ]
    return out


def pack(res, split: str, options: dict, seconds: float) -> dict:
    return {"split": split, "strategy": res.name, "universe": res.universe_kind, "start": res.start,
            "end": res.end, "options": options, "metrics": res.metrics, "btc": res.benchmark,
            "yearly": res.yearly.reset_index().to_dict("records"), "warnings": res.warnings,
            "seconds": round(seconds, 1)}


def build_parser(ap: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    ap = ap or argparse.ArgumentParser(prog="python -m research baselines")
    ap.add_argument("--out", default=str(config.OUT_DIR), help=f"output directory (default {config.OUT_DIR})")
    return ap


def main(argv=None, args=None) -> int:
    args = args or build_parser().parse_args(argv)
    out_dir = Path(args.out)
    try:
        panel = load_panel()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    spans = panel.spans()
    lock = pd.Timestamp(config.LOCK_AFTER)
    out = {"generated": pd.Timestamp.now().isoformat(timespec="seconds"), "data": panel.info,
           "costs": {"fee_pct": rules.TAKER_FEE_PCT, "slippage_pct": rules.SLIPPAGE_PCT},
           "panel": {"pairs": int(len(spans)),
                     "stopped_before_lock": int((spans["last_day"] < lock - pd.Timedelta(days=3)).sum()),
                     "tradable_pairs": int(sum(rules.is_tradable_pair(s) for s in spans.index))},
           "runs": []}
    for split, (start, end) in config.SPLITS.items():
        t = time.time()
        res = run_price(rules.BENCHMARK, start, end, capital=CAPITAL, panel=panel)
        out["runs"].append(pack(res, split, {"costs": "none"}, time.time() - t))
        for strat, kind, kw in jobs(split):
            t = time.time()
            opts = {"capital": CAPITAL, **kw}
            res = run(strat, start, end, universe_kind=kind if kind != "-" else "liq_top20", panel=panel, **opts)
            res.universe_kind = kind
            out["runs"].append(pack(res, split, kw, time.time() - t))
            print(split, res.summary(), f"({time.time() - t:.0f}s)", flush=True)
    (out_dir / "baselines.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    (out_dir / "baselines.md").write_text(to_markdown(out), encoding="utf-8")
    print(f"wrote {out_dir / 'baselines.json'} and {out_dir / 'baselines.md'}")
    return 0


def _fmt(v, nd=1):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    return str(v)


def to_markdown(out: dict) -> str:
    d, c, p = out["data"], out["costs"], out["panel"]
    lines = [
        "# Baselines (research harness, project ledger)", "",
        f"Generated {out['generated']}. Panel {p['pairs']} USDT pairs ({p['tradable_pairs']} tradable after the "
        f"exclusion rules; {p['stopped_before_lock']} stopped trading before {config.LOCK_AFTER}), "
        f"data through {d.get('data_end')} (truncated at {d.get('truncated_at', '-')}: TEST is locked).",
        "",
        f"Costs: fee {c['fee_pct']}% per fill, slippage {c['slippage_pct']}% per market fill (x slippage_mult), "
        "fills at the next day's open (00:00 UTC). Capital 10,000 USDT unless noted; cash earns nothing. "
        "`BTCUSDT_price` is BTC's price with NO costs (the benchmark column of every run).",
        "",
    ]
    for split in config.SPLITS:
        lines += [f"## {split} ({config.SPLITS[split][0]} .. {config.SPLITS[split][1]})", "",
                  "| strategy | universe | options | total % | CAGR % | MDD % | Sharpe | Sortino | Calmar | vol % | "
                  "turnover/yr | trades | fees % cap | time inv % | BTC % |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in out["runs"]:
            if r["split"] != split:
                continue
            m = r["metrics"]
            opts = ",".join(f"{k}={v}" for k, v in (r.get("options") or {}).items() if k != "costs") or "-"
            trades = (m.get("buys") or 0) + (m.get("sells") or 0) if "buys" in m else "-"
            lines.append("| " + " | ".join([
                r["strategy"], r.get("universe", "-"), opts, _fmt(m.get("total_return_pct")), _fmt(m.get("cagr_pct")),
                _fmt(m.get("max_drawdown_pct")), _fmt(m.get("sharpe"), 2), _fmt(m.get("sortino"), 2),
                _fmt(m.get("calmar"), 2), _fmt(m.get("vol_ann_pct")), _fmt(m.get("turnover_per_year"), 2),
                _fmt(trades), _fmt(m.get("fees_pct_capital"), 2), _fmt(m.get("pct_time_invested")),
                _fmt((r.get("btc") or {}).get("total_return_pct"))]) + " |")
        lines.append("")
    runs = [r for r in out["runs"] if r["split"] == "TRAIN+VALID" and r.get("yearly")]
    if runs:
        lines += ["## Calendar-year returns % (TRAIN+VALID runs)", ""]
        years = [y["year"] for y in runs[0]["yearly"]]
        head = [f"{r['strategy']}" + (f"/{r['universe']}" if r.get("universe") not in ("-", None) else "") for r in runs]
        lines.append("| year | " + " | ".join(head) + " |")
        lines.append("|---" * (len(head) + 1) + "|")
        for i, y in enumerate(years):
            row = [str(y)] + [_fmt(r["yearly"][i]["return_pct"]) if i < len(r["yearly"]) else "-" for r in runs]
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    lines += ["## Warnings (delisted / halted pairs liquidated at their last close)", ""]
    for r in out["runs"]:
        w = r.get("warnings") or []
        if w:
            lines.append(f"- {r['strategy']}/{r.get('universe')} ({r['split']}): {len(w)} - " + "; ".join(w[:3])
                         + (" ..." if len(w) > 3 else ""))
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
