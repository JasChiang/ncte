"""Whose voice tells the story: first person (the narrator is 「私」「俺」) or third person (ADR-040).

Japanese narration often leaves the subject out (「村長の後を追って邸宅内に入る」) or says 「こちら」 for the focal
character. In a third-person book the translator still wrote 「我跟在村長身後」 in eleven places of 英雄賢者 — it was
never told whose voice the narration is. The program can tell from the source alone: in third-person books narration
(outside quotes and parentheses) almost never has a first-person pronoun (0.1–0.2% of paragraphs), in first-person
books it does all the time (25–27%).
"""

from __future__ import annotations

import re
import sqlite3

FIRST_PERSON = re.compile(r"(私|俺|僕|わたし|あたし|ぼく|ボク|オレ|わたくし|あたい)(?!達|たち)")
_QUOTED = re.compile(r"「[^」]*」|『[^』]*』|（[^）]*）|\([^)]*\)")
_RUBY = re.compile(r"《[^》]*》")
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
    """A narration paragraph of a third-person book whose translation says 「我」 outside quotes."""
    if not is_narration(plain):
        return False
    from ..epub.units import PH_RE

    return bool(_ZH_I.search(_QUOTED.sub("", PH_RE.sub("", text))))
