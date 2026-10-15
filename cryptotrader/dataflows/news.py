"""Crypto news, dated and point-in-time: Google News RSS (English and Vietnamese), CoinDesk, Cointelegraph.

A run dated D reads only items published before ``date_window.cutoff(D)``
(the close of D's UTC candle, or now). Google News search takes
``after:``/``before:`` operators, so a past window is searched as such; the
window is re-applied to each item's ``pubDate`` because the operators are only
advisory. The CoinDesk and Cointelegraph feeds hold their latest items only:
for a window they do not reach back to, the report says so instead of reading
the absence as "no news".

Each coin item is tagged ✅ when its headline names the coin (ticker or name)
and ⚠️ when it is market-wide news; the agents may only treat ✅ items as news
*of* the coin.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from cryptotrader.data import net
from cryptotrader.data.errors import VendorUnavailableError
from cryptotrader.data.symbols import base_asset, normalize_symbol
from cryptotrader.dataflows import coins, date_window
from cryptotrader.dataflows.config import get_config

logger = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
COINDESK_RSS = "https://www.coindesk.com/arc/outboundfeeds/rss/"
COINTELEGRAPH_RSS = "https://cointelegraph.com/rss"
DEADLINE = 20.0


@dataclass
class Headline:
    title: str
    source: str
    published: datetime | None      # UTC-aware
    url: str = ""
    summary: str = ""
    about_coin: bool = False


# ── fetching (replaceable in tests) ──
def _fetch_text(url: str, params: dict | None, source: str) -> str:
    result = net.breaker(source).call(net.http_get, url, params, deadline=DEADLINE)
    return result.text


FETCH: Callable[[str, dict | None, str], str] = _fetch_text


def _get(url: str, params: dict | None, source: str) -> str:
    return FETCH(url, params, source)


# ── RSS parsing ──
def _tag(xml: str, tag: str) -> str:
    match = re.search(rf"<{tag}(?:\s[^>]*)?>([\s\S]*?)</{tag}>", xml, re.IGNORECASE)
    if not match:
        return ""
    text = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", match.group(1)).strip()
    return html.unescape(text)


def _plain(text: str, limit: int = 300) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _parse_date(raw: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return None
    return date_window.to_utc(parsed)


def parse_rss(text: str, default_source: str) -> list[Headline]:
    """Items of an RSS document; a Google News title's trailing " - Publisher" moves to ``source``."""
    items = []
    for raw in re.findall(r"<item>[\s\S]*?</item>", text or "", re.IGNORECASE):
        title = _tag(raw, "title")
        source = _tag(raw, "source") or default_source
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) <= 5:
            continue
        items.append(Headline(title=title, source=source, published=_parse_date(_tag(raw, "pubDate")),
                              url=_tag(raw, "link"), summary=_plain(_tag(raw, "description"))))
    return items


def _window(items: list[Headline], start: datetime, end: datetime) -> list[Headline]:
    """Dated items in ``[start, end)``; undated items are dropped (a past run cannot date them)."""
    return [i for i in items if i.published is not None and start <= i.published < end]


# ── sources ──
def google_news(query: str, start: datetime, end: datetime, lang: str = "en", limit: int = 20) -> list[Headline]:
    """Google News headlines for ``query`` published in ``[start, end)``, newest first."""
    before = (end + timedelta(days=1)).strftime("%Y-%m-%d")
    params = {"q": f"{query} after:{start:%Y-%m-%d} before:{before}"}
    params.update({"hl": "vi", "gl": "VN", "ceid": "VN:vi"} if lang == "vi"
                  else {"hl": "en-US", "gl": "US", "ceid": "US:en"})
    items = _window(parse_rss(_get(GOOGLE_NEWS_RSS, params, "google_news"), "Google News"), start, end)
    items.sort(key=lambda h: h.published, reverse=True)
    return items[:limit]


def publisher_feed(url: str, source: str, start: datetime, end: datetime) -> tuple[list[Headline], str | None]:
    """A publisher's RSS items in ``[start, end)`` and a coverage note when the feed does not reach ``start``."""
    everything = parse_rss(_get(url, None, source), source)
    dated = [i.published for i in everything if i.published is not None]
    gap = date_window.coverage_gap(min(dated) if dated else None, start, source, "tin")
    return _window(everything, start, end), gap


FEEDS = {"CoinDesk": COINDESK_RSS, "Cointelegraph": COINTELEGRAPH_RSS}


# ── keyword baseline ──
POS_WORDS = ("surge", "rally", "soar", "jump", "gain", "record high", "all-time high", "bullish", "inflow",
             "approval", "approved", "upgrade", "partnership", "adoption", "breakout", "rebound",
             "tăng", "bứt phá", "kỷ lục", "tích cực", "dòng tiền vào", "phê duyệt")
NEG_WORDS = ("plunge", "crash", "drop", "fall", "slump", "bearish", "outflow", "hack", "exploit", "lawsuit",
             "sue", "ban", "delist", "liquidation", "fraud", "scam", "sell-off", "selloff", "outage",
             "giảm", "lao dốc", "sập", "bán tháo", "hack", "kiện", "cấm", "huỷ niêm yết", "hủy niêm yết",
             "thanh lý", "lừa đảo")
NEGATION_WORDS = ("not ", "no ", "without", "fails to", "không", "chưa", "chẳng")


def sentiment_score(title: str) -> float:
    """-1..1 from positive/negative keywords; a negation just before a keyword flips it. A crude baseline."""
    lowered = (title or "").lower()
    score = 0.0
    for words, sign in ((POS_WORDS, 1), (NEG_WORDS, -1)):
        for word in words:
            idx = lowered.find(word)
            if idx < 0:
                continue
            before = lowered[max(0, idx - 15):idx]
            score += -sign if any(n in before for n in NEGATION_WORDS) else sign
    return max(-1.0, min(1.0, score / 3))


# ── collection ──
def _dedupe(items: list[Headline]) -> list[Headline]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = re.sub(r"\W+", " ", item.title.lower()).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _bounds(start_date: str, end_date: str, now: datetime | None) -> tuple[datetime, datetime]:
    start_day = date_window.parse_day(start_date)
    if start_day is None:
        raise ValueError(f"start_date {start_date!r} is not a date")
    start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=UTC)
    return start, date_window.cutoff(end_date, now)


def collect_coin_news(symbol: str, start_date: str, end_date: str, limit: int = 20,
                      now: datetime | None = None, name: str | None = None
                      ) -> tuple[list[Headline], list[str], list[str]]:
    """(items about the coin or the market, newest coin items first; failed sources; coverage notes)."""
    pair = normalize_symbol(symbol)
    ticker, names = coins.aliases(pair, name)
    start, end = _bounds(start_date, end_date, now)
    terms = " OR ".join([f'"{n}"' for n in names[:2]] + [f'"{ticker}"'])
    queries = {
        "Google News (en)": lambda: google_news(f"({terms}) crypto", start, end, "en", limit),
        "Google News (vi)": lambda: google_news(f"({terms}) tiền mã hoá OR crypto", start, end, "vi", limit),
    }
    items: list[Headline] = []
    failed: list[str] = []
    gaps: list[str] = []
    for label, fetch in queries.items():
        try:
            items.extend(fetch())
        except VendorUnavailableError as exc:
            logger.warning("%s unavailable for %s: %s", label, pair, exc)
            failed.append(label)
        except Exception as exc:  # noqa: BLE001 - one feed's parse error must not sink the others
            logger.warning("%s failed for %s: %s", label, pair, exc)
            failed.append(label)
    for source, url in FEEDS.items():
        try:
            found, gap = publisher_feed(url, source, start, end)
            items.extend(found)
            if gap:
                gaps.append(gap)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s feed failed: %s", source, exc)
            failed.append(source)
    for item in items:
        item.about_coin = coins.mentions(item.title, pair, name)
    unique = _dedupe(items)
    unique.sort(key=lambda h: (h.about_coin, h.published), reverse=True)
    return unique[:limit], failed, gaps


def render(items: list[Headline], tag: bool = True) -> list[str]:
    lines = []
    for item in items:
        when = f"{item.published:%Y-%m-%d %H:%M} UTC" if item.published else "không rõ ngày"
        mark = ("✅ " if item.about_coin else "⚠️ ") if tag else ""
        lines.append(f"### {mark}{item.title}")
        lines.append(f"({item.source}, {when})")
        if item.summary:
            lines.append(item.summary)
        lines.append("")
    return lines


def get_news(symbol: str, start_date: str, end_date: str, now: datetime | None = None) -> str:
    """News about the coin (and the market around it) published in ``[start_date, cutoff(end_date))``."""
    pair = normalize_symbol(symbol)
    limit = int(get_config().get("news_article_limit", 20))
    items, failed, gaps = collect_coin_news(pair, start_date, end_date, limit, now)
    sources = 2 + len(FEEDS)
    if not items and len(failed) == sources:
        raise VendorUnavailableError(f"every news source failed for {pair}")
    cut = date_window.cutoff(end_date, now)
    lines = [
        f"## Tin tức {base_asset(pair)} từ {start_date} đến trước {cut:%Y-%m-%d %H:%M} UTC",
        "Đánh dấu: ✅ tiêu đề nhắc đúng coin này; ⚠️ tin thị trường chung, KHÔNG được gán thành sự kiện của coin.",
    ]
    if failed:
        lines.append(f"(Không truy cập được nguồn: {', '.join(failed)} - thiếu tin không có nghĩa là không có sự kiện.)")
    lines += gaps
    lines.append("")
    if not items:
        lines.append("Không có tin nào trong khoảng này từ các nguồn còn truy cập được.")
        return "\n".join(lines)
    return "\n".join(lines + render(items))


def get_global_news(as_of_date: str, look_back_days: int | None = None, limit: int | None = None,
                    now: datetime | None = None) -> str:
    """Market-wide crypto and macro headlines in the days before the run's cutoff."""
    config = get_config()
    look_back = int(look_back_days or config.get("news_lookback_days", 7))
    limit = int(limit or config.get("global_news_article_limit", 12))
    end_day = date_window.parse_day(as_of_date)
    if end_day is None:
        raise ValueError(f"as_of_date {as_of_date!r} is not a date")
    start_date = f"{end_day - timedelta(days=look_back):%Y-%m-%d}"
    start, end = _bounds(start_date, as_of_date, now)
    queries = config.get("global_news_queries") or []
    per_query = max(3, limit // max(1, len(queries)) + 1)
    items: list[Headline] = []
    failed = 0
    for query in queries:
        lang = "vi" if re.search(r"[à-ỹđ]", query, re.IGNORECASE) else "en"
        try:
            items.extend(google_news(query, start, end, lang, per_query))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Google News failed for %r: %s", query, exc)
            failed += 1
    if not items and queries and failed == len(queries):
        raise VendorUnavailableError("Google News unavailable for every market query")
    unique = _dedupe(sorted(items, key=lambda h: h.published, reverse=True))[:limit]
    lines = [f"## Tin thị trường crypto & vĩ mô từ {start_date} đến trước {end:%Y-%m-%d %H:%M} UTC", ""]
    if not unique:
        lines.append("Không có tin thị trường nào trong khoảng này.")
        return "\n".join(lines)
    return "\n".join(lines + render(unique, tag=False))
