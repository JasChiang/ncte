"""Builds the per-batch context payload (spec §5.6).

Baseline: neighbouring source units (+ already committed translations of previous units) and
relevant glossary. Context mode additionally injects scene state, character profiles, verified
speech examples, summaries and facts — all filtered by visibility (no future knowledge).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Set as AbstractSet
from typing import Any

from ..config import Config
from .cache import latest_translation, relevant_glossary

BODY_KINDS = ("text", "heading")
_WHOLE = re.compile(r"⟦(\d+)⟧(.*)⟦/\1⟧", re.S)  # one placeholder around a whole unit
META_KINDS = ("head_title", "nav", "ncx", "opf_title")
FIX_LEAST_UNITS = 2  # a fix sees at least this many paragraphs on each side (when its scene has them)


def neighbor_context(conn: sqlite3.Connection, cfg: Config, book_id: str, rows: list[sqlite3.Row],
                     *, scene_id: str | None = None, whole_scene: bool = False, fix: bool = False,
                     scene_start: bool = False) -> dict[str, Any]:
    """Previous / next units as context. ``whole_scene`` (retranslating chosen units): the rest of the scene on
    both sides, nearest first, up to ``targeted_scene_context_chars`` Japanese characters per side. ``fix`` (a fix in
    the "window" scope): ``around`` each fixed unit its own window instead (``fix_windows``) — the fixes of one request
    may be far apart in their scene. ``scene_start`` (context mode, a request per scene): a request that begins a scene
    sees the end of the scene before, up to ``scene_start_context_chars``."""
    first, last = rows[0]["ordinal"], rows[-1]["ordinal"]
    if fix and (scene_id or rows[-1]["scene_id"]) and cfg.translation.fix_context_chars:
        return {"around": fix_windows(conn, cfg, book_id, rows)}
    if whole_scene and (scene_id or rows[-1]["scene_id"]) and cfg.translation.targeted_scene_context_chars:
        return _scene_window(conn, book_id, rows, scene_id or rows[-1]["scene_id"],
                             budget=cfg.translation.targeted_scene_context_chars,
                             start_chars=cfg.translation.scene_start_context_chars if scene_start else 0)
    # lookahead never crosses the current scene (spec §5.5), in every mode; "none" disables it
    scene_id = scene_id or rows[-1]["scene_id"]
    starts_scene = scene_start and bool(scene_id) and _first_of_scene(conn, book_id, rows[0], scene_id)
    if cfg.app.lookahead_scope == "none":
        nxt_limit = 0
    else:
        nxt_limit = cfg.translation.include_next_units
    scene_clause = " AND scene_id=?" if scene_id else ""
    prev = _before(conn, book_id, first, cfg.translation.include_previous_units,
                   cfg.translation.scene_start_context_chars if starts_scene else 0)
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


def _first_of_scene(conn: sqlite3.Connection, book_id: str, row: sqlite3.Row, scene_id: str) -> bool:
    got = conn.execute("SELECT MIN(ordinal) FROM units WHERE book_id=? AND scene_id=? AND translatable=1 AND kind IN "
                       "('text','heading') AND spine_index IS NOT NULL", (book_id, scene_id)).fetchone()
    return got is not None and got[0] == row["ordinal"]


def _before(conn: sqlite3.Connection, book_id: str, first: int, least: int, chars: int) -> list[sqlite3.Row]:
    """The paragraphs just before ``first``, in story order: at least ``least``, and on to ``chars`` Japanese
    characters (a scene's start: the end of the scene before, whichever chapter it is in)."""
    out: list[sqlite3.Row] = []
    used = 0
    # a paragraph the content filter refused would get this request refused too (reading leaves it out the same way)
    for r in conn.execute("SELECT * FROM units u WHERE book_id=? AND ordinal<? AND translatable=1 AND kind IN "
                          "('text','heading') AND spine_index IS NOT NULL AND NOT EXISTS (SELECT 1 FROM qa_findings f "
                          "WHERE f.unit_id=u.id AND f.type IN ('content_blocked','read_blocked')) "
                          "ORDER BY ordinal DESC LIMIT 40", (book_id, first)):
        if len(out) >= least and used >= chars:
            break
        out.append(r)
        used += len(r["plain_text"])
    return out[::-1]


def fix_window_rows(conn: sqlite3.Connection, cfg: Config, book_id: str, row: sqlite3.Row,
                    skip: AbstractSet[str] = frozenset()) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """The paragraphs before and after one fixed unit (ADR-077): within ``fix_context_chars`` a side, at least two a
    side when its scene has them, never past the scene. ``skip``: the other units fixed in the same request — they are
    in it already, and do not count against the window."""
    return _window_rows(conn, book_id, [row], row["scene_id"], budget=cfg.translation.fix_context_chars,
                        least=FIX_LEAST_UNITS, skip=skip)


def fix_windows(conn: sqlite3.Connection, cfg: Config, book_id: str,
                rows: list[sqlite3.Row]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Each fixed unit's own window: ``before`` with the current translation, ``after`` the source only."""
    fixed = {r["id"] for r in rows}
    out = {}
    for r in rows:
        prev, nxt = fix_window_rows(conn, cfg, book_id, r, fixed - {r["id"]})
        out[r["id"]] = {"before": _with_translation(conn, prev),
                        "after": [{"unit_id": x["id"], "source": x["source_text"]} for x in nxt]}
    return out


def shown_context(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """The paragraphs a request shows as context (before and after its units, or around each fixed one), each once."""
    items = [*ctx.get("context_before", []), *ctx.get("context_after", [])]
    for w in ctx.get("around", {}).values():
        items += [*w["before"], *w["after"]]
    out, seen = [], set()
    for c in items:
        if c["unit_id"] not in seen:
            seen.add(c["unit_id"])
            out.append(c)
    return out


def _window_rows(conn: sqlite3.Connection, book_id: str, rows: list[sqlite3.Row], scene_id: str, *, budget: int,
                 least: int = 1, skip: AbstractSet[str] = frozenset()) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    first, last = rows[0]["ordinal"], rows[-1]["ordinal"]
    q = ("SELECT * FROM units WHERE book_id=? AND scene_id=? AND translatable=1 AND kind IN ('text','heading') "
         "AND spine_index IS NOT NULL AND ordinal {} ? ORDER BY ordinal {}")

    def take(cands: list[sqlite3.Row]) -> list[sqlite3.Row]:
        out: list[sqlite3.Row] = []
        used = 0
        for r in cands:
            if r["id"] in skip:
                continue
            used += len(r["plain_text"])
            if len(out) >= least and used > budget:
                break
            out.append(r)
        return out

    prev = take(conn.execute(q.format("<", "DESC"), (book_id, scene_id, first)).fetchall())[::-1]
    nxt = take(conn.execute(q.format(">", "ASC"), (book_id, scene_id, last)).fetchall())
    return prev, nxt


def _with_translation(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        item = {"unit_id": r["id"], "source": r["source_text"]}
        tr = latest_translation(conn, r["id"], r["source_hash"])
        if tr is not None:
            item["translation"] = tr["text"]
        out.append(item)
    return out


def _scene_window(conn: sqlite3.Connection, book_id: str, rows: list[sqlite3.Row],
                  scene_id: str, *, budget: int, least: int = 1, start_chars: int = 0) -> dict[str, Any]:
    prev, nxt = _window_rows(conn, book_id, rows, scene_id, budget=budget, least=least)
    if not prev and start_chars and _first_of_scene(conn, book_id, rows[0], scene_id):
        prev = _before(conn, book_id, rows[0]["ordinal"], 1, start_chars)  # retranslating a scene's start
    return {"context_before": _with_translation(conn, prev),
            "context_after": [{"unit_id": r["id"], "source": r["source_text"]} for r in nxt]}


def glossary_payload(entries: list[dict[str, Any]], text: str, *, upto: tuple[float, int] | None = None,
                     revealed: dict[str, tuple[float, int]] | None = None,
                     readings: bool = False) -> list[dict[str, Any]]:
    """Terms present in ``text`` + (for named entities) the other name forms already revealed by story position
    ``upto`` = (volume, ordinal). Renderings of terms present in the text are always given (consistency across
    volumes, even when a later volume was translated first); sibling forms and their links only once revealed.
    ``readings`` (the translator's request): a term the source annotates with a reading carries it."""
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
        if readings and g.get("readings"):
            item["readings"] = g["readings"]
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


REPEAT_MIN = 8  # characters: a short line (「……」「はい」) comes again by itself, not as an echo
_NOT_WORDS = re.compile(r"[\s　―─—…・「」『』（）()]+")


def repeated_earlier(conn: sqlite3.Connection, book_id: str, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Paragraphs written word for word as an earlier paragraph of the book (an opening line the author repeats
    later), each with that paragraph's current translation. Only the match is the program's: whether the echo is
    meant, and so kept, is the model's to judge."""
    from ..epub.units import PH_RE

    out: list[dict[str, Any]] = []
    for r in rows:
        if len(_NOT_WORDS.sub("", r["plain_text"])) < REPEAT_MIN:
            continue
        for e in conn.execute("SELECT id, ordinal, source_hash FROM units WHERE book_id=? AND plain_text=? AND "
                              "ordinal<? AND kind IN ('text', 'heading') ORDER BY ordinal",
                              (book_id, r["plain_text"], r["ordinal"])):
            if (t := latest_translation(conn, e["id"], e["source_hash"])) is not None:
                out.append({"unit_id": r["id"], "earlier_translation": PH_RE.sub("", t["text"]).strip()})
                break
    return out
