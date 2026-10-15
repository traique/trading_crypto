"""Long/short trend rule for one perpetual contract, with volatility-targeted size and a stop.

``ls_sma`` counts how many simple moving averages the daily close is above:

1. Direction, from the daily closes up to decision day D: at least
   ``long_min`` of the 7 SMAs -> LONG; at most ``short_max`` -> SHORT;
   otherwise FLAT (and FLAT during the warm-up).
2. BTC filter (``btc_filter``): an altcoin trades only in the direction of
   BTC's own signal on the same day (BTC flat -> the altcoin is flat too).
3. Size: leverage = ``target_vol`` / realised volatility (30 days,
   annualised), capped at ``max_leverage``; notional = leverage x equity.
4. Exchange minimum: an order below the contract's minimum notional is
   raised to it when that needs no more than ``min_order_max_leverage``
   (default 3x); otherwise the book stays flat and says how much capital the
   contract needs. With 10 USDT this decides the leverage: ETHUSDT (minimum
   20 USDT) trades at 2-3x, BTCUSDT (50 USDT) cannot be traded.
5. Stop: ``stop_atr`` x ATR(14) from the entry, sent with the position.
6. Band: a position in the same direction is resized only when the target
   differs by more than ``band`` (25%) of it, to save fees.

Two parameter sets (``PRESETS``):

- ``v2`` (default): chosen by the walk-forward optimisation of
  ``research/optimize_futures.py`` (2592 rules, 14 coins, best median Sharpe
  on TRAIN 2020-2022, then the best of the top 20 on VALID 2023, TEST
  2024-09/2026 run once): SMAs of 20..80 days, long at >= 6/7, short at
  <= 2/7, 30% target volatility, BTC filter, a 4-ATR stop (the optimiser's
  pick had none; the 4-ATR neighbour scores the same and keeps a forced-
  leverage 10 USDT account from being liquidated). TEST, median of 14 coins,
  1000 USDT: +7.4%/year, max drawdown -35% (v1: +3.4%, -55%).
- ``v1``: the first rule (SMAs 50..200 of the spot strategy, >= 6/7 and
  <= 1/7, 50% target volatility, 3-ATR stop, no BTC filter), fixed before
  any test.

Nothing here promises a profit; with leverage, losses are faster too.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from cryptotrader.futures.rules import FuturesSpec

LOOKBACKS: tuple[int, ...] = (50, 75, 100, 125, 150, 175, 200)
FAST_LOOKBACKS: tuple[int, ...] = (20, 30, 40, 50, 60, 70, 80)
BTC = "BTCUSDT"

PRESETS: dict[str, dict] = {
    "v2": {"lookbacks": FAST_LOOKBACKS, "long_min": 6, "short_max": 2, "target_vol": 0.3,
           "max_leverage": 2.0, "stop_atr": 4.0, "band": 0.25, "btc_filter": True},
    "v1": {"lookbacks": LOOKBACKS, "long_min": 6, "short_max": 1, "target_vol": 0.5,
           "max_leverage": 2.0, "stop_atr": 3.0, "band": 0.25, "btc_filter": False},
}


def atr(frame: pd.DataFrame, n: int = 14) -> float | None:
    """Average true range of the last ``n`` days (simple mean), from Date/High/Low/Close."""
    if len(frame) < n + 1:
        return None
    high, low, close = (frame[k].to_numpy(dtype=float) for k in ("High", "Low", "Close"))
    prev = close[:-1]
    tr = np.maximum.reduce([high[1:] - low[1:], np.abs(high[1:] - prev), np.abs(low[1:] - prev)])
    value = float(np.mean(tr[-n:]))
    return value if math.isfinite(value) and value > 0 else None


def realised_vol(closes: np.ndarray, n: int = 30) -> float | None:
    """Annualised volatility of the last ``n`` daily log returns."""
    if len(closes) < n + 1:
        return None
    rets = np.diff(np.log(closes[-(n + 1):]))
    value = float(np.std(rets, ddof=1) * math.sqrt(365))
    return value if math.isfinite(value) and value > 0 else None


@dataclass
class LongShortSma:
    lookbacks: tuple[int, ...] = FAST_LOOKBACKS
    long_min: int = 6
    short_max: int = 2
    target_vol: float = 0.3
    max_leverage: float = 2.0
    min_order_max_leverage: float = 3.0
    stop_atr: float = 4.0
    band: float = 0.25
    btc_filter: bool = True
    name: str = "ls_sma"

    @classmethod
    def preset(cls, name: str = "v2", **overrides) -> LongShortSma:
        """The ``v2`` (optimised, default) or ``v1`` parameter set, with ``overrides``."""
        if name not in PRESETS:
            raise ValueError(f"unknown ls_sma preset {name!r}: {', '.join(PRESETS)}")
        return cls(**{**PRESETS[name], **{k: v for k, v in overrides.items() if v is not None}})

    @property
    def warmup_days(self) -> int:
        return max(self.lookbacks) + 3

    def direction(self, closes: np.ndarray) -> tuple[int, int]:
        """(+1 long / -1 short / 0 flat, number of SMAs the close is above)."""
        if len(closes) < max(self.lookbacks) + 1:
            return 0, 0
        last = closes[-1]
        above = sum(1 for n in self.lookbacks if last > closes[-n:].mean())
        if above >= self.long_min:
            return 1, above
        if above <= self.short_max:
            return -1, above
        return 0, above

    def btc_direction(self, btc_frame: pd.DataFrame | None, day) -> int:
        """BTC's own direction on ``day`` (0 when its candle of that day is missing)."""
        if btc_frame is None or not len(btc_frame):
            return 0
        if pd.Timestamp(btc_frame["Date"].iloc[-1]).normalize() != pd.Timestamp(day).normalize():
            return 0
        closes = btc_frame["Close"].to_numpy(dtype=float)
        return self.direction(closes[np.isfinite(closes)][-self.warmup_days:])[0]

    def decide(self, frame: pd.DataFrame, equity: float, current_qty: float, spec: FuturesSpec,
               btc_frame: pd.DataFrame | None = None, exposure: float = 1.0) -> dict:
        """The target for the next candle from candles up to the decision day (the frame's last row).

        Returns {"trade": bool, "qty": signed target, "stop_dist", "direction", "above", "leverage",
        "vol", "price", "reason"}; ``trade`` False means keep the current position."""
        closes = frame["Close"].to_numpy(dtype=float)
        closes = closes[np.isfinite(closes)]
        out = {"trade": False, "qty": current_qty, "stop_dist": None, "direction": 0, "above": 0,
               "leverage": 0.0, "vol": None, "price": float(closes[-1]) if len(closes) else None,
               "of": len(self.lookbacks), "reason": ""}
        if not len(closes):
            out["reason"] = "không có giá"
            return out
        price = float(closes[-1])
        direction, above = self.direction(closes)
        out.update(direction=direction, above=above)
        filtered = ""
        if self.btc_filter and direction and spec.symbol != BTC:
            btc_dir = self.btc_direction(btc_frame, frame["Date"].iloc[-1])
            if btc_dir != direction:
                side = "LONG" if direction > 0 else "SHORT"
                btc_txt = {1: "đang tăng", -1: "đang giảm", 0: "chưa có xu hướng rõ"}[btc_dir]
                filtered = f"BTC {btc_txt}; chỉ {side} khi BTC cũng {'tăng' if direction > 0 else 'giảm'}"
                direction = 0
                out["direction"] = 0
        if len(closes) < max(self.lookbacks) + 1:
            out["reason"] = f"mới có {len(closes)} nến, cần {max(self.lookbacks) + 1} - đứng ngoài"
            target = 0.0
        elif direction == 0 and filtered:
            out["reason"] = f"giá trên {above}/{len(self.lookbacks)} SMA nhưng {filtered} (bộ lọc BTC) - đứng ngoài"
            target = 0.0
        elif direction == 0:
            out["reason"] = f"giá trên {above}/{len(self.lookbacks)} SMA: không có xu hướng rõ - đứng ngoài"
            target = 0.0
        elif equity <= 0:
            out["reason"] = "hết vốn"
            target = 0.0
        else:
            vol = realised_vol(closes)
            out["vol"] = vol
            # ``exposure`` < 1: the account policy's drawdown scaling (runner.step).
            lev = min(self.max_leverage, self.target_vol / vol) * max(0.0, min(1.0, exposure)) if vol else 0.0
            qty = spec.floor_qty(lev * equity / price)
            side = "LONG" if direction > 0 else "SHORT"
            head = (f"giá trên {above}/{len(self.lookbacks)} SMA → {side}, biến động "
                    f"{(vol or 0) * 100:.0f}%/năm")
            note = ""
            if qty * price < spec.min_notional:
                need = spec.min_qty_at(price)
                need_lev = need * price / equity
                cap = max(self.max_leverage, self.min_order_max_leverage)
                if need_lev <= cap + 1e-9:
                    qty = need
                    note = (f", nâng lên lệnh tối thiểu của sàn {spec.min_notional:g} USDT → đòn bẩy thực "
                            f"x{need_lev:.1f}")
                    if lev > 0 and need_lev > 2 * lev:
                        note += (f" (⚠️ gấp {need_lev / lev:.0f} lần mức chiến lược muốn x{lev:.2f}: vốn quá nhỏ "
                                 "cho cặp này, rủi ro cao hơn thiết kế)")
                else:
                    qty = 0.0
                    note = (f", nhưng vốn {equity:.2f} USDT quá nhỏ cho {spec.symbol}: lệnh tối thiểu "
                            f"{spec.min_notional:g} USDT cần đòn bẩy x{need_lev:.1f} > tối đa x{cap:g} "
                            f"(cần ≥ {math.ceil(spec.min_notional / cap)} USDT) - đứng ngoài")
            target = direction * qty
            out["leverage"] = abs(target) * price / equity if equity > 0 else 0.0
            a = atr(frame)
            out["stop_dist"] = self.stop_atr * a if a else None
            scaled = f" (cỡ {exposure * 100:.0f}% vì đang sụt giảm)" if exposure < 0.999 else ""
            out["reason"] = head + (note or f" → đòn bẩy x{out['leverage']:.2f}") + scaled
        same_side = current_qty and target and (current_qty > 0) == (target > 0)
        if same_side and abs(target - current_qty) <= self.band * abs(current_qty):
            out["qty"] = current_qty
            out["reason"] += f"; vị thế hiện tại lệch mục tiêu không quá {self.band * 100:.0f}% nên giữ nguyên"
            return out
        if abs(target - current_qty) < 1e-12:
            out["qty"] = current_qty
            return out
        out.update(trade=True, qty=target)
        return out
