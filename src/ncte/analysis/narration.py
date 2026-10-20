"""Whose voice tells the story: first person (the narrator is 「私」「俺」) or third person (ADR-040).

Japanese narration often leaves the subject out (「村長の後を追って邸宅内に入る」) or says 「こちら」 for the focal
character. In a third-person book the translator still wrote 「我跟在村長身後」 in eleven places of the fantasy-romance book — it was
never told whose voice the narration is. The program can tell from the source alone: in third-person books narration
(outside quotes and parentheses) almost never has a first-person pronoun (0.1–0.2% of paragraphs), in first-person
books it does all the time (25–27%).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..epub.units import RUBY_NOTATION, in_source, source_form

FIRST_PERSON = re.compile(r"(私|俺|僕|わたし|あたし|ぼく|ボク|オレ|わたくし|あたい)(?!達|たち)")
_QUOTED = re.compile(r"「[^」]*」|『[^』]*』|（[^）]*）|\([^)]*\)")
_RUBY = RUBY_NOTATION  # readings only; the author's own 《》 brackets are text
MIN_NARRATION = 150  # paragraphs: fewer and the book does not say
THIRD_BELOW, FIRST_ABOVE = 0.03, 0.10


def narration_only(text: str) -> str:
    return _QUOTED.sub("", _RUBY.sub("", text))


def is_narration(plain: str) -> bool:
    return not plain.lstrip(" 　").startswith(("「", "『", "（", "("))


def person(conn: sqlite3.Connection, book_id: str) -> str | None:
    """'first', 'third', or None when the book does not say clearly."""
    from ..epub.matter import story_hrefs

    story = story_hrefs(conn, book_id)
    narr = [narration_only(r[0]) for r in conn.execute(
        "SELECT plain_text, resource_href FROM units WHERE book_id=? AND kind='text' AND translatable=1", (book_id,))
        if r[1] in story and is_narration(r[0])]
    if len(narr) < MIN_NARRATION:
        return None
    share = sum(1 for t in narr if FIRST_PERSON.search(t)) / len(narr)
    return "third" if share < THIRD_BELOW else "first" if share > FIRST_ABOVE else None


# 「我」 inside a word is not the narrator speaking (無關敵我, 自我介紹, 忘我)
_ZH_I = re.compile(r"(?<![敵自忘唯])我")


def first_person_slip(plain: str, text: str) -> bool:
    """A narration paragraph of a third-person book whose translation says 「我」 outside quotes.

    Not when the source paragraph itself says 「僕」「私」 outside quotes: a character's thoughts told in their own
    words (「そうだ僕は知っていたのだ」, 銀河鉄道の夜) are rightly 「我」.
    """
    if not is_narration(plain) or FIRST_PERSON.search(narration_only(plain)):
        return False
    from ..epub.units import PH_RE

    return bool(_ZH_I.search(_QUOTED.sub("", PH_RE.sub("", text))))


# ---------------------------------------------------------------- per scene (the read pass judges, like a translator)
def scene_counts(rows: list[sqlite3.Row]) -> tuple[int, int]:
    """(narration paragraphs, those with a first-person pronoun) in a scene's units."""
    narr = [narration_only(r["plain_text"]) for r in rows if r["kind"] == "text" and is_narration(r["plain_text"])]
    return len(narr), sum(1 for t in narr if FIRST_PERSON.search(t))


def hint(rows: list[sqlite3.Row]) -> dict[str, int]:
    n, k = scene_counts(rows)
    return {"narration_paragraphs": n, "with_first_person": k}


def judged(person_: str, rows: list[sqlite3.Row]) -> str | None:
    """The program's check of the model's answer: 'conflict' when the text plainly says otherwise."""
    n, k = scene_counts(rows)
    if person_ == "third" and n >= 8 and k / n > 0.15:
        return "conflict"
    if person_ == "first" and n >= 15 and k / n < 0.02:
        return "conflict"
    return None


def for_scene(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row,
              names: dict[str, str] | None = None) -> dict[str, Any] | None:
    """What the translator is told for this scene: the read pass's judgement, else the whole book's."""
    import json

    st = json.loads(scene["state_json"] or "{}").get("narration") or {}
    out: dict[str, Any] = {}
    if st.get("person") in ("first", "third") and st.get("check") != "conflict":
        out["person"] = st["person"]
        for key in ("narrator", "focal"):
            if st.get(key) and (names is None or st[key] in names):  # not yet known by any name here: left out
                out[key] = names.get(st[key], st[key]) if names is not None else st[key]
        if st["person"] == "first" and st.get("self_forms"):
            out["self_forms"] = st["self_forms"]
    else:
        whole = person(conn, book_id)
        if whole is None:
            return None
        out["person"] = whole
    return out


# ---------------------------------------------------------------- the narrator's word for themself (吾輩, 俺, 私)
def established_self_forms(conn: sqlite3.Connection, book_id: str, seq: int) -> list[dict[str, str | None]]:
    """What earlier scenes decided: the first decision for each (narrator, word) stands for the whole book."""
    import json

    seen: dict[tuple[str | None, str], dict[str, str | None]] = {}
    for (state,) in conn.execute("SELECT state_json FROM scenes WHERE book_id=? AND seq<? AND version>0 ORDER BY seq",
                                 (book_id, seq)):
        n = json.loads(state or "{}").get("narration") or {}
        for f in n.get("self_forms") or []:
            seen.setdefault((n.get("narrator"), f["ja"]), {"narrator_ref": n.get("narrator"), "ja": f["ja"], "zh": f["zh"]})
    return list(seen.values())


def settle_self_forms(forms: list[dict[str, str]], narrator: str | None, earlier: list[dict[str, str | None]],
                      rows: list[sqlite3.Row]) -> list[dict[str, str]]:
    """Keep the words the scene's narration really has; an earlier decision for the same narrator and word wins."""
    text = "\n".join(narration_only(r["plain_text"]) for r in rows if r["kind"] == "text" and is_narration(r["plain_text"]))
    before = {e["ja"]: e["zh"] for e in earlier if e["narrator_ref"] == narrator}
    out: dict[str, str] = {}
    for f in forms:
        ja, zh = source_form(f["ja"]), f["zh"].strip()  # 吾輩《わがはい》 as shown is the text's 吾輩
        if zh in ("自己", "自身"):  # a reflexive (自分 → 自己), not the narrator's voice
            continue
        if zh and in_source(ja, text) and ja not in out:
            out[ja] = before.get(ja) or zh
    return [{"ja": ja, "zh": zh} for ja, zh in out.items()]


def self_form_slip(plain: str, text: str, forms: dict[str, str]) -> str | None:
    """A first-person narration paragraph whose narrator calls themself ``ja`` but the translation does not say the
    book's ``zh`` (or copies a ``ja`` that was to become 我)."""
    if not is_narration(plain):
        return None
    from ..epub.units import PH_RE

    src = narration_only(plain)
    zh_text = _QUOTED.sub("", PH_RE.sub("", text))
    for ja, zh in forms.items():
        if ja not in src:
            continue
        if zh != "我" and zh not in zh_text:
            return f"the narrator calls themself 「{ja}」 here; this book renders it 「{zh}」 in the narration"
        if zh == "我" and ja != "我" and ja in zh_text:
            return f"the narrator's 「{ja}」 is rendered 「我」 in this book, not 「{ja}」"
    return None
