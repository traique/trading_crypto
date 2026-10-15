"""Point-in-time universes of Binance USDT pairs.

``universe(date, kind)`` uses only candles of days STRICTLY BEFORE ``date``
(the universe a strategy may trade on ``date``, known at the close of the day
before). ``kind`` is ``"liq_topN"`` (``liq_top10/20/30/50``, any N): the N pairs
with the highest median daily USDT volume (quote volume) over the last
``LIQ_WINDOW`` days, among pairs that

- are tradable by ``cryptotrader.market.rules.exclusion_reason`` (USDT quote; no
  stablecoins, fiat, leveraged tokens or wrapped/pegged duplicates),
- have at least ``MIN_HISTORY`` trading days in their current listing segment
  (history before a gap of more than ``config.DELIST_GAP_DAYS`` days belongs to
  another market: a pair delisted and listed again starts over),
- have a candle in the last ``RECENT`` days.

Days without a trading candle count as zero volume in the median (an
illiquid or halted pair). Delisted pairs are in the data (the archive keeps
them), so they enter the universe while they traded and leave it when they stop.
"""

from __future__ import annotations

import weakref

import numpy as np
import pandas as pd

from cryptotrader.market import rules
from research import config
from research.data import Panel

MIN_HISTORY = 180
LIQ_WINDOW = 30
RECENT = 3

# id(panel) -> (weak reference to the panel, its eligibility/median arrays). The weak
# reference ties each entry to the panel object itself (CPython reuses ids).
_cache: dict[int, tuple[weakref.ref, dict]] = {}


def _forget(key: int, ref: weakref.ref) -> None:
    entry = _cache.get(key)
    if entry is not None and entry[0] is ref:
        del _cache[key]


def segment_bars(has: np.ndarray, gap_days: int = config.DELIST_GAP_DAYS) -> np.ndarray:
    """For each (day, pair): trading days so far in the pair's current listing segment
    (0 before the first candle). A candle more than ``gap_days`` after the previous
    one starts a new segment."""
    n, m = has.shape
    out = np.full((n, m), np.nan)
    for j in range(m):
        rows = np.flatnonzero(has[:, j])
        if rows.size == 0:
            continue
        breaks = np.concatenate([[True], np.diff(rows) > gap_days])
        seg = np.cumsum(breaks) - 1
        starts = np.flatnonzero(breaks)
        within = np.arange(rows.size) - starts[seg] + 1
        out[rows, j] = within
    return pd.DataFrame(out).ffill().fillna(0.0).to_numpy()


def _prep(panel: Panel) -> dict:
    key = id(panel)
    entry = _cache.get(key)
    if entry is not None and entry[0]() is panel:
        return entry[1]
    has = panel.close.notna()
    listed = has.cumsum() > 0
    qv = panel.quote_volume.where(has, 0.0).where(listed)
    med = qv.rolling(LIQ_WINDOW, min_periods=LIQ_WINDOW).median()
    recent = has.astype(float).rolling(RECENT, min_periods=1).max() > 0
    tradable = np.array([rules.is_tradable_pair(s) for s in panel.close.columns])
    eligible = (segment_bars(has.to_numpy()) >= MIN_HISTORY) & recent.to_numpy() & (med.to_numpy() > 0)
    eligible &= tradable[None, :]
    prep = {"med": med.to_numpy(), "eligible": eligible, "cols": np.array(panel.close.columns), "out": {}}
    _cache[key] = (weakref.ref(panel, lambda ref, key=key: _forget(key, ref)), prep)
    return prep


def parse_kind(kind: str) -> int:
    if not isinstance(kind, str) or not kind.startswith("liq_top") or not kind[len("liq_top"):].isdigit():
        raise ValueError(f"unknown universe kind {kind!r} (use liq_top10, liq_top20, liq_top30, liq_top50...)")
    n = int(kind[len("liq_top"):])
    if n <= 0:
        raise ValueError(f"unknown universe kind {kind!r}")
    return n


def universe(date, kind: str = "liq_top20", panel: Panel | None = None) -> list[str]:
    """Pairs of ``kind`` for trading on ``date``, from candles strictly before ``date``,
    most liquid first."""
    n = parse_kind(kind)
    if panel is None:
        from research.data import load_panel

        panel = load_panel()
    i = int(panel.dates.searchsorted(pd.Timestamp(date), side="left")) - 1
    if i < 0:
        return []
    prep = _prep(panel)
    hit = prep["out"].get((i, n))
    if hit is not None:
        return list(hit)
    ok = prep["eligible"][i]
    med = np.where(ok, prep["med"][i], -np.inf)
    order = np.argsort(-med, kind="stable")[:n]
    out = [str(prep["cols"][j]) for j in order if ok[j]]
    prep["out"][(i, n)] = out
    return list(out)


def universe_history(kind: str, start: str, end: str, panel: Panel | None = None) -> pd.Series:
    """The universe on the first day of each month in [start, end] (for inspection)."""
    if panel is None:
        from research.data import load_panel

        panel = load_panel()
    days = panel.dates[(panel.dates >= pd.Timestamp(start)) & (panel.dates <= pd.Timestamp(end))]
    firsts = [d for d in days if d.day == 1] or list(days[:1])
    return pd.Series({d: universe(d, kind, panel) for d in firsts})
