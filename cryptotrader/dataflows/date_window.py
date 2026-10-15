"""Point-in-time rules for every dated path: a run dated D sees only what was public by D's close.

Crypto trades 24/7 and the daily candle of day D (UTC) closes at D+1 00:00 UTC
(07:00 Vietnam time). A run dated D therefore reads:

- candles up to and including D (``cryptotrader.data.load_ohlcv`` drops a candle
  that is still forming);
- news, sentiment and on-chain/derivatives figures published strictly before
  ``cutoff(D)`` = min(D+1 00:00 UTC, now).

``as_of`` / ``as_of_window`` clamp a date the model asks a tool for to the run
date, so a tool never reaches a source with a later one. ``is_live`` says
whether present-day-only data (CoinGecko's current supply, BTC dominance) may
be served: only to a run dated on the last closed candle or later.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from cryptotrader.market import rules


def to_utc(dt: datetime) -> datetime:
    """A datetime as UTC-aware; a naive value is taken to be UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def parse_day(value) -> date | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def utc_today(now: datetime | None = None) -> date:
    return rules.as_utc(now).date()


def validate_trade_date(value, now: datetime | None = None) -> str:
    """The run date as canonical ``YYYY-MM-DD``, no later than today (UTC); ValueError otherwise."""
    text = str(value)
    day = parse_day(text)
    if day is None or day.strftime("%Y-%m-%d") != text:
        raise ValueError(f"Ngày phân tích phải có dạng YYYY-MM-DD, nhận được {value!r}")
    if day > utc_today(now):
        raise ValueError(f"Ngày phân tích {text} nằm trong tương lai (theo giờ UTC)")
    return text


def cutoff(trade_date: str, now: datetime | None = None) -> datetime:
    """The instant a run dated ``trade_date`` stands at: the close of its UTC day, or now if earlier."""
    day = parse_day(trade_date)
    if day is None:
        raise ValueError(f"not a date: {trade_date!r}")
    close = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
    return min(close, rules.as_utc(now))


def in_window(published: datetime | None, start: datetime, end: datetime,
              now: datetime | None = None) -> bool:
    """Whether an item published at ``published`` is in the half-open window ``[start, end)``.

    An undated item is kept only when the window reaches the present (within a
    day): a historical run cannot prove it was not written later.
    """
    if published is not None:
        return to_utc(start) <= to_utc(published) < to_utc(end)
    return to_utc(end) >= rules.as_utc(now) - timedelta(days=1)


def is_live(trade_date: str | None, now: datetime | None = None) -> bool:
    """Whether a run dated ``trade_date`` may read present-day-only data.

    True for the last closed candle (the usual run) and today; a run dated
    earlier would read values from after its own date.
    """
    day = parse_day(trade_date)
    return day is not None and day >= rules.final_day(rules.as_utc(now))


def as_of(requested: str | None, trade_date: str) -> str | None:
    """The date a tool serves: the model's date, but never later than the run's.

    An empty ``trade_date`` (a direct call outside a graph run) passes the request through.
    """
    if not trade_date:
        return requested
    asked, run = parse_day(requested), parse_day(trade_date)
    return requested if asked is not None and run is not None and asked <= run else trade_date


def as_of_window(start_date: str, end_date: str, trade_date: str) -> tuple[str, str]:
    """``[start, end]`` with its end clamped to the run date.

    A window wholly after the run date keeps its length and moves back to end
    there. An unreadable start raises ValueError for the tool to send back.
    """
    start, old_end = parse_day(start_date), parse_day(end_date)
    if start is None:
        raise ValueError(f"start_date {start_date!r} is not a date; give it as YYYY-MM-DD")
    end = as_of(end_date, trade_date)
    end_day = parse_day(end)
    if end_day is None:
        raise ValueError(f"end_date {end_date!r} is not a date; give it as YYYY-MM-DD")
    if end == end_date or start <= end_day:
        return start_date, end
    span = (old_end - start) if old_end is not None and old_end >= start else timedelta(0)
    return f"{end_day - span:%Y-%m-%d}", end


def coverage_gap(oldest: datetime | None, start: datetime, source: str, subject: str) -> str | None:
    """A note when a feed that only serves recent items did not reach back to ``start``, else None.

    An RSS feed holds its latest items only; for a past window it cannot say
    "no news", so the absence is reported as a gap rather than as a finding.
    """
    if oldest is not None and to_utc(oldest) <= to_utc(start):
        return None
    return (f"<{source}: nguồn chỉ giữ tin gần nhất, không phủ được tới {to_utc(start):%Y-%m-%d}; "
            f"thiếu tin ở đây KHÔNG có nghĩa là không có {subject}>")
