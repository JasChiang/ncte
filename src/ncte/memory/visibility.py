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
  was read before translating (ADR-023): 「謎の少女」 must not be shown as 「かぐや」 before the reveal
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
    latest: dict[str, sqlite3.Row] = {}
    for r in sorted(rows, key=lambda r: _p(r, vols, "book_id", "horizon_ordinal")):
        latest[r["name_ja"]] = r
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


FormPositions = tuple[dict[str, Pos], dict[str, Pos]]


def form_positions(conn: sqlite3.Connection, book_id: str) -> FormPositions:
    """(first appearance of each name form, reveal of the earliest accepted link touching the form)."""
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
    return first, linked


def character_view(conn: sqlite3.Connection, book_id: str, c: Any, upto: int,
                   forms_at: FormPositions | None = None) -> dict[str, Any] | None:
    """The names of character row ``c`` as known at ``upto``: ``name_ja``/``name_zh``/``aliases_ja``.

    The form that appeared first is the anchor; any other form is shown once it has appeared *and* its link to
    the character was revealed (forms without a recorded link count as known with the character). If the
    canonical name is not shown yet, the anchor stands in for it. None before any form has appeared."""
    import json

    hz = pos(conn, book_id, upto)
    first, linked = form_positions(conn, book_id) if forms_at is None else forms_at
    known_from = pos(conn, c["book_id"], c["known_from_ordinal"])
    names = list(dict.fromkeys([c["canonical_name"], *json.loads(c["aliases_json"] or "[]")]))
    at = {n: first.get(n, known_from) for n in names}
    anchor = min(names, key=lambda n: at[n])
    shown = [n for n in names if at[n] <= hz and (n == anchor or linked.get(n, (0.0, 0)) <= hz)]
    if not shown:
        return None
    head = c["canonical_name"] if c["canonical_name"] in shown else anchor
    if head == c["canonical_name"]:
        zh = c["display_name_zh"]
    else:
        g = conn.execute("SELECT target_term FROM glossary WHERE source_term=? LIMIT 1", (head,)).fetchone()
        zh = g[0] if g else None
    from .repository import gender_at

    out: dict[str, Any] = {"name_ja": head, "name_zh": zh, "aliases_ja": [n for n in shown if n != head]}
    if g := gender_at(conn, c["id"], hz):
        out["gender"] = g
    return out
