"""A house style for the book, chosen by the model after reading (ADR-053).

A Taiwan edition keeps one way of handling honorifics through the whole book (さん rendered as 先生／小姐 or left
out, 様 as 大人 …) and keeps each character's habits of speech the same (a 「〜っす」 that always sounds the same in
Chinese). Without a choice, the translation drifts scene by scene. After reading, the model looks at how people in
this book address each other, the main characters' lines and the speech endings the program counted for each, and
chooses: the honorific policy, each main character's habits with a rendering, and a few house rules for this book.
They go into the series style (``memory.style``) unless a person has set it, and reach naming and every translation.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from typing import Any

from ..epub.units import match_text
from ..llm.schemas import HouseStyleOutput
from ..memory.address import calls_by
from ..memory.repository import character_basics
from ..memory.style import SeriesStyle, load_style, save_style
from .impressions import main_characters

LINES = 12  # spread through the book
EXAMPLES = 8  # one line for each repeated ending, so a habit is seen whatever the spread picks
_END = re.compile(r"[」』！？!?…―─。、　 ～ー\s]+$")


def _ending_counts(lines: list[str]) -> list[tuple[str, int]]:
    cnt: Counter[str] = Counter()
    for q in lines:
        q = _END.sub("", match_text(q).strip("「『"))
        for n in (2, 3, 4):
            if len(q) > n + 1:
                cnt[q[-n:]] += 1
    out: list[tuple[str, int]] = []
    for e, k in cnt.most_common(30):
        if k >= 4 and not any(e in o or o in e for o, _ in out):
            out.append((e, k))
    return out[:6]


def endings(lines: list[str]) -> list[str]:
    """Line endings this character repeats (2–4 characters, at least 4 times): candidates for a habit of speech; the
    model decides which are habits."""
    return [f"〜{e}（{k} 次）" for e, k in _ending_counts(lines)]


def _pick(said: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Lines spread through the book, then one line for each repeated ending not among them, in book order."""
    idx = set(range(len(said))) if len(said) <= LINES else {j * (len(said) - 1) // (LINES - 1) for j in range(LINES)}
    for e, _ in _ending_counts([q for q, _ in said])[:EXAMPLES]:
        shown = [i for i in idx if _END.sub("", match_text(said[i][0])).endswith(e)]
        if not shown and (hit := next((i for i, (q, _) in enumerate(said)
                                       if _END.sub("", match_text(q).strip("「『")).endswith(e)), None)) is not None:
            idx.add(hit)
    return [said[i] for i in sorted(idx)]


def well_placed(conn: sqlite3.Connection, utterance_id: str, character_id: str) -> bool:
    """The reading put this line to this character on strong evidence (a tag, the action beside it, an answer to
    the name, knowledge only they have, a quote that runs on), not on how it sounds."""
    from ..llm.schemas import STRONG_EVIDENCE

    for (ev,) in conn.execute("SELECT evidence_json FROM speaker_hypotheses WHERE utterance_id=? AND "
                              "candidate_character_id=?", (utterance_id, character_id)):
        try:
            if set(json.loads(ev).get("evidence_types") or []) & STRONG_EVIDENCE:
                return True
        except (ValueError, AttributeError):
            continue
    return False


def request(conn: sqlite3.Connection, book_id: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Characters with lines spread through the book (with the current translation of each line's paragraph when there
    is one), the forms of address, and the names already chosen: a habit's rendering keeps to what the book already
    says, and names and nicknames are naming's choice, not the house style's."""
    from ..epub.units import PH_RE
    from ..translation.cache import latest_texts

    best = latest_texts(conn, book_id)
    chars, refs, items = main_characters(conn, book_id), {}, []
    for k, c in enumerate(chars, 1):
        rows = conn.execute("SELECT id, quote_text, unit_id, status FROM utterances WHERE speaker_id_nullable=? AND "
                            "book_id=? ORDER BY id", (c["id"], book_id)).fetchall()
        said = [(r[1], r[2]) for r in rows]
        # read as his on weak grounds only (the way he talks, a pronoun): never a habit alone — the habit would be
        # proved by itself. A line he is placed in by what the text shows (the action beside it, answering to his
        # name) counts: in a light novel few lines carry a tag (2 of 49 in one prologue), and none would count.
        guessed = {(r[1], r[2]) for r in rows if r[3] == "inferred" and not well_placed(conn, r[0], c["id"])}
        lines: list[dict[str, Any]] = []
        for q, uid in _pick(said):
            line: dict[str, Any] = {"ja": match_text(q)[:80]}
            if (q, uid) in guessed:
                line["guess"] = True
            if (t := best.get(uid)) is not None:
                line["zh"] = PH_RE.sub("", t["text"]).strip()[:120]  # the paragraph as translated now
            lines.append(line)
        from ..memory.repository import impressions_of

        # the whole book's view (a style for the book): every stretch, with where it holds from
        stretches = impressions_of(conn, c["id"])
        voice = "；".join((f"〔{s['period']}〕" if s["period"] else f"〔§{s['since_ordinal']}起〕" if len(stretches) > 1 else "")
                         + s["voice"] for s in stretches)
        refs[f"c{k}"] = c["id"]
        items.append({"ref": f"c{k}", "name": c["display_name_zh"] or c["canonical_name"], "ja": c["canonical_name"],
                      **character_basics(conn, c["id"]), **({"voice": voice} if voice else {}), "lines": lines,
                      "repeated_endings": endings([q for q, _ in said])})
    counted: dict[str, int] = {}
    for a in conn.execute("SELECT * FROM address_terms WHERE book_id=?", (book_id,)):
        if calls_by(conn, a):  # a title talked about (『賢者』が死んだ) is not a way of calling anyone
            counted[a["form_ja"]] = counted.get(a["form_ja"], 0) + int(a["uses"] or 0)
    forms = [f"{f}×{n}" for f, n in sorted(counted.items(), key=lambda x: -x[1])[:40]]
    names = [f"{g['source_term']}→{g['target_term']}" for g in conn.execute(
        "SELECT source_term, target_term FROM glossary WHERE book_id=? ORDER BY verified DESC, id LIMIT 80", (book_id,))]
    req: dict[str, Any] = {"task": "house_style", "characters": items, "address_forms": forms}
    if names:
        req["names"] = names
    return req, refs


def apply(conn: sqlite3.Connection, book_id: str, parsed: HouseStyleOutput, refs: dict[str, str]) -> dict[str, int]:
    """Into the series style when no person has set it (or the model set it before); returns what was kept."""
    style = load_style(conn, book_id)
    if not (style.is_default() or style.auto):
        return {"kept_human_style": 1}
    names = {r["id"]: r["canonical_name"] for r in conn.execute("SELECT id, canonical_name FROM characters")}
    voices: dict[str, str] = {}
    for h in parsed.habits:
        cid = refs.get(h.ref)
        if not cid or not h.habit_ja.strip() or not h.render_zh.strip():
            continue
        line = f"「{h.habit_ja.strip()}」譯作「{h.render_zh.strip()}」" + (f"（{h.note.strip()}）" if h.note.strip() else "")
        voices[names[cid]] = (voices.get(names[cid], "") + "；" + line).lstrip("；")
    new = SeriesStyle(honorifics=parsed.honorifics, rules=[r.strip() for r in parsed.rules if r.strip()][:6],
                      voices=voices, notes=style.notes, auto=True)
    save_style(conn, book_id, new)
    return {"habits": len(parsed.habits), "rules": len(new.rules)}


def names_seen(conn: sqlite3.Connection, book_id: str) -> dict[str, str]:
    """The renderings in the glossary now (``ja`` → ``zh``): what the house style is chosen with."""
    return {g["source_term"]: g["target_term"] for g in conn.execute(
        "SELECT source_term, target_term FROM glossary WHERE book_id=?", (book_id,)) if g["target_term"]}


def follow_renames(conn: sqlite3.Connection, book_id: str, seen: dict[str, str]) -> dict[str, str]:
    """Naming came after the house style (ADR-053) and may render a name differently from what the style saw: the
    old rendering is replaced with the new one in the model-chosen style's voices. A plain string replacement, like a
    rename (``translation/rename.py``): nothing is judged. A person's style is not touched. An old rendering of one
    character is left alone: it is also part of other words (「米」 in 「米飯」), and a single character is not matched
    anywhere else in the program either. Returns old → new (what was replaced)."""
    style = load_style(conn, book_id)
    if not style.auto or not style.voices:
        return {}
    now = names_seen(conn, book_id)
    changed = {old: now[ja] for ja, old in seen.items() if len(old) >= 2 and now.get(ja) and now[ja] != old}
    if not changed:
        return {}
    # one pass, longest first: a new name that contains an old one is not replaced twice
    old = re.compile("|".join(re.escape(o) for o in sorted(changed, key=len, reverse=True)))
    voices = {who: old.sub(lambda m: changed[m.group(0)], text) for who, text in style.voices.items()}
    if voices != style.voices:
        save_style(conn, book_id, style.model_copy(update={"voices": voices}))
    return changed
