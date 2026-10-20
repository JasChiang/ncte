"""Translation version lookup and cache-invalidation keys (spec §7 快取失效規則).

A unit is skipped as "already translated" only if its latest valid translation matches the
current ``source_hash``, translation prompt version, model, mode and the hash of the glossary
entries *relevant to that unit*. Changing one character name therefore invalidates exactly the
units that mention it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from ..memory.db import utcnow
from ..series import SERIES_SCOPE

VALID_MACHINE = ("succeeded", "needs_review")


def glossary_entries(conn: sqlite3.Connection, book_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT g.*, u.ordinal AS first_ordinal, b.volume_index AS first_volume FROM glossary g "
                        "LEFT JOIN units u ON u.id=g.first_unit_id LEFT JOIN books b ON b.id=u.book_id "
                        f"WHERE g.{SERIES_SCOPE} ORDER BY length(g.source_term) DESC",
                        (book_id,)).fetchall()
    aliases: dict[str, list[str]] = {}
    try:
        for c in conn.execute(f"SELECT canonical_name, aliases_json FROM characters WHERE {SERIES_SCOPE}", (book_id,)):
            aliases[c[0]] = json.loads(c[1])
    except sqlite3.OperationalError:  # pre-migration-2 database
        pass
    from ..memory.entities import senses

    shared = senses(conn, book_id)
    out = []
    for r in rows:
        out.append({"source_term": r["source_term"], "target_term": r["target_term"], "category": r["category"],
                    "verified": bool(r["verified"]), "version": r["version"], "origin": r["origin"],
                    "first_unit_id": r["first_unit_id"], "aliases": aliases.get(r["source_term"], []),
                    "entity_id": r["entity_id"], "form_type": r["form_type"], "form_note": r["form_note"],
                    "first_ordinal": r["first_ordinal"], "senses": shared.get(r["source_term"], []),
                    "readings": json.loads(r["readings_json"] or "[]"),
                    "first_pos": (float(r["first_volume"]), int(r["first_ordinal"]))
                    if r["first_ordinal"] is not None else None})
    return out


def relevant_glossary(entries: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    from ..epub.units import match_text

    t = match_text(text)
    return [g for g in entries if g["source_term"] and g["source_term"] in t]


def glossary_hash(entries: list[dict[str, Any]]) -> str:
    """Cache key part: only human-verified entries count (ADR-018). Automatic candidates discovered later in
    the book must not make earlier, already-correct translations stale; a human decision must."""
    blob = json.dumps(sorted((g["source_term"], g["target_term"]) for g in entries if g.get("verified")),
                      ensure_ascii=False)
    return "gv:" + hashlib.sha256(blob.encode()).hexdigest()[:12]


def latest_translation(conn: sqlite3.Connection, unit_id: str, source_hash: str,
                       *, include_human: bool = True) -> sqlite3.Row | None:
    if include_human:
        row = conn.execute("SELECT * FROM translations WHERE unit_id=? AND status='human' AND source_hash=? "
                           "ORDER BY id DESC LIMIT 1", (unit_id, source_hash)).fetchone()
        if row:
            return row
    return conn.execute(
        f"SELECT * FROM translations WHERE unit_id=? AND source_hash=? AND status IN "
        f"({','.join('?' * len(VALID_MACHINE))}) ORDER BY id DESC LIMIT 1",
        (unit_id, source_hash, *VALID_MACHINE)).fetchone()


def settle_translation(conn: sqlite3.Connection, translation_id: int) -> bool:
    """A translation waiting for review is cleared (``needs_review`` → ``succeeded``): the translate job of the run that
    wrote it is cleared with it, in the caller's transaction, so the job never says "waiting" for a unit whose text is
    done (a whole-book run left five such jobs). Returns whether the translation changed."""
    row = conn.execute("SELECT unit_id, run_id FROM translations WHERE id=? AND status='needs_review'",
                       (translation_id,)).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE translations SET status='succeeded' WHERE id=?", (translation_id,))
    conn.execute("UPDATE jobs SET status='succeeded', updated_at=? WHERE run_id=? AND stage='translate' AND target_id=? "
                 "AND status='needs_review'", (utcnow(), row[1], row[0]))
    return True


def prompt_major(version_id: str | None) -> str:
    """``translate/v1#a5b603e6`` → ``translate/v1``."""
    return (version_id or "").split("#", 1)[0]


def is_cached(row: sqlite3.Row | None, *, prompt_version: str, model: str, mode: str, gl_hash: str) -> bool:
    """Prompt *revisions* (same vN file edited) do not invalidate translations; a new prompt version (vN+1) does.
    Revisions are reported by the dry run so the user can decide (ADR-019)."""
    if row is None:
        return False
    if row["status"] == "human":
        return True
    return (prompt_major(row["prompt_version"]) == prompt_major(prompt_version) and row["model_id"] == model
            and row["mode"] == mode and row["glossary_version"] == gl_hash)


def latest_texts(conn: sqlite3.Connection, book_id: str) -> dict[str, sqlite3.Row]:
    """unit_id → best current translation row (human > latest machine), matching source_hash."""
    out: dict[str, sqlite3.Row] = {}
    rows = conn.execute(
        "SELECT t.* FROM translations t JOIN units u ON u.id=t.unit_id WHERE u.book_id=? AND "
        "t.source_hash=u.source_hash AND t.status IN ('human','succeeded','needs_review') ORDER BY t.id",
        (book_id,)).fetchall()
    for r in rows:
        cur = out.get(r["unit_id"])
        if cur is not None and cur["status"] == "human" and r["status"] != "human":
            continue
        out[r["unit_id"]] = r
    return out
