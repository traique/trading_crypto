"""The downloaded candles as aligned wide panels (days x pairs).

``load_panel()`` returns a ``Panel``; download the data first with
``python -m research download``.

- The calendar is every UTC day from the first candle to the data end
  (``data_end`` of ``download_meta.json``): the market trades 24/7.
- A cell is NaN on a day the pair had no TRADING candle: not listed yet,
  delisted, halted, or a candle with zero volume (Binance keeps printing flat
  zero-volume candles for some halted or delisted pairs; nothing could be
  bought or sold on them).
- Fields: open/high/low/close (USDT), volume (base asset), quote_volume (USDT
  traded, for liquidity), trades (number of trades).
- Prices are not adjusted for anything: crypto pairs have no dividends or
  splits. A redenomination or token swap shows as a gap (see ``segments``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import pickle
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from cryptotrader.data import klines as kl
from research import config

log = logging.getLogger("research.data")

FIELDS = ("open", "high", "low", "close", "volume", "quote_volume", "trades")
_COLUMN = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume",
           "quote_volume": "QuoteVolume", "trades": "Trades"}
CACHE_VERSION = 1           # bump when the panel's layout changes (rebuilds older caches)


class TestPeriodLocked(RuntimeError):
    """A run or a panel would include the locked TEST period (after ``config.LOCK_AFTER``)."""

    __test__ = False        # not a pytest class


@dataclass
class Panel:
    dates: pd.DatetimeIndex
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    quote_volume: pd.DataFrame
    trades: pd.DataFrame
    info: dict = field(default_factory=dict)

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)

    @property
    def end(self) -> pd.Timestamp:
        return self.dates[-1]

    def date_index(self, day) -> int:
        """Index of the last day on or before ``day`` (-1 when none)."""
        return int(self.dates.searchsorted(pd.Timestamp(day), side="right")) - 1

    def spans(self) -> pd.DataFrame:
        """Per pair: first and last trading day IN THIS PANEL, trading days, listing segments."""
        has = self.close.notna()
        rows = []
        for s in self.close.columns:
            days = self.dates[has[s].to_numpy()]
            if len(days) == 0:
                continue
            gaps = np.diff(days.to_numpy()).astype("timedelta64[D]").astype(int) if len(days) > 1 else np.array([])
            rows.append({"symbol": s, "first_day": days[0], "last_day": days[-1], "bars": len(days),
                         "segments": int((gaps > config.DELIST_GAP_DAYS).sum()) + 1})
        return pd.DataFrame(rows).set_index("symbol")


def make_panel(frames: dict[str, pd.DataFrame], end=None, info: dict | None = None) -> Panel:
    """A panel from ``{pair: candles}`` (``cryptotrader.data.klines`` columns).

    Candles after ``end`` and candles without volume are dropped; the calendar
    runs daily from the first candle to ``end`` (default: the last candle).
    """
    clean = {}
    for sym, frame in frames.items():
        f = kl.clean(frame)
        f = f[f["Volume"] > 0]
        if end is not None:
            f = f[f["Date"] <= pd.Timestamp(end)]
        if not f.empty:
            clean[sym] = f
    if not clean:
        raise ValueError("no candles to build a panel from")
    first = min(f["Date"].iloc[0] for f in clean.values())
    last = pd.Timestamp(end) if end is not None else max(f["Date"].iloc[-1] for f in clean.values())
    dates = pd.date_range(first, last, freq="D")
    cols = sorted(clean)
    arrays = {k: np.full((len(dates), len(cols)), np.nan) for k in FIELDS}
    for j, sym in enumerate(cols):
        f = clean[sym]
        rows = ((f["Date"] - first).dt.days).to_numpy()
        for k in FIELDS:
            arrays[k][rows, j] = f[_COLUMN[k]].to_numpy(dtype=float)
    wide = {k: pd.DataFrame(v, index=dates, columns=cols) for k, v in arrays.items()}
    info = {**(info or {}), "data_end": str(last.date()), "n_symbols": len(cols), "version": CACHE_VERSION}
    return Panel(dates=dates, info=info, **wide)


def read_klines(path) -> pd.DataFrame | None:
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError) as exc:
        log.warning("skipping %s: unreadable (%s); rerun `python -m research download`", path, exc)
        return None
    if "Date" not in frame.columns:
        return None
    return kl.clean(frame)


def build_panel() -> Panel:
    meta = {}
    with contextlib.suppress(OSError, ValueError):
        meta = json.loads(config.meta_file().read_text(encoding="utf-8"))
    frames = {}
    for path in sorted(config.klines_dir().glob("*.csv")):
        f = read_klines(path)
        if f is not None and not f.empty:
            frames[path.stem] = f
    end = meta.get("data_end") or (frames["BTCUSDT"]["Date"].iloc[-1] if "BTCUSDT" in frames else None)
    return make_panel(frames, end, {"coverage": meta.get("coverage", {})})


def truncate(panel: Panel, end) -> Panel:
    """A copy of ``panel`` holding no day after ``end``."""
    n = int(panel.dates.searchsorted(pd.Timestamp(end), side="right"))
    kw = {k: getattr(panel, k).iloc[:n].copy() for k in FIELDS}
    info = {k: v for k, v in panel.info.items() if k != "coverage"}     # coverage describes TEST days too
    info.update(truncated_at=str(pd.Timestamp(end).date()), data_end=str(panel.dates[n - 1].date()) if n else None)
    return Panel(dates=panel.dates[:n], info=info, **kw)


def check_locked(panel: Panel, unlock_test: bool) -> None:
    """Raise ``TestPeriodLocked`` when ``panel`` holds TEST days and the caller is not the evaluator."""
    if not unlock_test and len(panel.dates) and panel.dates[-1] > pd.Timestamp(config.LOCK_AFTER):
        raise TestPeriodLocked(f"the panel holds days after {config.LOCK_AFTER} (TEST); "
                               "load it with load_panel() (locked) - only the evaluator passes unlock_test=True")


_FULL: tuple[tuple, Panel] | None = None
_LOCKED: tuple[tuple, Panel] | None = None


def _key() -> tuple:
    meta = config.meta_file()
    return (str(config.data_dir()), meta.stat().st_mtime if meta.exists() else None)


def _cache_fresh() -> bool:
    cache = config.panel_cache()
    if not cache.exists():
        return False
    meta = config.meta_file()
    return not meta.exists() or cache.stat().st_mtime >= meta.stat().st_mtime


def load_panel(rebuild: bool = False, *, unlock_test: bool = False) -> Panel:
    """The panel (pickle-cached). Without ``unlock_test`` it ends on the last
    VALID day (``config.LOCK_AFTER``).

    That is a copy: the full panel, TEST days included, stays in this module
    (``_FULL``) and in the pickle cache. The lock is ``run()``'s date check plus
    strategies seeing only the truncated panel - research code must not reach
    for ``_FULL`` or the cache file.
    """
    global _FULL, _LOCKED
    key = _key()
    if _FULL is None or _FULL[0] != key or rebuild:
        _FULL = _LOCKED = None
        full = None
        if not rebuild and _cache_fresh():
            try:
                with open(config.panel_cache(), "rb") as fh:
                    cached = pickle.load(fh)
                if isinstance(cached, Panel) and cached.info.get("version") == CACHE_VERSION:
                    full = cached
            except (OSError, pickle.UnpicklingError, EOFError, AttributeError):
                full = None
        if full is None:
            if not config.data_ready():
                raise FileNotFoundError(f"no research data in {config.data_dir()}: run `python -m research download` "
                                        f"(or point {config.DATA_ENV} at a downloaded data directory)")
            full = build_panel()
            from cryptotrader.data.files import replace_file

            def dump(tmp):
                with open(tmp, "wb") as fh:
                    pickle.dump(full, fh, protocol=pickle.HIGHEST_PROTOCOL)

            replace_file(config.panel_cache(), dump)
        _FULL = (key, full)
    if unlock_test:
        return _FULL[1]
    if _LOCKED is None or _LOCKED[0] != key:
        _LOCKED = (key, truncate(_FULL[1], config.LOCK_AFTER))
    return _LOCKED[1]
