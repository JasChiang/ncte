"""Re-run program QA over existing translations after the rules improve (no LLM calls, no cost)."""

from __future__ import annotations

import sqlite3

from ..memory.db import Transaction, utcnow
from ..translation.cache import glossary_entries, latest_texts
from ..translation.translator import UnitInput
from .rules import check_translation, worst

QA_TYPES = ("empty_translation", "placeholder_error", "untranslated", "japanese_residue", "ruby_notation_leak",
            "too_short", "too_long", "quote_mismatch", "number_mismatch", "non_taiwan_glyph", "glossary_mismatch",
            "possible_added_name", "cantonese", "offensive_term", "name_form_substitution")


def recheck_book(conn: sqlite3.Connection, book_id: str) -> dict[str, int]:
    """Supersede old program-QA findings, store fresh ones and fix needs_review/succeeded statuses.

    Human translations are checked too but their status is never changed."""
    gl = glossary_entries(conn, book_id)
    best = latest_texts(conn, book_id)
    stats = {"checked": 0, "cleared": 0, "newly_flagged": 0, "still_flagged": 0}
    units = {r["id"]: r for r in conn.execute("SELECT * FROM units WHERE book_id=?", (book_id,))}
    ph = ",".join("?" * len(QA_TYPES))
    with Transaction(conn):
        for uid, t in best.items():
            u = UnitInput.from_row(units[uid])
            f = check_translation(unit_id=uid, kind=u.kind, source_text=u.source_text, plain_text=u.plain_text,
                                  inline_map=u.inline_map, ruby_count=u.ruby_count, text=t["text"], glossary=gl)
            stats["checked"] += 1
            # a reviewer (or human) already looked at exactly this finding and found no problem: keep that decision
            settled = {(r[0], r[1]) for r in conn.execute(
                "SELECT type, evidence FROM qa_findings WHERE unit_id=? AND status IN ('cleared_by_review','accepted')",
                (uid,))}
            f = [x for x in f if (x.type, x.evidence) not in settled]
            conn.execute(f"UPDATE qa_findings SET status='superseded' WHERE unit_id=? AND status='open' AND type IN ({ph})",
                         (uid, *QA_TYPES))
            for x in f:
                if x.severity not in ("info", "error"):
                    conn.execute("INSERT INTO qa_findings(run_id, book_id, unit_id, type, severity, evidence, created_at) "
                                 "VALUES (?,?,?,?,?,?,?)", (t["run_id"], book_id, uid, x.type, x.severity, x.evidence,
                                                            utcnow()))
            if t["status"] == "human":
                continue
            high = worst(f) in ("high", "error")
            if t["status"] == "needs_review" and not high:
                conn.execute("UPDATE translations SET status='succeeded' WHERE id=?", (t["id"],))
                stats["cleared"] += 1
            elif t["status"] == "succeeded" and high:
                conn.execute("UPDATE translations SET status='needs_review' WHERE id=?", (t["id"],))
                stats["newly_flagged"] += 1
            elif high:
                stats["still_flagged"] += 1
    return stats
