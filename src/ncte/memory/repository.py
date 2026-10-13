"""Memory repository: characters, utterances, speech examples, facts, summaries, props."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from ..analysis.dialogue import speech_profile
from ..series import SERIES_SCOPE
from .db import utcnow


def character_id_for(book_id: str, name_ja: str) -> str:
    return "c_" + hashlib.sha256(f"{book_id}|{name_ja}".encode()).hexdigest()[:12]


def fact_id_for(book_id: str, scene_id: str, n: int, text: str) -> str:
    return "f_" + hashlib.sha256(f"{book_id}|{scene_id}|{n}|{text}".encode()).hexdigest()[:12]


def utterance_id_for(unit_id: str, quote_index: int) -> str:
    return f"q_{unit_id[2:]}_{quote_index}"


def characters(conn: sqlite3.Connection, book_id: str) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE} ORDER BY known_from_ordinal", (book_id,)).fetchall()


def alias_map(rows: list[sqlite3.Row]) -> dict[str, str]:
    """name/alias (ja) → character id. Earlier-known characters win on collisions."""
    out: dict[str, str] = {}
    for r in rows:
        for name in [r["canonical_name"], *json.loads(r["aliases_json"])]:
            if name and name not in out:
                out[name] = r["id"]
    return out


def upsert_character(conn: sqlite3.Connection, book_id: str, name_ja: str, *, name_zh: str | None,
                     aliases: list[str], unit_id: str, ordinal: int, origin: str) -> tuple[str, bool]:
    row = conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE} AND canonical_name=?", (book_id, name_ja)).fetchone()
    cid = row["id"] if row else character_id_for(book_id, name_ja)
    if row:
        # seen earlier in the story (e.g. volume 1 translated after volume 2): move the first appearance back
        from ..series import pos

        if pos(conn, book_id, ordinal) < pos(conn, row["book_id"], row["known_from_ordinal"]):
            conn.execute("UPDATE characters SET book_id=?, known_from_unit_id=?, known_from_ordinal=?, updated_at=? "
                         "WHERE id=?", (book_id, unit_id, ordinal, utcnow(), row["id"]))
        old = json.loads(row["aliases_json"])
        merged = old + [a for a in aliases if a not in old and a != name_ja]
        if merged != old:
            conn.execute("UPDATE characters SET aliases_json=?, updated_at=? WHERE id=?",
                         (json.dumps(merged, ensure_ascii=False), utcnow(), cid))
        return cid, False
    conn.execute(
        "INSERT INTO characters(id, book_id, canonical_name, display_name_zh, aliases_json, known_from_unit_id, "
        "known_from_ordinal, status, origin, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (cid, book_id, name_ja, name_zh, json.dumps([a for a in aliases if a != name_ja], ensure_ascii=False),
         unit_id, ordinal, "partial", origin, utcnow(), utcnow()))
    return cid, True


def ensure_glossary(conn: sqlite3.Connection, book_id: str, source: str, target: str, *, category: str,
                    origin: str, first_unit_id: str | None, verified: bool = False) -> bool:
    if not source or not target:
        return False
    row = conn.execute(f"SELECT id FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, source)).fetchone()
    if row:
        return False
    conn.execute("INSERT INTO glossary(book_id, source_term, target_term, category, verified, origin, first_unit_id, "
                 "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                 (book_id, source, target, category, int(verified), origin, first_unit_id, utcnow()))
    from .entities import ensure_entity

    ensure_entity(conn, book_id, source)
    return True


def set_glossary(conn: sqlite3.Connection, book_id: str, source: str, target: str, *, category: str = "term",
                 verified: bool = True, note: str | None = None) -> dict[str, Any]:
    row = conn.execute(f"SELECT * FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, source)).fetchone()
    if row:
        conn.execute("UPDATE glossary SET target_term=?, verified=?, version=version+1, origin='user', note=?, "
                     "updated_at=? WHERE id=?", (target, int(verified), note, utcnow(), row["id"]))
        if row["category"] == "character" or category == "character":
            conn.execute(f"UPDATE characters SET display_name_zh=?, updated_at=? WHERE {SERIES_SCOPE} AND canonical_name=?",
                         (target, utcnow(), book_id, source))
        return {"previous": row["target_term"], "version": row["version"] + 1}
    conn.execute("INSERT INTO glossary(book_id, source_term, target_term, category, verified, origin, note, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (book_id, source, target, category, int(verified), "user", note, utcnow()))
    from .entities import ensure_entity

    ensure_entity(conn, book_id, source)
    return {"previous": None, "version": 1}


def add_speech_example(conn: sqlite3.Connection, character_id: str, unit_id: str, utterance_id: str, raw: str,
                       status: str) -> None:
    conn.execute("INSERT OR IGNORE INTO speech_examples(character_id, unit_id, utterance_id, raw_text, "
                 "verification_status, created_at) VALUES (?,?,?,?,?,?)",
                 (character_id, unit_id, utterance_id, raw, status, utcnow()))


def remove_speech_examples_for(conn: sqlite3.Connection, utterance_id: str) -> None:
    conn.execute("DELETE FROM speech_examples WHERE utterance_id=?", (utterance_id,))


def refresh_profiles(conn: sqlite3.Connection, book_id: str) -> None:
    for c in characters(conn, book_id):
        ex = [r["raw_text"] for r in conn.execute(
            "SELECT raw_text FROM speech_examples WHERE character_id=? AND verification_status IN "
            "('explicit','human_verified') ORDER BY id", (c["id"],))]
        prof = speech_profile(ex) if ex else {}
        conn.execute("UPDATE characters SET speech_profile_json=? WHERE id=?",
                     (json.dumps(prof, ensure_ascii=False), c["id"]))


def reject(conn: sqlite3.Connection, book_id: str, scene_id: str | None, stage: str, item: Any, reason: str) -> None:
    conn.execute("INSERT INTO memory_rejections(book_id, scene_id, stage, item_json, reason, created_at) VALUES "
                 "(?,?,?,?,?,?)", (book_id, scene_id, stage, json.dumps(item, ensure_ascii=False, default=str),
                                   reason, utcnow()))


def merge_characters(conn: sqlite3.Connection, keep_id: str, drop_id: str) -> None:
    """Fold character ``drop_id`` into ``keep_id``: utterances, hypotheses, speech examples, facts and entity
    references follow; the dropped name becomes an alias; the earliest first appearance is kept."""
    if keep_id == drop_id:
        return
    keep = conn.execute("SELECT * FROM characters WHERE id=?", (keep_id,)).fetchone()
    drop = conn.execute("SELECT * FROM characters WHERE id=?", (drop_id,)).fetchone()
    if keep is None or drop is None:
        return
    for sql in ("UPDATE utterances SET speaker_id_nullable=? WHERE speaker_id_nullable=?",
                "UPDATE speaker_hypotheses SET candidate_character_id=? WHERE candidate_character_id=?",
                "UPDATE OR IGNORE speech_examples SET character_id=? WHERE character_id=?",
                "UPDATE facts SET subject=? WHERE subject=?",
                "UPDATE entities SET character_id=? WHERE character_id=?"):
        conn.execute(sql, (keep_id, drop_id))
    conn.execute("DELETE FROM speech_examples WHERE character_id=?", (drop_id,))
    aliases = json.loads(keep["aliases_json"])
    for a in [drop["canonical_name"], *json.loads(drop["aliases_json"])]:
        if a and a != keep["canonical_name"] and a not in aliases:
            aliases.append(a)
    conn.execute("UPDATE characters SET aliases_json=?, updated_at=? WHERE id=?",
                 (json.dumps(aliases, ensure_ascii=False), utcnow(), keep_id))
    from ..series import pos

    if pos(conn, drop["book_id"], drop["known_from_ordinal"]) < pos(conn, keep["book_id"], keep["known_from_ordinal"]):
        conn.execute("UPDATE characters SET book_id=?, known_from_unit_id=?, known_from_ordinal=? WHERE id=?",
                     (drop["book_id"], drop["known_from_unit_id"], drop["known_from_ordinal"], keep_id))
    conn.execute("DELETE FROM characters WHERE id=?", (drop_id,))
    refresh_profiles(conn, keep["book_id"])
