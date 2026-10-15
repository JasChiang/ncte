"""Relations between characters, from the evidence-backed facts (ADR-039).

The relationship graph used to draw a line between any two characters found in the same scene (a staff member and a
father who never met were joined by one coarse scene). After the read pass the model summarizes relations — 父女,
未婚夫妻, 師生 — citing the facts that show them; the program keeps only relations between known characters backed by
facts about one of the two, and shows each from where its earliest fact is revealed (no spoilers).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..llm.schemas import RelationsOutput
from ..memory.db import Transaction, utcnow
from ..series import SERIES_SCOPE

MAX_CHARACTERS = 30
MAX_FACTS = 400


def payload(conn: sqlite3.Connection, book_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(request, lookup) — the lookup maps the short refs back (``_chars``: ref → id, ``_facts``: ref → row)."""
    scenes: dict[str, int] = {}
    for r in conn.execute("SELECT speaker_id_nullable sp, COUNT(DISTINCT scene_id) n FROM utterances WHERE book_id=? AND "
                          "sp IS NOT NULL GROUP BY sp", (book_id,)):
        scenes[r["sp"]] = r["n"]
    chars = conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book_id,)).fetchall()
    chars = sorted(chars, key=lambda c: -scenes.get(c["id"], 0))[:MAX_CHARACTERS]
    ref = {c["id"]: f"c{i + 1}" for i, c in enumerate(chars)}
    name = {c["id"]: c["display_name_zh"] or c["canonical_name"] for c in chars}
    facts: list[dict[str, Any]] = []
    lookup_f: dict[str, sqlite3.Row] = {}
    for f in conn.execute("SELECT * FROM facts WHERE book_id=? AND retracted_by IS NULL ORDER BY first_revealed_ordinal",
                          (book_id,)):
        about = [cid for cid in ref if cid == f["subject"] or name[cid] in (f["object"] or "")
                 or name[cid] in (f["predicate"] or "")]
        if not about:
            continue
        fr = f"f{len(facts) + 1}"
        lookup_f[fr] = f
        subj = name.get(f["subject"], f["subject"])
        facts.append({"ref": fr, "about": [ref[c] for c in about], "text": f"{subj} {f['predicate']} {f['object']}".strip()})
        if len(facts) >= MAX_FACTS:
            break
    address = [{"from": ref[a["speaker_id"]], "to": ref[a["addressee_id"]], "form": a["form_ja"]}
               for a in conn.execute("SELECT * FROM address_terms WHERE book_id=? AND speaker_id!=addressee_id ORDER BY "
                                     "uses DESC LIMIT 80", (book_id,))
               if a["speaker_id"] in ref and a["addressee_id"] in ref]
    req = {"task": "relations", "characters": [{"ref": ref[c["id"]], "name": name[c["id"]]} for c in chars],
           "facts": facts, "address": address}
    return req, {"_chars": {v: k for k, v in ref.items()}, "_facts": lookup_f}


def apply(conn: sqlite3.Connection, book_id: str, parsed: RelationsOutput, lookup: dict[str, Any]) -> int:
    chars, facts = lookup["_chars"], lookup["_facts"]
    n = 0
    with Transaction(conn):
        conn.execute("DELETE FROM relations WHERE book_id=?", (book_id,))
        for r in parsed.relations:
            a, b = chars.get(r.a), chars.get(r.b)
            cited = [facts[f] for f in r.facts if f in facts]
            if not a or not b or a == b or not cited or not r.relation.strip():
                continue
            since = min(f["first_revealed_ordinal"] for f in cited)
            conn.execute("INSERT OR REPLACE INTO relations(book_id, a_id, b_id, relation_zh, a_is, b_is, since_ordinal, "
                         "fact_ids_json, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (book_id, a, b, r.relation.strip()[:20], r.a_is.strip()[:12] or None, r.b_is.strip()[:12] or None,
                          since, json.dumps([f["id"] for f in cited]), r.reason[:200], utcnow()))
            n += 1
    return n


def visible(conn: sqlite3.Connection, book_id: str, upto: int | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM relations WHERE book_id=?"
    params: list[Any] = [book_id]
    if upto is not None:
        q += " AND since_ordinal<=?"
        params.append(upto)
    return conn.execute(q + " ORDER BY since_ordinal", params).fetchall()
