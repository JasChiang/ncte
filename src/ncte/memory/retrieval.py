"""Memory retrieval for the context builder + FTS5 search (spec §5.6). All results are horizon-filtered."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..series import INF_POS, pos, scope_clause, volumes
from ..translation.cache import latest_translation
from . import visibility as vis


def search(conn: sqlite3.Connection, book_id: str, term: str, *, horizon: int | None = None,
           limit: int = 10) -> list[dict[str, Any]]:
    """Keyword search over book text and visible memory. Trigram FTS for ≥3 chars, LIKE otherwise."""
    term = term.strip()
    if not term:
        return []
    hz = pos(conn, book_id, horizon) if horizon is not None else INF_POS
    where, ids = scope_clause(conn, book_id)
    if len(term) >= 3:
        q = '"' + term.replace('"', '""') + '"'
        rows = conn.execute(f"SELECT kind, ref_id, ordinal, text, book_id FROM memory_fts WHERE memory_fts MATCH ? "
                            f"AND {where}", (q, *ids)).fetchall()
    else:
        rows = conn.execute(f"SELECT kind, ref_id, ordinal, text, book_id FROM memory_fts WHERE {where} AND text LIKE ?",
                            (*ids, f"%{term}%")).fetchall()
    vols = volumes(conn)
    hits = [(vols.get(r["book_id"], 1.0), int(r["ordinal"]), r) for r in rows]
    hits = sorted((h for h in hits if (h[0], h[1]) < hz), key=lambda h: (h[0], h[1]))[:limit]
    return [{"kind": r["kind"], "ref_id": r["ref_id"], "ordinal": o, "volume": v, "book_id": r["book_id"],
             "text": r["text"]} for v, o, r in hits]


def index_memory(conn: sqlite3.Connection, book_id: str, kind: str, ref_id: str, ordinal: int, text: str) -> None:
    conn.execute("INSERT INTO memory_fts(text, kind, ref_id, book_id, ordinal) VALUES (?,?,?,?,?)",
                 (text, kind, ref_id, book_id, ordinal))


def _cap(items: list[Any], n: int) -> list[Any]:
    return items[-n:] if n >= 0 else items


def memory_context(conn: sqlite3.Connection, book_id: str, *, scene: sqlite3.Row, batch_rows: list[sqlite3.Row],
                   max_evidence: int = 8, chars_budget: int = 2400) -> dict[str, Any]:
    """Characters, scene state, summaries, facts and props visible at this scene."""
    horizon = scene["start_ordinal"]
    upto = batch_rows[-1]["ordinal"]
    scene_text = " ".join(match_text(r["plain_text"]) for r in conn.execute(
        "SELECT plain_text FROM units WHERE book_id=? AND scene_id=? AND ordinal <= ? ORDER BY ordinal",
        (book_id, scene["id"], upto)))
    state = json.loads(scene["state_json"] or "{}")
    participants = {p["character_id"] for p in state.get("participants", [])}
    forms_at = vis.form_positions(conn, book_id)
    views = {c["id"]: v for c in vis.visible_characters(conn, book_id, upto)
             if (v := vis.character_view(conn, book_id, c, upto, forms_at)) is not None}
    chars = [c for c in vis.visible_characters(conn, book_id, upto) if c["id"] in views]
    names = {cid: (v["name_zh"] or v["name_ja"]) for cid, v in views.items()}
    present = []
    for c in chars:
        v = views[c["id"]]
        aliases = [v["name_ja"], *v["aliases_ja"]]
        if c["id"] not in participants and not any(a in scene_text for a in aliases):
            continue
        prof = json.loads(c["speech_profile_json"] or "{}")
        examples = []
        for e in vis.visible_examples(conn, c["id"], horizon, book_id=book_id):
            item = {"ja": e["raw_text"]}
            tr = latest_translation(conn, e["unit_id"], conn.execute(
                "SELECT source_hash FROM units WHERE id=?", (e["unit_id"],)).fetchone()[0])
            if tr is not None:
                item["unit_translation"] = tr["text"]
            examples.append(item)
        card = {"character_id": c["id"], "name_ja": v["name_ja"], "name_zh": v["name_zh"],
                "aliases_ja": v["aliases_ja"], "speech_profile": prof, "verified_examples": examples}
        if v.get("gender"):
            card["gender"] = v["gender"]
        present.append(card)
    # relations among the people of this scene, as the story has shown them so far (ADR-039): how they talk to each other
    ids = {c["character_id"] for c in present}
    for r in conn.execute("SELECT * FROM relations WHERE book_id=? AND since_ordinal<=?", (book_id, horizon)):
        if r["a_id"] in ids and r["b_id"] in ids:
            for card in present:
                if card["character_id"] == r["a_id"]:
                    card.setdefault("relations", []).append({"with": names.get(r["b_id"]), "is": r["a_is"] or r["relation_zh"]})
                elif card["character_id"] == r["b_id"]:
                    card.setdefault("relations", []).append({"with": names.get(r["a_id"]), "is": r["b_is"] or r["relation_zh"]})
    batch_ids = {r["id"] for r in batch_rows}
    utts = []
    for u in conn.execute("SELECT * FROM utterances WHERE book_id=? AND scene_id=? ORDER BY unit_id, quote_index",
                          (book_id, scene["id"])):
        if u["unit_id"] not in batch_ids:
            continue
        sid = u["speaker_id_nullable"]
        utts.append({"unit_id": u["unit_id"], "quote_index": u["quote_index"], "quote": u["quote_text"][:60],
                     "speaker": names.get(sid) if sid else None, "status": u["status"]})
    actions = [a for a in state.get("actions", []) if a.get("unit_id") in batch_ids]
    for a in actions:
        a["subject_candidates"] = [names.get(x, x) for x in a.get("subject_candidates", [])]
        a["object_candidates"] = [names.get(x, x) for x in a.get("object_candidates", [])]
    summaries = vis.visible_summaries(conn, book_id, horizon)
    here = (book_id, scene_chapter(conn, scene))
    same_ch = [s["summary"] for s in summaries if (s["book_id"], s["chapter_ref"]) == here]
    prev_ch = [s["summary"] for s in summaries if (s["book_id"], s["chapter_ref"]) != here]
    present_ids = {p["character_id"] for p in present}
    batch_plain = match_text(" ".join(r["plain_text"] for r in batch_rows))
    facts = select_facts(vis.visible_facts(conn, book_id, horizon), batch_plain, present, names, limit=max_evidence)
    props = [{"item": p["name_ja"], "holder": names.get(p["holder_character_id"]) if p["holder_character_id"] else None}
             for p in vis.visible_props(conn, book_id, horizon) if p["name_ja"] in scene_text]
    mem: dict[str, Any] = {
        "note": "Only information revealed before this scene. 'inferred'/'unknown' items are NOT facts.",
        "previous_chapter_summary": prev_ch[-2:],
        "this_chapter_so_far": _cap(same_ch, 4),
        "facts": _cap(facts, max_evidence),
        "props": props,
    }
    # keep the memory block within a character budget (drop oldest facts/summaries first)
    while len(json.dumps(mem, ensure_ascii=False)) > chars_budget and (mem["facts"] or mem["this_chapter_so_far"]):
        if mem["facts"]:
            mem["facts"].pop(0)
        else:
            mem["this_chapter_so_far"].pop(0)
    from .address import context_items

    batch_text = " ".join(r["plain_text"] for r in batch_rows)
    batch_speakers = {u["speaker_id_nullable"] for u in conn.execute(
        f"SELECT speaker_id_nullable FROM utterances WHERE unit_id IN ({','.join('?' * len(batch_ids))}) AND "
        "speaker_id_nullable IS NOT NULL", list(batch_ids))}
    all_names = {c["id"]: (c["display_name_zh"] or c["canonical_name"]) for c in chars}
    out: dict[str, Any] = {
        "characters": present,
        "scene": {"scene_id": scene["id"], "pov": state.get("pov"), "location": state.get("location"),
                  "utterances": utts, "omitted_arguments": actions,
                  "uncertainties": [x for x in state.get("uncertainties", [])
                                    if set(x.get("unit_ids", [])) & batch_ids]},
        "memory": mem,
    }
    address = context_items(conn, book_id, horizon, batch_text, batch_speakers, all_names, present_ids, upto=upto)
    if address:
        out["address_terms"] = address
    return out


_KEY = re.compile(r"[\u3400-\u9fff\u30a0-\u30ff々ー]")
_IDS = re.compile(r"(?<![0-9A-Za-z_])c_[0-9a-f]{12}(?![0-9a-f])")


def _grams(text: str) -> set[str]:
    """Character bigrams that carry meaning (at least one kanji or katakana): particles and endings left out."""
    t = re.sub(r"\s", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1) if _KEY.search(t[i:i + 2])}


def select_facts(rows: list[Any], batch_text: str, present: list[dict[str, Any]], names: dict[str, str], *,
                 limit: int = 8, min_score: float = 1.0) -> list[dict[str, Any]]:
    """The facts most related to *this batch* (not every fact about whoever is present): overlap between the
    batch's words and each fact's original evidence sentence, plus a bonus when the batch names the fact's
    subject. Each fact carries its Japanese evidence so the model reads the source, not only our paraphrase."""
    bg = _grams(batch_text)
    named = {p["character_id"] for p in present if any(n and n in batch_text for n in [p["name_ja"], *p["aliases_ja"]])}
    present_ids = {p["character_id"] for p in present}
    scored = []
    for i, f in enumerate(rows):
        if not (f["subject"] in present_ids or f["subject"] in batch_text
                or any(p["name_ja"] in (f["subject"] + f["object"]) for p in present)):
            continue
        quote = match_text(f["evidence_quote"] or "")
        qg = _grams(quote)
        overlap = len(bg & qg) / (len(qg) ** .5) if qg else 0.0
        score = overlap + (1.5 if f["subject"] in named else 0) + i / max(1, len(rows)) * .3  # later facts: closer
        if score >= min_score:
            scored.append((score, i, f))
    keep = sorted(sorted(scored, key=lambda x: -x[0])[:limit], key=lambda x: x[1])  # most relevant, story order

    def nm(text: str | None) -> str:
        return _IDS.sub(lambda m: names.get(m.group(0), m.group(0)), text or "")

    return [{"subject": names.get(f["subject"], f["subject"]), "predicate": nm(f["predicate"]), "object": nm(f["object"]),
             "type": f["fact_type"], "certainty": f["certainty"],
             **({"viewpoint": nm(f["source_viewpoint"])} if f["source_viewpoint"] else {}),
             "source": match_text(f["evidence_quote"] or "")[:90]} for _, _, f in keep]


def scene_chapter(conn: sqlite3.Connection, scene: sqlite3.Row) -> str:
    row = conn.execute("SELECT resource_href FROM units WHERE id=?", (scene["start_unit_id"],)).fetchone()
    return row[0] if row else ""
