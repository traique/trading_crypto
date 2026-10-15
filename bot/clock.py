"""The app's clock: UTC for the market, Vietnam time for people.

Every part of the bot reads the time through ``now_utc`` (tests replace it), so
the daily candle (a UTC day) and the times shown to the owner (Vietnam time,
UTC+7 all year: no daylight saving) never depend on the host's own timezone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from cryptotrader.market import rules

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_vn() -> datetime:
    return now_utc().astimezone(VN_TZ)


def today_utc() -> str:
    """Today's UTC date (the day of the candle that is forming now), YYYY-MM-DD."""
    return now_utc().date().isoformat()


def final_day() -> str:
    """The UTC day of the last closed (final) daily candle, YYYY-MM-DD."""
    return rules.final_day(now_utc()).isoformat()


def vn_time(ts: float | None, fmt: str = "%d/%m %H:%M") -> str:
    """An epoch time in Vietnam time, whatever the server's own zone (often UTC)."""
    return datetime.fromtimestamp(ts, VN_TZ).strftime(fmt) if ts else ""
