"""Prompt integrity: every prompt is about crypto spot, in Vietnamese, under the grounding rules.

The prompts are captured from a full scripted run, so a prompt edited anywhere
in the graph is checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage

from cryptotrader.agents import crypto_context
from cryptotrader.graph import trading_graph
from tests.test_graph_end_to_end import DAY, ScriptedModel, _Client, offline  # noqa: F401 - fixture

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1] / "cryptotrader"
OWNED = [ROOT / "agents", ROOT / "graph", ROOT / "llm_clients", ROOT / "dataflows",
         ROOT / "report_html.py", ROOT / "reporting.py", ROOT / "default_config.py"]

# Vietnamese-equity vocabulary that must not survive the port.
VN_TERMS = re.compile(r"HOSE|HNX|UPCoM|VNINDEX|VN-Index|VN30|T\+2|cổ phiếu|CTCK|\bVND\b|\bATO\b|\bATC\b|"
                      r"Ho_Chi_Minh|DNSE|\bVCI\b|CafeF|lô 100|biên độ trần|khối ngoại|tradingagents")


class Recording(ScriptedModel):
    prompts: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.prompts.append("\n".join(m.content if isinstance(m, BaseMessage) else str(m) for m in messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


def _files():
    for path in OWNED:
        yield from ([path] if path.is_file() else sorted(path.rglob("*.py")))


def test_no_vietnamese_equity_content_is_left_in_the_code():
    hits = [f"{p.relative_to(ROOT)}:{n}: {line.strip()}"
            for p in _files() for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if VN_TERMS.search(line)]
    assert hits == []


@pytest.fixture
def prompts(tmp_path, monkeypatch, offline):  # noqa: F811 - pytest fixture
    model = Recording(structured=False)
    model.prompts.clear()
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(model))
    trading_graph.CryptoTradingGraph(config={"results_dir": str(tmp_path)}).propagate("BTCUSDT", DAY)
    return list(model.prompts)


def test_every_prompt_is_crypto_spot_in_vietnamese(prompts):
    # 4 analysts (2 with tool turns) + bull + bear + RM + trader + 3 risk + PM.
    assert len(prompts) >= 12
    for text in prompts:
        assert not VN_TERMS.search(text), text[:200]
        assert "Vietnamese" in text
        assert "BTCUSDT" in text
        assert crypto_context.NO_LEVERAGE_RULE in text or "no leverage" in text.lower()


def test_decision_prompts_carry_the_fixed_system_action_and_the_no_invent_rule(prompts):
    deciding = [p for p in prompts if "QUYẾT ĐỊNH CỦA HỆ THỐNG" in p]
    assert len(deciding) >= 7      # bull, bear, RM, trader, 3 risk, PM
    for text in deciding:
        assert crypto_context.NO_INVENT_RULE in text
        assert "KHÔNG được đổi" in text


def test_the_market_rules_name_the_crypto_facts():
    rules = crypto_context.market_rules()
    for fact in ("24/7", "00:00 UTC", "07:00 giờ Việt Nam", "0.1%", "LOT_SIZE", "MIN_NOTIONAL", "lưu ký"):
        assert fact in rules
