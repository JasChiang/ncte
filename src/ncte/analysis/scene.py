"""Scene segmentation (deterministic, spec §4.2/§5.2) and LLM scene analysis (M3)."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
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
            if overrides.get(r["id"]) == "split" or breaks.get(r["id"]) == "llm":
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


def _standalone(alias: str, text: str, longer_names: list[str]) -> bool:
    """True if ``alias`` occurs in ``text`` outside every longer name that contains it (カイ vs カイト)."""
    for n in sorted({n for n in longer_names if alias in n and n != alias}, key=len, reverse=True):
        text = text.replace(n, "\0")
    return alias in text


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
                or has("SELECT 1 FROM glossary WHERE book_id=? AND category='character' AND source_term=? AND "
                       "(verified=1 OR origin='user')", book_id, c["canonical_name"]) \
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
        conn.execute("DELETE FROM glossary WHERE book_id=? AND category='character' AND source_term=? AND verified=0 AND "
                     "coalesce(origin,'')!='user'", (book_id, c["canonical_name"]))
        conn.execute("DELETE FROM characters WHERE id=?", (cid,))
        dropped.append(cid)
    return dropped


def build_analysis_payload(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row,
                           glossary: list[dict[str, Any]]) -> dict[str, Any]:
    rows = scene_rows(conn, book_id, scene["id"])
    known = vis.visible_characters(conn, book_id, scene["start_ordinal"] - 1)
    forms_at = vis.form_positions(conn, book_id)
    texts = _unit_texts(rows)
    quotes = find_quotes(texts)
    # the lines found by their brackets (structure); who says them is the model's reading
    q_payload = [{"unit_id": q.unit_id, "quote_index": q.index, "text": q.text[:200],
                  "continues_previous_quote": q.continuation_of is not None} for q in quotes]
    scene_text = " ".join(t.text for t in texts)
    from .narration import hint as narration_hint

    prev = conn.execute("SELECT state_json FROM scenes WHERE book_id=? AND seq<? AND version>0 ORDER BY seq DESC LIMIT 1",
                        (book_id, scene["seq"])).fetchone()
    prev_narr = json.loads(prev[0] or "{}").get("narration") if prev else None
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
    return {
        **extra,
        "task": "scene_analyze", "scene_id": scene["id"],
        "units": [{"unit_id": r["id"], "source": r["plain_text"]} for r in rows],
        "quotes": q_payload,
        "known_characters": [{"character_id": c["id"], **v, "speech_profile": json.loads(c["speech_profile_json"] or "{}"),
                              **({"relations": kin[c["id"]]} if kin.get(c["id"]) else {})}
                             for c in known
                             if (v := vis.character_view(conn, book_id, c, scene["start_ordinal"] - 1, forms_at))],
        "glossary": [g for g in glossary if str(g.get("ja", "")) in scene_text],
    }


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
    names = {r["id"]: r["display_name_zh"] or r["canonical_name"] for r in repo.characters(conn, book_id)}
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
                         parsed: SceneAnalysisOutput | None, *, run_id: str | None = None) -> dict[str, int]:
    """Validate an analysis against the source and persist it. ``parsed=None`` → program-only analysis."""
    sid = scene["id"]
    rows = scene_rows(conn, book_id, sid)
    texts = _unit_texts(rows)
    unit_text = {t.id: t.text for t in texts}
    scene_text = "\n".join(unit_text.values())
    stats = {"new_characters": 0, "rejected": 0, "explicit": 0, "inferred": 0, "unknown": 0, "examples": 0,
             "human_kept": 0}
    known = vis.visible_characters(conn, book_id, scene["start_ordinal"] - 1)
    ref_map: dict[str, str] = {c["id"]: c["id"] for c in known}
    names_of: dict[str, list[str]] = {c["id"]: [c["canonical_name"], *json.loads(c["aliases_json"])] for c in known}

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
        existing_alias = repo.alias_map(repo.characters(conn, book_id))
        reveals: list[tuple[str, str, str, Any, list[str]]] = []
        for nc in parsed.new_characters:
            name = nc.name_ja.strip()
            ev_ids = [u for u in nc.evidence_unit_ids if u in unit_text]
            if not name or len(name) > MAX_NAME_LEN or any(ch in name for ch in "「」『』") or name in PRONOUNS:
                rej(nc.model_dump(), "invalid character name")
                continue
            if not any(name in unit_text[u] for u in ev_ids):
                rej(nc.model_dump(), "name_ja not found verbatim in evidence units (possible hallucination)")
                continue
            first = next(t for t in texts if name in t.text)
            if name not in existing_alias:
                from ..memory.entities import character_for_form

                via_entity = character_for_form(conn, book_id, name)
                if via_entity:  # a known name form of an existing character (full name, nickname …)
                    existing_alias[name] = via_entity
            if nc.other_person and (name in existing_alias or nc.period.strip()):
                # 「母親」 of a past life is not the mother of this one: a card of its own, named with its time, and the
                # bare word stays with the card it had (the reader tells them apart by the cards it is given) — also
                # when the past one is met first in the scene, so the order of mention does not decide who is who
                label = f"{name}（{nc.period.strip()[:10] or '另一人'}）"
                cid, created = repo.upsert_character(conn, book_id, label, name_zh=None, aliases=[], unit_id=first.id,
                                                     ordinal=first.ordinal, origin="scene_analysis")
                stats["new_characters"] += int(created)
                ref_map[nc.temp_id] = ref_map[cid] = cid
                names_of[cid] = [name]
                continue
            if name in existing_alias:
                ref_map[nc.temp_id] = existing_alias[name]
                canon = conn.execute("SELECT canonical_name FROM characters WHERE id=?", (existing_alias[name],)).fetchone()
                if canon:  # already known in the series (e.g. from a later volume): keep the earliest appearance
                    repo.upsert_character(conn, book_id, canon[0], name_zh=None, aliases=[], unit_id=first.id,
                                          ordinal=first.ordinal, origin="scene_analysis")
                names_of[existing_alias[name]] = [name]
                continue
            # the model says what each alias is; a common noun (「少女」「兵士」) names anyone and is not kept
            # (the generic word list stays as a check for the model's omissions)
            common = {k.ja.strip() for k in nc.alias_kinds if k.kind == "common"}
            aliases = [a for a in nc.aliases_ja if a and a != name and len(a) <= MAX_NAME_LEN and a not in PRONOUNS
                       and a not in common
                       and _standalone(a, scene_text, [name, *existing_alias, *(x.name_ja for x in parsed.new_characters)])]
            proposed_zh = nc.proposed_name_zh.strip()[:MAX_NAME_LEN] or None
            # described first and named in the same scene (「長い銀髪の美しい女性」 … 「エレナ」): the model marks the
            # words it was introduced by as a description and the name among the aliases; the person is known by the
            # name, the description stays an alias
            real = next((k.ja.strip() for k in nc.alias_kinds if k.kind == "name" and k.ja.strip() in aliases), None)
            described = any(k.ja.strip() == name and k.kind == "description" for k in nc.alias_kinds)
            if real and described and real not in existing_alias:
                aliases = [name, *(a for a in aliases if a != real)]
                name, proposed_zh = real, None
            gl = conn.execute(f"SELECT target_term FROM glossary WHERE {SERIES_SCOPE} AND source_term=?",
                              (book_id, name)).fetchone()
            zh = gl[0] if gl else proposed_zh
            cid, created = repo.upsert_character(conn, book_id, name, name_zh=zh, aliases=aliases, unit_id=first.id,
                                                 ordinal=first.ordinal, origin="scene_analysis")
            if created:
                stats["new_characters"] += 1
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
                reveals.append((cid, same, name, first, nc.evidence_unit_ids))
        # a name revealed for a character known so far only by a description: one character from here on.
        # The link records where it was revealed, so the name is not shown before that point.
        from ..memory.entities import record_link

        for cid, same, name, first, _ev in reveals:
            old = conn.execute("SELECT canonical_name FROM characters WHERE id=?", (same,)).fetchone()
            if old is None:
                continue
            own = [name, *json.loads(conn.execute("SELECT aliases_json FROM characters WHERE id=?", (cid,)).fetchone()[0])]
            repo.merge_characters(conn, cid, same)
            # the new name and its own aliases (「王女」) are known from the reveal, not from the description's scene
            # (the description itself stays known from its own scene)
            for form in own:
                record_link(conn, book_id, form, name, "alias" if form != name else "official", origin="scene_analysis",
                            status="accepted", unit_id=first.id, quote=None, ordinal=first.ordinal)
            for k, v in list(ref_map.items()):
                if v == same:
                    ref_map[k] = cid
            names_of[cid] = [*names_of.get(cid, [name]), *names_of.pop(same, [])]
            stats["merged"] = stats.get("merged", 0) + 1

    all_visible = vis.visible_characters(conn, book_id, scene["end_ordinal"])
    amap = repo.alias_map(all_visible)
    quotes = find_quotes(texts)
    position = {t.id: i for i, t in enumerate(texts)}
    told = [_QUOTED.sub("", clean(t.text)) for t in texts]  # the paragraphs without what is said in them

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
            words = _QUOTED.sub("", clean(tag)).strip(" 　。、")
            # a name, not part of a longer known name (「ミナミが言った」 does not name ミナ)
            return bool(words) and any(words in n for n in near) and any(
                nm and _standalone(nm, words, all_names) for nm in names_for(cid))
        return ok

    claims: dict[tuple[str, int], LlmClaim] = {}
    participants: list[dict[str, object]] = []
    actions: list[dict[str, object]] = []
    props: list[dict[str, object]] = []
    uncertainties: list[dict[str, object]] = []
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
                                   ua.speech_tag[:80])
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
            name = re.sub(r"[（(].*?[）)]", "", pr.name_ja or "").strip()  # 「魔剣（魔法で生成された大剣）」: the name
            if name and name in scene_text:
                holder = ref_map.get(pr.holder_ref) if pr.holder_ref else None
                props.append({"name_ja": name, "holder": holder,
                              "evidence_unit_ids": [u for u in pr.evidence_unit_ids if u in unit_text]})
            else:
                rej(pr.model_dump(), "prop not found in scene text")
        uncertainties = [{"unit_ids": [u for u in x.unit_ids if u in unit_text], "question": x.question[:300]}
                         for x in parsed.uncertainties]

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
        resolutions[uid_key] = res
        stats[res.status] += 1
        cont = repo.utterance_id_for(*q.continuation_of) if q.continuation_of else None
        conn.execute(
            "INSERT INTO utterances(id, book_id, unit_id, scene_id, quote_index, quote_start, quote_end, quote_text, "
            "continuation_of, speaker_id_nullable, status, resolution_source, notes_json, analysis_version, updated_at, "
            "addressee_ids_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET scene_id=excluded.scene_id, "
            "addressee_ids_json=excluded.addressee_ids_json, "
            "quote_start=excluded.quote_start, quote_end=excluded.quote_end, quote_text=excluded.quote_text, "
            "continuation_of=excluded.continuation_of, speaker_id_nullable=excluded.speaker_id_nullable, "
            "status=excluded.status, resolution_source=excluded.resolution_source, notes_json=excluded.notes_json, "
            "analysis_version=excluded.analysis_version, updated_at=excluded.updated_at",
            (utt_id, book_id, q.unit_id, sid, q.index, q.start, q.end, q.text, cont, res.speaker_id, res.status,
             res.source, json.dumps(res.notes, ensure_ascii=False), version, utcnow(),
             json.dumps(claims[uid_key].listeners if uid_key in claims else [])))
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
    narration: dict[str, Any] | None = None
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
    state = {"narration": narration, "pov": parsed.pov if parsed else None, "location": parsed.location if parsed else None,
             "time": parsed.time if parsed else None, "participants": participants, "actions": actions,
             "props": props, "uncertainties": uncertainties, "analysis": "llm" if parsed else "program_only",
             "quotes": len(quotes), "run_id": run_id}
    conn.execute("UPDATE scenes SET state_json=?, version=? WHERE book_id=? AND id=?",
                 (json.dumps(state, ensure_ascii=False), version, book_id, sid))
    repo.refresh_profiles(conn, book_id)
    return stats


def quotes_for_scene(conn: sqlite3.Connection, book_id: str, scene_id: str) -> list[Quote]:
    return find_quotes(_unit_texts(scene_rows(conn, book_id, scene_id)))
