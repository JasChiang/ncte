"""House punctuation for Taiwan Traditional Chinese (ADR-053), applied like a typesetter to every machine translation.

The model copies Japanese punctuation as often as not: 「――」 (U+2015) or 「──」 (box drawing) beside 「——」, half-width
「!?」 and 「,」 beside Chinese, 「...」. Within one book that is a mixture; a Taiwan edition writes the dash as 「——」,
the ellipsis as 「……」 and full-width ！？，. These are typography, not meaning, so the program writes them the
same way everywhere; the words are left as they are. Human edits are never touched.
"""

from __future__ import annotations

import re

_CJK = r"぀-ヿ㐀-鿿豈-﫿「」『』（）《》〈〉【】、。，．！？：；…—"
_DASH = re.compile(r"[―─━—‐－]{1,}|-{2,}")
_ELLIPSIS = re.compile(r"\.{3,}|・{3,}|…+")
_HALF = {"!": "！", "?": "？", ",": "，", ":": "：", ";": "；"}
_HALF_NEAR_CJK = re.compile(rf"(?<=[{_CJK}])[!?,:;]+|[!?,:;]+(?=[{_CJK}])")
_PLACEHOLDER = re.compile(r"⟦/?\d+/?⟧")


def _dash(m: re.Match[str]) -> str:
    n = len(m.group(0))
    return "——" * max(1, (n + 1) // 2)  # a dash is two em dashes; a long rule stays long


def _ellipsis(m: re.Match[str]) -> str:
    s = m.group(0)
    dots = len(s) if s[0] == "…" else max(1, len(s) // 3)
    return "……" * max(1, (dots + 1) // 2)  # 「…」 alone, 「...」, 「・・・」 → 「……」


def typeset(text: str) -> str:
    """The same punctuation everywhere; placeholders (⟦1⟧) are kept exactly."""
    parts = _PLACEHOLDER.split(text)
    marks = _PLACEHOLDER.findall(text)
    out = []
    for p in parts:
        if any("぀" <= ch <= "鿿" for ch in p):  # Chinese text; a Latin-only run is left alone
            p = _DASH.sub(_dash, p)
            p = _ELLIPSIS.sub(_ellipsis, p)
            p = _HALF_NEAR_CJK.sub(lambda m: "".join(_HALF[c] for c in m.group(0)), p)
        out.append(p)
    res = out[0]
    for mark, p in zip(marks, out[1:], strict=True):
        res += mark + p
    return res
