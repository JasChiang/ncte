"""Entities and name forms (ADR-019).

One *entity* (a character, team, place, item …) has several *name forms* in the source, each with its own
Chinese rendering and type::

    entity 黑鐵 (organization)
        ハガネ      official  → 黑鐵
        黒鬼          nickname  → …      note: the fans' nickname (never the slur 黑鬼 in Chinese)
    entity 索拉 (character)
        ソラ  official → 索拉 · 天野ソラ full → 天野索拉 · ソラっち nickname → 小索拉

Every glossary row is a form; ``glossary.entity_id`` groups forms. Links between forms need evidence:
* ``llm``      – proposed by the memory-update model; both names must occur verbatim in the scene and the
                 evidence quote must be verbatim (otherwise only a *candidate*)
* ``human``    – ``ncte names link``
Each link stores the ordinal where it was revealed; translation requests only see forms/links revealed so far.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from ..series import SERIES_SCOPE, volumes
from .db import utcnow
from .repository import FINDABLE

FORM_TYPES = ("official", "full", "short", "nickname", "alias", "title")


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
    # the card named so; never one only known by other cards' words or one of another time (ADR-103)
    ch = conn.execute(f"SELECT id FROM characters WHERE {SERIES_SCOPE} AND canonical_name=? AND {FINDABLE}",
                      (book_id, term)).fetchone()
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

    # a card a name finds: the aliases of one known only by other cards' words, or of one of another time, are those
    # cards' own words and not forms of the entity named like them (ADR-103)
    for c in conn.execute(f"SELECT canonical_name, aliases_json, known_from_ordinal FROM characters WHERE {SERIES_SCOPE} "
                          f"AND {FINDABLE}",
                          (book_id,)).fetchall():
        target = conn.execute(f"SELECT entity_id FROM glossary WHERE {SERIES_SCOPE} AND source_term=?",
                              (book_id, c[0])).fetchone()
        if not target:
            continue
        for alias in json.loads(c[1]):
            if conn.execute(f"SELECT 1 FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, alias)).fetchone():
                # no link here: the model never said the two cards are one, so the explicit-link exception does not apply
                if is_shared(conn, book_id, alias) or shared_word(conn, book_id, alias, c[0]) is not None \
                        or _two_cards(conn, book_id, alias, c[0]):
                    # a word another card has too (ADR-108)
                    add_sense(conn, book_id, alias, c[0], origin="backfill", ordinal=c[2])
                    continue
                n += merge_into(conn, book_id, alias, c[0], "alias")
    return n


def _entity_of(conn: sqlite3.Connection, book_id: str, term: str) -> str | None:
    row = conn.execute(f"SELECT entity_id FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, term)).fetchone()
    return str(row[0]) if row and row[0] else None


def _form_count(conn: sqlite3.Connection, book_id: str, eid: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM glossary WHERE {SERIES_SCOPE} AND entity_id=?", (book_id, eid))
               .fetchone()[0])


def _joins_two_entities(conn: sqlite3.Connection, book_id: str, term: str, target: str) -> bool:
    """True when linking would fuse two entities that each already have several forms."""
    s_eid, t_eid = _entity_of(conn, book_id, term), _entity_of(conn, book_id, target)
    return (s_eid is not None and t_eid is not None and s_eid != t_eid
            and _form_count(conn, book_id, s_eid) > 1 and _form_count(conn, book_id, t_eid) > 1)


def _cards_of(conn: sqlite3.Connection, book_id: str, eid: str | None) -> set[str]:
    """The cards (findable characters, the ones ``reconcile_characters`` merges) named by a form of the entity."""
    if eid is None:
        return set()
    return {r[0] for r in conn.execute(
        f"SELECT canonical_name FROM characters WHERE {SERIES_SCOPE} AND {FINDABLE} AND canonical_name IN "
        f"(SELECT source_term FROM glossary WHERE {SERIES_SCOPE} AND entity_id=?)", (book_id, book_id, eid))}


def _two_cards(conn: sqlite3.Connection, book_id: str, a: str, b: str) -> bool:
    """The two forms belong to different entities that each have a card."""
    a_eid, b_eid = _entity_of(conn, book_id, a), _entity_of(conn, book_id, b)
    return a_eid is not None and b_eid is not None and a_eid != b_eid and bool(_cards_of(conn, book_id, a_eid)) \
        and bool(_cards_of(conn, book_id, b_eid))


def shared_word(conn: sqlite3.Connection, book_id: str, term: str, target: str) -> str | None:
    """For an automatic link of ``term`` to ``target``: the word to record as shared (it may refer to either), or None
    when the link merges. A link naming the only card on each side, by names that name only them, is the model's
    statement that they are one person and merges; otherwise a link that would fuse two established entities, or two cards, is a shared word — the side that is not a
    card's name (「母上」 in 「実母 → 母上」), else ``term`` (ADR-108)."""
    s_eid, t_eid = _entity_of(conn, book_id, term), _entity_of(conn, book_id, target)
    if s_eid is None or t_eid is None or s_eid == t_eid:
        return None
    # a word already shared may name either person: whichever it is linked to is one more person it may name, never
    # a reason to move the people it already names (third review: 「母上」 → リリア took トーネの母親 with it)
    for word in (term, target):
        if is_shared(conn, book_id, word):
            return word
    s_cards, t_cards = _cards_of(conn, book_id, s_eid), _cards_of(conn, book_id, t_eid)
    # the link names the one card on each side, by a name that names only it: the model says the two are one
    if s_cards == {term} and t_cards == {target} and not is_shared(conn, book_id, term) \
            and not is_shared(conn, book_id, target):
        return None
    if not (_joins_two_entities(conn, book_id, term, target) or (s_cards and t_cards)):
        return None
    return target if term in s_cards else term


def is_shared(conn: sqlite3.Connection, book_id: str, term: str) -> bool:
    """A form recorded as possibly referring to more than one entity (ADR-022/108)."""
    return conn.execute(f"SELECT 1 FROM form_senses WHERE {SERIES_SCOPE} AND term=?", (book_id, term)).fetchone() is not None


def add_sense(conn: sqlite3.Connection, book_id: str, term: str, target: str, *, origin: str, note: str | None = None,
              unit_id: str | None = None, quote: str | None = None, ordinal: int | None = None) -> bool:
    """``term`` (keeping its own entity) may also refer to ``target``'s entity: a shared name form (ADR-022)."""
    s_eid, t_eid = _entity_of(conn, book_id, term), ensure_entity(conn, book_id, target)
    if s_eid is None or t_eid is None or s_eid == t_eid:
        return False
    conn.execute("INSERT OR IGNORE INTO form_senses(book_id, term, entity_id, note, origin, evidence_unit_id, "
                 "evidence_quote, revealed_ordinal, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (book_id, term, t_eid, note, origin, unit_id, (quote or "")[:300] or None, ordinal, utcnow()))
    return True


def senses(conn: sqlite3.Connection, book_id: str) -> dict[str, list[dict[str, Any]]]:
    """term → the *other* entities a shared form may refer to (with the story position it became known)."""
    vols = volumes(conn)
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(f"SELECT * FROM form_senses WHERE {SERIES_SCOPE}", (book_id,)):
        out.setdefault(r["term"], []).append(
            {"entity_id": r["entity_id"], "note": r["note"], "origin": r["origin"],
             "pos": (vols.get(r["book_id"], 1.0), int(r["revealed_ordinal"])) if r["revealed_ordinal"] is not None
             else None})
    return out


def refresh_entity(conn: sqlite3.Connection, book_id: str, eid: str) -> None:
    """Label an entity by its official form (else its earliest form) and make it a character entity when any
    form is a character name, so the first form that happened to be linked does not name it forever."""
    forms = conn.execute(f"SELECT source_term, category, form_type FROM glossary WHERE {SERIES_SCOPE} AND entity_id=? "
                         "ORDER BY form_type!='official', category!='character', id", (book_id, eid)).fetchall()
    if not forms:
        return
    kind = "character" if any(f[1] == "character" for f in forms) else forms[0][1]
    conn.execute("UPDATE entities SET label_ja=?, kind=?, updated_at=? WHERE id=?", (forms[0][0], kind, utcnow(), eid))


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
        conn.execute(f"UPDATE OR IGNORE form_senses SET entity_id=? WHERE {SERIES_SCOPE} AND entity_id=?",
                     (t_eid, book_id, s_eid))
        conn.execute("DELETE FROM entities WHERE id=?", (s_eid,))
    # a sense pointing at the entity the form now belongs to is no longer "another" referent
    conn.execute(f"DELETE FROM form_senses WHERE {SERIES_SCOPE} AND (entity_id=? AND term IN (SELECT source_term FROM "
                 f"glossary WHERE entity_id=?) OR entity_id=?)", (book_id, t_eid, t_eid, s_eid))
    conn.execute(f"UPDATE glossary SET form_type=?, form_note=COALESCE(?, form_note) WHERE {SERIES_SCOPE} AND source_term=?",
                 (form_type, note, book_id, term))
    refresh_entity(conn, book_id, t_eid)
    return 1


def unlink(conn: sqlite3.Connection, book_id: str, term: str) -> None:
    row = conn.execute(f"SELECT * FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, term)).fetchone()
    if row is None:
        raise KeyError(term)
    old_eid = row["entity_id"]
    old_forms = [r[0] for r in conn.execute(f"SELECT source_term FROM glossary WHERE {SERIES_SCOPE} AND entity_id=? "
                                            "AND source_term!=?", (book_id, old_eid, term))] if old_eid else []
    label = term
    n = 1
    while entity_id_for(book_id, label) == old_eid:  # the entity may have been named after this very form
        n += 1
        label = f"{term}#{n}"
    eid = _new_entity(conn, book_id, label, row["category"], None)
    if label != term:
        conn.execute("UPDATE entities SET label_ja=? WHERE id=?", (term, eid))
    conn.execute("UPDATE glossary SET entity_id=?, form_type='official' WHERE id=?", (eid, row["id"]))
    conn.execute(f"UPDATE entity_links SET status='rejected' WHERE {SERIES_SCOPE} AND term=? AND status='accepted'",
                 (book_id, term))
    if old_eid and old_eid != eid:
        refresh_entity(conn, book_id, old_eid)
        _drop_alias(conn, book_id, term, old_forms, old_eid)


def _drop_alias(conn: sqlite3.Connection, book_id: str, term: str, entity_forms: list[str], eid: str) -> None:
    """A detached form is no longer an alias of the character of the entity it was merged into: the card the entity
    is bound to, or else the card its forms name (a card a name finds, ADR-103). Other characters that list it — a
    card of another time or one known only by other cards' words with the same name — keep it."""
    import json

    if not entity_forms:
        return
    bound = conn.execute("SELECT character_id FROM entities WHERE id=?", (eid,)).fetchone()
    rows = conn.execute("SELECT id, aliases_json FROM characters WHERE id=?", (bound[0],)).fetchall() \
        if bound is not None and bound[0] else conn.execute(
            f"SELECT id, aliases_json FROM characters WHERE {SERIES_SCOPE} AND {FINDABLE} AND canonical_name IN "
            f"({','.join('?' * len(entity_forms))})", (book_id, *entity_forms)).fetchall()
    for cid, aliases_json in rows:
        aliases = json.loads(aliases_json)
        if term in aliases:
            aliases.remove(term)
            conn.execute("UPDATE characters SET aliases_json=?, updated_at=? WHERE id=?",
                         (json.dumps(aliases, ensure_ascii=False), utcnow(), cid))
            # nor a word the card is shown by (``character_forms``)
            conn.execute("DELETE FROM character_forms WHERE character_id=? AND form=?", (cid, term))


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
    """Link two forms. Returns 'accepted', 'shared' (an automatic link that would fuse two established entities:
    the form is recorded as possibly referring to either) or 'candidate' (one side has no glossary entry yet)."""
    known = {r[0] for r in conn.execute(f"SELECT source_term FROM glossary WHERE {SERIES_SCOPE} AND source_term IN (?,?)",
                                        (book_id, term, target))}
    if term in known and target in known:
        if origin != "human" and (word := shared_word(conn, book_id, term, target)) is not None:
            # An ambiguous form (「しまちゃん」 was used for both sisters, 「母上」 by two children for two mothers)
            # must not fuse two established entities or two cards – merge_into moves every form of the term's entity.
            # The form is recorded as *shared* (it may refer to either); merging stays a decision of the model's
            # second look or a human (ncte names link)
            add_sense(conn, book_id, word, target if word == term else term, origin=origin, note=note,
                      unit_id=unit_id, quote=quote, ordinal=ordinal)
            record_link(conn, book_id, term, target, form_type, origin=origin, status="candidate", unit_id=unit_id,
                        quote=quote, ordinal=ordinal)
            return "shared"
        if origin == "human":  # a person deciding what the word names settles it: no longer shared
            conn.execute(f"DELETE FROM form_senses WHERE {SERIES_SCOPE} AND term=?", (book_id, term))
        merge_into(conn, book_id, term, target, form_type, note)
        record_link(conn, book_id, term, target, form_type, origin=origin, status="accepted", unit_id=unit_id,
                    quote=quote, ordinal=ordinal)
        reconcile_characters(conn, book_id)
        if origin == "human":
            # the two cards the person named are one, even when a word of theirs still names someone else too
            # (reconcile_characters skips a card named by a shared word)
            _merge_named_cards(conn, book_id, target, term)
        return "accepted"
    record_link(conn, book_id, term, target, form_type, origin=origin, status="candidate", unit_id=unit_id,
                quote=quote, ordinal=ordinal)
    return "candidate"


def _merge_named_cards(conn: sqlite3.Connection, book_id: str, keep: str, drop: str) -> None:
    from .repository import merge_characters

    ids = [conn.execute(f"SELECT id FROM characters WHERE {SERIES_SCOPE} AND {FINDABLE} AND canonical_name=?",
                        (book_id, name)).fetchone() for name in (keep, drop)]
    if ids[0] is not None and ids[1] is not None:
        merge_characters(conn, ids[0][0], ids[1][0])


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

    if is_shared(conn, book_id, name):  # a word that may name more than one person finds none (ADR-108)
        return None
    row = conn.execute(f"SELECT entity_id FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, name)).fetchone()
    forms = [name]
    if row and row[0]:
        forms = [r[0] for r in conn.execute(f"SELECT source_term FROM glossary WHERE {SERIES_SCOPE} AND entity_id=? "
                                            "ORDER BY form_type!='official'", (book_id, row[0]))]
    # a card known only by other cards' words (ADR-103) is never the one a form finds
    chars = conn.execute(f"SELECT id, canonical_name, aliases_json FROM characters WHERE {SERIES_SCOPE} AND {FINDABLE}",
                         (book_id,)).fetchall()
    # a form several cards have (「母上」 of two mothers, each from her own place in the story): with no position to
    # tell which, none is picked (ADR-103)
    for f in forms:
        if len(hits := {str(c[0]) for c in chars if f == c[1] or f in json.loads(c[2])}) == 1:
            return next(iter(hits))
        if hits:
            return None
    return None


def reconcile_characters(conn: sqlite3.Connection, book_id: str) -> int:
    """Single source of truth: characters whose names are forms of the same entity are merged into the
    character of the entity's official form (ADR-021)."""
    from .repository import merge_characters

    # a card known only by other cards' words (ADR-103) is merged by the second look's answer, never by its words
    chars = {r[0]: r[1] for r in conn.execute(f"SELECT canonical_name, id FROM characters WHERE {SERIES_SCOPE} AND "
                                              f"{FINDABLE}", (book_id,))}
    groups: dict[str, list[tuple[str, str]]] = {}
    for term, eid, form in conn.execute(f"SELECT source_term, entity_id, form_type FROM glossary WHERE {SERIES_SCOPE} "
                                        "AND entity_id IS NOT NULL", (book_id,)):
        # a card named by a word that may name more than one person is never someone by that word (ADR-108)
        if term in chars and not is_shared(conn, book_id, term):
            groups.setdefault(eid, []).append((form, chars[term]))
    n = 0
    for members in groups.values():
        ids = list(dict.fromkeys(cid for _, cid in sorted(members, key=lambda m: m[0] != "official")))
        for drop in ids[1:]:
            merge_characters(conn, ids[0], drop)
            n += 1
    return n
