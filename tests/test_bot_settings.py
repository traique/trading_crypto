"""Settings, the book registry and what production installs."""

import tomllib
from pathlib import Path

import pytest

from bot import books, settings as settings_module
from tests import test_bot_support as support

settings = support.settings

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def test_the_trade_time_is_never_before_the_daily_candle_is_final(settings, monkeypatch):
    assert settings_module.MIN_TRADE_TIME == "07:05"          # 00:05 UTC in Vietnam time
    assert settings.trade_time == "07:10" == settings.run_time
    monkeypatch.setenv("TRADE_TIME", "6:30")
    assert settings_module.load().trade_time == "07:05"
    monkeypatch.setenv("TRADE_TIME", "soon")
    assert settings_module.load().trade_time == "07:10"
    monkeypatch.setenv("TRADE_TIME", "9:5")
    assert settings_module.load().trade_time == "09:05"
    monkeypatch.setenv("TRADE_TIME", "23:30")
    assert settings_module.load().trade_time == "23:30"


def test_settings_from_the_environment(settings, monkeypatch, tmp_path):
    import os

    assert settings.data_dir == (tmp_path / "data").resolve()
    assert os.environ["CRYPTO_DATA_DIR"] == str(settings.data_dir)     # the core library writes there too
    assert settings.telegram_allowed_ids == frozenset({111, 222}) == settings.telegram_report_chat_ids
    assert settings.trade_mode == "paper" and settings.signal_strategy == "btc_sma_ens"
    assert settings.paper_capital == 10_000
    monkeypatch.setenv("TRADE_MODE", "LIVE")                  # unknown: the safe default
    monkeypatch.setenv("SIGNAL_STRATEGY", "btc_buy_hold")
    monkeypatch.setenv("PAPER_CAPITAL", "5")
    monkeypatch.setenv("TELEGRAM_REPORT_CHAT_IDS", "-100123; 5")
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com/")
    s = settings_module.load()
    assert s.trade_mode == "paper" and s.signal_strategy == "btc_buy_hold"
    assert s.paper_capital == settings_module.MIN_PAPER_CAPITAL
    assert s.telegram_report_chat_ids == frozenset({-100123, 5}) and s.public_url == "https://bot.example.com"
    monkeypatch.setenv("SIGNAL_STRATEGY", "martingale")
    assert settings_module.load().signal_strategy == books.DEFAULT_SIGNAL_STRATEGY


def test_every_book_has_a_registered_strategy_and_a_safe_name():
    from cryptotrader.strategies import REGISTRY

    assert [s.name for s in books.BOOKS][:3] == ["btc_sma_ens", "btc_trend", "btc_buy_hold"]
    assert books.get_book("btc_trend").benchmark and books.get_book("btc_buy_hold").benchmark
    assert all(s.name in REGISTRY for s in books.BOOKS)
    assert books.DEFAULT_SIGNAL_STRATEGY in books.book_names()
    assert not books.get_book(books.DEFAULT_SIGNAL_STRATEGY).benchmark
    with pytest.raises(ValueError):
        books.BookSpec("../evil", "x")
    assert books.get_book("btc_buy_hold").strategy().name == "btc_buy_hold"


def test_a_book_passes_its_parameters_to_the_strategy():
    spec = books.BookSpec("btc_trend", "Xu hướng nhanh", params=(("sma_days", 50),))
    assert spec.strategy().sma_days == 50 and spec.file_name == "paper_btc_trend.json"


def test_the_botkeep_requirements_match_the_project_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    wanted = set(project["dependencies"]) | set(project["optional-dependencies"]["bot"])
    lines = (ROOT / "deploy" / "botkeep" / "requirements.txt").read_text(encoding="utf-8").splitlines()
    pinned = {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}
    assert wanted <= pinned, f"missing in deploy/botkeep/requirements.txt: {sorted(wanted - pinned)}"


def test_cost_and_source_knobs_in_dotenv_are_honoured(settings, monkeypatch, tmp_path):
    """The core library is imported before .env is loaded: load() must read them again."""
    import os

    from cryptotrader.data import binance, okx
    from cryptotrader.market import rules
    from cryptotrader.trading.ledger import Ledger

    names = ("CRYPTO_TAKER_FEE_PCT", "CRYPTO_SLIPPAGE_PCT", "CRYPTO_BINANCE_DATA_URL", "CRYPTO_OKX_URL",
             "TRUSTED_PROXY_IPS", "FORWARDED_ALLOW_IPS")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    for module, attr in ((rules, "TAKER_FEE_PCT"), (rules, "SLIPPAGE_PCT"), (binance, "BASE_URL"), (okx, "BASE_URL")):
        monkeypatch.setattr(module, attr, getattr(module, attr))   # restored after the test
    (tmp_path / ".env").write_text("CRYPTO_TAKER_FEE_PCT=0.075\nCRYPTO_SLIPPAGE_PCT=0.05\n"
                                   "CRYPTO_BINANCE_DATA_URL=https://mirror.example/\n"
                                   "TRUSTED_PROXY_IPS=10.0.0.1\n", encoding="utf-8")
    try:
        loaded = settings_module.load(tmp_path / ".env")
        assert rules.TAKER_FEE_PCT == 0.075 and rules.SLIPPAGE_PCT == 0.05
        book = Ledger(cash=100.0, start_cash=100.0)
        assert book.fee_pct == 0.075 and book.slippage_pct == 0.05
        assert binance.BASE_URL == "https://mirror.example"
        assert loaded.trusted_proxy_ips == "10.0.0.1"
    finally:
        for name in names:
            os.environ.pop(name, None)
