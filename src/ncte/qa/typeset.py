"""House punctuation for Taiwan Traditional Chinese (ADR-053), applied like a typesetter to every machine translation.

The model copies Japanese punctuation as often as not: 「――」 (U+2015) or 「──」 (box drawing) beside 「——」, half-width
「!?」 and 「,」 beside Chinese, 「...」. Within one book that is a mixture; a Taiwan edition writes the dash as 「——」,
the ellipsis as 「……」, full-width ！？，, letters and digits half-width (OK, not ＯＫ) and a space between Chinese
as 「　」 (a space beside Latin or digits is left as it is). These are typography, not meaning, so the program writes them the
same way everywhere; the words are left as they are. Human edits are never touched. The paragraph's indent is the
source's: the model drops or adds the leading 「　」 as often as not.
"""

from __future__ import annotations

import re

_CJK = "\u2600-\u27bf\u3000-\u303f\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uff00-\uffef…—"  # ☆♡♪, CJK symbols, kana, kanji, full-width
_DASH = re.compile(r"[―─━—‐－]{1,}|-{2,}")
_ELLIPSIS = re.compile(r"\.{3,}|・{3,}|…+")
_HALF = {"!": "！", "?": "？", ",": "，", ":": "：", ";": "；"}
_HALF_NEAR_CJK = re.compile(rf"(?<=[{_CJK}])[!?,:;]+|[!?,:;]+(?=[{_CJK}])")
_CJK_CHAR = re.compile(rf"[{_CJK}]")  # a Chinese character or full-width mark beside a placeholder
_SPACE_IN_CJK = re.compile(rf"(?<=[{_CJK}]) +(?=[{_CJK}])")  # 「完美了♡ 對吧」; beside Latin or digits it stays
_FULL_ALNUM = str.maketrans({chr(c): chr(c - 0xFEE0) for c in [*range(0xFF10, 0xFF1A), *range(0xFF21, 0xFF3B),
                                                               *range(0xFF41, 0xFF5B)]})
_PLACEHOLDER = re.compile(r"⟦/?\d+/?⟧")


_NUMBER_AFTER = re.compile(r"[￥¥$€£]?\d")


def _dash(m: re.Match[str]) -> str:
    n = len(m.group(0))
    if m.group(0) == "－" and _NUMBER_AFTER.match(m.string, m.end()):
        return "－"  # a minus sign (「－5度」, 「－￥124400」), not a dash
    return "——" * max(1, (n + 1) // 2)  # a dash is two em dashes; a long rule stays long


def _ellipsis(m: re.Match[str]) -> str:
    s = m.group(0)
    dots = len(s) if s[0] == "…" else max(1, len(s) // 3)
    return "……" * max(1, (dots + 1) // 2)  # 「…」 alone, 「...」, 「・・・」 → 「……」


def halfwidth(text: str) -> str:
    """Full-width letters and digits (ＯＫ, ２０２６) as a Taiwan edition writes them: half-width."""
    return text.translate(_FULL_ALNUM)


def _chinese(s: str) -> bool:
    return any("぀" <= ch <= "鿿" for ch in s)


_LEAD = re.compile(r"^([ \u3000]*)((?:⟦\d+⟧)*)([ \u3000]*)")


def indent_like(text: str, source: str) -> str:
    """The translation starts with the source paragraph's indent (「　」 or none), whatever the model wrote; the indent
    may come after the opening placeholders of a paragraph that is all one styled run (「⟦1⟧　チーム…」)."""
    lead, rest = _LEAD.match(source), _LEAD.match(text)
    assert lead is not None and rest is not None  # the pattern matches any string
    if lead.group(2) == rest.group(2):
        return lead.group(1) + rest.group(2) + lead.group(3) + text[rest.end():]
    return lead.group(1) + lead.group(3) + rest.group(2) + text[rest.end():]


def typeset(text: str, source: str | None = None) -> str:
    """The same punctuation everywhere (and, given the source, its indent); placeholders (⟦1⟧) are kept exactly. A run inside a placeholder (a styled
    「⟦1⟧!?⟦/1⟧」) is read with the characters on either side of it, so marks alone between tags count as Chinese
    when the text around them is."""
    text = halfwidth(text)
    parts = _PLACEHOLDER.split(text)
    marks = _PLACEHOLDER.findall(text)
    out = []
    for i, p in enumerate(parts):
        left = "".join(parts[:i])[-1:]
        right = "".join(parts[i + 1:])[:1]
        latin = re.search(r"[A-Za-z0-9]", p)
        if _chinese(p) or (p and not latin and bool(_CJK_CHAR.match(left + " ") or _CJK_CHAR.match(right + " "))):  # a Latin-only run is left alone
            p = _DASH.sub(_dash, p)
            p = _ELLIPSIS.sub(_ellipsis, p)
            s = _HALF_NEAR_CJK.sub(lambda m: "".join(_HALF[c] for c in m.group(0)), left + p + right)
            s = _SPACE_IN_CJK.sub("　", s)
            p = s[len(left):len(s) - len(right)]
        out.append(p)
    res = out[0]
    for mark, p in zip(marks, out[1:], strict=True):
        res += mark + p
    return res if source is None else indent_like(res, source)
