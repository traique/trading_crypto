"""Tokenomics from CoinGecko (no key): market cap, FDV, supply, ATH, with a disk cache.

CoinGecko's free API throttles hard (HTTP 429), so every answer is cached
under ``<CRYPTO_DATA_DIR>/coingecko/`` (coin ids for 30 days, coin data for 6
hours, a dated history snapshot forever) and the source sits behind its own
circuit breaker: once throttled, later calls get the cache or a clear gap
instead of waiting.

Point-in-time: supply, FDV, ATH and categories exist only as present-day
values, so they are served to live runs only. A past run gets CoinGecko's
dated snapshot (price, market cap, volume at 00:00 UTC of its date) and a note
that supply data is withheld. No free source publishes unlock schedules; the
report says so and derives what it can (circulating share of max supply, FDV
to market-cap ratio) without inventing dates.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime

from cryptotrader.config import data_dir
from cryptotrader.data import net
from cryptotrader.data.errors import NoMarketDataError, VendorError, VendorUnavailableError
from cryptotrader.data.files import write_text_atomic
from cryptotrader.data.symbols import base_asset, normalize_symbol
from cryptotrader.dataflows import date_window, fmt

logger = logging.getLogger(__name__)

API = "https://api.coingecko.com/api/v3"
ID_TTL = 30 * 86400
DATA_TTL = 6 * 3600
# The free (public) API serves /history only for the past 365 days (HTTP 401,
# error_code 10012, beyond); a margin keeps a run near the limit from asking.
HISTORY_DAYS = 360
# Answers that mean "no data for this request", not "the source is down": they
# must not open the shared breaker (live runs would lose tokenomics for minutes).
_NO_DATA_STATUSES = (400, 401, 404)


def _fetch_json(url: str, params: dict | None, source: str):
    result = net.breaker(source).call(net.http_get, url, params, deadline=20.0, retries=0,
                                      allow_status=_NO_DATA_STATUSES)
    if result.status in _NO_DATA_STATUSES:
        message = ""
        try:
            body = result.json()
            status = (body.get("error") or {}).get("status") if isinstance(body.get("error"), dict) else None
            message = str((status or {}).get("error_message") or body.get("error") or body.get("status") or "")
        except Exception:  # noqa: BLE001 - only for the message
            message = ""
        raise NoMarketDataError(source, f"CoinGecko HTTP {result.status}{': ' + message[:160] if message else ''}")
    return result.json()


FETCH_JSON: Callable[[str, dict | None, str], object] = _fetch_json


def _cache_path(name: str):
    return data_dir() / "coingecko" / name


def _read_cache(name: str, ttl: float | None):
    try:
        cached = json.loads(_cache_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict):
        return None
    if ttl is not None and time.time() - float(cached.get("fetched_at", 0)) > ttl:
        return None
    return cached.get("data")


def _write_cache(name: str, data) -> None:
    try:
        write_text_atomic(_cache_path(name), json.dumps({"fetched_at": time.time(), "data": data}))
    except OSError as exc:
        logger.warning("Could not cache %s: %s", name, exc)


def coin_id(symbol: str) -> str:
    """CoinGecko's id for the coin: the exact-ticker match with the best market-cap rank."""
    ticker = base_asset(symbol)
    cached = _read_cache(f"id-{ticker}.json", ID_TTL)
    if cached:
        return str(cached)
    payload = FETCH_JSON(f"{API}/search", {"query": ticker}, "coingecko")
    matches = [c for c in (payload or {}).get("coins") or []
               if str(c.get("symbol", "")).upper() == ticker and c.get("id")]
    if not matches:
        raise NoMarketDataError(normalize_symbol(symbol), "CoinGecko không có coin với mã này")
    best = min(matches, key=lambda c: (c.get("market_cap_rank") is None, c.get("market_cap_rank") or 0))
    _write_cache(f"id-{ticker}.json", best["id"])
    return str(best["id"])


def coin_data(cid: str) -> dict:
    cached = _read_cache(f"coin-{cid}.json", DATA_TTL)
    if cached:
        return cached
    params = {"localization": "false", "tickers": "false", "market_data": "true",
              "community_data": "false", "developer_data": "false", "sparkline": "false"}
    data = FETCH_JSON(f"{API}/coins/{cid}", params, "coingecko")
    if not isinstance(data, dict) or not data.get("market_data"):
        raise NoMarketDataError(cid, "CoinGecko không trả về market_data")
    keep = {k: data.get(k) for k in ("id", "symbol", "name", "categories", "genesis_date", "hashing_algorithm",
                                     "market_cap_rank")}
    keep["market_data"] = {k: data["market_data"].get(k) for k in (
        "current_price", "market_cap", "fully_diluted_valuation", "total_volume", "circulating_supply",
        "total_supply", "max_supply", "ath", "ath_date", "market_cap_rank")}
    _write_cache(f"coin-{cid}.json", keep)
    return keep


def coin_history(cid: str, day: str) -> dict:
    """CoinGecko's snapshot of the coin at 00:00 UTC of ``day`` (price, market cap, volume)."""
    name = f"history-{cid}-{day}.json"
    cached = _read_cache(name, None)
    if cached:
        return cached
    d = date_window.parse_day(day)
    data = FETCH_JSON(f"{API}/coins/{cid}/history", {"date": f"{d:%d-%m-%Y}", "localization": "false"}, "coingecko")
    md = (data or {}).get("market_data") if isinstance(data, dict) else None
    if not md:
        raise NoMarketDataError(cid, f"CoinGecko không có dữ liệu ngày {day}")
    keep = {k: (md.get(k) or {}).get("usd") for k in ("current_price", "market_cap", "total_volume")}
    _write_cache(name, keep)
    return keep


def _usd(field) -> float | None:
    if isinstance(field, dict):
        field = field.get("usd")
    try:
        return float(field) if field is not None else None
    except (TypeError, ValueError):
        return None


def _supply(value) -> str:
    try:
        return f"{float(value):,.0f}" if value is not None else "không công bố"
    except (TypeError, ValueError):
        return "không công bố"


def _live_report(pair: str, data: dict) -> list[str]:
    md = data.get("market_data") or {}
    price, mcap, fdv = _usd(md.get("current_price")), _usd(md.get("market_cap")), _usd(md.get("fully_diluted_valuation"))
    vol, ath = _usd(md.get("total_volume")), _usd(md.get("ath"))
    circ, total, cap = md.get("circulating_supply"), md.get("total_supply"), md.get("max_supply")
    lines = [
        f"- Tên: {data.get('name') or '?'} (CoinGecko id `{data.get('id')}`), hạng vốn hoá: "
        f"{md.get('market_cap_rank') or data.get('market_cap_rank') or 'không rõ'}",
        f"- Giá CoinGecko (hiện tại, chỉ để đối chiếu; giá giao dịch dùng nến Binance): {fmt.price(price)} USD",
        f"- Vốn hoá lưu hành (market cap): {fmt.usd_short(mcap)} USD",
        f"- FDV (vốn hoá pha loãng hoàn toàn): {fmt.usd_short(fdv)} USD",
        f"- Khối lượng 24h: {fmt.usd_short(vol)} USD"
        + (f" (bằng {fmt.pct(vol / mcap * 100)} vốn hoá)" if vol and mcap else ""),
        f"- Cung lưu hành: {_supply(circ)}; tổng cung: {_supply(total)}; cung tối đa: {_supply(cap)}",
    ]
    try:
        denominator = float(cap or total)
        lines.append(f"- Tỷ lệ đang lưu hành: {fmt.pct(float(circ) / denominator * 100)} "
                     f"của {'cung tối đa' if cap else 'tổng cung'}")
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    if fdv and mcap:
        ratio = fdv / mcap
        note = (" - phần lớn token chưa lưu hành: rủi ro pha loãng từ unlock/phát hành thêm" if ratio >= 2
                else " - phần token chưa lưu hành đáng kể" if ratio >= 1.3 else "")
        lines.append(f"- FDV / market cap: {fmt.number(ratio)}x{note}")
    if ath and price:
        ath_date = md.get("ath_date")
        ath_date = ath_date.get("usd") if isinstance(ath_date, dict) else ath_date
        lines.append(f"- Đỉnh lịch sử (ATH): {fmt.price(ath)} USD ngày {str(ath_date or 'không rõ')[:10]}; "
                     f"giá hiện cách ATH {fmt.signed_pct((price / ath - 1) * 100)}")
    if data.get("categories"):
        lines.append(f"- Nhóm: {', '.join(str(c) for c in data['categories'][:6] if c)}")
    if data.get("genesis_date"):
        lines.append(f"- Ngày ra đời: {data['genesis_date']}")
    lines.append("- Lịch unlock: không có nguồn miễn phí công bố lịch mở khoá token; KHÔNG suy đoán ngày "
                 "unlock - chỉ dùng tỷ lệ lưu hành và FDV/market cap ở trên.")
    return lines


def tokenomics_report(symbol: str, trade_date: str, now: datetime | None = None) -> str:
    """The tokenomics block for the analyst; degrades to a note when CoinGecko cannot answer."""
    pair = normalize_symbol(symbol)
    header = [f"## Tokenomics {base_asset(pair)} (CoinGecko)"]
    live = date_window.is_live(trade_date, now)
    day = date_window.parse_day(trade_date)
    if not live and day is not None and (date_window.utc_today(now) - day).days > HISTORY_DAYS:
        # Not asked at all: the free API refuses it (401), which is no outage.
        return "\n".join(header + [
            f"<Lượt phân tích ngày quá khứ {trade_date}: API miễn phí của CoinGecko chỉ có ảnh chụp lịch sử "
            f"trong {HISTORY_DAYS + 5} ngày gần nhất, nên không có dữ liệu tokenomics cho ngày này. "
            "Cung, FDV và ATH hiện tại cũng không được dùng (sẽ lộ thông tin tương lai).>"])
    try:
        cid = coin_id(pair)
        if live:
            return "\n".join(header + ["Số liệu hiện tại (lượt phân tích ngày gần nhất):"]
                             + _live_report(pair, coin_data(cid)))
        snap = coin_history(cid, trade_date)
        return "\n".join(header + [
            f"Lượt phân tích ngày quá khứ {trade_date}: chỉ dùng ảnh chụp CoinGecko lúc 00:00 UTC ngày đó.",
            f"- Giá: {fmt.price(snap.get('current_price'))} USD; vốn hoá: {fmt.usd_short(snap.get('market_cap'))} USD; "
            f"khối lượng 24h: {fmt.usd_short(snap.get('total_volume'))} USD",
            "- Cung lưu hành/tối đa, FDV, ATH và nhóm ngành bị GIỮ LẠI: CoinGecko chỉ có giá trị hiện tại, "
            "dùng cho ngày quá khứ sẽ lộ thông tin tương lai.",
            f"- Lưu ý: mã {base_asset(pair)} được khớp với coin CoinGecko theo xếp hạng vốn hoá HIỆN TẠI; nếu mã "
            "này từng thuộc một coin khác (mã bị dùng lại, ví dụ LUNA năm 2022), ảnh chụp có thể là của coin khác.",
        ])
    except VendorUnavailableError as exc:
        return "\n".join(header + [f"<CoinGecko không trả lời (lỗi mạng, máy chủ hoặc giới hạn tần suất): {exc}. "
                                   "Thiếu dữ liệu tokenomics lượt này.>"])
    except VendorError as exc:
        return "\n".join(header + [f"<Không có dữ liệu tokenomics: {exc}>"])


def cached_name(symbol: str) -> str | None:
    """The coin's CoinGecko name when it is already cached (no network)."""
    cid = _read_cache(f"id-{base_asset(symbol)}.json", None)
    if not cid:
        return None
    data = _read_cache(f"coin-{cid}.json", None) or {}
    return data.get("name")
