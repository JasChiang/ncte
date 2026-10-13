"""Series scope and story positions (ADR-020).

All knowledge (characters, glossary / name forms, entity links, facts, summaries, props, speech examples) is
shared by every volume of a series. A *story position* is ``(volume_index, ordinal)``: everything in volume 1
comes before volume 2, whatever order the user translates the volumes in. Spoiler filters compare positions,
so translating volume 1 after volume 3 never sees knowledge first revealed in volumes 2–3.
"""

from __future__ import annotations

import sqlite3

Pos = tuple[float, int]
INF_POS: Pos = (float("inf"), 1 << 62)


# SQL fragment selecting every book of the series of the book given as the single parameter
SERIES_SCOPE = ("book_id IN (SELECT b2.id FROM books b1 JOIN books b2 ON b2.id=b1.id OR "
                "(b1.series_id IS NOT NULL AND b2.series_id=b1.series_id) WHERE b1.id=?)")


def series_id_for(book_id: str) -> str:
    import hashlib

    return "sr_" + hashlib.sha256(book_id.encode()).hexdigest()[:12]


def ensure_series(conn: sqlite3.Connection, title: str, *, series_id: str | None = None,
                  slug: str | None = None) -> str:
    from .memory.db import utcnow

    sid = series_id or "sr_" + __import__("hashlib").sha256(title.encode()).hexdigest()[:12]
    conn.execute("INSERT OR IGNORE INTO series(id, title, slug, created_at) VALUES (?,?,?,?)",
                 (sid, title, slug or sid, utcnow()))
    return sid


def scope_ids(conn: sqlite3.Connection, book_id: str) -> list[str]:
    """Book ids of the series that ``book_id`` belongs to (volume order); a lone book is its own series."""
    row = conn.execute("SELECT series_id FROM books WHERE id=?", (book_id,)).fetchone()
    if row is None or not row[0]:
        return [book_id]
    return [r[0] for r in conn.execute("SELECT id FROM books WHERE series_id=? ORDER BY volume_index, id", (row[0],))]


def scope_clause(conn: sqlite3.Connection, book_id: str, column: str = "book_id") -> tuple[str, list[str]]:
    ids = scope_ids(conn, book_id)
    return f"{column} IN ({','.join('?' * len(ids))})", ids


def volumes(conn: sqlite3.Connection) -> dict[str, float]:
    return {r[0]: float(r[1]) for r in conn.execute("SELECT id, volume_index FROM books")}


def volume_of(conn: sqlite3.Connection, book_id: str) -> float:
    row = conn.execute("SELECT volume_index FROM books WHERE id=?", (book_id,)).fetchone()
    return float(row[0]) if row else 1.0


def pos(conn: sqlite3.Connection, book_id: str, ordinal: int) -> Pos:
    return (volume_of(conn, book_id), int(ordinal))


def unit_pos(conn: sqlite3.Connection, unit_id: str | None) -> Pos | None:
    if not unit_id:
        return None
    row = conn.execute("SELECT b.volume_index, u.ordinal FROM units u JOIN books b ON b.id=u.book_id WHERE u.id=?",
                       (unit_id,)).fetchone()
    return (float(row[0]), int(row[1])) if row else None
