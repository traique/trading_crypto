"""The report as one self-contained HTML page.

The page loads nothing and runs nothing: styles are inline, there is no
script, and a Content-Security-Policy forbids any fetch. Report text is
rendered as CommonMark with raw HTML and images off, so nothing a model wrote
can inject markup or load a remote asset.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from markdown_it import MarkdownIt

from cryptotrader.agents.rating import run_rating
from cryptotrader.data.symbols import base_asset
from cryptotrader.reporting import analyst_names

_TONE = {"BUY": "up", "SELL": "down", "HOLD": "level", "NO_TRADE": "level"}
_ACTION_VI = {"BUY": "MUA", "SELL": "BÁN", "HOLD": "GIỮ", "NO_TRADE": "ĐỨNG NGOÀI"}

_CSS = """
:root {
  --paper: #faf5ef; --ink: #2a211b; --muted: #75665a; --rule: #eaddd0; --wash: #f6eee5;
  --accent: #a44a1a; --up: #2e7d3a; --down: #b83a2e; --level: #75665a; --edge: #dccab8; --mark: #9a8b80;
  --serif: Charter, "Bitstream Charter", "Sitka Text", Cambria, Georgia, serif;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #14100d; --ink: #f5ece3; --muted: #a08f82; --rule: #3a3029; --wash: #28211c;
    --accent: #f2a47c; --up: #86c98a; --down: #f08f84; --level: #a08f82; --edge: #4a3d34; --mark: #7e6f64;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font: 17px/1.62 var(--serif); font-kerning: normal;
}
.page { max-width: 46rem; margin: 0 auto; padding: 3.5rem 1.5rem 6rem; }
main h2, main h3 { scroll-margin-top: 1rem; }
a { color: var(--accent); text-underline-offset: 0.15em; }
h1, h2, h3, h4 { text-wrap: balance; }
a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.masthead { padding-bottom: 1.75rem; border-bottom: 1px solid var(--rule); }
.masthead h1 {
  margin: 0; font: 600 2.6rem/1.1 var(--sans); letter-spacing: -0.02em;
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.2em 0.5em;
}
.masthead .rating { font-weight: 500; }
.rating.up { color: var(--up); } .rating.down { color: var(--down); } .rating.level { color: var(--level); }
.rating.review { color: var(--muted); font-style: italic; }
.masthead .date { margin: 0.55rem 0 1.5rem; font: 1rem/1.4 var(--sans); color: var(--muted); }
.run { margin: 0; display: grid; grid-template-columns: max-content 1fr; gap: 0.45rem 1.25rem;
  font: 0.85rem/1.5 var(--sans); }
.run > div { display: contents; }
.run dt { color: var(--muted); }
.run dd { margin: 0; overflow-wrap: anywhere; }
.run .note dd { color: var(--down); }
.field { display: inline-block; margin: 0 0.15rem 0.15rem 0; padding: 0 0.4rem; border: 1px solid var(--edge);
  border-radius: 4px; background: var(--wash); font: 0.8rem/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.run .group { display: inline-block; margin-right: 0.9rem; white-space: nowrap; }
.run .group > .name { color: var(--muted); margin-right: 0.3rem; }
.run .then { color: var(--muted); margin: 0 0.2rem; }

/* The sections: listed under the header on narrow screens. On wide ones, a
   quiet rail centred on the left edge (numerals for the sections, short bars
   for their agents) that opens into the full list when pointed at or focused. */
.contents { margin: 2.25rem 0 0; font: 0.9rem/1.45 var(--sans); }
.contents ul { margin: 0; padding: 0; list-style: none; }
.contents > ul > li { margin: 0 0 0.75rem; }
.contents ul ul { margin-top: 0.2rem; padding-left: 1.6rem; font-size: 0.85rem; }
.contents ul ul li { margin: 0.1rem 0; }
.contents a { display: flex; align-items: center; gap: 0.6rem; color: var(--muted); text-decoration: none; }
.contents > ul > li > a { color: var(--ink); }
.contents .num { min-width: 1.6rem; color: var(--muted); font-variant-numeric: tabular-nums; }
.contents .bar { display: none; }
.contents a:hover .label, .contents a:focus-visible .label { color: var(--accent); text-decoration: underline; }
.to-top { position: fixed; right: 1rem; bottom: calc(1rem + env(safe-area-inset-bottom));
  padding: 0.25rem 0.6rem; border: 1px solid var(--edge); border-radius: 4px; background: var(--paper);
  color: var(--muted); font: 0.8rem/1.4 var(--sans); text-decoration: none; }

@media (min-width: 60rem) {
  .contents { position: fixed; left: 1rem; top: 50%; transform: translateY(-50%); z-index: 1;
    max-height: 86vh; overflow-y: auto; overscroll-behavior: contain; margin: 0;
    padding: 0.7rem 0.9rem; border: 1px solid transparent; border-radius: 6px;
    transition: background-color 0.15s, border-color 0.15s, box-shadow 0.15s; }
  .contents > ul > li { margin: 0 0 0.35rem; }
  .contents ul ul { margin-top: 0; padding-left: 0; }
  .contents ul ul li { margin: 0; }
  /* Marks reach 3:1 contrast and rows 24px, the WCAG 2.2 floors for a control's
     visual cue and its pointer target. */
  .contents a { min-height: 1.5rem; }
  .contents .num { min-width: 1.4rem; }
  .contents .bar { display: block; flex: none; width: 1.25rem; height: 2px; margin-left: 0.2rem;
    border-radius: 1px; background: var(--mark); }
  .contents .label { max-width: 0; opacity: 0; overflow: hidden; white-space: nowrap;
    transition: opacity 0.15s, max-width 0.2s; }
  .contents:hover, .contents:focus-within { background: var(--paper); border-color: var(--rule);
    box-shadow: 0 6px 24px rgb(0 0 0 / 0.08); }
  .contents:hover .label, .contents:focus-within .label { max-width: 18rem; opacity: 1; }
  .contents a:hover .bar, .contents a:focus-visible .bar { background: var(--accent); }
  .to-top { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .contents, .contents .label { transition: none; }
}

/* The maker's credit: one quiet line at the foot. */
.credit { margin-top: 4.5rem; padding-top: 1.25rem; border-top: 1px solid var(--rule);
  color: var(--muted); font: 0.8rem/1.4 var(--sans); }
.credit a { color: inherit; text-decoration: none; }
.credit a.brand { color: var(--accent); }
.credit a:hover, .credit a:focus-visible { text-decoration: underline; }

/* Three levels of separation: the five groups, the agents within a group, and
   breaks an agent draws inside its own text. */
main h2 {
  margin: 4.5rem 0 0.25rem; padding-top: 1.5rem; border-top: 2px solid var(--ink);
  font: 600 1.45rem/1.3 var(--sans); letter-spacing: -0.01em;
}
main section:first-child h2 { margin-top: 3rem; }
main h3 { margin: 2.75rem 0 0.75rem; padding-top: 1.1rem; border-top: 1px solid var(--rule);
  font: 600 1.15rem/1.35 var(--sans); color: var(--accent); }
main h2 + h3 { margin-top: 1.25rem; padding-top: 0; border-top: 0; }
main h4, main h5, main h6 { margin: 1.75rem 0 0.5rem; font: 600 1rem/1.4 var(--sans); }
main h5, main h6 { color: var(--muted); }
main p, main ul, main ol { margin: 0 0 1rem; }
main li { margin: 0.25rem 0; }
main strong { font-weight: 650; }
main blockquote { margin: 1.25rem 0; padding: 0.1rem 0 0.1rem 1rem; border-left: 3px solid var(--rule);
  color: var(--muted); }
main code { font: 0.88em/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: var(--wash); padding: 0.1em 0.3em; border-radius: 3px; }
main pre { background: var(--wash); padding: 0.9rem 1rem; border-radius: 4px; overflow-x: auto; }
main pre code { background: none; padding: 0; }
main hr { width: 3rem; margin: 2rem auto; border: 0; border-top: 1px solid var(--rule); }

.table { overflow-x: auto; margin: 0 0 1.25rem; }
table { border-collapse: collapse; width: 100%; font: 0.86rem/1.45 var(--sans);
  font-variant-numeric: tabular-nums; }
th, td { padding: 0.45rem 0.7rem; text-align: left; vertical-align: top; border-bottom: 1px solid var(--rule); }
th { font-weight: 600; border-bottom-color: var(--muted); }
tbody tr:last-child td { border-bottom: 0; }

@media (max-width: 34rem) {
  body { font-size: 16px; }
  .page { padding: 2rem 1rem 3rem; }
  .masthead h1 { font-size: 2rem; }
  .run { grid-template-columns: 1fr; gap: 0 0; }
  .run dd { margin-bottom: 0.45rem; }
  .run .group { white-space: normal; }
}
@media print {
  :root { --paper: #fff; --ink: #000; --muted: #444; --rule: #bbb; --accent: #000; --wash: #f2f2f2;
    --edge: #bbb; --up: #087a59; --down: #b4372f; --level: #444; }
  body { font-size: 11pt; }
  .table { overflow: visible; }
  .page { max-width: none; padding: 0; }
  .contents, .to-top { display: none; }
  main h2 { break-before: page; border-top: 0; margin-top: 0; }
  main section:first-child h2 { break-before: auto; }
  main h3, main h4 { break-after: avoid; }
  a { color: inherit; text-decoration: none; }
  table { break-inside: avoid; }
}
"""


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")
    md.add_render_rule("table_open", lambda *a: '<div class="table"><table>\n')
    md.add_render_rule("table_close", lambda *a: "</table></div>\n")
    return md


def _render_agent_text(md: MarkdownIt, text: str) -> str:
    """An agent's text, its own headings placed below the agent's heading (h3)."""
    tokens = md.parse(text)
    for token in tokens:
        if token.type in ("heading_open", "heading_close"):
            token.tag = f"h{min(int(token.tag[1]) + 3, 6)}"
    return md.renderer.render(tokens, md.options, {})


def _fields(values) -> str:
    return " ".join(f'<span class="field">{escape(str(v))}</span>' for v in values)


def _run_details(final_state: dict, settings: dict | None) -> list[tuple[str, str]]:
    """The run details as (label, value HTML); every value from the run is escaped."""
    details = [("Tạo lúc", escape(datetime.now().strftime("%Y-%m-%d %H:%M")))]
    if settings:
        s = settings.get
        for tier, label in (("deep", "Mô hình sâu"), ("quick", "Mô hình nhanh")):
            details.append((label, _fields([s(f"{tier}_think_provider") or s("llm_provider", "?"),
                                             s(f"{tier}_think_llm", "?")])))
        details.append(("Nhóm phân tích", _fields(analyst_names(s("analysts")))))
        details.append(("Chiến lược", _fields([s("engine_strategy", "?")])))
        details.append(("Phiên bản", escape(f"trading-crypto {s('version', '?')}")))
    if final_state.get("guard_notes"):
        details.append(("Output Guard", escape("; ".join(final_state["guard_notes"].splitlines()))))
    return details


def render_report(pair: str, final_state: dict, settings: dict | None, parts) -> str:
    """The page: title and system action, what produced the run, contents, sections."""
    md = _markdown()
    rating = run_rating(final_state) if final_state.get("final_trade_decision") else ""
    tone = _TONE.get(rating, "review")
    date = final_state.get("trade_date", "")
    shown = f"{base_asset(pair)}/USDT" if pair.endswith("USDT") else pair
    title = f'<span class="ticker">{escape(shown)}</span>'
    if rating:
        label = f"{rating} ({_ACTION_VI[rating]})" if rating in _ACTION_VI else rating
        title += f' <span class="rating {tone}">{escape(label)}</span>'
    details = "".join(f'<div class="detail"><dt>{escape(label)}</dt><dd>{value}</dd></div>'
                      for label, value in _run_details(final_state, settings))
    contents, body = [], []
    for n, (heading, _folder, written) in enumerate(parts, 1):
        agents = "".join(
            f'<li><a href="#s{n}-{m}"><span class="bar"></span> <span class="label">{escape(agent)}</span></a></li>'
            for m, (agent, _f, _t) in enumerate(written, 1))
        numeral, _, name = heading.partition(". ")
        contents.append(f'<li><a href="#s{n}"><span class="num">{escape(numeral)}</span> '
                        f'<span class="label">{escape(name)}</span></a><ul>{agents}</ul></li>')
        body.append(f'<section id="s{n}"><h2>{escape(heading)}</h2>')
        for m, (agent, _filename, text) in enumerate(written, 1):
            body.append(f'<h3 id="s{n}-{m}">{escape(agent)}</h3>\n{_render_agent_text(md, text)}')
        body.append("</section>")
    page_title = " ".join(x for x in (shown, date, rating) if x)
    return f"""<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{escape(page_title)}: báo cáo phân tích</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page" id="top">
<header class="masthead">
<h1>{title}</h1>
<p class="date">{escape(f"Phân tích ngày {date} (UTC)" if date else "Phân tích")}</p>
<dl class="run">{details}</dl>
</header>
<nav class="contents" aria-label="Mục lục"><ul>{"".join(contents)}</ul></nav>
<main>
{"".join(body)}
</main>
<footer class="credit">trading-crypto - dựa trên TradingAgents (Tauric Research), giấy phép Apache-2.0. Không phải khuyến nghị đầu tư.</footer>
</div>
<a class="to-top" href="#top">Lên đầu</a>
</body>
</html>
"""
