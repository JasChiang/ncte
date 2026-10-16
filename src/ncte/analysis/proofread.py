"""A bilingual proofreading pass (ADR-051).

The reader's look reads the Chinese alone and cannot see a sentence that reads well and says something else: a fixed
phrase taken literally (「いやまったく」 as an apology), a negation lost, a subject swapped, a person addressed as 「那位」
then 「你」. The selective review sees only flagged paragraphs. Here, after a volume is translated, every paragraph
goes to the model with its source, who says each line (as read), and the cards of the people in that stretch (name,
gender, way of speaking); the model points at real errors only — meaning, omission, addition, speaker or addressee,
tone, a character's voice, a house-style slip — and each goes back to the translator with the source and the scene's
memory, which fixes it or keeps the rendering. The program finds the paragraphs and the people; it decides nothing.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..epub.units import PH_RE
from ..translation.cache import latest_texts

CHUNK_UNITS = 60  # paragraphs per request: source and translation side by side


def chunks(conn: sqlite3.Connection, book_id: str, unit_ids: set[str] | None = None) -> list[list[dict[str, Any]]]:
    """Consecutive translated story paragraphs, chapter by chapter, each with its source and who says its lines."""
    best = latest_texts(conn, book_id)
    names = {r["id"]: r["display_name_zh"] or r["canonical_name"] for r in conn.execute("SELECT * FROM characters")}
    said: dict[str, set[str]] = {}
    for u in conn.execute("SELECT unit_id, speaker_id_nullable sp FROM utterances WHERE book_id=? AND sp IS NOT NULL",
                          (book_id,)):
        said.setdefault(u["unit_id"], set()).add(u["sp"])
    out: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    href = None
    for r in conn.execute("SELECT * FROM units WHERE book_id=? AND translatable=1 AND kind IN ('text','heading') AND "
                          "spine_index IS NOT NULL ORDER BY ordinal", (book_id,)):
        t = best.get(r["id"])
        if t is None or (unit_ids is not None and r["id"] not in unit_ids):
            continue
        if cur and (r["resource_href"] != href and len(cur) >= CHUNK_UNITS // 3 or len(cur) >= CHUNK_UNITS):
            out.append(cur)
            cur = []
        href = r["resource_href"]
        # the source as written: 《》 may be a reading (漢字《かな》) or the author's own brackets (「…、《亡雄の旅団》」);
        # stripping it would make the translation look like it added words
        item: dict[str, Any] = {"unit_id": r["id"], "ja": r["plain_text"], "zh": PH_RE.sub("", t["text"])}
        if who := sorted(names.get(x, "?") for x in said.get(r["id"], ())):
            item["speaker"] = "、".join(who)
            item["_who"] = sorted(said[r["id"]])
        cur.append(item)
    if cur:
        out.append(cur)
    return out


def cast(conn: sqlite3.Connection, book_id: str, part: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The people who speak in this stretch (and anyone named in it): name, gender, way of speaking."""
    from ..memory.style import load_style

    ids = {x for p in part for x in p.get("_who", [])}
    text = "".join(p["ja"] for p in part)
    habits = load_style(conn, book_id).voices  # the house style's speech habits (ADR-053), by Japanese name
    out = []
    for c in conn.execute("SELECT c.*, g.gender, i.voice FROM characters c LEFT JOIN character_gender g ON "
                          "g.character_id=c.id LEFT JOIN character_impressions i ON i.character_id=c.id WHERE c.book_id=?",
                          (book_id,)):
        forms = [c["canonical_name"], *json.loads(c["aliases_json"] or "[]")]
        if c["id"] not in ids and not any(f and len(f) >= 2 and f in text for f in forms):
            continue
        item = {"name": c["display_name_zh"] or c["canonical_name"], "ja": c["canonical_name"]}
        if c["gender"] in ("female", "male"):
            item["gender"] = c["gender"]
        if c["voice"]:
            item["voice"] = c["voice"]
        if habits.get(c["canonical_name"]):
            item["habits"] = habits[c["canonical_name"]]
        out.append(item)
    return out[:30]


FIX_NOTE = ("對照校稿認為這段譯文有誤（見說明）。請對照原文與前後文判斷：說明正確就改正，只改那個錯，其他照舊；"
            "說明不對（原文其實就是這個意思），就維持原譯。")
