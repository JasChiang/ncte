"""Memory retrieval for the context builder + FTS5 search (spec §5.6). All results are horizon-filtered."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
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


QUOTA_UNITS = 25  # a quota (facts, forms of address) is for this many paragraphs of the batch
QUOTA_MAX_TIMES = 4  # a long batch gets at most this many quotas
MEMORY_CHARS_MAX = 8000  # the memory block of a long batch grows with it up to this many characters


def batch_quota(base: int, n_units: int) -> int:
    """``base`` per QUOTA_UNITS paragraphs of the batch (rounded up), at least ``base``, at most QUOTA_MAX_TIMES times
    it (ADR-069): a whole scene of 120 paragraphs in one request is not given the handful of facts a 20-paragraph batch
    gets. Only how many are kept changes, not how they are chosen."""
    return base * min(QUOTA_MAX_TIMES, max(1, -(-n_units // QUOTA_UNITS)))


def memory_chars(chars_budget: int, n_units: int) -> int:
    """The memory block's character budget for a batch of ``n_units`` paragraphs: grows like the fact quota, capped."""
    return max(chars_budget, min(MEMORY_CHARS_MAX, batch_quota(chars_budget, n_units)))


def memory_context(conn: sqlite3.Connection, book_id: str, *, scene: sqlite3.Row, batch_rows: list[sqlite3.Row],
                   max_evidence: int = 8, chars_budget: int = 2400,
                   window_rows: list[sqlite3.Row] | None = None, fix_facts: int = 4,
                   compact: bool = True) -> dict[str, Any]:
    """Characters, scene state, summaries, facts and props visible at this scene.

    ``max_evidence`` and ``chars_budget`` are for a batch of up to QUOTA_UNITS paragraphs; a longer batch gets one
    more quota of facts per QUOTA_UNITS paragraphs (rounded up, at most QUOTA_MAX_TIMES times) and a character budget
    grown in proportion, up to MEMORY_CHARS_MAX (``batch_quota``, ``memory_chars``; ADR-069).

    ``window_rows`` (a fix in the "window" scope: the paragraphs shown around it): only what the fixed paragraphs and
    that window need — the people who speak, are spoken to, are named or do (or undergo) a recorded action there, and
    the narrator of a first-person scene; their lines and actions; ``fix_facts`` facts chosen for that text; the
    latest summary of the chapter and no recap of the one before. Known as of the window's last paragraph (a woman
    named two paragraphs after the fix is on the cards: her 妳 was once changed to 你).

    ``compact`` (``translation.compact_payload``): the same blocks in fewer characters — a line's ``quote`` its first
    QUOTE_CHARS characters, a fact's ``source`` an excerpt around what it is about (``fact_excerpt``), a line's
    ``quote_index`` left out when it is its paragraph's only line and its ``status`` when ``explicit``, COMPACT_EXAMPLES
    speech examples a character (the prompts say what a missing field means). Which facts are kept is decided on the
    full form, as without it: fewer characters, not more facts."""
    fix = window_rows is not None
    seen = sorted([*batch_rows, *(window_rows or [])], key=lambda r: r["ordinal"])
    max_evidence = fix_facts if fix else batch_quota(max_evidence, len(batch_rows))
    chars_budget = memory_chars(chars_budget, len(batch_rows))
    horizon = scene["start_ordinal"]
    upto = seen[-1]["ordinal"]
    seen_ids = {r["id"] for r in seen}
    seen_text = match_text(" ".join(r["plain_text"] for r in seen))
    scene_text = " ".join(match_text(r["plain_text"]) for r in conn.execute(
        "SELECT plain_text FROM units WHERE book_id=? AND scene_id=? AND ordinal <= ? ORDER BY ordinal",
        (book_id, scene["id"], upto)))
    state = json.loads(scene["state_json"] or "{}")
    narration = state.get("narration") or {}
    if not fix:
        participants = {p["character_id"] for p in state.get("participants", [])}
        # the scene's narrator (named or not, rarely named in their own narration) and whoever speaks in it are present
        participants |= {x for x in (narration.get("narrator"),) if x}
        participants |= {r[0] for r in conn.execute("SELECT DISTINCT speaker_id_nullable FROM utterances WHERE book_id=? "
                                                    "AND scene_id=? AND speaker_id_nullable IS NOT NULL", (book_id, scene["id"]))}
    else:  # only who is in the window: speaks there, is spoken to, does or undergoes an action the source leaves out
        participants = {narration["narrator"]} if narration.get("narrator") and narration.get("person") == "first" \
            else set()
        for u in conn.execute("SELECT unit_id, speaker_id_nullable, addressee_ids_json FROM utterances WHERE book_id=? "
                              "AND scene_id=?", (book_id, scene["id"])):
            if u["unit_id"] in seen_ids:
                participants |= {x for x in [u["speaker_id_nullable"], *json.loads(u["addressee_ids_json"] or "[]")] if x}
        for a in state.get("actions", []):
            if a.get("unit_id") in seen_ids:
                participants |= {*a.get("subject_candidates", []), *a.get("object_candidates", [])}
    forms_at = vis.form_positions(conn, book_id)
    views = {c["id"]: v for c in vis.visible_characters(conn, book_id, upto)
             if (v := vis.character_view(conn, book_id, c, upto, forms_at)) is not None}
    chars = [c for c in vis.visible_characters(conn, book_id, upto) if c["id"] in views]
    names = {cid: (v["name_zh"] or v["name_ja"]) for cid, v in views.items()}
    from .repository import readings_of

    said = readings_of(conn, book_id)  # how the source annotates a name's reading (彩葉《いろは》)
    present = []
    for c in chars:
        v = views[c["id"]]
        aliases = [v["name_ja"], *v["aliases_ja"]]
        if c["id"] not in participants and not any(a in (seen_text if fix else scene_text) for a in aliases):
            continue
        prof = json.loads(c["speech_profile_json"] or "{}")
        examples = []
        for e in vis.visible_examples(conn, c["id"], horizon, book_id=book_id,
                                      **({"limit": COMPACT_EXAMPLES} if compact else {})):
            item = {"ja": e["raw_text"]}
            tr = latest_translation(conn, e["unit_id"], conn.execute(
                "SELECT source_hash FROM units WHERE id=?", (e["unit_id"],)).fetchone()[0])
            if tr is not None:
                item["unit_translation"] = tr["text"]
            examples.append(item)
        card = {"character_id": c["id"], "name_ja": v["name_ja"], "name_zh": v["name_zh"],
                "aliases_ja": v["aliases_ja"], "speech_profile": prof, "verified_examples": examples}
        if said.get(v["name_ja"]):
            card["readings"] = said[v["name_ja"]]
        if v.get("gender"):
            card["gender"] = v["gender"]
        if v.get("narrator"):
            card["narrator"] = True
        from .repository import impression_at

        # how this character talks as of this scene (ADR-045, ADR-060: the stretch in force where it begins — a change
        # inside the scene is not given to the lines before it; never the place in the story — no reveals)
        if imp := impression_at(conn, c["id"], scene["start_ordinal"]):
            card["voice"] = imp["voice"]
            if imp.get("other_times"):
                card["voice_other_times"] = imp["other_times"]
        present.append(card)
    # relations among the people of this scene, as the story has shown them so far (ADR-039): how they talk to each other
    ids = {c["character_id"] for c in present}
    for r in conn.execute("SELECT * FROM relations WHERE book_id=? AND since_ordinal<=? AND (until_ordinal IS NULL OR "
                          "until_ordinal>?)", (book_id, horizon, horizon)):  # begun and not yet replaced
        if r["a_id"] in ids and r["b_id"] in ids:
            sure = {"certainty": "inferred"} if r["certainty"] == "inferred" else {}  # read from the story, not stated
            for card in present:
                if card["character_id"] == r["a_id"]:
                    card.setdefault("relations", []).append({"with": names.get(r["b_id"]), "is": r["a_is"] or r["relation_zh"],
                                                             **sure})
                elif card["character_id"] == r["b_id"]:
                    card.setdefault("relations", []).append({"with": names.get(r["a_id"]), "is": r["b_is"] or r["relation_zh"],
                                                             **sure})
    # a fix gets its own paragraphs' lines and actions only: the window around it is there as text (measured on a whole
    # book, the window's lines were 30% of a fix request); who is in the window still gets a card (above)
    batch_ids = {r["id"] for r in batch_rows}
    utts = []
    # in story order: by unit id the lines came in a hashed order (P35, P22, P16 …)
    lines = [u for u in conn.execute("SELECT t.* FROM utterances t JOIN units n ON n.id=t.unit_id WHERE t.book_id=? AND "
                                     "t.scene_id=? ORDER BY n.ordinal, t.quote_index", (book_id, scene["id"]))
             if u["unit_id"] in batch_ids]
    per_unit = Counter(u["unit_id"] for u in lines)
    for u in lines:
        sid = u["speaker_id_nullable"]
        if compact:  # a paragraph's only line needs no index; a stated speaker no status (said so in the prompts)
            item = {"unit_id": u["unit_id"],
                    **({"quote_index": u["quote_index"]} if per_unit[u["unit_id"]] > 1 or u["quote_index"] else {}),
                    "quote": clip(u["quote_text"], QUOTE_CHARS), "speaker": names.get(sid) if sid else None,
                    **({"status": u["status"]} if u["status"] != "explicit" else {})}
        else:
            item = {"unit_id": u["unit_id"], "quote_index": u["quote_index"], "quote": u["quote_text"][:60],
                    "speaker": names.get(sid) if sid else None, "status": u["status"]}
        to = [names[x] for x in json.loads(u["addressee_ids_json"] or "[]") if x in names]
        if to:  # who the line is said to: 你 or 妳 follows that person, not the last woman mentioned
            item["to"] = to
            if (u["addressee_status"] if "addressee_status" in u.keys() else None) != "explicit":
                item["to_certainty"] = "inferred"  # read from the scene, not stated
        utts.append(item)
    actions = [a for a in state.get("actions", []) if a.get("unit_id") in batch_ids]
    for a in actions:
        a["subject_candidates"] = [names.get(x, x) for x in a.get("subject_candidates", [])]
        a["object_candidates"] = [names.get(x, x) for x in a.get("object_candidates", [])]
    same_ch, prev_ch = story_so_far(conn, book_id, scene)
    present_ids = {p["character_id"] for p in present}
    batch_plain = seen_text if fix else match_text(" ".join(r["plain_text"] for r in batch_rows))
    chosen = chosen_facts(vis.visible_facts(conn, book_id, horizon), batch_plain, present, names, limit=max_evidence)
    facts = [item for _, item in chosen]
    props = [{"item": p["name_ja"], "holder": names.get(p["holder_character_id"]) if p["holder_character_id"] else None,
              **({"period": p["period"]} if p["period"] else {})}
             for p in vis.visible_props(conn, book_id, horizon) if p["name_ja"] in (seen_text if fix else scene_text)]
    mem: dict[str, Any] = {
        "note": "Only information revealed before this scene. 'inferred'/'unknown' items are NOT facts.",
        **({} if fix else {"previous_chapter_summary": prev_ch}),
        "this_chapter_so_far": _cap(same_ch, 1 if fix else 4),
        "facts": _cap(facts, max_evidence),
        "props": props,
    }
    # keep the memory block within a character budget (drop oldest facts/summaries first)
    while len(json.dumps(mem, ensure_ascii=False)) > chars_budget and (mem["facts"] or mem["this_chapter_so_far"]):
        if mem["facts"]:
            mem["facts"].pop(0)
        else:
            mem["this_chapter_so_far"].pop(0)
    if compact:  # kept as above; each source then shortened to the part around what the fact is about
        rows_of = {id(item): f for f, item in chosen}
        for item in mem["facts"]:
            item["source"] = fact_excerpt(rows_of[id(item)], present, _grams(batch_plain))
    from .address import FORMS_PER_QUOTA, context_items

    batch_text = " ".join(r["plain_text"] for r in (seen if fix else batch_rows))
    batch_speakers = {u["speaker_id_nullable"] for u in conn.execute(
        f"SELECT speaker_id_nullable FROM utterances WHERE unit_id IN ({','.join('?' * len(batch_ids))}) AND "
        "speaker_id_nullable IS NOT NULL", list(batch_ids))}
    all_names = names  # the names known here: a name revealed later never reaches a form of address
    if fix:  # a form of address between two of the people on the cards, not one of the scene's others
        all_names = {cid: n for cid, n in names.items() if cid in present_ids}
    out: dict[str, Any] = {
        "characters": present,
        "scene": {"scene_id": scene["id"], "pov": state.get("pov"), "location": state.get("location"),
                  "utterances": utts, "omitted_arguments": actions,
                  "uncertainties": [_question(x, names) for x in state.get("uncertainties", [])
                                    if set(x.get("unit_ids", [])) & batch_ids]},
        "memory": mem,
    }
    address = context_items(conn, book_id, horizon, batch_text, batch_speakers, all_names, present_ids, upto=upto,
                            limit=batch_quota(FORMS_PER_QUOTA, len(batch_rows)), compact=compact)
    if address:
        out["address_terms"] = address
    return out


_KEY = re.compile(r"[\u3400-\u9fff\u30a0-\u30ff々ー]")
_IDS = re.compile(r"(?<![0-9A-Za-z_])c_[0-9a-f]{12}(?![0-9a-f])")


def _grams(text: str) -> set[str]:
    """Character bigrams that carry meaning (at least one kanji or katakana): particles and endings left out."""
    t = re.sub(r"\s", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1) if _KEY.search(t[i:i + 2])}


WORLD_EXTRA = 0.5  # how much more a fact about no one present must share with the text to be given


def fact_overlap(f: Any, grams: set[str]) -> float:
    """How much a fact's source sentence shares with a text (meaningful bigrams, scaled by the sentence's length)."""
    qg = _grams(match_text(f["evidence_quote"] or ""))
    return len(grams & qg) / (len(qg) ** .5) if qg else 0.0


def select_facts(rows: list[Any], batch_text: str, present: list[dict[str, Any]], names: dict[str, str], *,
                 limit: int = 8, min_score: float = 1.0) -> list[dict[str, Any]]:
    """The facts most related to *this batch* (not every fact about whoever is present): overlap between the
    batch's words and each fact's original evidence sentence, plus a bonus when the batch names the fact's
    subject. Each fact carries its Japanese evidence so the model reads the source, not only our paraphrase."""
    return [item for _, item in chosen_facts(rows, batch_text, present, names, limit=limit, min_score=min_score)]


def chosen_facts(rows: list[Any], batch_text: str, present: list[dict[str, Any]], names: dict[str, str], *,
                 limit: int = 8, min_score: float = 1.0) -> list[tuple[Any, dict[str, Any]]]:
    """``select_facts`` with each fact's row (its full evidence, for ``fact_excerpt``)."""
    bg = _grams(batch_text)
    named = {p["character_id"] for p in present if any(n and n in batch_text for n in [p["name_ja"], *p["aliases_ja"]])}
    present_ids = {p["character_id"] for p in present}
    scored = []
    for i, f in enumerate(rows):
        about = (f["subject"] in present_ids or f["subject"] in batch_text
                 or any(p["name_ja"] in (f["subject"] + f["object"]) for p in present))
        overlap = fact_overlap(f, bg)
        # a fact about the world (アルテイン vanished from the books) counts when the batch talks about the same thing
        if not about and overlap < min_score + WORLD_EXTRA:
            continue
        score = overlap + (1.5 if f["subject"] in named else 0) + i / max(1, len(rows)) * .3  # later facts: closer
        if score >= min_score:
            scored.append((score, i, f))
    keep = sorted(sorted(scored, key=lambda x: -x[0])[:limit], key=lambda x: x[1])  # most relevant, story order

    def nm(text: str | None) -> str:
        return _IDS.sub(lambda m: names.get(m.group(0), m.group(0)), text or "")

    return [(f, {"subject": names.get(f["subject"], f["subject"]), "predicate": nm(f["predicate"]),
                 "object": nm(f["object"]), "type": f["fact_type"], "certainty": f["certainty"],
                 **({"viewpoint": nm(f["source_viewpoint"])} if f["source_viewpoint"] else {}),
                 **({"period": f["period"]} if "period" in f.keys() and f["period"] else {}),
                 "source": match_text(f["evidence_quote"] or "")[:90]}) for _, _, f in keep]


QUOTE_CHARS = 16  # a line's quote only says which line it is (the paragraph is in the request)
FACT_SOURCE_CHARS = 60  # a fact's source: the part of its evidence around what it is about, marks included
COMPACT_EXAMPLES = 2  # speech examples a character (human-verified first, then the latest before the scene)


def clip(text: str, n: int) -> str:
    """The first ``n`` characters, 「…」 after them when the text goes on."""
    return text if len(text) <= n else text[:n] + "…"


def fact_excerpt(f: Any, present: list[dict[str, Any]], grams: set[str], width: int = FACT_SOURCE_CHARS) -> str:
    """At most ``width`` characters of a fact's evidence, 「…」 where it is cut: around the first of what the fact is
    about that the evidence writes (its subject's Japanese name or other names on the card, the subject or object as
    written, the person the object refers to), kept whole; when it writes none of them, the stretch sharing the most
    with the batch's words (``_grams``), else its beginning. Which part is shown only; the fact is unchanged."""
    text = match_text(f["evidence_quote"] or "")
    if len(text) <= width:
        return text
    cards = {p["character_id"]: [p["name_ja"], *p["aliases_ja"]] for p in present}
    obj_ref = f["object_ref"] if "object_ref" in f.keys() else None
    terms = [*cards.get(f["subject"], [f["subject"]]), f["object"], *cards.get(obj_ref or "", [])]
    inner = width - 2  # room for a mark on each side
    at = next(((i, len(t)) for t in terms if t and len(t) <= inner and (i := text.find(t)) >= 0), None)
    if at is not None:
        start = max(0, min(at[0] + at[1] // 2 - inner // 2, len(text) - inner))
    else:
        def shared(i: int) -> int:
            return len(_grams(text[i:i + inner]) & grams)
        start = max(range(len(text) - inner + 1), key=lambda i: (shared(i), -i))
    return ("…" if start else "") + text[start:start + inner] + ("…" if start + inner < len(text) else "")


RECAP_CHARS = 1200  # the previous chapter's recap: its scene summaries, the latest that fit


def story_so_far(conn: sqlite3.Connection, book_id: str, scene: sqlite3.Row) -> tuple[list[str], list[str]]:
    """(this chapter's scene summaries so far, the recap of the chapter before): what a reader knows on reaching
    this scene. Chapters from the TOC (a chapter may span several files); a book cut the old way counts a file as a
    chapter and its recap is the last two summaries before it. The first chapter's recap is the last of the book
    (volume) before, in a series."""
    summaries = vis.visible_summaries(conn, book_id, scene["start_ordinal"])
    from ..epub.chapters import chapter_of

    ch = chapter_of(conn, book_id, scene["start_ordinal"])
    if ch is None:
        here = (book_id, scene_chapter(conn, scene))
        return ([s["summary"] for s in summaries if (s["book_id"], s["chapter_ref"]) == here],
                [s["summary"] for s in summaries if (s["book_id"], s["chapter_ref"]) != here][-2:])
    mine = [s for s in summaries if s["book_id"] == book_id]
    same = [s["summary"] for s in mine if s["horizon_ordinal"] >= ch["start_ordinal"]]
    before = [s for s in mine if s["horizon_ordinal"] < ch["start_ordinal"]]
    if before:  # the chapter before that has summaries (book matter has none)
        prev = chapter_of(conn, book_id, before[-1]["horizon_ordinal"])
        recap = [s["summary"] for s in before if prev is None or s["horizon_ordinal"] >= prev["start_ordinal"]]
    else:
        recap = [s["summary"] for s in summaries if s["book_id"] != book_id][-2:]  # the volume before
    kept: list[str] = []
    for text in reversed(recap):
        if kept and sum(map(len, kept)) + len(text) > RECAP_CHARS:
            break
        kept.insert(0, text)
    return same, kept


def scene_chapter(conn: sqlite3.Connection, scene: sqlite3.Row) -> str:
    row = conn.execute("SELECT resource_href FROM units WHERE id=?", (scene["start_unit_id"],)).fetchone()
    return row[0] if row else ""


def _question(x: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """A question the read pass raised, with the second look's answer when there is one (ADR-067): who it names is
    shown only if known by this point of the story."""
    out = {"unit_ids": x.get("unit_ids", []), "question": x.get("question", "")}
    if x.get("answer"):
        out["answer"] = x["answer"]
        if (who := names.get(x.get("answer_character_id") or "")):
            out["answer_who"] = who
    return out


def unit_actions(conn: sqlite3.Connection, book_id: str,
                 unit_ids: Iterable[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """unit → its sentences whose subject or object the source leaves out, as the read pass judged them (the scene's
    ``actions``): who does it, to whom, and how sure that reading is — named as known at the end of the scene (as the
    scene was translated; a name revealed later stays out). Handed to the steps that judge a translation (ADR-067);
    nothing here decides who did it."""
    wanted = set(unit_ids) if unit_ids is not None else None
    if wanted is not None and not wanted:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    forms_at = None
    for s in conn.execute("SELECT id, state_json FROM scenes WHERE book_id=?", (book_id,)):
        acts = [a for a in json.loads(s["state_json"] or "{}").get("actions", [])
                if wanted is None or a.get("unit_id") in wanted]
        if not acts:
            continue
        end = conn.execute("SELECT MAX(ordinal) FROM units WHERE book_id=? AND scene_id=?", (book_id, s["id"])).fetchone()[0]
        forms_at = vis.form_positions(conn, book_id) if forms_at is None else forms_at
        names = vis.visible_names(conn, book_id, end, forms_at) if end is not None else {}
        for a in acts:
            out.setdefault(a["unit_id"], []).append({
                "summary": a.get("summary", ""),
                "subject_candidates": [names.get(x, "？") for x in a.get("subject_candidates", [])],
                "object_candidates": [names.get(x, "？") for x in a.get("object_candidates", [])],
                "resolution_status": a.get("status") or a.get("resolution_status") or "unknown"})
    return out
