"""Builds the per-batch context payload (spec §5.6).

Baseline: neighbouring source units (+ already committed translations of previous units) and
relevant glossary. Context mode additionally injects scene state, character profiles, verified
speech examples, summaries and facts — all filtered by visibility (no future knowledge).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..config import Config
from .cache import latest_translation, relevant_glossary

BODY_KINDS = ("text", "heading")
_WHOLE = re.compile(r"⟦(\d+)⟧(.*)⟦/\1⟧", re.S)  # one placeholder around a whole unit
META_KINDS = ("head_title", "nav", "ncx", "opf_title")


def neighbor_context(conn: sqlite3.Connection, cfg: Config, book_id: str, rows: list[sqlite3.Row],
                     *, scene_id: str | None = None, whole_scene: bool = False) -> dict[str, Any]:
    """Previous / next units as context. ``whole_scene`` (retranslating chosen units): the rest of the scene on
    both sides, nearest first, up to ``targeted_scene_context_chars`` Japanese characters per side."""
    first, last = rows[0]["ordinal"], rows[-1]["ordinal"]
    if whole_scene and (scene_id or rows[-1]["scene_id"]) and cfg.translation.targeted_scene_context_chars:
        return _scene_window(conn, cfg, book_id, rows, scene_id or rows[-1]["scene_id"])
    # lookahead never crosses the current scene (spec §5.5), in every mode; "none" disables it
    scene_id = scene_id or rows[-1]["scene_id"]
    if cfg.app.lookahead_scope == "none":
        nxt_limit = 0
    else:
        nxt_limit = cfg.translation.include_next_units
    scene_clause = " AND scene_id=?" if scene_id else ""
    prev = conn.execute(
        "SELECT * FROM units WHERE book_id=? AND ordinal<? AND translatable=1 AND kind IN ('text','heading') "
        "AND spine_index IS NOT NULL ORDER BY ordinal DESC LIMIT ?",
        (book_id, first, cfg.translation.include_previous_units)).fetchall()[::-1]
    nxt_params: list[Any] = [book_id, last]
    if scene_clause:
        nxt_params.append(scene_id)
    nxt_params.append(nxt_limit)
    nxt = conn.execute(
        "SELECT * FROM units WHERE book_id=? AND ordinal>? AND translatable=1 AND kind IN ('text','heading') "
        f"AND spine_index IS NOT NULL{scene_clause} ORDER BY ordinal LIMIT ?", nxt_params).fetchall()
    before = []
    for r in prev:
        tr = latest_translation(conn, r["id"], r["source_hash"])
        item = {"unit_id": r["id"], "source": r["source_text"]}
        if tr is not None:
            item["translation"] = tr["text"]
        before.append(item)
    return {"context_before": before,
            "context_after": [{"unit_id": r["id"], "source": r["source_text"]} for r in nxt]}


def _scene_window(conn: sqlite3.Connection, cfg: Config, book_id: str, rows: list[sqlite3.Row],
                  scene_id: str) -> dict[str, Any]:
    budget = cfg.translation.targeted_scene_context_chars
    first, last = rows[0]["ordinal"], rows[-1]["ordinal"]
    q = ("SELECT * FROM units WHERE book_id=? AND scene_id=? AND translatable=1 AND kind IN ('text','heading') "
         "AND spine_index IS NOT NULL AND ordinal {} ? ORDER BY ordinal {}")

    def take(cands: list[sqlite3.Row]) -> list[sqlite3.Row]:
        out: list[sqlite3.Row] = []
        used = 0
        for r in cands:
            used += len(r["plain_text"])
            if out and used > budget:
                break
            out.append(r)
        return out

    prev = take(conn.execute(q.format("<", "DESC"), (book_id, scene_id, first)).fetchall())[::-1]
    nxt = take(conn.execute(q.format(">", "ASC"), (book_id, scene_id, last)).fetchall())
    before = []
    for r in prev:
        item = {"unit_id": r["id"], "source": r["source_text"]}
        tr = latest_translation(conn, r["id"], r["source_hash"])
        if tr is not None:
            item["translation"] = tr["text"]
        before.append(item)
    return {"context_before": before,
            "context_after": [{"unit_id": r["id"], "source": r["source_text"]} for r in nxt]}


def glossary_payload(entries: list[dict[str, Any]], text: str, *, upto: tuple[float, int] | None = None,
                     revealed: dict[str, tuple[float, int]] | None = None) -> list[dict[str, Any]]:
    """Terms present in ``text`` + (for named entities) the other name forms already revealed by story position
    ``upto`` = (volume, ordinal). Renderings of terms present in the text are always given (consistency across
    volumes, even when a later volume was translated first); sibling forms and their links only once revealed."""
    revealed = revealed or {}

    def visible(g: dict[str, Any]) -> bool:
        if upto is None:
            return True
        first = g.get("first_pos")
        rev = revealed.get(g["source_term"])
        return (first is None or first <= upto) and (rev is None or rev <= upto)

    by_entity: dict[str, list[dict[str, Any]]] = {}
    for g in entries:
        if g.get("entity_id"):
            by_entity.setdefault(g["entity_id"], []).append(g)
    out = []
    for g in relevant_glossary(entries, text):
        item: dict[str, Any] = {"ja": g["source_term"], "zh": g["target_term"], "verified": g["verified"],
                                "category": g["category"]}
        siblings = [o for o in by_entity.get(g.get("entity_id") or "", []) if o is not g and visible(o)]
        if siblings and visible(g):
            item["form"] = g.get("form_type") or "official"
            if g.get("form_note"):
                item["note"] = g["form_note"]
            item["same_entity_as"] = [{"ja": o["source_term"], "zh": o["target_term"],
                                       "form": o.get("form_type") or "official"} for o in siblings]
        others = [x for x in g.get("senses") or [] if x["entity_id"] in by_entity
                  and (upto is None or x["pos"] is None or x["pos"] <= upto)]
        if others:  # a shared form (ADR-022): say who it may refer to, never which one
            item["may_refer_to"] = [_entity_name(by_entity.get(g.get("entity_id") or "", [g]))] + \
                [_entity_name(by_entity[x["entity_id"]]) for x in others]
        out.append(item)
    return out


def _entity_name(forms: list[dict[str, Any]]) -> dict[str, str]:
    head = next((f for f in forms if f.get("form_type") == "official"), forms[0])
    return {"ja": head["source_term"], "zh": head["target_term"]}


def heading_hints(conn: sqlite3.Connection, book_id: str) -> list[dict[str, str]]:
    """Translated chapter titles, given to the TOC/title task (and reused for an identical TOC entry) so the TOC matches
    them: headings, and any paragraph whose source is exactly a TOC entry (a book's own contents page, a title set as a
    styled paragraph), with one placeholder around the whole of it taken off."""
    rows = conn.execute(
        "SELECT * FROM units u WHERE book_id=? AND (kind='heading' OR (kind='text' AND spine_index IS NOT NULL AND "
        "length(plain_text)<=40 AND plain_text IN (SELECT plain_text FROM units v WHERE v.book_id=u.book_id AND "
        "v.kind IN ('nav','ncx')))) ORDER BY ordinal", (book_id,)).fetchall()
    out = []
    for r in rows:
        tr = latest_translation(conn, r["id"], r["source_hash"])
        if tr is None:
            continue
        src, zh = r["source_text"], tr["text"]
        if (m := _WHOLE.fullmatch(src)) and (z := _WHOLE.fullmatch(zh)) and m.group(1) == z.group(1):
            src, zh = m.group(2), z.group(2)
        if "⟦" not in zh and "⟦" not in src:
            out.append({"ja": r["plain_text"], "zh": zh})
    return out
