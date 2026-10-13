"""Human corrections (spec §7 human_edits): validated, versioned, never overwritten by machine runs."""

from __future__ import annotations

import json
import sqlite3

from ..errors import NCTEError
from ..memory.db import Transaction, utcnow
from ..qa.rules import Finding, check_translation
from .cache import glossary_entries, latest_translation
from .translator import UnitInput


def apply_human_edit(conn: sqlite3.Connection, book_id: str, unit_id: str, text: str, *, reason: str = "",
                     force: bool = False) -> list[Finding]:
    row = conn.execute("SELECT * FROM units WHERE id=? AND book_id=?", (unit_id, book_id)).fetchone()
    if row is None:
        raise NCTEError(f"unknown unit {unit_id}")
    if not row["translatable"]:
        raise NCTEError(f"unit {unit_id} is kept in the original ({row['skip_reason']}); it cannot be edited safely")
    u = UnitInput.from_row(row)
    findings = check_translation(unit_id=unit_id, kind=u.kind, source_text=u.source_text, plain_text=u.plain_text,
                                 inline_map=u.inline_map, ruby_count=u.ruby_count, text=text,
                                 glossary=glossary_entries(conn, book_id))
    blocking = [f for f in findings if f.severity == "error"]
    if any(f.type == "placeholder_error" for f in blocking):
        raise NCTEError("placeholders must be kept exactly (cannot be forced): "
                        + "; ".join(f.evidence for f in blocking if f.type == "placeholder_error")
                        + f"\nsource: {u.source_text}")
    if blocking and not force:
        raise NCTEError("edit rejected by QA (use --force to override): " + "; ".join(f"{f.type}: {f.evidence}" for f in blocking))
    prev = latest_translation(conn, unit_id, row["source_hash"])
    with Transaction(conn):
        conn.execute("INSERT INTO human_edits(unit_id, previous_text, corrected_text, reason, edited_at) VALUES (?,?,?,?,?)",
                     (unit_id, prev["text"] if prev else None, text, reason, utcnow()))
        conn.execute("INSERT INTO translations(unit_id, target_lang, text, source_hash, prompt_version, model_id, mode, "
                     "status, notes_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (unit_id, "zh-TW", text, row["source_hash"], "human", "human", "human", "human",
                      json.dumps([reason] if reason else [], ensure_ascii=False), utcnow()))
        conn.execute("UPDATE qa_findings SET status='resolved' WHERE unit_id=? AND status='open'", (unit_id,))
    return findings
