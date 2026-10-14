"""Validate and persist an LLM memory update (spec §5.5, §6.2).

Facts are stored only when every evidence unit belongs to the scene and the ``evidence_quote`` is
found verbatim (whitespace/ruby-insensitive) in those units. Rejections are logged in
``memory_rejections``; nothing unverified becomes memory.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from ..lexicon import acceptable_glossary_term
from ..llm.schemas import MemoryUpdateOutput
from ..memory import repository as repo
from ..memory.db import utcnow
from ..memory.retrieval import index_memory
from ..series import SERIES_SCOPE, unit_pos, volume_of
from .dialogue import clean
from .scene import PRONOUNS, scene_rows

_WS = re.compile(r"[\s　]+")


def _norm(t: str) -> str:
    return _WS.sub("", clean(t))


def build_memory_payload(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row,
                         glossary: list[dict[str, Any]]) -> dict[str, Any]:
    from ..memory import visibility as vis
    from ..translation.cache import latest_translation

    rows = scene_rows(conn, book_id, scene["id"])
    units = []
    for r in rows:
        item = {"unit_id": r["id"], "source": r["plain_text"]}
        tr = latest_translation(conn, r["id"], r["source_hash"])
        if tr is not None:
            item["translation"] = tr["text"]
        units.append(item)
    forms_at = vis.form_positions(conn, book_id)
    views = {c["id"]: v for c in vis.visible_characters(conn, book_id, scene["end_ordinal"])
             if (v := vis.character_view(conn, book_id, c, scene["end_ordinal"], forms_at))}
    names = {cid: v["name_zh"] or v["name_ja"] for cid, v in views.items()}
    utts = [{"unit_id": u["unit_id"], "quote_index": u["quote_index"],
             "speaker": names.get(u["speaker_id_nullable"]) if u["speaker_id_nullable"] else None,
             "status": u["status"]}
            for u in conn.execute("SELECT * FROM utterances WHERE book_id=? AND scene_id=?", (book_id, scene["id"]))]
    text = " ".join(clean(r["plain_text"]) for r in rows)
    return {"task": "memory_update", "scene_id": scene["id"], "units": units,
            "known_characters": [{"character_id": cid, **v} for cid, v in views.items()],
            "scene": {"utterances": utts, **json.loads(scene["state_json"] or "{}")},
            "glossary": [g for g in glossary if str(g.get("ja", "")) in text]}


def apply_memory_update(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row, parsed: MemoryUpdateOutput,
                        *, run_id: str | None = None) -> dict[str, int]:
    sid = scene["id"]
    rows = scene_rows(conn, book_id, sid)
    text_of = {r["id"]: r["plain_text"] for r in rows}
    ordinal = {r["id"]: r["ordinal"] for r in rows}
    chars = {c["id"]: c for c in repo.characters(conn, book_id)}
    stats = {"facts": 0, "rejected": 0, "glossary": 0, "characters": 0, "props": 0, "retractions": 0}

    def rej(item: object, reason: str) -> None:
        stats["rejected"] += 1
        repo.reject(conn, book_id, sid, "memory_update", item, reason)

    # re-running a scene's memory update replaces that scene's previous facts/props (no duplicates)
    old_ids = [r[0] for r in conn.execute("SELECT id FROM facts WHERE book_id=? AND scene_id=?", (book_id, sid))]
    if old_ids:
        ph = ",".join("?" * len(old_ids))
        conn.execute(f"UPDATE facts SET retracted_by=NULL, retracted_at_ordinal=NULL WHERE retracted_by IN ({ph})", old_ids)
        conn.execute(f"DELETE FROM memory_fts WHERE kind='fact' AND ref_id IN ({ph})", old_ids)
        conn.execute(f"DELETE FROM facts WHERE id IN ({ph})", old_ids)
    conn.execute("DELETE FROM prop_states WHERE book_id=? AND scene_id=?", (book_id, sid))
    conn.execute("DELETE FROM memory_fts WHERE kind='summary' AND ref_id=? AND book_id=?", (sid, book_id))

    for n, f in enumerate(parsed.facts):
        ev = [u for u in f.evidence_unit_ids if u in text_of]
        if not ev or len(ev) != len(f.evidence_unit_ids):
            rej(f.model_dump(), "evidence_unit_ids missing or outside the current scene")
            continue
        quote = _norm(f.evidence_quote)
        if len(quote) < 2 or quote not in _norm("".join(text_of[u] for u in ev)):
            rej(f.model_dump(), "evidence_quote not found verbatim in evidence units")
            continue
        if f.subject_ref.startswith("c_") and f.subject_ref not in chars:
            rej(f.model_dump(), "unknown character id in subject_ref")
            continue
        first = min(ev, key=lambda u: ordinal[u])
        fid = repo.fact_id_for(book_id, sid, n, f.subject_ref + f.predicate + f.object)
        conn.execute(
            "INSERT OR REPLACE INTO facts(id, book_id, subject, predicate, object, fact_type, certainty, fact_json, "
            "evidence_unit_ids_json, evidence_quote, first_revealed_unit_id, first_revealed_ordinal, source_viewpoint, "
            "scene_id, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, book_id, f.subject_ref, f.predicate[:200], f.object[:400], f.fact_type, f.certainty,
             json.dumps(f.model_dump(), ensure_ascii=False), json.dumps(ev), f.evidence_quote[:400], first,
             ordinal[first], f.source_viewpoint, sid, run_id, utcnow()))
        subj = chars[f.subject_ref]["canonical_name"] if f.subject_ref in chars else f.subject_ref
        index_memory(conn, book_id, "fact", fid, ordinal[first], f"{subj} {f.predicate} {f.object}")
        stats["facts"] += 1
        for old in f.retracts_fact_ids:
            r = conn.execute(f"SELECT * FROM facts WHERE id=? AND {SERIES_SCOPE}", (old, book_id)).fetchone()
            if r is not None and not ((unit_pos(conn, r["first_revealed_unit_id"]) or (0.0, 0))
                                      < (volume_of(conn, book_id), ordinal[first])):
                r = None
            if r is None:
                rej({"retracts": old}, "retracted fact unknown or not earlier")
                continue
            conn.execute("UPDATE facts SET retracted_by=?, retracted_at_ordinal=?, revision=revision+1 WHERE id=?",
                         (fid, ordinal[first], old))
            stats["retractions"] += 1

    for g in parsed.glossary_candidates:
        ev = [u for u in g.evidence_unit_ids if u in text_of]
        if not ev or not any(g.source_term in clean(text_of[u]) for u in ev):
            rej(g.model_dump(), "glossary source_term not found verbatim in evidence units")
            continue
        if not acceptable_glossary_term(g.source_term):
            rej(g.model_dump(), "generic or single-character term is not a glossary term")
            continue
        first = min((u for u in ev if g.source_term in clean(text_of[u])), key=lambda u: ordinal[u])
        if g.category == "character" and len(g.source_term) <= 24 and g.source_term not in PRONOUNS:
            _, created = repo.upsert_character(conn, book_id, g.source_term, name_zh=g.target_term, aliases=[],
                                               unit_id=first, ordinal=ordinal[first], origin="memory_update")
            stats["characters"] += int(created)
        if repo.ensure_glossary(conn, book_id, g.source_term, g.target_term, category=g.category,
                                origin="memory_update", first_unit_id=first):
            stats["glossary"] += 1

    # name forms: «A。通称B» statements found by the program, then the model's proposals (ADR-019)
    from ..memory.entities import detect_links, link

    stats["name_links"] = sum(1 for *_, st in detect_links(conn, book_id, rows) if st == "accepted")
    scene_plain = "".join(clean(t) for t in text_of.values())
    for nl in parsed.name_links:
        ev = [u for u in nl.evidence_unit_ids if u in text_of]
        known_terms = {r[0] for r in conn.execute(f"SELECT source_term FROM glossary WHERE {SERIES_SCOPE} AND source_term IN "
                                                  "(?,?)", (book_id, nl.term, nl.same_as))}
        if not ev or _norm(nl.evidence_quote) not in _norm("".join(text_of[u] for u in ev)):
            rej(nl.model_dump(), "name link evidence_quote not found verbatim")
            continue
        if nl.term not in scene_plain or (nl.same_as not in scene_plain and nl.same_as not in known_terms):
            rej(nl.model_dump(), "name link terms must occur verbatim in the scene (or be known forms)")
            continue
        if nl.term == nl.same_as or not acceptable_glossary_term(nl.term):
            rej(nl.model_dump(), "invalid name link term")
            continue
        first = min(ev, key=lambda u: ordinal[u])
        if nl.term not in known_terms and nl.term_zh.strip():
            target_cat = conn.execute(f"SELECT category FROM glossary WHERE {SERIES_SCOPE} AND source_term=?",
                                      (book_id, nl.same_as)).fetchone()
            repo.ensure_glossary(conn, book_id, nl.term, nl.term_zh.strip()[:24],
                                 category=target_cat[0] if target_cat else "term", origin="memory_update",
                                 first_unit_id=first)
        if link(conn, book_id, nl.term, nl.same_as, nl.form_type, origin="llm", unit_id=first,
                quote=nl.evidence_quote, ordinal=ordinal[first], note=nl.note[:200] or None) == "accepted":
            stats["name_links"] += 1

    # who calls whom what, with the rendering the translation used (ADR-022)
    from ..memory.address import upsert as upsert_address

    stats["address_terms"] = 0
    for at in parsed.address_terms:
        ev = [u for u in at.evidence_unit_ids if u in text_of]
        quote = _norm(at.evidence_quote)
        if at.speaker_ref not in chars or at.addressee_ref not in chars:
            rej(at.model_dump(), "address term: speaker/addressee must be known character ids")
            continue
        if not ev or len(quote) < 2 or quote not in _norm("".join(text_of[u] for u in ev)) \
                or _norm(at.term) not in quote or not acceptable_glossary_term(at.term):
            rej(at.model_dump(), "address term: term/evidence_quote not found verbatim")
            continue
        first = min(ev, key=lambda u: ordinal[u])
        upsert_address(conn, book_id, at.speaker_ref, at.addressee_ref, clean(at.term).strip(), ordinal=ordinal[first],
                       unit_id=first, origin="llm", certainty="inferred", form_zh=at.term_zh.strip()[:24] or None,
                       quote=at.evidence_quote, note=at.note[:200] or None)
        stats["address_terms"] += 1

    for p in parsed.prop_states:
        if p.name_ja not in "".join(clean(t) for t in text_of.values()):
            rej(p.model_dump(), "prop not mentioned in scene")
            continue
        holder = p.holder_ref if p.holder_ref in chars else None
        conn.execute("INSERT INTO prop_states(book_id, name_ja, holder_character_id, evidence_unit_ids_json, "
                     "horizon_ordinal, scene_id, created_at) VALUES (?,?,?,?,?,?,?)",
                     (book_id, p.name_ja, holder, json.dumps([u for u in p.evidence_unit_ids if u in text_of]),
                      scene["end_ordinal"], sid, utcnow()))
        stats["props"] += 1

    summary = parsed.scene_summary.strip()[:800]
    if summary:
        chapter = rows[0]["resource_href"] if rows else ""
        conn.execute("DELETE FROM chapter_summaries WHERE book_id=? AND scene_id=?", (book_id, sid))
        conn.execute("INSERT INTO chapter_summaries(book_id, chapter_ref, scene_id, summary, horizon_unit_id, "
                     "horizon_ordinal, run_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (book_id, chapter, sid, summary, scene["end_unit_id"], scene["end_ordinal"], run_id, utcnow()))
        index_memory(conn, book_id, "summary", sid, scene["end_ordinal"], summary)
    repo.refresh_profiles(conn, book_id)
    return stats
