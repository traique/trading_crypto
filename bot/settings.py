"""Settings of the always-on app, read once from the environment (and a .env file).

``load()`` also points the core library's data directory (``CRYPTO_DATA_DIR``)
at the app's, so the candle cache, the books and the reports all live under the
one writable folder a small host guarantees.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from bot.clock import VN_TZ
from cryptotrader.market import rules

logger = logging.getLogger(__name__)


def _vn_hhmm(utc_time) -> str:
    return datetime.combine(date(2026, 1, 1), utc_time, tzinfo=UTC).astimezone(VN_TZ).strftime("%H:%M")


# The daily candle closes at 00:00 UTC (07:00 Vietnam time) and is final from
# rules.FINAL_AFTER (00:05 UTC = 07:05 Vietnam time; Vietnam has no daylight
# saving). The trading run never starts before it, so it always sees the candle
# of the day before as closed.
MIN_TRADE_TIME = _vn_hhmm(rules.FINAL_AFTER)
DEFAULT_TRADE_TIME = "07:10"
TRADE_MODES = ("paper", "signal", "off")
DEFAULT_PAPER_CAPITAL = 10_000.0      # USDT
MIN_PAPER_CAPITAL = 100.0


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value == value and value not in (float("inf"), float("-inf")) else default


def _hhmm(raw: str) -> str:
    """HH:MM with two-digit hours ("7:10" -> "07:10"), so times compare as text; "" when invalid."""
    try:
        hour, minute = (int(x) for x in (raw or "").strip().split(":", 1))
    except ValueError:
        return ""
    return f"{hour:02d}:{minute:02d}" if 0 <= hour < 24 and 0 <= minute < 60 else ""


def _trade_time() -> str:
    """TRADE_TIME (Vietnam time), default 07:10, never before MIN_TRADE_TIME (07:05).

    Between 00:00 and 07:05 Vietnam time the UTC day of the last closed candle is
    still the one before, so an earlier value is moved up rather than honoured."""
    raw = (os.environ.get("TRADE_TIME") or "").strip()
    value = _hhmm(raw) if raw else DEFAULT_TRADE_TIME
    if not value:
        logger.warning("TRADE_TIME=%r is not HH:MM; using %s", raw, DEFAULT_TRADE_TIME)
        value = DEFAULT_TRADE_TIME
    if value < MIN_TRADE_TIME:
        logger.warning("TRADE_TIME %s is before the daily candle is final (%s Vietnam time); using %s",
                       value, MIN_TRADE_TIME, MIN_TRADE_TIME)
        value = MIN_TRADE_TIME
    return value


def _choice(name: str, options: tuple[str, ...], default: str) -> str:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw and raw not in options:
        logger.warning("%s=%r is not one of %s; using %s", name, raw, ", ".join(options), default)
    return raw if raw in options else default


def _ids(name: str) -> frozenset[int]:
    out = set()
    for part in (os.environ.get(name) or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return frozenset(out)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    port: int = 8080
    web_password: str = ""
    public_url: str = ""
    telegram_token: str = ""
    telegram_allowed_ids: frozenset[int] = frozenset()
    telegram_report_chat_ids: frozenset[int] = frozenset()
    keep_reports: int = 60
    max_pending: int = 10               # web/Telegram analyses allowed to wait at once
    trade_mode: str = "paper"           # paper | signal | off
    paper_capital: float = DEFAULT_PAPER_CAPITAL
    signal_strategy: str = "btc_sma_ens"  # the book whose targets the real signals follow
    trade_time: str = DEFAULT_TRADE_TIME  # HH:MM, Vietnam time, >= MIN_TRADE_TIME
    # Reverse proxies whose X-Forwarded-For is believed (uvicorn's forwarded_allow_ips):
    # comma-separated IPs/networks, or "*" when the app is reachable ONLY through a proxy.
    trusted_proxy_ips: str = "127.0.0.1"
    extra: dict = field(default_factory=dict)

    @property
    def run_time(self) -> str:
        """When the scheduler really runs the bot: ``trade_time``, never before the candle is final."""
        return max(self.trade_time, MIN_TRADE_TIME)


def load(env_file: str | Path | None = None) -> Settings:
    """The settings from the environment, after loading ``env_file`` (default: the .env
    found from the app's folder upwards). Variables already set are not overridden."""
    from dotenv import load_dotenv

    from bot.books import DEFAULT_SIGNAL_STRATEGY, book_names

    load_dotenv(env_file)
    # The core library read its environment knobs when first imported, which can be
    # before this .env was loaded: read them again so the .env values count.
    from cryptotrader.data import binance, okx

    rules.reload_costs()
    binance.reload_base_url()
    okx.reload_base_url()
    data_dir = Path(os.environ.get("CRYPTO_DATA_DIR") or os.environ.get("DATA_DIR") or "data").expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    # The core library (candle cache, exchangeInfo cache) writes under the same folder.
    os.environ["CRYPTO_DATA_DIR"] = str(data_dir)
    allowed = _ids("TELEGRAM_ALLOWED_IDS") or _ids("ALLOWED_USER_ID")
    capital = _float("PAPER_CAPITAL", DEFAULT_PAPER_CAPITAL)
    return Settings(
        data_dir=data_dir,
        port=_int("SERVER_PORT", _int("PORT", 8080)),
        web_password=os.environ.get("WEB_PASSWORD", ""),
        public_url=(os.environ.get("PUBLIC_URL") or "").strip().rstrip("/"),
        telegram_token=(os.environ.get("TELEGRAM_TOKEN") or "").strip(),
        telegram_allowed_ids=allowed,
        telegram_report_chat_ids=_ids("TELEGRAM_REPORT_CHAT_IDS") or allowed,
        keep_reports=max(1, _int("KEEP_REPORTS", 60)),
        max_pending=max(1, _int("MAX_PENDING", 10)),
        trade_mode=_choice("TRADE_MODE", TRADE_MODES, "paper"),
        paper_capital=max(capital, MIN_PAPER_CAPITAL),
        signal_strategy=_choice("SIGNAL_STRATEGY", book_names(), DEFAULT_SIGNAL_STRATEGY),
        trade_time=_trade_time(),
        trusted_proxy_ips=(os.environ.get("TRUSTED_PROXY_IPS") or os.environ.get("FORWARDED_ALLOW_IPS")
                           or "127.0.0.1").strip(),
    )
