"""Relations between characters (ADR-039, ADR-045).

A translator notes 「お父様」, 婚約者, 先生と生徒 while reading and, after the book, looks at each pair of people with
all those notes together. The engine does the same: the read pass notes relations the text states (``relation_notes``,
by character id), and here, after reading, each pair with enough material — those notes, the facts about both (by id,
or naming either by full name, short name or alias), how they address each other, how often they speak in the same
scene — is put to the model, a few pairs per request. The model may cite only that pair's own evidence; the program
shows each relation from its earliest evidence and, when the model says a later relation replaces an earlier one
(宿敵 → 未婚夫妻), ends the earlier one there. Pairs the material says should be related but came back without a relation
are asked once more. Relations the model made are replaced by a new run; those set by a person stay.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from typing import Any

from ..llm.schemas import RelationsOutput  # noqa: F401  (re-exported for callers)
from ..memory.db import Transaction, utcnow
from ..series import SERIES_SCOPE

MAX_PAIRS = 40
PAIRS_PER_REQUEST = 8
MAX_EVIDENCE = 10  # per kind and pair


def _names(c: sqlite3.Row) -> set[str]:
    out = {c["canonical_name"], c["display_name_zh"] or ""}
    zh = c["display_name_zh"] or ""
    if "·" in zh:
        out.add(zh.split("·")[0])
    out.update(json.loads(c["aliases_json"] or "[]"))
    return {n for n in out if n and len(n) >= 2}


def candidate_pairs(conn: sqlite3.Connection, book_id: str) -> tuple[list[dict[str, Any]], dict[str, sqlite3.Row]]:
    chars = {c["id"]: c for c in conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book_id,))}
    names = {cid: _names(c) for cid, c in chars.items()}
    pairs: dict[tuple[str, str], dict[str, Any]] = {}

    def pair(a: str, b: str) -> dict[str, Any]:
        k = (a, b) if a < b else (b, a)
        return pairs.setdefault(k, {"a": k[0], "b": k[1], "notes": [], "facts": [], "address": [], "together": 0})

    for n in conn.execute("SELECT * FROM relation_notes WHERE book_id=? ORDER BY first_ordinal", (book_id,)):
        if n["a_id"] in chars and n["b_id"] in chars:
            pair(n["a_id"], n["b_id"])["notes"].append(n)
    for f in conn.execute("SELECT * FROM facts WHERE book_id=? AND retracted_by IS NULL ORDER BY first_revealed_ordinal",
                          (book_id,)):
        about = {x for x in (f["subject"], f["object_ref"]) if x in chars}
        text = f"{f['predicate']} {f['object']}"
        about |= {cid for cid, ns in names.items() if any(n in text for n in ns)}
        for a, b in itertools.combinations(sorted(about), 2):
            pair(a, b)["facts"].append(f)
    for t in conn.execute("SELECT * FROM address_terms WHERE book_id=? AND speaker_id!=addressee_id", (book_id,)):
        if t["speaker_id"] in chars and t["addressee_id"] in chars:
            pair(t["speaker_id"], t["addressee_id"])["address"].append(t)
    speaking: dict[str, set[str]] = {}
    for u in conn.execute("SELECT scene_id, speaker_id_nullable sp FROM utterances WHERE book_id=? AND sp IS NOT NULL",
                          (book_id,)):
        speaking.setdefault(u["scene_id"], set()).add(u["sp"])
    for who in speaking.values():
        for a, b in itertools.combinations(sorted(w for w in who if w in chars), 2):
            if (a, b) in pairs:
                pairs[(a, b)]["together"] += 1
            elif len(who) <= 6:
                pair(a, b)["together"] += 1
    out = []
    for p in pairs.values():
        uses = sum(int(t["uses"] or 0) for t in p["address"])
        p["weight"] = 5 * len(p["notes"]) + 2 * len(p["facts"]) + min(uses, 5) + p["together"] / 2
        p["should"] = bool(p["notes"]) or (bool(p["address"]) and p["together"] >= 5)
        if p["notes"] or p["facts"] or (p["address"] and p["together"] >= 3):
            out.append(p)
    out.sort(key=lambda p: -p["weight"])
    return out[:MAX_PAIRS], chars


def requests(pairs: list[dict[str, Any]], chars: dict[str, sqlite3.Row], *, recheck: bool = False
             ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(request, lookup) per group of pairs; the lookup maps refs back (characters, evidence → (kind, id, ordinal))."""
    out = []
    for i in range(0, len(pairs), PAIRS_PER_REQUEST):
        group = pairs[i:i + PAIRS_PER_REQUEST]
        cref: dict[str, str] = {}
        ev: dict[str, tuple[str, Any, int]] = {}
        allowed: dict[str, set[str]] = {}
        items = []
        for k, p in enumerate(group, 1):
            for cid in (p["a"], p["b"]):
                cref.setdefault(cid, f"c{len(cref) + 1}")
            pid = f"p{k}"
            allowed[pid] = set()
            notes, facts = [], []
            for n in p["notes"][:MAX_EVIDENCE]:
                r = f"n{len(ev) + 1}"
                ev[r] = ("note", n["id"], n["first_ordinal"])
                allowed[pid].add(r)
                who_a = cref[n["a_id"]]
                notes.append({"ref": r, "at": n["first_ordinal"], "text": f"{who_a} 是 {cref[n['b_id']]} 的{n['a_is'] or ''}"
                              f"（{n['relation_zh']}，{n['kind']}）", "quote": n["evidence_quote"]})
            for f in p["facts"][:MAX_EVIDENCE]:
                r = f"f{len(ev) + 1}"
                ev[r] = ("fact", f["id"], f["first_revealed_ordinal"])
                allowed[pid].add(r)
                subj = cref.get(f["subject"]) or (chars[f["subject"]]["display_name_zh"] if f["subject"] in chars else f["subject"])
                facts.append({"ref": r, "at": f["first_revealed_ordinal"], "text": f"{subj} {f['predicate']} {f['object']}",
                              "quote": f["evidence_quote"]})
            items.append({"pair": pid, "a": cref[p["a"]], "b": cref[p["b"]], "notes": notes, "facts": facts,
                          "address": [{"from": cref[t["speaker_id"]], "to": cref[t["addressee_id"]], "form": t["form_ja"],
                                       "uses": t["uses"]} for t in p["address"][:MAX_EVIDENCE]],
                          "scenes_together": p["together"]})
        req: dict[str, Any] = {"task": "relations",
                               "characters": [{"ref": r, "name": chars[c]["display_name_zh"] or chars[c]["canonical_name"]}
                                              for c, r in cref.items()], "pairs": items}
        if recheck:
            req["recheck"] = True
        out.append((req, {"_chars": {r: c for c, r in cref.items()}, "_ev": ev, "_allowed": allowed,
                          "_pairs": {f"p{k}": (p["a"], p["b"]) for k, p in enumerate(group, 1)}}))
    return out


def unanswered(pairs: list[dict[str, Any]], results: list[tuple[RelationsOutput, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Pairs whose material says they are related (noted while reading, or talking often) that came back empty."""
    got = set()
    for parsed, lk in results:
        for r in parsed.relations:
            a, b = lk["_chars"].get(r.a), lk["_chars"].get(r.b)
            if a and b:
                got.add((a, b) if a < b else (b, a))
    return [p for p in pairs if p["should"] and (p["a"], p["b"]) not in got]


def apply(conn: sqlite3.Connection, book_id: str, results: list[tuple[RelationsOutput, dict[str, Any]]]) -> int:
    """Replace the model's relations of the book (a person's stay), each shown from its earliest cited evidence."""
    n = 0
    with Transaction(conn):
        manual = {(r["a_id"], r["b_id"], r["relation_zh"]) for r in conn.execute(
            "SELECT * FROM relations WHERE book_id=? AND origin!='llm'", (book_id,))}
        conn.execute("DELETE FROM relations WHERE book_id=? AND origin='llm'", (book_id,))
        placed: list[tuple[str, str, str, int, str | None]] = []
        for parsed, lk in results:
            chars, ev = lk["_chars"], lk["_ev"]
            allowed = set().union(*lk["_allowed"].values()) if lk["_allowed"] else set()
            for r in parsed.relations:
                a, b = chars.get(r.a), chars.get(r.b)
                cited = [ev[x] for x in r.facts if x in ev and x in allowed]
                if not a or not b or a == b or not cited or not r.relation.strip():
                    continue
                pair_ok = any(set(lk["_pairs"][p]) == {a, b} and set(r.facts) & lk["_allowed"][p] for p in lk["_pairs"])
                if not pair_ok:  # evidence of another pair: not this pair's relation
                    continue
                rel = r.relation.strip()[:20]
                if (a, b, rel) in manual or (b, a, rel) in manual:
                    continue
                since = min(o for _, _, o in cited)
                conn.execute(
                    "INSERT OR REPLACE INTO relations(book_id, a_id, b_id, relation_zh, a_is, b_is, since_ordinal, "
                    "fact_ids_json, reason, created_at, kind, certainty, origin, evidence_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (book_id, a, b, rel, r.a_is.strip()[:12] or None, r.b_is.strip()[:12] or None, since,
                     json.dumps([i for k, i, _ in cited if k == "fact"]), r.reason[:200], utcnow(), r.kind, r.certainty,
                     "llm", json.dumps([{"kind": k, "id": i, "at": o} for k, i, o in cited])))
                placed.append((a, b, rel, since, (r.replaces or "").strip() or None))
                n += 1
        for a, b, rel, since, replaces in placed:  # 宿敵 → 未婚夫妻: the earlier relation ends where the later begins
            if replaces:
                conn.execute("UPDATE relations SET until_ordinal=? WHERE book_id=? AND relation_zh=? AND since_ordinal<? "
                             "AND ((a_id=? AND b_id=?) OR (a_id=? AND b_id=?))",
                             (since, book_id, replaces, since, a, b, b, a))
    return n


def visible(conn: sqlite3.Connection, book_id: str, upto: int | None = None) -> list[sqlite3.Row]:
    """Relations known at ``upto`` (begun by then, not yet replaced)."""
    q = "SELECT * FROM relations WHERE book_id=?"
    params: list[Any] = [book_id]
    if upto is not None:
        q += " AND since_ordinal<=? AND (until_ordinal IS NULL OR until_ordinal>?)"
        params += [upto, upto]
    return conn.execute(q + " ORDER BY since_ordinal", params).fetchall()
