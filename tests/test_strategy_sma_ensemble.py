"""btc_sma_ens: the evaluated rule, its parity with the frozen evaluated code, and its hooks."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cryptotrader.strategies import DEFAULT_STRATEGY, REGISTRY, StrategyContext, get_strategy
from cryptotrader.strategies.sma_ensemble import (
    LOOKBACKS,
    BtcSmaEnsemble,
    band_target,
    ideal_weight,
)
from research import config
from research.backtest import run
from research.data import make_panel
from research.evaluated import EVALUATED_SHA256
from research.strategies import STRATEGIES, evaluated_c2, make

pytestmark = pytest.mark.unit

BTC = "BTCUSDT"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _research_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_ENV, str(tmp_path / "research_data"))


def regime_walk(n: int, seed: int, start: float = 10_000.0) -> np.ndarray:
    """A random walk with alternating bull/bear drifts, so the SMA votes change often."""
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([-0.004, -0.001, 0.002, 0.005], size=n // 60 + 1), 60)[:n]
    return start * np.exp(np.cumsum(drift + rng.normal(0, 0.035, n)))


def candles(first: str, closes: np.ndarray, seed: int = 0, drop=()) -> pd.DataFrame:
    rng = np.random.default_rng(seed + 1)
    opens = np.r_[closes[0], closes[:-1]] * np.exp(rng.normal(0, 0.004, len(closes)))
    frame = pd.DataFrame({"Date": pd.date_range(first, periods=len(closes), freq="D"), "Open": opens,
                          "High": np.maximum(opens, closes) * 1.02, "Low": np.minimum(opens, closes) * 0.98,
                          "Close": closes, "Volume": 1_000.0, "QuoteVolume": closes * 1_000.0, "Trades": 100.0})
    return frame.drop(index=list(drop)).reset_index(drop=True)


def ctx_for(closes, weights=None, day=None, state=None) -> StrategyContext:
    frame = candles("2024-01-01", np.asarray(closes, dtype=float))
    day = day or f"{frame['Date'].iloc[-1]:%Y-%m-%d}"
    return StrategyContext(day, {BTC: frame}, weights=weights or {}, state={} if state is None else state)


# ── the frozen evaluated code ──
def test_the_frozen_evaluated_code_is_byte_identical():
    for name, digest in EVALUATED_SHA256.items():
        data = (ROOT / "research" / "evaluated" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest


# ── parity: identical fills on a synthetic panel ──
@pytest.mark.parametrize("seed", [3, 11])
def test_the_live_module_fills_exactly_like_the_evaluated_code(seed):
    closes = regime_walk(1_300, seed)
    # Days without a BTC candle (a short halt): "change nothing" must match too.
    panel = make_panel({BTC: candles("2019-01-01", closes, seed, drop=(400, 401, 777))})
    start, end = "2019-01-01", "2022-07-23"
    evaluated = run(make("btc_sma_ens_eval"), start, end, panel=panel)
    live = run(make("btc_sma_ens"), start, end, panel=panel)
    cols = ["date", "symbol", "side", "qty", "price", "fee"]
    assert len(evaluated.fills) > 40                         # the rule really trades here
    pd.testing.assert_frame_equal(evaluated.fills[cols].reset_index(drop=True), live.fills[cols].reset_index(drop=True))
    np.testing.assert_array_equal(evaluated.daily["nav"].to_numpy(), live.daily["nav"].to_numpy())
    # The bot plans with 1% minimum trades: still the same fills (every trade is >= 1/7 or > 10% of NAV).
    bot_form = make("btc_sma_ens")
    bot_form.min_trade_pct = 1.0
    again = run(bot_form, start, end, panel=panel)
    pd.testing.assert_frame_equal(evaluated.fills[cols].reset_index(drop=True), again.fills[cols].reset_index(drop=True))
    # The command-line check (python -m research.parity) agrees.
    from research.parity import compare

    result = compare(start, end, panel, bot_min_trade_pct=1.0)
    assert result["identical_fills"] and result["max_abs_nav_diff"] == 0.0
    assert result["live"] == result["evaluated"]


def test_the_research_names_drive_the_live_module_and_the_frozen_code():
    assert {"btc_sma_ens", "btc_sma_ens_eval"} <= set(STRATEGIES)
    assert isinstance(make("btc_sma_ens"), BtcSmaEnsemble)
    frozen = evaluated_c2()
    assert frozen.name == "C2_btc_sma_ens" and frozen.signal == ("ens_sma", LOOKBACKS) and frozen.band == 0.10


# ── the rule ──
def test_registered_as_the_default():
    assert DEFAULT_STRATEGY == "btc_sma_ens" and REGISTRY["btc_sma_ens"] is BtcSmaEnsemble
    s = get_strategy("btc_sma_ens")
    assert s.symbols == (BTC,) and s.warmup_days == 203 and s.min_closes == 201


def test_warm_up_holds_usdt_until_201_closes():
    rising = np.linspace(100, 300, 201)
    assert ideal_weight(rising[:200]) == 0.0
    assert ideal_weight(rising) == 1.0
    s = BtcSmaEnsemble()
    assert s.decide(ctx_for(rising[:200])) == {}
    assert s.decide(ctx_for(rising)) == {BTC: 1.0}


def test_the_weight_is_the_share_of_smas_below_the_close():
    # 250 days up, then a dip: the close sits above the long SMAs and below the short ones.
    closes = np.r_[np.linspace(100, 400, 265), np.linspace(400, 350, 15)]
    s = BtcSmaEnsemble()
    signal = s.assess(ctx_for(closes))
    above = sum(1 for n in LOOKBACKS if closes[-1] > closes[-n:].mean())
    assert 0 < above < 7 and signal["above"] == above
    assert signal["ideal"] == pytest.approx(above / 7)
    assert s.decide(ctx_for(closes)) == {BTC: above / 7}


def test_the_band_avoids_small_resizes():
    assert band_target(0.62, 4 / 7) is None                 # 4.9% off: keep
    assert band_target(0.70, 4 / 7) == 4 / 7                # 12.9% off: resize
    assert band_target(0.0, 1 / 7) == 1 / 7                 # flat -> holding: always
    assert band_target(0.05, 0.0) == 0.0                    # holding -> flat: always
    assert band_target(0.004, 0.0) is None                  # dust counts as flat
    rising = np.linspace(100, 300, 260)
    s = BtcSmaEnsemble()
    state: dict = {}
    assert s.decide(ctx_for(rising, {BTC: 0.95}, state=state)) == {BTC: 0.95}   # inside the band: unchanged
    assert state["signal"]["trade"] is False and state["signal"]["ideal"] == 1.0


def test_no_candle_on_the_day_changes_nothing():
    closes = np.linspace(100, 300, 260)
    ctx = ctx_for(closes, {BTC: 0.3}, day="2030-01-01")
    assert BtcSmaEnsemble().decide(ctx) == {BTC: 0.3} and "signal" not in ctx.state


def test_the_real_account_follows_the_same_band():
    s = BtcSmaEnsemble()
    state = {"signal": {"day": "2026-10-14", "ideal": 4 / 7}}
    assert s.real_target({BTC: 0.6}, state, "2026-10-14") is None                     # within the band
    assert s.real_target({}, state, "2026-10-14") == {BTC: 4 / 7}                     # flat -> buy
    assert s.real_target({BTC: 0.9}, state, "2026-10-14") == {BTC: 4 / 7}
    assert s.real_target({BTC: 0.6, "ETHUSDT": 0.2}, state, "2026-10-14") == {BTC: 0.6}   # stray coin sold
    assert s.real_target({BTC: 0.6}, {"signal": {"day": "2026-10-14", "ideal": 0.0}}, "2026-10-14") == {}
    with pytest.raises(LookupError):
        s.real_target({}, state, "2026-10-15")                  # yesterday's signal is not reused


def test_explanations_are_in_vietnamese():
    s = BtcSmaEnsemble()
    closes = np.linspace(100, 300, 260)
    state: dict = {}
    s.decide(ctx_for(closes, state=state))
    line = s.explain(state)
    assert "giá trên 7/7 đường SMA" in line and "BTC lý tưởng 100% NAV" in line
    assert s.explain({}) == ""
    lines = s.explain_closes(BTC, pd.Series(closes))
    assert "mục tiêu BTC 100% NAV" in lines[0] and "SMA200 ✓" in lines[1]
    assert "chỉ giao dịch BTC" in s.explain_closes("ETHUSDT", closes)[0]
    assert "cần 201" in s.explain_closes(BTC, closes[:50])[0]


# ── the agents' engine view ──
def test_the_engine_view_reads_the_votes(monkeypatch):
    from cryptotrader.agents import quant_engine

    closes = np.r_[np.linspace(100, 400, 265), np.linspace(400, 350, 15)]
    frame = candles("2025-12-09", closes)
    day = f"{frame['Date'].iloc[-1]:%Y-%m-%d}"
    eth = candles("2025-12-09", np.linspace(50, 40, 280))
    monkeypatch.setattr(quant_engine, "_load", lambda symbols, end, warmup: {s: {BTC: frame, "ETHUSDT": eth}[s]
                                                                           for s in symbols})
    above = sum(1 for n in LOOKBACKS if closes[-1] > closes[-n:].mean())
    view = quant_engine.analyze("BTC", day)
    assert view.strategy == "btc_sma_ens" and view.action == "BUY" and view.in_basket
    assert view.target_weight == pytest.approx(above / 7)
    assert {f"SMA{n}" for n in LOOKBACKS} <= set(view.levels) and "ATR14" in view.levels
    assert any(f"{above}/7" in r for r in view.reasons) and any("không biết danh mục" in r for r in view.reasons)
    held = quant_engine.analyze("BTC", day, portfolio={"weights": {BTC: above / 7}})
    assert held.action == "HOLD" and any("giữ nguyên" in r for r in held.reasons)
    eth_view = quant_engine.analyze("ETH", day, portfolio={"weights": {}})
    assert eth_view.action == "NO_TRADE" and not eth_view.in_basket
    assert quant_engine.analyze("ETH", day, portfolio={"weights": {"ETHUSDT": 0.1}}).action == "SELL"


def test_the_engine_action_follows_the_band_like_the_daily_signal(monkeypatch):
    """/phantich must say what the daily report says: the band on the held weight."""
    from cryptotrader.agents import quant_engine

    closes = np.r_[np.linspace(100, 400, 265), np.linspace(400, 350, 15)]
    frame = candles("2025-12-09", closes)
    day = f"{frame['Date'].iloc[-1]:%Y-%m-%d}"
    monkeypatch.setattr(quant_engine, "_load", lambda symbols, end, warmup: dict.fromkeys(symbols, frame))
    ideal = sum(1 for n in LOOKBACKS if closes[-1] > closes[-n:].mean()) / 7
    assert 0 < ideal < 0.9
    over = quant_engine.analyze("BTC", day, portfolio={"weights": {BTC: ideal + 0.5}})
    assert over.action == "SELL" and over.target_weight == pytest.approx(ideal)
    near = quant_engine.analyze("BTC", day, portfolio={"weights": {BTC: ideal + 0.05}})
    assert near.action == "HOLD" and near.target_weight == pytest.approx(ideal + 0.05)
    none = quant_engine.analyze("BTC", day, portfolio={"weights": {}})
    assert none.action == "BUY" and none.target_weight == pytest.approx(ideal)
    small = quant_engine.analyze("BTC", day, portfolio={"weights": {BTC: max(ideal - 0.3, 0.01)}})
    assert small.action == "BUY"
