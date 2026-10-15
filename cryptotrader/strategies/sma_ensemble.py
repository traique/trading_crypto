"""The evaluated strategy: BTC weight = share of 7 simple moving averages the close is above.

This is ``C2_btc_sma_ens``, the pick of the independent evaluation of
2026-10-15 (``research/RESULTS.md``). The rule, exactly as evaluated:

1. Decide once a day at the close of UTC day D (00:00 UTC = 07:00 Vietnam
   time) from BTCUSDT daily closes up to and including D. No candle on D:
   change nothing.
2. Warm-up: with fewer than 201 closes the target is 0 (all USDT).
3. For each n in (50, 75, 100, 125, 150, 175, 200): vote_n = 1 when
   close_D > mean of the last n closes (D included), else 0.
4. Ideal BTC weight w* = (sum of votes) / 7 (steps of 1/7); the rest stays in
   USDT. No leverage, no shorting.
5. Band: with w the current BTC weight at D's close, trade to w* when
   (w > 0.005) differs from (w* > 0.005); otherwise only when |w - w*| > 0.10.
   Else no order.
6. Orders are market orders that fill at D+1's open (the ledger's job).

The arithmetic mirrors the evaluated code (numpy means over the last n closes
of a float64 array, at most 203 closes): ``tests/test_strategy_sma_ensemble.py``
proves identical fills against a frozen copy of that code on a synthetic
panel, and ``research/RESULTS.md`` records the same check on the real data.

Expected behaviour (out of sample, after 0.1% fee + 0.1% slippage per fill):
roughly BTC's return over a full cycle with about half of BTC's drawdowns; it
trails BTC by tens of points in strong bull years. Not a promise of profit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from cryptotrader.market.rules import BENCHMARK
from cryptotrader.strategies.base import Decision, StrategyContext

LOOKBACKS: tuple[int, ...] = (50, 75, 100, 125, 150, 175, 200)
BAND = 0.10                 # drift tolerance (absolute weight) before a resize
ON_EPS = 0.005              # a weight above this counts as "holding"


def sma_votes(closes: np.ndarray, lookbacks: tuple[int, ...] = LOOKBACKS) -> list[tuple[int, float, bool]]:
    """(n, SMA_n, close > SMA_n) for each lookback, from closes ending on the decision day."""
    last = closes[-1]
    out = []
    for n in lookbacks:
        sma = closes[-n:].mean()
        out.append((int(n), float(sma), bool(last > sma)))
    return out


def ideal_weight(closes: np.ndarray, lookbacks: tuple[int, ...] = LOOKBACKS) -> float:
    """w*: the share of SMAs the last close is above; 0 during the warm-up."""
    if len(closes) < max(lookbacks) + 1:
        return 0.0
    return float(np.mean([last_above for _, _, last_above in sma_votes(closes, lookbacks)]))


def band_target(current: float, ideal: float, band: float = BAND) -> float | None:
    """The weight to trade to, or None to leave the book as it is (the drift band)."""
    if (current > ON_EPS) == (ideal > ON_EPS) and abs(current - ideal) <= band:
        return None
    return ideal


@dataclass
class BtcSmaEnsemble:
    """BTCUSDT only; weight = fraction of the 7 SMAs (50..200 days) the close is above; 10% band."""

    lookbacks: tuple[int, ...] = LOOKBACKS
    band: float = BAND
    name: str = "btc_sma_ens"
    symbols: tuple[str, ...] = (BENCHMARK,)
    # The evaluated research form plans with 0.5; every trade of this rule is at least
    # 1/7 or 10% of NAV, so the bot's 1% would give the same fills.
    min_trade_pct: float = 0.5
    universe_label: str = "-"

    def __post_init__(self) -> None:
        self.lookbacks = tuple(int(n) for n in self.lookbacks)
        if not self.lookbacks or min(self.lookbacks) < 2:
            raise ValueError(f"lookbacks must be at least 2 days: {self.lookbacks!r}")
        self.band = float(self.band)

    @property
    def warmup_days(self) -> int:
        """Closes the decision reads (the evaluated window: longest SMA + 3)."""
        return max(self.lookbacks) + 3

    @property
    def min_closes(self) -> int:
        """Below this many closes the target is 0 (warm-up)."""
        return max(self.lookbacks) + 1

    # ── the rule ──
    def _closes(self, ctx: StrategyContext) -> np.ndarray:
        return ctx.closes(BENCHMARK).dropna().to_numpy()[-self.warmup_days:]

    def assess(self, ctx: StrategyContext) -> dict | None:
        """The day's signal as plain data (None when BTC has no candle on the day)."""
        if ctx.last_close(BENCHMARK) is None:
            return None
        closes = self._closes(ctx)
        ready = len(closes) >= self.min_closes
        votes = sma_votes(closes, self.lookbacks) if ready else []
        ideal = float(np.mean([v[2] for v in votes])) if ready else 0.0
        current = float(dict(ctx.weights).get(BENCHMARK, 0.0) or 0.0)
        target = band_target(current, ideal, self.band)
        return {"day": ctx.date, "close": float(closes[-1]), "ready": ready, "n_closes": int(len(closes)),
                "votes": [[n, round(sma, 8), above] for n, sma, above in votes],
                "above": sum(1 for v in votes if v[2]), "of": len(self.lookbacks),
                "ideal": ideal, "current": round(current, 6), "trade": target is not None}

    def decide(self, ctx: StrategyContext) -> Decision:
        signal = self.assess(ctx)
        if signal is None:
            return dict(ctx.weights)            # no candle today: change nothing
        ctx.state["signal"] = signal            # for reports, /nhanh and the real-account signal
        if not signal["trade"]:
            return dict(ctx.weights)            # inside the band: keep the book as it is
        ideal = signal["ideal"]
        return {BENCHMARK: ideal} if ideal > 0 else {}

    # ── hooks used by the bot and the agents ──
    def real_target(self, weights: Mapping[str, float], state: Mapping, day: str) -> dict | None:
        """The same band rule applied to ANOTHER book (the real account) on ``day``.

        Uses the ideal weight the paper book computed that day (``state["signal"]``).
        Returns target weights, None for "no order", and raises LookupError when
        the day's signal is not known (the caller then falls back)."""
        signal = (state or {}).get("signal") or {}
        if signal.get("day") != day:
            raise LookupError(f"no {self.name} signal for {day}")
        current = float(weights.get(BENCHMARK, 0.0) or 0.0)
        others = any(float(w or 0.0) > ON_EPS for s, w in weights.items() if s != BENCHMARK)
        target = band_target(current, float(signal["ideal"]), self.band)
        if target is None:
            if not others:
                return None
            target = current                    # BTC stays; coins outside the rule are sold
        return {BENCHMARK: target} if target > ON_EPS else {}

    def explain(self, state: Mapping | None) -> str:
        """One Vietnamese line on the last signal (for the daily report)."""
        signal = (state or {}).get("signal") or {}
        if not signal:
            return ""
        if not signal.get("ready"):
            return (f"Tín hiệu {signal.get('day')}: mới có {signal.get('n_closes')} nến, cần "
                    f"{self.min_closes} để tính đủ 7 SMA - giữ USDT.")
        above, of = signal.get("above", 0), signal.get("of", len(self.lookbacks))
        ideal, current = float(signal.get("ideal", 0.0)), float(signal.get("current", 0.0))
        text = (f"Tín hiệu {signal.get('day')}: giá trên {above}/{of} đường SMA "
                f"({'/'.join(str(n) for n in self.lookbacks)} ngày) → BTC lý tưởng {ideal * 100:.0f}% NAV")
        if signal.get("trade"):
            return text + f"; đang {current * 100:.0f}% → đổi về mục tiêu."
        return text + (f"; đang {current * 100:.0f}%, lệch không quá {self.band * 100:.0f}% nên giữ nguyên "
                       "(tránh phí).")

    def explain_closes(self, pair: str, closes) -> list[str]:
        """/nhanh lines for ``pair`` from its closes up to the day (a pandas Series or array)."""
        if pair != BENCHMARK:
            return [f"Chiến lược chính ({self.name}) chỉ giao dịch BTC; coin này không nằm trong rổ."]
        arr = np.asarray(closes, dtype=float)
        arr = arr[~np.isnan(arr)][-self.warmup_days:]
        if len(arr) < self.min_closes:
            return [f"Chiến lược chính ({self.name}): mới có {len(arr)} nến, cần {self.min_closes} - mục tiêu 0% BTC."]
        votes = sma_votes(arr, self.lookbacks)
        above = sum(1 for v in votes if v[2])
        marks = " ".join(f"SMA{n} {'✓' if ok else '✗'}" for n, _, ok in votes)
        return [f"Chiến lược chính ({self.name}): giá trên {above}/{len(votes)} SMA → mục tiêu BTC "
                f"{above / len(votes) * 100:.0f}% NAV (chỉ đổi khi lệch quá {self.band * 100:.0f}%).",
                marks]

    def engine_view(self, ctx: StrategyContext, pair: str) -> dict:
        """What the agents' quant engine needs: basket, target weight, reasons, the SMA levels.

        When the holdings are known (``ctx.state["holdings_known"]``), the action is the
        daily signal's own: the 10% band applied to the held BTC weight (BUY / SELL to
        the band target, HOLD or NO_TRADE inside the band), so /phantich never
        contradicts the report. Unknown holdings: no ``action``, the engine derives it
        from ``in_basket``."""
        if pair != BENCHMARK:
            return {"in_basket": False, "target_weight": 0.0,
                    "reasons": [f"chiến lược {self.name} chỉ giao dịch BTC (phần còn lại giữ USDT); "
                                f"{pair[:-4] if pair.endswith('USDT') else pair} không nằm trong rổ"]}
        closes = self._closes(ctx)
        if len(closes) < self.min_closes:
            return {"in_basket": False, "target_weight": 0.0,
                    "reasons": [f"mới có {len(closes)} nến ngày, chiến lược cần {self.min_closes} - giữ USDT"]}
        votes = sma_votes(closes, self.lookbacks)
        above = sum(1 for v in votes if v[2])
        ideal = above / len(votes)
        reasons = [f"giá đóng cửa trên {above}/{len(votes)} đường SMA "
                   f"({', '.join(f'SMA{n} ' + ('✓' if ok else '✗') for n, _, ok in votes)}) "
                   f"→ tỷ trọng BTC lý tưởng {ideal * 100:.0f}% NAV"]
        view = {"in_basket": ideal > 0, "target_weight": ideal, "reasons": reasons,
                "levels_extra": {f"SMA{n}": sma for n, sma, _ in votes}}
        weights = dict(ctx.weights)
        known = bool((ctx.state or {}).get("holdings_known")) or bool(weights)
        if not known:
            return view
        current = float(weights.get(BENCHMARK, 0.0) or 0.0)
        target = band_target(current, ideal, self.band)
        if target is None:
            view["action"] = "HOLD" if current > ON_EPS else "NO_TRADE"
            view["target_weight"] = current
            reasons.append(f"đang giữ {current * 100:.0f}% NAV: lệch không quá {self.band * 100:.0f}% "
                           "so với tỷ trọng lý tưởng nên giữ nguyên")
        else:
            view["action"] = "BUY" if target > current else "SELL"
            view["target_weight"] = target
            reasons.append(f"đang giữ {current * 100:.0f}% NAV: đổi về mục tiêu {target * 100:.0f}% NAV")
        return view
