"""Re-run program QA over existing translations after the rules improve (no LLM calls, no cost)."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..memory.db import Transaction, utcnow
from ..translation.cache import glossary_entries, latest_texts, settle_translation
from ..translation.translator import UnitInput
from . import second_person
from .rules import Finding, check_translation, worst
from .typeset import typeset

QA_TYPES = ("empty_translation", "placeholder_error", "untranslated", "japanese_residue", "ruby_notation_leak",
            "too_short", "too_long", "quote_mismatch", "number_mismatch", "non_taiwan_glyph", "glossary_mismatch",
            "possible_added_name", "cantonese", "offensive_term", "name_form_substitution", "second_person_gender", "narration_first_person", "latin_word")


def recheck_book(conn: sqlite3.Connection, book_id: str) -> dict[str, int]:
    """Supersede old program-QA findings, store fresh ones and fix needs_review/succeeded statuses.

    Human translations are checked too but their status is never changed."""
    gl = glossary_entries(conn, book_id)
    best = latest_texts(conn, book_id)
    stats = {"checked": 0, "cleared": 0, "newly_flagged": 0, "still_flagged": 0}
    from ..analysis.narration import first_person_slip, for_scene
    from ..epub.matter import story_hrefs

    story = story_hrefs(conn, book_id)
    third_scenes = {sc["id"] for sc in conn.execute("SELECT * FROM scenes WHERE book_id=?", (book_id,))
                    if (w := for_scene(conn, book_id, sc)) is not None and w["person"] == "third"}
    units = {r["id"]: r for r in conn.execute("SELECT * FROM units WHERE book_id=?", (book_id,))}
    ph = ",".join("?" * len(QA_TYPES))
    with Transaction(conn):
        for uid, row in best.items():
            u = UnitInput.from_row(units[uid])
            t: dict[str, Any] = dict(row)
            if t["status"] != "human" and (fixed := typeset(t["text"], u.source_text)) != t["text"]:  # house punctuation (ADR-053)
                conn.execute("UPDATE translations SET text=? WHERE id=?", (fixed, t["id"]))
                t["text"] = fixed
                stats["typeset"] = stats.get("typeset", 0) + 1
            f = check_translation(unit_id=uid, kind=u.kind, source_text=u.source_text, plain_text=u.plain_text,
                                  inline_map=u.inline_map, ruby_count=u.ruby_count, text=t["text"], glossary=gl)
            stats["checked"] += 1
            # a reviewer (or human) already looked at exactly this finding and found no problem: keep that decision
            # (the model's check of a rule flag counts too; it appends its reason to the evidence)
            settled = [(r[0], r[1] or "") for r in conn.execute(
                "SELECT type, evidence FROM qa_findings WHERE unit_id=? AND status IN ('cleared_by_review','accepted',"
                "'dismissed_by_check')", (uid,))]
            if (ev := second_person.check(conn, book_id, units[uid], t["text"])):
                f.append(Finding("second_person_gender", "high", ev, uid))
            if units[uid]["scene_id"] in third_scenes and units[uid]["resource_href"] in story \
                    and first_person_slip(units[uid]["plain_text"], t["text"]):
                f.append(Finding("narration_first_person", "high", "third-person narration says 我 outside quotes", uid))
            f = [x for x in f if not any(x.type == ty and ev.startswith(x.evidence) for ty, ev in settled)]
            conn.execute(f"UPDATE qa_findings SET status='superseded' WHERE unit_id=? AND status='open' AND type IN ({ph})",
                         (uid, *QA_TYPES))
            for x in f:  # an "error" here is a translation already stored (rules got stricter): keep it visible
                if x.severity != "info":
                    conn.execute("INSERT INTO qa_findings(run_id, book_id, unit_id, type, severity, evidence, created_at) "
                                 "VALUES (?,?,?,?,?,?,?)", (t["run_id"], book_id, uid, x.type, x.severity, x.evidence,
                                                            utcnow()))
            if t["status"] == "human":
                continue
            high = worst(f) in ("high", "error")
            # the rules see words, not meaning: an open reviewer's issue or an unsettled fix on this text keeps it
            # waiting for review whatever the rules say now
            semantic = conn.execute(
                "SELECT 1 FROM qa_findings WHERE unit_id=? AND status='open' AND severity IN ('high','medium') AND "
                "(type LIKE 'review:%' OR type LIKE 'fix\\_%' ESCAPE '\\') AND (translation_id IS NULL OR translation_id=?) "
                "LIMIT 1", (uid, t["id"])).fetchone() is not None
            if t["status"] == "needs_review" and not high and not semantic:
                settle_translation(conn, t["id"])
                stats["cleared"] += 1
            elif t["status"] == "succeeded" and high:
                conn.execute("UPDATE translations SET status='needs_review' WHERE id=?", (t["id"],))
                stats["newly_flagged"] += 1
            elif high:
                stats["still_flagged"] += 1
        # a unit that failed once and has a translation now (retranslated later): the old failure is settled
        done = [uid for uid in best]
        for i in range(0, len(done), 500):
            part = done[i:i + 500]
            conn.execute(f"UPDATE qa_findings SET status='superseded' WHERE book_id=? AND status='open' AND "
                         f"type IN ('translation_failed','content_blocked') AND unit_id IN ({','.join('?' * len(part))})",
                         (book_id, *part))
    return stats
