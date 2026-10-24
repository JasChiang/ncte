"""A second look at the read pass's disputed judgments, before naming and translation (ADR-033).

Only what the first pass left in doubt is asked again, with the surrounding text: lines the model was unsure of
(it named candidates but no speaker, its evidence for a stated speaker did not check, or it read another speaker for
the rest of a speech), genders known only by inference, and characters merged when a name was
revealed, and actions the reading inferred and gave to someone absent from the scene (a quoted line: who said it).
The answers never become facts: a speaker stays ``inferred``; a doubtful inferred gender is dropped (the
translation then uses 你); a merge judged "different" stops counting the merged description as this character
(its forms leave the aliases) and becomes a finding for the user — lines moved by the merge are not moved back.
A card made for someone known only by words other cards have is asked which of them it is, or someone else
(``same_person``, ADR-103): the model's answer, resting on cited paragraphs, merges it or clears its mark.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..llm.schemas import VerifyOutput
from ..memory import repository as repo
from ..memory.db import utcnow
from ..memory.visibility import shared_names, tell_apart

CONTEXT_UNITS = 4  # paragraphs before and after a disputed line
AROUND_CHARS = 200  # a paragraph around it: its opening
FULL_CHARS = 2000  # the asked paragraph and the paragraphs its evidence is in: whole (bounded only against a runaway one)
MAX_EVIDENCE = 3  # evidence paragraphs outside the window, sent whole
MAX_ITEMS = 150
MAX_ACTION_ITEMS = 40  # default for ``context.verify_action_items``
ACTION_CHARS = 8000  # an action's passage: from a little before it to the end of its scene, at most this much
MAX_ADDRESS_ITEMS = 300  # forms found by the program's string match (ADR-037)
ADDRESS_LINES = 2  # lines shown per form


def _names(conn: sqlite3.Connection, book_id: str) -> dict[str, str]:
    from ..memory.visibility import first_seen
    from ..series import SERIES_SCOPE

    rows = {r["id"]: r for r in conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book_id,))}
    names = {cid: r["canonical_name"] + (f"（{r['display_name_zh']}）" if r["display_name_zh"] else "")
             + (f"〔{w or repo.OTHER_PERSON}〕" if (w := repo.period_of(r)) is not None else "")  # another time (ADR-103)
             for cid, r in rows.items()}
    # cards the book calls by the same words (two unnamed mothers): told apart by the book's own text where each was
    # first seen, never by a label (ADR-103)
    return {cid: f"{n}〔初見：{first_seen(conn, rows[cid])}〕" if cid in shared else n
            for shared in [shared_names(names)] for cid, n in names.items()}


def _window(conn: sqlite3.Connection, book_id: str, ordinal: int, full: set[int] | None = None) -> list[str]:
    """The paragraphs around ``ordinal``: the marked one (▶) and those in ``full`` (where cited evidence is) whole, the
    others by their opening — a clue late in a long paragraph is not cut off where the question is decided."""
    whole = {ordinal} | (full or set())
    rows = conn.execute("SELECT ordinal, plain_text FROM units WHERE book_id=? AND translatable=1 AND ordinal BETWEEN ? "
                        "AND ? ORDER BY ordinal", (book_id, ordinal - CONTEXT_UNITS, ordinal + CONTEXT_UNITS)).fetchall()
    return [("▶ " if r["ordinal"] == ordinal else "")
            + match_text(r["plain_text"])[:FULL_CHARS if r["ordinal"] in whole else AROUND_CHARS] for r in rows]


def _ordinals(conn: sqlite3.Connection, book_id: str, unit_ids: list[str]) -> list[int]:
    ids = [u for u in unit_ids if u]
    if not ids:
        return []
    return [r[0] for r in conn.execute(f"SELECT ordinal FROM units WHERE book_id=? AND id IN ({','.join('?' * len(ids))}) "
                                       "ORDER BY ordinal", (book_id, *ids))]


def _whole(conn: sqlite3.Connection, book_id: str, ordinals: list[int]) -> list[str]:
    """Paragraphs given whole (evidence outside the window), in reading order."""
    if not ordinals:
        return []
    marks = ",".join("?" * len(ordinals))
    return [match_text(r[0])[:FULL_CHARS] for r in conn.execute(
        f"SELECT plain_text FROM units WHERE book_id=? AND ordinal IN ({marks}) ORDER BY ordinal", (book_id, *ordinals))]


def _speaker_evidence(conn: sqlite3.Connection, book_id: str, utterance_id: str) -> list[int]:
    """Where the reading's evidence for the line's candidates is."""
    ids: list[str] = []
    for (ev,) in conn.execute("SELECT evidence_json FROM speaker_hypotheses WHERE utterance_id=?", (utterance_id,)):
        try:
            ids += [str(x) for x in (json.loads(ev or "{}").get("evidence_unit_ids") or [])]
        except (ValueError, AttributeError):
            continue
    return _ordinals(conn, book_id, list(dict.fromkeys(ids)))


def collect(conn: sqlite3.Connection, book_id: str, max_actions: int = MAX_ACTION_ITEMS) -> list[dict[str, Any]]:
    """Items to look at again. Keys starting with ``_`` stay out of the request."""
    names = _names(conn, book_id)
    items: list[dict[str, Any]] = []
    for u in conn.execute(
            "SELECT ut.*, un.ordinal FROM utterances ut JOIN units un ON un.id=ut.unit_id WHERE ut.book_id=? AND "
            "ut.status IN ('inferred','unknown') AND ut.notes_json LIKE '%(to look at)%' AND ut.resolution_source!='verify' "
            "ORDER BY un.ordinal", (book_id,)):  # answered by an earlier second look: not asked again (a re-read resets it)
        # the reading's candidates, and anyone the scene has (who speaks or is present there): the second look may find
        # the speaker the reading did not list (ADR-103 real run)
        cands = sorted(({h["candidate_character_id"] for h in conn.execute(
            "SELECT candidate_character_id FROM speaker_hypotheses WHERE utterance_id=?", (u["id"],))}
            | _scene_people(conn, book_id, u["scene_id"])) & names.keys())
        evidence = _speaker_evidence(conn, book_id, u["id"])
        near = {o for o in evidence if abs(o - u["ordinal"]) <= CONTEXT_UNITS}
        item = {"ref": f"q:{u['id']}", "kind": "speaker", "quote": match_text(u["quote_text"])[:120],
                "context": _window(conn, book_id, u["ordinal"], near),
                "candidates": {c: names[c] for c in cands},
                "first_answer": names.get(u["speaker_id_nullable"]) if u["speaker_id_nullable"] else None,
                "_ordinal": u["ordinal"]}
        if far := [o for o in evidence if o not in near][:MAX_EVIDENCE]:
            item["evidence_paragraphs"] = _whole(conn, book_id, far)
        items.append(item)
    for g in conn.execute("SELECT g.* FROM character_gender g JOIN characters c ON c.id=g.character_id WHERE "
                          "c.book_id=? AND g.certainty='inferred' AND g.origin='llm' AND g.gender IN ('female','male')",
                          (book_id,)):
        c = conn.execute("SELECT * FROM characters WHERE id=?", (g["character_id"],)).fetchone()
        forms = [c["canonical_name"], *json.loads(c["aliases_json"] or "[]")]
        hits = [r for r in conn.execute("SELECT ordinal, plain_text FROM units WHERE book_id=? AND translatable=1 "
                                        "ORDER BY ordinal", (book_id,)) if any(f in r["plain_text"] for f in forms)]
        spread = hits if len(hits) <= 5 else [hits[i * (len(hits) - 1) // 4] for i in range(5)]
        items.append({"ref": f"g:{c['id']}", "kind": "gender", "character": names.get(c["id"], c["canonical_name"]),
                      "first_answer": g["gender"], "evidence": g["evidence_quote"],
                      "evidence_paragraph": next(iter(_whole(conn, book_id, [g["ordinal"]])), None),
                      "samples": [match_text(r["plain_text"])[:160] for r in spread],
                      "_version": f"{g['updated_at']}|{g['certainty']}|{g['origin']}"})  # a gender written again is new
    for link in conn.execute("SELECT * FROM entity_links WHERE book_id=? AND origin='scene_analysis' AND "
                             "status='accepted' AND form_type='official'", (book_id,)):
        # the card the reveal named: the one a name finds, else one of another time named so (never a card known
        # only by other cards' words)
        c = conn.execute("SELECT * FROM characters WHERE book_id=? AND canonical_name=? AND (label='' OR period IS NOT "
                         "NULL) ORDER BY period IS NOT NULL LIMIT 1", (book_id, link["target_term"])).fetchone()
        if c is None:
            continue
        # the new name and its own aliases were linked at the reveal; the rest came from the merged description
        own = {r[0] for r in conn.execute("SELECT term FROM entity_links WHERE book_id=? AND target_term=? AND "
                                          "origin='scene_analysis'", (book_id, c["canonical_name"]))}
        others = [a for a in json.loads(c["aliases_json"] or "[]") if a and a not in own]
        if not others:
            continue
        items.append({"ref": f"m:{c['id']}", "kind": "merge", "name": c["canonical_name"], "merged_forms": others,
                      "context": _window(conn, book_id, link["revealed_ordinal"] or c["known_from_ordinal"]),
                      "_ordinal": link["revealed_ordinal"], "_version": f"{link['id']}|{c['updated_at']}"})
    for sc in conn.execute("SELECT * FROM scenes WHERE book_id=? AND state_json LIKE '%\"check\": \"conflict\"%' "
                           "ORDER BY seq", (book_id,)):
        narr = json.loads(sc["state_json"]).get("narration") or {}
        if narr.get("check") != "conflict":
            continue
        paras = [match_text(r["plain_text"])[:140] for r in conn.execute(
            "SELECT plain_text FROM units WHERE book_id=? AND scene_id=? AND kind='text' ORDER BY ordinal LIMIT 40",
            (book_id, sc["id"])) if not r[0].lstrip(" 　").startswith(("「", "『"))][:12]
        items.append({"ref": f"n:{sc['id']}", "kind": "narration", "first_answer": narr["person"], "paragraphs": paras,
                      "_version": str(sc["version"])})  # a scene read again is asked again
    items += question_items(conn, book_id)
    items += action_items(conn, book_id)[:max_actions]
    _same_name_candidates(conn, book_id, items)
    # an item answered before with the same content and the same version of its data is not asked again (asking until
    # the model agrees adds nothing); a gender written again, a scene read again, a changed card are asked again
    answered = {r[0]: r[1] for r in conn.execute("SELECT ref, fingerprint FROM verify_answers WHERE book_id=?", (book_id,))}

    def fresh(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for i in group:
            i["_fp"] = fingerprint(i)
            i["_fp_all"] = fingerprint(i, with_candidates=True)
            # answered "unknown": asked again once the cards it could be change (the right one may have come); answered
            # with someone: the card's own material decides
            if i["kind"] == "speaker" or answered.get(i["ref"]) not in (i["_fp"], i["_fp_all"]):
                out.append(i)
        return out

    # the few of each other kind first, then the lines, so many lines never push the rest out
    items = fresh([i for i in items if i["kind"] != "speaker"] + [i for i in items if i["kind"] == "speaker"])[:MAX_ITEMS]
    items += fresh(narrator_items(conn, book_id, names))
    items += fresh(same_person_items(conn, book_id, names))
    items += fresh(identity_items(conn, book_id, names))
    return items


def answer_valid(item: dict[str, Any], answer: str) -> bool:
    """An answer that can be applied (or a plain "unknown"): only these count as answered; an id not among the
    candidates or a value the kind does not take is asked again next time."""
    kind = item["kind"]
    cands = set(item.get("candidates") or {})
    if answer == "unknown":
        return True
    if kind == "gender":
        return answer in ("female", "male")
    if kind == "merge":
        return answer in ("same", "different", "unsure")
    if kind == "same_person":
        return answer == "different" or answer in cands
    if kind == "narration":
        return answer in ("first", "third", "unclear")
    if kind == "question":
        return bool(answer)
    if kind in ("group", "action"):
        if kind == "group" and answer.startswith("same:"):
            return answer[5:] in cands
        return all(x.strip() in cands for x in answer.split(","))
    return answer in cands  # speaker, identity, narrator


def fingerprint(item: dict[str, Any], *, with_candidates: bool = False) -> str:
    import hashlib

    seen = {k: v for k, v in item.items() if not k.startswith("_")} | {"_version": item.get("_version")}
    if item.get("kind") in ("identity", "group", "narrator") and not with_candidates:
        # the card's own material, not the list of every other card (a new card elsewhere is no reason to ask again;
        # a name revealed later reaches the card through the reveal and the merge, not through this question)
        seen.pop("candidates", None)
        seen.pop("narrators_of_other_first_person_scenes", None)
    return hashlib.sha1(json.dumps(seen, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def identity_items(conn: sqlite3.Connection, book_id: str, names: dict[str, str]) -> list[dict[str, Any]]:
    """Is a card the same person as another card (「姉ちゃん」 in the sister's story is a character known by name), and who
    is a group card (「跟班兩人」) made of — or is it the same group as another card? Whether a card's name is a proper
    name or only a description is the model's call (the glossary keeps 「村長」「跟班」 as character names too), so every
    card with lines or relations is asked: groups first, then the cards known by the fewest name forms and lines
    (ADR-046)."""
    from ..series import SERIES_SCOPE

    rows = conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM utterances u WHERE u.speaker_id_nullable=c.id) n, "
        "(SELECT COUNT(*) FROM relation_notes r WHERE r.a_id=c.id OR r.b_id=c.id) "
        "+ (SELECT COUNT(*) FROM address_terms t WHERE t.speaker_id=c.id OR t.addressee_id=c.id) "
        "+ (SELECT COUNT(*) FROM group_members g WHERE g.group_id=c.id OR g.member_id=c.id) k FROM characters c "
        "WHERE c.book_id=? AND c.is_narrator=0 ORDER BY c.known_from_ordinal", (book_id,)).fetchall()
    cards = {r[0]: names[r[0]] for r in conn.execute(  # earlier volumes' characters too
        f"SELECT id FROM characters WHERE {SERIES_SCOPE} AND is_narrator=0", (book_id,)) if r[0] in names}

    def order(c: sqlite3.Row) -> tuple[int, int, int]:
        return (0 if c["is_group"] else 1, len(json.loads(c["aliases_json"] or "[]")), c["n"])

    out = []
    # a card marked as maybe one of a few cards with the same words is asked that (``same_person_items``) instead
    for c in sorted((r for r in rows if r["id"] in cards and (r["n"] or r["k"]) and not repo.may_be(r)), key=order):
        others = {k: v for k, v in cards.items() if k != c["id"]}
        ctx = _window(conn, book_id, c["known_from_ordinal"])
        called_by = [f"{names.get(a['speaker_id'], '?')}：{a['form_ja']}" for a in conn.execute(
            "SELECT speaker_id, form_ja FROM address_terms WHERE book_id=? AND addressee_id=? LIMIT 5", (book_id, c["id"]))]
        kind = "group" if c["is_group"] else "identity"  # as the model read it when the card was made
        item = {"ref": f"{'p' if kind == 'group' else 'i'}:{c['id']}", "kind": kind, "name": cards[c["id"]],
                "context": ctx, "called_by": called_by, "candidates": others}
        if kind == "group":  # members noted while reading: the model confirms or adds
            item["members"] = [cards[g[0]] for g in conn.execute(
                "SELECT member_id FROM group_members WHERE group_id=?", (c["id"],)) if g[0] in cards]
        out.append(item)
    return out  # every card is asked, a batch at a time (about US$0.001 each)


SAME_LINES = 4  # a card's lines shown for the same-person question, each with its paragraph


def same_person_items(conn: sqlite3.Connection, book_id: str, names: dict[str, str]) -> list[dict[str, Any]]:
    """Each card made for a person the reading took as new but knew only by words other cards have (two unnamed
    mothers, each 「母親」; ADR-103): is it one of those cards, or someone else? The card and each candidate are shown
    by the book's own text — the paragraphs around where each was first seen and a few of their lines, every paragraph
    with a code — so cards with the same name are told apart without a made-up name. A "same" or "different" answer
    cites the paragraphs it rests on; unanswered, the card stays marked."""
    out = []
    for c in conn.execute("SELECT * FROM characters WHERE book_id=? AND may_be_json!='[]' ORDER BY known_from_ordinal",
                          (book_id,)).fetchall():
        may = [x for x in repo.may_be(c) if x in names and x != c["id"]]
        if not may:
            continue
        codes: dict[str, str] = {}
        rows = {r["id"]: r for r in conn.execute(f"SELECT * FROM characters WHERE id IN ({','.join('?' * len(may))})",
                                                 may)}
        # what earlier steps recorded of each, as known where this card is first seen (and the passage shown around it)
        hints = tell_apart(conn, book_id, [c, *rows.values()], c["known_from_ordinal"] + CONTEXT_UNITS)
        out.append({"ref": f"s:{c['id']}", "kind": "same_person", "name": names[c["id"]],
                    "card": {**_card_text(conn, c, codes), **_notes_of(hints, c["id"])},
                    "candidates": {x: names[x] for x in may if x in rows},
                    "candidate_cards": {x: {**_card_text(conn, rows[x], codes), **_notes_of(hints, x)}
                                        for x in may if x in rows},
                    "_codes": codes, "_version": f"{c['updated_at']}|{','.join(may)}"})
    return out


def _coded(conn: sqlite3.Connection, book_id: str, ordinals: list[int], mark: int,
           codes: dict[str, str]) -> list[dict[str, str]]:
    """These paragraphs, each with a code (``codes``: code → unit id, shared by the whole item); the marked one whole,
    the others by their opening."""
    out: list[dict[str, str]] = []
    for r in conn.execute(f"SELECT id, ordinal, plain_text FROM units WHERE book_id=? AND translatable=1 AND "
                          f"ordinal IN ({','.join('?' * len(ordinals))}) ORDER BY ordinal", (book_id, *ordinals)):
        code = next((k for k, v in codes.items() if v == r["id"]), None) or f"s{len(codes) + 1}"
        codes[code] = r["id"]
        whole = r["ordinal"] == mark
        out.append({"p": code, "text": ("▶ " if whole else "")
                    + match_text(r["plain_text"])[:FULL_CHARS if whole else AROUND_CHARS]})
    return out


def _card_text(conn: sqlite3.Connection, row: sqlite3.Row, codes: dict[str, str]) -> dict[str, Any]:
    """A card as the book shows it: the paragraphs around where it was first seen, and the paragraphs of a few of its
    lines further on (in the volume it was first seen in, an earlier one's card too)."""
    at, book_id = row["known_from_ordinal"], row["book_id"]
    lines = [r[0] for r in conn.execute(
        "SELECT DISTINCT un.ordinal FROM utterances u JOIN units un ON un.id=u.unit_id WHERE u.speaker_id_nullable=? "
        "AND un.book_id=? ORDER BY un.ordinal LIMIT ?", (row["id"], book_id, SAME_LINES)) if abs(r[0] - at) > CONTEXT_UNITS]
    out: dict[str, Any] = {"first_seen": _coded(conn, book_id, list(range(at - CONTEXT_UNITS, at + CONTEXT_UNITS + 1)),
                                                at, codes)}
    if lines:
        out["lines"] = [p for o in lines for p in _coded(conn, book_id, [o], o, codes)]
    return out


def _notes_of(hints: dict[str, dict[str, Any]], cid: str) -> dict[str, Any]:
    """A card's earlier readings and relations (``tell_apart``) without its first paragraph (shown whole already)."""
    return {k: v for k, v in hints.get(cid, {}).items() if k != "first_seen"}


def _same_name_candidates(conn: sqlite3.Connection, book_id: str, items: list[dict[str, Any]]) -> None:
    """A question naming candidates two of which have the same name (two unnamed mothers): what tells those apart, as
    known at the question's paragraph (ADR-103). Other questions stay as they were."""
    for item in items:
        cands = item.get("candidates")
        if not isinstance(cands, dict) or "_ordinal" not in item or not item["_ordinal"]:
            continue
        base = {cid: n.split("〔初見：")[0] for cid, n in cands.items()}
        if shared := shared_names(base):
            rows = conn.execute(f"SELECT * FROM characters WHERE id IN ({','.join('?' * len(shared))})",
                                sorted(shared)).fetchall()
            item["same_name_candidates"] = tell_apart(conn, book_id, rows, item["_ordinal"])


def _rests_on(item: dict[str, Any], a: Any) -> bool:
    """A same-person answer that changes the cards cites at least one of the paragraphs it was shown."""
    return item.get("kind") != "same_person" or a.answer.strip() == "unknown" or any(
        x in item.get("_codes", {}) for x in a.evidence)


def _scene_people(conn: sqlite3.Connection, book_id: str, scene_id: str | None) -> set[str]:
    """Who the scene has: present (the reading's participants) or speaking a line there."""
    if not scene_id:
        return set()
    sc = conn.execute("SELECT state_json FROM scenes WHERE book_id=? AND id=?", (book_id, scene_id)).fetchone()
    people = {p.get("character_id") for p in json.loads(sc[0] or "{}").get("participants") or []} if sc else set()
    people |= {r[0] for r in conn.execute("SELECT speaker_id_nullable FROM utterances WHERE book_id=? AND scene_id=? "
                                          "AND speaker_id_nullable IS NOT NULL", (book_id, scene_id))}
    return {str(x) for x in people if x}


def question_items(conn: sqlite3.Connection, book_id: str) -> list[dict[str, Any]]:
    """The questions the model raised itself while reading a scene (its ``uncertainties``) that have no answer yet,
    each with the passage around it and the scene's people (named as known at the end of the scene): asked once, with
    more of the text around (ADR-067). The answer is kept with the question; it decides nothing."""
    from ..memory.visibility import visible_names

    out = []
    for sc in conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book_id,)):
        state = json.loads(sc["state_json"] or "{}")
        open_ = [(i, x) for i, x in enumerate(state.get("uncertainties") or [])
                 if x.get("question") and not x.get("answer")]
        if not open_:
            continue
        names = visible_names(conn, book_id, sc["end_ordinal"])
        people = {p.get("character_id") for p in state.get("participants", [])}
        people |= {r[0] for r in conn.execute("SELECT speaker_id_nullable FROM utterances WHERE book_id=? AND scene_id=? "
                                              "AND speaker_id_nullable IS NOT NULL", (book_id, sc["id"]))}
        # the scene's people first, then anyone known by then: the one asked about may not be present (a note's writer)
        cands = {c: names[c] for c in sorted(x for x in people if x) if c in names}
        cands.update({c: n for c, n in names.items() if c not in cands})
        for i, x in open_:
            at = [r[0] for r in conn.execute(
                f"SELECT ordinal FROM units WHERE book_id=? AND id IN ({','.join('?' * len(x.get('unit_ids') or []))}) "
                "ORDER BY ordinal", (book_id, *(x.get("unit_ids") or [])))] if x.get("unit_ids") else []
            ordinal = at[0] if at else sc["start_ordinal"]
            item = {"ref": f"u:{sc['id']}:{i}", "kind": "question", "question": x["question"],
                    "context": _window(conn, book_id, ordinal, set(at)), "candidates": cands,
                    "_ordinal": ordinal, "_version": str(sc["version"])}
            # the lines in the paragraphs the question is about: an answer naming who says one of them sets its
            # speaker, as a speaker re-check does (ADR-103 real run)
            if lines := [{"line_ref": r[0], "quote": match_text(r[1])[:80]} for r in conn.execute(
                    f"SELECT ut.id, ut.quote_text FROM utterances ut JOIN units un ON un.id=ut.unit_id WHERE ut.book_id=? "
                    f"AND ut.unit_id IN ({','.join('?' * len(x.get('unit_ids') or []))}) ORDER BY un.ordinal, "
                    "ut.quote_index", (book_id, *(x.get("unit_ids") or [])))] if x.get("unit_ids") else []:
                item["lines"] = lines
            out.append(item)
    return out


def action_items(conn: sqlite3.Connection, book_id: str) -> list[dict[str, Any]]:
    """Actions the reading only inferred (or could not place) and gave to someone who is not in the scene — no
    participant, speaker or narrator there: typically a line quoted from someone absent (「〜って言われて」). Inside the
    scene's reading this is one small part of a long request; asked alone, with the scene's text from a little before
    it to its end (where the evidence often is), the same model reads it with care. The question shows neither the
    reading's answer nor its summary of the action (which names that answer): the model reads the passage fresh and the
    program compares. The passage runs from the scene's start (or a few paragraphs before the action when the scene is
    long) through the end of the scene — never past it. Who did it stays the model's call, and stays ``inferred``; an
    action the second look answered is not asked again until the scene is read again."""
    from ..memory.visibility import visible_names

    out = []
    for sc in conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY seq", (book_id,)):
        state = json.loads(sc["state_json"] or "{}")
        acts = [(i, a) for i, a in enumerate(state.get("actions") or [])
                if a.get("status") in ("inferred", "unknown") and not a.get("verified") and a.get("subject_candidates")]
        if not acts:
            continue
        present = {p.get("character_id") for p in state.get("participants") or []}
        present |= {(state.get("narration") or {}).get("narrator")}
        present |= {r[0] for r in conn.execute("SELECT DISTINCT speaker_id_nullable FROM utterances WHERE book_id=? AND "
                                               "scene_id=? AND speaker_id_nullable IS NOT NULL", (book_id, sc["id"]))}
        rows = conn.execute("SELECT id, ordinal, plain_text FROM units WHERE book_id=? AND scene_id=? AND translatable=1 "
                            "ORDER BY ordinal", (book_id, sc["id"])).fetchall()
        at = {r["id"]: k for k, r in enumerate(rows)}
        names: dict[str, str] | None = None
        for i, a in acts:
            if not set(a["subject_candidates"]) - present or a.get("unit_id") not in at:
                continue
            names = names if names is not None else visible_names(conn, book_id, sc["end_ordinal"])
            k0 = at[a["unit_id"]]
            paras: list[dict[str, str]] = []
            codes: dict[str, str] = {}
            used = 0
            lead = sum(len(match_text(r["plain_text"])[:FULL_CHARS]) for r in rows[:k0 + 1])
            for k in range(0 if lead <= ACTION_CHARS // 2 else max(0, k0 - CONTEXT_UNITS), len(rows)):
                text = match_text(rows[k]["plain_text"])[:FULL_CHARS]
                if used + len(text) > ACTION_CHARS and k > k0:
                    break
                code = f"a{len(paras) + 1}"
                codes[code] = rows[k]["id"]
                paras.append({"p": code, "text": ("▶ " if k == k0 else "") + text})
                used += len(text)
            out.append({"ref": f"a:{sc['id']}:{i}", "kind": "action",
                        "done_to": [names.get(x, x) for x in a.get("object_candidates") or []],
                        "paragraphs": paras, "candidates": dict(sorted(names.items())),
                        "_summary": a.get("summary", ""), "_first": list(a["subject_candidates"]),
                        "_codes": codes, "_version": f"{sc['version']}|{','.join(a['subject_candidates'])}"})
    return out


def narrator_items(conn: sqlite3.Connection, book_id: str, names: dict[str, str]) -> list[dict[str, Any]]:
    """Each unnamed first-person narrator card, with what reading gathered about them, against the named characters:
    is the narrator one of them (named later, or never tied while reading)? (ADR-045)"""
    out = []
    named = [r for r in conn.execute(
        "SELECT c.id, COUNT(u.id) n FROM characters c LEFT JOIN utterances u ON u.speaker_id_nullable=c.id WHERE "
        "c.book_id=? AND c.is_narrator=0 GROUP BY c.id ORDER BY n DESC LIMIT 15", (book_id,))]
    for c in conn.execute("SELECT * FROM characters WHERE book_id=? AND is_narrator=1 AND canonical_name LIKE '（敘述者%'",
                          (book_id,)):
        facts = [f"{f['predicate']} {f['object']}" for f in conn.execute(
            "SELECT predicate, object FROM facts WHERE book_id=? AND (subject=? OR object_ref=?) AND retracted_by IS NULL "
            "ORDER BY first_revealed_ordinal LIMIT 10", (book_id, c["id"], c["id"]))]
        called = [f"{names.get(a['speaker_id'], '?')}：{a['form_ja']}" for a in conn.execute(
            "SELECT speaker_id, form_ja FROM address_terms WHERE book_id=? AND addressee_id=? AND speaker_id!=? "
            "ORDER BY uses DESC LIMIT 8", (book_id, c["id"], c["id"]))]
        lines = [match_text(u["quote_text"])[:100] for u in conn.execute(
            "SELECT quote_text FROM utterances WHERE book_id=? AND speaker_id_nullable=? LIMIT 6", (book_id, c["id"]))]
        # who the reading judged to narrate the other first-person scenes (a name learned later: 「坊っちゃん」)
        others: dict[str, int] = {}
        for (state,) in conn.execute("SELECT state_json FROM scenes WHERE book_id=?", (book_id,)):
            narr = json.loads(state or "{}").get("narration") or {}
            if narr.get("person") == "first" and narr.get("narrator") not in (None, c["id"]) and narr["narrator"] in names:
                others[narr["narrator"]] = others.get(narr["narrator"], 0) + 1
        cands = {r["id"]: names[r["id"]] for r in named if r["id"] in names}
        cands.update({x: names[x] for x in others})
        out.append({"ref": f"r:{c['id']}", "kind": "narrator", "facts": facts, "called": called, "lines": lines,
                    "first_scene": _window(conn, book_id, c["known_from_ordinal"])[:6],
                    "narrators_of_other_first_person_scenes": {names[x]: n for x, n in others.items()},
                    "candidates": cands})
    return out


def apply(conn: sqlite3.Connection, book_id: str, items: list[dict[str, Any]], parsed: VerifyOutput,
          finding: Any, *, unapplied: list[str] | None = None, went: dict[str, str] | None = None) -> dict[str, int]:
    """Apply the second look. ``finding(unit_id, type, severity, evidence)`` records what needs the user.

    The answers come from one snapshot: a card an earlier answer of this pass merged away is followed to the card it
    went into. A same-person answer left with nothing to do that way (the card and the one it names are now one) is
    not an answer: its ref goes to ``unapplied`` (asked again, ADR-102) and is not recorded as answered."""
    from ..memory.address import follow_speakers

    by_ref = {i["ref"]: i for i in items}
    stats = {"speakers_changed": 0, "speakers_unknown": 0, "speakers_kept": 0, "genders_dropped": 0,
             "genders_kept": 0, "merges_doubted": 0, "ignored": 0}
    changed: dict[str, str | None] = {}  # lines whose speaker the second look changed → the speaker before
    from ..memory.db import utcnow as _now

    went = {} if went is None else went  # card merged away in this pass → the card it went into

    def now(cid: str) -> str:
        while cid in went:
            cid = went[cid]
        return cid

    def alive(cid: str) -> str | None:
        """The card an answer names, followed through this pass's merges; None if it is gone with no chain."""
        x = now(cid)
        return x if conn.execute("SELECT 1 FROM characters WHERE id=?", (x,)).fetchone() else None

    def unanswered(ref: str) -> None:  # nothing it names is there any more: asked again (ADR-102), not recorded
        conn.execute("DELETE FROM verify_answers WHERE book_id=? AND ref=?", (book_id, ref))
        if unapplied is not None and ref not in unapplied:
            unapplied.append(ref)
        stats["unapplied"] = stats.get("unapplied", 0) + 1

    def merge(keep: str, drop: str) -> None:
        repo.merge_characters(conn, keep, drop)
        went[drop] = keep

    for a in parsed.items:
        if (seen := by_ref.get(a.ref)) is not None and seen.get("_fp") and answer_valid(seen, a.answer.strip()) \
                and _rests_on(seen, a):  # an answer without the passage it rests on is asked again
            fp = seen["_fp_all"] if a.answer.strip() == "unknown" and seen.get("_fp_all") else seen["_fp"]
            conn.execute("INSERT OR REPLACE INTO verify_answers(book_id, ref, fingerprint, answer, created_at) VALUES "
                         "(?,?,?,?,?)", (book_id, a.ref, fp, a.answer.strip()[:120], _now()))
        item = by_ref.get(a.ref)
        if item is None:
            stats["ignored"] += 1
            continue
        kind, key = a.ref.split(":", 1)
        answer = a.answer.strip()
        if kind == "q":
            u = conn.execute("SELECT * FROM utterances WHERE id=?", (key,)).fetchone()
            if u is None or u["status"] == "human_verified":
                continue
            note = f"second look: {answer} — {a.reason[:160]}"
            if answer in item["candidates"] or (answer.startswith("c_") and conn.execute(
                    "SELECT 1 FROM characters WHERE id=? AND book_id=?", (answer, book_id)).fetchone()):
                new_id, status = alive(answer), "inferred"
                if new_id is None:
                    unanswered(a.ref)
                    continue
            elif answer == "unknown":
                new_id, status = None, "unknown"
            else:
                stats["ignored"] += 1
                continue
            _set_speaker(conn, key, new_id, status, note, stats, changed)
        elif kind == "g":
            if answer == item["first_answer"]:
                stats["genders_kept"] += 1
            else:  # a doubtful inference is not kept: the translation falls back to 你
                conn.execute("DELETE FROM character_gender WHERE character_id=? AND origin='llm' AND certainty='inferred'",
                             (key,))
                stats["genders_dropped"] += 1
        elif kind == "n":
            sc = conn.execute("SELECT state_json FROM scenes WHERE book_id=? AND id=?", (book_id, key)).fetchone()
            if sc is None or answer not in ("first", "third", "unclear"):
                stats["ignored"] += 1
                continue
            state = json.loads(sc[0] or "{}")
            narr = state.get("narration") or {}
            narr.update({"check": "verified", "person": answer} if answer != "unclear" else {"check": "conflict"})
            state["narration"] = narr
            conn.execute("UPDATE scenes SET state_json=? WHERE book_id=? AND id=?",
                         (json.dumps(state, ensure_ascii=False), book_id, key))
            stats["narration_checked"] = stats.get("narration_checked", 0) + 1
        elif kind == "u":  # the read pass's own question: the answer is kept with it (ADR-067)
            sid, _, idx = key.rpartition(":")
            sc = conn.execute("SELECT state_json FROM scenes WHERE book_id=? AND id=?", (book_id, sid)).fetchone()
            state = json.loads(sc[0] or "{}") if sc is not None else {}
            asked = state.get("uncertainties") or []
            if not answer or not idx.isdigit() or int(idx) >= len(asked) or asked[int(idx)].get("question") != \
                    item["question"]:
                stats["ignored"] += 1
                continue
            # the person it names as the card is after this look's merges; gone with nothing left: not an answer —
            # nothing is kept with the question, so it is asked again
            who_now = alive(a.character_id) if a.character_id in item["candidates"] else None
            if a.character_id in item["candidates"] and who_now is None:
                unanswered(a.ref)
                continue
            asked[int(idx)].update({"answer": answer[:300], "answer_reason": a.reason[:200],
                                    **({"answer_character_id": who_now} if who_now else {})})
            conn.execute("UPDATE scenes SET state_json=? WHERE book_id=? AND id=?",
                         (json.dumps(state, ensure_ascii=False), book_id, sid))
            stats["questions_answered"] = stats.get("questions_answered", 0) + 1
            # who says (or is told) a line of the question's paragraphs: only when the answer says which it is about
            # (``line_role``): the speaker through the speaker path, who it is said to as the line's addressee — the
            # lines it names, or the only one there; a person of the scene; unknown, or neither, leaves them
            lines = [x["line_ref"] for x in item.get("lines") or []]
            named = [x for x in dict.fromkeys([*a.line_refs, a.line_ref]) if x in lines]
            chosen = named or (lines if len(lines) == 1 else [])
            if a.line_role and answer != "unknown" and who_now is not None:
                note = f"second look (question): {answer[:80]} — {a.reason[:120]}"
                for line in chosen:
                    if a.line_role == "speaker":
                        _set_speaker(conn, line, who_now, "inferred", note, stats, changed)
                    else:
                        _set_addressee(conn, line, who_now, stats)
        elif kind == "a":  # who did an action the reading inferred and gave to someone absent
            sid, _, idx = key.rpartition(":")
            sc = conn.execute("SELECT state_json FROM scenes WHERE book_id=? AND id=?", (book_id, sid)).fetchone()
            state = json.loads(sc[0] or "{}") if sc is not None else {}
            acts = state.get("actions") or []
            who = [x.strip() for x in answer.split(",") if x.strip()] if answer != "unknown" else []
            cited = [c for c in a.evidence if c in item["_codes"]]
            if who and answer_valid(item, answer):  # the cards as they are after this pass's merges
                now_who = [alive(x) for x in who]
                if any(w is None for w in now_who):  # one is gone with nothing left: not an answer, asked again
                    unanswered(a.ref)
                    continue
                who = list(dict.fromkeys(w for w in now_who if w is not None))
            if not idx.isdigit() or int(idx) >= len(acts) or acts[int(idx)].get("summary", "") != item["_summary"] \
                    or not answer_valid(item, answer) or (who and not cited):
                stats["ignored"] += 1  # an answer without the passage it rests on changes nothing
                continue
            act = acts[int(idx)]
            before = act.get("subject_candidates") or []
            act["verified"] = True
            act["verify"] = {"before": before, "reason": a.reason[:200], "evidence": [item["_codes"][c] for c in cited]}
            if who == before:
                stats["actions_kept"] = stats.get("actions_kept", 0) + 1
            else:  # the reading's one-line summary names the one it guessed: replaced by the second look's own
                act["verify"]["summary_before"] = act.get("summary", "")
                act["summary"] = a.summary.strip()[:200]
                act["subject_candidates"] = who
                act["status"] = "inferred" if who else "unknown"
                stats["actions_changed"] = stats.get("actions_changed", 0) + 1
            conn.execute("UPDATE scenes SET state_json=? WHERE book_id=? AND id=?",
                         (json.dumps(state, ensure_ascii=False), book_id, sid))
        elif kind == "i":
            if answer in item["candidates"] and now(answer) != now(key):  # 「姉ちゃん」 in the sister's story is 高木
                _narrator_is(conn, book_id, now(key), now(answer))
                went[now(key)] = now(answer)
                stats["identities_merged"] = stats.get("identities_merged", 0) + 1
        elif kind == "s":  # a card known by words other cards have: one of them, or someone else (ADR-103)
            if not answer_valid(item, answer) or not _rests_on(item, a) or answer == "unknown":
                left = "same_person_open" if answer == "unknown" else "ignored"
                stats[left] = stats.get(left, 0) + 1
                continue  # stays marked: asked again when the card or the cards it may be change
            card, other = now(key), now(answer) if answer != "different" else ""
            c = conn.execute("SELECT * FROM characters WHERE id=? AND book_id=?", (card, book_id)).fetchone()
            if c is None or card == other or (answer == "different" and not repo.may_be(c)):
                # nothing left to do (an earlier answer of this pass made them one card): not an answer
                conn.execute("DELETE FROM verify_answers WHERE book_id=? AND ref=?", (book_id, a.ref))
                if unapplied is not None:
                    unapplied.append(a.ref)
                stats["same_person_unapplied"] = stats.get("same_person_unapplied", 0) + 1
                continue
            if answer == "different":  # holds for the cards it was asked about; a card not asked about is asked later
                asked = sorted({*json.loads(c["not_same_json"] or "[]"), *(now(x) for x in item["candidates"])})
                conn.execute("UPDATE characters SET may_be_json='[]', not_same_json=?, updated_at=? WHERE id=?",
                             (json.dumps(asked), utcnow(), card))
                stats["same_person_different"] = stats.get("same_person_different", 0) + 1
            else:  # the same person: one card from here on, its lines and memory moved over
                keep, drop = _same_as(conn, book_id, card, other)
                went[drop] = keep
                # settled: the person is that card, so not the other cards it was asked about (a re-read of the
                # scene finds the kept card and does not mark it again)
                row = conn.execute("SELECT not_same_json FROM characters WHERE id=?", (keep,)).fetchone()
                others = {now(x) for x in item["candidates"]} - {keep}
                conn.execute("UPDATE characters SET may_be_json=CASE WHEN ? THEN '[]' ELSE may_be_json END, "
                             "not_same_json=?, updated_at=? WHERE id=?",
                             (keep == card, json.dumps(sorted({*json.loads(row[0] or "[]"), *others})), utcnow(), keep))
                stats["same_person_merged"] = stats.get("same_person_merged", 0) + 1
        elif kind == "p":
            if answer.startswith("same:") and answer[5:].strip() in item["candidates"] \
                    and now(answer[5:].strip()) != now(key):
                merge(now(answer[5:].strip()), now(key))  # one group known by two descriptions
                stats["groups_merged"] = stats.get("groups_merged", 0) + 1
            else:
                members = [m.strip() for m in answer.split(",") if m.strip() in item["candidates"]]
                for m in members:
                    conn.execute("INSERT OR IGNORE INTO group_members(group_id, member_id, book_id, reason, created_at) "
                                 "VALUES (?,?,?,?,?)", (key, m, book_id, a.reason[:200], utcnow()))
                if members:
                    stats["groups_with_members"] = stats.get("groups_with_members", 0) + 1
        elif kind == "r":
            if answer in item["candidates"] and now(answer) != now(key):  # the narrator is this named character
                _narrator_is(conn, book_id, now(key), now(answer))
                went[now(key)] = now(answer)
                stats["narrators_matched"] = stats.get("narrators_matched", 0) + 1
            else:
                stats["narrators_kept"] = stats.get("narrators_kept", 0) + 1
        elif kind == "m":
            if answer != "same":
                unit = conn.execute("SELECT id FROM units WHERE book_id=? AND ordinal=?",
                                    (book_id, item.get("_ordinal") or 0)).fetchone()
                dropped = answer == "different"  # the merged description no longer names this character
                if dropped:
                    repo.drop_aliases(conn, key, item["merged_forms"])
                    stats["merges_undone"] = stats.get("merges_undone", 0) + 1
                finding(unit[0] if unit else None, "merge_doubted", "high",
                        f"{item['name']} ← {', '.join(item['merged_forms'])}: {answer} — {a.reason[:200]}"
                        + ("（這些稱呼已不再算作此角色；合併時搬過來的台詞請人工確認）" if dropped else ""))
                stats["merges_doubted"] += 1
    follow_speakers(conn, book_id, changed)
    repo.refresh_profiles(conn, book_id)
    return stats


def _set_speaker(conn: sqlite3.Connection, utterance_id: str, new_id: str | None, status: str, note: str,
                 stats: dict[str, int], changed: dict[str, str | None]) -> None:
    """The second look's speaker for a line: never stated (``inferred``, or ``unknown``), a person's choice kept."""
    u = conn.execute("SELECT * FROM utterances WHERE id=?", (utterance_id,)).fetchone()
    if u is None or u["status"] == "human_verified":
        return
    if new_id == u["speaker_id_nullable"] and status == u["status"]:
        stats["speakers_kept"] += 1
    else:
        stats["speakers_changed" if new_id else "speakers_unknown"] += 1
        if new_id != u["speaker_id_nullable"]:
            changed[utterance_id] = u["speaker_id_nullable"]
    notes = json.loads(u["notes_json"]) + [note]
    conn.execute("UPDATE utterances SET speaker_id_nullable=?, status=?, resolution_source='verify', notes_json=?, "
                 "updated_at=? WHERE id=?", (new_id, status, json.dumps(notes, ensure_ascii=False), utcnow(), utterance_id))
    if new_id != u["speaker_id_nullable"]:
        repo.speaker_changed_addressees(conn, utterance_id, new_id)
    repo.remove_speech_examples_for(conn, utterance_id)


def _set_addressee(conn: sqlite3.Connection, utterance_id: str, who: str, stats: dict[str, int]) -> None:
    """The second look's answer to who a line is said to: that person, read (``inferred``). A line whose addressee the
    text states, or whose speaker a person set, and a line said by that person themself, are left as they are."""
    u = conn.execute("SELECT * FROM utterances WHERE id=?", (utterance_id,)).fetchone()
    if u is None or u["status"] == "human_verified" or u["addressee_status"] == "explicit" \
            or u["speaker_id_nullable"] == who:
        return
    conn.execute("UPDATE utterances SET addressee_ids_json=?, addressee_status='inferred', updated_at=? WHERE id=?",
                 (json.dumps([who]), utcnow(), utterance_id))
    stats["addressees_changed"] = stats.get("addressees_changed", 0) + 1


def _narrator_is(conn: sqlite3.Connection, book_id: str, placeholder: str, named: str) -> None:
    """Fold the narrator card into the named character; the name stays hidden before it first appeared (ADR-032)."""
    from ..memory.entities import record_link

    c = conn.execute("SELECT * FROM characters WHERE id=?", (named,)).fetchone()
    if c is None:
        return
    unit = conn.execute("SELECT id FROM units WHERE book_id=? AND ordinal=?", (book_id, c["known_from_ordinal"])).fetchone()
    own = [c["canonical_name"], *json.loads(c["aliases_json"] or "[]")]
    repo.merge_characters(conn, named, placeholder)
    for form in own:
        record_link(conn, book_id, form, c["canonical_name"], "alias" if form != c["canonical_name"] else "official",
                    origin="narrator_match", status="accepted", unit_id=unit[0] if unit else None, quote=None,
                    ordinal=c["known_from_ordinal"])


def _same_as(conn: sqlite3.Connection, book_id: str, card: str, other: str) -> tuple[str, str]:
    """The second look found the card known by other cards' words to be ``other`` (ADR-103): one card from here on,
    lines and memory with it. Returns (the card kept, the card folded into it). When the kept card was first seen
    later, its own names stay hidden until its own first appearance: only the folded card's words are known from its
    place (ADR-032)."""
    from ..memory.entities import record_link
    from ..series import pos

    c = conn.execute("SELECT * FROM characters WHERE id=?", (card,)).fetchone()
    o = conn.execute("SELECT * FROM characters WHERE id=?", (other,)).fetchone()
    if c is None or o is None:
        return other, card
    # the card that stays keeps a real name: one named so (not known only by other cards' words), and of two such the
    # one whose name the book revealed (リリア over 「母親」); otherwise the card the answer names
    def rank(r: sqlite3.Row) -> tuple[bool, bool]:
        revealed = conn.execute("SELECT 1 FROM entity_links WHERE book_id=? AND target_term=? AND form_type='official' "
                                "AND status='accepted'", (book_id, r["canonical_name"])).fetchone() is not None
        return bool(repo.label_of(r)), not revealed

    keep, drop = (c, o) if rank(c) < rank(o) else (o, c)
    later = pos(conn, keep["book_id"], keep["known_from_ordinal"]) > pos(conn, drop["book_id"], drop["known_from_ordinal"])
    mine = {drop["canonical_name"], *json.loads(drop["aliases_json"] or "[]")}
    own = [f for f in [keep["canonical_name"], *json.loads(keep["aliases_json"] or "[]")] if f not in mine]
    repo.merge_characters(conn, keep["id"], drop["id"])
    # the kept card's own names stay hidden until its own first appearance (its forms keep their positions too)
    for form in own if later else []:
        record_link(conn, book_id, form, keep["canonical_name"],
                    "alias" if form != keep["canonical_name"] else "official", origin="same_person", status="accepted",
                    unit_id=keep["known_from_unit_id"], quote=None, ordinal=keep["known_from_ordinal"])
    return str(keep["id"]), str(drop["id"])
