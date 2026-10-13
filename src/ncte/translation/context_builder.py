"""Builds the per-batch context payload (spec §5.6).

Baseline: neighbouring source units (+ already committed translations of previous units) and
relevant glossary. Context mode additionally injects scene state, character profiles, verified
speech examples, summaries and facts — all filtered by visibility (no future knowledge).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..config import Config
from .cache import latest_translation, relevant_glossary

BODY_KINDS = ("text", "heading")
META_KINDS = ("head_title", "nav", "ncx", "opf_title")


def neighbor_context(conn: sqlite3.Connection, cfg: Config, book_id: str, rows: list[sqlite3.Row],
                     *, scene_id: str | None = None) -> dict[str, Any]:
    first, last = rows[0]["ordinal"], rows[-1]["ordinal"]
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
    """Translated headings, given to the TOC/title task so the TOC matches chapter titles."""
    rows = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='heading' ORDER BY ordinal",
                        (book_id,)).fetchall()
    out = []
    for r in rows:
        tr = latest_translation(conn, r["id"], r["source_hash"])
        if tr is not None and "⟦" not in tr["text"] and "⟦" not in r["source_text"]:
            out.append({"ja": r["plain_text"], "zh": tr["text"]})
    return out
