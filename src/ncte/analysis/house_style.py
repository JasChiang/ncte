"""A house style for the book, chosen by the model after reading (ADR-053).

A Taiwan edition keeps one way of handling honorifics through the whole book (さん rendered as 先生／小姐 or left
out, 様 as 大人 …) and keeps each character's habits of speech the same (a 「〜っす」 that always sounds the same in
Chinese). Without a choice, the translation drifts scene by scene. After reading, the model looks at how people in
this book address each other, the main characters' lines and the speech endings the program counted for each, and
chooses: the honorific policy, each main character's habits with a rendering, and a few house rules for this book.
They go into the series style (``memory.style``) unless a person has set it, and reach naming and every translation.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from typing import Any

from ..epub.units import match_text
from ..llm.schemas import HouseStyleOutput
from ..memory.style import SeriesStyle, load_style, save_style
from .impressions import main_characters

LINES = 10
_END = re.compile(r"[」』！？!?…―─。、　 ～ー\s]+$")


def endings(lines: list[str]) -> list[str]:
    """Line endings this character repeats (2–4 characters, at least 4 times): candidates for a habit of speech; the
    model decides which are habits."""
    cnt: Counter[str] = Counter()
    for q in lines:
        q = _END.sub("", match_text(q).strip("「『"))
        for n in (2, 3, 4):
            if len(q) > n + 1:
                cnt[q[-n:]] += 1
    out: list[str] = []
    for e, k in cnt.most_common(30):
        if k >= 4 and not any(e in o or o in e for o in out):
            out.append(f"〜{e}（{k} 次）")
    return out[:6]


def request(conn: sqlite3.Connection, book_id: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Characters with lines spread through the book (with the current translation of each line's paragraph when there
    is one), the forms of address, and the names already chosen: a habit's rendering keeps to what the book already
    says, and names and nicknames are naming's choice, not the house style's."""
    from ..epub.units import PH_RE
    from ..translation.cache import latest_texts

    best = latest_texts(conn, book_id)
    chars, refs, items = main_characters(conn, book_id), {}, []
    for k, c in enumerate(chars, 1):
        said = [(r[0], r[1]) for r in conn.execute("SELECT quote_text, unit_id FROM utterances WHERE "
                                                   "speaker_id_nullable=? AND book_id=? ORDER BY id", (c["id"], book_id))]
        spread = said if len(said) <= LINES else [said[j * (len(said) - 1) // (LINES - 1)] for j in range(LINES)]
        lines: list[dict[str, str]] = []
        for q, uid in spread:
            line = {"ja": match_text(q)[:80]}
            if (t := best.get(uid)) is not None:
                line["zh"] = PH_RE.sub("", t["text"]).strip()[:120]  # the paragraph as translated now
            lines.append(line)
        imp = conn.execute("SELECT voice FROM character_impressions WHERE character_id=?", (c["id"],)).fetchone()
        refs[f"c{k}"] = c["id"]
        items.append({"ref": f"c{k}", "name": c["display_name_zh"] or c["canonical_name"], "ja": c["canonical_name"],
                      **({"voice": imp[0]} if imp and imp[0] else {}), "lines": lines,
                      "repeated_endings": endings([q for q, _ in said])})
    forms = [f"{a['form_ja']}×{a['uses']}" for a in conn.execute(
        "SELECT form_ja, SUM(uses) uses FROM address_terms WHERE book_id=? AND speaker_id!=addressee_id GROUP BY form_ja "
        "ORDER BY uses DESC LIMIT 40", (book_id,))]
    names = [f"{g['source_term']}→{g['target_term']}" for g in conn.execute(
        "SELECT source_term, target_term FROM glossary WHERE book_id=? ORDER BY verified DESC, id LIMIT 80", (book_id,))]
    req: dict[str, Any] = {"task": "house_style", "characters": items, "address_forms": forms}
    if names:
        req["names"] = names
    return req, refs


def apply(conn: sqlite3.Connection, book_id: str, parsed: HouseStyleOutput, refs: dict[str, str]) -> dict[str, int]:
    """Into the series style when no person has set it (or the model set it before); returns what was kept."""
    style = load_style(conn, book_id)
    if not (style.is_default() or style.auto):
        return {"kept_human_style": 1}
    names = {r["id"]: r["canonical_name"] for r in conn.execute("SELECT id, canonical_name FROM characters")}
    voices: dict[str, str] = {}
    for h in parsed.habits:
        cid = refs.get(h.ref)
        if not cid or not h.habit_ja.strip() or not h.render_zh.strip():
            continue
        line = f"「{h.habit_ja.strip()}」譯作「{h.render_zh.strip()}」" + (f"（{h.note.strip()}）" if h.note.strip() else "")
        voices[names[cid]] = (voices.get(names[cid], "") + "；" + line).lstrip("；")
    new = SeriesStyle(honorifics=parsed.honorifics, rules=[r.strip() for r in parsed.rules if r.strip()][:6],
                      voices=voices, notes=style.notes, auto=True)
    save_style(conn, book_id, new)
    return {"habits": len(parsed.habits), "rules": len(new.rules)}
