"""Dialogue structure (ADR-059: who says a line is the model's reading).

* quote detection (「…」 with nested 『』, multi-paragraph quotes that close in a later unit; a paragraph that is one
  『…』 on its own — a voice that is not quite speech: a monster, a thought heard, a crowd — counts as a quote too)
* speech profiles computed only from explicit / human-verified examples
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ..epub.units import RUBY_NOTATION

OPEN, CLOSE = "「", "」"
# what may follow a whole-paragraph 『…』 for it to still be a line someone says: nothing, or a speech tag
# (『帰ったか』と、ルカの脳裏に声が届いた / 『この苦しみを…』──と). 『私』の世界は… is emphasis inside narration.
_VOICE_TAIL = re.compile(r"^[\s　]*(?:$|[──―─]*[\s　]*と)")
RUBY = RUBY_NOTATION  # readings only; the author's own 《》 brackets are text

FIRST_PERSON = ["わたくし", "わたし", "あたし", "あたい", "私", "僕", "ぼく", "ボク", "俺様", "俺", "おれ", "オレ",
                "うち", "ウチ", "わし", "儂", "拙者", "我輩", "吾輩", "自分", "妾", "余"]
SECOND_PERSON = ["あなた", "貴方", "あんた", "お前", "おまえ", "君", "きみ", "貴様", "てめえ", "てめぇ", "そなた", "汝"]


@dataclass
class Quote:
    unit_id: str
    index: int
    start: int
    end: int  # exclusive
    text: str
    opens: bool  # starts with 「 in this unit
    closes: bool  # ends with 」 in this unit
    continuation_of: tuple[str, int] | None = None


@dataclass
class UnitText:
    id: str
    text: str  # plain text (ruby notation removed)
    ordinal: int


def clean(text: str) -> str:
    return RUBY.sub("", text)


def find_quotes(units: list[UnitText]) -> list[Quote]:
    """Top-level 「」 spans per unit, linking multi-paragraph quotes.

    Two conventions are recognised for a quote left open at the end of a paragraph:
    the next paragraph continues without 「 and closes with 」, or the next paragraph
    re-opens with 「 (classic Japanese typesetting). Both are linked via ``continuation_of``.
    """
    out: list[Quote] = []
    carry: tuple[str, int] | None = None  # (unit_id, index) of an unclosed quote
    for u in units:
        t = u.text
        idx = 0
        i = 0
        link: tuple[str, int] | None = None
        if carry is not None:
            first_open, first_close = t.find(OPEN), t.find(CLOSE)
            if first_close >= 0 and (first_open < 0 or first_close < first_open) or (first_open < 0 and first_close < 0):
                closes = first_close >= 0
                end = first_close + 1 if closes else len(t)
                out.append(Quote(u.id, idx, 0, end, t[:end], False, closes, carry))
                carry = None if closes else (u.id, idx)
                idx += 1
                i = end
            elif t.lstrip(" 　").startswith(OPEN):
                link = carry
                carry = None
            else:
                carry = None
        depth = 0
        start: int | None = None
        while i < len(t):
            ch = t[i]
            if ch == OPEN:
                if depth == 0:
                    start = i
                depth += 1
            elif ch == CLOSE and depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    out.append(Quote(u.id, idx, start, i + 1, t[start:i + 1], True, True, link))
                    link = None
                    idx += 1
                    start = None
            i += 1
        if depth > 0 and start is not None:
            out.append(Quote(u.id, idx, start, len(t), t[start:], True, False, link))
            carry = (u.id, idx)
        if idx == 0 and carry is None:
            voice = _voice(t)
            if voice is not None:
                out.append(Quote(u.id, 0, voice[0], voice[1], t[voice[0]:voice[1]], True, True, None))
    return out


def _voice(t: str) -> tuple[int, int] | None:
    """A paragraph that is a single top-level 『…』 (optionally followed by a speech tag): (start, end) of it."""
    start = len(t) - len(t.lstrip(" 　"))
    if not t.startswith("『", start):
        return None
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "『":
            depth += 1
        elif t[i] == "』":
            depth -= 1
            if depth == 0:
                return (start, i + 1) if _VOICE_TAIL.match(t[i + 1:]) else None
    return None


def speech_profile(examples: list[str]) -> dict[str, Any]:
    """Derive a soft speech profile from verified utterances only."""
    fp: Counter[str] = Counter()
    sp: Counter[str] = Counter()
    endings: Counter[str] = Counter()
    polite = 0
    for raw in examples:
        t = clean(raw).strip("「」『』 　")
        for w in FIRST_PERSON:
            if w in t:
                fp[w] += 1
                break
        for w in SECOND_PERSON:
            if w in t:
                sp[w] += 1
                break
        core = re.sub(r"[。、！？!?…‥ー～〜\s]+$", "", t)
        if len(core) >= 2:
            endings[core[-2:]] += 1
        if re.search(r"(です|ます|でした|ました|ません|ください)", t):
            polite += 1
    n = len(examples)
    return {
        "n_examples": n,
        "first_person": [w for w, _ in fp.most_common(3)],
        "second_person": [w for w, _ in sp.most_common(3)],
        "sentence_endings": [w for w, _ in endings.most_common(5)],
        "formality": ("usually_polite" if n and polite / n >= 0.6 else "mixed" if polite else "casual") if n else "unknown",
    }
