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
    for g in conn.execute("SELECT group_id, member_id FROM group_members WHERE book_id=?", (book_id,)):
        if g[0] in views and g[1] in names:  # a group card shows who it is known to be made of
            views[g[0]].setdefault("members", []).append(names[g[1]])
    utts = [{"unit_id": u["unit_id"], "quote_index": u["quote_index"],
             "speaker": u["speaker_id_nullable"],  # an id like known_characters' (the model writes speaker_ref with it)
             "status": u["status"]}
            for u in conn.execute("SELECT * FROM utterances WHERE book_id=? AND scene_id=?", (book_id, scene["id"]))]
    text = " ".join(clean(r["plain_text"]) for r in rows)
    out = {"task": "memory_update", "scene_id": scene["id"], "units": units,
           "known_characters": [{"character_id": cid, **v} for cid, v in views.items()],
           "scene": {"utterances": utts, **{k: v for k, v in json.loads(scene["state_json"] or "{}").items()
                                            if k not in ("run_id", "analysis", "quotes")}},
           "glossary": [g for g in glossary if str(g.get("ja", "")) in text]}
    if known := known_facts(conn, book_id, scene, views, names, text):
        out["known_facts"] = known
    if "kind" in scene.keys() and scene["kind"] and scene["kind"] != "present":
        out["scene_kind"] = scene["kind"]  # a flashback's facts are of its time
    return out


KNOWN_FACTS = 30  # earlier facts shown to a scene's memory update: the most related to it, in story order


def known_facts(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row, views: dict[str, dict[str, Any]],
                names: dict[str, str], text: str) -> list[dict[str, Any]]:
    """Earlier facts about the people (and things) of this scene, with their ids: a scene that changes, corrects or
    refutes one can say which (``retracts_fact_ids``) — without them the model could only guess ids."""
    from ..memory import visibility as vis

    here = {cid for cid, v in views.items() if any(n and n in text for n in [v["name_ja"], *v.get("aliases_ja", [])])}
    here |= {r[0] for r in conn.execute("SELECT DISTINCT speaker_id_nullable FROM utterances WHERE book_id=? AND scene_id=? "
                                        "AND speaker_id_nullable IS NOT NULL", (book_id, scene["id"]))}
    from ..memory.retrieval import WORLD_EXTRA, _grams, fact_overlap

    grams = _grams(text)
    scored = []
    for i, f in enumerate(vis.visible_facts(conn, book_id, scene["start_ordinal"])):
        about = f["subject"] in here or f["object_ref"] in here or (not f["subject"].startswith("c_") and f["subject"] in text)
        overlap = fact_overlap(f, grams)
        if about or overlap >= 1.0 + WORLD_EXTRA:  # the world's facts too, when this scene takes them up
            scored.append((overlap + (1.0 if about else 0.0) + i * 1e-4, i, f))  # ties: the later fact
    out = []
    for _, _, f in sorted(sorted(scored, key=lambda x: -x[0])[:KNOWN_FACTS], key=lambda x: x[1]):
        item: dict[str, Any] = {"fact_id": f["id"], "subject": names.get(f["subject"], f["subject"]),
                                "predicate": f["predicate"], "object": f["object"], "fact_type": f["fact_type"],
                                "certainty": f["certainty"]}
        if f["source_viewpoint"]:
            item["source_viewpoint"] = names.get(f["source_viewpoint"], f["source_viewpoint"])
        if f["period"]:
            item["period"] = f["period"]
        out.append(item)
    return out


def forget_scene(conn: sqlite3.Connection, book_id: str, sid: str, *, summaries: bool = True) -> None:
    """Drop what reading this scene left in memory: its facts (and the retractions they made), props and, unless the
    scene is being re-read right now, its summary, reading notes and rejections. Used when a scene is read again and
    when re-cutting the book removes or changes the scene (``scene.segment_book``)."""
    old_ids = [r[0] for r in conn.execute("SELECT id FROM facts WHERE book_id=? AND scene_id=?", (book_id, sid))]
    if old_ids:
        ph = ",".join("?" * len(old_ids))
        conn.execute(f"UPDATE facts SET retracted_by=NULL, retracted_at_ordinal=NULL WHERE retracted_by IN ({ph})", old_ids)
        conn.execute(f"DELETE FROM memory_fts WHERE kind='fact' AND ref_id IN ({ph})", old_ids)
        conn.execute(f"DELETE FROM facts WHERE id IN ({ph})", old_ids)
    conn.execute("DELETE FROM prop_states WHERE book_id=? AND scene_id=?", (book_id, sid))
    from ..memory.address import forget_scene as forget_address

    forget_address(conn, book_id, sid)  # what the scene's lines were read to contain (a re-cut scene too)
    conn.execute("DELETE FROM relation_notes WHERE book_id=? AND scene_id=?", (book_id, sid))
    conn.execute("DELETE FROM group_members WHERE book_id=? AND scene_id=? AND origin='read'", (book_id, sid))
    conn.execute("DELETE FROM memory_fts WHERE kind='summary' AND ref_id=? AND book_id=?", (sid, book_id))
    if summaries:
        conn.execute("DELETE FROM chapter_summaries WHERE book_id=? AND scene_id=?", (book_id, sid))
        conn.execute("DELETE FROM reader_notes WHERE book_id=? AND scene_id=? AND origin='llm'", (book_id, sid))
        conn.execute("DELETE FROM memory_rejections WHERE book_id=? AND scene_id=?", (book_id, sid))


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
    forget_scene(conn, book_id, sid, summaries=False)

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
        obj_ref = f.object_ref if f.object_ref in chars and f.object_ref != f.subject_ref else None
        first = min(ev, key=lambda u: ordinal[u])
        fid = repo.fact_id_for(book_id, sid, n, f.subject_ref + f.predicate + f.object)
        conn.execute(
            "INSERT OR REPLACE INTO facts(id, book_id, subject, predicate, object, fact_type, certainty, fact_json, "
            "evidence_unit_ids_json, evidence_quote, first_revealed_unit_id, first_revealed_ordinal, source_viewpoint, "
            "scene_id, run_id, created_at, object_ref, period) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, book_id, f.subject_ref, f.predicate[:200], f.object[:400], f.fact_type, f.certainty,
             json.dumps(f.model_dump(), ensure_ascii=False), json.dumps(ev), f.evidence_quote[:400], first,
             ordinal[first], f.source_viewpoint, sid, run_id, utcnow(), obj_ref, f.period.strip()[:20] or None))
        subj = chars[f.subject_ref]["canonical_name"] if f.subject_ref in chars else f.subject_ref
        index_memory(conn, book_id, "fact", fid, ordinal[first],
                     f"{f'〔{f.period.strip()}〕' if f.period.strip() else ''}{subj} {f.predicate} {f.object}")
        stats["facts"] += 1
        for old in f.retracts_fact_ids:
            r = conn.execute(f"SELECT * FROM facts WHERE id=? AND {SERIES_SCOPE}", (old, book_id)).fetchone()
            if r is not None and not ((unit_pos(conn, r["first_revealed_unit_id"]) or (0.0, 0))
                                      < (volume_of(conn, book_id), ordinal[first])):
                r = None
            if r is None:
                rej({"retracts": old}, "retracted fact unknown or not earlier")
                continue
            if r["fact_type"] == "character_belief" and not (
                    f.fact_type == "character_belief" and f.source_viewpoint == r["source_viewpoint"]):
                # what someone believed stays part of the story once the truth is out (the misunderstanding happened);
                # only that person's own change of mind ends it
                rej({"retracts": old, "by": f.predicate}, "a belief is ended only by the same person's new belief")
                continue
            conn.execute("UPDATE facts SET retracted_by=?, retracted_at_ordinal=?, revision=revision+1 WHERE id=?",
                         (fid, ordinal[first], old))
            stats["retractions"] += 1

    for g in parsed.glossary_candidates:
        ev = [u for u in g.evidence_unit_ids if u in text_of]
        if not ev or not any(g.source_term in clean(text_of[u]) for u in ev):
            rej(g.model_dump(), "glossary source_term not found verbatim in evidence units")
            continue
        if len(g.source_term.strip()) < 2:  # which words need a fixed rendering is the model's reading
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

    # name forms: the model's links, each with its evidence (ADR-019; ADR-059: the program finds no links itself)
    from ..memory.entities import link

    stats["name_links"] = 0
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
        if nl.term == nl.same_as or len(nl.term.strip()) < 2:
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

    # who calls whom what, with the rendering the translation used (ADR-022): the model's reading, linked to the line
    # its evidence is in (ADR-059: the program checks the words are there; it does not match names itself)
    from ..memory.address import _PRONOUNS as ADDRESS_PRONOUNS
    from ..memory.address import forget_scene as forget_address
    from ..memory.address import set_usage
    from ..memory.address import upsert as upsert_address

    forget_address(conn, book_id, sid)  # a scene read again replaces what its lines were said to contain
    stats["address_terms"] = 0
    lines = conn.execute("SELECT id, unit_id, quote_text, speaker_id_nullable sp, status FROM utterances WHERE "
                         "book_id=? AND scene_id=?", (book_id, sid)).fetchall()
    for at in parsed.address_terms:
        ev = [u for u in at.evidence_unit_ids if u in text_of]
        quote = _norm(at.evidence_quote)
        term = clean(at.term).strip()
        if at.speaker_ref not in chars or at.addressee_ref not in chars:
            rej(at.model_dump(), "address term: speaker/addressee must be known character ids")
            continue
        if not ev or len(quote) < 2 or quote not in _norm("".join(text_of[u] for u in ev)) \
                or _norm(at.term) not in quote or len(term) < 2:
            rej(at.model_dump(), "address term: term/evidence_quote not found verbatim")
            continue
        # the line(s) the evidence is in: a use observed there (counted once per line)
        said_in = [ln for ln in lines if ln["unit_id"] in ev and _norm(at.term) in _norm(ln["quote_text"])
                   and (quote in _norm(ln["quote_text"]) or _norm(ln["quote_text"]) in quote)]
        # a pronoun (「あなた」「お前」) is kept per speaker → addressee, but its rendering (妳／你／您／老公 …)
        # depends on who is addressed: naming decides it, the memory model's guess is not kept
        pronoun = term in PRONOUNS | ADDRESS_PRONOUNS
        first = min(ev, key=lambda u: ordinal[u])
        for ln in said_in or [None]:
            stated = ln is not None and ln["sp"] == at.speaker_ref and ln["status"] in ("explicit", "human_verified")
            rid = upsert_address(conn, book_id, at.speaker_ref, at.addressee_ref, term,
                                 ordinal=ordinal[ln["unit_id"] if ln else first], unit_id=ln["unit_id"] if ln else first,
                                 origin="llm", certainty="explicit" if stated else "inferred",
                                 form_zh=None if pronoun else (at.term_zh.strip()[:24] or None),
                                 quote=at.evidence_quote, note=at.note[:200] or None,
                                 utterance_id=ln["id"] if ln else None)
        row = conn.execute("SELECT * FROM address_terms WHERE id=?", (rid,)).fetchone()
        usage = "self" if at.speaker_ref == at.addressee_ref else at.usage
        # a form the speaker calls the person by anywhere is how they call them: talk about them elsewhere does not
        # take that away
        before = conn.execute("SELECT usage FROM address_usage WHERE speaker_id=? AND addressee_id=? AND form_ja=?",
                              (row["speaker_id"], row["addressee_id"], row["form_ja"])).fetchone()
        elsewhere = conn.execute("SELECT 1 FROM address_observations o JOIN utterances ut ON ut.id=o.utterance_id WHERE "
                                 "o.address_id=? AND COALESCE(ut.scene_id,'')!=? LIMIT 1", (rid, sid)).fetchone()
        if not (before is not None and before[0] == "address" and usage == "mention" and elsewhere):
            set_usage(conn, book_id, row, usage, at.note or "read with the scene", origin="llm")
        stats["address_terms"] += 1

    # wordplay / real-world references a Chinese reader would miss (ADR-026); re-reading replaces the scene's list
    conn.execute("DELETE FROM reader_notes WHERE book_id=? AND scene_id=? AND origin='llm'", (book_id, sid))
    stats["wordplay"] = 0
    for w in parsed.wordplay:
        src = text_of.get(w.unit_id)
        if src is None or not w.term.strip() or clean(w.term).strip() not in clean(src) \
                or (w.evidence_quote and _norm(w.evidence_quote) not in _norm(src)):
            rej(w.model_dump(), "wordplay: term/quote not found verbatim in the unit")
            continue
        conn.execute("INSERT INTO reader_notes(book_id, unit_id, scene_id, term, explanation, note_zh, evidence_quote, "
                     "origin, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (book_id, w.unit_id, sid, clean(w.term).strip()[:60], w.explanation.strip()[:300],
                      w.note_zh.strip()[:120] or None, w.evidence_quote[:300] or None, "llm", utcnow()))
        stats["wordplay"] += 1

    stats["genders"] = apply_genders(conn, book_id, sid, text_of, ordinal, parsed.genders, rej)
    stats["relation_notes"] = 0
    for rel in parsed.relations:  # relations the text states, noted while reading (ADR-045)
        ev = [u for u in rel.evidence_unit_ids if u in text_of]
        if rel.a_ref not in chars or rel.b_ref not in chars or rel.a_ref == rel.b_ref or not rel.relation.strip():
            rej(rel.model_dump(), "relation between unknown characters")
            continue
        if not ev or _norm(rel.evidence_quote) not in _norm("".join(text_of[u] for u in ev)) or len(_norm(rel.evidence_quote)) < 2:
            rej(rel.model_dump(), "relation evidence_quote not found verbatim in evidence units")
            continue
        first = min(ev, key=lambda u: ordinal[u])
        when = rel.period.strip()[:20]
        conn.execute("INSERT INTO relation_notes(book_id, scene_id, a_id, b_id, relation_zh, a_is, b_is, kind, "
                     "evidence_unit_ids_json, evidence_quote, first_ordinal, run_id, created_at, period) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (book_id, sid, rel.a_ref, rel.b_ref, repo.at_period(rel.relation.strip()[:20], when),
                      repo.at_period(rel.a_is.strip()[:12], when) or None, repo.at_period(rel.b_is.strip()[:12], when) or None,
                      rel.kind, json.dumps(ev), rel.evidence_quote[:300], ordinal[first], run_id, utcnow(), when or None))
        stats["relation_notes"] += 1

    stats["group_members"] = 0
    for grp in parsed.groups:  # who a group card is made of, as this scene shows (ADR-046)
        ev = [u for u in grp.evidence_unit_ids if u in text_of]
        members = [m for m in grp.member_refs if m in chars and m != grp.group_ref]
        if grp.group_ref not in chars or not members:
            rej(grp.model_dump(), "group or members not known characters")
            continue
        if not ev or len(_norm(grp.evidence_quote)) < 2 or _norm(grp.evidence_quote) not in _norm("".join(text_of[u] for u in ev)):
            rej(grp.model_dump(), "group evidence_quote not found verbatim in evidence units")
            continue
        for m in members:
            conn.execute("INSERT OR IGNORE INTO group_members(group_id, member_id, book_id, reason, created_at, scene_id, "
                         "origin) VALUES (?,?,?,?,?,?,?)", (grp.group_ref, m, book_id, grp.evidence_quote[:200], utcnow(), sid, "read"))
            stats["group_members"] += 1

    for p in parsed.prop_states:
        if p.name_ja not in "".join(clean(t) for t in text_of.values()):
            rej(p.model_dump(), "prop not mentioned in scene")
            continue
        holder = p.holder_ref if p.holder_ref in chars else None
        conn.execute("INSERT INTO prop_states(book_id, name_ja, holder_character_id, evidence_unit_ids_json, "
                     "horizon_ordinal, scene_id, created_at, period) VALUES (?,?,?,?,?,?,?,?)",
                     (book_id, p.name_ja, holder, json.dumps([u for u in p.evidence_unit_ids if u in text_of]),
                      scene["end_ordinal"], sid, utcnow(), p.period.strip()[:20] or None))
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


def apply_genders(conn: sqlite3.Connection, book_id: str, sid: str, text_of: dict[str, str], ordinal: dict[str, int],
                  genders: list[Any], rej: Any = None) -> int:
    """Gender, only from words in the text that show it (for 妳／你); never guessed from a name."""
    chars = {c["id"] for c in repo.characters(conn, book_id)}
    n = 0
    for gd in genders:
        ev = [u for u in gd.evidence_unit_ids if u in text_of]
        quote = _norm(gd.evidence_quote)
        if gd.character_ref not in chars or not ev or not quote or quote not in _norm("".join(text_of[u] for u in ev)):
            if rej is not None:
                rej(gd.model_dump(), "gender: unknown character or evidence_quote not found verbatim")
            else:
                repo.reject(conn, book_id, sid, "memory_update", gd.model_dump(), "gender: evidence not verbatim")
            continue
        # how sure is the model's reading (ADR-059): the program checked the words are there; an inferred gender
        # goes to the second look
        certainty = "explicit" if gd.certainty == "explicit" else "inferred"
        repo.set_gender(conn, book_id, gd.character_ref, gd.gender, ordinal=min(ordinal[u] for u in ev),
                        quote=gd.evidence_quote, origin="llm", certainty=certainty)
        n += 1
    return n
