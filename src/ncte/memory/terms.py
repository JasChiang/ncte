"""What the book's recurring terms mean (ADR-069): the definitions kept, and the ones a request is given.

A definition is the model's (from the terms step, citing example sentences and facts) or a person's; a person's is
never replaced by the model's. It is shown from the latest position of the evidence it cites (``revealed_ordinal``):
before that, a request where the term occurs does not get it. A term whose meaning the book reveals bit by bit has
several of the model's definitions (stages), each shown from its own position; a request gets the latest one shown by
then. A person's is one definition, shown from the start. Which definitions a request gets is a string match of
the term in the request's source text, as the glossary is chosen; nothing here decides what a passage means.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..series import SERIES_SCOPE, pos, scope_clause, volumes
from .db import utcnow

DEFINITION_CHARS = 300  # a definition is one or two sentences


def _insert(conn: sqlite3.Connection, book_id: str, term: str, definition: str, contrast_with: str, origin: str,
            evidence: list[dict[str, Any]], since: int, revealed: int, run_id: str | None) -> None:
    conn.execute(
        "INSERT INTO term_definitions(book_id, source_term, definition, contrast_with, evidence_json, since_ordinal, "
        "revealed_ordinal, origin, run_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (book_id, term, definition.strip()[:DEFINITION_CHARS], contrast_with.strip() or None,
         json.dumps(evidence, ensure_ascii=False), since, revealed, origin, run_id, utcnow()))


def set_definition(conn: sqlite3.Connection, book_id: str, term: str, definition: str, *, contrast_with: str = "",
                   origin: str = "human", evidence: list[dict[str, Any]] | None = None, since: int = 0,
                   revealed: int = 0, run_id: str | None = None) -> None:
    """Keep one definition, replacing all of this book's ones for the term (the model's stages too). A person's has no
    evidence and is shown from the start of the book."""
    conn.execute("DELETE FROM term_definitions WHERE book_id=? AND source_term=?", (book_id, term))
    _insert(conn, book_id, term, definition, contrast_with, origin, evidence or [], since, revealed, run_id)


def set_model_stages(conn: sqlite3.Connection, book_id: str, term: str, stages: list[dict[str, Any]],
                     run_id: str | None = None) -> None:
    """Replace the model's definitions of the term in this book with ``stages`` (each: definition, contrast_with,
    evidence, since, revealed; at distinct ``revealed``). None: the term is left without the model's. A person's
    definition is not touched (the model is not asked about such a term)."""
    conn.execute("DELETE FROM term_definitions WHERE book_id=? AND source_term=? AND origin='model'", (book_id, term))
    for st in stages:
        _insert(conn, book_id, term, st["definition"], st.get("contrast_with", ""), "model", st["evidence"],
                st["since"], st["revealed"], run_id)


def human_terms(conn: sqlite3.Connection, book_id: str) -> set[str]:
    """Terms of this series a person has defined: the model is not asked about them again."""
    return {r[0] for r in conn.execute(f"SELECT source_term FROM term_definitions WHERE {SERIES_SCOPE} AND "
                                       "origin='human'", (book_id,))}


def all_definitions(conn: sqlite3.Connection, book_id: str) -> list[sqlite3.Row]:
    """Every definition of the series, in story order of where it is shown from."""
    vols = volumes(conn)
    where, ids = scope_clause(conn, book_id)
    rows = conn.execute(f"SELECT * FROM term_definitions WHERE {where}", ids).fetchall()
    return sorted(rows, key=lambda r: (vols.get(r["book_id"], 1.0), int(r["revealed_ordinal"]), r["source_term"]))


def definitions_for(conn: sqlite3.Connection, book_id: str, text: str, upto: int) -> list[dict[str, str]]:
    """``term_definitions`` for a request: each defined term that occurs in ``text`` (as written, ``match_text``) and
    whose definition is shown by story position ``upto`` of this book, with its current rendering; in the order the
    terms first occur in the text. A term defined in stages, or in several volumes of a series: the latest
    one shown by then."""
    t = match_text(text)
    hz = pos(conn, book_id, upto)
    vols = volumes(conn)
    zh = {r[0]: r[1] for r in conn.execute(f"SELECT source_term, target_term FROM glossary WHERE {SERIES_SCOPE}",
                                           (book_id,))}
    best: dict[str, sqlite3.Row] = {}
    for r in all_definitions(conn, book_id):
        if r["source_term"] and r["source_term"] in t and (vols.get(r["book_id"], 1.0), int(r["revealed_ordinal"])) <= hz:
            best[r["source_term"]] = r  # later in story order wins
    return [{"ja": term, "zh": zh.get(term, ""), "definition": r["definition"]}
            for term, r in sorted(best.items(), key=lambda kv: t.find(kv[0]))]


def definitions_shown(conn: sqlite3.Connection, book_id: str, upto: int) -> dict[str, str]:
    """term → its definition shown by story position ``upto`` of this book (the latest one in story order), only the
    ones the model or a person wrote: for a step that is given a list of terms rather than a text (naming)."""
    hz = pos(conn, book_id, upto)
    vols = volumes(conn)
    out: dict[str, str] = {}
    for r in all_definitions(conn, book_id):
        if r["origin"] in ("model", "human") and (vols.get(r["book_id"], 1.0), int(r["revealed_ordinal"])) <= hz:
            out[r["source_term"]] = r["definition"]
    return out
