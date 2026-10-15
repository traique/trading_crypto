import hashlib
from datetime import date

import pandas as pd
import pytest

from cryptotrader.data import archive
from cryptotrader.data.errors import ArchiveChecksumError, NoMarketDataError
from cryptotrader.data.net import HttpResult
from tests.helpers import csv_bytes, kline_rows, zip_bytes

NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


def listing(prefixes=(), keys=(), truncated=False, next_marker=None) -> bytes:
    parts = [f'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult {NS}><Name>data.binance.vision</Name>',
             f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>"]
    if next_marker:
        parts.append(f"<NextMarker>{next_marker}</NextMarker>")
    parts += [f"<Contents><Key>{k}</Key><Size>1</Size></Contents>" for k in keys]
    parts += [f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in prefixes]
    parts.append("</ListBucketResult>")
    return "".join(parts).encode()


def test_parse_listing():
    prefixes, keys, truncated, marker = archive.parse_listing(listing(
        prefixes=["data/spot/monthly/klines/BTCUSDT/", "data/spot/monthly/klines/LUNAUSDT/"],
        truncated=True, next_marker="data/spot/monthly/klines/LUNAUSDT/"))
    assert prefixes == ["data/spot/monthly/klines/BTCUSDT/", "data/spot/monthly/klines/LUNAUSDT/"]
    assert keys == [] and truncated and marker == "data/spot/monthly/klines/LUNAUSDT/"


def test_checksum():
    data = b"hello"
    digest = hashlib.sha256(data).hexdigest()
    archive.verify_checksum(data, f"{digest}  BTCUSDT-1d-2025-01.zip\n")
    with pytest.raises(ArchiveChecksumError):
        archive.verify_checksum(b"tampered", f"{digest}  x.zip")
    with pytest.raises(ArchiveChecksumError):
        archive.verify_checksum(data, "garbage")


class FakeArchive:
    """Serves an S3 listing, zips and checksums from memory."""

    def __init__(self, symbol, months, days, *, bad_checksum=None, no_checksum=()):
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []
        for month, rows in months.items():
            self.files[f"data/spot/monthly/klines/{symbol}/1d/{symbol}-1d-{month}.zip"] = zip_bytes(
                f"{symbol}-1d-{month}.csv", csv_bytes(rows))
        for day, rows in days.items():
            self.files[f"data/spot/daily/klines/{symbol}/1d/{symbol}-1d-{day}.zip"] = zip_bytes(
                f"{symbol}-1d-{day}.csv", csv_bytes(rows))
        self.bad_checksum = bad_checksum
        self.no_checksum = set(no_checksum)

    def __call__(self, url, params=None, allow_status=(), **kw):
        self.requests.append(url + (f"?{params}" if params else ""))
        if url == archive.LISTING_URL:
            prefix = params["prefix"]
            keys = sorted(k for k in self.files if k.startswith(prefix))
            return HttpResult(200, listing(keys=keys), url)
        key = url[len(archive.ARCHIVE_URL) + 1:]
        if key.endswith(".CHECKSUM"):
            target = key[: -len(".CHECKSUM")]
            if target in self.no_checksum or target not in self.files:
                return HttpResult(404, b"", url)
            digest = "0" * 64 if target == self.bad_checksum else hashlib.sha256(self.files[target]).hexdigest()
            return HttpResult(200, f"{digest}  {target.rsplit('/', 1)[-1]}".encode(), url)
        if key in self.files:
            return HttpResult(200, self.files[key], url)
        return HttpResult(404, b"", url)


def _months_fixture():
    jan = kline_rows(date(2025, 1, 1), 31, 100.0, micros=True)
    feb = kline_rows(date(2025, 2, 1), 28, 131.0, micros=True)
    days = {f"2025-03-0{i}": kline_rows(date(2025, 3, i), 1, 158.0 + i, micros=True) for i in range(1, 4)}
    return {"2025-01": jan, "2025-02": feb}, days


def test_fetch_monthly_then_daily(monkeypatch):
    months, days = _months_fixture()
    fake = FakeArchive("LUNAUSDT", months, days, no_checksum={
        "data/spot/daily/klines/LUNAUSDT/1d/LUNAUSDT-1d-2025-03-02.zip"})
    monkeypatch.setattr(archive, "http_get", fake)
    f = archive.fetch_klines("LUNAUSDT", end=date(2025, 3, 31))
    assert len(f) == 31 + 28 + 3
    assert f["Date"].iloc[0] == pd.Timestamp("2025-01-01")
    assert f["Date"].iloc[-1] == pd.Timestamp("2025-03-03")
    assert f.attrs["source"] == archive.SOURCE


def test_fetch_window_skips_months_outside(monkeypatch):
    months, days = _months_fixture()
    fake = FakeArchive("LUNAUSDT", months, days)
    monkeypatch.setattr(archive, "http_get", fake)
    f = archive.fetch_klines("LUNAUSDT", start=date(2025, 2, 10), end=date(2025, 2, 20))
    assert f["Date"].iloc[0] == pd.Timestamp("2025-02-10") and f["Date"].iloc[-1] == pd.Timestamp("2025-02-20")
    assert not any("2025-01.zip" in r for r in fake.requests)


def test_checksum_mismatch_refuses_the_file(monkeypatch):
    months, days = _months_fixture()
    fake = FakeArchive("LUNAUSDT", months, days,
                       bad_checksum="data/spot/monthly/klines/LUNAUSDT/1d/LUNAUSDT-1d-2025-02.zip")
    monkeypatch.setattr(archive, "http_get", fake)
    with pytest.raises(ArchiveChecksumError):
        archive.fetch_klines("LUNAUSDT", end=date(2025, 3, 31))


def test_unknown_pair(monkeypatch):
    monkeypatch.setattr(archive, "http_get", FakeArchive("LUNAUSDT", {}, {}))
    with pytest.raises(NoMarketDataError):
        archive.fetch_klines("NOPEUSDT", end=date(2025, 3, 31))


def test_list_symbols_paginates_and_caches(monkeypatch):
    pages = [
        HttpResult(200, listing(prefixes=["data/spot/monthly/klines/BTCUSDT/", "data/spot/monthly/klines/ETHBTC/"],
                                truncated=True, next_marker="data/spot/monthly/klines/ETHBTC/"), "u"),
        HttpResult(200, listing(prefixes=["data/spot/monthly/klines/LUNAUSDT/"]), "u"),
    ]
    seen = []

    def fake(url, params=None, **kw):
        seen.append(dict(params))
        return pages.pop(0)

    monkeypatch.setattr(archive, "http_get", fake)
    assert archive.list_symbols() == ["BTCUSDT", "LUNAUSDT"]
    assert seen[1]["marker"] == "data/spot/monthly/klines/ETHBTC/"

    def boom(*a, **k):
        raise AssertionError("cached list should be used")

    monkeypatch.setattr(archive, "http_get", boom)
    assert archive.list_symbols() == ["BTCUSDT", "LUNAUSDT"]
