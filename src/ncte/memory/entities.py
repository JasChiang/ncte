"""Entities and name forms (ADR-019).

One *entity* (a character, team, place, item …) has several *name forms* in the source, each with its own
Chinese rendering and type::

    entity 黑鐵 (organization)
        クロガネ      official  → 黑鐵
        黒鬼          nickname  → …      note: the fans' nickname (never the slur 黑鬼 in Chinese)
    entity 索拉 (character)
        ソラ  official → 索拉 · 天野ソラ full → 天野索拉 · ソラっち nickname → 小索拉

Every glossary row is a form; ``glossary.entity_id`` groups forms. Links between forms need evidence:
* ``pattern``  – the source says so («クロガネ。通称黒鬼»), detected by the program
* ``llm``      – proposed by the memory-update model; both names must occur verbatim in the scene and the
                 evidence quote must be verbatim (otherwise only a *candidate*)
* ``human``    – ``ncte names link``
Each link stores the ordinal where it was revealed; translation requests only see forms/links revealed so far.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..series import SERIES_SCOPE, volumes
from .db import utcnow

FORM_TYPES = ("official", "full", "short", "nickname", "alias", "title")
MARKERS = {"通称": "nickname", "通稱": "nickname", "愛称": "nickname", "あだ名": "nickname", "渾名": "nickname",
           "呼び名": "nickname", "ニックネーム": "nickname", "略称": "short", "略して": "short", "本名": "official"}
_MARKER_RE = re.compile(r"(" + "|".join(map(re.escape, MARKERS)) + r")(?:は|が|も|の|で)?[「『]?")
_RIGHT_TERM = re.compile(r"([^\s　、。，,「」『』！？!?…・（）()]{2,16}?)(?=[」』]|の|と|で|、|。|は|が|を|も|$)")


def entity_id_for(book_id: str, label: str) -> str:
    return "e_" + hashlib.sha256(f"{book_id}|{label}".encode()).hexdigest()[:12]


def _new_entity(conn: sqlite3.Connection, book_id: str, label: str, kind: str, character_id: str | None) -> str:
    eid = entity_id_for(book_id, label)
    conn.execute("INSERT OR IGNORE INTO entities(id, book_id, kind, label_ja, character_id, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?)", (eid, book_id, kind, label, character_id, utcnow(), utcnow()))
    return eid


def ensure_entity(conn: sqlite3.Connection, book_id: str, term: str) -> str | None:
    """Make sure the glossary form ``term`` belongs to an entity; returns the entity id."""
    row = conn.execute(f"SELECT * FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, term)).fetchone()
    if row is None:
        return None
    if row["entity_id"]:
        return str(row["entity_id"])
    ch = conn.execute(f"SELECT id FROM characters WHERE {SERIES_SCOPE} AND canonical_name=?", (book_id, term)).fetchone()
    eid = _new_entity(conn, book_id, term, row["category"], ch[0] if ch else None)
    conn.execute("UPDATE glossary SET entity_id=? WHERE id=?", (eid, row["id"]))
    return eid


def backfill_entities(conn: sqlite3.Connection, book_id: str) -> int:
    """Give every glossary row an entity; group validated character aliases with their character."""
    for (term,) in conn.execute(f"SELECT source_term FROM glossary WHERE {SERIES_SCOPE} AND entity_id IS NULL",
                                (book_id,)).fetchall():
        ensure_entity(conn, book_id, term)
    n = 0
    import json

    for c in conn.execute(f"SELECT canonical_name, aliases_json FROM characters WHERE {SERIES_SCOPE}", (book_id,)).fetchall():
        target = conn.execute(f"SELECT entity_id FROM glossary WHERE {SERIES_SCOPE} AND source_term=?",
                              (book_id, c[0])).fetchone()
        if not target:
            continue
        for alias in json.loads(c[1]):
            if conn.execute(f"SELECT 1 FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, alias)).fetchone():
                n += merge_into(conn, book_id, alias, c[0], "alias")
    return n


def merge_into(conn: sqlite3.Connection, book_id: str, term: str, target: str, form_type: str,
               note: str | None = None) -> int:
    """Attach form ``term`` (and every other form of its current entity) to ``target``'s entity."""
    if form_type not in FORM_TYPES:
        raise ValueError(f"form_type must be one of {FORM_TYPES}")
    t_eid = ensure_entity(conn, book_id, target)
    s_eid = ensure_entity(conn, book_id, term)
    if t_eid is None or s_eid is None:
        return 0
    if s_eid != t_eid:
        conn.execute(f"UPDATE glossary SET entity_id=? WHERE {SERIES_SCOPE} AND entity_id=?", (t_eid, book_id, s_eid))
        conn.execute("DELETE FROM entities WHERE id=?", (s_eid,))
    conn.execute(f"UPDATE glossary SET form_type=?, form_note=COALESCE(?, form_note) WHERE {SERIES_SCOPE} AND source_term=?",
                 (form_type, note, book_id, term))
    return 1


def unlink(conn: sqlite3.Connection, book_id: str, term: str) -> None:
    row = conn.execute(f"SELECT * FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, term)).fetchone()
    if row is None:
        raise KeyError(term)
    eid = _new_entity(conn, book_id, term, row["category"], None)
    conn.execute("UPDATE glossary SET entity_id=?, form_type='official' WHERE id=?", (eid, row["id"]))
    conn.execute(f"UPDATE entity_links SET status='rejected' WHERE {SERIES_SCOPE} AND term=? AND status='accepted'",
                 (book_id, term))


def record_link(conn: sqlite3.Connection, book_id: str, term: str, target: str, form_type: str, *, origin: str,
                status: str, unit_id: str | None, quote: str | None, ordinal: int | None) -> None:
    dup = conn.execute(f"SELECT 1 FROM entity_links WHERE {SERIES_SCOPE} AND term=? AND target_term=? AND status=?",
                       (book_id, term, target, status)).fetchone()
    if dup:
        return
    conn.execute("INSERT INTO entity_links(book_id, term, target_term, form_type, evidence_unit_id, evidence_quote, "
                 "origin, status, revealed_ordinal, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (book_id, term, target, form_type, unit_id, (quote or "")[:300], origin, status, ordinal, utcnow()))


def link(conn: sqlite3.Connection, book_id: str, term: str, target: str, form_type: str, *, origin: str,
         unit_id: str | None = None, quote: str | None = None, ordinal: int | None = None,
         note: str | None = None) -> str:
    """Link two forms. Returns 'accepted' or 'candidate' (when one side has no glossary entry yet)."""
    known = {r[0] for r in conn.execute(f"SELECT source_term FROM glossary WHERE {SERIES_SCOPE} AND source_term IN (?,?)",
                                        (book_id, term, target))}
    if term in known and target in known:
        merge_into(conn, book_id, term, target, form_type, note)
        record_link(conn, book_id, term, target, form_type, origin=origin, status="accepted", unit_id=unit_id,
                    quote=quote, ordinal=ordinal)
        reconcile_characters(conn, book_id)
        return "accepted"
    record_link(conn, book_id, term, target, form_type, origin=origin, status="candidate", unit_id=unit_id,
                quote=quote, ordinal=ordinal)
    return "candidate"


def detect_links(conn: sqlite3.Connection, book_id: str, rows: list[sqlite3.Row]) -> list[tuple[str, str, str]]:
    """Find «A。通称B» style statements in ``rows``; returns (term, target, status) for each link made."""
    forms = sorted({r[0] for r in conn.execute(f"SELECT source_term FROM glossary WHERE {SERIES_SCOPE}", (book_id,))
                    if len(r[0]) >= 2}, key=len, reverse=True)
    out = []
    for r in rows:
        text = match_text(r["plain_text"])
        for m in _MARKER_RE.finditer(text):
            rt = _RIGHT_TERM.match(text[m.end():m.end() + 20])
            if not rt:
                continue
            right = rt.group(1)
            left_zone = text[max(0, m.start() - 40):m.start()]
            best = None
            for f in forms:
                pos = left_zone.rfind(f)
                if pos >= 0 and f != right and (best is None or pos + len(f) > best[1] + len(best[0])
                                                or (pos + len(f) == best[1] + len(best[0]) and len(f) > len(best[0]))):
                    best = (f, pos)
            if not best:
                continue
            kind = MARKERS[m.group(1)]
            if kind == "official":  # «Bの本名はA» – the right side is the real name
                term, target = best[0], right
                kind = "alias"
            else:
                term, target = right, best[0]
            status = link(conn, book_id, term, target, kind, origin="pattern", unit_id=r["id"],
                          quote=text[max(0, m.start() - 30):m.end() + 20], ordinal=r["ordinal"])
            out.append((term, target, status))
    return out


def entity_forms(conn: sqlite3.Connection, book_id: str) -> dict[str, list[dict[str, Any]]]:
    """entity_id → forms (with first-appearance ordinal for visibility filtering)."""
    rows = conn.execute(
        "SELECT g.*, u.ordinal AS first_ordinal, b.volume_index AS first_volume FROM glossary g LEFT JOIN units u ON "
        f"u.id=g.first_unit_id LEFT JOIN books b ON b.id=u.book_id WHERE g.{SERIES_SCOPE} AND g.entity_id IS NOT NULL "
        "ORDER BY g.id", (book_id,)).fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["entity_id"], []).append(
            {"ja": r["source_term"], "zh": r["target_term"], "form": r["form_type"], "note": r["form_note"],
             "verified": bool(r["verified"]), "category": r["category"], "first_ordinal": r["first_ordinal"],
             "first_pos": (float(r["first_volume"]), int(r["first_ordinal"])) if r["first_ordinal"] is not None else None})
    return out


def link_revealed(conn: sqlite3.Connection, book_id: str) -> dict[str, tuple[float, int]]:
    """term → story position at which its link to another form was first revealed (pattern/llm links)."""
    vols = volumes(conn)
    out: dict[str, tuple[float, int]] = {}
    for r in conn.execute(f"SELECT term, book_id, revealed_ordinal FROM entity_links WHERE {SERIES_SCOPE} AND "
                          "status='accepted' AND revealed_ordinal IS NOT NULL", (book_id,)):
        p = (vols.get(r[1], 1.0), int(r[2]))
        if r[0] not in out or p < out[r[0]]:
            out[r[0]] = p
    return out


def character_for_form(conn: sqlite3.Connection, book_id: str, name: str) -> str | None:
    """Character id for any name form of a character entity (official name, full name, nickname …)."""
    import json

    row = conn.execute(f"SELECT entity_id FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, name)).fetchone()
    forms = [name]
    if row and row[0]:
        forms = [r[0] for r in conn.execute(f"SELECT source_term FROM glossary WHERE {SERIES_SCOPE} AND entity_id=? "
                                            "ORDER BY form_type!='official'", (book_id, row[0]))]
    chars = conn.execute(f"SELECT id, canonical_name, aliases_json FROM characters WHERE {SERIES_SCOPE}",
                         (book_id,)).fetchall()
    for f in forms:
        for c in chars:
            if f == c[1] or f in json.loads(c[2]):
                return str(c[0])
    return None


def reconcile_characters(conn: sqlite3.Connection, book_id: str) -> int:
    """Single source of truth: characters whose names are forms of the same entity are merged into the
    character of the entity's official form (ADR-021)."""
    from .repository import merge_characters

    chars = {r[0]: r[1] for r in conn.execute(f"SELECT canonical_name, id FROM characters WHERE {SERIES_SCOPE}",
                                              (book_id,))}
    groups: dict[str, list[tuple[str, str]]] = {}
    for term, eid, form in conn.execute(f"SELECT source_term, entity_id, form_type FROM glossary WHERE {SERIES_SCOPE} "
                                        "AND entity_id IS NOT NULL", (book_id,)):
        if term in chars:
            groups.setdefault(eid, []).append((form, chars[term]))
    n = 0
    for members in groups.values():
        ids = list(dict.fromkeys(cid for _, cid in sorted(members, key=lambda m: m[0] != "official")))
        for drop in ids[1:]:
            merge_characters(conn, ids[0], drop)
            n += 1
    return n
