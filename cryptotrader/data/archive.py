"""Binance bulk archives (data.binance.vision): full history, delisted pairs included.

The REST API forgets a pair once it is delisted; the archive keeps it. That is
what a survivorship-free backtest universe needs: ``list_symbols`` reads the
S3 bucket listing, so it returns every pair that ever had spot klines.

Files: ``data/spot/monthly/klines/<SYM>/1d/<SYM>-1d-YYYY-MM.zip`` for complete
months and ``data/spot/daily/klines/<SYM>/1d/<SYM>-1d-YYYY-MM-DD.zip`` for the
days after the last monthly file. Each may have a ``.CHECKSUM`` file (SHA-256);
when present it is verified and a mismatching file is refused. Timestamps are
milliseconds before 2025 and microseconds after; ``klines`` handles both.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import time
import zipfile
from datetime import UTC, date, datetime, timedelta
from xml.etree import ElementTree

import pandas as pd

from cryptotrader.config import data_dir
from cryptotrader.data import klines as kl
from cryptotrader.data.errors import (
    ArchiveChecksumError,
    NoMarketDataError,
    VendorError,
    VendorUnavailableError,
)
from cryptotrader.data.files import write_text_atomic
from cryptotrader.data.net import breaker, http_get

logger = logging.getLogger(__name__)

SOURCE = "binance_archive"
ARCHIVE_URL = "https://data.binance.vision"
LISTING_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
KLINES_PREFIX = "data/spot/{period}/klines/"
MAX_LISTING_PAGES = 50
SYMBOL_LIST_MAX_AGE = 24 * 3600
FETCH_DEADLINE = 600.0               # seconds for one symbol's whole archive history
DAILY_WINDOW_DAYS = 62

_MONTHLY = re.compile(r"-(\d{4})-(\d{2})\.zip$")
_DAILY = re.compile(r"-(\d{4})-(\d{2})-(\d{2})\.zip$")


# ── the bucket listing ──
def parse_listing(xml: bytes | str) -> tuple[list[str], list[str], bool, str | None]:
    """(common prefixes, keys, is_truncated, next marker) of one S3 ListBucket page."""
    root = ElementTree.fromstring(xml)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag[: root.tag.index("}") + 1]
    prefixes = [text for cp in root.findall(f"{ns}CommonPrefixes") if (text := cp.findtext(f"{ns}Prefix"))]
    keys = [k.text for k in root.iter(f"{ns}Key") if k.text]
    truncated = (root.findtext(f"{ns}IsTruncated") or "").strip().lower() == "true"
    marker = root.findtext(f"{ns}NextMarker") or None
    if truncated and not marker:
        marker = (keys or prefixes or [None])[-1]
    return prefixes, keys, truncated, marker


def _list(prefix: str, *, delimiter: bool) -> tuple[list[str], list[str]]:
    """Every common prefix and key under ``prefix`` (all pages)."""
    def call():
        prefixes: list[str] = []
        keys: list[str] = []
        marker: str | None = None
        for _ in range(MAX_LISTING_PAGES):
            params = {"prefix": prefix}
            if delimiter:
                params["delimiter"] = "/"
            if marker:
                params["marker"] = marker
            result = http_get(LISTING_URL, params=params)
            try:
                page_prefixes, page_keys, truncated, marker = parse_listing(result.content)
            except ElementTree.ParseError as exc:
                raise VendorUnavailableError(f"{SOURCE}: danh sách S3 không đọc được ({exc})") from None
            prefixes += page_prefixes
            keys += page_keys
            if not truncated or not marker:
                break
        return prefixes, keys

    return breaker(SOURCE).call(call)


def list_symbols(quote: str = "USDT", *, max_age: float = SYMBOL_LIST_MAX_AGE) -> list[str]:
    """Every pair quoted in ``quote`` that has monthly spot klines, delisted ones included.

    Cached a day under the data dir; a stale cache is served when S3 cannot answer.
    """
    path = data_dir() / "archive_symbols.json"
    cached: list[str] | None = None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        cached = [str(s) for s in raw["symbols"]]
        if time.time() - float(raw["fetched_at"]) < max_age:
            return [s for s in cached if s.endswith(quote)]
    except (OSError, ValueError, KeyError, TypeError):
        cached = None
    try:
        prefixes, _ = _list(KLINES_PREFIX.format(period="monthly"), delimiter=True)
    except VendorError:
        if cached:
            return [s for s in cached if s.endswith(quote)]
        raise
    symbols = sorted({p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes if p.rstrip("/")})
    if symbols:
        write_text_atomic(path, json.dumps({"fetched_at": time.time(), "symbols": symbols}))
    return [s for s in symbols if s.endswith(quote)]


def list_files(symbol: str, period: str = "monthly", *, interval: str = "1d",
               month: str | None = None) -> list[str]:
    """The .zip keys of ``symbol``'s ``period`` (monthly/daily) klines, sorted; ``month``
    (YYYY-MM) narrows a daily listing to that month."""
    prefix = f"{KLINES_PREFIX.format(period=period)}{symbol}/{interval}/"
    if month:
        prefix += f"{symbol}-{interval}-{month}"
    _, keys = _list(prefix, delimiter=False)
    return sorted(k for k in keys if k.endswith(".zip"))


# ── files ──
def verify_checksum(data: bytes, checksum_text: str) -> None:
    """Raise ``ArchiveChecksumError`` unless ``data``'s SHA-256 is the one in ``checksum_text``."""
    expected = (checksum_text.strip().split() or [""])[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ArchiveChecksumError(f"tệp CHECKSUM không hợp lệ: {checksum_text[:80]!r}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ArchiveChecksumError(f"SHA-256 không khớp: {actual} != {expected}")


def download(key: str, *, verify: bool = True) -> bytes:
    """One archive file, checked against its .CHECKSUM when the archive has one."""
    def call():
        result = http_get(f"{ARCHIVE_URL}/{key}", allow_status=(404,))
        if result.status == 404:
            raise NoMarketDataError(key, "không có trong kho lưu trữ")
        if verify:
            check = http_get(f"{ARCHIVE_URL}/{key}.CHECKSUM", allow_status=(403, 404))
            if check.status == 200:
                verify_checksum(result.content, check.text)
            else:
                logger.debug("No checksum for %s", key)
        return result.content

    return breaker(SOURCE).call(call)


def read_zip_klines(data: bytes) -> pd.DataFrame:
    """The kline CSV inside an archive zip, as the standard frame."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if not names:
                return kl.empty_frame()
            return kl.parse_binance_csv(archive.read(names[0]))
    except zipfile.BadZipFile as exc:
        raise ArchiveChecksumError(f"tệp zip hỏng: {exc}") from None


def _month_of(key: str) -> date | None:
    m = _MONTHLY.search(key)
    return date(int(m.group(1)), int(m.group(2)), 1) if m else None


def _day_of(key: str) -> date | None:
    m = _DAILY.search(key)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _month_end(first: date) -> date:
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _months(first: date, last: date) -> list[str]:
    out, cur = [], date(first.year, first.month, 1)
    while cur <= last:
        out.append(cur.strftime("%Y-%m"))
        cur = _month_end(cur) + timedelta(days=1)
    return out


def fetch_klines(symbol: str, start: date | None = None, end: date | None = None, *,
                 interval: str = "1d") -> pd.DataFrame:
    """Daily candles of ``symbol`` from the archive: monthly files, then daily files for the
    days after the last complete month. Raises ``NoMarketDataError`` when the archive has
    no file for the pair in the window."""
    end = end or datetime.now(UTC).date()
    began = time.monotonic()
    frames: list[pd.DataFrame] = []

    def check_deadline():
        if time.monotonic() - began > FETCH_DEADLINE:
            raise VendorUnavailableError(f"{SOURCE}: tải lưu trữ {symbol} quá {FETCH_DEADLINE:.0f}s")

    monthly = [(m, k) for k in list_files(symbol, "monthly", interval=interval) if (m := _month_of(k))]
    for month, key in monthly:
        if month > end or (start and _month_end(month) < start):
            continue
        check_deadline()
        frames.append(read_zip_klines(download(key)))
    # Daily files exist only for the days a monthly file does not cover yet (the
    # monthly file of a month appears early in the next one), so at most about
    # two months past the last monthly file are searched: a pair whose monthly
    # files stopped long ago was delisted, and listing every month since would
    # cost a request per month for nothing.
    last_monthly = max((_month_end(m) for m, _ in monthly), default=None)
    if last_monthly is not None:
        daily_from = last_monthly + timedelta(days=1)
        daily_to = min(end, last_monthly + timedelta(days=DAILY_WINDOW_DAYS))
    else:                                    # a pair too young for a monthly file
        daily_from, daily_to = end - timedelta(days=DAILY_WINDOW_DAYS), end
    if start and daily_from < start:
        daily_from = start
    if daily_from <= daily_to:
        for month in _months(daily_from, daily_to):
            check_deadline()
            for key in list_files(symbol, "daily", interval=interval, month=month):
                day = _day_of(key)
                if day is None or day < daily_from or day > daily_to:
                    continue
                check_deadline()
                frames.append(read_zip_klines(download(key)))
    frame = kl.merge(None, pd.concat(frames, ignore_index=True)) if frames else kl.empty_frame()
    if start is not None and not frame.empty:
        frame = frame[frame["Date"] >= pd.Timestamp(start)]
    frame = frame[frame["Date"] <= pd.Timestamp(end)].reset_index(drop=True)
    if frame.empty:
        raise NoMarketDataError(symbol, "kho lưu trữ Binance không có nến ngày cho cặp này")
    frame.attrs["source"] = SOURCE
    return frame
