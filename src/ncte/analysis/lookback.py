"""Look back once (ADR-104): what a reader does when something jars.

After a scene's lines are attributed, the program gathers — without judging — the lines where the words around them
do not fit the reading by the record. A line (not stated in the text, not set by a person) is gathered when

- the next line (the speech just after, starting within ``NEAR`` paragraphs) has a recorded word for a card that is
  neither this line's speaker nor the next line's own speaker (the reply calls someone 「姉さん」, the form recorded for
  her, and this line was given to someone else);
- the previous line (the speech just before, ending within ``NEAR`` paragraphs) has a recorded word for a card that is
  not this line's speaker, not someone this line is said to, and not the previous line's own speaker;
- the line itself has a recorded word for its own speaker (as if calling itself), or a self-reference word the record
  has from another person of the scene and never from its speaker (with no speaker: from exactly one person).

A word recorded for several cards counts only when none of them fits. Only those lines go back to the model, in one
small request with their paragraphs and the scene's picture (``situation``); the model confirms or changes each,
citing the paragraphs its answer rests on. A change without a cited paragraph of the item changes nothing; a line a
person set is never touched; nothing gathered, no request.

What is matched (strings and positions only; the program never decides what a word means here):
- a card's recorded forms: its name and aliases as known at the scene's end (``character_view`` in lookup mode: never
  a form kept for display only), two characters or
  more and not a pronoun; cards of another time (``period``) are left out, as everywhere words find a card;
- forms of address the memory step recorded others using for a card (``address_terms``, used as address), first used
  by the scene's end;
- self-reference: the words a card used for itself in its lines read before this scene (the first ``FIRST_PERSON``
  word of each line, as ``speech_profile`` counts) and the self-forms the memory step recorded for it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..llm.schemas import SpeakerLookbackOutput
from ..memory import repository as repo
from ..memory import visibility as vis
from .dialogue import FIRST_PERSON, clean
from .scene import PRONOUNS, situation_at
from .verify import _scene_people

MAX_LINES = 10  # lines gathered per scene (in story order)
NEAR = 3  # a neighbouring line starts at most this many paragraphs from the line's speech
CONTEXT = 2  # paragraphs shown before and after a gathered line (and its neighbours)
LINE_CHARS = 200
MIN_FORM = 2  # a one-character word (「母」) names anyone
SKIP = ("explicit", "human_verified")  # a line stated in the text (its tag checked) or set by a person


def _speeches(conn: sqlite3.Connection, book_id: str, scene_id: str, pos: dict[str, int]) -> list[dict[str, Any]]:
    """The scene's speeches in order: a line and the lines continuing it over paragraphs, as one."""
    rows = conn.execute("SELECT t.* FROM utterances t JOIN units u ON u.id=t.unit_id WHERE t.book_id=? AND "
                        "t.scene_id=? ORDER BY u.ordinal, t.quote_index", (book_id, scene_id)).fetchall()
    by_id = {r["id"]: r for r in rows}
    out: list[dict[str, Any]] = []
    head: dict[str, dict[str, Any]] = {}
    for r in rows:
        root = r
        while root["continuation_of"] and root["continuation_of"] in by_id:
            root = by_id[root["continuation_of"]]
        if r["unit_id"] not in pos:
            continue
        if root["id"] in head:
            sp = head[root["id"]]
            sp["rest"].append(r["id"])
            sp["text"] += clean(r["quote_text"])
            sp["last"] = pos[r["unit_id"]]
            continue
        sp = {"id": r["id"], "row": r, "rest": [], "text": clean(r["quote_text"]), "first": pos[r["unit_id"]],
              "last": pos[r["unit_id"]], "speaker": r["speaker_id_nullable"],
              "to": json.loads(r["addressee_ids_json"] or "[]")}
        head[r["id"]] = sp
        out.append(sp)
    return out


def recorded_forms(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row) -> dict[str, dict[str, str]]:
    """form → {card id: how it is recorded (name / alias / address)} for the cards known at the scene's end."""
    from ..memory.address import calls_by

    end = scene["end_ordinal"]
    forms_at = vis.form_positions(conn, book_id)
    out: dict[str, dict[str, str]] = {}

    def add(form: str, cid: str, kind: str) -> None:
        if form and len(form) >= MIN_FORM and form not in PRONOUNS:
            out.setdefault(form, {}).setdefault(cid, kind)

    for c in vis.visible_characters(conn, book_id, end):
        if repo.period_of(c) is not None:
            continue
        # lookup mode: only the words that find the card, never a form recorded for display only (ADR-103)
        if v := vis.character_view(conn, book_id, c, end, forms_at, lookup=True):
            add(v["name_ja"], c["id"], "name")
            for a in v["aliases_ja"]:
                add(a, c["id"], "alias")
    for r in conn.execute("SELECT * FROM address_terms WHERE book_id=? AND first_ordinal<=? AND speaker_id!=addressee_id",
                          (book_id, end)).fetchall():
        if calls_by(conn, r):
            add(r["form_ja"], r["addressee_id"], "address")
    return out


def self_words(conn: sqlite3.Connection, book_id: str, cids: set[str], before: int) -> dict[str, set[str]]:
    """card → the words it used for itself in its lines read before ordinal ``before`` (the first ``FIRST_PERSON`` word
    of each line) and the self-forms the memory step recorded for it by then."""
    out: dict[str, set[str]] = {c: set() for c in cids}
    for c in cids:
        for (q,) in conn.execute("SELECT t.quote_text FROM utterances t JOIN units u ON u.id=t.unit_id WHERE t.book_id=? "
                                 "AND t.speaker_id_nullable=? AND u.ordinal<? AND t.status IN "
                                 "('explicit','human_verified','inferred')", (book_id, c, before)):
            said = clean(q)
            if w := next((w for w in FIRST_PERSON if w in said), None):
                out[c].add(w)
        out[c] |= {r[0] for r in conn.execute("SELECT form_ja FROM address_terms WHERE book_id=? AND speaker_id=? AND "
                                              "addressee_id=? AND first_ordinal<?", (book_id, c, c, before))}
    return out


def _found(text: str, forms: dict[str, dict[str, str]]) -> list[tuple[str, dict[str, str]]]:
    """Each recorded form in ``text``, longest first, a shorter one inside a longer one found there not counted
    (「ミナ」 in 「ミナミ」)."""
    t = match_text(text)
    hits = []
    for f in sorted(forms, key=len, reverse=True):
        if f in t:
            hits.append((f, forms[f]))
            t = t.replace(f, "\0")
    return hits


def gather(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row) -> list[dict[str, Any]]:
    """The lines of a scene to look at once more, with what was noticed (see the module). Keys starting with ``_``
    stay out of the request."""
    rows = conn.execute("SELECT id, plain_text FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal",
                        (book_id, scene["id"])).fetchall()
    pos = {r["id"]: i for i, r in enumerate(rows)}
    speeches = _speeches(conn, book_id, scene["id"], pos)
    if not speeches:
        return []
    forms = recorded_forms(conn, book_id, scene)
    state = json.loads(scene["state_json"] or "{}")
    people = _scene_people(conn, book_id, scene["id"])
    mine = self_words(conn, book_id, people, scene["start_ordinal"])
    items: list[dict[str, Any]] = []
    for i, sp in enumerate(speeches):
        if sp["row"]["status"] in SKIP or len(items) >= MAX_LINES:
            continue
        speaker, to = sp["speaker"], set(sp["to"])
        noticed: list[dict[str, Any]] = []
        lo, hi = sp["first"], sp["last"]
        for where, j in (("previous_line", i - 1), ("next_line", i + 1)):
            if not 0 <= j < len(speeches):
                continue
            nb = speeches[j]
            if (nb["first"] - sp["last"] if j > i else sp["first"] - nb["last"]) > NEAR:
                continue
            # the reply calls the one it answers; the line before may call whom this line is said to as well
            fits = {speaker, nb["speaker"], *(to if j < i else ())} - {None}
            for form, cards in _found(nb["text"], forms):
                if fits & set(cards):  # the word is (also) recorded for the speaker, an addressee or the one saying it
                    continue
                for cid, kind in cards.items():
                    noticed.append({"in": where, "word": form, "of": cid, "recorded_as": kind,
                                    "line": nb["text"][:LINE_CHARS], "said_by": nb["speaker"]})
                    lo, hi = min(lo, nb["first"]), max(hi, nb["last"])
        said = sp["text"]
        if speaker is not None:  # a word recorded for the speaker in their own line, as if calling themself
            for form, cards in _found(said, forms):
                if speaker in cards and len(cards) == 1:
                    noticed.append({"in": "this_line", "word": form, "of": speaker, "recorded_as": cards[speaker]})
        words ={w for w in {next((w for w in FIRST_PERSON if w in said), None)} if w} \
            | {w for ws in mine.values() for w in ws if w not in FIRST_PERSON and w in said}
        for w in sorted(words):
            owners = sorted(c for c, ws in mine.items() if w in ws and c != speaker)
            if speaker is not None and (not mine.get(speaker) or w in mine[speaker]):
                continue  # nothing recorded for the speaker to compare with, or the word is theirs too
            if speaker is None and len(owners) != 1:
                continue
            for cid in owners:
                noticed.append({"in": "this_line", "word": w, "of": cid, "recorded_as": "self_reference"})
        if not noticed:
            continue
        lo, hi = max(0, lo - CONTEXT), min(len(rows) - 1, hi + CONTEXT)
        ctx = [{"unit_id": rows[k]["id"], "source": rows[k]["plain_text"]} for k in range(lo, hi + 1)]
        who = people | {n["of"] for n in noticed} | ({speaker} if speaker else set()) | to \
            | {str(n["said_by"]) for n in noticed if n.get("said_by")}
        at = situation_at(state, pos, sp["row"]["unit_id"])
        items.append({"ref": f"l{len(items) + 1}", "unit_id": sp["row"]["unit_id"], "qi": sp["row"]["quote_index"],
                      "line": sp["text"][:LINE_CHARS], "speaker": speaker, "to": sorted(to), "noticed": noticed,
                      **({"situation": at} if at else {}), "context": ctx,
                      "_utt": sp["id"], "_rest": sp["rest"], "_speaker": speaker, "_people": sorted(who),
                      "_units": [c["unit_id"] for c in ctx]})
    return items


def build_payload(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row,
                  items: list[dict[str, Any]]) -> dict[str, Any]:
    names = vis.visible_names(conn, book_id, scene["end_ordinal"])
    people = sorted({c for i in items for c in i["_people"]})
    return {"task": "speaker_lookback", "scene_id": scene["id"],
            "people": {c: names[c] for c in people if c in names},
            "lines": [{k: v for k, v in i.items() if not k.startswith("_")} for i in items]}


def apply(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row, items: list[dict[str, Any]],
          parsed: SpeakerLookbackOutput, *, went: dict[str, str] | None = None) -> dict[str, int]:
    """The model's answers: a line's speaker changed only with a paragraph of its item cited, to someone of the item's
    people (or to no one: the text does not tell), through the second look's speaker path; a line changed since it
    was gathered or set by a person is left. Whom a line is said to changes the same way. The lines continuing a
    changed speech follow it. As in the second look (``verify.apply``), a card the answer names is followed through
    the merges ``went`` records (card merged away → the card it went into); a card gone with no chain is not an
    answer (``unapplied``), never written."""
    from ..memory.address import follow_speakers
    from .verify import _set_speaker

    went = went or {}

    def alive(cid: str) -> str | None:
        while cid in went:
            cid = went[cid]
        return cid if conn.execute("SELECT 1 FROM characters WHERE id=?", (cid,)).fetchone() else None

    by_ref = {i["ref"]: i for i in items}
    stats = {"unapplied": 0, "asked": len(items), "speakers_changed": 0, "speakers_unknown": 0, "speakers_kept": 0, "uncited": 0,
             "ignored": 0, "addressees_changed": 0}
    changed: dict[str, str | None] = {}
    for a in parsed.lines:
        item = by_ref.pop(a.ref, None)
        if item is None:
            stats["ignored"] += 1
            continue
        u = conn.execute("SELECT * FROM utterances WHERE id=?", (item["_utt"],)).fetchone()
        if u is None or u["status"] == "human_verified" or u["speaker_id_nullable"] != item["_speaker"]:
            stats["ignored"] += 1
            continue
        new = a.speaker if a.speaker not in (None, "", "unknown", "null") else None
        if new is not None and new not in item["_people"]:
            stats["ignored"] += 1
            continue
        if new is not None and (new := alive(new)) is None:
            stats["unapplied"] += 1
            continue
        cited = [e for e in a.evidence_unit_ids if e in item["_units"]]
        note = f"look back: {new or 'unknown'} — {a.reason[:160]}"
        if new == u["speaker_id_nullable"]:
            stats["speakers_kept"] += 1
        elif not cited:
            stats["uncited"] += 1  # an answer without the passage it rests on changes nothing
            continue
        else:
            status = "inferred" if new else "unknown"
            for line in [item["_utt"], *item["_rest"]]:
                row = conn.execute("SELECT speaker_id_nullable, status FROM utterances WHERE id=?", (line,)).fetchone()
                if row is not None and row["status"] != "human_verified" and row[0] == item["_speaker"]:
                    _set_speaker(conn, line, new, status, note, stats if line == item["_utt"] else {
                        "speakers_changed": 0, "speakers_unknown": 0, "speakers_kept": 0}, changed, source="lookback")
        to = [y for x in dict.fromkeys(a.to) if x in item["_people"] and (y := alive(x)) and y != new]
        if a.to and cited and len(to) == len(set(a.to)) and set(to) != set(item["to"]):
            conn.execute("UPDATE utterances SET addressee_ids_json=?, addressee_status=? WHERE id=?",
                         (json.dumps(to), "inferred" if to else None, item["_utt"]))
            stats["addressees_changed"] += 1
    stats["unanswered"] = len(by_ref)
    follow_speakers(conn, book_id, changed)
    repo.refresh_profiles(conn, book_id)
    row = conn.execute("SELECT state_json FROM scenes WHERE book_id=? AND id=?", (book_id, scene["id"])).fetchone()
    state = json.loads(row[0] or "{}") if row else {}
    state["lookback"] = {k: v for k, v in stats.items() if v}
    conn.execute("UPDATE scenes SET state_json=? WHERE book_id=? AND id=?",
                 (json.dumps(state, ensure_ascii=False), book_id, scene["id"]))
    return stats
