from datetime import UTC, date, datetime

import pandas as pd
import pytest

from cryptotrader.data import archive, klines as kl, okx
from tests.helpers import csv_bytes, kline_row, kline_rows, zip_bytes


def test_rest_rows_to_frame():
    f = kl.binance_rows_to_frame(kline_rows(date(2026, 1, 1), 3))
    assert list(f.columns) == kl.COLUMNS
    assert list(f["Date"]) == [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-03")]
    assert f["Close"].tolist() == [100.0, 101.0, 102.0]
    assert f["QuoteVolume"].iloc[0] == pytest.approx(1050.0)
    assert f["Trades"].iloc[0] == 1234
    assert str(f["Date"].dtype) == "datetime64[ns]"
    assert f["Date"].dt.tz is None


def test_microsecond_and_millisecond_timestamps_mix():
    rows = [kline_row(date(2024, 12, 31), 90.0), kline_row(date(2025, 1, 1), 91.0, micros=True)]
    f = kl.binance_rows_to_frame(rows)
    assert list(f["Date"].dt.strftime("%Y-%m-%d")) == ["2024-12-31", "2025-01-01"]


@pytest.mark.parametrize("header", [False, True])
def test_archive_csv_from_in_memory_zip(header):
    rows = kline_rows(date(2025, 1, 1), 31, micros=True)
    data = zip_bytes("BTCUSDT-1d-2025-01.csv", csv_bytes(rows, header=header))
    f = archive.read_zip_klines(data)
    assert len(f) == 31
    assert f["Date"].iloc[0] == pd.Timestamp("2025-01-01")
    assert f["Date"].iloc[-1] == pd.Timestamp("2025-01-31")


def test_bad_zip_is_an_archive_error():
    from cryptotrader.data.errors import ArchiveChecksumError

    with pytest.raises(ArchiveChecksumError):
        archive.read_zip_klines(b"not a zip")


def test_clean_drops_bad_rows_and_duplicates():
    raw = pd.DataFrame({"Date": ["2026-01-02", "2026-01-01", "2026-01-02", "junk", "2026-01-03"],
                        "Open": [1, 1, 1, 1, 1], "High": [2, 2, 2, 2, 2], "Low": [1, 1, 1, 1, 1],
                        "Close": [1.5, 1.4, 1.6, 1.0, 0.0], "Volume": [1, 1, 1, 1, 1]})
    f = kl.clean(raw)
    assert list(f["Date"].dt.strftime("%Y-%m-%d")) == ["2026-01-01", "2026-01-02"]
    assert f["Close"].tolist() == [1.4, 1.6]             # duplicate day keeps the last row
    assert f["Trades"].isna().all()


class TestFormingCandle:
    F = kl.binance_rows_to_frame(kline_rows(date(2026, 10, 12), 4))   # 12..15 Oct

    def test_forming_candle_dropped(self):
        out = kl.drop_unfinished(self.F, datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
        assert out["Date"].iloc[-1] == pd.Timestamp("2026-10-14")

    def test_just_after_midnight_yesterday_is_not_final_yet(self):
        out = kl.drop_unfinished(self.F, datetime(2026, 10, 15, 0, 3, tzinfo=UTC))
        assert out["Date"].iloc[-1] == pd.Timestamp("2026-10-13")


def test_last_segment_drops_history_before_a_relisting():
    old = kl.binance_rows_to_frame(kline_rows(date(2020, 1, 1), 10, 50.0))
    new = kl.binance_rows_to_frame(kline_rows(date(2022, 6, 1), 5, 2.0))
    f = kl.last_segment(kl.merge(old, new))
    assert len(f) == 5 and f["Date"].iloc[0] == pd.Timestamp("2022-06-01")
    assert len(kl.last_segment(old)) == 10


def test_merge_prefers_new_rows():
    old = kl.binance_rows_to_frame(kline_rows(date(2026, 1, 1), 3, 100.0))
    new = kl.binance_rows_to_frame([kline_row(date(2026, 1, 3), 999.0), kline_row(date(2026, 1, 4), 1000.0)])
    f = kl.merge(old, new)
    assert f["Close"].tolist() == [100.0, 101.0, 999.0, 1000.0]


def test_okx_rows_drop_unconfirmed():
    rows = [["1792022400000", "81682.6", "81933.5", "80228.2", "81340", "3630.7", "2.9e8", "2.9e8", "0"],
            ["1791936000000", "83172.2", "84147.1", "81453.2", "81682.6", "6219.0", "5.1e8", "5.1e8", "1"]]
    f = okx.rows_to_frame(rows)
    assert len(f) == 1
    assert f["Date"].iloc[0] == pd.Timestamp("2026-10-14")
    assert f["Close"].iloc[0] == 81682.6
    assert f["Trades"].isna().all()
    assert okx.inst_id("BTCUSDT") == "BTC-USDT"
