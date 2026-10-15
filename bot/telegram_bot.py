"""Telegram bot over the Bot API with long polling (no webhook, no extra library).

Commands (Vietnamese):

    /phantich COIN [YYYY-MM-DD]   multi-agent AI analysis (minutes); the quick view
                                  arrives first, the report after
    /nhanh COIN                   the quick view only (seconds, no AI)
    /danhmuc /lenh /chay          the paper books, the next candle's orders, run now
    /khop mua|ban COIN SL GIÁ [phi=coin|usdt|bnb] [YYYY-MM-DD]  record a real fill (signal mode)
    /von SỐ_USDT                  set the real book's USDT cash (signal mode)
    /trangthai                    queue, AI providers and the bot's state
    /id                           your Telegram id (to put in TELEGRAM_ALLOWED_IDS)

Only ids in TELEGRAM_ALLOWED_IDS are served. The bot never places exchange
orders and never asks for exchange API keys.

Only new messages run commands: an edited message is ignored, so editing an
old "/khop ..." never records the fill twice. Updates already handed out are
remembered across restarts for the same reason. The bot token sits in every
Bot API URL, so it is scrubbed from this module's log records and replies.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import requests

from bot import fmt
from bot.analysis import fallback_message, gate_line, provider_status, unavailable_reason
from bot.checks import check_request
from bot.jobs import Job, JobQueue
from bot.service import quick_text
from bot.settings import Settings
from cryptotrader.data.files import write_text_atomic

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE = 3900
COMMAND_LANES = 3          # commands run on this many worker threads at most
MAX_QUEUED_COMMANDS = 50   # beyond this a burst is dropped, not queued
# Messages from ids not in TELEGRAM_ALLOWED_IDS never take one of those slots: at
# most this many are queued at once, and each chat gets one reply per window.
MAX_STRANGER_COMMANDS = 3
STRANGER_REPLY_SECONDS = 300
SEEN_KEPT = 1000           # update ids remembered across restarts
# A request that did not get through (network error, Bot API 5xx) is tried again
# after these pauses (seconds): a blip at the daily broadcast must not lose it.
NETWORK_RETRY_DELAYS = (2.0, 5.0)
# Broadcast messages that still did not get through wait in a small persisted
# outbox, retried by the polling loop at most this often, for at most a day.
OUTBOX_RETRY_SECONDS = 60
OUTBOX_MAX_AGE_SECONDS = 24 * 3600
OUTBOX_KEPT = 30
# A Bot API token: "<bot id>:<35-ish url-safe characters>".
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])\d{5,}:[A-Za-z0-9_-]{30,}")
_SECRETS: set[str] = set()


def redact(text) -> str:
    """``text`` without any Telegram bot token (requests puts the URL in its errors)."""
    text = str(text)
    for secret in _SECRETS:
        text = text.replace(secret, "<TOKEN>")
    return _TOKEN_RE.sub("<TOKEN>", text)


class _RedactFilter(logging.Filter):
    """Scrubs the token from a record's message and traceback before any handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg, record.args = redact(record.getMessage()), ()
        except Exception:  # noqa: BLE001 - a bad format string still gets logged
            record.msg = redact(record.msg)
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


_REDACT = _RedactFilter()


def _install_redaction() -> None:
    # This module's records, and urllib3's request lines ("POST /bot<token>/...") at DEBUG.
    for name in (__name__, "urllib3.connectionpool"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _RedactFilter) for f in target.filters):
            target.addFilter(_REDACT)


_install_redaction()


class TelegramNetworkError(requests.RequestException):
    """A Bot API request that did not get through, with the token removed from the message."""


_MD_NOISE = re.compile(r"(\*\*|__|^#+\s*)", re.MULTILINE)

HELP = (
    "Crypto bot - Binance spot, cặp USDT. Bot KHÔNG đặt lệnh lên sàn và không cần API key sàn.\n\n"
    "/nhanh BTC - xem nhanh: giá, xu hướng, coin có trong rổ bot không (vài giây, không dùng AI)\n"
    "/phantich BTC - phân tích bằng các agent AI (vài phút)\n"
    "/phantich BTC 2026-10-01 - phân tích tại một ngày trong quá khứ (ngày UTC)\n"
    "/danhmuc - các tài khoản ảo (và tài khoản thật ở chế độ tín hiệu)\n"
    "/lenh - lệnh bot đặt cho nến tới\n"
    "/chay - chạy ngay các nến đã đóng chưa xử lý (không đợi {trade_time})\n"
    "/khop mua BTC 0.0123 65000 - báo đã khớp lệnh thật (bán: /khop ban ...; "
    "thêm ngày UTC YYYY-MM-DD nếu khớp hôm trước). Khối lượng là khối lượng khớp của lệnh (như trong "
    "lịch sử lệnh), giá tính bằng USDT, dùng dấu chấm cho phần thập phân. Phí lệnh mua mặc định trừ vào coin "
    "(Binance không dùng BNB); trả phí bằng BNB thì thêm phi=bnb\n"
    "/von 1000 - đặt số USDT của tài khoản thật\n"
    "/futures - sổ futures long/short (ảo), vị thế, tiến độ mục tiêu; /futures chay - chạy ngay; "
    "/futures tieptuc ETH - chạy lại sau khi dừng\n"
    "/fvon 10 - số dư ví futures thật (chế độ FUTURES_MODE=signal); /fvon 10 moi - đặt lại vốn gốc\n"
    "/trangthai - hàng đợi, nhà cung cấp AI và trạng thái bot\n"
    "/id - Telegram id của bạn\n\n"
    "Nến ngày đóng lúc 00:00 UTC (07:00 giờ VN); bot chạy lúc {trade_time} giờ VN mỗi ngày, kể cả cuối tuần. "
    "Kết quả tài khoản ảo đã trừ phí 0.1% và trượt giá giả định; không phải khuyến nghị đầu tư."
)

_SIDES = {"mua": "BUY", "buy": "BUY", "ban": "SELL", "bán": "SELL", "sell": "SELL"}


def plain(text: str) -> str:
    """Markdown from the reports as plain text, so Telegram shows it without parse errors."""
    return _MD_NOISE.sub("", text or "").strip()


def chunks(text: str, size: int = MAX_MESSAGE) -> list[str]:
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            parts.append(current + line[: size - len(current)])
            line, current = line[size - len(current):], ""
        if len(current) + len(line) > size:
            parts.append(current)
            current = ""
        current += line
    if current.strip():
        parts.append(current)
    return parts or [""]


class TelegramBot:
    def __init__(self, settings: Settings, jobs: JobQueue):
        self.settings, self.jobs = settings, jobs
        if len(settings.telegram_token or "") >= 10:
            _SECRETS.add(settings.telegram_token)
        self._session = requests.Session()
        self._thread = threading.Thread(target=self._poll, name="telegram-bot", daemon=True)
        self.trader = None   # bot.trader.TradingBot, set by main_botkeep
        self.futures = None  # bot.futures_bot.FuturesTrader, set by main_botkeep
        # Commands of one chat run in order on one lane; a burst waits instead of
        # spawning a thread per message (the host is small).
        self._lanes = [ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"telegram-cmd-{i}")
                       for i in range(COMMAND_LANES)]
        self._queued = 0
        self._queued_lock = threading.Lock()
        self._strangers_queued = 0
        self._stranger_replied: dict = {}      # chat id -> time.monotonic() of the last reply
        self._seen_path = settings.data_dir / "telegram_seen.json"
        self._seen: deque[int] = deque(self._load_seen(), maxlen=SEEN_KEPT)
        self._outbox_lock = threading.Lock()
        self._outbox_tried = 0.0
        jobs.add_listener(self.on_job_event)

    def _help(self) -> str:
        return HELP.format(trade_time=self.settings.run_time)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_token)

    def start(self) -> None:
        if self.enabled:
            self._thread.start()
        else:
            logger.info("TELEGRAM_TOKEN not set; the Telegram bot is off")

    # ── Bot API ──
    def _call(self, method: str, http_timeout: float = 30, **params):
        url = API.format(token=self.settings.telegram_token, method=method)
        files = params.pop("files", None)
        # getUpdates is retried by the polling loop itself; everything else here.
        delays = list(NETWORK_RETRY_DELAYS) if method != "getUpdates" else []
        flood = 0
        while True:
            if files:
                for value in files.values():   # re-send the file from its start
                    handle = value[1] if isinstance(value, tuple) else value
                    if hasattr(handle, "seek"):
                        handle.seek(0)
            try:
                response = self._session.post(url, data=params, files=files, timeout=http_timeout)
            except requests.RequestException as exc:
                if delays:
                    logger.warning("Telegram %s did not get through (%s); retrying", method, redact(exc))
                    time.sleep(delays.pop(0))
                    continue
                # ``from None``: the original carries the URL, so the token, in its text.
                raise TelegramNetworkError(f"Telegram {method}: {redact(exc)}") from None
            status = getattr(response, "status_code", 200)
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(status, int) and status >= 500 and not (isinstance(payload, dict) and payload.get("ok")):
                if delays:
                    logger.warning("Telegram %s: HTTP %s; retrying", method, status)
                    time.sleep(delays.pop(0))
                    continue
                raise TelegramNetworkError(f"Telegram {method}: HTTP {status}")
            if not isinstance(payload, dict):
                raise RuntimeError(f"Telegram {method}: HTTP {status}")
            if payload.get("ok"):
                return payload.get("result")
            retry_after = (payload.get("parameters") or {}).get("retry_after")
            if payload.get("error_code") == 429 and retry_after and flood < 2:
                # Flood control: a long report is several messages in a row.
                flood += 1
                time.sleep(min(float(retry_after), 60) + 0.5)
                continue
            if payload.get("error_code") == 429:
                raise RuntimeError(f"Telegram {method}: still rate limited")
            raise RuntimeError(f"Telegram {method}: {payload.get('description')}")

    def send(self, chat_id: int, text: str) -> list[str]:
        """Send ``text`` (in parts when long); returns the parts that did not get through."""
        failed = []
        for part in chunks(redact(text)):
            if not self._send_part(chat_id, part):
                failed.append(part)
        return failed

    def _send_part(self, chat_id: int, part: str) -> bool:
        try:
            self._call("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview="true")
            return True
        except Exception:  # noqa: BLE001 - delivery failures are logged, not fatal
            logger.exception("Telegram sendMessage to %s failed", chat_id)
            return False

    def broadcast(self, text: str) -> None:
        """Send to the report chats (the daily bot report). What does not get through
        is kept in the outbox and sent again later (the report is not produced twice)."""
        self.report(text)

    def report(self, text: str, also=None) -> None:
        """Send a report to the report chats plus ``also`` (the chat that asked), each once."""
        if not self.enabled:
            return
        targets = set(self.settings.telegram_report_chat_ids)
        if also is not None:
            targets.add(also)
        for chat_id in sorted(targets, key=str):
            failed = self.send(chat_id, text)
            if failed:
                self._queue_outbox(chat_id, failed)

    # ── outbox: broadcast parts that did not get through ──
    @property
    def _outbox_path(self):
        return self.settings.data_dir / "telegram_outbox.json"

    def _read_outbox(self) -> list[dict]:
        try:
            data = json.loads(self._outbox_path.read_text(encoding="utf-8"))
            return [e for e in data if isinstance(e, dict) and e.get("text")] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _write_outbox(self, entries: list[dict]) -> None:
        try:
            if entries:
                write_text_atomic(self._outbox_path, json.dumps(entries[-OUTBOX_KEPT:], ensure_ascii=False))
            else:
                self._outbox_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Telegram: cannot save the outbox: %s", exc)

    def _queue_outbox(self, chat_id, parts: list[str]) -> None:
        with self._outbox_lock:
            entries = self._read_outbox()
            entries += [{"chat_id": chat_id, "text": p, "at": time.time()} for p in parts]
            self._write_outbox(entries)
        logger.warning("Telegram: %d message part(s) to %s kept for a later retry", len(parts), chat_id)

    def flush_outbox(self) -> int:
        """Send what the outbox holds, in order; returns how many parts got through."""
        with self._outbox_lock:
            entries = self._read_outbox()
            if not entries:
                return 0
            now, left, sent = time.time(), [], 0
            for entry in entries:
                if now - float(entry.get("at") or 0) > OUTBOX_MAX_AGE_SECONDS:
                    logger.warning("Telegram: undelivered message to %s dropped after a day", entry.get("chat_id"))
                    continue
                if left and left[-1].get("chat_id") == entry.get("chat_id"):
                    left.append(entry)          # keep a chat's parts in order
                elif self._send_part(entry.get("chat_id"), str(entry["text"])):
                    sent += 1
                else:
                    left.append(entry)
            self._write_outbox(left)
            return sent

    def _maybe_flush_outbox(self) -> None:
        now = time.monotonic()
        if now - self._outbox_tried < OUTBOX_RETRY_SECONDS:
            return
        self._outbox_tried = now
        try:
            self.flush_outbox()
        except Exception:  # noqa: BLE001 - the polling loop must keep going
            logger.exception("Telegram: outbox retry failed")

    def send_file(self, chat_id: int, path, caption: str = "") -> None:
        try:
            with open(path, "rb") as handle:
                self._call("sendDocument", http_timeout=60, chat_id=chat_id, caption=caption[:1000],
                           files={"document": (path.name, handle, "text/html")})
        except Exception:  # noqa: BLE001
            logger.exception("Telegram sendDocument to %s failed", chat_id)

    # ── polling ──
    def _load_seen(self) -> list[int]:
        try:
            return [int(x) for x in json.loads(self._seen_path.read_text(encoding="utf-8"))][-SEEN_KEPT:]
        except (OSError, ValueError, TypeError):
            return []

    def _save_seen(self) -> None:
        try:
            write_text_atomic(self._seen_path, json.dumps(list(self._seen)))
        except OSError as exc:
            logger.warning("Telegram: cannot save handled update ids: %s", exc)

    def _accept(self, updates) -> tuple[int | None, list[dict]]:
        """The next offset and the new messages of a getUpdates batch.

        Every update id is marked handled (and saved) before any command runs:
        a crash or restart mid-batch must not run "/khop" or "/von" twice when
        Telegram hands the unconfirmed batch out again.
        """
        offset, messages = None, []
        seen = set(self._seen)
        for update in updates or []:
            try:
                update_id = int(update["update_id"])
            except (KeyError, TypeError, ValueError):
                logger.warning("Telegram: update without an id skipped")
                continue
            offset = max(offset or 0, update_id + 1)
            if update_id in seen:
                continue
            seen.add(update_id)
            self._seen.append(update_id)
            message = update.get("message")
            if message is None:
                # An edited message is not a new command (editing "/khop" would
                # record the fill again); channel posts and the rest are ignored.
                logger.info("Telegram: update %s ignored (%s)", update_id,
                            ",".join(k for k in update if k != "update_id"))
                continue
            if isinstance(message, dict) and message.get("text") and isinstance(message.get("chat"), dict):
                messages.append(message)
        if offset is not None:
            self._save_seen()
        return offset, messages

    def _dispatch(self, message: dict) -> None:
        """Queue a command on its chat's lane, bounded so a burst cannot pile up threads.

        Only the allowed ids share the command budget: a stranger flooding a public
        bot must not make it drop the owner's "/khop" or "/von"."""
        chat_id = message["chat"].get("id")
        user_id = (message.get("from") or {}).get("id")
        if user_id not in self.settings.telegram_allowed_ids:
            self._dispatch_stranger(message, chat_id)
            return
        with self._queued_lock:
            full = self._queued >= MAX_QUEUED_COMMANDS
            if not full:
                self._queued += 1
        lane = self._lanes[hash(chat_id) % len(self._lanes)]
        if full:
            logger.warning("Telegram: %d commands waiting; message from chat %s dropped", self._queued, chat_id)
            # The owner is told, so a dropped "/khop" is not mistaken for a recorded one.
            lane.submit(self.send, chat_id, f"⚠️ Bot đang bận ({MAX_QUEUED_COMMANDS} lệnh chờ): chưa thực hiện "
                                            f"\"{message['text'][:60]}\". Gửi lại sau ít phút.")
            return
        try:
            lane.submit(self._run, message)
        except Exception:
            with self._queued_lock:
                self._queued -= 1
            raise

    def _dispatch_stranger(self, message: dict, chat_id) -> None:
        """A message from an id not allowed: at most one reply per chat per window (the
        /id answer or the refusal), on a small budget of its own; the rest is ignored."""
        now = time.monotonic()
        with self._queued_lock:
            last = self._stranger_replied.get(chat_id)
            if (last is not None and now - last < STRANGER_REPLY_SECONDS) \
                    or self._strangers_queued >= MAX_STRANGER_COMMANDS:
                logger.info("Telegram: message from unauthorised chat %s ignored", chat_id)
                return
            if len(self._stranger_replied) > 1000:
                self._stranger_replied = {k: t for k, t in self._stranger_replied.items()
                                          if now - t < STRANGER_REPLY_SECONDS}
            self._stranger_replied[chat_id] = now
            self._strangers_queued += 1
        lane = self._lanes[hash(chat_id) % len(self._lanes)]
        try:
            lane.submit(self._run, message, stranger=True)
        except Exception:
            with self._queued_lock:
                self._strangers_queued -= 1
            raise

    def _run(self, message: dict, stranger: bool = False) -> None:
        try:
            self._handle(message)
        except Exception:  # noqa: BLE001 - one bad message must not stop its lane
            logger.exception("Telegram message handling failed")
        finally:
            with self._queued_lock:
                if stranger:
                    self._strangers_queued -= 1
                else:
                    self._queued -= 1

    def _poll(self) -> None:
        offset = None
        while True:
            try:
                # Edited messages are not asked for: only new messages run commands.
                params = {"timeout": 50, "allowed_updates": '["message"]'}
                if offset is not None:
                    params["offset"] = offset
                updates = self._call("getUpdates", http_timeout=70, **params)
                next_offset, messages = self._accept(updates)
                if next_offset is not None:
                    # Advanced before any command runs: whatever happens below,
                    # the next getUpdates confirms this batch and never repeats it.
                    offset = next_offset
                for message in messages:
                    try:
                        self._dispatch(message)
                    except Exception:  # noqa: BLE001
                        logger.exception("Telegram: cannot queue a message")
                self._maybe_flush_outbox()
            except requests.RequestException as exc:
                logger.warning("Telegram polling error: %s", exc)
                time.sleep(5)
            except RuntimeError as exc:
                text = str(exc)
                if "webhook is active" in text:
                    # The token still has a webhook from another service or an
                    # earlier deploy; long polling only works once it is removed.
                    self._drop_webhook()
                    time.sleep(1)
                elif "terminated by other getUpdates" in text:
                    logger.warning("Telegram: another process is polling with this bot token. "
                                   "Stop the other copy (another server or a local run).")
                    time.sleep(30)
                else:
                    logger.warning("Telegram polling failed: %s", text)
                    time.sleep(10)
            except Exception:  # noqa: BLE001
                logger.exception("Telegram polling failed")
                time.sleep(10)

    def _drop_webhook(self) -> None:
        try:
            self._call("deleteWebhook", drop_pending_updates="false")
            logger.warning("Telegram: removed the webhook set on this bot token, switching to polling.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram deleteWebhook failed: %s", exc)
            time.sleep(30)

    # ── commands ──
    def _handle(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        user_id = (message.get("from") or {}).get("id")
        text = message["text"].strip()
        command, _, rest = text.partition(" ")
        command = command.split("@")[0].lower()
        args = rest.strip()

        if command == "/id":
            self.send(chat_id, f"Telegram id của bạn: {user_id}")
            return
        if user_id not in self.settings.telegram_allowed_ids:
            self.send(chat_id, f"Bot chưa cho phép tài khoản này. Thêm {user_id} vào TELEGRAM_ALLOWED_IDS.")
            return
        try:
            if command in ("/start", "/help", "/trogiup"):
                self.send(chat_id, self._help())
            elif command == "/phantich":
                self._analyze(chat_id, args)
            elif command == "/nhanh":
                parts = args.split()
                if not parts:
                    raise ValueError("Nhập tên coin, ví dụ: /nhanh BTC")
                self.send(chat_id, quick_text(parts[0], parts[1] if len(parts) > 1 else None, trader=self.trader))
            elif command == "/trangthai":
                self.send(chat_id, self._status())
            elif command in ("/danhmuc", "/lenh", "/khop", "/von", "/chay"):
                self._trading(chat_id, command, args)
            elif command in ("/futures", "/fvon"):
                self._futures(chat_id, command, args)
            else:
                self.send(chat_id, self._help())
        except ValueError as exc:
            self.send(chat_id, f"⚠️ {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Command %s failed", command)
            self.send(chat_id, f"⚠️ Lỗi: {redact(exc)}")

    def _analyze(self, chat_id: int, args: str) -> None:
        parts = args.split()
        if not parts:
            raise ValueError("Nhập tên coin, ví dụ: /phantich BTC")
        pair, day = check_request(parts[0], parts[1] if len(parts) > 1 else None)
        reason = unavailable_reason()
        if reason:
            # No queue slot for an analysis that cannot run: the quick view and why.
            try:
                quick = quick_text(pair, day, trader=self.trader)
            except Exception as exc:  # noqa: BLE001
                quick = f"(không tính được số liệu nhanh: {redact(exc)})"
            self.send(chat_id, f"{quick}\n\n{fallback_message(pair, reason)}")
            return
        job = self.jobs.submit(pair, day, "telegram", chat_id)
        if job.status != "queued":
            self.send(chat_id, f"⏳ {pair} ngày {day} đang được phân tích; bạn sẽ nhận kết quả khi xong.")
            if job.quick_summary:
                self.send(chat_id, job.quick_summary)
            return
        ahead = self.jobs.position(job.id)
        wait = f" (đang có {ahead} lượt chờ trước)" if ahead else ""
        self.send(chat_id, f"⏳ Đã nhận {pair} ngày {day}{wait}. Số liệu nhanh tới trước, "
                           "báo cáo của các agent AI tới sau vài phút.")

    def _trading(self, chat_id: int, command: str, args: str) -> None:
        if self.trader is None or self.settings.trade_mode == "off":
            raise ValueError("Bot giao dịch đang tắt (TRADE_MODE=off).")
        if command == "/chay":
            # The run's report goes to the asker AND the report chats: the scheduled run
            # later finds the day done and would not broadcast it again.
            self.send(chat_id, self.trader.start_run(notify=lambda text: self.send(chat_id, text),
                                                     report=lambda text: self.report(text, also=chat_id)))
        elif command == "/danhmuc":
            self.send(chat_id, self.trader.portfolio_text())
        elif command == "/lenh":
            self.send(chat_id, self.trader.orders_text())
        elif command == "/von":
            if self.settings.trade_mode != "signal":
                raise ValueError("/von chỉ dùng ở chế độ tín hiệu (TRADE_MODE=signal).")
            parts = args.split()
            if len(parts) != 1:
                raise ValueError("Dùng: /von 1000 (số USDT trên sàn)")
            self.send(chat_id, self.trader.set_capital(fmt.parse_number(parts[0], "Số vốn")))
        else:
            if self.settings.trade_mode != "signal":
                raise ValueError("/khop chỉ dùng ở chế độ tín hiệu (TRADE_MODE=signal).")
            parts = args.split()
            usage = "Dùng: /khop mua BTC 0.0123 65000 [phi=coin|usdt|bnb] [YYYY-MM-DD] (hoặc /khop ban ...)"
            if len(parts) not in (4, 5, 6):
                raise ValueError(usage)
            side = _SIDES.get(parts[0].lower())
            if side is None:
                raise ValueError("Chiều lệnh là mua hoặc ban.")
            qty = fmt.parse_number(parts[2], "Khối lượng")
            price = fmt.parse_number(parts[3], "Giá")
            day = fee_in = None
            for extra in parts[4:]:
                key, eq, value = extra.lower().partition("=")
                if eq and key in ("phi", "phí", "fee") and fee_in is None:
                    fee_in = value
                elif not eq and day is None:
                    day = extra
                else:
                    raise ValueError(usage)
            kwargs = {"fee_in": fee_in} if fee_in is not None else {}
            self.send(chat_id, self.trader.record_fill(side, parts[1], qty, price, day, **kwargs))

    def _futures(self, chat_id: int, command: str, args: str) -> None:
        if self.futures is None or self.settings.futures_mode == "off":
            raise ValueError("Bot futures đang tắt (FUTURES_MODE=off).")
        parts = args.split()
        if command == "/fvon":
            if self.settings.futures_mode != "signal":
                raise ValueError("/fvon chỉ dùng ở chế độ tín hiệu (FUTURES_MODE=signal).")
            if len(parts) not in (1, 2) or (len(parts) == 2 and parts[1].lower() not in ("moi", "mới")):
                raise ValueError("Dùng: /fvon 10 (số dư ví futures, USDT); /fvon 10 moi để đặt lại vốn gốc")
            equity = fmt.parse_number(parts[0], "Số vốn")
            self.send(chat_id, self.futures.reset_real_start(equity) if len(parts) == 2
                      else self.futures.set_real_equity(equity))
            return
        sub = parts[0].lower() if parts else ""
        if not sub:
            self.send(chat_id, self.futures.status_text())
        elif sub in ("chay", "chạy"):
            self.send(chat_id, self.futures.start_run(notify=lambda text: self.send(chat_id, text),
                                                      report=lambda text: self.report(text, also=chat_id)))
        elif sub in ("tieptuc", "tiếptục") and len(parts) == 2:
            self.send(chat_id, self.futures.resume(parts[1]))
        else:
            raise ValueError("Dùng: /futures, /futures chay, /futures tieptuc ETH")

    def _status(self) -> str:
        lines = ["Hàng đợi phân tích:"]
        recent = self.jobs.recent(8)
        lines += [f"- {job.ticker} {job.date}: {job.status} {job.action}".rstrip() for job in recent] or ["- (trống)"]
        lines.append("\nNhà cung cấp AI (theo thứ tự):")
        rows = provider_status()
        for row in rows:
            state = f"nghỉ {row['resting_seconds']}s ({row['last_error']})" if row.get("resting_seconds") else "sẵn sàng"
            model = f" [{row['model']}]" if row.get("model") else ""
            lines.append(f"- {row['label']}{model}: {state}")
        if not rows:
            lines.append("- chưa cấu hình (chỉ dùng được /nhanh)")
        gate = gate_line()
        if gate:
            lines.append(gate)
        if self.trader is not None and self.settings.trade_mode != "off":
            lines.append("\n" + self.trader.next_run_text())
        if self.futures is not None and self.settings.futures_mode != "off":
            lines.append(self.futures.next_run_text())
        return "\n".join(lines)

    # ── job events ──
    def on_job_event(self, job: Job, event: str) -> None:
        if not self.enabled:
            return
        for chat_id in list(job.chat_ids):
            if event == "quick":
                self.send(chat_id, job.quick_summary)
            elif event == "done" and job.note:
                self.send(chat_id, job.note)
            elif event == "done":
                link = f"\n\nXem báo cáo: {self.settings.public_url}/reports/{job.id}" if self.settings.public_url else ""
                notes = f"\n\n⚠️ Kiểm tra đầu ra: {job.guard_notes}" if job.guard_notes else ""
                self.send(chat_id, f"✅ {job.ticker} {job.date} - {job.action or 'xong'}\n\n"
                                   f"{plain(job.final_decision)}{notes}{link}")
                if job.html_path:
                    self.send_file(chat_id, job.html_path, caption=f"Báo cáo đầy đủ {job.ticker} {job.date}")
            elif event == "failed":
                self.send(chat_id, f"❌ {job.ticker} {job.date}: {redact(job.error)}")
