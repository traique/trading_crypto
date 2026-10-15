"""Performance metrics from a daily NAV series (and the fills, when there are any).

Crypto trades every day: annualisation uses 365 days a year.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

DAYS_PER_YEAR = 365


def _returns(nav: pd.Series, capital: float) -> pd.Series:
    """Daily returns, the first one against ``capital`` (the NAV before the first day)."""
    vals = np.concatenate([[float(capital)], nav.to_numpy(dtype=float)])
    return pd.Series(vals[1:] / vals[:-1] - 1, index=nav.index)


def max_drawdown(nav: pd.Series, capital: float | None = None) -> float:
    """Largest peak-to-trough fall, in percent (negative or 0)."""
    vals = nav.to_numpy(dtype=float)
    if capital is not None:
        vals = np.concatenate([[capital], vals])
    if not len(vals):
        return 0.0
    peak = np.maximum.accumulate(vals)
    return float(((vals / peak) - 1).min() * 100)


def _ratio(num: float, den: float) -> float | None:
    return num / den if den and den > 0 and math.isfinite(den) else None


def compute(nav: pd.Series, capital: float, exposure: pd.Series | None = None,
            fills: pd.DataFrame | None = None) -> dict:
    """Metrics of a NAV path that starts from ``capital`` before its first day.

    - years: calendar days from the day before the first to the last / 365.25.
    - sharpe: daily returns, rf = 0, x sqrt(365); sortino: mean / downside
      deviation (sqrt of the mean squared negative return), x sqrt(365).
    - calmar: CAGR / |max drawdown|.
    - turnover_per_year: one-way, (buys + sells) / 2 / average NAV / years.
    - pct_time_invested: share of days with more than 5% of the NAV in coins.
    - fees_paid: USDT; fees_pct_capital: the same in % of the starting capital.
    """
    nav = nav.astype(float)
    r = _returns(nav, capital)
    years = max(len(nav) / 365.25, 1 / DAYS_PER_YEAR)
    total = nav.iloc[-1] / capital - 1
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    std = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    downside = float(np.sqrt(np.mean(np.minimum(r.to_numpy(), 0.0) ** 2))) if len(r) else 0.0
    sharpe = _ratio(float(r.mean()), std)
    sortino = _ratio(float(r.mean()), downside)
    mdd = max_drawdown(nav, capital)
    out = {
        "start": str(nav.index[0].date()), "end": str(nav.index[-1].date()), "days": int(len(nav)),
        "years": round(years, 2),
        "final_nav": round(float(nav.iloc[-1]), 2),
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(mdd, 2),
        "sharpe": round(sharpe * math.sqrt(DAYS_PER_YEAR), 3) if sharpe is not None else 0.0,
        "sortino": round(sortino * math.sqrt(DAYS_PER_YEAR), 3) if sortino is not None else 0.0,
        "calmar": round(cagr * 100 / abs(mdd), 3) if mdd < 0 else None,
        "vol_ann_pct": round(std * math.sqrt(DAYS_PER_YEAR) * 100, 2),
    }
    if exposure is not None:
        exp_mean = float(exposure.mean())
        out["avg_exposure_pct"] = round(exp_mean * 100, 1)
        out["pct_time_invested"] = round(float((exposure > 0.05).mean() * 100), 1)
    if fills is not None:
        if len(fills):
            value = fills["qty"] * fills["price"]
            buys = fills[fills["side"] == "BUY"]
            sells = fills[fills["side"] == "SELL"]
            pnl = sells["pnl"].dropna() if "pnl" in sells else pd.Series(dtype=float)
            out.update(
                turnover_per_year=round(float(value.sum() / 2 / nav.mean() / years), 2),
                buys=int(len(buys)), sells=int(len(sells)),
                trades_per_year=round((len(buys) + len(sells)) / years, 1),
                win_rate_pct=round(float((pnl > 0).mean() * 100), 1) if len(pnl) else None,
                fees_paid=round(float(fills["fee"].sum()), 2),
                fees_pct_capital=round(float(fills["fee"].sum() / capital * 100), 2),
            )
        else:
            out.update(turnover_per_year=0.0, buys=0, sells=0, trades_per_year=0.0, win_rate_pct=None,
                       fees_paid=0.0, fees_pct_capital=0.0)
    return {k: (v.item() if isinstance(v, np.generic) else v) for k, v in out.items()}


def yearly(nav: pd.Series, capital: float, exposure: pd.Series | None = None,
           bench: pd.Series | None = None) -> pd.DataFrame:
    """One row per calendar year: return, max drawdown, Sharpe, exposure, BTC buy & hold return."""
    rows = []
    prev_nav = float(capital)
    prev_bench = float(bench.dropna().iloc[0]) if bench is not None and bench.notna().any() else None
    for year, chunk in nav.groupby(nav.index.year):
        r = _returns(chunk, prev_nav)
        std = float(r.std(ddof=1)) if len(r) > 1 else 0.0
        row = {"year": int(year), "return_pct": round((chunk.iloc[-1] / prev_nav - 1) * 100, 2),
               "max_drawdown_pct": round(max_drawdown(chunk, prev_nav), 2),
               "sharpe": round(float(r.mean()) / std * math.sqrt(DAYS_PER_YEAR), 2) if std > 0 else None}
        if exposure is not None:
            row["avg_exposure_pct"] = round(float(exposure.loc[chunk.index].mean() * 100), 1)
        if prev_bench is not None:
            b = bench.loc[chunk.index].ffill()
            last = float(b.iloc[-1]) if b.notna().any() else prev_bench
            row["btc_pct"] = round((last / prev_bench - 1) * 100, 2)
            row["excess_pct"] = round(row["return_pct"] - row["btc_pct"], 2)
            prev_bench = last
        rows.append(row)
        prev_nav = float(chunk.iloc[-1])
    return pd.DataFrame(rows).set_index("year")
