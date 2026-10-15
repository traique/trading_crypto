"""Futures (USDⓈ-M perpetual, long/short): paper books and signals for a real account.

One paper book per symbol of ``FUTURES_SYMBOLS`` (default ETHUSDT), each
starting with ``FUTURES_CAPITAL`` USDT (default 10). Every day after the daily
candle closes, ``catch_up`` replays each closed UTC day the book has not
processed (at most ``trader.MAX_CATCH_UP``), with the candles up to that day
only, through ``cryptotrader.futures.step``: the planned position fills at the
open, stop and liquidation are checked on the candle, funding is charged, then
the ``ls_sma`` rule plans the next position. The account policy closes and
halts a book at the goal (capital + ``FUTURES_GOAL_PROFIT``) or at the floor
(``FUTURES_STOP_EQUITY``); the owner resumes it with ``/futures tieptuc``.

Prices: the Binance spot daily candles stand in for the perpetual's (the two
differ by the basis, usually well under 0.1%); funding is the conservative
default of ``cryptotrader.futures.rules`` (the backtest uses the real rates).

Signal mode (``FUTURES_MODE=signal``): the owner tells the bot the equity of
the real futures account (``/fvon 10``) and gets, each morning, the target
position for ``FUTURES_SIGNAL_SYMBOL`` sized on that equity with the same
rule (side, quantity on the contract's step, leverage, stop price), and the
same goal / floor checks on the real equity. The bot never places an
exchange order and never asks for exchange API keys.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from bot import clock
from bot.settings import Settings
from bot.trader import MAX_CATCH_UP, RunResult, _Market, _pending
from cryptotrader.data.files import write_text_atomic
from cryptotrader.futures import AccountPolicy, FuturesBook, LongShortSma, spec_for, step

logger = logging.getLogger(__name__)

_SIDE_VI = {"LONG": "LONG (mua)", "SHORT": "SHORT (bán khống)", "FLAT": "đứng ngoài"}
_HALT_VI = {"goal": "🎯 đã đạt mục tiêu - đang dừng", "floor": "🛑 chạm mức dừng lỗ tổng - đang dừng"}


def _base(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _num(value: float, digits: int = 6) -> str:
    """A number without thousands separators (copyable into an order form)."""
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


class FuturesTrader:
    def __init__(self, settings: Settings, notify=None):
        self.settings = settings
        self.notify = notify or (lambda text: None)
        self.dir = settings.data_dir / "trading"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.symbols = tuple(settings.futures_symbols)
        self._lock = threading.RLock()
        self._busy = False

    # ── configuration ──
    def strategy(self) -> LongShortSma:
        s = self.settings
        return LongShortSma.preset(s.futures_rule, target_vol=s.futures_target_vol,
                                   max_leverage=s.futures_max_leverage,
                                   min_order_max_leverage=s.futures_min_order_leverage)

    def _btc(self, market: _Market, day: str):
        """BTCUSDT's candles up to ``day`` for the BTC filter (None when unavailable)."""
        try:
            frame = market.frame("BTCUSDT", day)
        except Exception:  # noqa: BLE001 - the filter then keeps altcoins flat
            logger.warning("Futures: no BTC candles for the BTC filter on %s", day)
            return None
        return None if frame is None else frame.iloc[-self.strategy().warmup_days:]

    def policy(self) -> AccountPolicy:
        return AccountPolicy(goal_profit=self.settings.futures_goal_profit,
                             stop_equity=self.settings.futures_stop_equity,
                             dd_scale=self.settings.futures_dd_scale)

    # ── storage ──
    def _path(self, symbol: str):
        return self.dir / f"futures_{symbol}.json"

    def load_book(self, symbol: str) -> FuturesBook:
        path = self._path(symbol)
        if path.exists():
            try:
                return FuturesBook.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError) as exc:
                raise RuntimeError(f"không đọc được sổ futures {symbol}: {exc}") from exc
        return FuturesBook(symbol=symbol, capital=self.settings.futures_capital,
                           meta={"start_capital": self.settings.futures_capital})

    def save_book(self, book: FuturesBook) -> None:
        write_text_atomic(self._path(book.symbol), json.dumps(book.to_dict(), ensure_ascii=False))

    def _real(self) -> dict:
        try:
            return json.loads((self.dir / "futures_real.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_real(self, data: dict) -> None:
        write_text_atomic(self.dir / "futures_real.json", json.dumps(data, ensure_ascii=False))

    # ── the daily run ──
    @property
    def running(self) -> bool:
        return self._busy

    def catch_up(self, final: str) -> RunResult:
        """Bring every futures book up to ``final`` (the last closed UTC day)."""
        with self._lock:
            self._busy = True
            try:
                return self._catch_up(final)
            finally:
                self._busy = False

    def _catch_up(self, final: str) -> RunResult:
        from cryptotrader.futures import rules as futures_rules

        futures_rules.refresh_specs(self.settings.data_dir)   # step / minimum order, cached a day
        market = _Market(final)
        strategy, policy = self.strategy(), self.policy()
        processed: list[str] = []
        notes: list[str] = []
        reached = None
        for symbol in self.symbols:
            book = self.load_book(symbol)
            days = [d for d in market.days(symbol) if d <= final]
            if not days:
                notes.append(f"{_base(symbol)}: không có nến ngày nào (cặp không tồn tại hoặc nguồn chưa có) - bỏ qua.")
                continue
            todo, skipped = _pending(days, str(book.meta.get("last_day") or ""))
            if skipped:
                notes.append(f"{_base(symbol)}: bỏ qua {skipped} ngày cũ (quá {MAX_CATCH_UP} ngày), "
                             "lệnh chờ bị huỷ.")
                book.pending = None
            for day in todo:
                frame = market.frame(symbol, day, required=True)
                if frame is None or not len(frame):
                    continue
                frame = frame.iloc[-strategy.warmup_days:]
                btc = self._btc(market, day) if strategy.btc_filter and symbol != "BTCUSDT" else None
                for note in step(book, strategy, frame, policy, btc_frame=btc):
                    notes.append(f"{_base(symbol)} {day}: {note}")
                book.meta["last_day"] = day
                self.save_book(book)
                processed.append(day)
            last = str(book.meta.get("last_day") or "")
            reached = last if reached is None else min(reached, last)
        new = bool(processed)
        real_lines: list[str] = []
        if self.settings.futures_mode == "signal":
            real_lines = self._real_signal(market, reached or final)
        text = self.report_text(reached or final, notes, real_lines)
        if new:
            write_text_atomic(self.dir / "futures_last_report.txt", text)
        return RunResult(day=reached, text=text, new=new, days=sorted(set(processed)))

    def is_done(self, day: str) -> bool:
        return all(str(self.load_book(s).meta.get("last_day") or "") >= day for s in self.symbols)

    def start_run(self, notify=None, report=None) -> str:
        """Run the last closed day in a background thread (for /futures chay)."""
        notify = notify or self.notify
        report = report or notify
        if self._busy:
            return "⏳ Bot futures đang chạy, đợi một chút."
        day = clock.final_day()
        if self.is_done(day):
            return f"✅ Futures: nến {day} đã chạy rồi - xem /futures."

        def work():
            try:
                result = self.catch_up(day)
                (report if result.new else notify)(result.text)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Futures run %s failed", day)
                notify(f"❌ Bot futures lỗi ngày {day}: {exc}")

        threading.Thread(target=work, name="futures-run-now", daemon=True).start()
        return f"▶️ Futures: đang chạy nến {day} (UTC); báo cáo sẽ gửi khi xong."

    # ── the real account (signal mode) ──
    def set_real_equity(self, equity: float) -> str:
        if not equity or equity <= 0:
            raise ValueError("Vốn futures phải lớn hơn 0.")
        real = self._real()
        real["equity"] = float(equity)
        real.setdefault("start", float(equity))
        real["peak"] = max(float(real.get("peak") or 0.0), float(equity))
        real["updated"] = time.time()
        self._save_real(real)
        start = float(real["start"])
        goal = self.settings.futures_goal_profit
        return (f"✅ Đã ghi vốn tài khoản futures thật: {_num(equity, 2)} USDT (vốn gốc {_num(start, 2)} USDT"
                + (f", mục tiêu {_num(start + goal, 2)} USDT" if goal else "") + "). Lệnh đề xuất sáng mai "
                "tính theo số này; gửi lại /fvon khi số dư thay đổi. Muốn đặt lại vốn gốc: /fvon <số> moi.")

    def reset_real_start(self, equity: float) -> str:
        real = self._real()
        real.update({"equity": float(equity), "start": float(equity), "peak": float(equity),
                     "updated": time.time()})
        self._save_real(real)
        return f"✅ Đặt lại vốn gốc futures thật = {_num(equity, 2)} USDT (mục tiêu tính lại từ đây)."

    def _real_signal(self, market: _Market, day: str) -> list[str]:
        """The target position of each signal symbol, sized on the real equity split evenly."""
        symbols = tuple(self.settings.futures_signal_symbols) or (self.settings.futures_signal_symbol,)
        real = self._real()
        equity = float(real.get("equity") or 0.0)
        if equity <= 0:
            return ["Tài khoản futures thật: chưa có vốn - gửi /fvon <số USDT> (số dư ví futures)."]
        start = float(real.get("start") or equity)
        s = self.settings
        names = "+".join(_base(x) for x in symbols)
        head = f"Tài khoản futures THẬT ({names}, vốn {_num(equity, 2)} USDT, gốc {_num(start, 2)}):"
        if s.futures_goal_profit and equity >= start + s.futures_goal_profit:
            real["last_signal"] = {"day": day, "targets": {}}
            self._save_real(real)
            return [head, f"🎯 ĐÃ ĐẠT MỤC TIÊU (+{_num(equity - start, 2)} USDT). Đóng mọi vị thế và rút lãi về "
                          "ví spot. Muốn chạy tiếp với mục tiêu mới: /fvon <số dư> moi."]
        if s.futures_stop_equity and equity <= s.futures_stop_equity:
            real["last_signal"] = {"day": day, "targets": {}}
            self._save_real(real)
            return [head, f"🛑 Vốn ≤ mức dừng {_num(s.futures_stop_equity, 2)} USDT: đóng mọi vị thế, NGỪNG "
                          "giao dịch và xem lại kế hoạch. Muốn chạy lại: /fvon <số dư> moi."]
        strategy = self.strategy()
        exposure = self.policy().exposure(equity, float(real.get("peak") or equity))
        share = equity / len(symbols)
        lines = [head]
        if len(symbols) > 1:
            lines.append(f"Vốn chia đều: {_num(share, 2)} USDT mỗi cặp.")
        if exposure < 0.999:
            lines.append(f"Đang sụt giảm so với đỉnh {_num(float(real.get('peak') or equity), 2)} USDT: "
                         f"cỡ vị thế {exposure * 100:.0f}% (FUTURES_DD_SCALE).")
        targets = {}
        for symbol in symbols:
            frame = market.frame(symbol, day)
            if frame is None or not len(frame):
                lines.append(f"• {_base(symbol)}: không có nến để tính tín hiệu.")
                continue
            spec = spec_for(symbol)
            btc = self._btc(market, day) if strategy.btc_filter and symbol != "BTCUSDT" else None
            d = strategy.decide(frame.iloc[-strategy.warmup_days:], share, 0.0, spec, btc_frame=btc,
                                exposure=exposure)
            price = float(d["price"] or 0.0)
            qty = float(d["qty"] or 0.0)
            targets[symbol] = {"qty": qty, "price": price, "stop_dist": d["stop_dist"], "reason": d["reason"]}
            if not qty:
                lines.append(f"• {_base(symbol)}: ĐỨNG NGOÀI (đóng vị thế nếu đang có). Lý do: {d['reason']}.")
                continue
            side = "LONG" if qty > 0 else "SHORT"
            lines.append(f"• {_base(symbol)}: {_SIDE_VI[side]} {_num(abs(qty))} "
                         f"(≈ {_num(abs(qty) * price, 2)} USDT theo giá đóng cửa {_num(price, 4)}, "
                         f"đòn bẩy ≈ x{d['leverage']:.2f}; cross, one-way).")
            if d["stop_dist"]:
                stop = price - d["stop_dist"] if qty > 0 else price + d["stop_dist"]
                lines.append(f"  Cắt lỗ: STOP_MARKET {'bán' if qty > 0 else 'mua'} đóng vị thế ở ≈ {_num(stop, 4)} "
                             f"(= giá vào {'−' if qty > 0 else '+'} {_num(d['stop_dist'], 4)}; tính lại theo giá khớp thật).")
            lines.append(f"  Lý do: {d['reason']}.")
            if not spec.known:
                lines.append(f"  ⚠️ Chưa có thông số hợp đồng {symbol} trong bot: kiểm tra bước khối lượng và "
                             "lệnh tối thiểu trên Binance.")
        real["last_signal"] = {"day": day, "targets": targets}
        self._save_real(real)
        lines.append("Đã đúng chiều và khối lượng lệch không quá 25% thì giữ nguyên; ngược chiều thì đóng rồi mở lại.")
        return lines

    # ── texts ──
    def _book_lines(self, book: FuturesBook) -> list[str]:
        price = float(book.history[-1]["close"]) if book.history else None
        nav = book.last_nav
        start = float(book.meta.get("start_capital") or book.capital)
        goal = self.settings.futures_goal_profit
        lines = [f"• {_base(book.symbol)} (ảo, vốn {_num(book.capital, 2)}): NAV {_num(nav, 2)} USDT "
                 f"({(nav / book.capital - 1) * 100:+.1f}%)"
                 + (f", mục tiêu {_num(book.capital + goal, 2)} → {max(0.0, min(1.0, (nav - book.capital) / goal)) * 100:.0f}%"
                    if goal else "")
                 + (f"; từ đầu {_num(start, 2)} → {(nav / start - 1) * 100:+.1f}%" if start != book.capital else "")]
        if book.meta.get("halted"):
            lines.append(f"  {_HALT_VI.get(book.meta['halted'], book.meta['halted'])} từ "
                         f"{book.meta.get('halted_on', '?')}; gửi /futures tieptuc {_base(book.symbol)} để chạy lại.")
        if book.qty and price:
            liq = book.liquidation()
            lines.append(f"  Vị thế: {_SIDE_VI[book.side]} {_num(abs(book.qty))} giá vào {_num(book.entry, 4)}, "
                         f"đòn bẩy x{book.leverage(price):.2f}"
                         + (f", cắt lỗ {_num(book.stop, 4)}" if book.stop else "")
                         + (f", thanh lý ≈ {_num(liq, 4)}" if liq else ""))
        else:
            lines.append("  Vị thế: đứng ngoài")
        if book.pending:
            q = float(book.pending.get("qty") or 0.0)
            what = (f"{'LONG' if q > 0 else 'SHORT'} {_num(abs(q))}" if q else "đóng vị thế")
            lines.append(f"  Lệnh cho nến tới (giá mở cửa): {what} - {book.pending.get('note') or ''}".rstrip(" -"))
        signal = book.meta.get("signal") or {}
        if signal.get("reason") and not book.meta.get("halted"):
            lines.append(f"  Tín hiệu {signal.get('day')}: {signal['reason']}")
        return lines

    def report_text(self, day: str, notes: list[str] | None = None, real_lines: list[str] | None = None) -> str:
        lines = [f"📈 FUTURES long/short - nến {day} (UTC)"]
        for note in notes or []:
            lines.append(note)
        for symbol in self.symbols:
            lines += self._book_lines(self.load_book(symbol))
        if real_lines:
            lines.append("")
            lines += real_lines
        lines.append("")
        lines.append("Đã trừ phí 0.05%, trượt giá 0.05%, funding giả định cho lệnh long. Đòn bẩy làm lỗ nhanh "
                     "như lãi; không phải khuyến nghị đầu tư.")
        return "\n".join(lines)

    def status_text(self) -> str:
        days = [str(self.load_book(s).meta.get("last_day") or "") for s in self.symbols]
        day = min(days) if all(days) else (max(days) or "-")
        real_lines = []
        if self.settings.futures_mode == "signal":
            real = self._real()
            sig = real.get("last_signal") or {}
            if real.get("equity"):
                real_lines.append(f"Tài khoản futures thật: vốn {_num(real['equity'], 2)} USDT "
                                  f"(gốc {_num(real.get('start') or real['equity'], 2)}).")
                if sig:
                    parts = []
                    for sym, t in (sig.get("targets") or {}).items():
                        q = float(t.get("qty") or 0.0)
                        parts.append(f"{_base(sym)} " + (f"{'LONG' if q > 0 else 'SHORT'} {_num(abs(q))}" if q
                                                         else "đứng ngoài"))
                    real_lines.append(f"Mục tiêu ngày {sig.get('day')}: " + (", ".join(parts) or "đứng ngoài"))
            else:
                real_lines.append("Tài khoản futures thật: chưa có vốn - gửi /fvon <số USDT>.")
        return self.report_text(day, None, real_lines)

    def resume(self, coin: str) -> str:
        symbol = coin.upper().replace("/", "")
        symbol = symbol if symbol.endswith("USDT") else symbol + "USDT"
        if symbol not in self.symbols:
            raise ValueError(f"Không có sổ futures {symbol}; đang chạy: {', '.join(self.symbols)}.")
        with self._lock:
            book = self.load_book(symbol)
            if not book.meta.get("halted"):
                return f"{_base(symbol)} đang chạy bình thường."
            reason = book.meta.pop("halted")
            book.meta.pop("halted_on", None)
            book.meta.setdefault("start_capital", book.capital)
            book.capital = round(book.last_nav, 8)          # the goal and the floor count from here
            s = self.settings
            if s.futures_stop_equity and s.futures_capital:
                book.meta["stop_equity"] = round(book.capital * s.futures_stop_equity / s.futures_capital, 4)
            self.save_book(book)
        return (f"▶️ {_base(symbol)} chạy lại ({'sau khi đạt mục tiêu' if reason == 'goal' else 'sau mức dừng'}); "
                f"vốn mốc mới {_num(book.capital, 2)} USDT, lệnh mới từ lần chạy tới.")

    def next_run_text(self) -> str:
        return (f"Futures ({self.settings.futures_mode}, chiến lược ls_sma {self.settings.futures_rule}): "
                f"{', '.join(_base(s) for s in self.symbols)}; "
                f"chạy mỗi ngày lúc {self.settings.run_time} giờ VN cùng bot spot.")
