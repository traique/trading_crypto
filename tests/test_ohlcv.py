import os
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from cryptotrader.data import binance, klines as kl, ohlcv
from cryptotrader.data.errors import NoMarketDataError, UnknownSymbolError, VendorUnavailableError
from tests.helpers import kline_rows

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)          # last final candle: 2026-10-14


class Source:
    """A fake source over a fixed history; records the windows asked for."""

    def __init__(self, first=date(2026, 1, 1), last=date(2026, 10, 15), error=None, price=100.0):
        self.frame = kl.binance_rows_to_frame(kline_rows(first, (last - first).days + 1, price))
        self.calls: list[tuple] = []
        self.error = error

    def __call__(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if self.error:
            raise self.error
        f = self.frame
        if start is not None:
            f = f[f["Date"] >= pd.Timestamp(start)]
        return f[f["Date"] <= pd.Timestamp(end)].reset_index(drop=True)


@pytest.fixture
def sources(monkeypatch):
    srcs = {"binance": Source(), "archive": Source(error=AssertionError("archive not expected")),
            "okx": Source(error=AssertionError("okx not expected"))}
    monkeypatch.setattr(ohlcv, "FETCHERS", srcs)
    return srcs


def test_first_load_downloads_drops_forming_candle_and_caches(sources):
    f = ohlcv.load_ohlcv("btc", now=NOW)
    assert list(f.columns) == kl.COLUMNS
    assert f["Date"].iloc[-1] == pd.Timestamp("2026-10-14")         # 15 Oct is still forming
    assert f.attrs["symbol"] == "BTCUSDT" and f.attrs["source"] == "binance"
    csv, meta = ohlcv.cache_paths("BTCUSDT")
    assert csv.exists() and meta.exists()
    cached = pd.read_csv(csv)
    assert cached["Date"].iloc[-1] == "2026-10-14"                    # forming candle never cached
    # Same day again: served from the cache.
    ohlcv.load_ohlcv("BTC/USDT", now=NOW)
    assert len(sources["binance"].calls) == 1


def test_end_date_and_start(sources):
    f = ohlcv.load_ohlcv("BTCUSDT", "2026-03-31", start="2026-03-01", now=NOW)
    assert f["Date"].iloc[0] == pd.Timestamp("2026-03-01") and f["Date"].iloc[-1] == pd.Timestamp("2026-03-31")
    # A later end date is clamped to the last final candle.
    f = ohlcv.load_ohlcv("BTCUSDT", "2030-01-01", now=NOW)
    assert f["Date"].iloc[-1] == pd.Timestamp("2026-10-14")


def test_incremental_update_next_day(sources):
    ohlcv.load_ohlcv("BTCUSDT", now=NOW)
    later = NOW + timedelta(days=1)
    sources["binance"].frame = kl.binance_rows_to_frame(kline_rows(date(2026, 1, 1), 289))
    f = ohlcv.load_ohlcv("BTCUSDT", now=later)
    assert f["Date"].iloc[-1] == pd.Timestamp("2026-10-15")
    assert sources["binance"].calls[-1][1] == date(2026, 10, 14)     # only from the last cached day


def test_past_end_date_needs_no_download(sources):
    ohlcv.load_ohlcv("BTCUSDT", now=NOW)
    ohlcv.load_ohlcv("BTCUSDT", "2026-05-01", now=NOW + timedelta(days=3))
    assert len(sources["binance"].calls) == 1


def test_stale_history_is_refused_unless_allowed(sources):
    sources["binance"].frame = sources["binance"].frame[sources["binance"].frame["Date"] <= "2026-10-01"]
    with pytest.raises(NoMarketDataError, match="2026-10-01"):
        ohlcv.load_ohlcv("BTCUSDT", now=NOW)
    f = ohlcv.load_ohlcv("BTCUSDT", now=NOW, allow_stale=True)
    assert f["Date"].iloc[-1] == pd.Timestamp("2026-10-01")


def test_delisted_pair_comes_from_the_archive(sources):
    sources["binance"].error = UnknownSymbolError("LUNAUSDT")
    sources["archive"] = Source(first=date(2020, 8, 1), last=date(2022, 5, 12), price=5.0)
    f = ohlcv.load_ohlcv("LUNA", "2022-05-12", now=NOW)
    assert f["Date"].iloc[-1] == pd.Timestamp("2022-05-12") and f.attrs["source"] == "binance_archive"
    with pytest.raises(NoMarketDataError):                           # today: stale (delisted)
        ohlcv.load_ohlcv("LUNA", now=NOW)
    assert len(sources["archive"].calls) == 1                         # already checked through today
    ohlcv.load_ohlcv("LUNA", now=NOW + timedelta(days=1), allow_stale=True)
    assert len(sources["archive"].calls) == 1                         # archive not asked again within a week
    ohlcv.load_ohlcv("LUNA", now=NOW + timedelta(days=9), allow_stale=True)
    assert len(sources["archive"].calls) == 2


def test_okx_fills_the_tail_but_is_never_cached(sources):
    ohlcv.load_ohlcv("BTCUSDT", "2026-10-10", now=datetime(2026, 10, 11, 1, tzinfo=UTC))
    csv, _ = ohlcv.cache_paths("BTCUSDT")
    before = csv.read_bytes()
    sources["binance"].error = VendorUnavailableError("down")
    sources["okx"] = Source(price=100.0)
    f = ohlcv.load_ohlcv("BTCUSDT", now=NOW)
    assert f["Date"].iloc[-1] == pd.Timestamp("2026-10-14") and f.attrs["source"] == "binance+okx"
    assert sources["okx"].calls[-1][1] == date(2026, 10, 11)
    assert csv.read_bytes() == before


def test_every_source_down(sources):
    sources["binance"].error = VendorUnavailableError("down")
    sources["okx"].error = VendorUnavailableError("down too")
    with pytest.raises(VendorUnavailableError):
        ohlcv.load_ohlcv("BTCUSDT", now=NOW)
    # With a recent cache, the cache is served.
    sources["binance"].error = None
    ohlcv.load_ohlcv("BTCUSDT", now=NOW - timedelta(days=1))
    sources["binance"].error = VendorUnavailableError("down")
    f = ohlcv.load_ohlcv("BTCUSDT", now=NOW)
    assert f["Date"].iloc[-1] == pd.Timestamp("2026-10-13") and f.attrs["source"] == "cache"


def test_relisted_pair_keeps_only_the_current_listing(sources):
    old = kl.binance_rows_to_frame(kline_rows(date(2021, 1, 1), 100, 80.0))
    sources["binance"].frame = kl.merge(old, sources["binance"].frame)
    f = ohlcv.load_ohlcv("BTCUSDT", now=NOW)
    assert f["Date"].iloc[0] == pd.Timestamp("2026-01-01")
    # Point in time: on a day before the relisting, the old listing is what existed.
    f = ohlcv.load_ohlcv("BTCUSDT", "2021-03-01", now=NOW, allow_stale=True)
    assert f["Date"].iloc[0] == pd.Timestamp("2021-01-01")


def test_damaged_cache_is_downloaded_again(sources):
    ohlcv.load_ohlcv("BTCUSDT", now=NOW)
    csv, meta = ohlcv.cache_paths("BTCUSDT")
    csv.write_text("garbage\x00,\n,,")
    meta.write_text("{not json")
    f = ohlcv.load_ohlcv("BTCUSDT", now=NOW)
    assert f["Date"].iloc[-1] == pd.Timestamp("2026-10-14")
    assert len(sources["binance"].calls) == 2


def test_cache_write_is_atomic(sources, monkeypatch):
    ohlcv.load_ohlcv("BTCUSDT", "2026-10-10", now=datetime(2026, 10, 11, 1, tzinfo=UTC))
    csv, _ = ohlcv.cache_paths("BTCUSDT")
    before = csv.read_bytes()
    real_to_csv = pd.DataFrame.to_csv

    def half_write(self, path, *args, **kwargs):
        with open(path, "w") as handle:
            handle.write("Date,Open\n2026-")
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_csv", half_write)
    with pytest.raises(OSError):
        ohlcv.load_ohlcv("BTCUSDT", now=NOW)
    monkeypatch.setattr(pd.DataFrame, "to_csv", real_to_csv)
    assert csv.read_bytes() == before
    assert not [p for p in os.listdir(csv.parent) if p.endswith(".tmp")]


def test_breaker_stops_asking_a_dead_source(monkeypatch):
    calls = []

    def dead(url, params=None, **kw):
        calls.append(url)
        raise VendorUnavailableError("timeout")

    monkeypatch.setattr(binance, "http_get", dead)
    okx_src = Source(error=VendorUnavailableError("okx down"))
    monkeypatch.setattr(ohlcv, "FETCHERS", {"binance": binance.fetch_klines, "archive": Source(), "okx": okx_src})
    for _ in range(4):
        with pytest.raises(VendorUnavailableError):
            ohlcv.load_ohlcv("ETHUSDT", now=NOW)
    assert len(calls) == 2                       # breaker opened after two failures


def test_load_many_skips_failures(sources):
    out = ohlcv.load_many(["BTC", "bad/pair/x", "ETH"], now=NOW)
    assert set(out) == {"BTCUSDT", "ETHUSDT"}


def test_flat_zero_volume_candles_do_not_make_a_dead_pair_current(sources):
    f = sources["binance"].frame
    f.loc[f["Date"] > "2026-09-30", "Volume"] = 0.0
    with pytest.raises(NoMarketDataError, match="2026-09-30"):
        ohlcv.load_ohlcv("BTCUSDT", now=NOW)
    _, meta = ohlcv.read_cache("BTCUSDT")
    assert meta["status"] == "inactive"


def test_lagging_mirror_is_asked_again_on_the_next_call(sources):
    """At 00:10 UTC the mirror may still end two days back: the cache must not be
    marked complete through a day the source did not return, so a later retry
    picks up the late candle."""
    now = datetime(2026, 10, 15, 0, 10, tzinfo=UTC)                  # last final candle: 10-14
    full = sources["binance"].frame
    sources["binance"].frame = full[full["Date"] <= "2026-10-13"].reset_index(drop=True)
    f = ohlcv.load_ohlcv("BTCUSDT", now=now)
    assert f["Date"].iloc[-1] == pd.Timestamp("2026-10-13")
    _, meta_path = ohlcv.cache_paths("BTCUSDT")
    import json
    assert json.loads(meta_path.read_text())["checked_through"] == "2026-10-13"
    sources["binance"].frame = full                                   # the mirror catches up
    f = ohlcv.load_ohlcv("BTCUSDT", now=now + timedelta(minutes=10))
    assert len(sources["binance"].calls) == 2
    assert f["Date"].iloc[-1] == pd.Timestamp("2026-10-14")
    ohlcv.load_ohlcv("BTCUSDT", now=now + timedelta(minutes=20))
    assert len(sources["binance"].calls) == 2                         # now complete: cache served
