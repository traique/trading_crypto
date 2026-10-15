"""The report tree of a finished run: Markdown per section, ``complete_report.md`` and one HTML page."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from cryptotrader.agents.rating import run_rating

# The report's sections in order: (heading, folder, [(agent, file, state path)]).
SECTIONS = (
    ("0. Quyết định của hệ thống", "0_system", (
        ("Hệ thống định lượng", "system.md", ("quant_report",)),
    )),
    ("I. Báo cáo của nhóm phân tích", "1_analysts", (
        ("Phân tích kỹ thuật", "market.md", ("market_report",)),
        ("Tâm lý thị trường", "sentiment.md", ("sentiment_report",)),
        ("Tin tức", "news.md", ("news_report",)),
        ("Tokenomics", "tokenomics.md", ("tokenomics_report",)),
    )),
    ("II. Tranh luận của nhóm nghiên cứu", "2_research", (
        ("Phe lạc quan (Bull)", "bull.md", ("investment_debate_state", "bull_history")),
        ("Phe bi quan (Bear)", "bear.md", ("investment_debate_state", "bear_history")),
        ("Trưởng nhóm nghiên cứu", "manager.md", ("investment_plan",)),
    )),
    ("III. Kế hoạch giao dịch", "3_trading", (
        ("Trader", "trader.md", ("trader_investment_plan",)),
    )),
    ("IV. Tranh luận về rủi ro", "4_risk", (
        ("Rủi ro - mạo hiểm", "aggressive.md", ("risk_debate_state", "aggressive_history")),
        ("Rủi ro - thận trọng", "conservative.md", ("risk_debate_state", "conservative_history")),
        ("Rủi ro - trung lập", "neutral.md", ("risk_debate_state", "neutral_history")),
    )),
    ("V. Quyết định cuối cùng", "5_portfolio", (
        ("Quản lý danh mục", "decision.md", ("final_trade_decision",)),
    )),
)


def analyst_names(keys) -> list[str]:
    return list(keys or [])


def report_parts(final_state: dict) -> list[tuple[str, str, list[tuple[str, str, str]]]]:
    """The sections with something to say: (heading, folder, [(agent, file, text)])."""
    parts = []
    for heading, folder, agents in SECTIONS:
        written = []
        for agent, filename, path in agents:
            value = final_state
            for key in path:
                value = (value or {}).get(key) if isinstance(value, dict) else None
            if isinstance(value, str) and value.strip():
                written.append((agent, filename, value.strip()))
        if written:
            parts.append((heading, folder, written))
    return parts


def _header(pair: str, final_state: dict, settings: dict | None) -> str:
    lines = [f"# Báo cáo phân tích {pair}", ""]
    if final_state.get("trade_date"):
        lines.append(f"- Ngày phân tích (UTC): {final_state['trade_date']}")
    if final_state.get("final_trade_decision"):
        lines.append(f"- Hành động của hệ thống: {run_rating(final_state)}")
    lines.append(f"- Tạo lúc: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if settings:
        s = settings.get
        lines.append(f"- trading-crypto {s('version', '?')}: deep {s('deep_think_provider', '?')} "
                     f"{s('deep_think_llm', '?')}, quick {s('quick_think_provider', '?')} {s('quick_think_llm', '?')}")
        lines.append(f"- Chiến lược hệ thống: {s('engine_strategy', '?')}; nhóm phân tích: "
                     f"{', '.join(analyst_names(s('analysts')))}")
    if final_state.get("guard_notes"):
        lines.append("- Output Guard: " + "; ".join(final_state["guard_notes"].splitlines()))
    return "\n".join(lines) + "\n\n"


def write_report_tree(final_state: dict, pair: str, save_path, settings: dict | None = None,
                      html: bool = True) -> Path:
    """Save a finished run's reports under ``save_path``; return the path of complete_report.md."""
    save_path = Path(save_path)
    save_path.mkdir(parents=True, exist_ok=True)
    parts = report_parts(final_state)
    sections = []
    for heading, folder, written in parts:
        (save_path / folder).mkdir(exist_ok=True)
        for _agent, filename, text in written:
            (save_path / folder / filename).write_text(text, encoding="utf-8")
        sections.append(f"## {heading}\n\n" + "\n\n".join(f"### {agent}\n{text}" for agent, _f, text in written))
    md = save_path / "complete_report.md"
    md.write_text(_header(pair, final_state, settings) + "\n\n".join(sections), encoding="utf-8")
    if html:
        from cryptotrader.report_html import render_report

        (save_path / "complete_report.html").write_text(render_report(pair, final_state, settings, parts),
                                                        encoding="utf-8")
    return md
