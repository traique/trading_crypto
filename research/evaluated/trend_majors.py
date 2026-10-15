"""Time-series trend following on the majors (BTC, ETH), USDT when off.

One configurable research strategy for ``research.backtest.run`` (``on_day(ctx)``):

- ``assets``: pairs traded, e.g. ``("BTCUSDT",)`` or ``("BTCUSDT", "ETHUSDT")``.
- ``signal``: per-asset on/off (or fractional) exposure, computed from closes up to D:
  - ``("sma", n)``: close > SMA(n)
  - ``("sma_band", n, band)``: enter when close > SMA(n)*(1+band), exit when close < SMA(n)*(1-band)
  - ``("ema", fast, slow)``: EMA(fast) > EMA(slow)
  - ``("donchian", n_in, n_out)``: enter when close > max(previous n_in closes),
    exit when close < min(previous n_out closes)
  - ``("tsmom", L)``: close / close L days ago > 1
  - ``("ens_tsmom", (L1, L2, ...))``: mean of tsmom(Li) -> fraction in [0, 1]
  - ``("ens_sma", (n1, n2, ...))``: mean of close > SMA(ni) -> fraction in [0, 1]
  - ``("ens", [sub_signal, ...])``: mean of any of the signals above -> fraction in [0, 1]
  - ``("always",)``: always on (buy & hold, for the benchmarks)
- ``alloc``: sleeve sizes for several assets: ``"equal"`` (1/N each) or
  ``"invvol"`` (inverse 60-day realised vol, normalised over ALL assets, so an
  asset that is off leaves its sleeve in USDT); ``"realloc"``: the on-assets share
  the whole book equally.
- ``vol_target``: optional annualised vol target; each sleeve is scaled by
  min(1, vol_target / realised vol) (the rest in USDT).
- ``check``: ``"daily"`` or ``"weekly"`` (signals are only read on ``weekday``,
  Monday=0 ... Sunday=6, at that day's close; orders fill at the next open).
- ``band``: drift tolerance; when no asset's on/off state changes the book is
  only resized if some weight is more than ``band`` (absolute) off target.

Signals need ``warm`` days of history (the longest lookback); until then the
asset counts as off (cash). Every input is ``ctx.close`` (sliced to D by the harness).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

BTC, ETH = "BTCUSDT", "ETHUSDT"


def _lookback(signal) -> int:
    kind = signal[0]
    if kind == "always":
        return 0
    if kind in ("sma", "tsmom"):
        return int(signal[1])
    if kind == "sma_band":
        return int(signal[1])
    if kind == "ema":
        return int(signal[2])
    if kind == "donchian":
        return int(max(signal[1], signal[2]))
    if kind in ("ens_tsmom", "ens_sma"):
        return int(max(signal[1]))
    if kind == "ens":
        return max(_lookback(tuple(sub)) for sub in signal[1])
    raise ValueError(f"unknown signal {signal!r}")


class TrendMajors:
    min_trade_pct = 0.5

    def __init__(self, assets=(BTC,), signal=("sma", 200), alloc="equal", check="daily", weekday=6,
                 band=0.10, vol_target=None, vol_days=60, name=None):
        self.assets = tuple(assets)
        self.signal = tuple(signal)
        self.alloc, self.check, self.weekday = alloc, check, int(weekday)
        self.band, self.vol_target, self.vol_days = float(band), vol_target, int(vol_days)
        self.warm = _lookback(self.signal) + 1
        self.name = name or f"trend_{'+'.join(a[:3] for a in self.assets)}_{self.signal}"
        self.universe_label = "-"

    # -- per-asset raw signal (0..1) at today's close --
    def _sig(self, px: np.ndarray, state: dict, asset, s=None) -> float:
        s = tuple(self.signal if s is None else s)
        kind = s[0]
        last = px[-1]
        if kind == "always":
            return 1.0
        if kind == "sma":
            return float(last > px[-s[1]:].mean())
        if kind == "sma_band":
            sma = px[-s[1]:].mean()
            key = ("on", asset)
            on = state.get(key, False)
            if not on and last > sma * (1 + s[2]):
                on = True
            elif on and last < sma * (1 - s[2]):
                on = False
            state[key] = on
            return float(on)
        if kind == "ema":
            ser = pd.Series(px)
            f = ser.ewm(span=s[1], adjust=False).mean().iloc[-1]
            sl = ser.ewm(span=s[2], adjust=False).mean().iloc[-1]
            return float(f > sl)
        if kind == "donchian":
            key = ("on", asset)
            on = state.get(key, False)
            hi = px[-s[1] - 1:-1].max()
            lo = px[-s[2] - 1:-1].min()
            if not on and last > hi:
                on = True
            elif on and last < lo:
                on = False
            state[key] = on
            return float(on)
        if kind == "tsmom":
            return float(last > px[-s[1] - 1])
        if kind == "ens_tsmom":
            return float(np.mean([last > px[-L - 1] for L in s[1]]))
        if kind == "ens_sma":
            return float(np.mean([last > px[-n:].mean() for n in s[1]]))
        if kind == "ens":
            return float(np.mean([self._sig(px, state, (asset, k), tuple(sub)) for k, sub in enumerate(s[1])]))
        raise ValueError(kind)

    def _vol(self, px: np.ndarray) -> float:
        r = np.diff(np.log(px[-self.vol_days - 1:]))
        return float(r.std(ddof=1) * math.sqrt(365)) if len(r) > 2 else float("nan")

    def _history_len(self) -> int:
        # EMA needs a long tail to converge; take a generous window.
        has_ema = self.signal[0] == "ema" or (self.signal[0] == "ens" and any(sub[0] == "ema" for sub in self.signal[1]))
        return max(self.warm, self.vol_days + 2) * (4 if has_ema else 1) + 2

    def _stateful(self) -> bool:
        return self.signal[0] in ("donchian", "sma_band") or (
            self.signal[0] == "ens" and any(sub[0] in ("donchian", "sma_band") for sub in self.signal[1]))

    def target(self, closes: dict, current: dict, dayofweek: int, state: dict):
        """Core rule. ``closes``: per asset, a numpy array of closes up to today (today last);
        ``current``: today's weights of the book. Returns target weights, or None (no trade)."""
        is_check = self.check != "weekly" or dayofweek == self.weekday
        if not is_check and not self._stateful():
            return None
        sigs, vols = {}, {}
        for a in self.assets:
            px = closes[a]
            sigs[a] = self._sig(px, state, a) if len(px) >= self.warm else 0.0
            vols[a] = self._vol(px) if len(px) > self.vol_days + 1 else float("nan")
        if not is_check:
            return None                       # stateful signals update daily, trades only on check days
        if self.alloc == "invvol" and len(self.assets) > 1 and all(math.isfinite(v) and v > 0 for v in vols.values()):
            inv = {a: 1 / vols[a] for a in self.assets}
            tot = sum(inv.values())
            sleeve = {a: inv[a] / tot for a in self.assets}
        elif self.alloc == "realloc":
            on = [a for a in self.assets if sigs[a] > 0]
            sleeve = {a: (1 / len(on) if a in on else 0.0) for a in self.assets} if on else dict.fromkeys(self.assets, 0)
        else:
            sleeve = {a: 1 / len(self.assets) for a in self.assets}
        target = {}
        for a in self.assets:
            w = sleeve[a] * sigs[a]
            if self.vol_target and math.isfinite(vols[a]) and vols[a] > 0:
                w *= min(1.0, self.vol_target / vols[a])
            target[a] = w
        on_now = {a for a in self.assets if current.get(a, 0) > 0.005}
        on_tgt = {a for a in self.assets if target[a] > 0.005}
        if on_now == on_tgt:
            drift = max(abs(current.get(a, 0) - target[a]) for a in self.assets)
            if drift <= self.band:
                return None
        return {a: w for a, w in target.items() if w > 0}

    def on_day(self, ctx):
        """Research-harness entry point (``research.backtest.run``)."""
        if any(ctx.last_close(a) is None for a in self.assets):
            return None                         # a missing candle: change nothing today
        hist = ctx.history("close", self.assets, self._history_len())
        closes = {a: hist[a].dropna().to_numpy() for a in self.assets}
        return self.target(closes, ctx.weights(), ctx.date.dayofweek, ctx.state)


class TrendMajorsLive:
    """The same rule as a ``cryptotrader.strategies`` object (``decide(StrategyContext)``),
    i.e. the shape the bot runs. The harness runs it through ``StrategyContext`` too."""

    def __init__(self, core: TrendMajors):
        self.core = core
        self.name = core.name
        self.symbols = core.assets
        self.warmup_days = core._history_len()
        self.min_trade_pct = core.min_trade_pct
        self.universe_label = "-"

    def decide(self, ctx):
        if any(ctx.last_close(a) is None for a in self.symbols):
            return dict(ctx.weights)            # no candle today: change nothing
        n = self.core._history_len()
        closes = {a: ctx.closes(a).dropna().to_numpy()[-n:] for a in self.symbols}
        out = self.core.target(closes, dict(ctx.weights), pd.Timestamp(ctx.date).dayofweek, ctx.state)
        return dict(ctx.weights) if out is None else out


# ── the candidates returned to the orchestrator (exact rules) ──────────────────
# Every candidate: decide at the daily close (00:00 UTC), fill at the next open.
# "SMA ensemble" = fraction of the 7 SMAs (50, 75, ..., 200 days) that today's close is above.
SMA7 = ("ens_sma", (50, 75, 100, 125, 150, 175, 200))
TSM7 = ("ens_tsmom", (30, 60, 90, 120, 180, 270, 365))

CANDIDATES = {
    # BTC + ETH, inverse-60d-vol sleeves (normalised over both), each sleeve x SMA-ensemble fraction.
    "C1_majors_sma_ens": {"assets": (BTC, ETH), "signal": SMA7, "alloc": "invvol", "check": "daily", "band": 0.10},
    # BTC only, weight = SMA-ensemble fraction.
    "C2_btc_sma_ens": {"assets": (BTC,), "signal": SMA7, "alloc": "equal", "check": "daily", "band": 0.10},
    # C1 with each sleeve scaled by min(1, 50% / its 60-day annualised vol).
    "C3_majors_sma_ens_vt50": {"assets": (BTC, ETH), "signal": SMA7, "alloc": "invvol", "check": "daily", "band": 0.10,
                                   "vol_target": 0.5},
    # not a pick: the 1-12 month TSMOM ensemble, shown as the sibling family check.
    "X_majors_tsm_ens": {"assets": (BTC, ETH), "signal": TSM7, "alloc": "invvol", "check": "daily", "band": 0.10},
}


def candidate(key: str, live: bool = False):
    """A final candidate by key; ``live=True`` gives the ``decide(StrategyContext)`` form."""
    core = TrendMajors(name=key, **CANDIDATES[key])
    return TrendMajorsLive(core) if live else core
