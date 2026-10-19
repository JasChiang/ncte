"""A bilingual proofreading pass (ADR-051).

The reader's look reads the Chinese alone and cannot see a sentence that reads well and says something else: a fixed
phrase taken literally (「いやまったく」 as an apology), a negation lost, a subject swapped, a person addressed as 「那位」
then 「你」. The selective review sees only flagged paragraphs. Here, after a volume is translated, every paragraph
goes to the model with its source, who says each line and to whom (as read), the cards of the people in that stretch
(name, gender, way of speaking), the decided renderings, forms of address and house rules the translator had, and the
paragraphs just before the stretch (to read, not to check); the model reads the stretch through first, then
checks paragraph by paragraph and points at real errors only — meaning, omission, addition, speaker or addressee,
tone, a character's voice, a house-style slip — and each goes back to the translator with the source and the scene's
memory, which fixes it or keeps the rendering. The program finds the paragraphs and the people; it decides nothing.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..epub.units import PH_RE
from ..memory.repository import character_basics
from ..series import pos
from ..translation.cache import latest_texts

CHUNK_UNITS = 60  # paragraphs per request: source and translation side by side (config: proofread_chunk_units)
BEFORE_UNITS = 6  # paragraphs before the stretch, given to read (not checked)


_UNITS = ("SELECT * FROM units WHERE book_id=? AND translatable=1 AND kind IN ('text','heading') AND "
          "spine_index IS NOT NULL")


def chunks(conn: sqlite3.Connection, book_id: str, unit_ids: set[str] | None = None,
           size: int = CHUNK_UNITS, after: int | None = None,
           story: set[str] | None = None, since: int | None = None) -> list[list[dict[str, Any]]]:
    """Consecutive translated paragraphs, chapter by chapter, each with its source and who says its lines.
    ``after``: only paragraphs after this ordinal (a resumed pass; a stretch starts afresh after each one checked, so
    the stretches are the same as in the first run). ``story``: only paragraphs of these files (the story without
    covers, table of contents, afterword or colophon, ADR-024, when ``context.skip_book_matter``); None: every file.
    ``since``: only paragraphs whose current translation is newer than this translation id (2.11)."""
    from ..memory.retrieval import unit_actions
    from ..memory.visibility import form_positions, visible_names

    best = latest_texts(conn, book_id)
    actions = unit_actions(conn, book_id)  # who does what where the subject is left out, as read (ADR-067)
    forms_at = form_positions(conn, book_id)
    names_at: dict[str, dict[str, str]] = {}  # a speaker is named as known there, never by a later reveal
    said: dict[str, set[str]] = {}
    guessed: set[tuple[str, str]] = set()  # (unit, speaker) read as theirs, not stated in the text
    told: dict[str, dict[str, bool]] = {}  # unit → who its lines are said to → stated (False: read from the scene)
    for u in conn.execute("SELECT unit_id, speaker_id_nullable sp, status, addressee_ids_json, addressee_status FROM "
                          "utterances WHERE book_id=?", (book_id,)):
        for x in json.loads(u["addressee_ids_json"] or "[]"):
            sure = told.setdefault(u["unit_id"], {})
            sure[x] = sure.get(x, False) or u["addressee_status"] == "explicit"
        if u["sp"] is None:
            continue
        said.setdefault(u["unit_id"], set()).add(u["sp"])
        if u["status"] == "inferred":
            guessed.add((u["unit_id"], u["sp"]))
    out: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    href = None
    for r in conn.execute(_UNITS + " AND ordinal>? ORDER BY ordinal", (book_id, -(1 << 62) if after is None else after)):
        t = best.get(r["id"])
        if t is None or (unit_ids is not None and r["id"] not in unit_ids) or (story is not None and
                                                                             r["resource_href"] not in story):
            continue
        if since is not None and t["id"] <= since:  # looked at in an earlier whole pass, unchanged since
            continue
        if cur and (r["resource_href"] != href and len(cur) >= size // 3 or len(cur) >= size):
            out.append(cur)
            cur = []
        href = r["resource_href"]
        # the source as written: 《》 may be a reading (漢字《かな》) or the author's own brackets (「…、《亡雄の旅団》」);
        # stripping it would make the translation look like it added words
        item: dict[str, Any] = {"unit_id": r["id"], "ja": r["plain_text"], "zh": PH_RE.sub("", t["text"]),
                                "_tid": t["id"], "_at": r["ordinal"], "_href": r["resource_href"]}  # program only
        if r["id"] in actions:
            item["actions"] = actions[r["id"]]
        if said_by_translator := json.loads(t["notes_json"] or "[]"):  # what it left for whoever checks (ADR-067)
            item["translator_notes"] = said_by_translator
        if r["id"] in said or r["id"] in told:
            key = r["scene_id"] or str(r["ordinal"])
            if (names := names_at.get(key)) is None:  # once per scene, as known at its end (as its translation was)
                end = conn.execute("SELECT MAX(ordinal) FROM units WHERE book_id=? AND scene_id=?",
                                   (book_id, r["scene_id"])).fetchone()[0] if r["scene_id"] else None
                names = names_at[key] = visible_names(conn, book_id, end or r["ordinal"], forms_at)
            if r["id"] in said:
                who = sorted(names.get(x, "?") + ("（推測）" if (r["id"], x) in guessed else "") for x in said[r["id"]])
                item["speaker"] = "、".join(who)
            if to := sorted(names[x] + ("" if sure else "（推測）") for x, sure in told.get(r["id"], {}).items()
                            if x in names):
                item["to"] = "、".join(to)
            item["_who"] = sorted(said.get(r["id"], set()) | set(told.get(r["id"], {})))
        cur.append(item)
    if cur:
        out.append(cur)
    return out


def tail(conn: sqlite3.Connection, book_id: str, upto: int, unit_ids: set[str] | None = None,
         n: int = BEFORE_UNITS, story: set[str] | None = None) -> list[dict[str, Any]]:
    """The last ``n`` translated paragraphs up to ``upto`` (of the ``story`` files, when given): the stretch before,
    for a pass resumed after it."""
    best = latest_texts(conn, book_id)
    out: list[dict[str, Any]] = []
    for r in conn.execute(_UNITS + " AND ordinal<=? ORDER BY ordinal DESC", (book_id, upto)):
        t = best.get(r["id"])
        if t is None or (unit_ids is not None and r["id"] not in unit_ids) or (story is not None and
                                                                             r["resource_href"] not in story):
            continue
        out.append({"unit_id": r["id"], "ja": r["plain_text"], "zh": PH_RE.sub("", t["text"]), "_tid": t["id"],
                    "_at": r["ordinal"], "_href": r["resource_href"]})
        if len(out) >= n:
            break
    return out[::-1]


def cast(conn: sqlite3.Connection, book_id: str, part: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The people who speak in this stretch (and anyone named in it): name, gender, way of speaking."""
    from ..memory.style import load_style
    from ..memory.visibility import character_view

    ids = {x for p in part for x in p.get("_who", [])}
    text = "".join(p["ja"] for p in part)
    start = min((p["_at"] for p in part if "_at" in p), default=None)
    habits = load_style(conn, book_id).voices  # the house style's speech habits (ADR-053), by Japanese name
    out = []
    from ..memory.repository import impression_at

    for c in conn.execute("SELECT c.*, g.gender FROM characters c LEFT JOIN character_gender g ON g.character_id=c.id "
                          "WHERE c.book_id=?", (book_id,)):
        forms = [c["canonical_name"], *json.loads(c["aliases_json"] or "[]")]
        if c["id"] not in ids and not any(f and len(f) >= 2 and f in text for f in forms):
            continue
        # the card as known where the stretch begins: 「神祕少女」, not the name a later chapter reveals
        view = character_view(conn, book_id, c, start) if start is not None else None
        if start is not None and view is None:
            view = character_view(conn, book_id, c, max(p["_at"] for p in part))
        item: dict[str, Any] = {"name": (view["name_zh"] or view["name_ja"]) if view else c["display_name_zh"] or
                                c["canonical_name"], "ja": view["name_ja"] if view else c["canonical_name"]}
        # gender and narrator as known there too (a gender revealed later must not judge an earlier 你)
        basics = view if view is not None else character_basics(
            conn, c["id"], pos(conn, book_id, max(p["_at"] for p in part)) if start is not None else None)
        item.update({k: basics[k] for k in ("gender", "narrator") if k in basics})
        if imp := impression_at(conn, c["id"], start):  # the way of speaking in force where the stretch begins
            item["voice"] = imp["voice"]
        if habits.get(c["canonical_name"]):
            item["habits"] = habits[c["canonical_name"]]
        out.append(item)
    return out[:30]


def notes(conn: sqlite3.Connection, book_id: str, part: list[dict[str, Any]], before: list[dict[str, Any]], *,
          glossary: list[dict[str, Any]], volume: float, revealed: dict[str, tuple[float, int]],
          definitions: bool = False) -> dict[str, Any]:
    """What the translator had for judging this stretch, as known there: the paragraphs just before it (to read, not
    to check), decided renderings, how these speakers call these people, the house rules; with ``definitions``, what
    the terms in it mean as shown by its end (ADR-069)."""
    from ..memory.address import context_items
    from ..memory.style import load_style, style_payload
    from ..memory.visibility import visible_names
    from ..translation.context_builder import glossary_payload
    from ..translation.context_pipeline import drop_address_forms

    text = " ".join(p["ja"] for p in part)
    start = min(p["_at"] for p in part)
    end = max(p["_at"] for p in part)
    out: dict[str, Any] = {}
    if before:
        out["context_before"] = [{"ja": b["ja"], "zh": b["zh"]} for b in before]
    gl = glossary_payload(glossary, text, upto=(volume, end), revealed=revealed)
    names = visible_names(conn, book_id, end)
    ids = {x for p in part for x in p.get("_who", [])}
    address = context_items(conn, book_id, start, text, ids, names, ids, upto=end)
    if address:
        out["address_terms"] = address
        gl = drop_address_forms(gl, address)
    if gl:
        out["glossary"] = gl
    if style := style_payload(load_style(conn, book_id), text, [names[x] for x in ids if x in names]):
        out["style"] = style
    if definitions:
        from ..memory.terms import definitions_for

        if defs := definitions_for(conn, book_id, text, end):
            out["term_definitions"] = defs
    return out


FIX_NOTE = ("對照校稿認為這段譯文有誤（見說明）。請對照原文與前後文判斷：說明正確就改正，只改那個錯，其他照舊；"
            "說明不對（原文其實就是這個意思），就維持原譯。")
