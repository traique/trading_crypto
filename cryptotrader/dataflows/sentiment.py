"""Market sentiment figures that need no API key, each point-in-time or withheld.

- Fear & Greed index (api.alternative.me): one value a day, stamped 00:00 UTC;
  a run reads the values stamped before its cutoff.
- Perpetual funding rate and open interest (OKX public API; Binance futures is
  geo-blocked here): funding settlements and daily open-interest rows before
  the cutoff. A coin without an OKX USDT perpetual simply has no such block.
- BTC dominance (CoinGecko ``/global``): a present-day value only, so it is
  served to live runs and withheld from past ones.

Every source degrades to a one-line note: the sentiment analyst always sees
either data or a clear gap, never an exception.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from cryptotrader.config import data_dir
from cryptotrader.data import net
from cryptotrader.data.errors import VendorError
from cryptotrader.data.files import write_text_atomic
from cryptotrader.data.symbols import base_asset
from cryptotrader.dataflows import date_window, fmt

logger = logging.getLogger(__name__)

FNG_URL = "https://api.alternative.me/fng/"
OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate-history"
OKX_OI_URL = "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume"
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
FNG_CACHE_SECONDS = 6 * 3600


def _fetch_json(url: str, params: dict | None, source: str):
    return net.breaker(source).call(net.http_get, url, params, deadline=20.0).json()


# Replaceable in tests: (url, params, source) -> parsed JSON.
FETCH_JSON: Callable[[str, dict | None, str], object] = _fetch_json


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# ── Fear & Greed ──
def _fng_rows(now: datetime | None) -> list[dict]:
    """Every daily Fear & Greed value (newest first), cached on disk for a few hours."""
    path = data_dir() / "sentiment" / "fear_greed.json"
    now_ts = date_window.to_utc(now).timestamp() if now else time.time()
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if now_ts - float(cached.get("fetched_at", 0)) < FNG_CACHE_SECONDS and cached.get("data"):
            return cached["data"]
    except (OSError, ValueError, AttributeError):
        pass
    payload = FETCH_JSON(FNG_URL, {"limit": 0, "format": "json"}, "alternative_me")
    rows = (payload or {}).get("data") or []
    if rows:
        write_text_atomic(path, json.dumps({"fetched_at": now_ts, "data": rows}))
    return rows


def fear_greed(trade_date: str, now: datetime | None = None, days: int = 30) -> dict | None:
    """{latest, label, date, avg_7d, avg_30d, min_30d, max_30d, series} as of the run, or None."""
    end = date_window.cutoff(trade_date, now)
    rows = []
    for row in _fng_rows(now):
        try:
            stamp = datetime.fromtimestamp(int(row["timestamp"]), tz=UTC)
            value = int(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if stamp < end:
            rows.append((stamp, value, str(row.get("value_classification") or "")))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0], reverse=True)
    recent = rows[:days]
    values = [v for _, v, _ in recent]
    return {
        "latest": values[0], "label": recent[0][2], "date": f"{recent[0][0]:%Y-%m-%d}",
        "avg_7d": sum(values[:7]) / len(values[:7]), "avg_30d": sum(values) / len(values),
        "min_30d": min(values), "max_30d": max(values),
        "series": [(f"{s:%Y-%m-%d}", v) for s, v, _ in recent[:14]],
    }


# ── OKX derivatives ──
def funding(symbol: str, trade_date: str, now: datetime | None = None) -> dict | None:
    """Funding settlements of the coin's OKX USDT perpetual before the cutoff, or None when it has none."""
    end = date_window.cutoff(trade_date, now)
    inst = f"{base_asset(symbol)}-USDT-SWAP"
    payload = FETCH_JSON(OKX_FUNDING_URL, {"instId": inst, "limit": 100, "after": _ms(end)}, "okx")
    if str((payload or {}).get("code", "0")) != "0":
        return None                       # no such perpetual on OKX
    rows = []
    for row in (payload or {}).get("data") or []:
        try:
            stamp = datetime.fromtimestamp(int(row["fundingTime"]) / 1000, tz=UTC)
            rate = float(row.get("realizedRate") or row["fundingRate"])
        except (KeyError, TypeError, ValueError):
            continue
        if stamp < end:
            rows.append((stamp, rate))
    if not rows:
        return None
    rows.sort(reverse=True)
    week = [r for s, r in rows if s >= end - timedelta(days=7)]
    per_day = 24 / max(1.0, _interval_hours(rows))
    return {
        "instrument": inst, "latest": rows[0][1], "latest_time": f"{rows[0][0]:%Y-%m-%d %H:%M}",
        "avg_7d": sum(week) / len(week) if week else None,
        "annualised_7d_pct": (sum(week) / len(week)) * per_day * 365 * 100 if week else None,
        "periods_7d": len(week),
    }


def _interval_hours(rows: list[tuple[datetime, float]]) -> float:
    if len(rows) < 2:
        return 8.0
    gaps = sorted((a[0] - b[0]).total_seconds() / 3600 for a, b in zip(rows, rows[1:], strict=False))
    return gaps[len(gaps) // 2] or 8.0


def open_interest(symbol: str, trade_date: str, now: datetime | None = None) -> dict | None:
    """Daily open interest (USD) of the coin's OKX contracts before the cutoff, or None."""
    end = date_window.cutoff(trade_date, now)
    payload = FETCH_JSON(OKX_OI_URL, {"ccy": base_asset(symbol), "period": "1D"}, "okx")
    if str((payload or {}).get("code", "0")) != "0":
        return None
    rows = []
    for row in (payload or {}).get("data") or []:
        try:
            stamp = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
            oi = float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        # A daily row stamped at the start of its day is complete one day later.
        if stamp + timedelta(days=1) <= end:
            rows.append((stamp, oi))
    if not rows:
        return None
    rows.sort(reverse=True)
    if (end - rows[0][0]) > timedelta(days=3):
        return None                       # the feed does not reach back to this date
    out = {"latest": rows[0][1], "date": f"{rows[0][0]:%Y-%m-%d}"}
    if len(rows) > 7 and rows[7][1] > 0:
        out["change_7d_pct"] = (rows[0][1] / rows[7][1] - 1) * 100
    return out


# ── CoinGecko ──
def btc_dominance(trade_date: str, now: datetime | None = None) -> float | None:
    """BTC's share of total crypto market cap (%), for a live run only."""
    if not date_window.is_live(trade_date, now):
        return None
    payload = FETCH_JSON(COINGECKO_GLOBAL, None, "coingecko")
    try:
        return float(payload["data"]["market_cap_percentage"]["btc"])
    except (KeyError, TypeError, ValueError):
        return None


def _safe(label: str, fn, *args):
    try:
        return fn(*args), None
    except VendorError as exc:
        logger.info("%s unavailable: %s", label, exc)
        return None, f"{label}: không truy cập được ({exc})"
    except Exception as exc:  # noqa: BLE001 - one source must not sink the block
        logger.warning("%s failed: %s", label, exc)
        return None, f"{label}: lỗi khi đọc ({type(exc).__name__})"


def sentiment_block(symbol: str, trade_date: str, now: datetime | None = None) -> str:
    """Every sentiment figure available for the run, as text for the sentiment analyst."""
    lines = [f"## Chỉ số tâm lý thị trường (đến trước {date_window.cutoff(trade_date, now):%Y-%m-%d %H:%M} UTC)"]
    gaps = []
    fng, gap = _safe("Fear & Greed (alternative.me)", fear_greed, trade_date, now)
    if fng:
        series = ", ".join(f"{d}: {v}" for d, v in fng["series"])
        lines.append(f"- Fear & Greed ngày {fng['date']}: {fng['latest']} ({fng['label']}); "
                     f"TB 7 ngày {fmt.number(fng['avg_7d'], 1)}, TB 30 ngày {fmt.number(fng['avg_30d'], 1)}, "
                     f"thấp/cao 30 ngày {fng['min_30d']}/{fng['max_30d']}. Chuỗi gần nhất: {series}")
    else:
        gaps.append(gap or "Fear & Greed: không có giá trị nào trước ngày phân tích")
    fund, gap = _safe("Funding OKX", funding, symbol, trade_date, now)
    if fund:
        lines.append(f"- Funding perpetual {fund['instrument']} (OKX) lần gần nhất {fund['latest_time']} UTC: "
                     f"{fund['latest'] * 100:.4f}%; TB 7 ngày {fund['avg_7d'] * 100:.4f}%/kỳ "
                     f"(~{fmt.number(fund['annualised_7d_pct'], 1)}%/năm, {fund['periods_7d']} kỳ). "
                     "Funding dương cao = phe long đòn bẩy đông.")
    else:
        gaps.append(gap or "Funding: coin này không có hợp đồng perpetual USDT trên OKX hoặc chưa có dữ liệu")
    oi, gap = _safe("Open interest OKX", open_interest, symbol, trade_date, now)
    if oi:
        change = f", thay đổi 7 ngày {fmt.signed_pct(oi['change_7d_pct'])}" if "change_7d_pct" in oi else ""
        lines.append(f"- Open interest hợp đồng {base_asset(symbol)} trên OKX ngày {oi['date']}: "
                     f"{fmt.usd_short(oi['latest'])} USD{change}.")
    else:
        gaps.append(gap or "Open interest: không có dữ liệu OKX cho ngày này")
    if date_window.is_live(trade_date, now):
        dom, gap = _safe("BTC dominance (CoinGecko)", btc_dominance, trade_date, now)
        if dom is not None:
            lines.append(f"- BTC dominance hiện tại (CoinGecko): {fmt.pct(dom)}.")
        else:
            gaps.append(gap or "BTC dominance: CoinGecko không trả lời (giới hạn tần suất)")
    else:
        gaps.append("BTC dominance: chỉ có giá trị hiện tại, không dùng cho lượt phân tích ngày quá khứ")
    if gaps:
        lines.append("- Thiếu dữ liệu: " + "; ".join(gaps))
    return "\n".join(lines)
