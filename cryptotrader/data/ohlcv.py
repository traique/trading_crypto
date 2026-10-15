"""Daily OHLCV of Binance USDT pairs: cached, point-in-time, closed candles only.

``load_ohlcv(symbol, end_date, start=None)`` is the one entry point. Every
frame it returns:

- has the columns of ``klines.COLUMNS`` (Date as the candle's UTC day);
- holds no row after ``end_date`` and no candle that is not final yet
  (``rules.final_day``: today's forming candle is always dropped);
- holds only the pair's current listing (rows before a gap of more than a week,
  e.g. a delisting and relisting, are dropped; see ``klines.last_segment``);
- is refused (``NoMarketDataError``) when its last traded candle (volume > 0)
  is more than ``MAX_STALE_DAYS`` before the requested day, unless
  ``allow_stale`` (research on delisted pairs).

Sources: the Binance data mirror (REST) first (it keeps the history of most
delisted pairs too); when it does not know the pair, the bulk archive; when Binance cannot answer, OKX fills the missing
tail for this call only (never written to the cache, as its volumes differ).
Each source has a circuit breaker and hard timeouts (``net``), so a hung source
costs a bounded wait and is then skipped.

The cache is one CSV per pair under ``<CRYPTO_DATA_DIR>/ohlcv/`` plus a small
JSON sidecar recording through which UTC day the file is known complete
(``checked_through``). Files are replaced atomically under a per-file lock.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

from cryptotrader.config import data_dir
from cryptotrader.data import archive, binance, klines as kl, okx
from cryptotrader.data.errors import (
    NoMarketDataError,
    UnknownSymbolError,
    VendorError,
    VendorUnavailableError,
)
from cryptotrader.data.files import locked, replace_file, write_text_atomic
from cryptotrader.data.symbols import normalize_symbol, safe_file_component
from cryptotrader.market import rules

logger = logging.getLogger(__name__)

COLUMNS = kl.COLUMNS
# Crypto trades every day: a last candle older than this is a halted or delisted pair.
MAX_STALE_DAYS = 3
# A delisted pair's archive is looked at again after this long (it does not change).
ARCHIVE_RECHECK = timedelta(days=7)

# Replaceable in tests: source name -> fetch(symbol, start, end) -> frame.
FETCHERS = {
    "binance": binance.fetch_klines,
    "archive": archive.fetch_klines,
    "okx": okx.fetch_klines,
}


def cache_dir() -> Path:
    path = data_dir() / "ohlcv"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_paths(symbol: str) -> tuple[Path, Path]:
    name = safe_file_component(symbol)
    base = cache_dir()
    return base / f"{name}-1d.csv", base / f"{name}-1d.meta.json"


def read_cache(symbol: str) -> tuple[pd.DataFrame | None, dict]:
    """The cached candles and their sidecar; (None, {}) when missing or unreadable."""
    csv_path, meta_path = cache_paths(symbol)
    try:
        frame = kl.clean(pd.read_csv(csv_path, encoding="utf-8"))
    except (OSError, ValueError, KeyError):     # missing, empty or damaged: download again
        frame = None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            meta = {}
    except (OSError, ValueError):
        meta = {}
    if frame is not None and frame.empty:
        frame = None
    return frame, meta


def write_cache(symbol: str, frame: pd.DataFrame, meta: dict) -> None:
    csv_path, meta_path = cache_paths(symbol)
    out = frame[COLUMNS].copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    replace_file(csv_path, lambda temp: out.to_csv(temp, index=False, encoding="utf-8"))
    write_text_atomic(meta_path, json.dumps(meta, sort_keys=True))


def _checked_through(frame: pd.DataFrame | None, meta: dict) -> date | None:
    raw = meta.get("checked_through")
    if raw:
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            pass
    if frame is not None and not frame.empty:
        return frame["Date"].iloc[-1].date()
    return None


def _recent(stamp: str | None, now: datetime, within: timedelta) -> bool:
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return now - when < within


def _refresh(symbol: str, target: date, now: datetime) -> tuple[pd.DataFrame | None, str]:
    """The pair's candles complete through ``target`` when the sources allow, and their source.

    Downloads only what the cache lacks. When every Binance source fails, OKX
    fills the tail for this call; failing that, the cache is served as it is
    (the staleness check of ``load_ohlcv`` then decides whether it is usable).
    """
    csv_path, _ = cache_paths(symbol)
    cached, meta = read_cache(symbol)
    if cached is not None and (_checked_through(cached, meta) or date.min) >= target:
        return cached, str(meta.get("source") or "cache")
    with locked(csv_path):
        cached, meta = read_cache(symbol)            # another process may have refreshed it
        if cached is not None and (_checked_through(cached, meta) or date.min) >= target:
            return cached, str(meta.get("source") or "cache")
        last = cached["Date"].iloc[-1].date() if cached is not None else None
        last_final = rules.final_day(now)
        try:
            try:
                new = FETCHERS["binance"](symbol, last, now.date())
                source, status = "binance", "trading"
            except UnknownSymbolError:
                if cached is not None and meta.get("status") == "delisted" and _recent(
                        meta.get("archive_checked_at"), now, ARCHIVE_RECHECK):
                    new = kl.empty_frame()
                else:
                    try:
                        new = FETCHERS["archive"](symbol, (last + timedelta(days=1)) if last else None, last_final)
                    except NoMarketDataError:
                        if cached is None:
                            raise
                        new = kl.empty_frame()      # nothing after what the cache already has
                    meta["archive_checked_at"] = now.isoformat()
                source, status = "binance_archive", "delisted"
        except VendorUnavailableError as exc:
            logger.warning("Binance unavailable for %s (%s); trying OKX for the missing days", symbol, exc)
            return _okx_tail(symbol, cached, last, target)
        new = kl.drop_unfinished(new, now)
        merged = kl.merge(cached, new)
        if merged.empty:
            raise NoMarketDataError(symbol, "nguồn không trả về nến ngày nào")
        if status == "trading" and (last_final - _last_traded(merged)).days > MAX_STALE_DAYS:
            status = "inactive"                     # still known to REST, but no longer trading
        checked = last_final
        if status == "trading":
            # A lagging mirror may not have published the last final candle yet: never
            # mark a day complete that the source did not return, so the next call
            # (e.g. the scheduler's late-candle retry) asks again from ``last``.
            checked = min(last_final, merged["Date"].iloc[-1].date())
        meta.update({"checked_through": checked.isoformat(), "status": status, "source": source,
                     "updated_at": now.isoformat()})
        write_cache(symbol, merged, meta)
        return merged, source


def _last_traded(frame: pd.DataFrame) -> date:
    """The last day with trading: a halted or delisted pair can keep printing flat
    candles with zero volume, which must not make it look current."""
    traded = frame[~(frame["Volume"] <= 0)]          # NaN volume (unknown) counts as traded
    return (traded if not traded.empty else frame)["Date"].iloc[-1].date()


def _okx_tail(symbol: str, cached: pd.DataFrame | None, last: date | None,
              target: date) -> tuple[pd.DataFrame | None, str]:
    try:
        tail = FETCHERS["okx"](symbol, (last + timedelta(days=1)) if last else None, target)
    except VendorError as exc:
        if cached is not None:
            logger.warning("OKX also failed for %s (%s); serving the cached candles", symbol, exc)
            return cached, "cache"
        raise VendorUnavailableError(f"Không nguồn nào trả lời cho {symbol}: {exc}") from None
    if cached is None:
        return kl.clean(tail), "okx"
    return kl.merge(cached, tail[tail["Date"] > cached["Date"].iloc[-1]]), "binance+okx"


def load_ohlcv(symbol: str, end_date=None, start=None, *, now: datetime | None = None,
               allow_stale: bool = False, segment: bool = True) -> pd.DataFrame:
    """Closed daily candles of ``symbol`` up to and including ``end_date`` (UTC day).

    ``end_date`` defaults to the last final candle; a later date is clamped to it.
    ``start`` trims the beginning. Raises ``NoMarketDataError`` when no usable
    rows exist (unknown pair, nothing in the window, stale), and
    ``VendorUnavailableError`` when no source answered and nothing is cached.
    The source used is in ``frame.attrs["source"]``.
    """
    pair = normalize_symbol(symbol)
    now = rules.as_utc(now)
    last_final = rules.final_day(now)
    target = min(kl.to_day(end_date), last_final) if end_date is not None else last_final
    frame, source = _refresh(pair, target, now)
    if frame is None or frame.empty:
        raise NoMarketDataError(pair, "không có dữ liệu")
    frame = kl.drop_unfinished(frame, now)
    frame = frame[frame["Date"] <= pd.Timestamp(target)].reset_index(drop=True)
    if segment:
        frame = kl.last_segment(frame)
    if frame.empty:
        raise NoMarketDataError(pair, f"không có nến nào đến ngày {target.isoformat()}")
    latest = _last_traded(frame)
    if not allow_stale and (target - latest).days > MAX_STALE_DAYS:
        raise NoMarketDataError(pair, f"nến cuối là {latest.isoformat()}, cũ hơn {target.isoformat()} "
                                      f"{(target - latest).days} ngày (ngừng giao dịch hoặc bị huỷ niêm yết)")
    if start is not None:
        frame = frame[frame["Date"] >= pd.Timestamp(kl.to_day(start))].reset_index(drop=True)
        if frame.empty:
            raise NoMarketDataError(pair, f"không có nến nào từ {kl.to_day(start)} đến {target}")
    frame.attrs["source"] = source
    frame.attrs["symbol"] = pair
    return frame


def load_many(symbols, end_date=None, start=None, **kwargs) -> dict[str, pd.DataFrame]:
    """``load_ohlcv`` for several pairs; a pair that fails is logged and left out."""
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        try:
            frame = load_ohlcv(symbol, end_date, start, **kwargs)
        except (VendorError, ValueError) as exc:
            logger.warning("No candles for %s: %s", symbol, exc)
            continue
        out[frame.attrs["symbol"]] = frame
    return out
