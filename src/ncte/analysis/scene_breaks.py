"""Where a long stretch without the author's scene markers breaks into scenes (ADR-036).

The author's own markers (a new file, a separator, a heading, blank lines) stay the program's to apply: they are not
guesses. Inside a stretch the size limit cut blindly, sometimes in the middle of a conversation. Here the model reads
the stretch (each paragraph's opening) and says where a new scene begins; the program checks the answer — a break must
be a paragraph of that stretch, not the continuation of a quote spanning paragraphs, and every scene keeps a sensible
size — and otherwise keeps its own cuts for that stretch.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..memory.db import Transaction, utcnow
from .dialogue import UnitText, clean, find_quotes

OPENING_CHARS = 50
MIN_SCENE = 12  # paragraphs: shorter scenes lose the context the analysis needs


def stretches(conn: sqlite3.Connection, book_id: str) -> list[list[sqlite3.Row]]:
    """Runs of scenes joined only by size cuts: one stretch of text the author did not divide."""
    scenes = conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book_id,)).fetchall()
    groups: list[list[sqlite3.Row]] = []
    for s in scenes:
        if s["break_reason"] == "max_size" and groups:
            groups[-1].append(s)
        else:
            groups.append([s])
    out = []
    for g in groups:
        if len(g) < 2:
            continue
        out.append(conn.execute("SELECT * FROM units WHERE book_id=? AND kind IN ('text','heading') AND spine_index IS "
                                "NOT NULL AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
                                (book_id, g[0]["start_ordinal"], g[-1]["end_ordinal"])).fetchall())
    return out


def payload(rows: list[sqlite3.Row], max_units: int) -> dict[str, Any]:
    paras = []
    for r in rows:
        t = clean(r["plain_text"]).strip(" 　")
        paras.append({"unit_id": r["id"], "text": t[:OPENING_CHARS], "line": t.startswith(("「", "『"))})
    return {"task": "scenes", "paragraphs": paras, "scene_size": [MIN_SCENE * 3, max_units], "max_size": max_units * 2}


def check(rows: list[sqlite3.Row], proposed: list[str], max_units: int) -> list[str] | None:
    """The breaks the program accepts, in order; None when the stretch keeps the program's own cuts."""
    pos = {r["id"]: i for i, r in enumerate(rows)}
    quotes = find_quotes([UnitText(r["id"], clean(r["plain_text"]), r["ordinal"]) for r in rows])
    inside = {q.unit_id for q in quotes if q.continuation_of is not None}  # a quote begun in the paragraph before
    ok = sorted({pos[u] for u in proposed if u in pos and pos[u] > 0 and u not in inside})
    kept: list[int] = []
    for i in ok:  # drop breaks that would leave a too-short scene
        if i - (kept[-1] if kept else 0) >= MIN_SCENE and len(rows) - i >= MIN_SCENE:
            kept.append(i)
    edges = [0, *kept, len(rows)]
    if any(b - a > max_units * 2 for a, b in zip(edges, edges[1:], strict=False)):
        return None
    return [rows[i]["id"] for i in kept]


def save(conn: sqlite3.Connection, book_id: str, rows: list[sqlite3.Row], breaks: list[str], reasons: dict[str, str],
         ) -> None:
    """The stretch's first paragraph marks it as decided (no size cuts inside it); each break starts a scene."""
    with Transaction(conn):
        conn.executemany("DELETE FROM scene_breaks WHERE book_id=? AND unit_id=?", [(book_id, r["id"]) for r in rows])
        conn.execute("INSERT INTO scene_breaks(book_id, unit_id, reason, origin, created_at) VALUES (?,?,?,?,?)",
                     (book_id, rows[0]["id"], "stretch decided by the model", "stretch", utcnow()))
        conn.executemany("INSERT INTO scene_breaks(book_id, unit_id, reason, origin, created_at) VALUES (?,?,?,?,?)",
                         [(book_id, u, reasons.get(u, "")[:200], "llm", utcnow()) for u in breaks])
