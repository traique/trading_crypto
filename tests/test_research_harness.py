"""The research harness: synthetic panels (no network) and, with -m integration, the real data set."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date

import numpy as np
import pandas as pd
import pytest

from cryptotrader.data.net import HttpResult
from cryptotrader.market import rules
from cryptotrader.strategies import BtcBuyHold
from research import cli, config, download, metrics as M
from research.backtest import TestPeriodLocked, run, run_price
from research.data import load_panel, make_panel, truncate
from research.strategies import BuyHold, CrossSectionalMomentum, EqualWeightMonthly
from research.universe import segment_bars, universe
from tests.helpers import csv_bytes, kline_rows, zip_bytes

FEE = rules.TAKER_FEE_PCT / 100
SLIP = rules.SLIPPAGE_PCT / 100
STEP = rules.DEFAULT_FILTERS.step_size
TICK = rules.DEFAULT_FILTERS.tick_size


# The real data set for the integration tests: CRYPTO_RESEARCH_DATA as set when the tests start.
REAL_DATA = os.environ.get(config.DATA_ENV)


@pytest.fixture(autouse=True)
def _research_dir(tmp_path, monkeypatch, request):
    """Unit tests get an empty research data dir; integration tests the real one."""
    if request.node.get_closest_marker("integration") is not None:
        if REAL_DATA:
            monkeypatch.setenv(config.DATA_ENV, REAL_DATA)
        else:
            monkeypatch.delenv(config.DATA_ENV, raising=False)
    else:
        monkeypatch.setenv(config.DATA_ENV, str(tmp_path / "research_data"))


def candles(first: str, closes, *, qv=1e6, opens=None) -> pd.DataFrame:
    """Daily candles in klines columns: open 1% under the close unless given, a wide range."""
    c = np.asarray(closes, dtype=float)
    o = c * 0.99 if opens is None else np.asarray(opens, dtype=float)
    q = np.full(len(c), float(qv)) if np.isscalar(qv) else np.asarray(qv, dtype=float)
    return pd.DataFrame({"Date": pd.date_range(first, periods=len(c), freq="D"), "Open": o,
                         "High": np.maximum(o, c) * 1.02, "Low": np.minimum(o, c) * 0.97, "Close": c,
                         "Volume": np.full(len(c), 100.0), "QuoteVolume": q, "Trades": np.full(len(c), 10.0)})


def walk(n: int, seed: int, start: float = 100.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(0, 0.03, n)))


# ── buy & hold: exact after costs ──
def test_buy_hold_is_exact_after_costs():
    closes = walk(60, 1, 20_000.0)
    opens = closes * 1.004
    panel = make_panel({"BTCUSDT": candles("2020-01-01", closes, opens=opens)})
    res = run(BuyHold("BTCUSDT"), "2020-01-01", "2020-02-29", capital=10_000, panel=panel)
    # The decision at day 0's close: weight 1 of 10,000 at the close, room for slippage and fee.
    order_qty = rules.floor_to_step(10_000 / (closes[0] * (1 + SLIP) * (1 + FEE)), STEP)
    price = min(rules.ceil_to_tick(opens[1] * (1 + SLIP), TICK), max(opens[1], closes[1]) * 1.02)
    qty = min(order_qty, rules.floor_to_step(10_000 / (price * (1 + FEE)), STEP))
    cash = 10_000 - qty * price - qty * price * FEE
    assert len(res.fills) == 1
    fill = res.fills.iloc[0]
    assert fill["date"] == "2020-01-02" and fill["qty"] == pytest.approx(qty, rel=1e-12)
    assert fill["price"] == pytest.approx(price, rel=1e-12)
    assert res.daily["nav"].iloc[0] == 10_000
    expected = cash + qty * closes
    np.testing.assert_allclose(res.daily["nav"].to_numpy()[1:], expected[1:], rtol=1e-12)
    assert res.metrics["final_nav"] == pytest.approx(expected[-1], abs=0.01)
    assert res.metrics["fees_paid"] == pytest.approx(qty * price * FEE, abs=0.01)
    # The benchmark is BTC's price from the first close, without costs.
    assert res.benchmark["total_return_pct"] == pytest.approx((closes[-1] / closes[0] - 1) * 100, abs=0.01)
    assert res.metrics["total_return_pct"] < res.benchmark["total_return_pct"]


def test_live_strategy_runs_like_the_bot():
    """cryptotrader's BtcBuyHold through StrategyContext gives the same book as the harness strategy."""
    closes = walk(40, 2, 30_000.0)
    panel = make_panel({"BTCUSDT": candles("2021-03-01", closes)})
    a = run(BuyHold("BTCUSDT"), "2021-03-01", "2021-04-09", panel=panel)
    b = run(BtcBuyHold(), "2021-03-01", "2021-04-09", panel=panel)
    np.testing.assert_allclose(a.daily["nav"], b.daily["nav"], rtol=1e-12)
    assert b.name == "btc_buy_hold"


def test_fee_and_slippage_options_change_the_cost():
    closes = np.full(30, 100.0)
    panel = make_panel({"BTCUSDT": candles("2020-01-01", closes, opens=closes)})
    base = run(BuyHold("BTCUSDT"), "2020-01-01", "2020-01-30", panel=panel)
    dear = run(BuyHold("BTCUSDT"), "2020-01-01", "2020-01-30", panel=panel, fee_pct=0.5, slippage_mult=3)
    assert dear.ledger.fee_pct == 0.5 and dear.ledger.slippage_pct == pytest.approx(3 * rules.SLIPPAGE_PCT)
    assert dear.metrics["final_nav"] < base.metrics["final_nav"] < 10_000


# ── no look-ahead ──
class Spy:
    name = "spy"

    def __init__(self):
        self.seen = []

    def on_day(self, ctx):
        for frame in (ctx.close, ctx.open, ctx.high, ctx.low, ctx.volume, ctx.quote_volume, ctx.trades):
            assert frame.index[-1] == ctx.date
        assert ctx.candles("BTCUSDT")["Date"].max() <= ctx.date
        assert ctx.history("close", ["BTCUSDT"], 5).index[-1] == ctx.date
        self.seen.append(ctx.date)
        return None


def test_context_never_shows_the_future():
    panel = make_panel({"BTCUSDT": candles("2020-01-01", walk(50, 3))})
    spy = Spy()
    run(spy, "2020-01-10", "2020-02-19", panel=panel)
    assert spy.seen[0] == pd.Timestamp("2020-01-10") and spy.seen[-1] == pd.Timestamp("2020-02-19")


class DailyMomentum:
    """Holds yesterday's best 30-day performer: uses every bit of data it is given."""

    name = "daily_mom"

    def on_day(self, ctx):
        syms = ctx.universe("liq_top10")
        px = ctx.history("close", syms, 31)
        if len(px) < 31 or not syms:
            return {}
        ret = (px.iloc[-1] / px.iloc[0] - 1).dropna()
        return {ret.idxmax(): 1.0} if len(ret) else {}


def test_changing_the_future_does_not_change_the_past():
    n, k = 400, 300
    base = {f"P{i}USDT": walk(n, 10 + i) for i in range(6)}
    base["BTCUSDT"] = walk(n, 99, 10_000.0)
    future = {s: v.copy() for s, v in base.items()}
    for s in future:
        future[s][k + 1:] *= np.linspace(1, 5 if s == "P0USDT" else 0.2, n - k - 1)
    pa = make_panel({s: candles("2019-01-01", v) for s, v in base.items()})
    pb = make_panel({s: candles("2019-01-01", v) for s, v in future.items()})
    a = run(DailyMomentum(), "2019-07-01", str(pa.dates[-1].date()), panel=pa)
    b = run(DailyMomentum(), "2019-07-01", str(pb.dates[-1].date()), panel=pb)
    cut = pa.dates[k]
    np.testing.assert_allclose(a.daily["nav"].loc[:cut], b.daily["nav"].loc[:cut], rtol=1e-12)
    assert not np.allclose(a.daily["nav"].iloc[-1], b.daily["nav"].iloc[-1])
    for day in pa.dates[200: k + 2]:
        assert universe(day, "liq_top10", pa) == universe(day, "liq_top10", pb)


def test_orders_fill_on_the_next_day_only():
    closes = walk(20, 4)
    panel = make_panel({"BTCUSDT": candles("2020-01-01", closes)})
    res = run(BuyHold("BTCUSDT"), "2020-01-05", "2020-01-20", panel=panel)
    assert res.fills.iloc[0]["date"] == "2020-01-06"


# ── the TEST lock ──
def test_run_refuses_the_test_period():
    panel = make_panel({"BTCUSDT": candles("2023-11-01", walk(120, 5))})
    with pytest.raises(TestPeriodLocked):
        run(BuyHold("BTCUSDT"), "2023-11-01", "2024-01-15", panel=panel)
    with pytest.raises(TestPeriodLocked):            # a panel holding TEST days, even for a VALID run
        run(BuyHold("BTCUSDT"), "2023-11-01", "2023-12-31", panel=panel)
    with pytest.raises(TestPeriodLocked):
        run_price("BTCUSDT", "2023-11-01", "2024-01-15", panel=panel)
    ok = run(BuyHold("BTCUSDT"), "2023-11-01", "2024-01-15", panel=panel, unlock_test=True)
    assert ok.end == "2024-01-15"
    locked = truncate(panel, config.LOCK_AFTER)
    assert locked.dates[-1] == pd.Timestamp(config.LOCK_AFTER)
    assert run(BuyHold("BTCUSDT"), "2023-11-01", "2023-12-31", panel=locked).end == "2023-12-31"


def _write_klines(symbol: str, frame: pd.DataFrame, meta: dict | None = None) -> None:
    kdir = config.klines_dir()
    kdir.mkdir(parents=True, exist_ok=True)
    out = frame.copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    out.to_csv(kdir / f"{symbol}.csv", index=False)
    if meta is not None:
        config.meta_file().write_text(json.dumps(meta))


def test_load_panel_is_locked_unless_unlocked():
    _write_klines("BTCUSDT", candles("2023-12-01", walk(62, 6)), {"data_end": "2024-01-31"})
    locked = load_panel()
    assert locked.dates[-1] == pd.Timestamp("2023-12-31")
    assert locked.close.index.max() == pd.Timestamp("2023-12-31")
    full = load_panel(unlock_test=True)
    assert full.dates[-1] == pd.Timestamp("2024-01-31")
    assert config.panel_cache().exists()
    assert load_panel(rebuild=True).dates[-1] == pd.Timestamp("2023-12-31")


def test_cli_refuses_test_and_reports_missing_data(capsys):
    assert cli.main(["run", "btc_buy_hold", "--split", "TEST"]) == 2
    assert cli.main(["run", "btc_buy_hold", "--split", "ALL"]) == 2
    assert cli.main(["run", "btc_buy_hold", "--start", "2023-01-01", "--end", "2024-02-01"]) == 2
    assert cli.main(["run", "btc_buy_hold", "--split", "VALID"]) == 1      # no data downloaded
    assert cli.main(["baselines", "--out", str(config.data_dir() / "out")]) == 1
    assert "download" in capsys.readouterr().err


def test_cli_runs_a_strategy(capsys, tmp_path):
    _write_klines("BTCUSDT", candles("2021-06-01", walk(300, 7, 30_000.0)), {"data_end": "2022-03-27"})
    _write_klines("ETHUSDT", candles("2021-06-01", walk(300, 8, 2_000.0)))
    assert cli.main(["run", "buyhold:ETHUSDT", "--start", "2022-01-01", "--end", "2022-03-01",
                     "--out", str(tmp_path / "o")]) == 0
    text = capsys.readouterr().out
    assert "buyhold_ETHUSDT" in text and "BTC buy&hold" in text
    assert json.loads((tmp_path / "o" / "summary.json").read_text())["metrics"]["buys"] >= 1
    assert cli.main(["run", "buyhold:NOPEUSDT", "--split", "VALID"]) == 1
    assert cli.main(["run", "price:BTCUSDT", "--start", "2022-01-01", "--end", "2022-03-01"]) == 0
    assert cli.main(["list"]) == 0


# ── delisted pairs ──
def test_a_pair_that_stops_trading_is_liquidated_at_its_last_close():
    closes = walk(30, 9, 5.0)
    panel = make_panel({"BTCUSDT": candles("2020-01-01", walk(90, 10, 9_000.0)),
                        "DEADUSDT": candles("2020-01-01", closes)})
    res = run(BuyHold("DEADUSDT"), "2020-01-01", "2020-03-30", panel=panel)
    buy, sell = res.fills.iloc[0], res.fills.iloc[-1]
    assert sell["side"] == "SELL" and sell["symbol"] == "DEADUSDT"
    last_day = pd.Timestamp("2020-01-30")
    assert sell["date"] == str((last_day + pd.Timedelta(days=config.DELIST_GAP_DAYS + 1)).date())
    assert sell["price"] == pytest.approx(rules.floor_to_tick(closes[-1] * (1 - SLIP), TICK), rel=1e-12)
    assert sell["qty"] == pytest.approx(buy["qty"])
    assert "DEADUSDT" not in res.ledger.positions
    expected_cash = 10_000 - buy["qty"] * buy["price"] * (1 + FEE) + sell["qty"] * sell["price"] * (1 - FEE)
    assert res.ledger.cash == pytest.approx(expected_cash, rel=1e-12)
    assert res.daily["nav"].iloc[-1] == pytest.approx(expected_cash, rel=1e-12)
    assert any("DEADUSDT" in w and "liquidated" in w for w in res.warnings)
    assert res.options["liquidated"] == ["DEADUSDT"]


def test_a_short_halt_is_not_a_delisting():
    closes = walk(60, 11, 5.0)
    frame = candles("2020-01-01", closes)
    frame = frame.drop(index=range(20, 25)).reset_index(drop=True)      # a 5-day halt
    panel = make_panel({"BTCUSDT": candles("2020-01-01", walk(60, 12, 9_000.0)), "HALTUSDT": frame})
    res = run(BuyHold("HALTUSDT"), "2020-01-01", "2020-02-29", panel=panel)
    assert (res.fills["side"] == "SELL").sum() == 0 and not res.warnings
    assert "HALTUSDT" in res.ledger.positions


def test_zero_volume_candles_are_days_without_trading():
    frame = candles("2020-01-01", walk(40, 13))
    frame.loc[30:, "Volume"] = 0.0
    panel = make_panel({"BTCUSDT": frame})
    assert panel.close["BTCUSDT"].last_valid_index() == pd.Timestamp("2020-01-30")


# ── liquidity cap ──
def test_liquidity_cap_limits_buys_to_a_share_of_volume():
    closes = np.full(40, 2.0)
    panel = make_panel({"BTCUSDT": candles("2020-01-01", walk(40, 14, 9_000.0)),
                        "THINUSDT": candles("2020-01-01", closes, qv=2_000.0, opens=closes)})
    res = run(BuyHold("THINUSDT"), "2020-01-31", "2020-02-09", panel=panel, max_adv_pct=0.05)
    first = res.fills.iloc[0]
    assert first["qty"] == pytest.approx(rules.floor_to_step(0.05 * 2_000.0 / 2.0, STEP))
    assert (res.fills["qty"] * res.fills["price"] <= 0.05 * 2_000.0 * (1 + SLIP) + 1e-9).all()
    uncapped = run(BuyHold("THINUSDT"), "2020-01-31", "2020-02-09", panel=panel)
    assert uncapped.fills.iloc[0]["qty"] > 40 * first["qty"]


def test_liquidity_cap_drops_orders_below_the_minimum():
    closes = np.full(40, 2.0)
    panel = make_panel({"BTCUSDT": candles("2020-01-01", walk(40, 15, 9_000.0)),
                        "DRYUSDT": candles("2020-01-01", closes, qv=50.0, opens=closes)})
    res = run(BuyHold("DRYUSDT"), "2020-01-31", "2020-02-09", panel=panel, max_adv_pct=0.05)
    assert res.fills.empty                                  # 2.5 USDT < minNotional 10


# ── universe ──
def test_universe_rules():
    n = 400
    frames = {
        "BTCUSDT": candles("2019-01-01", walk(n, 20, 9_000.0), qv=5e8),
        "ETHUSDT": candles("2019-01-01", walk(n, 21), qv=3e8),
        "USDCUSDT": candles("2019-01-01", np.ones(n), qv=9e9),            # stablecoin: excluded
        "BTCUPUSDT": candles("2019-01-01", walk(n, 22), qv=8e9),          # leveraged token: excluded
        "NEWUSDT": candles("2019-07-01", walk(n - 181, 23), qv=7e9),      # young
        "OLDUSDT": candles("2019-01-01", walk(250, 24), qv=6e9),          # stops on day 249
    }
    panel = make_panel(frames)
    first_new = pd.Timestamp("2019-07-01")
    eligible_from = first_new + pd.Timedelta(days=180)     # 180 candles before that day
    assert "NEWUSDT" not in universe(eligible_from - pd.Timedelta(days=1), "liq_top10", panel)
    u = universe(eligible_from, "liq_top10", panel)
    assert u[0] == "NEWUSDT" and "USDCUSDT" not in u and "BTCUPUSDT" not in u
    last_old = pd.Timestamp("2019-01-01") + pd.Timedelta(days=249)
    assert "OLDUSDT" in universe(last_old + pd.Timedelta(days=3), "liq_top10", panel)
    assert "OLDUSDT" not in universe(last_old + pd.Timedelta(days=4), "liq_top10", panel)
    assert universe("2019-08-01", "liq_top10", panel) == ["OLDUSDT", "BTCUSDT", "ETHUSDT"]
    assert universe("2019-08-01", "liq_top2", panel) == ["OLDUSDT", "BTCUSDT"]
    assert universe("2018-01-01", "liq_top10", panel) == []
    with pytest.raises(ValueError):
        universe("2019-08-01", "top10", panel)


def test_relisted_pair_starts_its_history_again():
    has = np.zeros((40, 1), dtype=bool)
    has[0:10, 0] = True
    has[12:15, 0] = True          # a 2-day hole: same listing
    has[30:35, 0] = True          # a 15-day hole: listed again
    counts = segment_bars(has)[:, 0]
    assert counts[9] == 10 and counts[14] == 13 and counts[29] == 13
    assert counts[30] == 1 and counts[34] == 5


def test_universe_ignores_the_day_itself():
    """The universe for day D is built from candles before D: a volume change on D does not count."""
    n = 300
    a = {"BTCUSDT": candles("2019-01-01", walk(n, 30, 9_000.0), qv=5e8),
         "AAAUSDT": candles("2019-01-01", walk(n, 31), qv=4e8)}
    b = {k: v.copy() for k, v in a.items()}
    day = pd.Timestamp("2019-01-01") + pd.Timedelta(days=250)
    b["AAAUSDT"].loc[b["AAAUSDT"]["Date"] >= day, "QuoteVolume"] = 9e9
    pa, pb = make_panel(a), make_panel(b)
    assert universe(day, "liq_top1", pa) == universe(day, "liq_top1", pb) == ["BTCUSDT"]
    assert universe(day + pd.Timedelta(days=30), "liq_top1", pb) == ["AAAUSDT"]


def test_baseline_strategies_rebalance_monthly():
    n = 500
    frames = {f"P{i}USDT": candles("2019-01-01", walk(n, 40 + i), qv=1e8 * (i + 1)) for i in range(8)}
    frames["BTCUSDT"] = candles("2019-01-01", walk(n, 50, 9_000.0), qv=1e10)
    panel = make_panel(frames)
    ew = run(EqualWeightMonthly(universe="liq_top5"), "2019-08-01", "2019-12-31", panel=panel)
    buy_days = sorted(set(ew.fills["date"]))
    assert buy_days[0] == "2019-08-02"
    assert all(d.endswith("-01") for d in buy_days[1:])
    assert len(set(ew.fills[ew.fills["date"] == "2019-08-02"]["symbol"])) == 5
    mom = run(CrossSectionalMomentum(top=3, lookback=60, universe="liq_top5"), "2019-08-01", "2019-12-31",
              panel=panel)
    assert mom.fills.groupby("date")["symbol"].nunique().max() <= 6
    held = mom.ledger.positions
    assert 1 <= len(held) <= 3


# ── metrics ──
def test_metrics_basics():
    idx = pd.date_range("2020-01-01", periods=366, freq="D")
    nav = pd.Series(np.linspace(100, 200, 366), index=idx)
    m = M.compute(nav, 100.0)
    assert m["total_return_pct"] == 100.0 and m["max_drawdown_pct"] == 0.0
    assert m["cagr_pct"] == pytest.approx((2 ** (365.25 / 366) - 1) * 100, abs=0.01)
    assert m["sharpe"] > 0 and m["sortino"] == 0.0           # no down day: no downside deviation
    dd = pd.Series([100.0, 120.0, 60.0, 90.0], index=idx[:4])
    assert M.max_drawdown(dd, 100.0) == pytest.approx(-50.0)
    flat = M.compute(pd.Series(100.0, index=idx[:10]), 100.0)
    assert flat["sharpe"] == 0.0 and flat["vol_ann_pct"] == 0.0
    y = M.yearly(nav, 100.0, None, nav)
    assert list(y.index) == [2020] and y.loc[2020, "excess_pct"] == 0.0


# ── the downloader (archive faked; no network) ──
SYM = "AAAUSDT"
MPRE = f"data/spot/monthly/klines/{SYM}/1d/"
DPRE = f"data/spot/daily/klines/{SYM}/1d/"


class FakeArchive:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.bad_etag: set[str] = set()
        self.downloads: list[str] = []

    def add_month(self, month: date, days: int, price: float = 100.0):
        rows = kline_rows(month, days, price)
        self.files[f"{MPRE}{SYM}-1d-{month:%Y-%m}.zip"] = zip_bytes("m.csv", csv_bytes(rows))

    def add_day(self, day: date, price: float):
        self.files[f"{DPRE}{SYM}-1d-{day:%Y-%m-%d}.zip"] = zip_bytes("d.csv", csv_bytes(kline_rows(day, 1, price)))

    def listing(self, prefix: str, delimiter: bool) -> bytes:
        keys = sorted(k for k in self.files if k.startswith(prefix))
        if delimiter:
            subs = sorted({prefix + k[len(prefix):].split("/", 1)[0] + "/" for k in keys if "/" in k[len(prefix):]})
            body = "".join(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in subs)
        else:
            body = ""
            for k in keys:
                etag = "0" * 32 if k in self.bad_etag else hashlib.md5(self.files[k]).hexdigest()
                body += f"<Contents><Key>{k}</Key><ETag>&quot;{etag}&quot;</ETag></Contents>"
        return (f'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<IsTruncated>false</IsTruncated>{body}</ListBucketResult>").encode()

    def get(self, url, params=None, allow_status=()):
        if params is not None:
            return HttpResult(200, self.listing(params["prefix"], "delimiter" in params), url)
        key = url.split("data.binance.vision/", 1)[1]
        if key in self.files:
            self.downloads.append(key)
            return HttpResult(200, self.files[key], url)
        return HttpResult(404, b"", url)


@pytest.fixture
def fake(monkeypatch):
    arch = FakeArchive()
    monkeypatch.setattr(download, "_get", arch.get)
    return arch


def _saved() -> pd.DataFrame:
    return pd.read_csv(config.klines_dir() / f"{SYM}.csv", parse_dates=["Date"])


def test_download_is_incremental_and_verified(fake):
    config.klines_dir().mkdir(parents=True)
    fake.add_month(date(2026, 7, 1), 31)
    fake.add_month(date(2026, 8, 1), 31)
    for d in range(1, 4):
        fake.add_day(date(2026, 9, d), 500.0 + d)
    info = download.update_symbol(SYM, date(2026, 9, 3))
    assert info["ok"] and info["fetched"] == 5 and info["bars"] == 65
    assert info["first_day"] == "2026-07-01" and info["last_day"] == "2026-09-03" and info["active"]
    assert len(_saved()) == 65
    # Nothing new: nothing downloaded.
    fake.downloads.clear()
    assert download.update_symbol(SYM, date(2026, 9, 3))["fetched"] == 0 and not fake.downloads
    # September's monthly file appears: it replaces the daily rows, whose manifest entries go.
    fake.add_month(date(2026, 9, 1), 30, price=900.0)
    fake.add_day(date(2026, 10, 1), 1000.0)
    info = download.update_symbol(SYM, date(2026, 10, 1))
    assert info["fetched"] == 2 and info["bars"] == 31 + 31 + 30 + 1
    saved = _saved().set_index("Date")
    assert saved.loc["2026-09-01", "Close"] == 900.0 and saved.loc["2026-10-01", "Close"] == 1000.0
    manifest = json.loads((config.klines_dir() / f"{SYM}.json").read_text())["files"]
    assert not any(n.startswith(f"{SYM}-1d-2026-09-") for n in manifest)
    assert f"{SYM}-1d-2026-09.zip" in manifest and f"{SYM}-1d-2026-10-01.zip" in manifest


def test_download_refuses_a_file_that_fails_its_checksum(fake):
    config.klines_dir().mkdir(parents=True)
    fake.add_month(date(2026, 7, 1), 31)
    fake.bad_etag.add(f"{MPRE}{SYM}-1d-2026-07.zip")
    info = download.update_symbol(SYM, date(2026, 7, 31))
    assert not info["ok"] and "MD5" in info["error"]
    assert not (config.klines_dir() / f"{SYM}.csv").exists()


def test_download_delisted_pair_is_not_searched_daily(fake):
    config.klines_dir().mkdir(parents=True)
    fake.add_month(date(2022, 1, 1), 20)
    info = download.update_symbol(SYM, date(2026, 10, 1))
    assert info["ok"] and info["last_day"] == "2022-01-20" and not info["active"]
    assert info["segments"] == 1


def test_list_bucket_follows_pages(monkeypatch):
    pages = [
        b'<ListBucketResult><IsTruncated>true</IsTruncated><NextMarker>m1</NextMarker>'
        b'<CommonPrefixes><Prefix>data/spot/monthly/klines/AUSDT/</Prefix></CommonPrefixes></ListBucketResult>',
        b'<ListBucketResult><IsTruncated>false</IsTruncated>'
        b'<CommonPrefixes><Prefix>data/spot/monthly/klines/BBTC/</Prefix></CommonPrefixes>'
        b'<CommonPrefixes><Prefix>data/spot/monthly/klines/CUSDT/</Prefix></CommonPrefixes></ListBucketResult>',
    ]
    seen = []

    def get(url, params=None, allow_status=()):
        seen.append(dict(params))
        return HttpResult(200, pages[len(seen) - 1], url)

    monkeypatch.setattr(download, "_get", get)
    _, prefixes = download.list_bucket("data/spot/monthly/klines/", delimiter=True)
    assert len(prefixes) == 3 and seen[1]["marker"] == "m1"
    seen.clear()
    assert download._pairs_under("data/spot/monthly/klines/") == {"AUSDT", "CUSDT"}


def test_download_main_records_meta_and_moves_data_end_only_on_full_runs(fake, monkeypatch):
    fake.add_month(date(2026, 7, 1), 31)
    monkeypatch.setattr(download.rules, "final_day", lambda now=None: date(2026, 7, 31))
    monkeypatch.setattr(download, "list_symbols", lambda: [SYM, "ZZZUSDT"])
    monkeypatch.setattr(download, "fetch_statuses", lambda: {})
    assert download.main(["--workers", "2"]) == 0
    meta = json.loads(config.meta_file().read_text())
    assert meta["data_end"] == "2026-07-31" and meta["failed"] == ["ZZZUSDT"]
    assert meta["symbols"][SYM]["last_day"] == "2026-07-31" and meta["coverage"]["pairs"] == 1
    assert "ZZZUSDT failed" in config.log_file().read_text()
    fake.add_month(date(2026, 8, 1), 31)
    monkeypatch.setattr(download.rules, "final_day", lambda now=None: date(2026, 8, 31))
    assert download.main(["--symbols", SYM]) == 0                       # partial: data end stays
    meta = json.loads(config.meta_file().read_text())
    assert meta["data_end"] == "2026-07-31" and meta["symbols"][SYM]["last_day"] == "2026-08-31"
    assert "ZZZUSDT" not in meta["symbols"]                             # never had a file: forgotten


def test_summarize_counts_segments():
    frame = pd.DataFrame({"Date": pd.to_datetime(["2020-01-01", "2020-01-02", "2020-02-01", "2020-02-02"]),
                          "Volume": [1.0, 1.0, 1.0, 0.0]})
    s = download.summarize(frame, date(2020, 2, 10))
    assert s["segments"] == 2 and s["bars"] == 3 and s["zero_volume_days"] == 1
    assert s["last_day"] == "2020-02-01" and not s["active"]


# ── the real data set (python -m pytest -m integration) ──
def _real_panel():
    if not config.data_ready():
        pytest.skip(f"no research data in {config.data_dir()} (run `python -m research download`)")
    return load_panel()


@pytest.mark.integration
def test_real_btc_buy_hold_matches_the_price():
    panel = _real_panel()
    res = run(BtcBuyHold(), "2020-01-01", "2020-12-31", panel=panel)
    price = run_price("BTCUSDT", "2020-01-01", "2020-12-31", panel=panel)
    gap = price.metrics["final_nav"] / res.metrics["final_nav"] - 1
    assert 0 < gap < 0.01                    # entry slippage + fee, and the next-day open
    assert len(res.fills) == 1


@pytest.mark.integration
def test_real_panel_is_locked_and_keeps_delisted_pairs():
    panel = _real_panel()
    assert panel.dates[-1] == pd.Timestamp(config.LOCK_AFTER)
    spans = panel.spans()
    assert spans.loc["BTCUSDT", "first_day"] == pd.Timestamp("2017-08-17")
    stopped = spans[spans["last_day"] < pd.Timestamp("2023-12-01")]
    assert len(stopped) >= 100
    assert spans.loc["SRMUSDT", "last_day"] < pd.Timestamp("2022-12-31")
    assert spans.loc["LUNAUSDT", "segments"] >= 2


@pytest.mark.integration
def test_real_universe_is_point_in_time():
    panel = _real_panel()
    u = universe("2021-01-01", "liq_top10", panel)
    assert "BTCUSDT" in u and "ETHUSDT" in u and len(u) == 10
    assert all(rules.is_tradable_pair(s) for s in universe("2023-06-01", "liq_top50", panel))
    assert "LUNAUSDT" in universe("2022-05-01", "liq_top20", panel)
    assert "LUNAUSDT" not in universe("2022-06-15", "liq_top50", panel)


@pytest.mark.integration
def test_real_archive_download_of_a_delisted_pair(tmp_path, monkeypatch):
    monkeypatch.setenv(config.DATA_ENV, str(tmp_path / "rd"))
    config.klines_dir().mkdir(parents=True)
    info = download.update_symbol("SRMUSDT", rules.final_day())
    assert info["ok"] and not info["active"] and info["first_day"].startswith("2020-08")
    assert info["last_day"] < "2023-01-01" and info["bars"] > 500
    assert float(pd.read_csv(config.klines_dir() / "SRMUSDT.csv")["Close"].iloc[-1]) > 0


def test_download_data_end_waits_for_a_failed_live_pair(monkeypatch):
    """A full run in which a pair still trading at the data end fails must not move the
    end: that pair's CSV would stop early and look delisted on the new days."""
    state = {"end": date(2026, 7, 31), "fail": set()}

    def update(symbol, end, refresh=False, sha256=False):
        if symbol in state["fail"]:
            return {"symbol": symbol, "ok": False, "error": "HTTP 503"}
        return {"symbol": symbol, "ok": True, "first_day": "2026-01-01", "last_day": end.isoformat(),
                "active": True, "bars": 100, "segments": 1}

    monkeypatch.setattr(download, "update_symbol", update)
    monkeypatch.setattr(download.rules, "final_day", lambda now=None: state["end"])
    monkeypatch.setattr(download, "list_symbols", lambda: ["BTCUSDT", "ETHUSDT"])
    monkeypatch.setattr(download, "fetch_statuses", lambda: {})
    assert download.main(["--workers", "1"]) == 0
    assert json.loads(config.meta_file().read_text())["data_end"] == "2026-07-31"
    state["end"], state["fail"] = date(2026, 8, 31), {"ETHUSDT"}
    assert download.main(["--workers", "1"]) == 0
    meta = json.loads(config.meta_file().read_text())
    assert meta["data_end"] == "2026-07-31" and meta["data_end_blocked_by"] == ["ETHUSDT"]
    state["fail"] = set()
    assert download.main(["--workers", "1"]) == 0
    meta = json.loads(config.meta_file().read_text())
    assert meta["data_end"] == "2026-08-31" and "data_end_blocked_by" not in meta
