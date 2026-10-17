"""Scene segmentation (deterministic, spec §4.2/§5.2) and LLM scene analysis (M3)."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from ..lexicon import GENERIC_REFERENTS, acceptable_glossary_term, names_a_group
from ..llm.schemas import SceneAnalysisOutput
from ..memory import repository as repo
from ..memory import visibility as vis
from ..memory.db import Transaction, utcnow
from ..series import SERIES_SCOPE
from .dialogue import FIRST_PERSON, SECOND_PERSON, Quote, UnitText, clean, detect_tags, find_quotes
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
                  breaks: dict[str, str] | None = None, *, lone_headings: bool = False,
                  chapters: dict[str, bool] | None = None) -> list[Scene]:
    """``breaks``: unit id → "llm" (a scene the model starts there) or "stretch" (a stretch the model divided: no
    size cuts inside it up to twice the limit, ADR-036). A heading stays with the text after it (「序章」 followed by
    blank lines was a scene of its own: read, summarised, translated and reviewed alone); ``lone_headings`` keeps the
    old cut for a book already read that way.

    ``chapters`` (first unit of each TOC chapter → whether the model has cut it): a chapter always starts a scene, and
    inside a chapter the model's cut is the cut (the author's blank lines and separators were candidates it weighed);
    a chapter not cut yet keeps the program's provisional cut."""
    breaks = breaks or {}
    forced = _long_scene_cuts(rows, breaks, overrides, chapters, max_units) if chapters is not None else set()
    decided = False
    scenes: list[Scene] = []
    per_spine: dict[int | None, int] = {}
    cur: Scene | None = None
    prev_href: str | None = None
    prev_row: sqlite3.Row | None = None
    decided_chapter = False
    for r in rows:
        reason = None
        if chapters is not None and (cur is None or r["id"] in chapters):
            reason = "chapter"
            decided_chapter = chapters.get(r["id"], False)
        elif chapters is not None and decided_chapter:
            if overrides.get(r["id"]) == "split" or breaks.get(r["id"]) == "llm":
                reason = "manual" if overrides.get(r["id"]) == "split" else "llm"
            elif r["id"] in forced:
                reason = "max_size"  # the model's scene is too long to read in one request: cut evenly, at quiet points
        elif cur is None or r["resource_href"] != prev_href:
            reason = "document"
        elif overrides.get(r["id"]) == "split":
            reason = "manual"
        elif overrides.get(r["id"]) == "merge":
            reason = None
        elif r["is_separator"]:
            reason = "separator"
        elif r["kind"] == "heading" and cur.has_body:
            reason = "heading"
        elif not cur.has_body and not lone_headings:
            reason = None  # only a heading so far: the text after it belongs with it
        elif r["blank_before"] >= 2:
            reason = "blank_lines"
        elif breaks.get(r["id"]) == "llm":
            reason = "llm"
        elif decided and len(cur.unit_ids) < max_units * 2:
            reason = None
        elif len(cur.unit_ids) >= max_units and (_calm(r, prev_row) or len(cur.unit_ids) >= max_units + MAX_SIZE_SLACK):
            reason = "max_size"
        if reason in ("chapter", "document", "manual", "separator", "heading", "blank_lines"):
            decided = False  # the author's marker ends a stretch
        if breaks.get(r["id"]) == "stretch":
            decided = True
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
LONG_SCENE = 1.5  # a scene the model cut, longer than this many times max_units, is read in even parts
CALM_REACH = 15  # how far an even cut may move to fall at a quiet point


def _long_scene_cuts(rows: list[sqlite3.Row], breaks: dict[str, str], overrides: dict[str, str],
                     chapters: dict[str, bool], max_units: int) -> set[str]:
    """Where a scene the model cut is divided because it is too long for one reading request (240 paragraphs and 136
    lines in one request were seen): into even parts of about ``max_units``, each cut moved to the nearest point
    between two narration paragraphs. The model's scenes are kept whole up to ``LONG_SCENE`` times the limit."""
    spans: list[list[int]] = []
    decided = False
    for i, r in enumerate(rows):
        if r["id"] in chapters:
            decided = chapters[r["id"]]
            spans.append([i])
        elif decided and (breaks.get(r["id"]) == "llm" or overrides.get(r["id"]) == "split"):
            spans.append([i])
        elif spans and decided:
            spans[-1].append(i)
        elif not decided and spans:
            spans[-1] = []  # a chapter the model has not cut: the program's own cut applies
    out: set[str] = set()
    for span in spans:
        n = len(span)
        if n <= max_units * LONG_SCENE:
            continue
        parts = -(-n // max_units)
        for k in range(1, parts):
            t = k * n // parts
            near = sorted(range(max(1, t - CALM_REACH), min(n - 1, t + CALM_REACH) + 1), key=lambda j: abs(j - t))
            j = next((j for j in near if _calm(rows[span[j]], rows[span[j] - 1])), t)
            if overrides.get(rows[span[j]]["id"]) != "merge":
                out.add(rows[span[j]]["id"])
    return out


def _calm(r: sqlite3.Row, prev: sqlite3.Row | None) -> bool:
    """A cut forced by size waits until it falls between two narration paragraphs (not inside an exchange of lines)."""
    def talk(x: sqlite3.Row | None) -> bool:
        t = (x["plain_text"] if x is not None else "").lstrip(" 　")
        return t.startswith(("「", "『")) or "」" in t[-3:]
    return not talk(r) and not talk(prev)


def segmentation(conn: sqlite3.Connection, book_id: str) -> str:
    """How this book is cut into scenes: "chapters" (TOC chapters, the model cuts inside them) for a book not read
    yet; "files" (files, headings, the author's markers, size) for one already read that way — a changed range would
    lose what reading it left and be read again (paid)."""
    row = conn.execute("SELECT segmentation FROM books WHERE id=?", (book_id,)).fetchone()
    if row is not None and row[0]:
        return str(row[0])
    read = conn.execute("SELECT 1 FROM scenes WHERE book_id=? AND version>0 LIMIT 1", (book_id,)).fetchone()
    scheme = "files" if read else "chapters"
    conn.execute("UPDATE books SET segmentation=? WHERE id=?", (scheme, book_id))
    return scheme


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
    if segmentation(conn, book_id) != "chapters":
        return None
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
    # a book read with headings as scenes of their own keeps them: a changed range would be read again
    lone = conn.execute("SELECT 1 FROM scenes s JOIN units u ON u.book_id=s.book_id AND u.id=s.start_unit_id AND "
                        "u.id=s.end_unit_id WHERE s.book_id=? AND s.version>0 AND u.kind='heading' LIMIT 1",
                        (book_id,)).fetchone() is not None
    from .memory_update import forget_scene

    with Transaction(conn):
        chapters = chapter_starts_for(conn, book_id, rows)
        scenes = segment_units(rows, overrides, cfg.memory.max_scene_units, breaks, lone_headings=lone,
                               chapters=chapters)
        existing = {r["id"]: r for r in conn.execute("SELECT * FROM scenes WHERE book_id=?", (book_id,))}
        conn.execute("UPDATE units SET scene_id=NULL WHERE book_id=?", (book_id,))
        keep: set[str] = set()
        kinds = {r[0]: r[1] for r in conn.execute("SELECT unit_id, kind FROM scene_breaks WHERE book_id=?", (book_id,))}
        last_kind: str | None = None
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
            if k is None and s.break_reason == "max_size":
                k = last_kind  # a long scene read in parts: each part is the same kind of scene (a flashback's second half)
            conn.execute("UPDATE scenes SET kind=? WHERE book_id=? AND id=?", (k, book_id, s.id))
            last_kind = k
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
    amap = repo.alias_map(known)
    texts = _unit_texts(rows)
    quotes = find_quotes(texts)
    ev = detect_tags(texts, quotes, amap)
    q_payload = []
    for q in quotes:
        tags = {e.character_id for e in ev.get((q.unit_id, q.index), []) if e.kind == "dialogue_tag"}
        q_payload.append({"unit_id": q.unit_id, "quote_index": q.index, "text": q.text[:200],
                          "program_speaker": next(iter(tags)) if len(tags) == 1 else None,
                          "continues_previous_quote": q.continuation_of is not None})
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


REVEAL_WINDOW = 400  # units: a description is matched to a revealed name only if it was introduced shortly before


def _revealed(conn: sqlite3.Connection, book_id: str, known: list[Any], same_as: str | None, aliases: list[str],
              ordinal: int) -> str | None:
    """The known character a newly named one turns out to be: the model's ``same_as_ref`` (a known character of
    this book), or else a known character whose name is one of the new aliases — only a specific description
    («黒髪の女性», «学院長»), not a generic word (「少女」), held by no one else, introduced shortly before."""
    def group(c: Any) -> bool:  # 「取り巻き」 known as 「取り巻きの二人」 is several people, whoever says otherwise
        return any(names_a_group(f) for f in [c["canonical_name"], *json.loads(c["aliases_json"] or "[]")])

    mine = {c["id"]: c for c in known if c["book_id"] == book_id}
    if same_as in mine:
        return None if group(mine[same_as]) else same_as
    hits = [c for c in mine.values() if c["canonical_name"] in aliases and c["canonical_name"] not in GENERIC_REFERENTS
            and not group(c)
            and acceptable_glossary_term(c["canonical_name"]) and 0 <= ordinal - c["known_from_ordinal"] <= REVEAL_WINDOW]
    if len(hits) != 1:
        return None
    term = hits[0]["canonical_name"]
    shared = [c for c in known if c["id"] != hits[0]["id"] and term in json.loads(c["aliases_json"] or "[]")]
    return None if shared else str(hits[0]["id"])


def _is_description(name: str) -> bool:
    """「黒髪の女性」「長い銀髪の美しい女性」「フードをかぶった少女」: words describing someone, not a name."""
    n = name.strip()
    return len(n) >= 4 and (bool(re.search(r"[のなたいる]", n[1:-1])) or any(
        n.endswith(g) and len(n) > len(g) + 1 for g in GENERIC_REFERENTS))


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
                       and a not in GENERIC_REFERENTS and a not in common and not names_a_group(a)
                       and _standalone(a, scene_text, [name, *existing_alias, *(x.name_ja for x in parsed.new_characters)])]
            proposed_zh = nc.proposed_name_zh.strip()[:MAX_NAME_LEN] or None
            # described first and named in the same scene (「長い銀髪の美しい女性」 … 「エレナ」): the model marks
            # the name among the aliases; the person is known by the name, the description stays an alias
            real = next((k.ja.strip() for k in nc.alias_kinds if k.kind == "name" and k.ja.strip() in aliases), None)
            if real and _is_description(name) and real not in existing_alias:
                aliases = [name, *(a for a in aliases if a != real)]
                name, proposed_zh = real, None
            gl = conn.execute(f"SELECT target_term FROM glossary WHERE {SERIES_SCOPE} AND source_term=?",
                              (book_id, name)).fetchone()
            zh = gl[0] if gl else proposed_zh
            cid, created = repo.upsert_character(conn, book_id, name, name_zh=zh, aliases=aliases, unit_id=first.id,
                                                 ordinal=first.ordinal, origin="scene_analysis")
            if created:
                stats["new_characters"] += 1
                if zh and acceptable_glossary_term(name):
                    repo.ensure_glossary(conn, book_id, name, zh, category="character", origin="scene_analysis",
                                         first_unit_id=first.id)
            ref_map[nc.temp_id] = cid
            ref_map[cid] = cid
            names_of[cid] = [name, *aliases]
            existing_alias[name] = cid
            same = _revealed(conn, book_id, known, nc.same_as_ref,
                             [a.strip() for a in nc.aliases_ja if a.strip() not in common], first.ordinal)
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
    program = detect_tags(texts, quotes, amap)

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
                                   [ref_map[x] for x in ua.listener_refs if x in ref_map and ref_map[x] != ref_map.get(sref or "")])
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
        res = resolve(program.get(uid_key, []), claims.get(uid_key), head)
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
    from ..memory.address import reharvest_scene

    stats["address_terms"] = reharvest_scene(conn, book_id, sid)
    return stats


def quotes_for_scene(conn: sqlite3.Connection, book_id: str, scene_id: str) -> list[Quote]:
    return find_quotes(_unit_texts(scene_rows(conn, book_id, scene_id)))
