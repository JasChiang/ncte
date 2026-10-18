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
from ..memory.address import calls_by
from ..memory.db import Transaction, utcnow
from ..memory.repository import at_period, qualified_fact
from ..series import SERIES_SCOPE

MAX_PAIRS = 40
PAIRS_PER_REQUEST = 8
MAX_EVIDENCE = 10  # per kind and pair
MAX_PASSAGES = 3  # source paragraphs naming a member's group together with the other person
PAIR_PASSAGES = 6  # source passages where the two appear together (talked about or named together first)


def _names(c: sqlite3.Row, proper: set[str]) -> set[str]:
    """The names a fact's text may name this character by: only proper names (in the glossary as a character name,
    or the Chinese of one) — a character called 「母親」 must not take every fact that mentions somebody's mother."""
    zh = c["display_name_zh"] or ""
    out = {c["canonical_name"], zh, *json.loads(c["aliases_json"] or "[]")}
    if "·" in zh:
        out.add(zh.split("·")[0])
    return {n for n in out if n and len(n) >= 2 and n in proper}


def candidate_pairs(conn: sqlite3.Connection, book_id: str) -> tuple[list[dict[str, Any]], dict[str, sqlite3.Row]]:
    chars = {c["id"]: c for c in conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book_id,))}
    proper: set[str] = set()
    for g in conn.execute(f"SELECT source_term, target_term FROM glossary WHERE {SERIES_SCOPE} AND category='character'",
                          (book_id,)):
        proper |= {g[0], g[1], *(g[1].split("·")[:1] if "·" in g[1] else [])}
    names = {cid: _names(c, proper) for cid, c in chars.items()}
    pairs: dict[tuple[str, str], dict[str, Any]] = {}

    def pair(a: str, b: str) -> dict[str, Any]:
        k = (a, b) if a < b else (b, a)
        return pairs.setdefault(k, {"a": k[0], "b": k[1], "notes": [], "facts": [], "address": [], "together": 0, "via": {},
                                    "passages": []})

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
        if t["speaker_id"] in chars and t["addressee_id"] in chars and calls_by(conn, t):
            pair(t["speaker_id"], t["addressee_id"])["address"].append(t)
    # a relation stated about a group (加爾德—跟班兩人：主從) is material for each member's own pair too; the model
    # decides whether it holds for that person (ADR-046)
    members: dict[str, list[str]] = {}
    for g in conn.execute("SELECT group_id, member_id FROM group_members WHERE book_id=?", (book_id,)):
        if g[0] in chars and g[1] in chars:
            members.setdefault(g[0], []).append(g[1])
    for (a, b), p in list(pairs.items()):
        for grp, other in ((a, b), (b, a)):
            for m in members.get(grp, []):
                if m == other:
                    continue
                q = pair(m, other)
                label = chars[grp]["display_name_zh"] or chars[grp]["canonical_name"]
                for kind in ("notes", "facts"):
                    for row in p[kind]:
                        if row not in q[kind]:
                            q[kind].append(row)
                            q["via"][(kind, row["id"])] = label
    _group_passages(conn, book_id, chars, members, pairs, pair)
    _pair_passages(conn, book_id, chars, pairs)
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
        p["weight"] = 5 * len(p["notes"]) + 2 * len(p["facts"]) + min(len(p["passages"]), 4) + min(uses, 5) + p["together"] / 2
        # asked once more when it came back empty: notes, or how they address each other plus anything else about the
        # two (「義父さん」 and one fact came back empty in two runs of three)
        p["should"] = bool(p["notes"]) or (bool(p["address"]) and (p["together"] >= 3 or bool(p["facts"]) or bool(p["passages"])))
        if p["notes"] or p["facts"] or p["passages"] or (p["address"] and p["together"] >= 3):
            out.append(p)
    out.sort(key=lambda p: -p["weight"])
    return out[:MAX_PAIRS], chars


def read_text(conn: sqlite3.Connection, book_id: str) -> list[sqlite3.Row]:
    """The story paragraphs read so far, in order: only scenes the read pass has been through. Book matter is never
    read (an afterword's 「二人が夫婦となり」 is the author's summary, not the story, which says 「夫婦じゃなくて婚約者」),
    and a sample run (--limit-units) does not reach past what it read."""
    return conn.execute("SELECT u.id, u.ordinal, u.plain_text FROM units u JOIN scenes s ON s.book_id=u.book_id AND "
                        "s.id=u.scene_id WHERE u.book_id=? AND u.kind='text' AND s.version>0 ORDER BY u.ordinal",
                        (book_id,)).fetchall()


def _forms(c: sqlite3.Row) -> list[str]:
    return [f for f in {c["canonical_name"], *json.loads(c["aliases_json"] or "[]")} if f and len(f) >= 2]


def _group_passages(conn: sqlite3.Connection, book_id: str, chars: dict[str, sqlite3.Row], members: dict[str, list[str]],
                    pairs: dict[tuple[str, str], dict[str, Any]], pair: Any) -> None:
    """A group often has no notes of its own (「ガルドは取り巻きたちを連れて」 is neither a fact nor a note): the
    paragraphs that name the group together with someone a member is paired with go to that member's pair, earliest
    first, for the model to read (ADR-046). Finding them is plain name matching; what they mean is the model's call."""
    if not members:
        return
    from .dialogue import clean

    units = [(u["id"], u["ordinal"], clean(u["plain_text"])) for u in read_text(conn, book_id)]
    for grp, ms in members.items():
        gforms = _forms(chars[grp])
        others = {x for (a, b) in list(pairs) for x in (a, b) if {a, b} & {grp, *ms}} - {grp, *ms}
        for other in others:
            oforms = _forms(chars[other])
            hits = [u for u in units if any(f in u[2] for f in gforms) and any(f in u[2] for f in oforms)][:MAX_PASSAGES]
            if not hits:
                continue
            label = chars[grp]["display_name_zh"] or chars[grp]["canonical_name"]
            for m in ms:
                q = pair(m, other)
                q["passages"] += [{"id": h[0], "at": h[1], "text": h[2][:200], "via": label} for h in hits]


def _pair_passages(conn: sqlite3.Connection, book_id: str, chars: dict[str, sqlite3.Row],
                   pairs: dict[tuple[str, str], dict[str, Any]]) -> None:
    """Reading notes a relation once, and misses some: a relation that needs a second step (「孫なし確定」 said of a
    brother and the man he lives with) was noted in one reading of seven. So each pair also gets the source passages
    where the two appear together — one named in a paragraph, the other named or speaking within a paragraph of it —
    with who says each line. Passages where one is talked about in a conversation the other takes part in, or both are
    named in one paragraph, come first, spread over the book. Finding them is name matching; what they show is the
    model's call (ADR-048)."""
    from .dialogue import clean

    rows = read_text(conn, book_id)
    speakers: dict[str, set[str]] = {}
    for u in conn.execute("SELECT unit_id, speaker_id_nullable FROM utterances WHERE book_id=? AND speaker_id_nullable "
                          "IS NOT NULL", (book_id,)):
        speakers.setdefault(u[0], set()).add(u[1])
    forms = {cid: _forms(c) for cid, c in chars.items()}
    text = [clean(r["plain_text"]) for r in rows]
    named = [{cid for cid, fs in forms.items() if any(f in t for f in fs)} for t in text]
    present = [named[i] | speakers.get(r["id"], set()) for i, r in enumerate(rows)]
    hits: dict[tuple[str, str], dict[int, int]] = {}  # pair → paragraph → strength
    for i in range(len(rows)):
        window = range(max(0, i - 1), min(len(rows), i + 2))
        near = set().union(*(present[j] for j in window))
        talking = set().union(*(speakers.get(rows[j]["id"], set()) for j in window))
        for a in named[i]:
            for b in near - {a}:
                k = (a, b) if a < b else (b, a)
                if k not in pairs:
                    continue
                strong = b in named[i] or b in talking  # named together, or talked about with the other in it
                got = hits.setdefault(k, {})
                if any(abs(i - j) <= 2 for j in got):  # the same stretch once
                    j = next(j for j in got if abs(i - j) <= 2)
                    got[j] = max(got[j], 2 if strong else 1)
                    continue
                got[i] = 2 if strong else 1

    def label(i: int) -> str:
        who = speakers.get(rows[i]["id"], set())
        name = chars[next(iter(who))]["display_name_zh"] or chars[next(iter(who))]["canonical_name"] if len(who) == 1 else ""
        return (f"{name}：" if name else "") + _around(text[i], forms, 140)

    def spread(idx: list[int]) -> list[int]:  # first, last, second, second last …: the whole book, not one end
        out, lo, hi = [], 0, len(idx) - 1
        while lo <= hi:
            out.append(idx[lo])
            if hi != lo:
                out.append(idx[hi])
            lo, hi = lo + 1, hi - 1
        return out

    for k, got in hits.items():
        order = spread(sorted(i for i, s in got.items() if s == 2)) + spread(sorted(i for i, s in got.items() if s == 1))
        for i in sorted(order[:PAIR_PASSAGES]):
            snippet = " / ".join(label(j) for j in range(max(0, i - 1), min(len(rows), i + 2)))
            pairs[k]["passages"].append({"id": rows[i]["id"], "at": rows[i]["ordinal"], "text": snippet, "via": None})


def _around(t: str, forms: dict[str, list[str]], n: int) -> str:
    """A long paragraph cut to ``n`` characters around the first name in it (the part the passage is there for)."""
    if len(t) <= n:
        return t
    at = min((t.find(f) for fs in forms.values() for f in fs if f in t), default=0)
    start = max(0, min(at - n // 3, len(t) - n))
    return ("…" if start else "") + t[start:start + n] + ("…" if start + n < len(t) else "")


def requests(pairs: list[dict[str, Any]], chars: dict[str, sqlite3.Row], *, recheck: bool = False,
             basics: dict[str, dict[str, Any]] | None = None
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
            for n in _distinct(p["notes"])[:MAX_EVIDENCE]:
                r = f"n{len(ev) + 1}"
                ev[r] = ("note", n["id"], n["first_ordinal"])
                allowed[pid].add(r)
                who_a = cref.get(n["a_id"]) or _label(chars, n["a_id"])
                via = p["via"].get(("notes", n["id"]))
                notes.append({"ref": r, "at": n["first_ordinal"], "text": (f"〔關於群體「{via}」〕" if via else "")
                              + f"{who_a} 是 {cref.get(n['b_id']) or _label(chars, n['b_id'])} 的{n['a_is'] or ''}"
                              f"（{n['relation_zh']}，{n['kind']}）", "quote": n["evidence_quote"]})
            for f in p["facts"][:MAX_EVIDENCE]:
                r = f"f{len(ev) + 1}"
                ev[r] = ("fact", f["id"], f["first_revealed_ordinal"])
                allowed[pid].add(r)
                subj = cref.get(f["subject"]) or (chars[f["subject"]]["display_name_zh"] if f["subject"] in chars else f["subject"])
                via = p["via"].get(("facts", f["id"]))
                facts.append({"ref": r, "at": f["first_revealed_ordinal"], "quote": f["evidence_quote"],
                              "text": (f"〔關於群體「{via}」〕" if via else "")
                              + qualified_fact(f, f"{subj} {f['predicate']} {f['object']}",
                                               {**{k: _label(chars, k) for k in chars}, **cref})})
            passages = []
            for g in p.get("passages", [])[:MAX_PASSAGES + PAIR_PASSAGES]:  # the group's first, then the pair's
                r = f"u{len(ev) + 1}"
                ev[r] = ("passage", g["id"], g["at"])
                allowed[pid].add(r)
                passages.append({"ref": r, "at": g["at"],
                                 "text": (f"〔關於群體「{g['via']}」〕" if g["via"] else "") + g["text"]})
            items.append({"pair": pid, "a": cref[p["a"]], "b": cref[p["b"]], "notes": notes, "facts": facts,
                          **({"passages": passages} if passages else {}),
                          "address": [{"from": cref[t["speaker_id"]], "to": cref[t["addressee_id"]], "form": t["form_ja"],
                                       "uses": t["uses"]} for t in p["address"][:MAX_EVIDENCE]],
                          "scenes_together": p["together"]})
        req: dict[str, Any] = {"task": "relations",
                               "characters": [{"ref": r, "name": chars[c]["display_name_zh"] or chars[c]["canonical_name"],
                                               **(basics or {}).get(c, {})} for c, r in cref.items()], "pairs": items}
        if recheck:
            req["recheck"] = True
        out.append((req, {"_chars": {r: c for c, r in cref.items()}, "_ev": ev, "_allowed": allowed,
                          "_pairs": {f"p{k}": (p["a"], p["b"]) for k, p in enumerate(group, 1)}}))
    return out


def _distinct(notes: list[Any]) -> list[Any]:
    """Notes of one kind of relation noted again and again (未婚夫妻 seven times) crowd the others out of the first
    ten: each relation keeps its earliest and its latest note, in story order, so every different one is seen."""
    by: dict[str, list[Any]] = {}
    for n in notes:
        by.setdefault((n["relation_zh"] or "").strip(), []).append(n)
    kept = [x for ns in by.values() for x in ({id(ns[0]): ns[0], id(ns[-1]): ns[-1]}.values())]
    return sorted(kept, key=lambda n: n["first_ordinal"])


def _label(chars: dict[str, sqlite3.Row], cid: str) -> str:
    c = chars.get(cid)
    return (c["display_name_zh"] or c["canonical_name"]) if c is not None else cid


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
        placed: list[tuple[str, str, str, int, str | None, str]] = []
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
                when = r.period.strip()[:20]
                rel = at_period(r.relation.strip()[:20], when)
                if (a, b, rel) in manual or (b, a, rel) in manual:
                    continue
                since = min(o for _, _, o in cited)
                conn.execute(
                    "INSERT OR REPLACE INTO relations(book_id, a_id, b_id, relation_zh, a_is, b_is, since_ordinal, "
                    "fact_ids_json, reason, created_at, kind, certainty, origin, evidence_json, period) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (book_id, a, b, rel, at_period(r.a_is.strip()[:12], when) or None,
                     at_period(r.b_is.strip()[:12], when) or None, since,
                     json.dumps([i for k, i, _ in cited if k == "fact"]), r.reason[:200], utcnow(), r.kind, r.certainty,
                     "llm", json.dumps([{"kind": k, "id": i, "at": o} for k, i, o in cited]), when or None))
                placed.append((a, b, rel, since, (r.replaces or "").strip() or None, when))
                n += 1
        # 宿敵 → 未婚夫妻: the earlier relation ends where the later begins — the latest stretch of it still holding,
        # in the same time (a relation of a past life is ended by what happens in that life, not in the present); the
        # model names it without the time (「朋友」), as stored it carries it (「前世：朋友」)
        for a, b, _rel, since, replaces, when in sorted(placed, key=lambda x: x[3]):
            if not replaces:
                continue
            target = replaces if "：" in replaces else at_period(replaces, when)
            # a person's relation is the person's to end
            conn.execute("UPDATE relations SET until_ordinal=? WHERE rowid=(SELECT rowid FROM relations WHERE book_id=? "
                         "AND relation_zh=? AND since_ordinal<? AND until_ordinal IS NULL AND origin='llm' AND ((a_id=? "
                         "AND b_id=?) OR (a_id=? AND b_id=?)) ORDER BY since_ordinal DESC LIMIT 1)",
                         (since, book_id, target, since, a, b, b, a))
        # the same relation said again while an earlier stretch of it still holds is that stretch going on, not a new one
        conn.execute("DELETE FROM relations WHERE book_id=? AND origin='llm' AND EXISTS (SELECT 1 FROM relations e WHERE "
                     "e.book_id=relations.book_id AND e.relation_zh=relations.relation_zh AND e.since_ordinal<"
                     "relations.since_ordinal AND (e.until_ordinal IS NULL OR e.until_ordinal>relations.since_ordinal) AND "
                     "((e.a_id=relations.a_id AND e.b_id=relations.b_id) OR (e.a_id=relations.b_id AND e.b_id=relations.a_id)))",
                     (book_id,))
    return n


def visible(conn: sqlite3.Connection, book_id: str, upto: int | None = None) -> list[sqlite3.Row]:
    """Relations known at ``upto`` (begun by then, not yet replaced)."""
    q = "SELECT * FROM relations WHERE book_id=?"
    params: list[Any] = [book_id]
    if upto is not None:
        q += " AND since_ordinal<=? AND (until_ordinal IS NULL OR until_ordinal>?)"
        params += [upto, upto]
    return conn.execute(q + " ORDER BY since_ordinal", params).fetchall()
