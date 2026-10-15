"""The system decision the agents debate around: a deterministic engine view of one coin.

No LLM decides the action. The engine view comes from the strategy registry
(``cryptotrader.strategies``): the configured strategy (``engine_strategy``,
env ``CRYPTO_ENGINE_STRATEGY``, default ``btc_sma_ens``, the evaluated strategy;
the bot passes its real-signal strategy) is run point-in-time on closed daily candles,
and whether the coin is in its target basket, together with whether the book
holds it, gives the action:

    wanted & not held -> BUY      wanted & held -> HOLD
    not wanted & held -> SELL     not wanted & not held -> NO_TRADE

Holdings come from the caller's ``portfolio`` ({"weights": {pair: w}}); without
one the book is unknown, and a wanted coin reads BUY ("the strategy wants it in
the basket"), an unwanted one NO_TRADE.

This is an interface, meant to be replaced:

- ``set_engine(fn)`` plugs in another engine, ``fn(symbol, trade_date, portfolio,
  config) -> EngineView`` (the bot's live book, the research winner);
- a strategy object may implement ``engine_view(ctx, pair) -> EngineView | dict``
  and the default engine uses it instead of reading its target weights. A dict
  without ``action`` gets it from ``in_basket`` and the holdings; its
  ``levels_extra`` are added to the reference levels (``btc_sma_ens`` adds its 7 SMAs).

The engine view carries every level the agents may cite, each with its
distance from the last close, so no agent has to compute (or invent) one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import timedelta

import pandas as pd

from cryptotrader.data.errors import NoMarketDataError
from cryptotrader.data.symbols import base_asset, normalize_symbol
from cryptotrader.dataflows import date_window, fmt, indicators

logger = logging.getLogger(__name__)

ACTIONS = ("BUY", "HOLD", "SELL", "NO_TRADE")
# The labels each system action allows in an agent's text (Output Guard).
# Words a text for that action may use: a BUY is followed by holding, so HOLD fits it.
COMPATIBLE_ACTIONS = {**{a: frozenset({a}) for a in ACTIONS}, "BUY": frozenset({"BUY", "HOLD"})}
ACTION_VI = {"BUY": "MUA", "HOLD": "GIỮ", "SELL": "BÁN", "NO_TRADE": "ĐỨNG NGOÀI"}


@dataclass
class EngineView:
    """The engine's view of one coin on one day. Plain data, safe to keep in graph state."""

    symbol: str
    as_of_date: str
    action: str
    last_candle_date: str = ""
    price: float | None = None                 # last close (USDT)
    strategy: str = ""
    trend_state: str = ""                      # TĂNG / GIẢM / TRUNG TÍNH / CHƯA ĐỦ DỮ LIỆU
    in_basket: bool | None = None              # whether the strategy wants the coin
    target_weight: float | None = None         # fraction of NAV the strategy targets
    current_weight: float | None = None        # fraction of NAV the book holds (None = unknown)
    levels: dict[str, float] = field(default_factory=dict)   # reference levels, label -> USDT
    entry_low: float | None = None
    entry_high: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source: str = "default"

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"unknown engine action {self.action!r}; expected one of {ACTIONS}")

    def to_state(self) -> dict:
        state = asdict(self)
        state["deterministic_summary"] = render_summary(state)
        return state


EngineFn = Callable[..., EngineView]
_engine: EngineFn | None = None


def set_engine(fn: EngineFn | None) -> None:
    """Plug in an engine ``fn(symbol, trade_date, portfolio=None, config=None) -> EngineView``; None restores the default."""
    global _engine
    _engine = fn


def get_engine() -> EngineFn:
    return _engine or default_engine


def analyze(symbol: str, trade_date: str, portfolio: dict | None = None, config: dict | None = None) -> EngineView:
    view = get_engine()(symbol, trade_date, portfolio=portfolio, config=config)
    if isinstance(view, dict):
        view = EngineView(**{k: v for k, v in view.items() if k in EngineView.__dataclass_fields__})
    return view


# ── the default engine ──
def trend_state(closes: pd.Series) -> str:
    """TĂNG when the close and SMA50 are above SMA200, GIẢM when both below, else TRUNG TÍNH."""
    if len(closes) < 200:
        return "CHƯA ĐỦ DỮ LIỆU"
    last, sma50, sma200 = closes.iloc[-1], closes.iloc[-50:].mean(), closes.iloc[-200:].mean()
    if last > sma200 and sma50 > sma200:
        return "TĂNG"
    if last < sma200 and sma50 < sma200:
        return "GIẢM"
    return "TRUNG TÍNH"


def reference_levels(frame: pd.DataFrame) -> dict[str, float]:
    """Levels the agents may cite: moving averages, recent highs/lows, ATR."""
    closes = frame["Close"].astype(float)
    levels: dict[str, float] = {}
    for n in (20, 50, 200):
        if len(closes) >= n:
            levels[f"SMA{n}"] = float(closes.iloc[-n:].mean())
    for days in (30, 90):
        if len(frame) >= days:
            levels[f"Đáy {days} ngày"] = float(frame["Low"].iloc[-days:].min())
            levels[f"Đỉnh {days} ngày"] = float(frame["High"].iloc[-days:].max())
    year = frame["High"].iloc[-365:]
    if len(year):
        levels["Đỉnh 365 ngày"] = float(year.max())
    atr = indicators.compute(frame, "atr").iloc[-1]
    if pd.notna(atr):
        levels["ATR14"] = float(atr)
    return levels


def _action(wanted: bool, held: bool | None) -> str:
    if wanted:
        return "HOLD" if held else "BUY"
    return "SELL" if held else "NO_TRADE"


def _held_weight(portfolio: dict | None, pair: str) -> float | None:
    if not portfolio:
        return None
    weights = portfolio.get("weights")
    if not isinstance(weights, dict):
        return None
    try:
        return float(weights.get(pair, 0.0) or 0.0)
    except (TypeError, ValueError):
        return None


def _load(symbols, end_day: str, warmup: int) -> dict[str, pd.DataFrame]:
    from cryptotrader.data.ohlcv import load_many

    start = date_window.parse_day(end_day) - timedelta(days=max(400, warmup + 30))
    return load_many(symbols, end_day, start=f"{start:%Y-%m-%d}")


def default_engine(symbol: str, trade_date: str, portfolio: dict | None = None,
                   config: dict | None = None) -> EngineView:
    from cryptotrader.strategies import DEFAULT_STRATEGY, StrategyContext, get_strategy

    config = config or {}
    pair = normalize_symbol(symbol)
    name = config.get("engine_strategy") or DEFAULT_STRATEGY
    strategy = get_strategy(name)
    frames = _load({pair, *strategy.symbols}, trade_date, int(getattr(strategy, "warmup_days", 0) or 0))
    frame = frames.get(pair)
    if frame is None or frame.empty:
        raise NoMarketDataError(pair, f"không có nến ngày nào đến {trade_date}")
    last_day = f"{pd.Timestamp(frame['Date'].iloc[-1]):%Y-%m-%d}"
    held_w = _held_weight(portfolio, pair)
    weights = dict((portfolio or {}).get("weights") or {})
    # ``holdings_known`` tells a custom engine_view whether {} weights mean "holds
    # nothing" (a known book) or "unknown" (no portfolio given).
    ctx = StrategyContext(date=last_day, history=frames, weights=weights,
                          nav=float((portfolio or {}).get("nav") or 0.0),
                          cash=float((portfolio or {}).get("cash") or 0.0),
                          state={"holdings_known": held_w is not None})
    price = float(frame["Close"].iloc[-1])
    view_kwargs = {"symbol": pair, "as_of_date": trade_date, "last_candle_date": last_day, "price": price,
                       "strategy": name, "trend_state": trend_state(frame["Close"].astype(float)),
                       "levels": reference_levels(frame), "current_weight": held_w, "source": f"strategy:{name}"}
    held = None if held_w is None else held_w > 0
    custom = getattr(strategy, "engine_view", None)
    if callable(custom):
        produced = custom(ctx, pair)
        if isinstance(produced, EngineView):
            return produced
        produced = dict(produced)
        # A view may leave the action to the engine (it knows the holdings) and add levels.
        if "action" not in produced:
            produced["action"] = _action(bool(produced.get("in_basket")), held)
            if held is None:
                produced["reasons"] = [*produced.get("reasons", []),
                                       "không biết danh mục đang giữ gì: BUY ở đây nghĩa là coin có trong rổ mục tiêu"
                                       if produced.get("in_basket") else
                                       "không biết danh mục đang giữ gì"]
        extra = produced.pop("levels_extra", None) or {}
        merged = {**view_kwargs, **produced}
        merged["levels"] = {**extra, **view_kwargs["levels"]}
        return EngineView(**{k: v for k, v in merged.items() if k in EngineView.__dataclass_fields__})

    reasons = []
    if pair not in strategy.symbols:
        wanted, target = False, None
        reasons.append(f"{base_asset(pair)} không nằm trong danh sách coin của chiến lược {name} "
                       f"({', '.join(base_asset(s) for s in strategy.symbols)})")
    else:
        decision = strategy.decide(ctx)
        if isinstance(decision, dict):
            target = float(decision.get(pair, 0.0) or 0.0)
            wanted = target > 0
            reasons.append(f"chiến lược {name} nhắm tỷ trọng {fmt.pct(target * 100, 0)} NAV cho {base_asset(pair)}"
                           if wanted else f"chiến lược {name} không nhắm giữ {base_asset(pair)} hôm nay")
        else:
            sides = {o.side.upper() for o in decision if getattr(o, "symbol", None) == pair}
            target = None
            wanted = "BUY" in sides or (held is True and "SELL" not in sides)
            reasons.append(f"chiến lược {name} ra lệnh: {', '.join(sorted(sides)) or 'không có lệnh'}")
    if held is None:
        reasons.append("không biết danh mục đang giữ gì: BUY ở đây nghĩa là coin có trong rổ mục tiêu")
    return EngineView(action=_action(wanted, held), in_basket=wanted, target_weight=target,
                      reasons=reasons, **view_kwargs)


# ── rendering ──
def render_summary(state: dict) -> str:
    """One paragraph: the action, the strategy, the basket, the price."""
    action = state.get("action") or "NO_TRADE"
    pair = state.get("symbol") or ""
    parts = [f"Tín hiệu hệ thống cho {base_asset(pair) if pair else '?'}/USDT: {action} ({ACTION_VI.get(action, action)})"]
    if state.get("strategy"):
        parts.append(f"theo chiến lược {state['strategy']}")
    if state.get("price") is not None:
        parts.append(f"giá đóng cửa nến {state.get('last_candle_date') or '?'} là {fmt.price(state['price'])} USDT")
    if state.get("in_basket") is not None:
        parts.append("coin " + ("CÓ" if state["in_basket"] else "KHÔNG") + " trong rổ mục tiêu")
    reasons = "; ".join(state.get("reasons") or [])
    return ", ".join(parts) + (f". Lý do: {reasons}." if reasons else ".")


def render_report(state: dict) -> str:
    """Everything the agents may cite, with every level's % distance from the last close pre-computed."""
    pair = state.get("symbol") or "?"
    price = state.get("price")
    lines = [
        f"=== QUYẾT ĐỊNH HỆ THỐNG - {base_asset(pair)}/USDT (Binance spot) ===",
        f"Hành động: {state.get('action')} ({ACTION_VI.get(state.get('action'), '')})",
        f"Chiến lược: {state.get('strategy') or 'không rõ'} (nguồn: {state.get('source') or '?'})",
        f"Ngày phân tích: {state.get('as_of_date')}. Nến ngày (UTC) đã đóng gần nhất: "
        f"{state.get('last_candle_date') or '?'}, giá đóng cửa {fmt.price(price)} USDT.",
        f"Xu hướng (giá/SMA50 so với SMA200): {state.get('trend_state') or 'không rõ'}",
    ]
    if state.get("in_basket") is not None:
        lines.append(f"Trong rổ mục tiêu của chiến lược: {'CÓ' if state['in_basket'] else 'KHÔNG'}")
    if state.get("target_weight") is not None:
        lines.append(f"Tỷ trọng mục tiêu: {fmt.pct(state['target_weight'] * 100, 1)} NAV")
    if state.get("current_weight") is not None:
        lines.append(f"Tỷ trọng đang giữ: {fmt.pct(state['current_weight'] * 100, 1)} NAV")
    else:
        lines.append("Tỷ trọng đang giữ: không biết (không có danh mục)")
    plan = [("Vùng mua từ", state.get("entry_low")), ("Vùng mua đến", state.get("entry_high")),
            ("Stop", state.get("stop_price")), ("Mục tiêu", state.get("target_price"))]
    plan = [(k, v) for k, v in plan if v is not None]
    if plan:
        lines.append("Kế hoạch của hệ thống:")
        lines += [f"- {k}: {fmt.price(v)} USDT ({fmt.signed_pct(fmt.distance_pct(price, v))} so với giá)"
                  for k, v in plan]
    else:
        lines.append("Kế hoạch của hệ thống: không có vùng mua/stop/mục tiêu cố định - chiến lược vào/ra theo "
                     "tín hiệu đóng cửa ngày, lệnh khớp ở giá mở cửa nến sau.")
    levels = state.get("levels") or {}
    if levels:
        lines.append("Các mốc tham chiếu (tính trên nến đã đóng):")
        for label, value in levels.items():
            if label.startswith("ATR"):
                lines.append(f"- {label}: {fmt.price(value)} USDT "
                             f"({fmt.pct((value / price * 100) if price else None)} giá)")
            else:
                lines.append(f"- {label}: {fmt.price(value)} USDT ({fmt.signed_pct(fmt.distance_pct(price, value))} so với giá)")
    if state.get("reasons"):
        lines.append("Lý do: " + "; ".join(state["reasons"]))
    for note in state.get("notes") or []:
        lines.append(f"Ghi chú: {note}")
    lines.append("Phí giả định: taker 0.1% mỗi chiều + trượt giá; không đòn bẩy, không bán khống (spot).")
    return "\n".join(lines)


def create_quant_engine(config: dict | None = None):
    """The graph node: the engine view in state; a failure degrades to NO_TRADE, never ends the run."""

    def quant_engine_node(state) -> dict:
        symbol = state["company_of_interest"]
        trade_date = state["trade_date"]
        try:
            view = analyze(symbol, trade_date, portfolio=state.get("portfolio") or None, config=config)
            decision = view.to_state()
        except Exception as exc:  # noqa: BLE001 - degrade to NO_TRADE, never crash the run
            logger.warning("Engine failed for %s on %s: %s", symbol, trade_date, exc)
            decision = EngineView(symbol=symbol, as_of_date=trade_date, action="NO_TRADE",
                                  reasons=[f"hệ thống không chạy được: {exc}"], source="error").to_state()
            return {"quant_decision": decision, "quant_report": ""}
        return {"quant_decision": decision, "quant_report": render_report(decision)}

    return quant_engine_node
