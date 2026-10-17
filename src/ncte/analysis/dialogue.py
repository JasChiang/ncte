"""Deterministic dialogue analysis (spec §5.3).

* quote detection (「…」 with nested 『』, multi-paragraph quotes that close in a later unit; a paragraph that is one
  『…』 on its own — a voice that is not quite speech: a monster, a thought heard, a crowd — counts as a quote too)
* explicit dialogue-tag detection («「…」とミナは言った», «ミナが口を開いた。「…」»)
* adjacent action narration («「…」\\n　カイトは頭をかいた。») → *action* evidence (inferred only)
* laughing, smiling (笑った, 微笑んだ, 苦笑した) say how a line is said only when the quotative と ties them to it
  («「…」とカイトは笑った»); a paragraph of its own («「君は犯人だ」\\n　カイトは笑った。») is someone's reaction —
  the listener's as often as the speaker's — so it is action evidence, never a dialogue tag
* speech profiles computed only from explicit / human-verified examples
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..epub.units import RUBY_NOTATION

OPEN, CLOSE = "「", "」"
# what may follow a whole-paragraph 『…』 for it to still be a line someone says: nothing, or a speech tag
# (『帰ったか』と、ルカの脳裏に声が届いた / 『この苦しみを…』──と). 『私』の世界は… is emphasis inside narration.
_VOICE_TAIL = re.compile(r"^[\s　]*(?:$|[──―─]*[\s　]*と)")
RUBY = RUBY_NOTATION  # readings only; the author's own 《》 brackets are text

SPEECH_VERBS = (
    "言っ", "言う", "言い", "言われ", "呟", "つぶや", "答え", "応え", "叫", "告げ", "返し", "返す", "返事", "尋ね", "訊",
    "囁", "ささや", "怒鳴", "続け", "口を開", "口にし", "問い", "問う", "ぼや", "漏らし", "促し", "命じ", "話し",
    "謝っ", "頼ん", "言葉を", "声をかけ", "声を上げ",
)
# how a line is said, not that it is said: a tag only right after the quote with と
MANNER_VERBS = ("笑っ", "微笑", "苦笑")
# 「続け」 after a verb stem is 「使い続け」 (keep doing), not 「…と続けた」 (went on saying)
_VERB_ALT = "|".join(r"(?<![いきしちにひみりぎじびぴえけせてねへめれげぜでべぺ])続け" if v == "続け" else re.escape(v)
                     for v in SPEECH_VERBS)
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
class TagEvidence:
    character_id: str
    kind: str  # dialogue_tag | action
    unit_id: str
    snippet: str
    adjacent: bool = False  # found in the paragraph before/after the quote (weaker than «「…」とミナは言った»)


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


def _alias_pattern(aliases: dict[str, str]) -> re.Pattern[str] | None:
    names = sorted((a for a in aliases if a), key=len, reverse=True)
    if not names:
        return None
    return re.compile("|".join(re.escape(n) for n in names))


def detect_tags(units: list[UnitText], quotes: list[Quote], aliases: dict[str, str]) -> dict[tuple[str, int], list[TagEvidence]]:
    """aliases: name_ja/alias → character_id. Returns evidence per (unit_id, quote_index)."""
    pat = _alias_pattern(aliases)
    ev: dict[tuple[str, int], list[TagEvidence]] = {}
    if pat is None:
        return ev
    names = pat.pattern
    # «「…」とミナは言った»: the name must be the subject (「と小林に声をかけた」 — 小林 is spoken to)
    after_tag = re.compile(rf"^[、，\s　]*と?[、\s　]*({names})(?:は|が|も|(?=[^にをへとの]))[^。「」]{{0,24}}?(?:{_VERB_ALT})")
    manner = "|".join(re.escape(v) for v in MANNER_VERBS)
    after_manner = re.compile(rf"^[、，\s　]*と[、\s　]*({names})(?:は|が|も|(?=[^にをへとの]))[^。「」]{{0,24}}?(?:{manner})")
    narr_tag = re.compile(rf"^[\s　]*({names})(?:は|が|も)[^。「」]{{0,24}}?(?:{_VERB_ALT})")
    narr_action = re.compile(rf"^[\s　]*({names})(?:は|が)")
    before_tag = re.compile(rf"({names})(?:は|が)[^。「」]{{0,24}}?(?:{_VERB_ALT})[^。「」]{{0,6}}。?[\s　]*$")
    by_unit = {u.id: i for i, u in enumerate(units)}
    q_by_unit: dict[str, list[Quote]] = {}
    for q in quotes:
        q_by_unit.setdefault(q.unit_id, []).append(q)

    def add(q: Quote, cid: str, kind: str, uid: str, snip: str) -> None:
        ev.setdefault((q.unit_id, q.index), []).append(TagEvidence(cid, kind, uid, snip[:40], uid != q.unit_id))

    for q in quotes:
        if not q.closes:
            continue
        u = units[by_unit[q.unit_id]]
        rest = u.text[q.end:]
        nxt_q = [x for x in q_by_unit.get(u.id, []) if x.start >= q.end]
        if nxt_q:
            rest = rest[: nxt_q[0].start - q.end]
        m = after_tag.match(rest) or after_manner.match(rest)
        if m:
            add(q, aliases[m.group(1)], "dialogue_tag", u.id, rest)
            continue
        only_quote = u.text[: q_by_unit[u.id][0].start].strip(" 　") == "" and not rest.strip(" 　")
        # narration paragraph right after a quote-only paragraph
        if only_quote and by_unit[u.id] + 1 < len(units):
            nu = units[by_unit[u.id] + 1]
            if OPEN not in nu.text[:2]:
                m = narr_tag.match(nu.text)
                if m:
                    add(q, aliases[m.group(1)], "dialogue_tag", nu.id, nu.text)
                    continue
                m = narr_action.match(nu.text)
                if m:
                    add(q, aliases[m.group(1)], "action", nu.id, nu.text)
        # narration paragraph right before a quote-only paragraph («ミナが口を開いた。»)
        if only_quote and q.index == 0 and by_unit[u.id] > 0:
            pu = units[by_unit[u.id] - 1]
            pp = units[by_unit[u.id] - 2] if by_unit[u.id] > 1 else None
            # the narration must not itself be the after-tag of an earlier quote
            follows_quote = pp is not None and pp.text.rstrip(" 　").endswith(CLOSE)
            # …and must be pure narration (a «「…」とミナは言った。» paragraph belongs to its own quote)
            if OPEN not in pu.text and CLOSE not in pu.text and not follows_quote:
                m = before_tag.search(pu.text)
                if m:
                    add(q, aliases[m.group(1)], "dialogue_tag", pu.id, pu.text)
    return ev


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


@dataclass
class StyleHint:
    character_id: str
    evidence: list[str] = field(default_factory=list)


def style_hints(quote: str, profiles: dict[str, dict[str, Any]]) -> list[StyleHint]:
    """Weak evidence only: which verified profiles share a first-person pronoun with the quote."""
    t = clean(quote)
    hints = []
    for cid, prof in profiles.items():
        ev = [f"first_person:{w}" for w in prof.get("first_person", []) if w in t]
        if ev:
            hints.append(StyleHint(cid, ev))
    return hints
