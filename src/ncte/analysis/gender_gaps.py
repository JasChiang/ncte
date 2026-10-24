"""Characters still without a gender after reading, asked once more with the whole book in view.

Reading records a gender from what one scene shows; a person whose gender the book shows only by description, or in
a scene where the reading did not say it, stays without one, and the translation then writes 你 to her. After the
second look, each character who matters (enough lines spoken or enough mentions) and still has no gender is put to the
model with source sentences spread over the book where they speak, are named or are spoken to, each with a code; it
says female, male or unknown, how sure, and which sentences it rests on. Finding the sentences is plain string and
record matching; what they show is the model's call (ADR-059). An answer is kept only when it cites sentences it was
given for that person, and is stored like any other gender read from the text (origin ``llm``, the certainty as
given, visible from the earliest sentence cited); ``unknown`` stores nothing.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..llm.schemas import GenderGapsOutput
from ..memory import repository as repo
from ..memory.db import Transaction
from .relations import _around, _forms, read_text
from .terms import _spread

MIN_LINES = 3  # lines spoken …
MIN_MENTIONS = 8  # … or paragraphs naming the character: who matters enough to ask about
SENTENCES = 8  # per character, spread over the book
PER_REQUEST = 10  # characters per request
TEXT_CHARS = 200


def candidates(conn: sqlite3.Connection, book_id: str) -> list[dict[str, Any]]:
    """The characters of this volume without a gender who speak or are named often enough, each with the paragraphs
    (story order) where they speak, are named or are spoken to."""
    paras = {r["id"]: r for r in read_text(conn, book_id)}
    every = repo.characters(conn, book_id)
    # a word other cards have too (「母親」 of three people) finds no sentence for any of them: those are found by id,
    # where the reading gave the person a line, said a line to them or saw them present (ADR-103)
    owners: dict[str, set[str]] = {}
    for x in every:
        for f in _forms(x):
            owners.setdefault(f, set()).add(x["id"])
    present: dict[str, set[str]] = {}
    referred: dict[str, set[str]] = {}  # paragraphs where the reading or the memory step refers to the card by id
    for (state,) in conn.execute("SELECT state_json FROM scenes WHERE book_id=?", (book_id,)):
        st = json.loads(state or "{}")
        for p in st.get("participants") or []:
            for u in p.get("evidence_unit_ids") or []:
                present.setdefault(str(p.get("character_id")), set()).add(u)
        for a in st.get("actions") or []:  # who does or undergoes an action the source leaves out
            for x in [*(a.get("subject_candidates") or []), *(a.get("object_candidates") or [])]:
                if a.get("unit_id"):
                    referred.setdefault(str(x), set()).add(a["unit_id"])
    for who, ev in [*((r[0], r[2]) for r in conn.execute(  # facts about the card, and relations it is in
            "SELECT subject, object_ref, evidence_unit_ids_json FROM facts WHERE book_id=? AND retracted_by IS NULL",
            (book_id,))), *((r[1], r[2]) for r in conn.execute(
            "SELECT subject, object_ref, evidence_unit_ids_json FROM facts WHERE book_id=? AND retracted_by IS NULL",
            (book_id,))), *((r[0], r[2]) for r in conn.execute(
            "SELECT a_id, b_id, evidence_unit_ids_json FROM relation_notes WHERE book_id=?", (book_id,))),
            *((r[1], r[2]) for r in conn.execute(
                "SELECT a_id, b_id, evidence_unit_ids_json FROM relation_notes WHERE book_id=?", (book_id,)))]:
        if who:
            referred.setdefault(str(who), set()).update(json.loads(ev or "[]"))
    for r in conn.execute("SELECT speaker_id, addressee_id, first_unit_id FROM address_terms WHERE book_id=?",
                          (book_id,)):  # who calls (or speaks of) whom what: both are in that paragraph
        if r[2]:
            for who in (r[0], r[1]):
                referred.setdefault(str(who), set()).add(r[2])
    genders = {g["character_id"]: g for g in conn.execute(
        "SELECT g.* FROM character_gender g JOIN characters c ON c.id=g.character_id WHERE c.book_id=?", (book_id,))}
    rows = conn.execute("SELECT * FROM characters WHERE book_id=? AND is_group=0 ORDER BY known_from_ordinal",
                        (book_id,)).fetchall()
    out = []
    for c in rows:
        # a gender read only later than the card is first there leaves the stretch before it without one: asked with
        # sentences up to where it was read (a person's choice, or one known from the start, is no gap)
        g = genders.get(c["id"])
        if g is not None and (g["origin"] == "human" or g["book_id"] != book_id
                              or g["ordinal"] <= c["known_from_ordinal"]):
            continue
        until = g["ordinal"] if g is not None else None

        def ok(uid: str, lo: int = c["known_from_ordinal"], hi: int | None = until) -> bool:  # this stretch only
            o = paras[uid]["ordinal"]
            return o >= lo and (hi is None or o < hi)

        forms = [f for f in _forms(c) if owners.get(f, set()) <= {c["id"]}]
        how: dict[str, set[str]] = {}
        lines = 0
        for u in conn.execute("SELECT unit_id FROM utterances WHERE book_id=? AND speaker_id_nullable=?", (book_id, c["id"])):
            if u[0] in paras and ok(u[0]):
                lines += 1
                how.setdefault(u[0], set()).add("speaks")
        for u in conn.execute("SELECT unit_id, addressee_ids_json FROM utterances WHERE book_id=? AND instr("
                              "addressee_ids_json, ?) > 0", (book_id, c["id"])):
            if c["id"] in json.loads(u[1] or "[]") and u[0] in paras and ok(u[0]):
                how.setdefault(u[0], set()).add("addressed")
        for u in present.get(c["id"], set()):
            if u in paras and ok(u):
                how.setdefault(u, set()).add("present")
        for u in referred.get(c["id"], set()):
            if u in paras and ok(u):
                how.setdefault(u, set()).add("referred")
        mentions = 0
        for uid, r in paras.items():
            if forms and ok(uid) and any(f in match_text(r["plain_text"]) for f in forms):
                mentions += 1
                how.setdefault(uid, set()).add("named")
        forms = forms or _forms(c)  # (shown with each sentence to mark where the person is; not used to find them)
        if lines < MIN_LINES and mentions < MIN_MENTIONS:
            continue
        hits = sorted(how, key=lambda u: paras[u]["ordinal"])
        out.append({"id": c["id"], "name": c["canonical_name"], "zh": c["display_name_zh"] or "",
                    "aliases": [f for f in forms if f != c["canonical_name"]], "lines": lines, "mentions": mentions,
                    "sentences": [(paras[u]["ordinal"], sorted(how[u]),
                                   _around(match_text(paras[u]["plain_text"]), {"": forms}, TEXT_CHARS))
                                  for u in _spread(hits, SENTENCES)]})
    return out


def requests(conn: sqlite3.Connection, book_id: str, only: set[str] | None = None
             ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(payload, lookup) per PER_REQUEST characters. Codes are unique over the whole step: ``g`` a character, ``s`` a
    sentence; the lookup maps each back to its character, and a sentence to where it is and what it says. ``only``:
    these characters (the ones a failed request left unasked, ADR-102)."""
    out = []
    chosen = [c for c in candidates(conn, book_id) if only is None or c["id"] in only]
    n = 0
    for i in range(0, len(chosen), PER_REQUEST):
        items, chars, ev = [], {}, {}
        for k, c in enumerate(chosen[i:i + PER_REQUEST], i + 1):
            ref = f"g{k}"
            chars[ref] = c["id"]
            sentences = []
            for at, how, text in c["sentences"]:
                n += 1
                ev[f"s{n}"] = (c["id"], at, text)
                sentences.append({"ref": f"s{n}", "how": how, "text": text})
            items.append({"ref": ref, "name": c["name"], **({"zh": c["zh"]} if c["zh"] else {}),
                          **({"aliases": c["aliases"]} if c["aliases"] else {}), "lines": c["lines"],
                          "mentions": c["mentions"], "sentences": sentences})
        out.append(({"task": "gender_gaps", "characters": items}, {"_chars": chars, "_ev": ev}))
    return out


def apply(conn: sqlite3.Connection, book_id: str, results: list[tuple[GenderGapsOutput, dict[str, Any]]]
          ) -> dict[str, int]:
    """Keep each female/male answer that cites at least one sentence, all of them given for that character."""
    stats = {"genders": 0, "unknown": 0, "rejected": 0}
    with Transaction(conn):
        for parsed, lk in results:
            for a in parsed.characters:
                cid = lk["_chars"].get(a.ref.strip())
                if cid is None:
                    stats["rejected"] += 1
                    continue
                if a.gender == "unknown":
                    stats["unknown"] += 1
                    continue
                cited = [lk["_ev"].get(x.strip()) for x in a.evidence]
                if not cited or any(x is None or x[0] != cid for x in cited):
                    stats["rejected"] += 1  # cites nothing, or what it was not given for this person: not kept
                    continue
                first = min((x for x in cited if x is not None), key=lambda x: x[1])
                repo.set_gender(conn, book_id, cid, a.gender, ordinal=first[1], quote=first[2], origin="llm",
                                certainty=a.certainty, basis=a.basis)
                stats["genders"] += 1
    return stats
