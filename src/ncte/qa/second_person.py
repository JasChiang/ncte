"""A line that calls a woman by name, translated with 你 (context mode).

The translation gets each character's gender on its card, and still now and then writes 你 to a woman (英雄賢者
§716 「セレナ、今の私はあなたに対する皮肉を…」 → 「瑟蕾娜，我剛才是在挖苦你」). The program can see the cheapest case:
the line opens with, or sets off with a comma, the name of a character known as female by then, and the translation
has 你 but no 妳. Such a unit is flagged for the reviewer, who fixes it with the context; nothing is changed here.
"""

from __future__ import annotations

import re
import sqlite3

from ..analysis.dialogue import clean
from ..memory.address import name_forms
from ..memory.repository import gender_at
from ..series import pos

_VOCATIVE_END = "、，,！!？?…―─"


def check(conn: sqlite3.Connection, book_id: str, unit: sqlite3.Row, text: str) -> str | None:
    """Evidence for a finding, or None."""
    if ev := _by_addressee(conn, book_id, unit, text):
        return ev
    if ev := _plural(conn, book_id, unit, text):
        return ev
    singular = text.replace("你們", "")  # 「莉娜、提歐，你們…」 speaks to a group: 你們 is right
    if "你" not in singular or "妳" in text:
        return None
    forms, ambiguous = name_forms(conn, book_id, unit["ordinal"])
    if not forms:
        return None
    hz = pos(conn, book_id, unit["ordinal"])
    src = clean(unit["plain_text"])
    for q in re.findall(r"「([^「」]*)」", src):
        q = q.lstrip("―─…　 ")
        for form in sorted(forms, key=len, reverse=True):
            if form in ambiguous or len(form) < 2:
                continue
            called = q.startswith(form) and len(q) > len(form) and q[len(form)] in _VOCATIVE_END
            if called and gender_at(conn, forms[form], hz) == "female":
                return f"「{form}、…」 calls her by name, but the translation uses 你 (she is female): 妳"
    return None


def _plural(conn: sqlite3.Connection, book_id: str, unit: sqlite3.Row, text: str) -> str | None:
    """Lines the reading says are said to several people, all with a gender written in the source: 妳們 only when
    every one is a woman. A plural said to one person (her army, her country) is the translator's call and is left
    alone, as is a unit whose lines go to different groups."""
    import json

    from ..memory.repository import gender_view

    if "你們" not in text and "妳們" not in text:
        return None
    groups = {frozenset(json.loads(u[0] or "[]")) for u in conn.execute(
        "SELECT addressee_ids_json FROM utterances WHERE unit_id=?", (unit["id"],))}
    groups = {g for g in groups if len(g) >= 2}
    if len(groups) != 1:
        return None
    hz = pos(conn, book_id, unit["ordinal"])
    genders = [gender_view(conn, c, hz) for c in next(iter(groups))]
    if any(not g or g[1] != "explicit" for g in genders):
        return None
    women = all(g and g[0] == "female" for g in genders)
    if women and "你們" in text and "妳們" not in text:
        return "the line is said to several people, all female: 你們 should be 妳們"
    if not women and "妳們" in text and "你們" not in text:
        return "the line is said to several people, not all female: 妳們 should be 你們"
    return None


def _by_addressee(conn: sqlite3.Connection, book_id: str, unit: sqlite3.Row, text: str) -> str | None:
    """The reading said who the unit's lines are said to (one person, the same for every line): their gender, written
    in the source (not inferred), against a lone 你／妳 in the translation. 你們／妳們 (a group) are left alone."""
    import json

    to: set[str] = set()
    stated = True
    for u in conn.execute("SELECT addressee_ids_json, addressee_status FROM utterances WHERE unit_id=?", (unit["id"],)):
        ids = set(json.loads(u[0] or "[]"))
        to |= ids
        stated = stated and (not ids or u[1] == "explicit")
    if len(to) != 1 or not stated:  # who it is said to only read from the scene: no rule on it
        return None
    from ..memory.repository import gender_view

    g = gender_view(conn, next(iter(to)), pos(conn, book_id, unit["ordinal"]))
    if not g or g[1] != "explicit":
        return None
    singular = text.replace("你們", "").replace("妳們", "")
    name = conn.execute("SELECT coalesce(display_name_zh, canonical_name) FROM characters WHERE id=?", (next(iter(to)),)).fetchone()
    who = name[0] if name else "?"
    if g[0] == "female" and "你" in singular and "妳" not in singular:
        return f"the line is said to {who} (female): 你 should be 妳"
    if g[0] == "male" and "妳" in singular and "你" not in singular:
        return f"the line is said to {who} (male): 妳 should be 你"
    return None
