"""Address terms: who calls whom what (ADR-022).

Japanese forms of address depend on the speaker: 日野 calls 安達 「あだっち」, 島村's sister calls 島村 「姉ちゃん」, and a
change of form (「安達さん」 → 「安達」) is often a plot signal. One row per (speaker, addressee, form) with the story
positions where it was first and last used, the number of uses and the rendering to keep. ``addressee == speaker``
means the speaker refers to themself by that name.

Sources, in order of authority:
* ``program`` – name forms (with an attached honorific) found inside quotes whose speaker is resolved
  (``explicit`` / ``inferred``; never ``unknown``). Idempotent per utterance (``address_observations``).
* ``llm``     – memory_update proposals with a verbatim evidence quote; may supply the rendering used.
* ``human``   – ``ncte names address-set``; never overwritten.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..lexicon import GENERIC_REFERENTS
from ..series import SERIES_SCOPE, Pos, pos, volumes
from .db import utcnow

HONORIFICS = ("ちゃん", "さん", "くん", "君", "様", "さま", "先輩", "せんぱい", "先生", "せんせい", "殿", "氏", "たん",
              "っち", "ちゃま", "にゃん")
_HONORIFIC_RE = re.compile("(" + "|".join(sorted(map(re.escape, HONORIFICS), key=len, reverse=True)) + ")")
_HONORIFIC_END = re.compile("(" + "|".join(map(re.escape, HONORIFICS)) + ")$")
RESOLVED = ("explicit", "inferred", "human_verified")
_PRONOUNS = {"私", "わたし", "僕", "ぼく", "俺", "おれ", "あたし", "うち", "あなた", "君", "きみ", "お前", "おまえ",
             "彼", "彼女", "あいつ", "こいつ", "そいつ"}


def name_forms(conn: sqlite3.Connection, book_id: str) -> tuple[dict[str, str], set[str]]:
    """(form → character id, ambiguous forms). Forms come from character names/aliases and from every glossary
    form of an entity that belongs to a character. A form claimed by two characters, or listed in
    ``form_senses``, is ambiguous and never attributed automatically."""
    chars = conn.execute(f"SELECT id, canonical_name, aliases_json FROM characters WHERE {SERIES_SCOPE}",
                         (book_id,)).fetchall()
    claims: dict[str, set[str]] = {}

    def claim(form: str | None, cid: str) -> None:
        base = _HONORIFIC_END.sub("", form or "")  # 「店員さん」 is still the generic 「店員」
        if form and len(form) >= 2 and not {form, base} & (GENERIC_REFERENTS | _PRONOUNS):
            claims.setdefault(form, set()).add(cid)

    by_name: dict[str, str] = {}
    for c in chars:
        for f in [c["canonical_name"], *json.loads(c["aliases_json"])]:
            claim(f, c["id"])
            by_name.setdefault(f, c["id"])
    groups: dict[str, list[str]] = {}
    for term, eid in conn.execute(f"SELECT source_term, entity_id FROM glossary WHERE {SERIES_SCOPE} AND "
                                  "entity_id IS NOT NULL", (book_id,)):
        groups.setdefault(eid, []).append(term)
    for forms in groups.values():
        owners = {by_name[f] for f in forms if f in by_name}
        if len(owners) == 1:
            cid = next(iter(owners))
            for f in forms:
                claim(f, cid)
    shared = {r[0] for r in conn.execute(f"SELECT term FROM form_senses WHERE {SERIES_SCOPE}", (book_id,))}
    ambiguous = {f for f, ids in claims.items() if len(ids) > 1} | shared
    return {f: next(iter(ids)) for f, ids in claims.items() if f not in ambiguous}, ambiguous


def find_forms(text: str, forms: dict[str, str]) -> list[tuple[str, str]]:
    """(form as used, incl. honorific; character id) for every name form in ``text``, longest match first."""
    t = match_text(text)
    taken = [False] * len(t)
    out: list[tuple[str, str]] = []
    for f in sorted(forms, key=len, reverse=True):
        start = 0
        while (i := t.find(f, start)) >= 0:
            end = i + len(f)
            start = end
            if any(taken[i:end]):
                continue
            m = _HONORIFIC_RE.match(t, end)
            if m and not f.endswith(m.group(1)):
                end = m.end()
            for k in range(i, end):
                taken[k] = True
            out.append((t[i:end], forms[f]))
    return out


def _glossary_zh(conn: sqlite3.Connection, book_id: str, form: str) -> str | None:
    row = conn.execute(f"SELECT target_term FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, form)).fetchone()
    return str(row[0]) if row else None


def upsert(conn: sqlite3.Connection, book_id: str, speaker: str, addressee: str, form: str, *, ordinal: int,
           unit_id: str | None, origin: str, certainty: str, form_zh: str | None = None, quote: str | None = None,
           note: str | None = None, utterance_id: str | None = None) -> int:
    """Record one use. Returns the row id. Re-processing the same utterance does not count it twice."""
    here: Pos = pos(conn, book_id, ordinal)
    row = conn.execute("SELECT * FROM address_terms WHERE speaker_id=? AND addressee_id=? AND form_ja=?",
                       (speaker, addressee, form)).fetchone()
    created = row is None
    if row is None:
        zh, zh_origin = (form_zh, origin) if form_zh else (_glossary_zh(conn, book_id, form), "glossary")
        cur = conn.execute(
            "INSERT INTO address_terms(book_id, speaker_id, addressee_id, form_ja, form_zh, zh_origin, note, origin, "
            "certainty, first_unit_id, first_ordinal, last_book_id, last_ordinal, uses, evidence_quote, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (book_id, speaker, addressee, form, zh, zh_origin if zh else None, note, origin, certainty, unit_id,
             ordinal, book_id, ordinal, 0, (quote or "")[:300] or None, utcnow(), utcnow()))
        rid = int(cur.lastrowid or 0)
        row = conn.execute("SELECT * FROM address_terms WHERE id=?", (rid,)).fetchone()
    rid = int(row["id"])
    if utterance_id is not None:
        if conn.execute("INSERT OR IGNORE INTO address_observations(utterance_id, address_id) VALUES (?,?)",
                        (utterance_id, rid)).rowcount == 0:
            return rid
    # uses = quotes actually observed; a form only the model reported counts once
    counted = utterance_id is not None or (created and origin == "llm")
    sets: dict[str, Any] = {"uses": row["uses"] + int(counted), "updated_at": utcnow()}
    if here < pos(conn, row["book_id"], row["first_ordinal"]):
        sets.update(book_id=book_id, first_ordinal=ordinal, first_unit_id=unit_id)
    if row["last_ordinal"] is None or here > pos(conn, row["last_book_id"] or row["book_id"], row["last_ordinal"]):
        sets.update(last_book_id=book_id, last_ordinal=ordinal)
    if certainty == "explicit" and row["certainty"] != "explicit" and row["origin"] != "human":
        sets["certainty"] = "explicit"
    # a rendering from a better source replaces a glossary default; a human rendering is never replaced
    rank = {None: 0, "glossary": 1, "llm": 2, "human": 3}
    if form_zh and rank.get(origin, 0) >= rank.get(row["zh_origin"], 0) and row["zh_origin"] != "human":
        sets.update(form_zh=form_zh, zh_origin=origin)
    if note and not row["note"]:
        sets["note"] = note
    if quote and not row["evidence_quote"]:
        sets["evidence_quote"] = quote[:300]
    conn.execute(f"UPDATE address_terms SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), rid))
    return rid


def harvest_scene(conn: sqlite3.Connection, book_id: str, scene_id: str,
                  forms: tuple[dict[str, str], set[str]] | None = None) -> int:
    """Program-only pass over the resolved quotes of one scene. Returns the number of new observations."""
    fmap, _ = forms or name_forms(conn, book_id)
    if not fmap:
        return 0
    n = 0
    rows = conn.execute(
        "SELECT ut.*, u.ordinal FROM utterances ut JOIN units u ON u.id=ut.unit_id WHERE ut.book_id=? AND ut.scene_id=? "
        f"AND ut.speaker_id_nullable IS NOT NULL AND ut.status IN ({','.join('?' * len(RESOLVED))}) ORDER BY u.ordinal",
        (book_id, scene_id, *RESOLVED)).fetchall()
    for ut in rows:
        for form, cid in find_forms(ut["quote_text"], fmap):
            before = conn.total_changes
            upsert(conn, book_id, ut["speaker_id_nullable"], cid, form, ordinal=ut["ordinal"], unit_id=ut["unit_id"],
                   origin="program", certainty="explicit" if ut["status"] != "inferred" else "inferred",
                   quote=ut["quote_text"], utterance_id=ut["id"])
            n += int(conn.total_changes > before)
    return n


def harvest_book(conn: sqlite3.Connection, book_id: str) -> int:
    forms = name_forms(conn, book_id)
    return sum(harvest_scene(conn, book_id, r[0], forms)
               for r in conn.execute("SELECT id FROM scenes WHERE book_id=? ORDER BY start_ordinal", (book_id,)).fetchall())


def visible_terms(conn: sqlite3.Connection, book_id: str, horizon: int, text: str | None = None,
                  speakers: set[str] | None = None, present: set[str] | None = None) -> list[sqlite3.Row]:
    """Address terms first used before story position (book, ``horizon``); optionally only forms in ``text``
    or used by ``speakers`` for someone ``present``. Sorted by first use (a change of form shows as a later row)."""
    hz = pos(conn, book_id, horizon)
    vols = volumes(conn)
    t = match_text(text) if text is not None else None
    out = []
    for r in conn.execute(f"SELECT * FROM address_terms WHERE {SERIES_SCOPE}", (book_id,)):
        if (vols.get(r["book_id"], 1.0), r["first_ordinal"]) >= hz:
            continue
        if t is not None and r["form_ja"] not in t and not (
                speakers and r["speaker_id"] in speakers and present is not None and r["addressee_id"] in present):
            continue
        out.append(r)
    return sorted(out, key=lambda r: (vols.get(r["book_id"], 1.0), r["first_ordinal"]))


def context_items(conn: sqlite3.Connection, book_id: str, horizon: int, text: str, speakers: set[str],
                  names: dict[str, str], present: set[str] | None = None, limit: int = 12) -> list[dict[str, Any]]:
    """Translation payload: forms occurring in the batch, plus how this batch's speakers usually call the
    characters present in the scene (the form they would use if they named them)."""
    vols = volumes(conn)
    out = []
    for r in visible_terms(conn, book_id, horizon, text, speakers, present or set()):
        if r["speaker_id"] not in names or r["addressee_id"] not in names:
            continue
        item: dict[str, Any] = {"speaker": names[r["speaker_id"]],
                                "to": "（自稱）" if r["speaker_id"] == r["addressee_id"] else names[r["addressee_id"]],
                                "ja": r["form_ja"], "uses": r["uses"],
                                "since": f"第{vols.get(r['book_id'], 1.0):g}集"}
        if r["form_zh"]:
            item["zh"] = r["form_zh"]
        if r["certainty"] == "inferred":
            item["certainty"] = "inferred"
        if r["note"]:
            item["note"] = r["note"]
        out.append(item)
    # most used first when trimming, but keep chronological order in the payload
    keep = {id(x) for x in sorted(out, key=lambda x: -x["uses"])[:limit]}
    return [x for x in out if id(x) in keep]


def forget_scene(conn: sqlite3.Connection, book_id: str, scene_id: str) -> None:
    """Withdraw the program observations of a scene (before re-harvesting it after its speakers changed)."""
    obs = conn.execute("SELECT o.utterance_id, o.address_id FROM address_observations o JOIN utterances ut ON "
                       "ut.id=o.utterance_id WHERE ut.book_id=? AND ut.scene_id=?", (book_id, scene_id)).fetchall()
    for utt, aid in obs:
        conn.execute("UPDATE address_terms SET uses=uses-1 WHERE id=?", (aid,))
        conn.execute("DELETE FROM address_observations WHERE utterance_id=? AND address_id=?", (utt, aid))
    conn.execute("DELETE FROM address_terms WHERE uses<=0 AND origin='program' AND id NOT IN "
                 "(SELECT address_id FROM address_observations)")


def reharvest_scene(conn: sqlite3.Connection, book_id: str, scene_id: str) -> int:
    forget_scene(conn, book_id, scene_id)
    return harvest_scene(conn, book_id, scene_id)
