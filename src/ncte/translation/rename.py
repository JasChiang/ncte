"""Carry a changed rendering into translations that already exist (ADR-029).

A name or form of address decided after translation leaves the old rendering in the paragraphs already translated.
``affected_units`` finds them (the form in the source, the old rendering in the current translation); the user can
replace the rendering in place (free, for a plain name) or retranslate those units. Human translations are never
changed here: they are listed so the user can edit them.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..errors import NCTEError
from ..memory.db import Transaction, utcnow
from ..series import scope_clause
from .cache import latest_translation

MIN_REPLACE = 2  # a one-character rendering (「社」) also occurs inside other words: retranslate instead


def affected_units(conn: sqlite3.Connection, book_id: str, *, form: str, old_zh: str, address_id: int | None = None,
                   added: bool = False) -> list[dict[str, Any]]:
    """Units of the series whose source has ``form`` and whose current translation contains ``old_zh``. With an
    ``address_id``, only the quotes in which that speaker used the form for that person."""
    from ..analysis.naming import term_pattern

    if not form or not old_zh:
        return []
    if address_id is not None:
        rows = conn.execute("SELECT DISTINCT u.* FROM address_observations o JOIN utterances ut ON ut.id=o.utterance_id "
                            "JOIN units u ON u.id=ut.unit_id WHERE o.address_id=? ORDER BY u.book_id, u.ordinal",
                            (address_id,)).fetchall()
    else:
        where, ids = scope_clause(conn, book_id, "u.book_id")
        pat = term_pattern(form)
        rows = [r for r in conn.execute(f"SELECT u.* FROM units u WHERE {where} AND u.translatable=1 AND "
                                        "instr(u.plain_text, ?) > 0 ORDER BY u.book_id, u.ordinal", (*ids, form[:1]))
                if pat.search(match_text(r["plain_text"]))]
    if added and len(old_zh) >= MIN_REPLACE:
        # the translator also wrote the name where the source leaves it implied (「…と高らかに笑って」 → 「加爾德如此
        # 放聲大笑」): a person's name rendering is distinctive enough to find in any paragraph
        seen = {r["id"] for r in rows}
        where, ids = scope_clause(conn, book_id, "u.book_id")
        rows = sorted([*rows, *(r for r in conn.execute(
            f"SELECT u.* FROM units u JOIN translations t ON t.unit_id=u.id WHERE {where} AND u.translatable=1 AND "
            "instr(t.text, ?) > 0 GROUP BY u.id", (*ids, old_zh)) if r["id"] not in seen)],
            key=lambda r: (r["book_id"], r["ordinal"]))
    out = []
    for r in rows:
        tr = latest_translation(conn, r["id"], r["source_hash"])
        if tr is None or old_zh not in tr["text"]:
            continue
        out.append({"unit_id": r["id"], "book_id": r["book_id"], "ordinal": r["ordinal"], "human": tr["status"] == "human",
                    "ja": match_text(r["plain_text"])[:80], "zh": tr["text"]})
    return out


def replace_rendering(conn: sqlite3.Connection, units: list[dict[str, Any]], old_zh: str, new_zh: str) -> int:
    """Write a new machine translation for each listed unit with ``old_zh`` replaced by ``new_zh`` (human
    translations are skipped). Returns the number of units changed."""
    if len(old_zh) < MIN_REPLACE:
        raise NCTEError(f"「{old_zh}」 is too short to replace safely (it may be part of other words); retranslate instead")
    if not new_zh or new_zh == old_zh:
        raise NCTEError("nothing to replace")
    n = 0
    with Transaction(conn):
        for u in units:
            if u["human"]:
                continue
            src = conn.execute("SELECT source_hash FROM units WHERE id=?", (u["unit_id"],)).fetchone()
            tr = latest_translation(conn, u["unit_id"], src[0], include_human=False)
            if tr is None or old_zh not in tr["text"]:
                continue
            flags = json.loads(tr["flags_json"] or "[]") + ["renamed"]
            conn.execute(
                "INSERT INTO translations(unit_id, target_lang, text, source_hash, context_hash, prompt_version, model_id, "
                "glossary_version, mode, status, run_id, request_id, flags_json, notes_json, created_at, translator_note) "
                "SELECT unit_id, target_lang, ?, source_hash, context_hash, prompt_version, model_id, glossary_version, mode, "
                "status, run_id, request_id, ?, notes_json, ?, translator_note FROM translations WHERE id=?",
                (tr["text"].replace(old_zh, new_zh), json.dumps(flags, ensure_ascii=False), utcnow(), tr["id"]))
            n += 1
    return n


def for_proposal(conn: sqlite3.Connection, book_id: str, pid: int, old_zh: str) -> list[dict[str, Any]]:
    """Affected units for a naming proposal (name: every use of the form; address: that speaker → person only)."""
    row = conn.execute("SELECT * FROM naming_proposals WHERE id=?", (pid,)).fetchone()
    if row is None or row["kind"] == "character":
        return []
    if row["kind"] == "name":
        from ..series import SERIES_SCOPE

        g = conn.execute(f"SELECT category FROM glossary WHERE {SERIES_SCOPE} AND source_term=?",
                         (book_id, row["ref"])).fetchone()
        return affected_units(conn, book_id, form=row["ref"], old_zh=old_zh,
                              added=g is not None and g["category"] == "character")
    a = conn.execute("SELECT form_ja FROM address_terms WHERE id=?", (int(row["ref"]),)).fetchone()
    return affected_units(conn, book_id, form=a[0], old_zh=old_zh, address_id=int(row["ref"])) if a else []
