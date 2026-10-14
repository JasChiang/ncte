"""Writes from the web UI, each in one transaction, reusing the same functions as the CLI."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import NCTEError
from ..library import Book
from ..memory.db import Transaction
from ..translation.cache import latest_texts


def edit_unit(conn: sqlite3.Connection, book: Book, unit_id: str, text: str, *, pre: str = "", post: str = "",
              reason: str = "") -> dict[str, Any]:
    """Save a human translation (the paragraph wrapper markers the editor hid are put back)."""
    from ..translation.edits import apply_human_edit

    text = text.strip()
    if not text:
        raise NCTEError("empty translation")
    findings = apply_human_edit(conn, book.id, unit_id, f"{pre}{text}{post}", reason=reason or "gui")  # own transaction
    return {"ok": True, "warnings": [{"type": f.type, "severity": f.severity, "evidence": f.evidence} for f in findings]}


def revert_unit(conn: sqlite3.Connection, book: Book, unit_id: str) -> dict[str, Any]:
    """Drop the human translation of a unit: the latest machine translation is used again."""
    with Transaction(conn):
        n = conn.execute("DELETE FROM translations WHERE unit_id=? AND status='human'", (unit_id,)).rowcount
    if not n:
        raise NCTEError(f"{unit_id} has no human translation")
    return {"ok": True}


def drop_note(conn: sqlite3.Connection, book: Book, unit_id: str) -> dict[str, Any]:
    tr = latest_texts(conn, book.id).get(unit_id)
    if tr is None or not tr["translator_note"]:
        raise NCTEError(f"{unit_id} has no note")
    with Transaction(conn):
        conn.execute("UPDATE translations SET translator_note=NULL WHERE id=?", (tr["id"],))
    return {"ok": True}


def decide(conn: sqlite3.Connection, book: Book, pid: int, zh: str) -> dict[str, Any]:
    from ..analysis.naming import decide as _decide

    with Transaction(conn):
        return {"ok": True, **_decide(conn, book.id, pid, zh)}


def apply_rename(conn: sqlite3.Connection, book: Book, pid: int, old: str, new: str) -> dict[str, Any]:
    """Replace the old rendering with the new one in the translations a decision left behind."""
    from ..translation.rename import for_proposal, replace_rendering

    units = for_proposal(conn, book.id, pid, old)
    n = replace_rendering(conn, units, old, new)
    return {"ok": True, "replaced": n, "human_left": [u["unit_id"] for u in units if u["human"]]}


def rename_preview(conn: sqlite3.Connection, book: Book, pid: int, old: str) -> dict[str, Any]:
    from ..translation.rename import for_proposal

    units = for_proposal(conn, book.id, pid, old)
    return {"units": units, "retranslate": " ".join(f"--unit-id {u['unit_id']}" for u in units if not u["human"])}


def accept_all(conn: sqlite3.Connection, book: Book) -> dict[str, Any]:
    from ..analysis.naming import decide as _decide
    from ..analysis.naming import pending

    rows = pending(conn, book.id)
    with Transaction(conn):
        for r in rows:
            _decide(conn, book.id, r["id"], r["chosen_zh"])
    return {"ok": True, "accepted": len(rows)}


def set_gender(conn: sqlite3.Connection, book: Book, cid: str, gender: str) -> dict[str, Any]:
    """A human setting (female / male), or unknown to clear it: same as `ncte names gender`."""
    from ..memory.repository import set_gender as _set

    if gender not in ("female", "male", "unknown"):
        raise NCTEError("gender must be female, male or unknown")
    c = conn.execute("SELECT book_id, known_from_ordinal FROM characters WHERE id=?", (cid,)).fetchone()
    if c is None:
        raise NCTEError(f"unknown character {cid}")
    with Transaction(conn):
        if gender == "unknown":
            conn.execute("DELETE FROM character_gender WHERE character_id=?", (cid,))
        else:
            _set(conn, c[0], cid, gender, ordinal=c[1], quote=None, origin="human")
    return {"ok": True}
