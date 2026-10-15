"""Entry point for Botkeep (or any always-on host): web page + Telegram bot + daily trading run.

    python main_botkeep.py

Reads its settings from the environment or a .env file beside it (see
deploy/botkeep/.env.example). Serves the web page on SERVER_PORT (Botkeep sets
it) or PORT, polls Telegram when TELEGRAM_TOKEN is set, and runs the trading
bot every day at TRADE_TIME (Vietnam time, default 07:10; never before 07:05 =
00:05 UTC, when the daily candle that closed at 00:00 UTC is final). The bot
never places exchange orders and never asks for exchange API keys.
"""

import logging
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()   # before any import reads the environment (costs, data source URLs)

from bot import settings as settings_module  # noqa: E402

SETTINGS = settings_module.load()   # first: points the core library's data dir at the app's

import uvicorn  # noqa: E402

from bot.analysis import configured_order, unavailable_reason  # noqa: E402
from bot.clock import VN_TZ  # noqa: E402
from bot.jobs import JobQueue  # noqa: E402
from bot.service import DailyScheduler  # noqa: E402
from bot.telegram_bot import TelegramBot  # noqa: E402
from bot.trader import TradingBot  # noqa: E402
from bot.web import create_app  # noqa: E402


def _vn_time(seconds):
    """Log timestamps in Vietnam time, whatever the host's timezone (often UTC)."""
    return datetime.fromtimestamp(seconds, VN_TZ).timetuple()


def main() -> None:
    logging.Formatter.converter = staticmethod(_vn_time)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s+07 %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    order = configured_order()
    if order:
        logging.info("AI provider chain: %s", " -> ".join(order))
    reason = unavailable_reason()
    if reason:
        logging.warning("AI analysis (/phantich) unavailable: %s. /nhanh and the trading bot still work.", reason)
    if not SETTINGS.web_password:
        logging.warning("WEB_PASSWORD is not set: the web page stays locked until it is (Telegram still works)")
    if SETTINGS.telegram_token and not SETTINGS.telegram_allowed_ids:
        logging.warning("TELEGRAM_ALLOWED_IDS is empty: the bot only answers /id until you add your id")

    trader = TradingBot(SETTINGS) if SETTINGS.trade_mode != "off" else None
    jobs = JobQueue(SETTINGS, trader=trader)
    bot = TelegramBot(SETTINGS, jobs)
    if trader is not None:
        trader.notify = bot.broadcast
    bot.trader = trader
    jobs.start()
    bot.start()
    DailyScheduler(SETTINGS, bot=trader, notify=bot.broadcast).start()
    logging.info("Trading bot: mode %s, books %s, signals follow %s, runs daily at %s (Vietnam time)",
                 SETTINGS.trade_mode, ", ".join(s.name for s in trader.specs) if trader else "-",
                 SETTINGS.signal_strategy, SETTINGS.run_time)

    app = create_app(SETTINGS, jobs, trader=trader)
    # Behind a reverse proxy (Botkeep) the socket peer is the proxy: the login lockout
    # needs the real client, which uvicorn takes from X-Forwarded-For only for the
    # proxies listed in TRUSTED_PROXY_IPS.
    uvicorn.run(app, host="0.0.0.0", port=SETTINGS.port, log_level="warning", access_log=False,
                proxy_headers=True, forwarded_allow_ips=SETTINGS.trusted_proxy_ips)


if __name__ == "__main__":
    main()
