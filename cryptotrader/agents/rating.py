"""The four system actions and a deterministic parser for decision text.

The action scale is the strategy engine's (``agents.quant_engine``):

- BUY: the strategy wants the coin in its basket and the book does not hold it;
- HOLD: the strategy wants it and the book holds it (or the holding is unknown and kept);
- SELL: the book holds it and the strategy no longer wants it (spot only: SELL exits, never shorts);
- NO_TRADE: the strategy does not want it and there is nothing to exit.

``extract_rating`` returns None when no rating can be read, and callers turn
that into ``REVIEW``: a decision nobody can read is not a HOLD.
"""

from __future__ import annotations

import re
import unicodedata

RATINGS: tuple[str, ...] = ("BUY", "HOLD", "NO_TRADE", "SELL")
RATING_REVIEW = "REVIEW"

# Words a decision may use for each action, lower-cased; two-word names by their first word.
_ALIASES = {
    "buy": "BUY", "mua": "BUY",
    "hold": "HOLD", "giữ": "HOLD", "giu": "HOLD",
    "sell": "SELL", "bán": "SELL", "ban": "SELL",
    "no_trade": "NO_TRADE", "notrade": "NO_TRADE", "đứng": "NO_TRADE", "dung": "NO_TRADE",
}
_RATING_SET = set(_ALIASES)

_RATING_LABEL_RE = re.compile(r"(?<![a-z])rating\b[^:\-\u2010-\u2015]*[:\-\u2010-\u2015][\s*]*(\w+)",
                              re.IGNORECASE)
_OWN_QUALIFIER = r"(?:(?:final|our|overall|my|recommended|updated|system)\s+)*"
_RATING_LINE_RE = re.compile(
    r"\s*(?P<item>(?:[-+*\u2022]|\d+[.)])\s+)?[\s*_#]*" + _OWN_QUALIFIER
    + r"rating[^\w:\-\u2010-\u2015]*[:\-\u2010-\u2015][\s*]*(?P<value>\w+)",
    re.IGNORECASE,
)
_RATING_SCALE_RE = re.compile(r"rating\s*(scale|options|legend)", re.IGNORECASE)


def extract_rating(text: str) -> str | None:
    """The rating of a decision from its own labelled line, or None when there is none or they disagree."""
    if not text:
        return None
    norm = unicodedata.normalize("NFKC", text)
    lines = [line for line in norm.splitlines() if line.strip() and not _RATING_SCALE_RE.search(line)]
    first = next((line for line in lines if not line.lstrip().startswith("#") or _RATING_LINE_RE.match(line)), "")
    m = _RATING_LINE_RE.match(first)
    opening = m if m and m.group("value").lower() in _RATING_SET else None
    if opening and not opening.group("item"):
        return _ALIASES[opening.group("value").lower()]
    own = [_ALIASES[opening.group("value").lower()]] if opening else []
    labels = []
    for line in lines:
        m = _RATING_LINE_RE.match(line)
        if m and not m.group("item") and m.group("value").lower() in _RATING_SET:
            own.append(_ALIASES[m.group("value").lower()])
        labels += [_ALIASES[v.lower()] for v in _RATING_LABEL_RE.findall(line) if v.lower() in _RATING_SET]
    found = own or labels
    return found[0] if found and len(set(found)) == 1 else None


def parse_rating(text: str, default: str = RATING_REVIEW) -> str:
    rating = extract_rating(text)
    return rating if rating is not None else default


def run_rating(final_state: dict) -> str:
    """A finished run's rating: the system action stored by the Output Guard, else read from the decision."""
    return final_state.get("final_rating") or parse_rating(final_state.get("final_trade_decision", ""))


def is_review(signal: str) -> bool:
    return signal == RATING_REVIEW
