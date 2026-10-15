"""Code-level checks of what the LLM agents wrote, and the report's fixed closing line.

Pure functions, no I/O. Nothing here changes a number the engine decided; it
decides how a text is cleaned and what it may claim:

- ``find_action_conflicts``: action labels or advice that contradict the system action;
- ``find_leverage_advice``: advice to use leverage, margin, futures or to short
  (this project gives spot advice only);
- ``find_unknown_prices``: price-like numbers that appear nowhere in the data the
  agents were given;
- ``clean_analysis_output`` / ``ensure_disclaimer``: one standard disclaimer, last.
"""

from __future__ import annotations

import math
import re

STANDARD_DISCLAIMER = (
    "Thông tin trên chỉ mang tính tham khảo, không phải khuyến nghị đầu tư. Tiền mã hoá biến động "
    "rất mạnh và có thể mất phần lớn hoặc toàn bộ vốn; quyết định cuối cùng và rủi ro thuộc về nhà đầu tư."
)

# ── disclaimers ──
_DISCLAIMER_HINTS = (
    "chỉ là tham khảo", "chỉ mang tính tham khảo", "mang tính tham khảo", "không phải khuyến nghị",
    "không phải là khuyến nghị", "không phải lời khuyên", "tự chịu trách nhiệm", "không đảm bảo kết quả",
    "not financial advice", "not investment advice", "dyor",
)
_ANALYSIS_WORDS_RE = re.compile(
    r"(?<!\w)(mua|bán|giữ|đứng ngoài|cắt lỗ|chốt lời|kết luận|vùng|stop|target|buy|sell|hold|"
    r"no_trade|tỷ trọng|r:r)(?!\w)", re.IGNORECASE)
_DISCLAIMER_MAX_LEN = 300


def _is_disclaimer(block: str) -> bool:
    """A paragraph that is only a disclaimer: short, no digits, no action words, a strong disclaimer phrase."""
    if len(block) > _DISCLAIMER_MAX_LEN or any(ch.isdigit() for ch in block):
        return False
    lowered = block.lower()
    if not any(hint in lowered for hint in _DISCLAIMER_HINTS):
        return False
    return not _ANALYSIS_WORDS_RE.search(block)


def clean_analysis_output(text: str) -> str:
    """Empty paragraphs dropped and a disclaimer the model repeated kept once."""
    if not text:
        return ""
    result: list[str] = []
    seen = False
    for block in re.split(r"\n\s*\n", text.strip()):
        stripped = block.strip()
        if not stripped:
            continue
        if _is_disclaimer(stripped):
            if seen:
                continue
            seen = True
        result.append(stripped)
    return "\n\n".join(result).strip()


def ensure_disclaimer(text: str) -> str:
    """The text ending with exactly one STANDARD_DISCLAIMER (the model's own ones replaced). Idempotent."""
    if not text:
        return STANDARD_DISCLAIMER
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    kept = [b for b in blocks if b != STANDARD_DISCLAIMER and not _is_disclaimer(b)]
    if not kept:
        return STANDARD_DISCLAIMER
    return "\n\n".join(kept) + "\n\n" + STANDARD_DISCLAIMER


# ── actions ──
_ACTION_LABEL_PATTERNS: dict[str, tuple[str, ...]] = {
    "BUY": (r"(?<![\w])BUY(?![\w])", r"(?<![\w])MUA(?![\w])"),
    "SELL": (r"(?<![\w])SELL(?![\w])", r"(?<![\w])BÁN(?![\w])"),
    "HOLD": (r"(?<![\w])HOLD(?![\w])", r"(?<![\w])GIỮ(?![\w])"),
    "NO_TRADE": (r"(?<![\w])NO_TRADE(?![\w])", r"(?<![\w])ĐỨNG NGOÀI(?![\w])"),
}
_ADVICE_PATTERNS: dict[str, tuple[str, ...]] = {
    "BUY": (
        r"(?<!\w)nên (?:mua|gom|giải ngân|vào lệnh|tích luỹ|tích lũy|dca)(?!\w)", r"(?<!\w)canh mua(?!\w)",
        r"(?<!\w)khuyến nghị\s*:?\s*(?:mua|buy|tích luỹ|tích lũy)(?!\w)", r"(?<!\w)mua vào(?!\w)",
        r"(?<!\w)giải ngân(?!\w)", r"(?<!\w)tăng tỷ trọng(?!\w)", r"(?<!\w)có thể mua(?!\w)",
        r"(?<!\w)all[- ]in(?!\w)", r"(?<!\w)strong buy(?!\w)",
    ),
    "SELL": (
        r"(?<!\w)nên (?:bán|thoát hàng|thoát vị thế|chốt lời toàn bộ)(?!\w)",
        r"(?<!\w)khuyến nghị\s*:?\s*(?:bán|sell)(?!\w)", r"(?<!\w)bán ra(?!\w)",
        r"(?<!\w)chốt lời toàn bộ(?!\w)", r"(?<!\w)cắt lỗ ngay(?!\w)", r"(?<!\w)giảm tỷ trọng(?!\w)",
        r"(?<!\w)strong sell(?!\w)",
    ),
    "HOLD": (r"(?<!\w)nên (?:tiếp tục )?(?:giữ|nắm giữ)(?!\w)", r"(?<!\w)tiếp tục nắm giữ(?!\w)"),
}
# Someone else's buying or selling (whales, ETFs, funds, miners) describes the market, not advice.
_THIRD_PARTY_BEFORE = re.compile(
    r"(?:cá voi|whale|quỹ|etf|tổ chức|thợ đào|miner|lực|bên|phe|áp lực|"
    r"tín hiệu|điểm|lệnh|chiều|vùng|mới được|được phép|sổ lệnh|khối lượng)\s*$",
    re.IGNORECASE,
)
_THIRD_PARTY_AFTER = re.compile(r"^\s*(?:ròng|chủ động|mạnh tay|tháo)", re.IGNORECASE)
_NEGATION_WORDS = ("không", "chưa", "đừng", "tránh", "chẳng", "chớ", "not", "no", "never", "cấm")
# Negating phrases that may stand further from the verb, anywhere earlier in its clause
# ("Không có lý do để bán ra lúc này", "Không nên dùng đòn bẩy x10").
_CLAUSE_NEGATIONS = re.compile(
    r"(?<!\w)(?:không có lý do|chẳng có lý do|chưa có lý do|không cần|chưa cần|không nên|chưa nên|đừng|"
    r"tránh|không phải lúc|chưa phải lúc|chưa đến lúc|không khuyến nghị|không khuyên|cấm|tuyệt đối không|"
    r"do not|don't|never|avoid)(?!\w)", re.IGNORECASE)
_CONDITIONAL_WORDS = ("nếu", "khi ", "trường hợp", "kịch bản", "if ", "chỉ khi")
# A condition after the advice, up to the end of its sentence ("... sẽ BÁN nếu thủng SMA").
_TRAILING_CONDITION = re.compile(r"(?<!\w)(?:nếu|khi|một khi|chỉ khi|trường hợp|kịch bản|if|unless|once)(?!\w)",
                                 re.IGNORECASE)
# A view attributed to a debate side ("Phe Bear cho rằng nên bán ..."): the managers are
# asked to summarise each side, which is reporting, not advice.
_DEBATE_SIDE = re.compile(
    r"(?<!\w)(?:bull|bear|phe|bên bò|bên gấu|lạc quan|bi quan|aggressive|conservative|neutral|"
    r"tích cực|thận trọng|trung lập|mạo hiểm|nhà phân tích|researcher|analyst)(?!\w)", re.IGNORECASE)
_ATTRIBUTION = re.compile(
    r"(?<!\w)(?:cho rằng|cho là|lập luận|nhận định|đề xuất|đề nghị|khuyên|muốn|tin rằng|nêu|kêu gọi|ủng hộ|"
    r"phản biện|nhấn mạnh|argues?|believes?|thinks?|suggests?|wants?)(?!\w)", re.IGNORECASE)
_NOT_A_CONDITION = re.compile(r"(?<!\w)(?:ngay\s+)?khi có thể(?!\w)|(?<!\w)if possible(?!\w)", re.IGNORECASE)
_NEGATION_WINDOW = 18
_CONDITIONAL_WINDOW = 80
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)|\n")


def _clause_before(text: str, start: int) -> str:
    window = text[max(0, start - _CONDITIONAL_WINDOW):start]
    return re.split(r"[,;:.!?\n]", window)[-1]


def _negated(text: str, start: int) -> bool:
    """A negation in the two words right before the match ("không nên MUA", "tránh mua"), or a
    negating phrase earlier in the same clause ("không có lý do để bán ra")."""
    window = text[max(0, start - _NEGATION_WINDOW):start].lower()
    window = re.split(r"[,;:.!?\n]", window)[-1]
    words = window.split()[-2:]
    if any(w in _NEGATION_WORDS for w in words):
        return True
    return bool(_CLAUSE_NEGATIONS.search(_clause_before(text, start)))


def _sentence_before(text: str, start: int) -> str:
    window = text[max(0, start - 160):start]
    return _SENTENCE_END.split(window)[-1]


def _attributed(text: str, start: int) -> bool:
    """The match reports what a debate side argued, not the writer's own advice."""
    before = _sentence_before(text, start)
    return bool(_DEBATE_SIDE.search(before) and _ATTRIBUTION.search(before))


def _third_party(text: str, match: re.Match) -> bool:
    before = text[max(0, match.start() - 24):match.start()]
    return bool(_THIRD_PARTY_BEFORE.search(before) or _THIRD_PARTY_AFTER.match(text, match.end()))


def _conditional(text: str, start: int, end: int | None = None) -> bool:
    """Inside a conditional sentence or scenario: a condition before the match ("nếu thủng
    ... thì bán") or after it in the same sentence ("chuyển sang BÁN nếu đa số SMA bị thủng")."""
    window = text[max(0, start - _CONDITIONAL_WINDOW):start]
    sentence = _SENTENCE_END.split(window)[-1].lower()
    if any(word in sentence for word in _CONDITIONAL_WORDS):
        return True
    if end is None:
        return False
    rest = _SENTENCE_END.split(text[end:end + 200], maxsplit=1)[0]
    rest = _NOT_A_CONDITION.sub(" ", rest)          # "MUA ngay khi có thể" is no condition
    return bool(_TRAILING_CONDITION.search(rest))


def _hits(text: str, patterns, flags=0, label: str = "") -> list[str]:
    found = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags):
            if not (_negated(text, match.start()) or _conditional(text, match.start(), match.end())
                    or _third_party(text, match) or _attributed(text, match.start())):
                found.append(f"{label}:{match.group(0)}")
    return found


def find_action_conflicts(text: str, allowed_actions) -> list[str]:
    """Action labels or advice in ``text`` outside ``allowed_actions`` (negated, conditional or
    third-party mentions ignored). Empty list = no contradiction."""
    if not text:
        return []
    conflicts: list[str] = []
    for action, patterns in _ACTION_LABEL_PATTERNS.items():
        if action not in allowed_actions:
            conflicts += _hits(text, patterns, 0, action)
    for action, patterns in _ADVICE_PATTERNS.items():
        if action not in allowed_actions:
            conflicts += _hits(text, patterns, re.IGNORECASE, action)
    return conflicts


_LEVERAGE_PATTERNS = (
    r"(?<!\w)(?:mở|vào|đặt) (?:lệnh |vị thế )?(?:long|short)(?!\w)",
    r"(?<!\w)(?:nên|có thể|hãy) (?:dùng|sử dụng|tận dụng|thêm|kéo) (?:một chút )?(?:đòn bẩy|margin|leverage)(?!\w)",
    r"(?<!\w)(?:đòn bẩy|leverage|margin)\s*(?:x\s*)?\d+(?:\s*x)?(?!\w)",
    r"(?<!\w)x\s?\d{1,3}\s+(?:đòn bẩy|leverage)(?!\w)",
    r"(?<!\w)(?:nên|có thể|hãy) (?:bán khống|short)(?!\w)",
    r"(?<!\w)(?:nên|có thể|hãy) (?:giao dịch|chơi|vào) (?:futures|hợp đồng tương lai|perp|perpetual)(?!\w)",
)


def find_leverage_advice(text: str) -> list[str]:
    """Advice to trade with leverage, margin, futures or to short: never given by this project."""
    if not text:
        return []
    found = []
    for pattern in _LEVERAGE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if not (_negated(text, match.start()) or _third_party(text, match)
                    or _attributed(text, match.start())):
                found.append(f"LEVERAGE:{match.group(0)}")
    return found


# ── prices ──
_NUMBER_RE = re.compile(r"(?<![\w.,:/\-])(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)(?![\w]|[.,]\d|[:/]\d|-\d)")
_SKIP_AFTER_RE = re.compile(
    r"\s*(?:%|x(?!\w)|lần|ngày|tuần|tháng|năm|giờ|phút|nến|kỳ|kì|phiên|điểm|người|tỷ|triệu|nghìn|ngàn|"
    r"[kKMBT](?!\w)|bn(?!\w)|billion|million|thousand|trillion|BTC(?!\w)|ETH(?!\w)|token|coin|giao dịch|lệnh)",
    re.IGNORECASE)
_SKIP_BEFORE_RE = re.compile(
    r"(?<!\w)(?:RSI|MFI|MACD|ADX|ATR|EMA|SMA|MA|WMA|VWMA|Bollinger|BB|Fear|Greed|F&G|chỉ số|điểm|hạng|rank|"
    r"top|lần|thứ|ngày|tháng|năm|tuần|quý|Q[1-4]|giờ|phiên|nến|kỳ|kì|chu kỳ|period|cung|supply|volume|"
    r"khối lượng|vốn hoá|vốn hóa|market cap|FDV|OI|open interest|funding)(?!\w)[^\d\n]{0,12}$", re.IGNORECASE)
# A bare year ("cuối 2026", "Q1 2027") is not a price unless it is written as one.
_YEAR_MIN, _YEAR_MAX = 1990, 2100
_PRICE_AFTER_RE = re.compile(r"\s*(?:USDT|USD|\$|đô)", re.IGNORECASE)


def _int_unit(digits: str) -> float:
    """Half a unit of the last significant digit of an integer: "65000" -> 500 (a rounded figure)."""
    stripped = digits.rstrip("0")
    return 0.5 * 10 ** (len(digits) - len(stripped)) if stripped else 0.5


def _readings(token: str) -> set[tuple[float, float]]:
    """Every (value, half unit of its last digit) a written number may mean.

    "65,000" is 65000 to an English reader and 65.000 to a Vietnamese one; both
    readings are kept. The half unit lets a rounded figure ("65,000") match the
    exact level it rounds (65,432).
    """
    if "." in token and "," in token:
        dec = "," if token.rfind(",") > token.rfind(".") else "."
        thou = "." if dec == "," else ","
        whole, frac = token.replace(thou, "").split(dec)
        return {(float(f"{whole}.{frac}"), 0.5 * 10 ** -len(frac))}
    for sep in (".", ","):
        if sep not in token:
            continue
        parts = token.split(sep)
        if len(parts) > 2:                       # 1,234,567 / 1.234.567: thousands only
            joined = "".join(parts)
            return {(float(joined), _int_unit(joined))}
        out = {(float(f"{parts[0]}.{parts[1]}"), 0.5 * 10 ** -len(parts[1]))}   # decimal reading
        if len(parts[1]) == 3 and parts[0] != "0":
            joined = "".join(parts)
            out.add((float(joined), _int_unit(joined)))                          # thousands reading
        return out
    return {(float(token), _int_unit(token))}


def all_numbers(text: str) -> list[float]:
    """Every value any number in ``text`` may mean (for the allowed set: nothing skipped)."""
    out = []
    for match in _NUMBER_RE.finditer(text or ""):
        try:
            out += [v for v, _ in _readings(match.group(1))]
        except ValueError:
            continue
    return out


def price_tokens(text: str, reference: float, band: tuple[float, float] = (0.25, 4.0)):
    """Numbers in ``text`` that read as a price near ``reference``: [(token, {(value, half_unit)})]."""
    found = []
    text = text or ""
    for match in _NUMBER_RE.finditer(text):
        if _SKIP_AFTER_RE.match(text, match.end()):
            continue
        if _SKIP_BEFORE_RE.search(text[max(0, match.start() - 24):match.start()].rsplit("\n", 1)[-1]):
            continue
        token = match.group(1)
        if (token.isdigit() and len(token) == 4 and _YEAR_MIN <= int(token) <= _YEAR_MAX
                and not _PRICE_AFTER_RE.match(text, match.end()) and not text[:match.start()].rstrip().endswith("$")):
            continue
        try:
            readings = _readings(token)
        except ValueError:
            continue
        in_band = {(v, u) for v, u in readings if band[0] * reference <= v <= band[1] * reference}
        if in_band:
            found.append((token, in_band))
    return found


def find_unknown_prices(text: str, allowed_text: str, reference: float | None,
                        rel_tolerance: float = 0.001) -> list[str]:
    """Price-like numbers in ``text`` matching no number of ``allowed_text``.

    A number is price-like when one of its readings lies within a wide band
    around the reference price (the last close); it matches when it equals an
    allowed number up to its own rounding (half a unit of its last digit) or
    ``rel_tolerance``. No reference price, no check.
    """
    if not reference or not math.isfinite(reference) or reference <= 0:
        return []
    allowed = sorted(set(all_numbers(allowed_text)))
    unknown = []
    for token, readings in price_tokens(text, reference):
        ok = False
        for value, half_unit in readings:
            slack = max(half_unit, abs(value) * rel_tolerance)
            if any(abs(a - value) <= slack for a in allowed):
                ok = True
                break
        if not ok:
            unknown.append(token)
    return list(dict.fromkeys(unknown))
