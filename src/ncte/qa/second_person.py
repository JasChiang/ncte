"""A line that calls a woman by name, translated with 你 (context mode).

The translation gets each character's gender on its card, and still now and then writes 你 to a woman (英雄賢者
§716 「エルリア、今の私はあなたに対する皮肉を…」 → 「艾爾莉亞，我剛才是在挖苦你」). The program can see the cheapest case:
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
    singular = text.replace("你們", "")  # 「米莉絲、威澤爾，你們…」 speaks to a group: 你們 is right
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
