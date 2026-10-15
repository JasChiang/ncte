"""A second look at the read pass's disputed judgments, before naming and translation (ADR-033).

Only what the first pass left in doubt is asked again, with the surrounding text: lines whose speaker the model and
the program disagreed on (the model's answer was kept as ``inferred``) or that ended ``unknown`` because adjacent
narration pointed at someone else, genders known only by inference, and characters merged when a name was
revealed. The answers never become facts: a speaker stays ``inferred``; a doubtful inferred gender is dropped (the
translation then uses 你); a merge judged "different" stops counting the merged description as this character
(its forms leave the aliases) and becomes a finding for the user — lines moved by the merge are not moved back.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..llm.schemas import VerifyOutput
from ..memory import repository as repo
from ..memory.db import utcnow

CONTEXT_UNITS = 4  # paragraphs before and after a disputed line
MAX_ITEMS = 150


def _names(conn: sqlite3.Connection, book_id: str) -> dict[str, str]:
    from ..series import SERIES_SCOPE

    return {r["id"]: r["canonical_name"] + (f"（{r['display_name_zh']}）" if r["display_name_zh"] else "")
            for r in conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book_id,))}


def _window(conn: sqlite3.Connection, book_id: str, ordinal: int, mark: str | None = None) -> list[str]:
    rows = conn.execute("SELECT ordinal, plain_text FROM units WHERE book_id=? AND translatable=1 AND ordinal BETWEEN ? "
                        "AND ? ORDER BY ordinal", (book_id, ordinal - CONTEXT_UNITS, ordinal + CONTEXT_UNITS)).fetchall()
    return [("▶ " if r["ordinal"] == ordinal else "") + match_text(r["plain_text"])[:200] for r in rows]


def collect(conn: sqlite3.Connection, book_id: str) -> list[dict[str, Any]]:
    """Items to look at again. Keys starting with ``_`` stay out of the request."""
    names = _names(conn, book_id)
    items: list[dict[str, Any]] = []
    for u in conn.execute(
            "SELECT ut.*, un.ordinal FROM utterances ut JOIN units un ON un.id=ut.unit_id WHERE ut.book_id=? AND "
            "ut.status IN ('inferred','unknown') AND (ut.notes_json LIKE '%(LLM kept)%' OR ut.notes_json LIKE "
            "'%conflict: action evidence%') ORDER BY un.ordinal", (book_id,)):
        cands = sorted({h["candidate_character_id"] for h in conn.execute(
            "SELECT candidate_character_id FROM speaker_hypotheses WHERE utterance_id=?", (u["id"],))} & names.keys())
        items.append({"ref": f"q:{u['id']}", "kind": "speaker", "quote": match_text(u["quote_text"])[:120],
                      "context": _window(conn, book_id, u["ordinal"]),
                      "candidates": {c: names[c] for c in cands},
                      "first_answer": names.get(u["speaker_id_nullable"]) if u["speaker_id_nullable"] else None,
                      "_ordinal": u["ordinal"]})
    for g in conn.execute("SELECT g.* FROM character_gender g JOIN characters c ON c.id=g.character_id WHERE "
                          "c.book_id=? AND g.certainty='inferred' AND g.origin='llm' AND g.gender IN ('female','male')",
                          (book_id,)):
        c = conn.execute("SELECT * FROM characters WHERE id=?", (g["character_id"],)).fetchone()
        forms = [c["canonical_name"], *json.loads(c["aliases_json"] or "[]")]
        hits = [r for r in conn.execute("SELECT ordinal, plain_text FROM units WHERE book_id=? AND translatable=1 "
                                        "ORDER BY ordinal", (book_id,)) if any(f in r["plain_text"] for f in forms)]
        spread = hits if len(hits) <= 5 else [hits[i * (len(hits) - 1) // 4] for i in range(5)]
        items.append({"ref": f"g:{c['id']}", "kind": "gender", "character": names.get(c["id"], c["canonical_name"]),
                      "first_answer": g["gender"], "evidence": g["evidence_quote"],
                      "samples": [match_text(r["plain_text"])[:160] for r in spread]})
    for link in conn.execute("SELECT * FROM entity_links WHERE book_id=? AND origin='scene_analysis' AND "
                             "status='accepted' AND form_type='official'", (book_id,)):
        c = conn.execute("SELECT * FROM characters WHERE book_id=? AND canonical_name=?",
                         (book_id, link["target_term"])).fetchone()
        if c is None:
            continue
        # the new name and its own aliases were linked at the reveal; the rest came from the merged description
        own = {r[0] for r in conn.execute("SELECT term FROM entity_links WHERE book_id=? AND target_term=? AND "
                                          "origin='scene_analysis'", (book_id, c["canonical_name"]))}
        others = [a for a in json.loads(c["aliases_json"] or "[]") if a and a not in own]
        if not others:
            continue
        items.append({"ref": f"m:{c['id']}", "kind": "merge", "name": c["canonical_name"], "merged_forms": others,
                      "context": _window(conn, book_id, link["revealed_ordinal"] or c["known_from_ordinal"]),
                      "_ordinal": link["revealed_ordinal"]})
    return items[:MAX_ITEMS]


def apply(conn: sqlite3.Connection, book_id: str, items: list[dict[str, Any]], parsed: VerifyOutput,
          finding: Any) -> dict[str, int]:
    """Apply the second look. ``finding(unit_id, type, severity, evidence)`` records what needs the user."""
    from ..memory.address import reharvest_scene

    by_ref = {i["ref"]: i for i in items}
    stats = {"speakers_changed": 0, "speakers_unknown": 0, "speakers_kept": 0, "genders_dropped": 0,
             "genders_kept": 0, "merges_doubted": 0, "ignored": 0}
    scenes: set[str] = set()
    for a in parsed.items:
        item = by_ref.get(a.ref)
        if item is None:
            stats["ignored"] += 1
            continue
        kind, key = a.ref.split(":", 1)
        answer = a.answer.strip()
        if kind == "q":
            u = conn.execute("SELECT * FROM utterances WHERE id=?", (key,)).fetchone()
            if u is None or u["status"] == "human_verified":
                continue
            note = f"second look: {answer} — {a.reason[:160]}"
            if answer in item["candidates"] or (answer.startswith("c_") and conn.execute(
                    "SELECT 1 FROM characters WHERE id=? AND book_id=?", (answer, book_id)).fetchone()):
                new_id, status = answer, "inferred"
            elif answer == "unknown":
                new_id, status = None, "unknown"
            else:
                stats["ignored"] += 1
                continue
            if new_id == u["speaker_id_nullable"] and status == u["status"]:
                stats["speakers_kept"] += 1
            else:
                stats["speakers_changed" if new_id else "speakers_unknown"] += 1
                if u["scene_id"]:
                    scenes.add(u["scene_id"])
            notes = json.loads(u["notes_json"]) + [note]
            conn.execute("UPDATE utterances SET speaker_id_nullable=?, status=?, resolution_source='verify', "
                         "notes_json=?, updated_at=? WHERE id=?",
                         (new_id, status, json.dumps(notes, ensure_ascii=False), utcnow(), key))
            repo.remove_speech_examples_for(conn, key)
        elif kind == "g":
            if answer == item["first_answer"]:
                stats["genders_kept"] += 1
            else:  # a doubtful inference is not kept: the translation falls back to 你
                conn.execute("DELETE FROM character_gender WHERE character_id=? AND origin='llm' AND certainty='inferred'",
                             (key,))
                stats["genders_dropped"] += 1
        elif kind == "m":
            if answer != "same":
                unit = conn.execute("SELECT id FROM units WHERE book_id=? AND ordinal=?",
                                    (book_id, item.get("_ordinal") or 0)).fetchone()
                dropped = answer == "different"  # the merged description no longer names this character
                if dropped:
                    repo.drop_aliases(conn, key, item["merged_forms"])
                    stats["merges_undone"] = stats.get("merges_undone", 0) + 1
                finding(unit[0] if unit else None, "merge_doubted", "high",
                        f"{item['name']} ← {', '.join(item['merged_forms'])}: {answer} — {a.reason[:200]}"
                        + ("（這些稱呼已不再算作此角色；合併時搬過來的台詞請人工確認）" if dropped else ""))
                stats["merges_doubted"] += 1
    for sid in scenes:
        reharvest_scene(conn, book_id, sid)
    repo.refresh_profiles(conn, book_id)
    return stats
