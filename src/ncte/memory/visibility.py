"""Spoiler control (spec §5.5, ADR-020): memory is only visible up to the current story position.

Callers pass the book being translated and an ordinal inside it; the horizon is the story position
``(volume_index, ordinal)``. Knowledge of the whole series is considered, so volume 2 sees volume 1, while
volume 1 (even when translated later) never sees what volumes 2+ revealed.

* facts / summaries / props / speech examples: visible if revealed strictly before the horizon
  (``horizon`` = first unit of the scene being processed)
* characters: visible once their first mention is ≤ the given position (current scene may be analysed)
* a retraction is honoured only if the retracting fact itself was revealed before the horizon
* a character's names (canonical name, aliases): the form that appeared first, plus every other form once it has
  appeared and its link to the character (an identity reveal) was revealed — matters most when the whole volume
  was read before translating (ADR-023): 「謎の少女」 must not be shown as 「サクヤ」 before the reveal
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..series import Pos, pos, scope_clause, volumes


def _p(row: Any, vols: dict[str, float], book_col: str, ord_col: str) -> Pos:
    return (vols.get(row[book_col], 1.0), int(row[ord_col]))


def visible_facts(conn: sqlite3.Connection, book_id: str, horizon: int) -> list[sqlite3.Row]:
    hz = pos(conn, book_id, horizon)
    vols = volumes(conn)
    where, ids = scope_clause(conn, book_id)
    rows = conn.execute(f"SELECT * FROM facts WHERE {where}", ids).fetchall()
    by_id = {r["id"]: r for r in rows}
    out = []
    for r in rows:
        if _p(r, vols, "book_id", "first_revealed_ordinal") >= hz:
            continue
        retractor = by_id.get(r["retracted_by"]) if r["retracted_by"] else None
        if retractor is not None and _p(retractor, vols, "book_id", "first_revealed_ordinal") < hz:
            continue
        out.append(r)
    return sorted(out, key=lambda r: _p(r, vols, "book_id", "first_revealed_ordinal"))


def visible_summaries(conn: sqlite3.Connection, book_id: str, horizon: int) -> list[sqlite3.Row]:
    hz = pos(conn, book_id, horizon)
    vols = volumes(conn)
    where, ids = scope_clause(conn, book_id)
    rows = [r for r in conn.execute(f"SELECT * FROM chapter_summaries WHERE {where}", ids)
            if _p(r, vols, "book_id", "horizon_ordinal") < hz]
    return sorted(rows, key=lambda r: _p(r, vols, "book_id", "horizon_ordinal"))


def visible_characters(conn: sqlite3.Connection, book_id: str, upto: int) -> list[sqlite3.Row]:
    hz = pos(conn, book_id, upto)
    vols = volumes(conn)
    where, ids = scope_clause(conn, book_id)
    rows = [r for r in conn.execute(f"SELECT * FROM characters WHERE {where}", ids)
            if _p(r, vols, "book_id", "known_from_ordinal") <= hz]
    return sorted(rows, key=lambda r: _p(r, vols, "book_id", "known_from_ordinal"))


def visible_props(conn: sqlite3.Connection, book_id: str, horizon: int) -> list[sqlite3.Row]:
    hz = pos(conn, book_id, horizon)
    vols = volumes(conn)
    where, ids = scope_clause(conn, book_id)
    rows = [r for r in conn.execute(f"SELECT * FROM prop_states WHERE {where}", ids)
            if _p(r, vols, "book_id", "horizon_ordinal") < hz]
    # the latest state for each time: a flashback (who held it three years before) does not replace who holds it now
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for r in sorted(rows, key=lambda r: _p(r, vols, "book_id", "horizon_ordinal")):
        latest[(r["name_ja"], (r["period"] if "period" in r.keys() else None) or "")] = r
    return list(latest.values())


def visible_examples(conn: sqlite3.Connection, character_id: str, horizon: int, limit: int = 3, *,
                     book_id: str | None = None) -> list[sqlite3.Row]:
    vols = volumes(conn)
    rows = conn.execute(
        "SELECT e.*, u.ordinal, u.book_id FROM speech_examples e JOIN units u ON u.id=e.unit_id WHERE e.character_id=? "
        "AND e.verification_status IN ('explicit','human_verified')", (character_id,)).fetchall()
    hz = pos(conn, book_id, horizon) if book_id else (float("inf"), horizon)
    if book_id is None:  # legacy callers: same-book ordinal comparison
        rows = [r for r in rows if r["ordinal"] < horizon]
    else:
        rows = [r for r in rows if _p(r, vols, "book_id", "ordinal") < hz]
    rows.sort(key=lambda r: (r["verification_status"] != "human_verified", tuple(-x for x in _p(r, vols, "book_id",
                                                                                                 "ordinal"))))
    return rows[:limit]


FormPositions = tuple[dict[str, Pos], dict[str, Pos], dict[str, dict[str, Pos]]]


def form_positions(conn: sqlite3.Connection, book_id: str) -> FormPositions:
    """(first appearance of each name form, reveal of the earliest accepted link touching the form, where each form
    was first seen with each card: ``character_forms``, ADR-103)."""
    where, ids = scope_clause(conn, book_id, "g.book_id")
    first: dict[str, Pos] = {}
    for r in conn.execute("SELECT g.source_term, b.volume_index, u.ordinal FROM glossary g JOIN units u ON "
                          f"u.id=g.first_unit_id JOIN books b ON b.id=u.book_id WHERE {where}", ids):
        first[r[0]] = (float(r[1]), int(r[2]))
    vols = volumes(conn)
    where, ids = scope_clause(conn, book_id)
    linked: dict[str, Pos] = {}
    for r in conn.execute(f"SELECT term, target_term, book_id, revealed_ordinal FROM entity_links WHERE {where} "
                          "AND status='accepted' AND revealed_ordinal IS NOT NULL", ids):
        p = (vols.get(r[2], 1.0), int(r[3]))
        for t in (r[0], r[1]):
            if t not in linked or p < linked[t]:
                linked[t] = p
    where, ids = scope_clause(conn, book_id, "f.book_id")
    held: dict[str, dict[str, Pos]] = {}  # card → form → where first seen with it
    for r in conn.execute("SELECT f.character_id, f.form, b.volume_index, f.ordinal FROM character_forms f JOIN books b "
                          f"ON b.id=f.book_id WHERE {where}", ids):
        held.setdefault(r[0], {})[r[1]] = (float(r[2]), int(r[3]))
    return first, linked, held


def character_view(conn: sqlite3.Connection, book_id: str, c: Any, upto: int,
                   forms_at: FormPositions | None = None, *, lookup: bool = False) -> dict[str, Any] | None:
    """The names of character row ``c`` as known at ``upto``: ``name_ja``/``name_zh``/``aliases_ja``.

    The form that appeared first is the anchor; any other form is shown once it has appeared *and* its link to
    the character was revealed (forms without a recorded link count as known with the character). If the
    canonical name is not shown yet, the anchor stands in for it. None before any form has appeared."""
    import json

    hz = pos(conn, book_id, upto)
    first, linked, held = form_positions(conn, book_id) if forms_at is None else forms_at
    known_from = pos(conn, c["book_id"], c["known_from_ordinal"])
    mine = held.get(c["id"], {})
    # the card's names, and the words the book called it by where it was first given a line before its name came
    # (recorded with the card only, ADR-103 實測三: shown, never a name a lookup finds it by)
    # ``lookup``: for finding who words name (the reading's owners of a word, a form of address) — only the card's
    # names; the words it is only shown by never name it (ADR-103)
    names = list(dict.fromkeys([c["canonical_name"], *json.loads(c["aliases_json"] or "[]"),
                                *([] if lookup else mine)]))
    # where each form is first seen with this card: a recorded position (a card moved earlier on a re-read does not
    # take its later forms with it, ADR-103), never before the form first appears in the book; else with the card
    def seen_at(n: str) -> Pos:
        h = mine.get(n)
        if h is None:
            return first.get(n, known_from)
        return max(h, first[n]) if n in first else h

    seen = {n: seen_at(n) for n in names}
    # a link revealed later only keeps that name back: the card itself is there from its first appearance, under the
    # form seen first (the unnamed narrator's 「（敘述者）」 before 汐乃 is revealed, ADR-103). Before, a link's reveal
    # also delayed the form it was linked to, and the card vanished until then
    from .repository import placeholder_name

    # (seen at the same place: the card's own name, which a reveal always records at the reveal, not before)
    anchor = min(names, key=lambda n: (not placeholder_name(n), seen[n], n != c["canonical_name"]))
    shown = [n for n in names if seen[n] <= hz and (n == anchor or linked.get(n, (0.0, 0)) <= hz)]
    if not shown:
        return None
    head = c["canonical_name"] if c["canonical_name"] in shown else anchor
    if head == c["canonical_name"]:
        zh = c["display_name_zh"]
    else:
        g = conn.execute("SELECT target_term FROM glossary WHERE source_term=? LIMIT 1", (head,)).fetchone()
        zh = g[0] if g else None
    from .repository import gender_basis, gender_view

    out: dict[str, Any] = {"name_ja": head, "name_zh": zh, "aliases_ja": [n for n in shown if n != head]}
    if (when := c["period"] if "period" in c.keys() else None) is not None:
        # someone of another time (a past life's 「母親」): the model's reading of who this is, not a name form
        out["other_person"] = True
        if when:
            out["period"] = when
    if c["is_narrator"] or head.startswith("（敘述者"):  # the unnamed first-person narrator (ADR-045): 「我」 in the text
        out["narrator"] = True
    if g := gender_view(conn, c["id"], hz):
        out["gender"] = g[0] if g[1] == "explicit" else f"{g[0]} (inferred)"
        if basis := gender_basis(conn, c["id"]):  # shown by the narration or someone's belief: a reveal may overturn it
            out["gender_basis"] = basis
    return out


def visible_names(conn: sqlite3.Connection, book_id: str, upto: int,
                  forms_at: FormPositions | None = None) -> dict[str, str]:
    """character id → the name a task may show at ``upto`` (Chinese if there is one): every place that names a character
    to the model (speakers, who a line is said to, forms of address, whose eyes the scene is told through) uses this,
    so a name revealed later in the book (「神祕少女」 → 「蘇菲」) never comes back through a side door."""
    forms_at = form_positions(conn, book_id) if forms_at is None else forms_at
    out = {}
    for c in visible_characters(conn, book_id, upto):
        if (v := character_view(conn, book_id, c, upto, forms_at)) is not None:
            name = v["name_zh"] or v["name_ja"]
            # someone of another time: the model's reading of the time goes with the name, as a note (「母親〔前世〕」)
            out[c["id"]] = f"{name}〔{v.get('period') or '另一人'}〕" if v.get("other_person") else name
    return out


FIRST_SEEN_CHARS = 60


def first_seen(conn: sqlite3.Connection, c: Any) -> str:
    """The opening of the paragraph a card was first seen in: what tells two cards the book calls by the same words
    apart (two unnamed mothers, each 「母親」) where a model must pick one — the book's own text, never a label or a
    name the program made up (ADR-103). Never after the card's first appearance, so it reveals nothing."""
    from ..epub.units import match_text

    row = conn.execute("SELECT plain_text FROM units WHERE id=?", (c["known_from_unit_id"],)).fetchone()
    return match_text(row[0]).strip(" 　")[:FIRST_SEEN_CHARS] if row is not None else ""


def shared_names(names: dict[str, str]) -> set[str]:
    """The ids whose name another id in ``names`` has too (cards told apart by ``first_seen``, ADR-103)."""
    seen: dict[str, int] = {}
    for n in names.values():
        seen[n] = seen.get(n, 0) + 1
    return {cid for cid, n in names.items() if seen[n] > 1}


TELL_APART_NOTES = 3
TELL_APART_RELATIONS = 4


def tell_apart(conn: sqlite3.Connection, book_id: str, cards: list[Any], upto: int) -> dict[str, dict[str, Any]]:
    """What tells cards with the same name apart (two unnamed mothers, each 「母親」) in one request, all as known at
    ``upto`` — nothing written for a later position, no name revealed later (ADR-103):

    * ``first_seen``: the opening of the paragraph each was first seen in (the book's text);
    * ``earlier_model_reading``: what the model itself wrote of the person earlier — its description of someone the
      book does not name (``character_notes``) and the role in its impression of them — an earlier reading by the
      model, not the book's words;
    * ``relations_so_far``: relations read by then (「卡伊的母親」), the others named as known at ``upto``.

    Callers give it only for cards whose name another card in the request has, so other requests stay as they were."""
    from .repository import impression_at

    hz = pos(conn, book_id, upto)
    names = visible_names(conn, book_id, upto)
    out: dict[str, dict[str, Any]] = {}
    for c in cards:
        hint: dict[str, Any] = {"first_seen": first_seen(conn, c)}
        notes = [r["text"] for r in conn.execute("SELECT * FROM character_notes WHERE character_id=? ORDER BY ordinal",
                                                 (c["id"],)) if pos(conn, r["book_id"], r["ordinal"]) <= hz]
        if (imp := impression_at(conn, c["id"], upto)) and imp.get("role"):
            notes.append(imp["role"])
        if notes:
            hint["earlier_model_reading"] = list(dict.fromkeys(notes))[:TELL_APART_NOTES]
        rel: list[str] = []
        for r in conn.execute(
                "SELECT a_id, b_id, a_is, b_is, relation_zh, book_id, first_ordinal, NULL FROM relation_notes WHERE "
                "a_id=? OR b_id=? UNION ALL SELECT a_id, b_id, a_is, b_is, relation_zh, book_id, since_ordinal, "
                "until_ordinal FROM relations WHERE a_id=? OR b_id=?", (c["id"],) * 4):
            if pos(conn, r[5], r[6]) > hz or (r[7] is not None and pos(conn, r[5], r[7]) <= hz):
                continue  # not read yet, or replaced by then
            other, role = (r[1], r[2]) if r[0] == c["id"] else (r[0], r[3])
            if other in names and other != c["id"]:
                rel.append(f"{names[other]}的{role or r[4]}")
        if rel:
            hint["relations_so_far"] = list(dict.fromkeys(rel))[:TELL_APART_RELATIONS]
        out[c["id"]] = hint
    return out
