"""The web page: the bot's dashboard, AI analyses and their reports.

Plain server-rendered HTML (no build step, no JavaScript framework) so the
upload stays small. Every page but /health asks for WEB_PASSWORD (HTTP Basic,
any user name); without it set the pages stay locked. A form posted from
another site (the browser would send the saved password with it) is refused.
Repeated wrong passwords from one client lock it out for a few minutes, except
a browser that logged in before (it carries a signed "known device" cookie): so
strangers sharing the owner's address (a reverse proxy without
TRUSTED_PROXY_IPS) cannot lock the owner out. Error pages never show an
exception's text.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import secrets
import threading
import time
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.exceptions import HTTPException as StarletteHTTPException

from bot import clock, fmt, ui
from bot.analysis import fallback_message, gate_line, provider_status, unavailable_reason
from bot.books import title_of
from bot.checks import check_request
from bot.jobs import Job, JobQueue
from bot.service import quick_text
from bot.settings import Settings
from cryptotrader.market import rules

logger = logging.getLogger(__name__)

_SOURCE_VI = {"web": "Web", "telegram": "Telegram"}
_MODE_VI = {"paper": "Tài khoản ảo", "signal": "Ảo + tín hiệu thật", "off": "Tắt"}

# Module level: the dependency annotations below are strings (postponed
# evaluation), which FastAPI resolves in module scope.
_BASIC = HTTPBasic(auto_error=False)

LOCKED_MESSAGE = (
    "Trang web đang khoá vì chưa đặt WEB_PASSWORD. Thêm WEB_PASSWORD vào file .env "
    "rồi khởi động lại server (bot Telegram vẫn chạy bình thường)."
)
OFF_MESSAGE = "Bot giao dịch đang tắt (TRADE_MODE=off)."


def _page(title: str, body: str, refresh: int | None = None, *, active: str = "",
          status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(ui.page(title, body, active=active, refresh=refresh), status_code=status_code)


def _when(ts: float | None) -> str:
    """An epoch time in Vietnam time, whatever the server's own zone (often UTC)."""
    return clock.vn_time(ts)


class LoginThrottle:
    """Wrong-password counter per client: after ``max_failures`` within ``window``
    seconds that client is refused for ``lockout`` seconds, right password or not.

    In memory only (one process); the table is bounded so a scan of many
    addresses cannot grow it without limit.
    """

    def __init__(self, max_failures: int = 8, window: float = 600.0, lockout: float = 300.0,
                 max_clients: int = 10_000, clock=time.monotonic):
        self.max_failures, self.window, self.lockout = max_failures, window, lockout
        self.max_clients, self.clock = max_clients, clock
        self._failures: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, client: str) -> int:
        """Seconds left on ``client``'s lockout, 0 when it may try."""
        with self._lock:
            until = self._locked.get(client)
            if until is None:
                return 0
            left = until - self.clock()
            if left <= 0:
                del self._locked[client]
                return 0
            return int(left) + 1

    def failure(self, client: str) -> None:
        with self._lock:
            now = self.clock()
            recent = [t for t in self._failures.get(client, []) if now - t < self.window] + [now]
            if len(recent) >= self.max_failures:
                self._failures.pop(client, None)
                self._locked[client] = now + self.lockout
            else:
                self._failures[client] = recent
            if len(self._failures) + len(self._locked) > self.max_clients:
                self._prune(now)

    def success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    def _prune(self, now: float) -> None:
        self._locked = {c: u for c, u in self._locked.items() if u > now}
        self._failures = {c: ts for c, ts in self._failures.items() if ts and now - ts[-1] < self.window}
        while self._failures and len(self._failures) + len(self._locked) > self.max_clients:
            self._failures.pop(next(iter(self._failures)))   # the oldest first seen


DEVICE_COOKIE = "cb_device"
DEVICE_COOKIE_MAX_AGE = 400 * 24 * 3600


def device_token(password: str) -> str:
    """The "known device" cookie value: tied to the password, so changing the password
    forgets every device. Only ever accepted together with the right password."""
    key = hashlib.sha256(b"crypto-bot-device|" + password.encode("utf-8")).digest()
    return hmac.new(key, b"known-device", hashlib.sha256).hexdigest()


def _client(request: Request) -> str:
    # The socket peer. Uvicorn already replaces it with X-Forwarded-For when the
    # request comes from a proxy it trusts (FORWARDED_ALLOW_IPS, 127.0.0.1 by
    # default); the header itself is never trusted here, a client could forge it.
    return request.client.host if request.client else "?"


def _host(value: str) -> str:
    return (urlsplit(value).netloc if "//" in value else value).strip().lower()


def _base(symbol: str) -> str:
    return rules.split_pair(symbol)[0]


def _job_row(job: Job) -> str:
    return (f'<tr><td><a class="sym" href="/jobs/{html.escape(job.id)}">{html.escape(_base(job.ticker))}</a></td>'
            f'<td class="num">{html.escape(job.date)}</td><td>{ui.action_badge(job.action)}</td>'
            f"<td>{ui.status_badge(job.status)}</td>"
            f'<td class="muted small">{_SOURCE_VI.get(job.source, html.escape(job.source))}</td>'
            f'<td class="muted small num">{_when(job.created)}</td></tr>')


def _side_badge(side: str) -> str:
    return ui.action_badge("BUY" if side == "BUY" else "SELL")


def _target_chips(target) -> str:
    if target is None:
        return '<span class="muted small">Chiến lược đặt lệnh trực tiếp (không theo tỷ trọng).</span>'
    if not target:
        return '<div class="chips"><span class="chip">100% USDT</span></div>'
    chips = [f'<span class="chip">{html.escape(_base(s))} {w * 100:.0f}%</span>'
             for s, w in sorted(target.items(), key=lambda kv: -kv[1])]
    rest = 1 - sum(target.values())
    if rest > 0.005:
        chips.append(f'<span class="chip">USDT {rest * 100:.0f}%</span>')
    return '<div class="chips">' + "".join(chips) + "</div>"


def _positions_table(book, closes: dict | None = None) -> str:
    rows = []
    for p in book.positions.values():
        last = (closes or {}).get(p.symbol) or p.last_price
        change = (last / p.avg_price - 1) * 100 if p.avg_price and last else None
        rows.append(f'<tr><td class="sym">{html.escape(_base(p.symbol))}</td><td class="num">{fmt.qty(p.qty)}</td>'
                    f'<td class="num">{fmt.price(p.avg_price)}</td><td class="num">{fmt.price(last)}</td>'
                    f'<td class="num {ui.tone(change)}">{fmt.pct(change)}</td>'
                    f'<td class="num">{fmt.price(p.stop) if p.stop else "—"}</td></tr>')
    return ui.table([("Coin", ""), ("Số lượng", "num"), ("Giá vốn", "num"), ("Giá", "num"), ("Lãi/lỗ", "num"),
                     ("Stop", "num")], rows, "Không giữ coin nào (100% USDT).")


def _fills_table(book, limit: int = 12) -> str:
    rows = [f'<tr><td class="num muted">{html.escape(f.date)}</td><td>{_side_badge(f.side)}</td>'
            f'<td class="sym">{html.escape(_base(f.symbol))}</td><td class="num">{fmt.qty(f.qty)}</td>'
            f'<td class="num">{fmt.price(f.price)}</td><td class="num muted">{fmt.usdt(f.fee, 4)}</td>'
            f'<td class="num {ui.tone(f.pnl)}">{fmt.usdt(f.pnl) if f.pnl is not None else "—"}</td>'
            f'<td class="muted small">{html.escape(f.reason)}</td></tr>'
            for f in reversed(book.fills[-limit:])]
    return ui.table([("Ngày", ""), ("Chiều", ""), ("Coin", ""), ("Số lượng", "num"), ("Giá", "num"),
                     ("Phí USDT", "num"), ("Lãi/lỗ", "num"), ("Lý do", "")], rows, "Chưa có giao dịch.")


def _book_card(spec, book, *, accent: str) -> str:
    st = book.stats()
    nav_sub = f"từ {html.escape(book.start_date)}" if book.start_date else "chưa chạy ngày nào"
    kpis = "".join([
        ui.kpi("NAV (USDT)", ui.money_short(st["nav"]), nav_sub),
        ui.kpi("Lợi nhuận", ui.pct(st["return_pct"]),
               f"BTC {ui.pct(st['benchmark_pct'])}" if st["benchmark_pct"] is not None else "", ui.tone(st["return_pct"])),
        ui.kpi("Sụt giảm tối đa", ui.pct(st["max_drawdown_pct"], signed=False), "từ đỉnh NAV"),
        ui.kpi("Phí đã trả", fmt.usdt(st["fees_paid"]), f"{st['buys']} lệnh mua"),
    ])
    orders = [f'<tr><td>{_side_badge(o.side)}</td><td class="sym">{html.escape(_base(o.symbol))}</td>'
              f'<td class="num">{fmt.qty(o.qty)}</td><td class="num">{fmt.price(o.limit) if o.limit else "giá mở cửa"}</td>'
              f'<td class="muted small">{html.escape(o.reason)}</td></tr>' for o in book.orders]
    body = (
        f'<div class="grid g4">{kpis}</div>'
        '<h2 style="margin:20px 0 10px">Mục tiêu tỷ trọng</h2>' + _target_chips(book.meta.get("target", {}))
        + '<h2 style="margin:20px 0 10px">Đang giữ</h2>' + _positions_table(book)
        + '<h2 style="margin:20px 0 10px">Lệnh cho nến tới</h2>'
        + ui.table([("Chiều", ""), ("Coin", ""), ("Số lượng", "num"), ("Giá", "num"), ("Lý do", "")], orders,
                   "Không có lệnh chờ.")
        + '<h2 style="margin:20px 0 10px">Giao dịch gần đây</h2>' + _fills_table(book)
    )
    return ui.card(html.escape(spec.title), body, sub=html.escape(spec.note), cls=accent)


def _common_start(books) -> str | None:
    """The first day every book with a history has: the date the chart indexes on."""
    starts = [min(h["date"] for h in b.history) for b in books if b.history]
    return max(starts) if starts else None


def _chart(trader, pairs) -> str:
    books = [b for _, b in pairs]
    dates = sorted({h["date"] for b in books for h in b.history})
    # Every line is 100 on the same day (the books may have started on different days):
    # the gaps between the lines after it are returns over the same period.
    base = _common_start(books) or ""

    def indexed(history, key):
        by_day = {h["date"]: h.get(key) for h in history}
        first = next((by_day[d] for d in dates if d >= base and by_day.get(d)), None)
        return [round(by_day[d] / first * 100, 3) if first and by_day.get(d) and d >= base else None for d in dates]

    series = [{"name": spec.title, "color": ui.SERIES_COLORS[i % len(ui.SERIES_COLORS)],
               "values": indexed(book.history, "nav")} for i, (spec, book) in enumerate(pairs)]
    if books:
        bench = max((b.history for b in books), key=len)
        series.append({"name": "BTC", "color": ui.BENCH_COLOR, "values": indexed(bench, "benchmark"), "dashed": True})
    return ui.nav_chart(dates, series, run_time=trader.settings.run_time)


def _real_card(trader) -> str:
    from cryptotrader.trading.ledger import LedgerLoadError

    try:
        real = trader.real()
    except LedgerLoadError as exc:
        return ui.card("Tài khoản thật", ui.alert(str(exc), "err"))
    prices = trader.last_prices()
    closes = {s: float(c) for s, c in (prices.get("closes") or {}).items()}
    nav = real.nav(closes)
    kpis = "".join([
        ui.kpi("USDT", fmt.usdt(real.cash), "theo /von và /khop"),
        ui.kpi("NAV ước tính", fmt.usdt(nav), f"giá đóng cửa {html.escape(prices.get('day') or '-')}"),
        ui.kpi("Theo chiến lược", html.escape(title_of(trader.settings.signal_strategy)), "SIGNAL_STRATEGY"),
    ])
    body = (f'<div class="grid g3">{kpis}</div><h2 style="margin:20px 0 10px">Đang giữ</h2>'
            + _positions_table(real, closes) + '<h2 style="margin:20px 0 10px">Lệnh đã báo khớp</h2>'
            + _fills_table(real))
    return ui.card("Tài khoản thật (chỉ đọc)", body,
                   sub="Bot không bao giờ đặt lệnh: bạn tự đặt trên sàn rồi báo /khop qua Telegram", cls="accent-sa")


def _dashboard(settings: Settings, trader, msg: str) -> str:
    if trader is None or settings.trade_mode == "off":
        return (ui.hero("Bot giao dịch crypto", "Binance spot · cặp USDT")
                + ui.alert(OFF_MESSAGE, "info")
                + ui.card("Phân tích", '<a class="btn btn-primary" href="/phantich">Mở trang phân tích →</a>'))
    pairs = trader.books()
    last = trader.last_report()
    running = trader.running
    run_btn = (
        '<form method="post" action="/bot/run" style="margin:0">'
        f'<button class="btn btn-primary" type="submit"{" disabled" if running else ""}>'
        f'{"Đang chạy…" if running else "▶ Chạy ngay"}</button></form>'
    )
    lead = (f'Chế độ <b>{_MODE_VI.get(settings.trade_mode, "")}</b>'
            + (f' · tín hiệu thật theo <b>{html.escape(title_of(settings.signal_strategy))}</b>'
               if settings.trade_mode == "signal" else "")
            + f' · tự chạy lúc {html.escape(settings.run_time)} giờ VN mỗi ngày (nến ngày đóng 07:00, chạy bù ngày bị lỡ)'
            + (f' · nến gần nhất {html.escape(last["day"])}' if last.get("day") else ""))
    parts = [ui.hero("Bot giao dịch crypto", lead, run_btn), ui.alert(msg, "info")]
    base = _common_start([b for _, b in pairs])
    parts.append(ui.card("So sánh các chiến lược", _chart(trader, pairs),
                         sub="NAV chỉ số hoá về 100 tại ngày " + html.escape(base or "đầu tiên")
                             + " (ngày đầu tiên chung của các tài khoản), cùng BTC; đã trừ phí và trượt giá giả định"))
    accents = ("accent-peach", "accent-factor", "accent-sa")
    cards = [_book_card(spec, book, accent=accents[i % len(accents)]) for i, (spec, book) in enumerate(pairs)]
    if settings.trade_mode == "signal":
        cards.append(_real_card(trader))
    parts.append('<div class="stack" style="margin-top:16px">' + "".join(cards) + "</div>")
    if last.get("text"):
        parts.append('<div style="margin-top:16px">'
                     + ui.card(f"Báo cáo nến {html.escape(last['day'])}",
                               f'<pre class="report">{html.escape(last["text"])}</pre>',
                               sub=f"Gửi lúc {html.escape(str(last.get('at', '')).replace('T', ' '))}")
                     + "</div>")
    return "".join(parts)


def create_app(settings: Settings, jobs: JobQueue, trader=None) -> FastAPI:
    app = FastAPI(title="Crypto Bot", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if getattr(request.state, "remember_device", False) and settings.web_password:
            response.set_cookie(DEVICE_COOKIE, device_token(settings.web_password), max_age=DEVICE_COOKIE_MAX_AGE,
                                httponly=True, samesite="strict", secure=request.url.scheme == "https")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    throttle = LoginThrottle()
    app.state.login_throttle = throttle

    def guard(request: Request,
              credentials: Annotated[HTTPBasicCredentials | None, Depends(_BASIC)] = None) -> None:
        if not settings.web_password:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, LOCKED_MESSAGE)
        client = _client(request)
        known = secrets.compare_digest(request.cookies.get(DEVICE_COOKIE, "").encode("utf-8"),
                                       device_token(settings.web_password).encode("utf-8"))
        wait = throttle.retry_after(client)
        if wait and not known:      # a browser that logged in before is never locked out by others
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                f"Sai mật khẩu quá nhiều lần. Thử lại sau {wait // 60 + 1} phút.",
                                headers={"Retry-After": str(wait)})
        if credentials is None:   # the browser's first request, before it asks for the password
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})
        if not secrets.compare_digest(credentials.password.encode("utf-8"), settings.web_password.encode("utf-8")):
            throttle.failure(client)
            logger.warning("Web: wrong password from %s", client)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})
        throttle.success(client)
        if not known:
            request.state.remember_device = True

    def same_origin(request: Request) -> None:
        """Refuse a form posted from another site (CSRF): Basic auth rides along with it."""
        allowed = {_host(h) for h in (
            request.headers.get("host", ""),
            request.headers.get("x-forwarded-host", "").split(",")[0],
            settings.public_url,
        ) if h}
        origin = request.headers.get("origin")
        if origin is not None:
            if origin == "null" or _host(origin) not in allowed:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Yêu cầu từ trang khác bị từ chối.")
            return
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Yêu cầu từ trang khác bị từ chối.")
        referer = request.headers.get("referer")
        if referer and _host(referer) not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Yêu cầu từ trang khác bị từ chối.")

    post_guard = [Depends(guard), Depends(same_origin)]

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True, "time": time.time()})

    @app.get("/", dependencies=[Depends(guard)])
    def index(msg: str = "") -> HTMLResponse:
        running = trader is not None and settings.trade_mode != "off" and trader.running
        return _page("Tổng quan", _dashboard(settings, trader, msg), active="home", refresh=15 if running else None)

    @app.post("/bot/run", dependencies=post_guard)
    def bot_run():
        if trader is None or settings.trade_mode == "off":
            raise HTTPException(404, OFF_MESSAGE)
        return RedirectResponse(f"/?msg={quote(trader.start_run())}", status_code=303)

    @app.get("/phantich", dependencies=[Depends(guard)])
    def analysis_page(error: str = "") -> HTMLResponse:
        rows = [_job_row(j) for j in jobs.recent(30)]
        providers = []
        for p in provider_status():
            state = (f'<span class="badge b-watch">nghỉ {int(p.get("resting_seconds") or 0)}s</span> '
                     f'<span class="muted small">{html.escape(str(p.get("last_error") or ""))}</span>'
                     if p.get("resting_seconds") else '<span class="badge b-ok">sẵn sàng</span>')
            providers.append(f'<tr><td><b>{html.escape(str(p.get("label", "")))}</b></td>'
                             f'<td class="muted small">{html.escape(str(p.get("model") or ""))}</td><td>{state}</td></tr>')
        reason = unavailable_reason()
        final = clock.final_day()
        ask = f"""
<form method="post" action="/analyze">
  <div class="row">
    <div class="fld"><label class="f" for="ticker">Coin</label>
      <input class="ticker" id="ticker" name="ticker" placeholder="BTC" required maxlength="24" autocomplete="off" autocapitalize="characters"></div>
    <div class="fld"><label class="f" for="date">Ngày (UTC)</label>
      <input id="date" name="date" type="date" value="{final}" max="{final}"></div>
  </div>
  <div class="row" style="margin-top:12px">
    <button class="btn btn-primary" type="submit"{" disabled" if reason else ""}>Phân tích bằng AI</button>
    <button class="btn btn-ghost" type="submit" formaction="/quick">Xem nhanh</button>
  </div>
  <p class="muted small" style="margin:10px 0 0">Xem nhanh ra trong vài giây, không tốn quota AI. Phân tích AI mất vài phút.
  Dữ liệu là nến ngày đã đóng (00:00 UTC = 07:00 giờ VN).</p>
</form>"""
        body = (
            ui.hero("Phân tích coin", "Binance spot · cặp USDT. Số liệu từ nến ngày đã đóng; các agent AI diễn giải.")
            + ui.alert(error, "err")
            + (ui.alert(f"Phân tích AI chưa dùng được: {reason}. Vẫn dùng được Xem nhanh.", "info") if reason else "")
            + '<div class="grid g2">'
            + ui.card("Một coin", ask)
            + ui.card("Nhà cung cấp AI", ui.table([("Nhà cung cấp", ""), ("Model", ""), ("Trạng thái", "")], providers,
                                                  "Chưa cấu hình nhà cung cấp AI nào.")
                      + (f'<p class="muted small" style="margin:10px 14px">{html.escape(gate_line())}</p>'
                         if gate_line() else ""),
                      sub="Theo thứ tự dự phòng", flush=True)
            + "</div>"
            + '<div style="margin-top:16px">'
            + ui.card("Lượt phân tích gần đây",
                      ui.table([("Coin", ""), ("Ngày", "num"), ("Kết luận", ""), ("Trạng thái", ""), ("Nguồn", ""),
                                ("Tạo lúc", "num")], rows, "Chưa có lượt phân tích nào."),
                      sub="Trang tự tải lại khi có lượt đang chạy" if jobs.is_active() else "", flush=True)
            + "</div>"
        )
        return _page("Phân tích", body, refresh=20 if jobs.is_active() else None, active="analysis")

    @app.post("/analyze", dependencies=post_guard)
    def analyze(ticker: str = Form(...), date: str = Form("")):
        try:
            pair, day = check_request(ticker, date)
            reason = unavailable_reason()
            if reason:
                raise ValueError(fallback_message(pair, reason))
            job = jobs.submit(pair, day, "web")
        except ValueError as exc:   # a bad request, AI unavailable, or QueueFull
            return RedirectResponse(f"/phantich?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/quick", dependencies=post_guard)
    def quick(ticker: str = Form(...), date: str = Form("")):
        try:
            text = quick_text(ticker, date or None, trader=trader)
        except ValueError as exc:
            return RedirectResponse(f"/phantich?error={quote(str(exc))}", status_code=303)
        except Exception:  # noqa: BLE001 - shown to the user, not raised
            # The details go to the log; the page only says what failed.
            logger.exception("Quick view for %r failed", ticker)
            text = "Không tính được số liệu nhanh (lỗi đọc dữ liệu hoặc tính toán). Thử lại sau ít phút."
        code = ticker.strip().upper()[:24]
        body = (ui.hero(f'<span class="tick">{html.escape(code)}</span> · xem nhanh',
                        "Tính bằng code trên nến ngày đã đóng, không gọi AI.",
                        '<a class="btn btn-ghost" href="/phantich">← Coin khác</a>')
                + ui.card("", f'<pre class="report">{html.escape(text)}</pre>'))
        return _page(f"{code} - xem nhanh", body, active="analysis")   # page() escapes the title

    @app.get("/jobs/{job_id}", dependencies=[Depends(guard)])
    def job_page(job_id: str) -> HTMLResponse:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Không có lượt phân tích này")
        live = job.status in ("queued", "quick", "running")
        right = (f'<a class="btn btn-primary" href="/reports/{html.escape(job.id)}">Báo cáo đầy đủ →</a>'
                 if job.html_path else '<a class="btn btn-ghost" href="/phantich">← Phân tích</a>')
        parts = [ui.hero(f'<span class="tick">{html.escape(_base(job.ticker))}</span> {ui.action_badge(job.action)}',
                         f"Nến ngày {html.escape(job.date)} (UTC) · {_SOURCE_VI.get(job.source, '')}", right),
                 ui.stepper(job.status)]
        if job.status == "queued":
            parts.append(ui.alert(f"Còn {jobs.position(job.id)} lượt chờ phía trước. Trang tự tải lại.", "info"))
        if job.status == "running":
            parts.append(ui.alert("Các agent đang phân tích (thường vài phút). Trang tự tải lại.", "info"))
        if job.note:
            parts.append(ui.alert(job.note, "info"))
        if job.error:
            parts.append(ui.alert(job.error, "err"))
        blocks = []
        if job.quick_summary:
            blocks.append(ui.card("Số liệu nhanh", f'<pre class="report">{html.escape(job.quick_summary)}</pre>',
                                  sub="Tính bằng code, không dùng AI", cls="accent-peach"))
        if job.final_decision:
            blocks.append(ui.card("Kết luận của các agent", f'<pre class="report">{html.escape(job.final_decision)}</pre>',
                                  cls="accent-sa"))
        if job.guard_notes:
            blocks.append(ui.card("Kiểm tra đầu ra", f'<pre class="report">{html.escape(job.guard_notes)}</pre>'))
        if blocks:
            parts.append('<div class="stack" style="margin-top:16px">' + "".join(blocks) + "</div>")
        return _page(f"{_base(job.ticker)} {job.date}", "".join(parts), refresh=15 if live else None, active="analysis")

    @app.get("/reports/{job_id}", dependencies=[Depends(guard)])
    def report(job_id: str):
        job = jobs.get(job_id)
        if job is None or job.html_path is None:
            raise HTTPException(404, "Chưa có báo cáo")
        return FileResponse(job.html_path, media_type="text/html; charset=utf-8",
                            headers={"Content-Security-Policy": "script-src 'none'"})

    # Starlette's class: also the 404/405 the router raises for unknown paths/methods.
    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 401:
            return HTMLResponse("Cần mật khẩu", status_code=401, headers=exc.headers)
        message = {"Not Found": "Không tìm thấy trang.",
                   "Method Not Allowed": "Phương thức không được hỗ trợ."}.get(exc.detail, str(exc.detail))
        title = {503: "Trang web đang khoá", 429: "Tạm khoá đăng nhập"}.get(exc.status_code, "Có lỗi")
        body = (ui.hero(title, "", '<a class="btn btn-ghost" href="/">← Trang chính</a>')
                + ui.alert(message, "err"))
        return HTMLResponse(ui.page("Lỗi", body), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    def bad_form(request: Request, exc: RequestValidationError):
        body = (ui.hero("Có lỗi", "", '<a class="btn btn-ghost" href="/">← Trang chính</a>')
                + ui.alert("Thiếu hoặc sai dữ liệu trong biểu mẫu.", "err"))
        return _page("Lỗi", body, status_code=422)

    @app.exception_handler(Exception)
    def server_error(request: Request, exc: Exception):
        # Never a traceback or an exception text on the page; Starlette re-raises
        # the exception after this response, so the server still logs it.
        body = (ui.hero("Có lỗi", "", '<a class="btn btn-ghost" href="/">← Trang chính</a>')
                + ui.alert("Lỗi máy chủ khi xử lý yêu cầu. Chi tiết đã ghi vào log.", "err"))
        return _page("Lỗi", body, status_code=500)

    return app
