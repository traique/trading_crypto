"""Long/short trend rule for one perpetual contract, with volatility-targeted size and a stop.

``ls_sma`` extends the evaluated spot rule (``btc_sma_ens``: 7 SMAs of 50..200
days) to both directions:

1. Direction, from the daily closes up to decision day D: count the SMAs
   (50, 75, ..., 200 days) the close is above. At least ``long_min`` (6 of 7)
   -> LONG; at most ``short_max`` (1 of 7) -> SHORT; otherwise FLAT. Fewer
   than 201 closes -> FLAT. Only strong, agreeing trends are traded.
2. Size: leverage = ``target_vol`` / realised volatility (30 days,
   annualised), capped at ``max_leverage``; notional = leverage x equity.
   With the defaults (50% target, cap 2x) BTC trades at about 1x.
3. Exchange minimum: an order below the contract's minimum notional is
   raised to it when that needs no more than ``min_order_max_leverage``
   (default 3x); otherwise the book stays flat and says how much capital the
   contract needs. With 10 USDT this is what decides the leverage: ETHUSDT
   (minimum 20 USDT) trades at 2-3x, BTCUSDT (100 USDT) cannot be traded.
4. Stop: ``stop_atr`` x ATR(14) from the entry, sent with the position.
5. Band: a position in the same direction is resized only when the target
   differs by more than ``band`` (25%) of it, to save fees.

The parameters were fixed before testing (the spot rule's lookbacks, round
risk numbers), not optimised. ``research/futures_study.py`` reports how the
rule did on Binance perpetual data 2020-2026 after fees and real funding.
Nothing here promises a profit; with leverage, losses are faster too.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from cryptotrader.futures.rules import FuturesSpec

LOOKBACKS: tuple[int, ...] = (50, 75, 100, 125, 150, 175, 200)


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
    lookbacks: tuple[int, ...] = LOOKBACKS
    long_min: int = 6
    short_max: int = 1
    target_vol: float = 0.5
    max_leverage: float = 2.0
    min_order_max_leverage: float = 3.0
    stop_atr: float = 3.0
    band: float = 0.25
    name: str = "ls_sma"

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

    def decide(self, frame: pd.DataFrame, equity: float, current_qty: float, spec: FuturesSpec) -> dict:
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
        if len(closes) < max(self.lookbacks) + 1:
            out["reason"] = f"mới có {len(closes)} nến, cần {max(self.lookbacks) + 1} - đứng ngoài"
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
            lev = min(self.max_leverage, self.target_vol / vol) if vol else 0.0
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
                    note = f", nâng lên lệnh tối thiểu của sàn {spec.min_notional:g} USDT → đòn bẩy thực x{need_lev:.1f}"
                else:
                    qty = 0.0
                    note = (f", nhưng vốn {equity:.2f} USDT quá nhỏ cho {spec.symbol}: lệnh tối thiểu "
                            f"{spec.min_notional:g} USDT cần đòn bẩy x{need_lev:.1f} > tối đa x{cap:g} "
                            f"(cần ≥ {math.ceil(spec.min_notional / cap)} USDT) - đứng ngoài")
            target = direction * qty
            out["leverage"] = abs(target) * price / equity if equity > 0 else 0.0
            a = atr(frame)
            out["stop_dist"] = self.stop_atr * a if a else None
            out["reason"] = head + (note or f" → đòn bẩy x{out['leverage']:.2f}")
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
