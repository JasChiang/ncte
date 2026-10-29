"""Scene segmentation (deterministic, spec §4.2/§5.2) and LLM scene analysis (M3)."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from ..epub.units import in_source, source_form, text_forms
from ..llm.schemas import SceneAnalysisOutput
from ..memory import repository as repo
from ..memory import visibility as vis
from ..memory.db import Transaction, utcnow
from ..series import SERIES_SCOPE
from .dialogue import FIRST_PERSON, SECOND_PERSON, Quote, UnitText, clean, find_quotes
from .reference_resolution import validate_actions
from .speaker import Candidate, LlmClaim, Resolution, resolve

BODY_KINDS = ("text", "heading")


@dataclass
class Scene:
    id: str
    seq: int
    spine_index: int | None
    unit_ids: list[str]
    start_ordinal: int
    end_ordinal: int
    break_reason: str
    has_body: bool = False


def segment_units(rows: list[sqlite3.Row], overrides: dict[str, str], max_units: int,
                  breaks: dict[str, str] | None = None, *, chapters: dict[str, bool] | None = None) -> list[Scene]:
    """``chapters`` (first unit of each TOC chapter → whether the model has cut it): a chapter always starts a scene,
    and inside a chapter the model cut, the model's breaks are the scenes (ADR-057, ADR-059: the program does not cut
    them further; a long scene is read in parts, not divided). A chapter not cut by the model (book matter, or a
    chapter whose cutting failed) keeps a provisional cut at the author's marks; a heading stays with the text after
    it."""
    breaks = breaks or {}
    chapters = chapters or {}
    scenes: list[Scene] = []
    per_spine: dict[int | None, int] = {}
    cur: Scene | None = None
    prev_href: str | None = None
    prev_row: sqlite3.Row | None = None
    decided_chapter = False
    for r in rows:
        reason = None
        if cur is None or r["id"] in chapters:
            reason = "chapter" if chapters else "document"
            decided_chapter = chapters.get(r["id"], False)
        elif decided_chapter:
            # a model's cut right after the chapter heading would leave the heading a scene of its own (read and
            # summarised alone): the heading stays with the text after it, as in a chapter the model did not cut. A
            # person's split is kept
            if overrides.get(r["id"]) == "split" or (breaks.get(r["id"]) == "llm" and cur is not None and cur.has_body):
                reason = "manual" if overrides.get(r["id"]) == "split" else "llm"
        elif r["resource_href"] != prev_href:
            reason = "document"
        elif overrides.get(r["id"]) == "split":
            reason = "manual"
        elif overrides.get(r["id"]) == "merge":
            reason = None
        elif r["is_separator"]:
            reason = "separator"
        elif r["kind"] == "heading" and cur.has_body:
            reason = "heading"
        elif not cur.has_body:
            reason = None  # only a heading so far: the text after it belongs with it
        elif r["blank_before"] >= 2:
            reason = "blank_lines"
        elif breaks.get(r["id"]) == "llm":
            reason = "llm"
        elif len(cur.unit_ids) >= max_units and (_calm(r, prev_row) or len(cur.unit_ids) >= max_units + MAX_SIZE_SLACK):
            reason = "max_size"  # provisional only: a chapter the model has not cut
        if reason:
            n = per_spine.get(r["spine_index"], 0) + 1
            per_spine[r["spine_index"]] = n
            sp = r["spine_index"] if r["spine_index"] is not None else 999
            cur = Scene(f"s_{sp:03d}_{n:02d}", len(scenes), r["spine_index"], [], r["ordinal"], r["ordinal"], reason)
            scenes.append(cur)
        assert cur is not None
        cur.unit_ids.append(r["id"])
        cur.has_body = cur.has_body or r["kind"] != "heading"
        cur.end_ordinal = r["ordinal"]
        prev_href = r["resource_href"]
        prev_row = r
    return scenes


MAX_SIZE_SLACK = 30  # how far past the size limit a cut may wait for a quiet point


def _calm(r: sqlite3.Row, prev: sqlite3.Row | None) -> bool:
    """A cut forced by size waits until it falls between two narration paragraphs (not inside an exchange of lines)."""
    def talk(x: sqlite3.Row | None) -> bool:
        t = (x["plain_text"] if x is not None else "").lstrip(" 　")
        return t.startswith(("「", "『")) or "」" in t[-3:]
    return not talk(r) and not talk(prev)


def ensure_chapters(conn: sqlite3.Connection, book_id: str) -> None:
    """Chapters from the TOC for a book that has none yet (imported before chapters came from the TOC, too)."""
    if conn.execute("SELECT 1 FROM chapters WHERE book_id=? LIMIT 1", (book_id,)).fetchone() is not None:
        return
    from ..epub.chapters import chapter_starts, save_chapters
    from ..epub.parser import load_epub

    path = conn.execute("SELECT stored_path FROM books WHERE id=?", (book_id,)).fetchone()
    starts = chapter_starts(conn, book_id, load_epub(path[0])) if path and path[0] and Path(path[0]).exists() else []
    save_chapters(conn, book_id, starts, "toc" if starts else "none")


def chapter_starts_for(conn: sqlite3.Connection, book_id: str, rows: list[sqlite3.Row]) -> dict[str, bool] | None:
    """First unit of each chapter → whether the model has cut it (None: the book is cut the old way). Chapters come
    from the TOC; a book without one is a single chapter until the model finds its chapters."""
    ensure_chapters(conn, book_id)
    decided = {r[0] for r in conn.execute("SELECT unit_id FROM scene_breaks WHERE book_id=? AND origin='chapter'",
                                          (book_id,))}
    out: dict[str, bool] = {}
    bounds = conn.execute("SELECT start_ordinal, end_ordinal FROM chapters WHERE book_id=? ORDER BY seq",
                          (book_id,)).fetchall()
    for start, end in bounds:
        first = next((r for r in rows if start <= r["ordinal"] <= end), None)
        if first is not None:
            out[first["id"]] = first["id"] in decided
    return out


def segment_book(conn: sqlite3.Connection, cfg: Config, book_id: str) -> list[Scene]:
    rows = conn.execute(
        f"SELECT * FROM units WHERE book_id=? AND kind IN ({','.join('?' * len(BODY_KINDS))}) "
        "AND spine_index IS NOT NULL ORDER BY ordinal", (book_id, *BODY_KINDS)).fetchall()
    overrides = {r["unit_id"]: r["action"] for r in
                 conn.execute("SELECT unit_id, action FROM scene_overrides WHERE book_id=?", (book_id,))}
    breaks = {r["unit_id"]: r["origin"] for r in conn.execute("SELECT unit_id, origin FROM scene_breaks WHERE book_id=?",
                                                              (book_id,))}
    from .memory_update import forget_scene

    with Transaction(conn):
        chapters = chapter_starts_for(conn, book_id, rows)
        scenes = segment_units(rows, overrides, cfg.memory.max_scene_units, breaks, chapters=chapters)
        existing = {r["id"]: r for r in conn.execute("SELECT * FROM scenes WHERE book_id=?", (book_id,))}
        conn.execute("UPDATE units SET scene_id=NULL WHERE book_id=?", (book_id,))
        keep: set[str] = set()
        kinds = {r[0]: r[1] for r in conn.execute("SELECT unit_id, kind FROM scene_breaks WHERE book_id=?", (book_id,))}
        for s in scenes:
            old = existing.get(s.id)
            if old and old["start_unit_id"] == s.unit_ids[0] and old["end_unit_id"] == s.unit_ids[-1]:
                conn.execute("UPDATE scenes SET seq=?, break_reason=? WHERE book_id=? AND id=?",
                             (s.seq, s.break_reason, book_id, s.id))
            else:
                if old is not None and int(old["version"]) > 0:  # what reading the old range left no longer fits
                    forget_scene(conn, book_id, s.id)
                conn.execute("DELETE FROM scenes WHERE book_id=? AND id=?", (book_id, s.id))
                conn.execute(
                    "INSERT INTO scenes(book_id, id, seq, spine_index, start_unit_id, end_unit_id, start_ordinal, "
                    "end_ordinal, break_reason) VALUES (?,?,?,?,?,?,?,?,?)",
                    (book_id, s.id, s.seq, s.spine_index, s.unit_ids[0], s.unit_ids[-1], s.start_ordinal,
                     s.end_ordinal, s.break_reason))
            keep.add(s.id)
            conn.executemany("UPDATE units SET scene_id=? WHERE id=?", [(s.id, u) for u in s.unit_ids])
            k = kinds.get(s.unit_ids[0])
            conn.execute("UPDATE scenes SET kind=? WHERE book_id=? AND id=?", (k, book_id, s.id))
        for sid in existing:
            if sid not in keep:
                if int(existing[sid]["version"]) > 0:
                    forget_scene(conn, book_id, sid)
                conn.execute("DELETE FROM scenes WHERE book_id=? AND id=?", (book_id, sid))
        # lines already read follow their paragraph into its new scene (re-read later with the new scene)
        conn.execute("UPDATE utterances SET scene_id=(SELECT scene_id FROM units WHERE units.id=utterances.unit_id) "
                     "WHERE book_id=?", (book_id,))
    return scenes


# --------------------------------------------------------------------------- LLM scene analysis (M3/M4)


MAX_NAME_LEN = 24
# a pronoun is never a character name or alias («私» is everybody's first person)
PRONOUNS = set(FIRST_PERSON) | set(SECOND_PERSON) | {"彼", "彼女", "あいつ", "こいつ", "そいつ", "奴", "やつ"}


def _standalone(alias: str, texts: str | list[str], longer_names: list[str]) -> bool:
    """True if ``alias`` occurs in ``texts`` (a text, or several looked in each alone: ``text_forms``) outside every
    longer name that contains it (カイ vs カイト)."""
    longer = sorted({n for n in longer_names if alias in n and n != alias}, key=len, reverse=True)
    for text in [texts] if isinstance(texts, str) else texts:
        for n in longer:
            text = text.replace(n, "\0")
        if alias in text:
            return True
    return False


def _valid_name(name: str) -> bool:
    return bool(name) and len(name) <= MAX_NAME_LEN and not any(ch in name for ch in "「」『』") and name not in PRONOUNS


MIN_FORM_LEN = 2  # words a person is known by, besides a name the model gives: one character (「男」「父」) names anyone
# what a scene's state keeps for the record only, never sent back to a model: which of the model's new characters the
# program took for an earlier card by the words it gives (``matched_by_words``) and which cards it made for people known
# by several cards' words (``unconfirmed_cards``). The label the model gave a person the book does not name is kept with
# the card (``character_notes``, ADR-103), not here
RECORD_ONLY = ("matched_by_words", "unconfirmed_cards")


_KIND_ORDER = ("name", "nickname", "title", "description")
NOT_A_NAME = ("title", "description")  # words a card may be known by until the person's name is given


def _said_alias(nc: Any, evidence: list[str]) -> str | None:
    """The book's words a new person is known by when ``name_ja`` is not in its evidence: of the aliases found in the
    evidence paragraphs, a name first, then a nickname, a title, a description, in the model's order otherwise
    (what each alias is, is the model's reading). A common noun, a pronoun or a single character (「男」, as other forms
    of a person are held to two) names anyone and is never one."""
    kinds = {source_form(k.ja): k.kind for k in nc.alias_kinds}
    found = [a for a in dict.fromkeys(source_form(x) for x in nc.aliases_ja)
             if _valid_name(a) and len(a) >= MIN_FORM_LEN and kinds.get(a) != "common"
             and any(in_source(a, t) for t in evidence)]
    order = {a: _KIND_ORDER.index(k) if (k := kinds.get(a)) in _KIND_ORDER else len(_KIND_ORDER) for a in found}
    return min(found, key=lambda a: order[a]) if found else None


def scene_rows(conn: sqlite3.Connection, book_id: str, scene_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM units WHERE book_id=? AND scene_id=? ORDER BY ordinal",
                        (book_id, scene_id)).fetchall()


def get_scene(conn: sqlite3.Connection, book_id: str, scene_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM scenes WHERE book_id=? AND id=?", (book_id, scene_id)).fetchone()
    if row is None:
        raise KeyError(scene_id)
    return row


def _unit_texts(rows: list[sqlite3.Row]) -> list[UnitText]:
    return [UnitText(r["id"], clean(r["plain_text"]), r["ordinal"]) for r in rows]


def forget_scene_cards(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row, *, memory_too: bool) -> list[str]:
    """Before a scene is read again: the cards its last reading made that nothing else uses are dropped, so the new
    reading starts as the first one did (a 「姉ちゃん」 card left from it drew the program's action matching away from
    the character the model named). Kept: cards with anything outside this scene, anything a person decided (a chosen
    rendering, a gender or a speaker set by hand), and — unless the scene's memory is read again too — anything the
    memory step wrote (facts, relations noted, forms of address, group members). Returns the dropped ids."""
    lo, hi = scene["start_ordinal"], scene["end_ordinal"]
    dropped = []
    for c in conn.execute("SELECT * FROM characters WHERE book_id=? AND known_from_ordinal BETWEEN ? AND ? AND "
                          "coalesce(origin,'') NOT IN ('human','user')", (book_id, lo, hi)).fetchall():
        cid = c["id"]

        def has(sql: str, *args: Any) -> bool:
            return conn.execute(sql, args).fetchone() is not None

        if has("SELECT 1 FROM utterances u JOIN units x ON x.id=u.unit_id WHERE u.speaker_id_nullable=? AND "
               "(x.ordinal NOT BETWEEN ? AND ? OR u.status='human_verified')", cid, lo, hi) \
                or has("SELECT 1 FROM character_gender WHERE character_id=? AND origin='human'", cid) \
                or has("SELECT 1 FROM naming_proposals WHERE kind='character' AND ref=? AND status='decided'", cid) \
                or (not c["label"] and has("SELECT 1 FROM glossary WHERE book_id=? AND category='character' AND "
                                           "source_term=? AND (verified=1 OR origin='user')", book_id, c["canonical_name"])) \
                or has("SELECT 1 FROM relations WHERE (a_id=? OR b_id=?) AND origin!='llm'", cid, cid):
            continue
        memory = [("SELECT 1 FROM facts WHERE (subject=? OR object_ref=?) AND retracted_by IS NULL AND "
                   "first_revealed_ordinal NOT BETWEEN ? AND ?", (cid, cid, lo, hi)),
                  ("SELECT 1 FROM relation_notes WHERE (a_id=? OR b_id=?) AND first_ordinal NOT BETWEEN ? AND ?",
                   (cid, cid, lo, hi)),
                  ("SELECT 1 FROM address_terms WHERE (speaker_id=? OR addressee_id=?) AND (first_ordinal NOT BETWEEN ? AND ? "
                   "OR zh_origin='human')", (cid, cid, lo, hi)),
                  ("SELECT 1 FROM group_members WHERE (group_id=? OR member_id=?) AND coalesce(scene_id,'')!=?",
                   (cid, cid, scene["id"]))]
        if any(has(q, *a) for q, a in memory):
            continue
        if not memory_too and (has("SELECT 1 FROM facts WHERE subject=? OR object_ref=?", cid, cid)
                               or has("SELECT 1 FROM relation_notes WHERE a_id=? OR b_id=?", cid, cid)
                               or has("SELECT 1 FROM address_terms WHERE speaker_id=? OR addressee_id=?", cid, cid)
                               or has("SELECT 1 FROM group_members WHERE group_id=? OR member_id=?", cid, cid)):
            continue  # the memory step's notes about this card stay, and so does the card
        for sql in ("UPDATE utterances SET speaker_id_nullable=NULL WHERE speaker_id_nullable=?",
                    "DELETE FROM speaker_hypotheses WHERE candidate_character_id=?",
                    "DELETE FROM speech_examples WHERE character_id=?",
                    "DELETE FROM character_gender WHERE character_id=?",
                    "DELETE FROM character_impressions WHERE character_id=?",
                    "DELETE FROM naming_proposals WHERE kind='character' AND ref=?",
                    "UPDATE entities SET character_id=NULL WHERE character_id=?"):
            conn.execute(sql, (cid,))
        for sql in ("DELETE FROM facts WHERE subject=? OR object_ref=?", "DELETE FROM relation_notes WHERE a_id=? OR b_id=?",
                    "DELETE FROM address_terms WHERE speaker_id=? OR addressee_id=?",
                    "DELETE FROM address_usage WHERE speaker_id=? OR addressee_id=?",
                    "DELETE FROM group_members WHERE group_id=? OR member_id=?",
                    "DELETE FROM relations WHERE a_id=? OR b_id=?"):
            conn.execute(sql, (cid, cid))
        if not c["label"] and not has(f"SELECT 1 FROM characters WHERE {SERIES_SCOPE} AND canonical_name=? AND id!=?",
                                      book_id, c["canonical_name"], cid):
            # the words' entry; a card known by other cards' words (ADR-103) never made one, and the others keep it
            conn.execute("DELETE FROM glossary WHERE book_id=? AND category='character' AND source_term=? AND verified=0 "
                         "AND coalesce(origin,'')!='user'", (book_id, c["canonical_name"]))
        repo.forget_card_records(conn, cid)
        conn.execute("DELETE FROM characters WHERE id=?", (cid,))
        for col in ("may_be_json", "not_same_json"):
            for row in conn.execute(f"SELECT id, {col} FROM characters WHERE instr({col}, ?) > 0", (cid,)).fetchall():
                conn.execute(f"UPDATE characters SET {col}=?, updated_at=? WHERE id=?",
                             (json.dumps([x for x in json.loads(row[1]) if x != cid]), utcnow(), row[0]))
        dropped.append(cid)
    return dropped


def build_analysis_payload(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row,
                           glossary: list[dict[str, Any]], *, card_voice: bool = True,
                           scene_state: bool = False) -> dict[str, Any]:
    """The reading request of a scene. ``card_voice`` (ADR-104): each card carries the program's tally of its earlier
    lines and a few facts known by then (``card_evidence``); ``scene_state``: the model is asked to picture the scene
    (``situation``) before it attributes the lines, and is shown how the scene before was left."""
    rows = scene_rows(conn, book_id, scene["id"])
    known = vis.visible_characters(conn, book_id, scene["start_ordinal"] - 1)
    forms_at = vis.form_positions(conn, book_id)
    texts = _unit_texts(rows)
    quotes = find_quotes(texts)
    # the lines found by their brackets (structure); who says them is the model's reading
    q_payload = [{"unit_id": q.unit_id, "quote_index": q.index, "text": q.text[:200],
                  "continues_previous_quote": q.continuation_of is not None} for q in quotes]
    seen = text_forms(" ".join(r["plain_text"] for r in rows))  # a term is the base text or a reading
    from .narration import hint as narration_hint

    prev = conn.execute("SELECT state_json FROM scenes WHERE book_id=? AND seq<? AND version>0 ORDER BY seq DESC LIMIT 1",
                        (book_id, scene["seq"])).fetchone()
    prev_state = json.loads(prev[0] or "{}") if prev else {}
    prev_narr = prev_state.get("narration")
    extra: dict[str, Any] = {"narration_hint": narration_hint(rows)}
    if prev_narr and prev_narr.get("person"):
        extra["previous_narration"] = {"person": prev_narr["person"], **{
            f"{k}_ref": prev_narr[k] for k in ("narrator", "focal") if prev_narr.get(k)}}
    from .narration import established_self_forms

    if forms := established_self_forms(conn, book_id, scene["seq"]):
        extra["established_self_forms"] = forms
    kin = _relations_so_far(conn, book_id, scene["start_ordinal"])
    extra.update(story_so_far(conn, book_id, scene))
    if "kind" in scene.keys() and scene["kind"] and scene["kind"] != "present":
        extra["scene_kind"] = scene["kind"]  # flashback / parallel / summary / other, as the scene was cut
    views = [(c, v) for c in known if (v := vis.character_view(conn, book_id, c, scene["start_ordinal"] - 1, forms_at))]
    # cards the book calls by the same words (two unnamed mothers): told apart by where each was first seen (ADR-103);
    # so is a card known by a title or a description (「給仕」): the word may be anyone holding the post, and where the
    # card was seen is what tells the model whether this is that person (ADR-110)
    shared = vis.shared_names({c["id"]: v["name_ja"] for c, v in views}) | {
        c["id"] for c, _ in views if c["name_kind"] in NOT_A_NAME}
    hints = vis.tell_apart(conn, book_id, [c for c, _ in views if c["id"] in shared], scene["start_ordinal"] - 1) \
        if shared else {}
    voice = card_evidence(conn, book_id, scene, rows, views) if card_voice else {}
    if scene_state:
        extra["write_situation"] = True  # the prompt's block asking for ``situation`` (ADR-104)
        if last := previous_situation(prev_state, {c["id"] for c, _ in views}):
            extra["previous_scene_state"] = last
    return {
        **extra,
        "task": "scene_analyze", "scene_id": scene["id"],
        "units": [{"unit_id": r["id"], "source": r["plain_text"]} for r in rows],
        "quotes": q_payload,
        "known_characters": [{"character_id": c["id"], **v, "speech_profile": json.loads(c["speech_profile_json"] or "{}"),
                              **({"relations": kin[c["id"]]} if kin.get(c["id"]) else {}),
                              **hints.get(c["id"], {}), **voice.get(c["id"], {})}
                             for c, v in views],
        "glossary": [g for g in glossary if any(str(g.get("ja", "")) in f for f in seen)],
    }


CARD_FACTS = 3  # evidence-backed facts given per visible card (ADR-104)
CARD_FORMS = 4  # forms of address others are recorded calling a card, given per card


def _uses_before(conn: sqlite3.Connection, address_id: int, book_id: str, before: int) -> int:
    """How many lines a recorded form of address is linked to (``address_observations``) before the story position
    (book ``book_id``, ordinal ``before``) across the series: earlier volumes, and this volume before the ordinal —
    never a later volume, nor a book outside the series (an ordinal alone means nothing across volumes)."""
    from ..series import pos, scope_clause, volumes

    here = pos(conn, book_id, before)
    vols = volumes(conn)
    where, ids = scope_clause(conn, book_id, "n.book_id")
    rows = conn.execute("SELECT n.book_id, n.ordinal FROM address_observations o JOIN utterances t ON "
                        f"t.id=o.utterance_id JOIN units n ON n.id=t.unit_id WHERE o.address_id=? AND {where}",
                        (address_id, *ids)).fetchall()
    return sum(1 for b, o in rows if (vols.get(b, 1.0), int(o)) < here)


def _tally(found: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    return {w: {"explicit": n.get("explicit", 0), "inferred": n.get("inferred", 0)}
            for w, n in sorted(found.items(), key=lambda x: -sum(x[1].values()))}


def card_evidence(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row, rows: list[sqlite3.Row],
                  views: list[tuple[Any, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """What a reader remembers of each person on reaching this scene (ADR-104), as the program's tally of the readings
    recorded before it — never from the scene's start on, and the earlier readings may be wrong:

    - ``earlier_lines``: how many lines before this scene were read as theirs, by how sure that reading was (explicit:
      stated in the text, or set by a person; inferred: an earlier model reading), and in those lines the
      self-reference words (the first word of ``FIRST_PERSON`` found, as ``speech_profile`` counts, and any self-form
      the memory step recorded for them), the second-person word (``SECOND_PERSON``, the same way) and the forms of
      address the memory step recorded them using for others (``self`` / ``you`` / ``calls``). Strings counted in the
      quoted text; what a word means is the model's reading.
    - ``called_as``: forms of address the memory step recorded others using for them, first used before this scene.
    - ``facts_so_far``: up to ``CARD_FACTS`` facts about them revealed before this scene that carry their source
      sentence, the ones nearest this scene's text first (the translation step's ``chosen_facts`` ranking).

    A card with nothing recorded gets no key."""
    from ..epub.units import match_text
    from ..memory.address import calls_by, visible_terms
    from ..memory.retrieval import chosen_facts

    start = scene["start_ordinal"]
    forms_at = vis.form_positions(conn, book_id)
    names = vis.visible_names(conn, book_id, start - 1)
    facts = vis.visible_facts(conn, book_id, start)
    terms = [r for r in visible_terms(conn, book_id, start) if r["book_id"] == book_id]
    text = match_text(" ".join(r["plain_text"] for r in rows))
    statuses = {"explicit": "explicit", "human_verified": "explicit", "inferred": "inferred"}
    out: dict[str, dict[str, Any]] = {}
    for c, v in views:
        cid = c["id"]
        own_self = {r["form_ja"] for r in terms if r["speaker_id"] == cid == r["addressee_id"]}
        own_calls = {r["form_ja"] for r in terms if r["speaker_id"] == cid and calls_by(conn, r)}
        lines: dict[str, int] = {}
        found: dict[str, dict[str, dict[str, int]]] = {"self": {}, "you": {}, "calls": {}}
        for u in conn.execute("SELECT t.quote_text, t.status FROM utterances t JOIN units n ON n.id=t.unit_id "
                              "WHERE t.book_id=? AND t.speaker_id_nullable=? AND n.ordinal<?", (book_id, cid, start)):
            st = statuses.get(u["status"])
            if st is None:
                continue
            lines[st] = lines.get(st, 0) + 1
            said = clean(u["quote_text"]).strip("「」『』 　")
            words = {"self": {next((w for w in FIRST_PERSON if w in said), ""), *(f for f in own_self if f in said)},
                     "you": {next((w for w in SECOND_PERSON if w in said), "")},
                     "calls": {f for f in own_calls if f in said}}
            for kind, ws in words.items():
                for w in ws - {""}:
                    n = found[kind].setdefault(w, {})
                    n[st] = n.get(st, 0) + 1
        extra: dict[str, Any] = {}
        if lines:
            extra["earlier_lines"] = {"lines": {"explicit": lines.get("explicit", 0), "inferred": lines.get("inferred", 0)},
                                      **{k: _tally(f) for k, f in found.items() if f}}
        # ranked by the uses seen before this scene (the lines a form is linked to), never by how often the book uses
        # it later: an earlier scene's request does not depend on later dialogue (a form first used before the scene
        # but linked to no earlier line counts once)
        called = sorted((r for r in terms if r["addressee_id"] == cid and r["speaker_id"] != cid
                         and r["speaker_id"] in names and calls_by(conn, r)),
                        key=lambda r: (-max(1, _uses_before(conn, r["id"], book_id, start)), r["first_ordinal"],
                                       r["id"]))
        if called:
            extra["called_as"] = [{"ja": r["form_ja"], "by": names[r["speaker_id"]]} for r in called[:CARD_FORMS]]
        mine = [f for f in facts if f["subject"] == cid and (f["evidence_quote"] or "").strip()]
        if mine:
            # which facts are about them is found by the words that find the card (lookup mode), never by a form
            # kept for display only (ADR-103)
            lv = vis.character_view(conn, book_id, c, start - 1, forms_at, lookup=True) or v
            present = [{"character_id": cid, "name_ja": lv["name_ja"], "aliases_ja": lv["aliases_ja"]}]
            got = chosen_facts(mine, text, present, names, limit=CARD_FACTS, min_score=0.0)
            if got:
                extra["facts_so_far"] = [{k: x for k, x in item.items() if k != "subject"} for _, item in got]
        if extra:
            out[cid] = extra
    return out


MAX_SITUATION = 8  # points of a scene's picture kept (ADR-104)
MAX_SITUATION_PEOPLE = 8
SITUATION_CHARS = 40  # what one person is doing, and the place, as kept


def previous_situation(state: dict[str, Any], visible: set[str]) -> dict[str, Any] | None:
    """How the scene before was left, as its reading pictured it (its last point): the cards in it that this request
    can show, the others by the text's words. Context for the opening lines, never evidence."""
    points = state.get("situation") or []
    if not points:
        return None
    last = points[-1]
    people = [{**({"ref": p["character_id"]} if p.get("character_id") in visible else {"who": p["who"]}),
               "doing": p.get("doing", "")}
              for p in last.get("people") or [] if p.get("character_id") in visible or p.get("who")]
    if not people and not last.get("place"):
        return None
    return {**({"place": last["place"]} if last.get("place") else {}), "people": people}


def situation_points(points: list[Any], rows: list[sqlite3.Row], ref_map: dict[str, str],
                     rej: Any) -> list[dict[str, Any]]:
    """The model's picture of the scene (ADR-104), as kept: each point starts at a paragraph of this scene (in story
    order; a point at a paragraph outside it is dropped), its people a card the reading knows (``ref``) or the text's
    words for someone without one (``who``); a person with a ref the reading does not know and no words is dropped.
    Short by construction: at most ``MAX_SITUATION`` points of ``MAX_SITUATION_PEOPLE`` people."""
    pos = {r["id"]: i for i, r in enumerate(rows)}
    out: list[tuple[int, dict[str, Any]]] = []
    for pt in points:
        if pt.unit_id not in pos:
            rej(pt.model_dump(), "situation point at a paragraph outside the scene")
            continue
        people = []
        for p in pt.people[:MAX_SITUATION_PEOPLE]:
            cid = ref_map.get(p.ref) if p.ref else None
            who = p.who.strip()[:MAX_NAME_LEN]
            if cid is None and not who:
                if p.ref:
                    rej(p.model_dump(), "unknown situation ref")
                continue
            people.append({**({"character_id": cid} if cid else {"who": who}), "doing": p.doing.strip()[:SITUATION_CHARS]})
        out.append((pos[pt.unit_id], {"from": pt.unit_id, "place": pt.place.strip()[:SITUATION_CHARS], "people": people}))
    out.sort(key=lambda x: x[0])
    kept: list[dict[str, Any]] = []
    for _, p in out:
        if kept and kept[-1]["from"] == p["from"]:
            kept[-1] = p  # two points at one paragraph: the later one written
        else:
            kept.append(p)
    return kept[:MAX_SITUATION]


def situation_at(state: dict[str, Any], rows_pos: dict[str, int], unit_id: str) -> dict[str, Any] | None:
    """The point of a scene's picture in force at paragraph ``unit_id`` (the last one starting at or before it)."""
    at = rows_pos.get(unit_id)
    if at is None:
        return None
    best = None
    for p in state.get("situation") or []:
        if p.get("from") in rows_pos and rows_pos[p["from"]] <= at:
            best = p
    return best


STORY_SUMMARIES = 3  # the last scene summaries before this scene
PREVIOUS_END_CHARS = 400  # and the last paragraphs of the scene before, up to this many characters


def story_so_far(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row) -> dict[str, Any]:
    """What a reader knows on reaching this scene: the recap of the chapter before, the summaries of this chapter's
    last scenes and how the scene before ends. Without them an untagged 「『賢者』が死んだ？」 opening a scene, or a promise remembered from an earlier
    scene (who said 「決着をつけよう」), was a guess. Only scenes already read; context, never evidence. A paragraph
    the content filter refused is left out (it would get this scene refused as well)."""
    from ..memory.retrieval import story_so_far as so_far

    out: dict[str, Any] = {}
    same, recap = so_far(conn, book_id, scene)
    if recap:
        out["previous_chapter"] = recap
    if same[-STORY_SUMMARIES:]:
        out["story_so_far"] = same[-STORY_SUMMARIES:]
    prev = conn.execute("SELECT id FROM scenes WHERE book_id=? AND seq<? AND version>0 ORDER BY seq DESC LIMIT 1",
                        (book_id, scene["seq"])).fetchone()
    if prev is not None:
        tail: list[str] = []
        used = 0
        # a paragraph the content filter refused there would get this scene refused too
        for r in conn.execute("SELECT plain_text FROM units u WHERE book_id=? AND scene_id=? AND kind='text' AND NOT "
                              "EXISTS (SELECT 1 FROM qa_findings f WHERE f.unit_id=u.id AND f.type='read_blocked') "
                              "ORDER BY ordinal DESC", (book_id, prev[0])):
            if tail and used + len(r[0]) > PREVIOUS_END_CHARS:
                break
            tail.insert(0, r[0])
            used += len(r[0])
        if tail:
            out["previous_scene_end"] = tail
    return out


def _relations_so_far(conn: sqlite3.Connection, book_id: str, before: int) -> dict[str, list[str]]:
    """Relations noted before this scene, by character: 「高木的妹妹」 tells the reader of the sister's own story who
    「姉ちゃん」 is (a translator knows who is whose sister)."""
    names = {r["id"]: repo.model_name(r) for r in repo.characters(conn, book_id)}
    out: dict[str, list[str]] = {}
    for r in conn.execute("SELECT a_id, b_id, a_is, b_is, relation_zh FROM relation_notes WHERE book_id=? AND first_ordinal<? "
                          "UNION SELECT a_id, b_id, a_is, b_is, relation_zh FROM relations WHERE book_id=? AND since_ordinal<? "
                          "AND (until_ordinal IS NULL OR until_ordinal>=?)", (book_id, before, book_id, before, before)):
        for me, other, role in ((r[0], r[1], r[2]), (r[1], r[0], r[3])):
            if me in names and other in names:
                line = f"{names[other]}的{role or r[4]}"
                if line not in out.setdefault(me, []):
                    out[me].append(line)
    return {k: v[:6] for k, v in out.items()}


def _revealed(known: list[Any], book_id: str, same_as: str | None) -> str | None:
    """The known character a newly named one turns out to be: the model's ``same_as_ref`` (a known character of this
    book). The program does not match names or descriptions itself (ADR-059)."""
    mine = {c["id"] for c in known if c["book_id"] == book_id}
    return same_as if same_as in mine else None


_QUOTED = re.compile(r"「[^」]*」|『[^』]*』")  # what is said inside a paragraph (a speech tag is outside it)
NEW_NARRATOR = "new_narrator"
NARRATOR_NAME = "（敘述者）"


def new_narrator(conn: sqlite3.Connection, book_id: str, first: sqlite3.Row) -> str:
    """A placeholder character for an unnamed first-person narrator of this volume (never linked across volumes)."""
    n = conn.execute("SELECT COUNT(*) FROM characters WHERE book_id=? AND is_narrator=1", (book_id,)).fetchone()[0]
    name = NARRATOR_NAME if n == 0 else f"（敘述者{n + 1}）"
    cid = repo.character_id_for(book_id, name)
    conn.execute("INSERT OR IGNORE INTO characters(id, book_id, canonical_name, display_name_zh, aliases_json, "
                 "known_from_unit_id, known_from_ordinal, status, origin, created_at, updated_at, is_narrator) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                 (cid, book_id, name, name, "[]", first["id"], first["ordinal"], "partial", "narrator", utcnow(), utcnow()))
    return cid


def apply_scene_analysis(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row,
                         parsed: SceneAnalysisOutput | None, *, run_id: str | None = None,
                         refs: dict[str, str] | None = None) -> dict[str, int]:
    """Validate an analysis against the source and persist it. ``parsed=None`` → program-only analysis. ``refs``
    (when given) receives which card each id of the answer became (``new_1`` → the card made or matched for it)."""
    sid = scene["id"]
    rows = scene_rows(conn, book_id, sid)
    texts = _unit_texts(rows)
    unit_text = {t.id: t.text for t in texts}
    unit_src = {r["id"]: r["plain_text"] for r in rows}  # as stored: the model's words are found in a reading too
    # what the model's words are looked for in: each paragraph as the base text and as read, each alone
    scene_text = [f for t in unit_src.values() for f in text_forms(t)]
    stats = {"new_characters": 0, "rejected": 0, "explicit": 0, "inferred": 0, "unknown": 0, "examples": 0,
             "human_kept": 0}
    known = vis.visible_characters(conn, book_id, scene["start_ordinal"] - 1)
    ref_map: dict[str, str] = {c["id"]: c["id"] for c in known}
    names_of: dict[str, list[str]] = {c["id"]: [c["canonical_name"], *json.loads(c["aliases_json"])] for c in known}
    matched: list[dict[str, str]] = []  # new characters taken for an earlier card by their words (on the record)
    # the model's own label of a person the book does not name, by its ref: (label, where it was written)
    labels: dict[str, tuple[str, int]] = {}
    if parsed is not None:  # read again: what this scene's last reading wrote of people is written anew
        conn.execute("DELETE FROM character_notes WHERE book_id=? AND scene_id=?", (book_id, sid))
    unconfirmed_cards: list[dict[str, Any]] = []  # cards made for people known by several cards' words (ADR-103)
    made_by_words: dict[str, int] = {}  # how many such cards this reading made for each of those words

    def rej(item: object, reason: str) -> None:
        stats["rejected"] += 1
        repo.reject(conn, book_id, sid, "scene_analyze", item, reason)

    if parsed is not None and parsed.narration is not None and parsed.narration.person == "first" \
            and parsed.narration.narrator_ref == NEW_NARRATOR and rows:
        # a first-person narrator with no name (yet): a card of their own, so their lines, facts, relations and how
        # others address them have somewhere to go; after reading it is matched to a named character or kept (ADR-045)
        cid = new_narrator(conn, book_id, rows[0])
        ref_map[NEW_NARRATOR] = cid
        names_of[cid] = []
    if parsed is not None:
        # every card of the series, seen or not yet (a card first read later is the same person found earlier: it
        # keeps the earliest appearance); which of them a form names here is decided by ``known_by`` below
        cards_now = repo.characters(conn, book_id)
        existing_alias = repo.alias_map(cards_now)
        # every card with the words — a labelled one (of another time too, ADR-103 實測三) among them: it is never
        # found by them (``alias_map``, ``FINDABLE``), but it is one of the people they may be
        owners: dict[str, set[str]] = {}  # every card with each form, seen or not yet
        for card in cards_now:
            for f in [card["canonical_name"], *json.loads(card["aliases_json"])]:
                owners.setdefault(f, set()).add(card["id"])
        forms_at = vis.form_positions(conn, book_id)
        known_by: dict[str, set[str]] = {}  # the words each person is known by so far, and whose they are
        for card in known:
            if view := vis.character_view(conn, book_id, card, scene["start_ordinal"] - 1, forms_at, lookup=True):
                for f in [view["name_ja"], *view["aliases_ja"]]:
                    known_by.setdefault(f, set()).add(card["id"])
        group_cards = {c["id"] for c in known if "is_group" in c.keys() and c["is_group"]}
        reveals: list[tuple[str, str, str, Any, list[str], bool]] = []  # (named, revealed, name, at, evidence, new)

        def _aliases(nc: Any, name: str) -> list[str]:
            # the model says what each alias is; a common noun (「少女」「兵士」) names anyone and is not kept
            # (the generic word list stays as a check for the model's omissions)
            common = {source_form(k.ja) for k in nc.alias_kinds if k.kind == "common"}
            return [a for a in dict.fromkeys(source_form(x) for x in nc.aliases_ja)
                    if a and a != name and len(a) <= MAX_NAME_LEN and a not in PRONOUNS and a not in common
                    and _standalone(a, scene_text, [name, *existing_alias,
                                                    *(source_form(x.name_ja) for x in parsed.new_characters)])]

        def marked(nc: Any) -> bool:
            """The model says this is someone else than a card with the words: another person, or another time."""
            return bool(nc.other_person or nc.period.strip())

        def _owned_word(nc: Any, evidence: list[str]) -> tuple[str, set[str]] | None:
            """An alias of the model's in the evidence that cards seen before this scene own (any kind, a common
            noun too), with those cards; two characters at least, as for every form."""
            for a in dict.fromkeys(source_form(x) for x in nc.aliases_ja):
                if _valid_name(a) and len(a) >= MIN_FORM_LEN and known_by.get(a) \
                        and any(in_source(a, t) for t in evidence):
                    return a, set(known_by[a])
            return None

        def unconfirmed(nc: Any, name: str, first: UnitText, cards: set[str], aliases: list[str]) -> None:
            """A new person known by words several cards have: the model's ``same_as_ref`` naming one of them is the
            model's reading of who this is; otherwise a card of its own, named by the book's words, labelled to tell
            it apart and marked as maybe one of those cards for the second look (ADR-103)."""
            same = _revealed(known, book_id, nc.same_as_ref)
            if same in cards:
                ref_map[nc.temp_id] = same
                names_of.setdefault(same, []).append(name)
                matched.append({"ref": nc.temp_id, "name_ja": name, "character_id": same, "by": "same_as_ref"})
                return
            order = {c["id"]: i for i, c in enumerate(cards_now)}
            # which of this reading's proposals with these words it is: the same one on a re-read, and two stay two
            k = made_by_words[name] = made_by_words.get(name, -1) + 1
            cid, created = repo.unconfirmed_card(
                conn, book_id, name, aliases=aliases, may_be=sorted(cards, key=lambda x: order.get(x, len(order))),
                unit_id=first.id, ordinal=first.ordinal, is_group=bool(nc.group), origin="scene_analysis",
                key=f"{sid}|{name}|{k}", period=nc.period.strip()[:10] or None)
            stats["new_characters"] += int(created)
            stats["unconfirmed"] = stats.get("unconfirmed", 0) + 1
            if created:  # what its words are, as the model read them (ADR-109): a second 「給仕」 takes a name later too
                conn.execute("UPDATE characters SET name_kind=? WHERE id=?",
                             ({source_form(k.ja): k.kind for k in nc.alias_kinds}.get(name), cid))
            ref_map[nc.temp_id] = ref_map[cid] = cid
            names_of[cid] = [name, *aliases]
            unconfirmed_cards.append({"ref": nc.temp_id, "character_id": cid, "may_be": sorted(cards)})
        def taken(form: str, who: str | None) -> bool:
            """A name some other person may have: another card's in the series (its name, or a form of its entity),
            words several people share, or a form this answer gave another card — a card known by a title does not
            take it (ADR-109); whether the two are one person is the second look's."""
            from ..memory.entities import character_for_form, is_shared

            if existing_alias.get(form, who) != who or is_shared(conn, book_id, form):
                return True
            if (owner := character_for_form(conn, book_id, form)) and owner != who:
                return True
            return any(form in ns for c, ns in names_of.items() if c != who) or bool(conn.execute(
                f"SELECT 1 FROM characters WHERE {SERIES_SCOPE} AND canonical_name=? AND {repo.FINDABLE} AND id IS NOT ?",
                (book_id, form, who)).fetchone())

        for nc in parsed.new_characters:
            name = source_form(nc.name_ja)  # a name quoted with its reading (婆《ばあ》) is the text's 婆
            ev_ids = [u for u in nc.evidence_unit_ids if u in unit_text]
            substituted = False  # known by the book's words among its aliases, not by the name the model gave
            if not _valid_name(name):
                rej(nc.model_dump(), "invalid character name")
                continue
            if not any(in_source(name, unit_src[u]) for u in ev_ids):
                cards = known_by.get(name, set())
                # words an earlier scene gave a known person (「指示を出していた男」 in the scene before): that card,
                # whose words were checked where they were read — before this scene, never after. Only when the words
                # are one card's, and the model said nothing that makes this someone else: another person or time
                # (a past life's 「母親」) or a group where the card is a person (or the other way round) is the
                # model's reading of who this is, and two cards with the words are not for the program to choose
                # between — those are handled as any person whose words are not in the evidence
                if ev_ids and len(cards) == 1 and not marked(nc) \
                        and bool(nc.group) == (next(iter(cards)) in group_cards):
                    cid = next(iter(cards))
                    ref_map[nc.temp_id] = cid
                    names_of.setdefault(cid, []).append(name)
                    matched.append({"ref": nc.temp_id, "name_ja": name, "character_id": cid})
                    stats["matched_by_words"] = stats.get("matched_by_words", 0) + 1
                    continue
                said = _said_alias(nc, [unit_src[u] for u in ev_ids])
                if said is None and ev_ids and cards and (len(cards) > 1 or marked(nc)):
                    # words an earlier scene gave cards seen before (two unnamed mothers, each 「母親」), checked where
                    # they were read: which of them this is — or someone else — is the model's call, asked in the
                    # second look; the person gets a card of their own meanwhile, so their lines keep a speaker
                    unconfirmed(nc, name, next(t for t in texts if t.id in ev_ids), cards, [])
                    continue
                owned = _owned_word(nc, [unit_src[u] for u in ev_ids]) if said is None and ev_ids else None
                if owned is not None:
                    # the book's only words for this person are a word cards seen before own (「母親」: the mother's
                    # name, the baker's other name), even one the model calls common: a marked card known by it, never
                    # found by it (a labelled card), so it takes no one's place — the ADR-101 reason a common word
                    # names no card (a bare 「男」 finding someone) does not apply. The model's own label for the person
                    # is kept as its description
                    word, word_owners = owned
                    labels[nc.temp_id] = (name, next(t.ordinal for t in texts if t.id in ev_ids))
                    unconfirmed(nc, word, next(t for t in texts if t.id in ev_ids and in_source(word, unit_src[t.id])),
                                word_owners, [])
                    continue
                if said is None:  # none of the words the person is given is in the book: the hallucination guard
                    rej(nc.model_dump(), "name_ja not found verbatim in evidence units (possible hallucination)"
                        + ("; the words name several known characters" if len(cards) > 1 else
                           "; the words name a known character the model says this is not" if cards and ev_ids else ""))
                    continue
                # no name in the book and a label of the model's own (「人攫いの指示役の男」), with the book's words for
                # the person among the aliases: the card is known by those words. The label and its rendering are not
                # the book's words and are not kept; the card's rendering is chosen for its words, as for any card
                # kept with the card as the model's description only (``character_notes``), from its first evidence
                labels[nc.temp_id] = (name, next(t.ordinal for t in texts if t.id in ev_ids))
                name, nc = said, nc.model_copy(update={"proposed_name_zh": ""})
                substituted = True
            first = next(t for t in texts if in_source(name, unit_src[t.id]))
            from ..memory.entities import is_shared

            same = _revealed(known, book_id, nc.same_as_ref)
            if same and is_shared(conn, book_id, name):
                # a word that may name more than one person (「母上」 of two mothers), said to be a known card: that
                # card, merged with no one the word also names (ADR-108)
                ref_map[nc.temp_id] = same
                names_of.setdefault(same, []).append(name)
                continue
            if name not in existing_alias:
                from ..memory.entities import character_for_form

                via_entity = character_for_form(conn, book_id, name)
                if via_entity:  # a known name form of an existing character (full name, nickname …)
                    existing_alias[name] = via_entity
            other = marked(nc)
            # the model's name, or the book's words standing in for it, among the people seen before this scene: two
            # cards with it are not for the program to pick between; one is the card (not one first seen later).
            # Seen by none, the cards first known later decide the same way: one is that person found earlier
            seen = known_by.get(name, set()) or owners.get(name, set())
            if other and not seen and name in existing_alias:
                seen = {existing_alias[name]}
            # ONE path for a new person whose words cards already have (ADR-103 實測三): several cards with them, or
            # the model saying it is someone else (another person, another time), is a labelled card marked as maybe
            # those cards — the second look asks which, or someone else; the time the model gives is only its reading,
            # kept on the card. Someone of another time no card has the words for gets a labelled card too, so the
            # words still find the person of the present
            if (len(seen) > 1 or (other and seen)) or nc.period.strip():
                unconfirmed(nc, name, first, seen, _aliases(nc, name))
                continue
            if len(seen) == 1:
                existing_alias[name] = next(iter(seen))
            held = conn.execute("SELECT is_group FROM characters WHERE id=?",
                                (existing_alias[name],)).fetchone() if substituted and name in existing_alias else None
            if held is not None and not other and bool(nc.group) != bool(held[0]):
                rej(nc.model_dump(), "name_ja not found verbatim in evidence units; its words name a known character "
                                     "the model says this is not")
                continue
            if name in existing_alias:
                ref_map[nc.temp_id] = existing_alias[name]
                # already known in the series (e.g. from a later volume): known from here, by these words — its other
                # forms stay where they were seen
                repo.seen_earlier(conn, existing_alias[name], name, book_id, first.id, first.ordinal)
                names_of[existing_alias[name]] = [name]
                # the name already has a card and the model says it is the one known so far by a description (or by
                # words several cards have): the reveal merges that one into it, as for a name new here
                same = _revealed(known, book_id, nc.same_as_ref)
                if same and same != existing_alias[name]:
                    reveals.append((existing_alias[name], same, name, first, nc.evidence_unit_ids, False))
                continue
            aliases = _aliases(nc, name)
            proposed_zh = nc.proposed_name_zh.strip()[:MAX_NAME_LEN] or None
            # described first and named in the same scene (「長い銀髪の美しい女性」 … 「エレナ」, 『英雄』――レイド): the
            # model marks the words it was introduced by as a description or a title and the name among the aliases;
            # the person is known by the name, the description stays an alias
            kinds = {source_form(k.ja): k.kind for k in nc.alias_kinds}
            real = next((source_form(k.ja) for k in nc.alias_kinds if k.kind == "name" and source_form(k.ja) in aliases),
                        None)
            if real and kinds.get(name) in NOT_A_NAME and not taken(real, None):
                aliases = [name, *(a for a in aliases if a != real)]
                name, proposed_zh = real, None
            gl = conn.execute(f"SELECT target_term FROM glossary WHERE {SERIES_SCOPE} AND source_term=?",
                              (book_id, name)).fetchone()
            zh = gl[0] if gl else proposed_zh
            cid, created = repo.upsert_character(conn, book_id, name, name_zh=zh, aliases=aliases, unit_id=first.id,
                                                 ordinal=first.ordinal, origin="scene_analysis")
            if created:
                stats["new_characters"] += 1
                # what the words it is known by are, as the model read them: a title or a description gives way to the
                # person's name when the model gives one later (``other_forms``)
                conn.execute("UPDATE characters SET name_kind=? WHERE id=?", (kinds.get(name), cid))
                if zh and len(name) >= 2:
                    repo.ensure_glossary(conn, book_id, name, zh, category="character", origin="scene_analysis",
                                         first_unit_id=first.id)
            ref_map[nc.temp_id] = cid
            ref_map[cid] = cid
            names_of[cid] = [name, *aliases]
            existing_alias[name] = cid
            if nc.group:
                conn.execute("UPDATE characters SET is_group=1 WHERE id=?", (cid,))
            same = _revealed(known, book_id, nc.same_as_ref)
            if same and same != cid:
                reveals.append((cid, same, name, first, nc.evidence_unit_ids, True))
        # a name revealed for a character known so far only by a description: one character from here on.
        # The link records where it was revealed, so the name is not shown before that point.
        from ..memory.entities import record_link

        went: dict[str, str] = {}  # a card an earlier reveal of this reading merged away → the card it went into

        def now(x: str) -> str:
            while x in went:
                x = went[x]
            return x

        def reveal(cid: str, same: str, name: str, first: Any, new: bool) -> None:
            # one answer may say A is B and B is A, or chain them: follow what the earlier reveals merged; two cards
            # already one need nothing, and a card no longer there is skipped
            cid, same = now(cid), now(same)
            old = conn.execute("SELECT * FROM characters WHERE id=?", (same,)).fetchone()
            named = conn.execute("SELECT aliases_json FROM characters WHERE id=?", (cid,)).fetchone()
            if cid == same or old is None or named is None:
                return
            own = [name, *json.loads(named[0])]
            repo.merge_characters(conn, cid, same)
            went[same] = cid
            if open_ := [x for x in repo.may_be(old) if x != cid]:
                # a name tells who she is called, not which of the cards with the same words she is: still asked
                # (the cards it was answered not to be came over with the merge)
                conn.execute("UPDATE characters SET may_be_json=? WHERE id=?", (json.dumps(open_), cid))
            if repo.period_of(old) is not None:
                # a name revealed for someone of another time (リリア, the past life's 「母親」): still of that time —
                # never a present person found by the words, and the same card when that time's words are read again
                # (found again by its reading keys, which came over with the merge); labelled like every card of another
                # time, so the name alone finds the person of the present
                named_row = conn.execute("SELECT canonical_name FROM characters WHERE id=?", (cid,)).fetchone()
                conn.execute("UPDATE OR IGNORE characters SET period=?, label=? WHERE id=? AND period IS NULL",
                             (repo.period_of(old), repo.next_label(conn, book_id, named_row[0]), cid))
            # the new name and its own aliases (「王女」) are known from the reveal, not from the description's scene
            # (the description itself stays known from its own scene); a card that already had the name keeps the
            # positions its forms were first seen at
            for form in own if new else []:
                record_link(conn, book_id, form, name, "alias" if form != name else "official", origin="scene_analysis",
                            status="accepted", unit_id=first.id, quote=None, ordinal=first.ordinal)
            for k, v in list(ref_map.items()):
                if v == same:
                    ref_map[k] = cid
            names_of[cid] = [*names_of.get(cid, [name]), *names_of.pop(same, [])]
            stats["merged"] = stats.get("merged", 0) + 1

        for cid, same, name, first, _ev, new in reveals:
            reveal(cid, same, name, first, new)
        # words naming a known person that their card does not have yet (a given name used alone): the model's
        # reading; the program checks the words are in the scene, standing alone, and not another person's name.
        # Known from here on (the link records where), so they are never shown before this scene.
        for of in parsed.other_forms:
            form, who = source_form(of.ja), ref_map.get(of.ref)
            row = conn.execute("SELECT canonical_name, aliases_json, name_kind FROM characters WHERE id=?",
                               (who,)).fetchone() if who else None
            ev = [u for u in of.evidence_unit_ids if u in unit_text and in_source(form, unit_src[u])]
            if row is None or of.kind == "common" or not 2 <= len(form) <= MAX_NAME_LEN or form in PRONOUNS \
                    or any(ch in form for ch in "「」『』"):
                rej(of.model_dump(), "invalid form of a known character")
                continue
            if form == row[0] or form in json.loads(row[1]):
                continue
            if not ev or not _standalone(form, scene_text, [*existing_alias, *(n for ns in names_of.values() for n in ns)]):
                rej(of.model_dump(), "form not found standing alone in its evidence units")
                continue
            # a form another card has too (「母上」: the late birth mother early, the stepmother from a later chapter) is
            # this card's as well from here — each card from where the model gave it the form (``character_forms``);
            # where both are visible, a lookup by the words picks neither (the user's decision, ADR-103)
            shared = existing_alias.get(form, who) != who
            first = next(t for t in texts if t.id in ev)
            if of.kind == "name" and not shared and row["name_kind"] in NOT_A_NAME and not taken(form, str(who)):
                # the person's name for a card known so far by a title or a description (『英雄』 then レイド・フリーデン):
                # the card is the name's from here, as when a name is revealed (the title stays an alias, the name is
                # not shown before this scene)
                gl = conn.execute(f"SELECT target_term FROM glossary WHERE {SERIES_SCOPE} AND source_term=?",
                                  (book_id, form)).fetchone()
                named, _ = repo.upsert_character(conn, book_id, form, name_zh=gl[0] if gl else None, aliases=[],
                                                       unit_id=first.id, ordinal=first.ordinal, origin="scene_analysis")
                conn.execute("UPDATE characters SET name_kind='name' WHERE id=?", (named,))
                reveal(named, str(who), form, first, True)
                existing_alias[form] = named
                stats["forms"] = stats.get("forms", 0) + 1
                continue
            conn.execute("UPDATE characters SET aliases_json=?, updated_at=? WHERE id=?",
                         (json.dumps([*json.loads(row[1]), form], ensure_ascii=False), utcnow(), who))
            repo.note_forms(conn, str(who), [form], book_id, first.ordinal)  # known from here, wherever the card is
            if not shared:  # a link reveals the form for every card that has it: not for a form another card had
                record_link(conn, book_id, form, row[0], "alias", origin="scene_analysis", status="accepted",
                            unit_id=first.id, quote=None, ordinal=first.ordinal)
            names_of.setdefault(str(who), []).append(form)
            if not shared:
                existing_alias[form] = str(who)
            stats["forms"] = stats.get("forms", 0) + 1

    all_visible = vis.visible_characters(conn, book_id, scene["end_ordinal"])
    amap = repo.alias_map(all_visible)
    quotes = find_quotes(texts)
    position = {t.id: i for i, t in enumerate(texts)}
    told = [_QUOTED.sub("", unit_src[t.id]) for t in texts]  # the paragraphs without what is said in them

    # a first-person scene: the narrator's own words for themself (「と俺は言った」) name the narrator — only for the
    # narrator the model gave for this scene, and only the self-forms the model recorded for that narrator
    narrator_id: str | None = None
    narrator_forms: set[str] = set()
    if parsed is not None and parsed.narration is not None and parsed.narration.person == "first":
        from .narration import established_self_forms, settle_self_forms

        narrator_id = ref_map.get(parsed.narration.narrator_ref or "")
        if narrator_id:
            earlier = established_self_forms(conn, book_id, scene["seq"])
            narrator_forms = {f["ja"] for f in settle_self_forms([f.model_dump() for f in parsed.narration.self_forms],
                                                                 narrator_id, earlier, rows) if f.get("ja")}
            narrator_forms |= {str(e["ja"]) for e in earlier if e["narrator_ref"] == narrator_id and e["ja"]}

    def names_for(cid: str) -> set[str]:
        names = {n for n, i in amap.items() if i == cid} | set(names_of.get(cid, []))
        return names | narrator_forms if cid == narrator_id else names

    has_quote = {q.unit_id for q in quotes}
    # a speech running over paragraphs: its later paragraphs (and their narration, 「…決めたわ」とミナは言った) belong to
    # the speech's first line
    by_key = {(q.unit_id, q.index): q for q in quotes}
    rest_of: dict[tuple[str, int], list[str]] = {}
    for q in quotes:
        root = q
        while root.continuation_of and root.continuation_of in by_key:
            root = by_key[root.continuation_of]
        if root is not q:
            rest_of.setdefault((root.unit_id, root.index), []).append(q.unit_id)

    all_names = [*amap, *(n for ns in names_of.values() for n in ns)]

    def tag_ok_for(q: Quote) -> Any:
        # the line's own paragraph (its narration, what is said taken out), or the paragraph before / after when it is
        # narration only. Which line in a paragraph the words belong to is the model's reading, not the program's
        # (ADR-059: the program checks the words are there and name the speaker; it does not parse Japanese)
        i = position[q.unit_id]
        near = [told[i]] + [told[j] for j in (i - 1, i + 1) if 0 <= j < len(texts) and texts[j].id not in has_quote]
        rest = rest_of.get((q.unit_id, q.index), [])
        if rest:  # the speech's later paragraphs, and narration right after where it ends
            last = position[rest[-1]]
            near += [told[position[u]] for u in rest]
            if last + 1 < len(texts) and texts[last + 1].id not in has_quote:
                near.append(told[last + 1])

        def ok(tag: str, cid: str) -> bool:  # the words are there, outside quotes, and name the speaker
            words = _QUOTED.sub("", source_form(tag)).strip(" 　。、")
            # a name, not part of a longer known name (「ミナミが言った」 does not name ミナ)
            return bool(words) and any(in_source(words, n) for n in near) and any(
                nm and _standalone(nm, words, all_names) for nm in names_for(cid))
        return ok

    claims: dict[tuple[str, int], LlmClaim] = {}
    participants: list[dict[str, object]] = []
    actions: list[dict[str, object]] = []
    props: list[dict[str, object]] = []
    uncertainties: list[dict[str, object]] = []
    situation: list[dict[str, Any]] = []
    if parsed is not None:
        qkeys = {(q.unit_id, q.index) for q in quotes}
        for ua in parsed.utterances:
            key = (ua.unit_id, ua.quote_index)
            if key not in qkeys:
                rej(ua.model_dump(), "utterance does not match a detected quote")
                continue
            cands = []
            for c in ua.speaker_candidates:
                if c.character_ref not in ref_map:
                    rej({"utterance": key, "candidate": c.model_dump()}, "unknown character_ref")
                    continue
                cands.append(Candidate(ref_map[c.character_ref], set(c.evidence_types),
                                       [u for u in c.evidence_unit_ids if u in unit_text], "llm", c.note[:120]))
            sref = ua.speaker_ref
            if sref is not None and sref not in ref_map:
                rej({"utterance": key, "speaker_ref": sref}, "unknown speaker_ref")
                sref = None
            claims[key] = LlmClaim(ref_map[sref] if sref else None, ua.resolution_status, cands,
                                   [x[:200] for x in ua.contradictions],
                                   [ref_map[x] for x in ua.listener_refs if x in ref_map and ref_map[x] != ref_map.get(sref or "")],
                                   ua.speech_tag[:80], ua.listener_status)
        for p in parsed.participants:
            if p.character_ref in ref_map:
                participants.append({"character_id": ref_map[p.character_ref], "presence": p.presence,
                                     "evidence_unit_ids": [u for u in p.evidence_unit_ids if u in unit_text]})
            else:
                rej(p.model_dump(), "unknown participant ref")
        actions, bad = validate_actions(parsed.actions, unit_text, ref_map, names_of)
        for item, reason in bad:
            rej(item, reason)
        for pr in parsed.props:
            name = re.sub(r"[（(].*?[）)]", "", source_form(pr.name_ja or "")).strip()  # 「魔剣（魔法で…）」: the name
            if any(in_source(name, t) for t in unit_src.values()):
                holder = ref_map.get(pr.holder_ref) if pr.holder_ref else None
                props.append({"name_ja": name, "holder": holder,
                              "evidence_unit_ids": [u for u in pr.evidence_unit_ids if u in unit_text]})
            else:
                rej(pr.model_dump(), "prop not found in scene text")
        uncertainties = [{"unit_ids": [u for u in x.unit_ids if u in unit_text], "question": x.question[:300]}
                         for x in parsed.uncertainties]
        situation = situation_points(parsed.situation, rows, ref_map, rej)

    resolutions: dict[tuple[str, int], Resolution] = {}
    version = int(scene["version"]) + 1
    for q in quotes:
        uid_key = (q.unit_id, q.index)
        head = resolutions.get(q.continuation_of) if q.continuation_of else None
        res = resolve(claims.get(uid_key), head, tag_ok_for(q))
        utt_id = repo.utterance_id_for(q.unit_id, q.index)
        old = conn.execute("SELECT * FROM utterances WHERE id=?", (utt_id,)).fetchone()
        if old is not None and old["status"] == "human_verified":
            stats["human_kept"] += 1
            resolutions[uid_key] = Resolution(old["speaker_id_nullable"], "human_verified", "human")
            continue
        if parsed is not None and uid_key not in claims and not res.speaker_id and q.continuation_of is None:
            # a line the reading said nothing about, also when asked again: for the second look (ADR-103 實測四)
            from .speaker import TO_LOOK_AT

            res.notes = [*res.notes, f"no answer from the reading {TO_LOOK_AT}"]
            stats["unanswered_lines"] = stats.get("unanswered_lines", 0) + 1
        resolutions[uid_key] = res
        stats[res.status] += 1
        cont = repo.utterance_id_for(*q.continuation_of) if q.continuation_of else None
        conn.execute(
            "INSERT INTO utterances(id, book_id, unit_id, scene_id, quote_index, quote_start, quote_end, quote_text, "
            "continuation_of, speaker_id_nullable, status, resolution_source, notes_json, analysis_version, updated_at, "
            "addressee_ids_json, addressee_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET scene_id=excluded.scene_id, "
            "addressee_ids_json=excluded.addressee_ids_json, addressee_status=excluded.addressee_status, "
            "quote_start=excluded.quote_start, quote_end=excluded.quote_end, quote_text=excluded.quote_text, "
            "continuation_of=excluded.continuation_of, speaker_id_nullable=excluded.speaker_id_nullable, "
            "status=excluded.status, resolution_source=excluded.resolution_source, notes_json=excluded.notes_json, "
            "analysis_version=excluded.analysis_version, updated_at=excluded.updated_at",
            (utt_id, book_id, q.unit_id, sid, q.index, q.start, q.end, q.text, cont, res.speaker_id, res.status,
             res.source, json.dumps(res.notes, ensure_ascii=False), version, utcnow(),
             json.dumps(claims[uid_key].listeners if uid_key in claims else []),
             claims[uid_key].listener_status if uid_key in claims and claims[uid_key].listeners else None))
        conn.execute("DELETE FROM speaker_hypotheses WHERE utterance_id=?", (utt_id,))
        for cand in res.candidates:
            conn.execute("INSERT INTO speaker_hypotheses(utterance_id, candidate_character_id, evidence_json, source, "
                         "status, created_at) VALUES (?,?,?,?,?,?)",
                         (utt_id, cand.character_id, json.dumps(cand.to_json(), ensure_ascii=False), cand.source,
                          "selected" if cand.character_id == res.speaker_id else "candidate", utcnow()))
        # anti self-pollution: only explicit attributions feed the speech-example library
        repo.remove_speech_examples_for(conn, utt_id)
        if res.status == "explicit" and res.speaker_id:
            repo.add_speech_example(conn, res.speaker_id, q.unit_id, utt_id, q.text, "explicit")
            stats["examples"] += 1
    # the words the model gave each person it read as new here (its name and every alias, a common word too)
    given: dict[str, list[str]] = {}
    for nc in parsed.new_characters if parsed is not None else []:
        if (cid_ := ref_map.get(nc.temp_id)) is not None:
            given.setdefault(cid_, []).extend(source_form(x) for x in [nc.name_ja, *nc.aliases_ja])
    _known_from_attributed(conn, book_id, texts, unit_src, resolutions, quotes, participants, given)
    narration: dict[str, Any] | None = None
    segments = (narration_segments(parsed.narration.segments, rows, ref_map)
                if parsed is not None and parsed.narration is not None else [])
    if parsed is not None and parsed.narration is not None and parsed.narration.person in ("first", "third"):
        from .narration import established_self_forms, judged, settle_self_forms

        n = parsed.narration
        narration = {"person": n.person, "source": "llm",
                     **{k: ref_map[r] for k, r in (("narrator", n.narrator_ref), ("focal", n.focal_ref))
                        if r and ref_map.get(r) in set(ref_map.values())},
                     **({"check": chk} if (chk := judged(n.person, rows)) else {})}
        if n.person == "first" and (forms := settle_self_forms(
                [f.model_dump() for f in n.self_forms], narration.get("narrator"),
                established_self_forms(conn, book_id, scene["seq"]), rows)):
            narration["self_forms"] = forms
    if segments:  # stretches in another voice are kept even when the scene's own narration is unclear
        narration = {**(narration or {"person": "unclear", "source": "llm"}), "segments": segments}
    state: dict[str, Any] = {"narration": narration, "pov": parsed.pov if parsed else None, "location": parsed.location if parsed else None,
             "time": parsed.time if parsed else None, "participants": participants, "actions": actions,
             "props": props, "uncertainties": uncertainties, "analysis": "llm" if parsed else "program_only",
             "quotes": len(quotes), "run_id": run_id}
    if situation:
        state["situation"] = situation  # the model's picture of the scene (ADR-104)
    if matched:
        state["matched_by_words"] = matched
    if unconfirmed_cards:
        state["unconfirmed_cards"] = unconfirmed_cards
    for r, (lb, at) in labels.items():  # the model's description of a person the book does not name (ADR-101/103)
        if r in ref_map:
            repo.note_description(conn, ref_map[r], lb, book_id=book_id, scene_id=sid, ordinal=at)
    conn.execute("UPDATE scenes SET state_json=?, version=? WHERE book_id=? AND id=?",
                 (json.dumps(state, ensure_ascii=False), version, book_id, sid))
    repo.refresh_profiles(conn, book_id)
    if refs is not None:
        refs.update(ref_map)
    return stats


def _known_from_attributed(conn: sqlite3.Connection, book_id: str, texts: list[UnitText], unit_src: dict[str, str],
                           resolutions: dict[tuple[str, int], Resolution], quotes: list[Quote],
                           participants: list[dict[str, object]], given: dict[str, list[str]]) -> None:
    """A card is there from the first paragraph of the scene the reading gives it — a line it speaks, or where it is
    present — even when its card was made where its name is revealed further on (イレナ named after a line of hers):
    known from that paragraph, under the words for it the scene has by then (「母親」); the revealed name still only from
    where it is first seen with the card (``character_forms``, ADR-103)."""
    at = {t.id: t.ordinal for t in texts}
    earliest: dict[str, int] = {}
    for q in quotes:
        res = resolutions.get((q.unit_id, q.index))
        if res is not None and res.speaker_id and q.unit_id in at:
            earliest[res.speaker_id] = min(earliest.get(res.speaker_id, at[q.unit_id]), at[q.unit_id])
    for p in participants:
        ev = p.get("evidence_unit_ids")
        for u in ev if isinstance(ev, list) else []:
            if isinstance(u, str) and u in at:
                cid = str(p["character_id"])
                earliest[cid] = min(earliest.get(cid, at[u]), at[u])
    for cid, ordinal in earliest.items():
        row = conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
        if row is None or row["book_id"] != book_id or row["known_from_ordinal"] <= ordinal:
            continue
        unit = next(t for t in texts if t.ordinal == ordinal)
        repo.hold_forms(conn, row)  # its forms stay where they were first seen with it
        conn.execute("UPDATE characters SET known_from_unit_id=?, known_from_ordinal=?, updated_at=? WHERE id=?",
                     (unit.id, ordinal, utcnow(), cid))
        for form in dict.fromkeys([row["canonical_name"], *json.loads(row["aliases_json"] or "[]"),
                                   *given.get(cid, [])]):
            if len(form) >= MIN_FORM_LEN and form not in PRONOUNS and \
                    any(in_source(form, unit_src[t.id]) for t in texts if t.ordinal <= ordinal):
                repo.note_forms(conn, cid, [form], book_id, ordinal)  # the words it is called by there


MAX_SEGMENTS = 6


def narration_segments(segments: list[Any], rows: list[sqlite3.Row], ref_map: dict[str, str]) -> list[dict[str, Any]]:
    """The read pass's stretches told in another voice (ADR-083), as stored: the paragraphs from the first to the last
    it named (in story order, inside the scene), its kind of telling, its teller when that is a known card, and the
    evidence paragraphs of the scene it cited. A stretch citing no paragraph of the scene is not kept."""
    pos = {r["id"]: i for i, r in enumerate(rows)}
    out: list[dict[str, Any]] = []
    for seg in segments[:MAX_SEGMENTS]:
        at = sorted(pos[u] for u in seg.unit_ids if u in pos)
        evidence = [u for u in seg.evidence_unit_ids if u in pos]
        if not at or not evidence or not seg.voice.strip():
            continue
        teller, to = ref_map.get(seg.teller_ref or ""), ref_map.get(seg.addressee_ref or "")
        out.append({"units": [rows[i]["id"] for i in range(at[0], at[-1] + 1)], "voice": seg.voice.strip()[:40],
                    "person": seg.person, **({"teller": teller} if teller else {}), **({"to": to} if to else {}),
                    "evidence": evidence})
    return out


def quotes_for_scene(conn: sqlite3.Connection, book_id: str, scene_id: str) -> list[Quote]:
    return find_quotes(_unit_texts(scene_rows(conn, book_id, scene_id)))
