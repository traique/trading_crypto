"""The web page: password, lockout, CSRF, error pages, the dashboard and the analysis pages.

No network: the market is faked, the AI graph is either missing or a stand-in.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bot import web
from bot.jobs import JobQueue
from bot.trader import TradingBot
from bot.web import LoginThrottle, _when, create_app
from tests import test_bot_support as support

settings, at, market = support.settings, support.at, support.market

pytestmark = pytest.mark.unit

PASSWORD = "s3cret"


@pytest.fixture
def jobs(settings):
    return JobQueue(settings)


@pytest.fixture
def app_parts(settings, jobs, market):
    object.__setattr__(settings, "web_password", PASSWORD)
    trader = TradingBot(settings)
    app = create_app(settings, jobs, trader=trader)
    client = TestClient(app)
    client.auth = ("u", PASSWORD)
    return app, client, trader


@pytest.fixture
def client(app_parts):
    return app_parts[1]


def test_times_on_the_page_are_vietnam_time():
    ts = datetime(2026, 10, 13, 1, 30, tzinfo=UTC).timestamp()
    assert _when(ts) == "13/10 08:30" and _when(None) == ""


def test_a_password_protects_every_page_but_health(app_parts):
    _, client, _ = app_parts
    for path in ("/", "/phantich", "/jobs/x", "/reports/x"):
        assert client.get(path, auth=None).status_code == 401
        assert client.get(path, auth=("u", "wrong")).status_code == 401
    assert client.post("/bot/run", auth=None).status_code == 401
    assert client.get("/health", auth=None).json()["ok"] is True


def test_without_a_password_the_web_stays_locked(settings, jobs):
    client = TestClient(create_app(settings, jobs))
    page = client.get("/", auth=("u", ""))
    assert page.status_code == 503 and "WEB_PASSWORD" in page.text
    assert client.get("/health").status_code == 200


def test_a_form_posted_from_another_site_is_refused(client):
    for headers in ({"origin": "https://evil.example"}, {"origin": "null"}, {"sec-fetch-site": "cross-site"},
                    {"referer": "https://evil.example/page"}):
        response = client.post("/bot/run", headers=headers, follow_redirects=False)
        assert response.status_code == 403 and "trang khác" in response.text
    ok = client.post("/quick", data={"ticker": "BTC"}, headers={"origin": "http://testserver"})
    assert ok.status_code == 200


def test_wrong_passwords_lock_the_client_out_for_a_while(app_parts):
    app, client, _ = app_parts
    now = [1000.0]
    throttle = app.state.login_throttle
    throttle.clock = lambda: now[0]
    for _ in range(throttle.max_failures - 1):
        assert client.get("/phantich", auth=("x", "guess")).status_code == 401
    assert client.get("/phantich", auth=None).status_code == 401   # no password yet: not a failure
    assert client.get("/phantich").status_code == 200              # still allowed, and it resets the count
    client.cookies.clear()                                         # a browser that never logged in
    for _ in range(throttle.max_failures):
        client.get("/phantich", auth=("x", "guess"))
    locked = client.get("/phantich")                              # even the right password waits
    assert locked.status_code == 429 and int(locked.headers["Retry-After"]) > 0
    assert "Sai mật khẩu quá nhiều lần" in locked.text
    assert client.post("/quick", data={"ticker": "BTC"}).status_code == 429
    assert client.get("/health").status_code == 200
    now[0] += throttle.lockout + 1
    assert client.get("/phantich").status_code == 200


def test_old_failures_fall_out_of_the_window(app_parts):
    app, client, _ = app_parts
    now = [0.0]
    throttle = app.state.login_throttle
    throttle.clock = lambda: now[0]
    for _ in range(throttle.max_failures * 3):
        client.get("/phantich", auth=("x", "guess"))
        now[0] += throttle.window / (throttle.max_failures - 1) + 1
    assert client.get("/phantich").status_code == 200


def test_the_throttle_table_stays_bounded():
    throttle = LoginThrottle(max_failures=3, max_clients=50)
    for i in range(500):
        throttle.failure(f"10.0.{i // 250}.{i % 250}")
    assert len(throttle._failures) + len(throttle._locked) <= 50


def test_the_dashboard_shows_the_books_against_btc_and_runs_the_bot(app_parts, at):
    _, client, trader = app_parts
    page = client.get("/")
    assert page.status_code == 200 and "Biểu đồ NAV xuất hiện sau 2 ngày" in page.text
    assert "theme-toggle" in page.text and "prefers-color-scheme" in page.text
    response = client.post("/bot/run", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/?msg=")
    import time

    deadline = time.time() + 5
    while (trader.running or not trader.last_report()) and time.time() < deadline:
        time.sleep(0.01)
    from tests.test_bot_support import utc

    at["now"] = utc(2026, 10, 16, 0, 30)
    trader.catch_up("2026-10-15")
    page = client.get("/").text
    for text in ("Xu hướng BTC", "Mua &amp; giữ BTC", "Lệnh cho nến tới", "Giao dịch gần đây", "BTC 100%",
                 'class="chart"', "Báo cáo nến 2026-10-15", "07:10 giờ VN"):
        assert text in page, text
    assert "Tài khoản thật" not in page                        # paper mode


def test_the_dashboard_shows_the_real_book_in_signal_mode(app_parts, settings):
    _, client, trader = app_parts
    object.__setattr__(settings, "trade_mode", "signal")
    trader.set_capital(1234.5)
    page = client.get("/").text
    assert "Tài khoản thật (chỉ đọc)" in page and "1,234.50" in page


def test_trade_mode_off_hides_the_bot(settings, jobs):
    object.__setattr__(settings, "web_password", PASSWORD)
    object.__setattr__(settings, "trade_mode", "off")
    client = TestClient(create_app(settings, jobs, trader=None))
    assert "TRADE_MODE=off" in client.get("/", auth=("u", PASSWORD)).text
    assert client.post("/bot/run", auth=("u", PASSWORD)).status_code == 404


def test_the_quick_page_shows_the_numbers_and_hides_exception_details(client, monkeypatch):
    page = client.post("/quick", data={"ticker": "eth"}).text
    assert "ETH/USDT" in page and "Xu hướng" in page

    def broken(*a, **k):
        raise RuntimeError("/srv/secret/path token=abc")

    monkeypatch.setattr(web, "quick_text", broken)
    page = client.post("/quick", data={"ticker": "BTC"}).text
    assert "Không tính được" in page and "secret" not in page


def test_a_bad_coin_returns_to_the_form_with_the_reason(client):
    response = client.post("/quick", data={"ticker": "USDC"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/phantich?error=")
    assert "stablecoin" in client.get(response.headers["location"]).text


def test_without_the_graph_the_analysis_is_refused_with_the_reason(client, jobs, monkeypatch):
    monkeypatch.setattr(web, "unavailable_reason", lambda: "chưa cài cryptotrader.graph")
    page = client.get("/phantich").text
    assert "Phân tích AI chưa dùng được: chưa cài cryptotrader.graph" in page
    response = client.post("/analyze", data={"ticker": "BTC"})
    assert "/nhanh BTC" in response.text and jobs.recent() == []


def test_a_web_request_queues_a_job_and_shows_it(client, jobs, monkeypatch):
    monkeypatch.setattr(web, "unavailable_reason", lambda: None)
    response = client.post("/analyze", data={"ticker": "btc", "date": "2026-10-13"}, follow_redirects=False)
    (job,) = jobs.recent()
    assert response.headers["location"] == f"/jobs/{job.id}" and job.date == "2026-10-13"
    page = client.get(f"/jobs/{job.id}").text
    assert "BTC" in page and "Xếp hàng" in page and "http-equiv=\"refresh\"" in page
    assert client.get(f"/reports/{job.id}").status_code == 404


def test_a_full_queue_is_explained_on_the_web(client, jobs, settings, monkeypatch):
    monkeypatch.setattr(web, "unavailable_reason", lambda: None)
    object.__setattr__(settings, "max_pending", 1)
    client.post("/analyze", data={"ticker": "BTC"})
    page = client.post("/analyze", data={"ticker": "ETH"}).text
    assert "Hàng đợi đang có 1 lượt chờ" in page


def test_a_finished_report_is_served_without_scripts(client, jobs, settings):
    job = jobs.submit("BTCUSDT", "2026-10-14", "web")
    folder = settings.data_dir / "results" / "reports" / "r"
    folder.mkdir(parents=True)
    (folder / "complete_report.html").write_text("<p>báo cáo</p>", encoding="utf-8")
    job.report_dir, job.status = str(folder), "done"
    response = client.get(f"/reports/{job.id}")
    assert response.status_code == 200 and "báo cáo" in response.text
    assert response.headers["content-security-policy"] == "script-src 'none'"


def test_a_server_error_page_hides_the_exception(settings, jobs):
    object.__setattr__(settings, "web_password", PASSWORD)

    def broken():
        raise RuntimeError("/srv/secret/ledger.json")

    trader = SimpleNamespace(books=broken, settings=settings, running=False, last_report=dict)
    client = TestClient(create_app(settings, jobs, trader=trader), raise_server_exceptions=False)
    response = client.get("/", auth=("u", PASSWORD))
    assert response.status_code == 500 and "secret" not in response.text and "Lỗi máy chủ" in response.text


def test_unknown_pages_and_bad_forms_get_the_vietnamese_error_page(client):
    missing = client.get("/khong-co")
    assert missing.status_code == 404 and "Không tìm thấy trang" in missing.text
    assert missing.headers["X-Frame-Options"] == "DENY"
    bad = client.post("/analyze", data={})
    assert bad.status_code == 422 and "Thiếu hoặc sai dữ liệu" in bad.text and "text/html" in bad.headers["content-type"]


def test_strangers_behind_the_same_proxy_cannot_lock_the_owner_out(app_parts):
    """Behind a reverse proxy every visitor can share one address: the owner's browser,
    which logged in before, keeps working while strangers are locked out."""
    app, owner, _ = app_parts
    throttle = app.state.login_throttle
    assert owner.get("/phantich").status_code == 200
    assert web.DEVICE_COOKIE in owner.cookies                     # remembered as a known device
    stranger = TestClient(app)
    for _ in range(throttle.max_failures):
        stranger.get("/phantich", auth=("x", "guess"))
    assert stranger.get("/phantich", auth=("x", "guess")).status_code == 429
    assert stranger.get("/phantich", auth=("u", PASSWORD)).status_code == 429   # no oracle while locked
    assert owner.get("/phantich").status_code == 200              # same address, still served
    assert owner.get("/phantich", auth=("u", "wrong")).status_code == 401       # the cookie alone is not enough
    # A forged cookie does not bypass the lockout.
    forged = TestClient(app, cookies={web.DEVICE_COOKIE: "0" * 64})
    assert forged.get("/phantich", auth=("u", PASSWORD)).status_code == 429


def test_the_device_token_changes_with_the_password():
    assert web.device_token("a") != web.device_token("b") and web.device_token("a") == web.device_token("a")
