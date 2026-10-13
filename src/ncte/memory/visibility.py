"""Spoiler control (spec §5.5, ADR-020): memory is only visible up to the current story position.

Callers pass the book being translated and an ordinal inside it; the horizon is the story position
``(volume_index, ordinal)``. Knowledge of the whole series is considered, so volume 2 sees volume 1, while
volume 1 (even when translated later) never sees what volumes 2+ revealed.

* facts / summaries / props / speech examples: visible if revealed strictly before the horizon
  (``horizon`` = first unit of the scene being processed)
* characters: visible once their first mention is ≤ the given position (current scene may be analysed)
* a retraction is honoured only if the retracting fact itself was revealed before the horizon
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
