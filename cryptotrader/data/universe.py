"""A point-in-time liquid universe for live use: the N most liquid tradable USDT pairs on a day.

``liquid_universe(day, top=20)`` follows the research harness's ``liq_topN``
definition (``research/universe.py``), on the bot's own data:

- candidates: pairs with status TRADING in exchangeInfo (spot, USDT quote) that
  pass ``rules.is_tradable_pair`` (no stablecoins, fiat, leveraged tokens,
  wrapped/pegged duplicates);
- each candidate's closed daily candles up to ``day`` (``ohlcv.load_ohlcv``, the
  same cache the bot trades from; nothing after ``day`` is read);
- eligible: at least ``MIN_HISTORY`` trading days in its current listing, a
  trading candle in the last ``RECENT`` days, a positive median;
- ranked by the median daily USDT volume of the last ``LIQ_WINDOW`` days (days
  without a trading candle count 0), largest first.

Difference from the research version: exchangeInfo only says which pairs trade
TODAY. For today's run that is exactly right (only those can be bought); for a
catch-up day in the past (the bot replays at most 30) a pair delisted since then
is missing. That bias is bounded by the catch-up window and stated here.

Failure mode: the universe is never built from partial or old inputs. When
exchangeInfo is unavailable or older than ``MAX_INFO_AGE``, when any candidate's
candles cannot be read because a source did not answer, or when fewer than
``top`` pairs are eligible on a day that should have them, ``UniverseUnavailable``
is raised: the bot's run fails and is retried by the scheduler (and caught up
later). A day's universe, once built, is cached under
``<CRYPTO_DATA_DIR>/universe/`` and reused for that same day only; another
day's file is never served in its place.

The first build downloads every candidate's history (a few hundred pairs, a
few minutes); later builds only add the new candles.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pandas as pd

from cryptotrader.config import data_dir
from cryptotrader.data import klines as kl
from cryptotrader.data.errors import NoMarketDataError, VendorError
from cryptotrader.data.files import write_text_atomic
from cryptotrader.market import rules

logger = logging.getLogger(__name__)

MIN_HISTORY = 180            # trading days in the current listing
LIQ_WINDOW = 30              # days of the volume median
RECENT = 3                   # a trading candle within this many days
MAX_INFO_AGE = 3 * 86_400    # seconds: older exchangeInfo is not trusted for a universe
CACHE_VERSION = 1


class UniverseUnavailable(RuntimeError):
    """The universe cannot be built correctly now; the run must be retried (message in Vietnamese)."""


def cache_path(day: str, top: int) -> Path:
    return data_dir() / "universe" / f"liq_top{int(top)}_{day}.json"


def _read_cached(day: str, top: int) -> list[str] | None:
    try:
        raw = json.loads(cache_path(day, top).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION or raw.get("day") != day:
        return None
    symbols = raw.get("symbols")
    return [str(s) for s in symbols] if isinstance(symbols, list) else None


def _exchange_info() -> tuple[dict, float]:
    """(exchangeInfo, its age in seconds) from ``binance.load_exchange_info`` and its cache stamp."""
    from cryptotrader.data import binance

    info = binance.load_exchange_info()
    try:
        raw = json.loads(binance._info_cache_path().read_text(encoding="utf-8"))
        age = time.time() - float(raw.get("fetched_at", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        age = 0.0 if info else float("inf")      # served fresh without a cache file
    return info, age


def candidates(info: dict) -> list[str]:
    """TRADING spot USDT pairs that pass the exclusion rules, sorted."""
    out = []
    for symbol, item in info.items():
        trading = getattr(item, "trading", None)
        if trading is None:
            trading = getattr(item, "status", "") == "TRADING" and getattr(item, "spot", True)
        base, quote = getattr(item, "base", None), getattr(item, "quote", None)
        if trading and rules.is_tradable_pair(symbol, base, quote):
            out.append(symbol)
    return sorted(out)


def liquidity(frame: pd.DataFrame, day: str) -> tuple[bool, float, str]:
    """(eligible, median USDT volume, why not) of one pair on ``day`` from its candles up to ``day``."""
    end = pd.Timestamp(day)
    frame = frame[frame["Date"] <= end]
    traded = frame[(frame["Volume"].fillna(0) > 0) & frame["Close"].notna()]
    if traded.empty:
        return False, 0.0, "không có nến giao dịch"
    if len(traded) < MIN_HISTORY:
        return False, 0.0, f"mới {len(traded)} ngày giao dịch (< {MIN_HISTORY})"
    if (end - traded["Date"].iloc[-1]).days >= RECENT:
        return False, 0.0, "không giao dịch trong 3 ngày gần nhất"
    window = pd.date_range(end - timedelta(days=LIQ_WINDOW - 1), end, freq="D")
    qv = traded.set_index("Date")["QuoteVolume"].astype(float).reindex(window).fillna(0.0)
    med = float(qv.median())
    if not med > 0:
        return False, 0.0, "khối lượng trung vị bằng 0"
    return True, med, ""


Loader = Callable[..., pd.DataFrame]


def liquid_universe(day: str, top: int = 20, *, loader: Loader | None = None, info: dict | None = None,
                    use_cache: bool = True) -> list[str]:
    """The ``top`` most liquid tradable USDT pairs for a decision at the close of ``day`` (UTC).

    Raises ``UniverseUnavailable`` instead of returning a partial or stale universe."""
    day = kl.to_day(day).isoformat()
    top = int(top)
    if top <= 0:
        raise ValueError(f"top must be positive: {top}")
    if use_cache and (hit := _read_cached(day, top)) is not None:
        return hit
    if info is None:
        info, age = _exchange_info()
        if not info:
            raise UniverseUnavailable("không tải được exchangeInfo của Binance (danh sách cặp đang giao dịch)")
        if age > MAX_INFO_AGE:
            raise UniverseUnavailable(f"exchangeInfo đã cũ {age / 86_400:.1f} ngày - không dựng rổ từ danh sách cũ")
    if loader is None:
        from cryptotrader.data import ohlcv

        loader = ohlcv.load_ohlcv
    names = candidates(info)
    if not names:
        raise UniverseUnavailable("exchangeInfo không có cặp USDT nào đang giao dịch")
    ranked, missing, excluded = [], [], {}
    for symbol in names:
        try:
            frame = loader(symbol, day)
        except NoMarketDataError as exc:          # not trading at that day (or not yet listed)
            excluded[symbol] = str(exc)[:120]
            continue
        except VendorError as exc:                # a source did not answer: not "no data"
            missing.append(symbol)
            logger.warning("Universe %s: candles of %s unavailable: %s", day, symbol, exc)
            continue
        ok, med, why = liquidity(frame, day)
        if ok:
            ranked.append((symbol, med))
        else:
            excluded[symbol] = why
    if missing:
        raise UniverseUnavailable(
            f"nguồn giá không trả lời cho {len(missing)}/{len(names)} cặp ({', '.join(missing[:5])}"
            f"{', ...' if len(missing) > 5 else ''}); rổ {day} chưa dựng, sẽ thử lại")
    if len(ranked) < top:
        raise UniverseUnavailable(f"chỉ {len(ranked)} cặp đủ điều kiện ngày {day} (cần {top}); dữ liệu có thể chưa đủ")
    ranked.sort(key=lambda kv: (-kv[1], kv[0]))
    symbols = [s for s, _ in ranked[:top]]
    write_text_atomic(cache_path(day, top), json.dumps({
        "version": CACHE_VERSION, "day": day, "kind": f"liq_top{top}", "symbols": symbols,
        "median_quote_volume": {s: round(m, 2) for s, m in ranked[:top]},
        "candidates": len(names), "eligible": len(ranked), "excluded": len(excluded),
        "built_at": time.time(),
    }, ensure_ascii=False))
    return symbols
