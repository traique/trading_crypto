"""The trading bot: paper books driven by strategies, and signals for a real account.

Each strategy of ``bot.books.BOOKS`` trades its own paper book in USDT. After
every daily candle closes (00:00 UTC, final from 00:05 UTC), ``catch_up``:

1. Replays, in order, every closed UTC day after each book's last processed day
   (``meta["last_day"]``), each with the candles up to that day only (no
   look-ahead): the orders placed on the day before fill at this day's open or
   lapse, stops and targets are checked on its candle (ledger rules: taker fee,
   slippage, step size, minimum notional), and the NAV is recorded beside BTC.
   A day whose candle is not final is never processed.
2. Asks the book's strategy for its decision on that day (target weights, or
   orders) and places the orders for the next candle. Each book is saved after
   each day, so a run that fails half-way is resumed, not replayed.
3. In ``signal`` mode, turns the ``SIGNAL_STRATEGY`` book's latest targets into
   suggested orders sized on the real book (USDT and coin quantities floored to
   the pair's step size, orders under the minimum notional left out), and flags
   stop hits on real holdings. The real book changes only when its owner
   reports a fill (/khop) or the cash (/von); the bot never writes it during a
   run, never places an exchange order and never asks for exchange API keys.

One run at a time (the scheduler, /chay and the web button share one lock); a
report of a fill takes another lock, so it never waits for a whole run.
"""

from __future__ import annotations

import bisect
import copy
import json
import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

from bot import clock, fmt
from bot.books import BookSpec, active_books, get_book, title_of
from bot.settings import Settings
from cryptotrader.data.files import write_text_atomic
from cryptotrader.market import rules

logger = logging.getLogger(__name__)

# Days replayed after downtime. Older missed days are skipped (their orders
# lapse) and the report says so: a long replay would trade on stale decisions.
MAX_CATCH_UP = 30

# A day is not saved when the price sources did not answer for a held pair or for
# more than this share of a strategy's pairs: it is retried later instead.
MAX_UNAVAILABLE_SHARE = 0.2

# A fill reported at a price this far from the pair's last close is refused as a
# typo (65000 typed 6500): a factor of 5 either way.
FILL_PRICE_BAND = 5.0

# Where the exchange took a reported fill's fee (/khop ... phi=...).
FEE_ASSETS = ("coin", "usdt", "bnb")


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _book_day(book) -> str:
    """The last day a book has processed ("" for a new book)."""
    day = book.meta.get("last_day")
    if day:
        return str(day)
    return str(book.history[-1]["date"]) if book.history else ""


def _pending(days: list[str], last: str) -> tuple[list[str], int]:
    """The days a book at ``last`` still has to process, and how many are skipped (cap)."""
    if not last:
        return days[-1:], 0              # a new book starts at the latest closed day, no backfill
    after = [d for d in days if d > last]
    skipped = max(0, len(after) - MAX_CATCH_UP)
    return after[skipped:], skipped


def _expire(book, prev: str | None) -> list:
    """Drop the orders whose day has passed without being processed.

    An order created on D is for the candle of D+1. Before processing day S
    (previous day ``prev``), an order created before ``prev`` was meant for a day
    up to ``prev``: it must lapse rather than fill at S's prices.
    """
    if not prev:
        return []
    stale = [o for o in book.orders if o.created < prev]
    if stale:
        book.orders = [o for o in book.orders if o.created >= prev]
    return stale


def _unavailable(exc: BaseException | None) -> bool:
    """Whether ``exc`` (or what caused it) is a price source that did not answer."""
    from cryptotrader.data.errors import NoMarketDataError, VendorError

    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, VendorError) and not isinstance(exc, NoMarketDataError):
            return True
        exc, seen = exc.__cause__ or exc.__context__, seen + 1
    return False


def _weights_text(weights: dict | None) -> str:
    if not weights:
        return "100% USDT"
    parts = [f"{rules.split_pair(s)[0]} {w * 100:.0f}%" for s, w in sorted(weights.items(), key=lambda kv: -kv[1])]
    rest = 1 - sum(weights.values())
    if rest > 0.005:
        parts.append(f"USDT {rest * 100:.0f}%")
    return ", ".join(parts)


def _min_trade_pct(strategy) -> float:
    """The strategy's smallest trade (% of NAV), as the research harness plans it; default 1%."""
    value = getattr(strategy, "min_trade_pct", 1.0)
    return float(value) if _finite(value) and float(value) >= 0 else 1.0


def _explain(strategy, book) -> str:
    """The strategy's one-line reading of its last signal (``explain(state)``), or ""."""
    explain = getattr(strategy, "explain", None)
    if not callable(explain):
        return ""
    try:
        return str(explain(book.meta.get("strategy_state") or {}) or "")
    except Exception:  # noqa: BLE001 - a report line must not fail the run
        logger.exception("explain() failed")
        return ""


def _side_vi(side: str) -> str:
    return "MUA" if side == "BUY" else "BÁN"


def _base(symbol: str) -> str:
    return rules.split_pair(symbol)[0]


@dataclass
class RunResult:
    day: str | None              # the last closed day the books are at
    text: str                    # the report (the previous one when nothing was new)
    new: bool = False            # whether this run processed at least one day
    days: list[str] = field(default_factory=list)   # processed by this run, in order


class _Market:
    """Daily candles for one run, read once per pair up to ``as_of`` and sliced per day."""

    def __init__(self, as_of: str):
        self.as_of = as_of
        self._frames: dict[str, tuple | Exception | None] = {}

    def _entry(self, symbol: str):
        if symbol not in self._frames:
            from cryptotrader.data import ohlcv
            from cryptotrader.data.errors import NoMarketDataError, VendorError

            try:
                frame = ohlcv.load_ohlcv(symbol, self.as_of, now=clock.now_utc())
                dates = [d.strftime("%Y-%m-%d") for d in frame["Date"]]
                self._frames[symbol] = (frame.reset_index(drop=True), dates)
            except (NoMarketDataError, ValueError) as exc:   # unknown, delisted or stale pair
                logger.warning("Bot: no candles for %s up to %s: %s", symbol, self.as_of, exc)
                self._frames[symbol] = None
            except VendorError as exc:          # the source did not answer: not "no data"
                logger.warning("Bot: candles for %s unavailable: %s", symbol, exc)
                self._frames[symbol] = exc
        return self._frames[symbol]

    def frame(self, symbol: str, day: str, *, required: bool = False):
        """Candles up to and including ``day``, or None. ``required``: a source that did
        not answer raises (the run is retried) instead of passing for "no data"."""
        entry = self._entry(symbol)
        if isinstance(entry, Exception):
            if required:
                raise entry
            return None
        if entry is None:
            return None
        frame, dates = entry
        return frame.iloc[: bisect.bisect_right(dates, day)]

    def days(self, symbol: str) -> list[str]:
        entry = self._entry(symbol)
        if isinstance(entry, Exception):
            raise entry
        return list(entry[1]) if entry else []

    def bar(self, symbol: str, day: str, *, required: bool = False):
        """The ledger Bar of ``symbol`` on ``day``; None when it has no candle that day."""
        from cryptotrader.trading.ledger import Bar

        frame = self.frame(symbol, day, required=required)
        if frame is None or not len(frame) or frame["Date"].iloc[-1].strftime("%Y-%m-%d") != day:
            return None
        row = frame.iloc[-1]
        volume = row.get("Volume")
        volume = float(volume) if _finite(volume) else None
        # NaN prices stay NaN: Bar.tradable then treats the day as one without trading.
        return Bar(day, float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"]), volume)

    def close(self, symbol: str, day: str) -> float | None:
        frame = self.frame(symbol, day)
        if frame is None or not len(frame):
            return None
        value = float(frame["Close"].iloc[-1])
        return value if _finite(value) and value > 0 else None


class TradingBot:
    def __init__(self, settings: Settings, notify=None):
        self.settings = settings
        self.notify = notify or (lambda text: None)
        self.dir = settings.data_dir / "trading"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.specs: list[BookSpec] = active_books()
        # One run at a time over the paper books and their state files (scheduler,
        # /chay and the web button may all ask at once).
        self._lock = threading.RLock()
        # The real book: written only by /khop and /von, read by the runs. Separate,
        # so a report of a fill is not kept waiting for a whole run.
        self._real_lock = threading.Lock()
        self._start_guard = threading.Lock()
        # Runs pending or in progress. Each run (or /chay request) counts itself in and
        # out, so one finishing never clears the flag of another still going.
        self._active = 0
        self._active_lock = threading.Lock()

    # ── books ──
    def book_path(self, spec: BookSpec) -> Path:
        return self.dir / spec.file_name

    @property
    def real_path(self) -> Path:
        return self.dir / "real.json"

    def book(self, spec: BookSpec):
        from cryptotrader.trading.ledger import Ledger

        # A damaged paper book is moved aside (kept) and restarted.
        return Ledger.load(self.book_path(spec), self.settings.paper_capital, fresh_on_error=True)

    def books(self) -> list[tuple[BookSpec, object]]:
        """(spec, paper book) of every active strategy, in report order."""
        return [(spec, self.book(spec)) for spec in self.specs]

    @property
    def signal_spec(self) -> BookSpec | None:
        spec = get_book(self.settings.signal_strategy)
        return spec if spec in self.specs else None

    def real(self):
        """The real book; raises LedgerLoadError when its file is damaged (never emptied).

        The ledger moves a damaged file aside; until its owner restores it (or deletes
        the copy to start over) the book stays refused, rather than coming back empty
        on the next load and being overwritten by the next /von or /khop."""
        from cryptotrader.trading.ledger import Ledger, LedgerLoadError

        path = self.real_path
        if not path.exists():
            aside = sorted(path.parent.glob(f"{path.name}.corrupt-*"))
            if aside:
                raise LedgerLoadError(
                    f"Sổ tài khoản thật bị hỏng, đã chuyển sang {aside[-1].name} (không xoá). "
                    f"Khôi phục file đó thành {path.name}, hoặc xoá nó để bắt đầu sổ mới.")
        return Ledger.load(path, 0.0)

    def _real_for_update(self):
        from cryptotrader.trading.ledger import LedgerLoadError

        try:
            return self.real()
        except LedgerLoadError as exc:
            raise ValueError(str(exc)) from exc

    def _real_snapshot(self):
        from cryptotrader.trading.ledger import LedgerLoadError

        with self._real_lock:
            try:
                return self.real(), ""
            except LedgerLoadError as exc:
                logger.error("Real book unreadable: %s", exc)
                return None, str(exc)

    def _read_json(self, name: str) -> dict:
        try:
            data = json.loads((self.dir / name).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_json(self, name: str, data) -> None:
        write_text_atomic(self.dir / name, json.dumps(data, ensure_ascii=False))

    def last_report(self) -> dict:
        return self._read_json("last_report.json")

    def last_prices(self) -> dict:
        """The closes of the last processed day ({"day", "closes"}), for display only."""
        return self._read_json("prices.json")

    def last_day(self) -> str:
        """The last day every paper book has processed ("" before the first run)."""
        days = [_book_day(book) for _, book in self.books()]
        return min(days) if days else ""

    @staticmethod
    def _filters() -> dict:
        """The pairs' exchange filters (exchangeInfo, cached a day); {} = safe defaults."""
        try:
            from cryptotrader.data import binance

            return {s: i.filters for s, i in binance.load_exchange_info().items()}
        except Exception as exc:  # noqa: BLE001 - the ledger falls back to conservative defaults
            logger.warning("Bot: exchangeInfo unavailable, default filters used: %s", exc)
            return {}

    # ── the run ──
    def run_day(self, day: str) -> str:
        """Bring the books up to ``day`` (every missed day first). Returns the report."""
        return self.catch_up(day).text

    def catch_up(self, day: str) -> RunResult:
        """Process, in order, every closed day after each book's last day up to ``day``;
        place the next candle's orders; report on the last one."""
        self._enter()
        try:
            with self._lock:
                return self._catch_up(day)
        finally:
            self._leave()

    def _enter(self) -> None:
        with self._active_lock:
            self._active += 1

    def _leave(self) -> None:
        with self._active_lock:
            self._active = max(0, self._active - 1)

    def _catch_up(self, day: str) -> RunResult:
        if not self.specs:
            raise RuntimeError("Không có chiến lược nào trong cryptotrader.strategies.REGISTRY khớp với bot.books.BOOKS")
        market = _Market(min(day, clock.final_day()))
        days = market.days(rules.BENCHMARK)       # BTC unreadable: raise, retried later
        if not days:
            raise RuntimeError(f"{rules.BENCHMARK} không có nến ngày nào tới {market.as_of}")
        target = days[-1]
        books = self.books()
        todo = {spec.name: _pending(days, _book_day(book)) for spec, book in books}
        last = self.last_report()
        if not any(t for t, _ in todo.values()):
            if last.get("day") == target and last.get("text"):
                return RunResult(target, last["text"])
            at = [_book_day(book) for _, book in books]
            if any(d != target for d in at):
                return RunResult(target, f"✅ Nến {target} đã xử lý (sổ đang ở ngày {max(at)}).")
            # Every book is at the target but its report was lost (a crash before writing
            # it): rebuild it from the books, without processing anything again.

        filters = self._filters()
        for spec, book in books:
            book.filters = filters
            strategy = spec.strategy()
            for d in todo[spec.name][0]:
                self._book_session(spec, strategy, book, d, days, market)

        closes = self._closes(books, market, target)
        self._write_json("prices.json", {"day": target, "closes": closes})
        text = "\n\n".join(self._book_text(spec, book, target) for spec, book in books)
        text += "\n\n" + self._comparison(books)
        last_signal = last.get("signal_strategy")
        if self.settings.trade_mode == "signal":
            real, real_error = self._real_snapshot()
            if real is None:
                text += f"\n\n⚠️ Không đọc được tài khoản thật: {real_error}"
            else:
                real.filters = filters
                text += self._strategy_switch_notice(last, real)
                text += "\n\n" + self._real_report(target, real, dict(books), market, closes)
        done = sorted({d for t, _ in todo.values() for d in t})
        earlier = [d for d in done if d != target]
        head = []
        if earlier:
            head.append(f"⏪ Chạy bù {len(earlier)} ngày bị lỡ: {', '.join(earlier)}" + self._missed_fills(earlier, books))
        skipped = max((s for _, s in todo.values()), default=0)
        if skipped:
            head.append(f"⚠️ Bỏ qua {skipped} ngày cũ hơn (chỉ chạy bù tối đa {MAX_CATCH_UP} ngày); "
                        "lệnh chờ của các ngày đó đã huỷ.")
        if head:
            text = "\n".join(head) + "\n\n" + text
        self._write_json("last_report.json", {
            "day": target, "text": text, "at": clock.now_vn().isoformat(timespec="minutes"), "days": done,
            "signal_strategy": self.settings.signal_strategy if self.settings.trade_mode == "signal" else last_signal,
        })
        # A rebuilt report (books done, report lost) was never sent either: it is new too.
        return RunResult(target, text, bool(done) or last.get("day") != target, done)

    def _book_session(self, spec: BookSpec, strategy, book, day: str, days: list[str], market: _Market) -> None:
        """One day of one paper book: fill or lapse yesterday's orders, exits, NAV, decide, plan, save."""
        from cryptotrader.strategies.base import StrategyContext

        i = days.index(day)
        _expire(book, days[i - 1] if i else None)
        touched = {o.symbol for o in book.orders} | set(book.positions)
        # A held pair whose source does not answer stops the run (retried later) rather
        # than pass for halted: its stop would be skipped and its order lost.
        bars = {s: b for s in touched if (b := market.bar(s, day, required=True)) is not None}
        book.process_day(bars, exit_rules=getattr(strategy, "exit_rules", None))
        book.end_of_day(day, market.close(rules.BENCHMARK, day))

        wanted = list(dict.fromkeys(list(self._strategy_symbols(strategy, day)) + list(book.positions)))
        history, down = {}, []
        for symbol in wanted:
            try:
                frame = market.frame(symbol, day, required=True)
            except Exception as exc:
                if symbol in book.positions or not _unavailable(exc):
                    raise
                down.append(symbol)
                continue
            if frame is not None and len(frame):
                history[symbol] = frame
        if down and len(down) > MAX_UNAVAILABLE_SHARE * len(wanted):
            raise RuntimeError(f"Nguồn giá không trả lời cho {len(down)}/{len(wanted)} cặp "
                               f"({', '.join(down[:5])}{', ...' if len(down) > 5 else ''}) - ngày {day} "
                               f"của {spec.title} chưa lưu, sẽ chạy lại.")
        prices = {s: c for s in set(history) | set(book.positions) if (c := market.close(s, day)) is not None}
        state = book.meta.get("strategy_state")
        if not isinstance(state, dict):
            state = {}
        ctx = StrategyContext(day, history, weights=book.weights(prices), nav=book.nav(prices),
                              cash=book.cash, state=state)
        decision = strategy.decide(ctx)
        if isinstance(decision, dict):
            target = {s.upper(): float(w) for s, w in decision.items() if _finite(w) and float(w) > 0}
            book.plan_target_weights(day, target, prices, min_trade_pct=_min_trade_pct(strategy))
            book.meta["target"] = {s: round(w, 6) for s, w in target.items()}
        else:
            for order in decision or []:
                order.created = day
                book.submit(order)
            book.meta["target"] = None
        book.meta["strategy_state"] = ctx.state
        book.meta["last_day"] = day
        book.save(self.book_path(spec))

    @staticmethod
    def _strategy_symbols(strategy, day: str) -> list[str]:
        """The pairs a strategy looks at on ``day``: ``universe(day)`` when it has one
        (a point-in-time universe), else its fixed ``symbols``."""
        universe = getattr(strategy, "universe", None)
        symbols = universe(day) if callable(universe) else getattr(strategy, "symbols", ())
        return [str(s).upper() for s in symbols or ()]

    def _closes(self, books, market: _Market, day: str) -> dict:
        """The closes of ``day`` the displays value the books with: BTC, the paper books'
        holdings and targets, and in signal mode the real book's holdings (a real coin
        the paper books do not hold must not stay valued at its fill price)."""
        wanted = {rules.BENCHMARK}
        for _, book in books:
            wanted |= set(book.positions) | set(book.meta.get("target") or {})
        if self.settings.trade_mode == "signal":
            real, _ = self._real_snapshot()
            if real is not None:
                wanted |= set(real.positions)
        return {s: c for s in sorted(wanted) if (c := market.close(s, day)) is not None}

    # ── report text ──
    @staticmethod
    def _book_text(spec: BookSpec, book, day: str) -> str:
        fills = [f for f in book.fills if f.date == day]
        orders = [o for o in book.orders if o.created == day]
        st = book.stats()
        bench = f" | BTC {fmt.pct(st['benchmark_pct'])}" if st["benchmark_pct"] is not None else ""
        lines = [f"📈 {spec.title.upper()} - nến {day} (UTC)",
                 f"NAV {fmt.usdt(st['nav'])} USDT ({fmt.pct(st['return_pct'])} từ {book.start_date or day}){bench} | "
                 f"sụt giảm tối đa {fmt.pct(st['max_drawdown_pct'], signed=False)}"]
        target = book.meta.get("target")
        if target is not None:
            lines.append(f"Mục tiêu: {_weights_text(target)}")
        if note := _explain(spec.strategy(), book):
            lines.append(note)
        lines += [f"- Khớp {_side_vi(f.side)} {_base(f.symbol)} {fmt.qty(f.qty)} @ {fmt.price(f.price)} ({f.reason})"
                  + (f", lãi/lỗ {fmt.usdt(f.pnl)} USDT" if f.pnl is not None else "") for f in fills]
        lines += [f"- Lệnh nến tới: {_side_vi(o.side)} {_base(o.symbol)} {fmt.qty(o.qty)} "
                  + (f"giá ≤ {fmt.price(o.limit)}" if o.limit and o.side == "BUY" else
                     f"giá ≥ {fmt.price(o.limit)}" if o.limit else "giá mở cửa")
                  + (f" ({o.reason})" if o.reason else "") for o in orders]
        rejected = [r for r in book.meta.get("rejections") or [] if r.get("date") == day]
        lines += [f"- Không đặt {_side_vi(r.get('side', ''))} {_base(str(r.get('symbol', '')))}: {r.get('reason', '')}"
                  for r in rejected[-5:]]
        if not fills and not orders and not rejected:
            lines.append("Không có lệnh mới.")
        return "\n".join(lines)

    @staticmethod
    def _missed_fills(days, books) -> str:
        wanted = set(days)
        lines = []
        for spec, book in books:
            lines += [f"\n- {f.date} {spec.title}: {_side_vi(f.side)} {_base(f.symbol)} {fmt.qty(f.qty)} "
                      f"@ {fmt.price(f.price)} ({f.reason})" for f in book.fills if f.date in wanted]
        return "".join(lines) if lines else " (không có lệnh khớp)"

    @staticmethod
    def _comparison(books) -> str:
        """Each book's return since its own start (dated: books may have started on
        different days, and then the figures cover different periods)."""
        parts, starts = [], set()
        for spec, book in books:
            st = book.stats()
            since = f" từ {book.start_date}" if book.start_date else ""
            starts.add(book.start_date or "")
            parts.append(f"{spec.title} {fmt.pct(st['return_pct'])}{since} "
                         f"(DD {fmt.pct(st['max_drawdown_pct'], signed=False, decimals=1)})")
        text = "📊 So sánh (sau phí và trượt giá giả định): " + " | ".join(parts)
        if len(starts) > 1:
            text += "\n(các tài khoản bắt đầu ở các ngày khác nhau: lợi nhuận không cùng giai đoạn)"
        return text

    def _strategy_switch_notice(self, last: dict, real) -> str:
        """A one-time note when the real signals follow another strategy than in the last
        report (a changed SIGNAL_STRATEGY) while the real account holds coins."""
        now = self.settings.signal_strategy
        before = last.get("signal_strategy")
        if not last.get("day") or not real.positions or not before or before == now:
            return ""
        return (f"\n\nℹ️ Tín hiệu thật đổi chiến lược: {title_of(before)} → {title_of(now)}. "
                "Lệnh dưới đây đưa tài khoản thật về mục tiêu của chiến lược mới, kể cả bán các coin "
                f"đang giữ theo chiến lược cũ. Muốn giữ chiến lược cũ: đặt SIGNAL_STRATEGY={before} "
                "trong .env rồi khởi động lại bot.")

    def _real_report(self, day: str, real, books: dict, market: _Market, closes: dict) -> str:
        """Orders that move the real book to the signal book's targets, sized on the real book."""
        from cryptotrader.trading.ledger import Ledger

        spec = self.signal_spec
        title = spec.title if spec else self.settings.signal_strategy
        lines = [f"📣 TÍN HIỆU THẬT - {title.upper()} (tự đặt lệnh trên sàn, rồi báo /khop)"]
        if spec is None:
            lines.append(f"⚠️ Chiến lược {self.settings.signal_strategy!r} không chạy trong bot; không có tín hiệu.")
            return "\n".join(lines)
        actions = 0
        for symbol, pos in real.positions.items():
            bar = market.bar(symbol, day)
            if bar is not None and bar.tradable and pos.stop and bar.low <= pos.stop \
                    and not real.is_dust(pos, bar.close):
                actions += 1
                lines.append(f"- ⛔ {_base(symbol)}: chạm stop {fmt.price(pos.stop)} - đề xuất BÁN "
                             f"{fmt.qty(real.sellable_qty(pos))}")
        if real.cash <= 0 and not real.positions:
            lines.append("Chưa có vốn tài khoản thật: gửi /von 1000 (USDT) để bot tính khối lượng.")
            return "\n".join(lines)
        paper = books[spec]
        prices = dict(closes)
        for symbol in real.positions:
            if symbol not in prices and (c := market.close(symbol, day)) is not None:
                prices[symbol] = c
        draft = Ledger.from_dict(copy.deepcopy(real.to_dict()))
        draft.filters = real.filters
        draft.orders, draft.meta["rejections"] = [], []
        target = paper.meta.get("target")
        strategy = spec.strategy()
        hook = getattr(strategy, "real_target", None)
        if target is not None and callable(hook):
            # The strategy's own rule (e.g. its drift band) on the REAL book's weights, from
            # the signal the paper book computed today; the paper target is the fallback.
            try:
                own = hook(draft.weights(prices), paper.meta.get("strategy_state") or {}, day)
                target = own if own is not None else {}
                hold = own is None
            except LookupError:
                hold = False
        else:
            hold = False
        if hold:
            lines.append(f"Mục tiêu: giữ nguyên {_weights_text(draft.weights(prices))} (lệch so với mục tiêu "
                         "lý tưởng còn trong biên của chiến lược).")
            orders = []
        elif target is not None:
            lines.append(f"Mục tiêu: {_weights_text(target)}")
            orders = draft.plan_target_weights(day, target, prices, min_trade_pct=_min_trade_pct(strategy))
        else:
            # A strategy that gives orders, not weights: its orders, scaled to the real NAV.
            ratio = draft.nav(prices) / paper.nav() if paper.nav() > 0 else 0.0
            orders = []
            for o in [o for o in paper.orders if o.created == day]:
                scaled = copy.deepcopy(o)
                scaled.qty = o.qty * ratio if o.side == "BUY" else min(o.qty * ratio, real.positions[o.symbol].qty
                                                                         if o.symbol in real.positions else 0.0)
                scaled.ref = scaled.ref or prices.get(o.symbol)
                if (placed := draft.submit(scaled)) is not None:
                    orders.append(placed)
        rejected = draft.meta.get("rejections") or []
        actions += len(orders) + len(rejected)
        for o in orders:
            ref = o.ref or prices.get(o.symbol) or 0.0
            lines.append(f"- {_side_vi(o.side)} {_base(o.symbol)} {fmt.qty(o.qty)} (~{fmt.usdt(o.qty * ref)} USDT "
                         f"theo giá đóng cửa {fmt.price(ref)}), lệnh thị trường")
        for r in rejected:
            lines.append(f"- Bỏ qua {_side_vi(r.get('side', ''))} {_base(str(r.get('symbol', '')))}: {r.get('reason', '')}")
        if not actions:
            lines.append("Không đổi danh mục: tài khoản thật đã ở gần mục tiêu.")
        lines.append(f"Khối lượng đã làm tròn theo bước khối lượng (stepSize) của sàn; phí giả định {fmt.pct(draft.fee_pct, signed=False)} "
                     "mỗi lệnh. Đặt lệnh sau khi nến đóng (07:00 giờ VN).")
        return "\n".join(lines)

    # ── on demand (/chay, the web button) ──
    @property
    def running(self) -> bool:
        with self._active_lock:
            return self._active > 0

    def is_done(self, day: str) -> bool:
        return bool(self.specs) and self.last_day() >= day and self.last_report().get("day") == day

    def start_run(self, notify=None, report=None) -> str:
        """Run the last closed day in the background. A new report goes to ``report``
        (default ``notify``); "already done" and errors go to ``notify``."""
        notify = notify or self.notify
        report = report or notify
        with self._start_guard:
            if self.running:
                return "⏳ Bot đang chạy, đợi vài phút."
            day = clock.final_day()
            if self.is_done(day):
                return f"✅ Nến {day} đã chạy rồi - xem /danhmuc, /lenh hoặc trang web."
            self._enter()           # counted from now, so a second /chay sees it at once

        def work():
            try:
                try:
                    result = self.catch_up(day)
                finally:
                    self._leave()   # only this request's own count: another run keeps its flag
                if result.new:
                    report(result.text)
                else:
                    notify(f"✅ Nến {day} đã chạy rồi - xem /danhmuc hoặc /lenh.")
            except Exception as exc:  # noqa: BLE001
                logger.exception("On-demand run %s failed", day)
                notify(f"❌ Bot giao dịch lỗi ngày {day}: {exc}")

        threading.Thread(target=work, name="bot-run-now", daemon=True).start()
        return (f"▶️ Đang chạy nến {day} (UTC) cho {len(self.specs)} tài khoản ảo (khoảng 1 phút); "
                "báo cáo sẽ gửi khi xong.")

    # ── the real book, updated by its owner ──
    @staticmethod
    def _fill_day(day: str | None, last: str) -> str:
        """The fill's UTC day: default today (UTC); not in the future, not before the last
        fill already recorded (the book is kept in date order)."""
        today = clock.today_utc()
        raw = (day or "").strip() or today
        try:
            parsed = _date.fromisoformat(raw) if len(raw) == 10 else None
        except ValueError:
            parsed = None
        if parsed is None:
            raise ValueError(f"Ngày khớp không hợp lệ: {raw!r}. Dùng dạng YYYY-MM-DD (ngày UTC).")
        text = parsed.isoformat()
        if text > today:
            raise ValueError(f"Ngày khớp {text} ở tương lai (hôm nay theo UTC là {today}).")
        if last and text < last:
            raise ValueError(f"Ngày khớp {text} trước lệnh đã ghi gần nhất ({last}); "
                             "báo các lệnh theo đúng thứ tự ngày.")
        return text

    def _reference_price(self, symbol: str) -> float | None:
        """The pair's last close, to catch a mistyped fill price; None when unknown.

        From the last run's closes, else the candle cache on disk: never a download,
        so a /khop does not wait on a price source (or on a run refreshing the cache)."""
        prices = self.last_prices().get("closes") or {}
        if _finite(prices.get(symbol)) and prices[symbol] > 0:
            return float(prices[symbol])
        try:
            from cryptotrader.data import ohlcv

            frame, _ = ohlcv.read_cache(symbol)
            value = float(frame["Close"].iloc[-1]) if frame is not None and len(frame) else None
            return value if value is not None and _finite(value) and value > 0 else None
        except Exception as exc:  # noqa: BLE001 - a check only: unknown is not an error
            logger.info("No reference price for %s: %s", symbol, exc)
            return None

    def record_fill(self, side: str, symbol: str, qty: float, price: float, day: str | None = None,
                    fee_in: str | None = None) -> str:
        """Book a fill the owner made on the exchange (/khop). Raises ValueError with a
        Vietnamese message, leaving the book untouched, when it cannot have happened.

        ``qty`` is the executed quantity of the order (as the exchange's order history
        and the bot's signal show it). ``fee_in`` is where the exchange took the fee:
        ``coin`` (default for a BUY: Binance takes a buy's fee from the coin received,
        so the book holds ``qty`` less the fee and pays no USDT fee), ``usdt`` (default
        for a SELL), or ``bnb`` (paid with BNB, outside this book: no fee booked).
        """
        from cryptotrader.data.symbols import normalize_symbol

        side = {"MUA": "BUY", "BAN": "SELL", "BÁN": "SELL"}.get(str(side).strip().upper(), str(side).strip().upper())
        if side not in ("BUY", "SELL"):
            raise ValueError("Dùng: /khop mua|ban COIN KHỐI_LƯỢNG GIÁ [phi=coin|usdt|bnb] [YYYY-MM-DD]")
        fee_in = (str(fee_in).strip().lower() if fee_in else "") or ("coin" if side == "BUY" else "usdt")
        if fee_in not in FEE_ASSETS:
            raise ValueError("Phí trả bằng coin, usdt hoặc bnb (ví dụ phi=bnb).")
        if side == "SELL" and fee_in == "coin":
            raise ValueError("Lệnh bán trả phí bằng USDT (mặc định) hoặc BNB (phi=bnb), không trừ vào coin.")
        pair = normalize_symbol(str(symbol))
        why = rules.exclusion_reason(pair)
        if why:
            raise ValueError(f"{pair}: {why}, bot không theo dõi cặp này.")
        if isinstance(qty, bool) or not _finite(qty) or float(qty) <= 0:
            raise ValueError("Khối lượng phải là số dương.")
        if isinstance(price, bool) or not _finite(price) or float(price) <= 0:
            raise ValueError("Giá khớp không hợp lệ.")
        qty, price = float(qty), float(price)
        ref = self._reference_price(pair)
        if ref and not (ref / FILL_PRICE_BAND <= price <= ref * FILL_PRICE_BAND):
            raise ValueError(f"Giá {fmt.price(price)} quá xa giá đóng cửa gần nhất của {_base(pair)} "
                             f"({fmt.price(ref)}) - kiểm tra lại (giá tính bằng USDT).")
        with self._real_lock:
            real = self._real_for_update()
            day = self._fill_day(day, str(real.meta.get("last_fill_day") or ""))
            held = real.positions.get(pair)
            t1_before = bool(held and held.t1_done)
            booked, fee = qty, None
            if fee_in == "coin":
                # The coin received is the executed quantity less the fee; the USDT paid is
                # the whole executed value, so the fee is booked as its USDT worth.
                booked = float(f"{qty * (1 - real.fee_pct / 100):.12g}")
                fee = (qty - booked) * price
            elif fee_in == "bnb":
                fee = 0.0
            fill = real.record_fill(side, pair, booked, price, day, fee=fee, reason="báo khớp")
            real.meta["last_fill_day"] = max(day, str(real.meta.get("last_fill_day") or ""))
            real.save(self.real_path)
        if side == "BUY":
            how = {"coin": f"phí {fmt.pct(real.fee_pct, signed=False)} trừ vào coin: thực nhận "
                           f"{fmt.qty(booked)} {_base(pair)} (~{fmt.usdt(fill.fee, 4)} USDT)",
                   "usdt": f"phí {fmt.usdt(fill.fee, 4)} USDT",
                   "bnb": "phí trả bằng BNB (không ghi vào sổ)"}[fee_in]
            return (f"✅ Đã ghi MUA {_base(pair)} {fmt.qty(qty)} @ {fmt.price(price)} ngày {day} (UTC), "
                    f"{how}. USDT còn {fmt.usdt(real.cash)}.")
        text = (f"✅ Đã ghi BÁN {_base(pair)} {fmt.qty(qty)} @ {fmt.price(price)} ngày {day} (UTC); "
                f"lãi/lỗ {fmt.usdt(fill.pnl)} USDT. USDT còn {fmt.usdt(real.cash)}.")
        pos = real.positions.get(pair)
        if pos is not None and pos.t1_done and not t1_before:
            text += f" Đã chốt T1: stop phần còn lại ({fmt.qty(pos.qty)}) dời về hoà vốn {fmt.price(pos.stop)}."
        return text

    def set_capital(self, cash: float) -> str:
        """Set the real book's USDT cash (/von), as the exchange shows it."""
        if isinstance(cash, bool) or not _finite(cash) or float(cash) <= 0:
            raise ValueError("Vốn phải là một số lớn hơn 0.")
        if float(cash) > 1e10:
            raise ValueError("Số vốn quá lớn - kiểm tra lại (nhập số USDT, ví dụ 1000).")
        cash = float(cash)
        with self._real_lock:
            real = self._real_for_update()
            real.cash = cash
            if not real.positions:
                real.start_cash = cash
            real.save(self.real_path)
        return f"✅ Tiền mặt tài khoản thật: {fmt.usdt(cash)} USDT."

    # ── for the AI analysis ──
    def analysis_portfolio(self, day: str) -> dict | None:
        """The book an analysis of ``day`` should reason about, or None when unknown.

        Only for the books' own last day (a past day's analysis must not see today's
        holdings): in signal mode the real account when it has anything recorded,
        else the paper book of the real-signal strategy. Weights at the last closes."""
        spec = self.signal_spec
        if spec is None or self.settings.trade_mode == "off" or not day or day != self.last_day():
            return None
        closes = {s: float(c) for s, c in (self.last_prices().get("closes") or {}).items() if _finite(c)}
        book, label = self.book(spec), f"tài khoản ảo {spec.title}"
        if self.settings.trade_mode == "signal":
            real, _ = self._real_snapshot()
            if real is not None and (real.positions or real.cash > 0):
                book, label = real, "tài khoản thật (theo /khop, /von)"
        nav = book.nav(closes)
        if nav <= 0:
            return None
        return {"weights": {s: round(w, 6) for s, w in book.weights(closes).items()}, "nav": round(nav, 2),
                "cash": round(book.cash, 2), "source": label, "day": day}

    # ── status ──
    def portfolio_text(self) -> str:
        from cryptotrader.trading.ledger import LedgerLoadError

        lines = []
        for spec, book in self.books():
            s = book.stats()
            lines.append(f"📈 {spec.title}: NAV {fmt.usdt(s['nav'])} USDT ({fmt.pct(s['return_pct'])}"
                         + (f" từ {book.start_date}" if book.start_date else "") + f"), USDT {fmt.usdt(book.cash)}, "
                         f"mục tiêu {_weights_text(book.meta.get('target'))}")
            lines += [f"- {_base(p.symbol)} {fmt.qty(p.qty)} @ {fmt.price(p.avg_price)} → {fmt.price(p.last_price)}"
                      + (f", stop {fmt.price(p.stop)}" if p.stop else "")
                      for p in book.positions.values()] or ["- (không giữ coin nào)"]
        if self.settings.trade_mode == "signal":
            try:
                real = self.real()
            except LedgerLoadError as exc:
                lines.append(f"💼 Thật: ⚠️ {exc}")
                return "\n".join(lines)
            closes = self.last_prices().get("closes") or {}
            nav = real.nav({s: float(c) for s, c in closes.items() if _finite(c)})
            lines.append(f"💼 Thật: USDT {fmt.usdt(real.cash)}, ước tính NAV {fmt.usdt(nav)} USDT "
                         f"(giá đóng cửa {self.last_prices().get('day') or '-'})")
            lines += [f"- {_base(p.symbol)} {fmt.qty(p.qty)} @ {fmt.price(p.avg_price)}"
                      + (f", stop {fmt.price(p.stop)}" if p.stop else "")
                      for p in real.positions.values()] or ["- (chưa ghi vị thế nào)"]
        return "\n".join(lines) or "Chưa có tài khoản ảo nào."

    def orders_text(self) -> str:
        lines = []
        for spec, book in self.books():
            lines.append(f"{spec.title}:")
            lines += [f"- {_side_vi(o.side)} {_base(o.symbol)} {fmt.qty(o.qty)} "
                      + (f"giá giới hạn {fmt.price(o.limit)}" if o.limit else "giá mở cửa")
                      + f" (đặt sau nến {o.created})" for o in book.orders] or ["- không có lệnh chờ"]
        return "Lệnh cho nến tới (khớp lúc 07:00 giờ VN)\n" + "\n".join(lines)

    def next_run_text(self) -> str:
        last = self.last_day() or "chưa chạy"
        return (f"Bot: chế độ {self.settings.trade_mode}, chạy mỗi ngày lúc {self.settings.run_time} (giờ VN), "
                f"nến đã xử lý tới {last}; tín hiệu thật theo {title_of(self.settings.signal_strategy)}.")
