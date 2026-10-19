"""Address terms: who calls whom what (ADR-022).

Japanese forms of address depend on the speaker: 小林 calls 水野 「みずっち」, 高木's sister calls 高木 「姉ちゃん」, and a
change of form (「水野さん」 → 「水野」) is often a plot signal. One row per (speaker, addressee, form) with the story
positions where it was first and last used, the number of uses and the rendering to keep. ``addressee == speaker``
means the speaker refers to themself by that name.

Sources, in order of authority:
* ``llm``     – the memory model reports each form with the line it is in (verbatim evidence) and how it is used
  (calling the person, talking about them, oneself); the program links it to that line (``address_observations``)
  and checks nothing else (ADR-059: no name matching by the program).
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
              "っち", "ちゃま", "にゃん", "嬢", "卿", "閣下", "姫")
_HONORIFIC_RE = re.compile("(" + "|".join(sorted(map(re.escape, HONORIFICS), key=len, reverse=True)) + ")")
_HONORIFIC_END = re.compile("(" + "|".join(map(re.escape, HONORIFICS)) + ")$")
RESOLVED = ("explicit", "inferred", "human_verified")
_PRONOUNS = {"私", "わたし", "僕", "ぼく", "俺", "おれ", "あたし", "うち", "あなた", "君", "きみ", "お前", "おまえ",
             "彼", "彼女", "あいつ", "こいつ", "そいつ"}


def name_forms(conn: sqlite3.Connection, book_id: str, upto: int | None = None) -> tuple[dict[str, str], set[str]]:
    """(form → character id, ambiguous forms). Forms come from character names/aliases and from every glossary
    form of an entity that belongs to a character. A form claimed by two characters, or listed in
    ``form_senses``, is ambiguous and never attributed automatically. With ``upto`` only characters already known
    at that story position count: 「水野さん」 first named as someone's mother in volume 2 must not take over
    volume 1's 「水野さん」 said to a classmate."""
    chars = conn.execute(f"SELECT id, book_id, canonical_name, aliases_json, known_from_ordinal FROM characters "
                         f"WHERE {SERIES_SCOPE}", (book_id,)).fetchall()
    if upto is not None:
        hz = pos(conn, book_id, upto)
        chars = [c for c in chars if pos(conn, c["book_id"], c["known_from_ordinal"]) <= hz]
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
    # a rendering from a better source replaces a weaker one (glossary < llm < naming < human); human is final
    rank = {None: 0, "glossary": 1, "llm": 2, "naming": 3, "human": 4}
    if form_zh and rank.get(origin, 0) >= rank.get(row["zh_origin"], 0) and row["zh_origin"] != "human":
        sets.update(form_zh=form_zh, zh_origin=origin)
    if note and not row["note"]:
        sets["note"] = note
    if quote and not row["evidence_quote"]:
        sets["evidence_quote"] = quote[:300]
    conn.execute(f"UPDATE address_terms SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), rid))
    return rid


_SMALL = str.maketrans("ぁぃぅぇぉゃゅょっゎァィゥェォャュョッヮ", "あいうえおやゆよつわアイウエオヤユヨツワ")


def variant_key(form: str) -> str:
    """One key for spellings of one nickname and the nickname with an honorific: 「みずのっつぃー」「あだっついー」
    「みずのっつぃーくん」 → same; 「水野ちゃん」 and 「みずっちー」 differ."""
    return _HONORIFIC_END.sub("", form).replace("ー", "").replace("～", "").replace("〜", "").translate(_SMALL)


def decided_variants(conn: sqlite3.Connection, speaker: str, addressee: str, form: str) -> list[dict[str, str]]:
    """Renderings already decided (human / naming) for other spellings of the same form by the same speaker for the
    same person — a new 「みずのっつぃーくん」 should build on the chosen 「みずのっつぃー」."""
    key = variant_key(form)
    return [{"ja": r["form_ja"], "zh": r["form_zh"]} for r in conn.execute(
        "SELECT form_ja, form_zh FROM address_terms WHERE speaker_id=? AND addressee_id=? AND form_ja!=? AND "
        f"form_zh IS NOT NULL AND zh_origin IN ({','.join('?' * len(DECIDED))}) ORDER BY zh_origin='human' DESC, uses DESC",
        (speaker, addressee, form, *DECIDED)) if variant_key(r["form_ja"]) == key][:3]


DECIDED = ("human", "naming")
# ADR-037: what the second look said a form in someone's line is. A quote of what others call him (「閣下と呼んでいた奴」)
# or a word that only looks like a name (「欲張り最強セット」) is not how the speaker calls anyone: never given onwards.
USAGES = ("address", "mention", "self", "quoted", "unrelated")
NOT_USED = ("quoted", "unrelated")


def usage_of(conn: sqlite3.Connection, r: sqlite3.Row) -> str | None:
    row = conn.execute("SELECT usage FROM address_usage WHERE speaker_id=? AND addressee_id=? AND form_ja=?",
                       (r["speaker_id"], r["addressee_id"], r["form_ja"])).fetchone()
    return row[0] if row else None


def calls_by(conn: sqlite3.Connection, r: sqlite3.Row) -> bool:
    """Whether a row is how the speaker *calls* the other (not one's own title, not talk about them, not a quote): what
    counts as a way of addressing in cards, house style and relations. 「『賢者』が死んだ？」 is about her."""
    return r["speaker_id"] != r["addressee_id"] and usage_of(conn, r) in (None, "address")


def set_usage(conn: sqlite3.Connection, book_id: str, r: sqlite3.Row, usage: str, reason: str, origin: str = "verify",
              ) -> None:
    """How a form is used. A person's decision is never replaced."""
    old = conn.execute("SELECT origin FROM address_usage WHERE speaker_id=? AND addressee_id=? AND form_ja=?",
                       (r["speaker_id"], r["addressee_id"], r["form_ja"])).fetchone()
    if old is not None and old[0] == "human" and origin != "human":
        return
    conn.execute("INSERT OR REPLACE INTO address_usage(book_id, speaker_id, addressee_id, form_ja, usage, reason, "
                 "origin, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (book_id, r["speaker_id"], r["addressee_id"], r["form_ja"], usage, reason[:200], origin, utcnow()))


def visible_terms(conn: sqlite3.Connection, book_id: str, horizon: int, text: str | None = None,
                  speakers: set[str] | None = None, present: set[str] | None = None,
                  upto: int | None = None) -> list[sqlite3.Row]:
    """Address terms first used before story position (book, ``horizon``); optionally only forms in ``text``
    or used by ``speakers`` for someone ``present``. Sorted by first use (a change of form shows as a later row).

    A form whose rendering was *decided* (by a human or by naming) and that occurs in ``text`` is also given when it
    is first used here, between ``horizon`` and ``upto`` — otherwise the decision would miss its first scene."""
    hz = pos(conn, book_id, horizon)
    end = pos(conn, book_id, upto) if upto is not None else hz
    vols = volumes(conn)
    t = match_text(text) if text is not None else None
    out = []
    for r in conn.execute(f"SELECT * FROM address_terms WHERE {SERIES_SCOPE}", (book_id,)):
        first = (vols.get(r["book_id"], 1.0), r["first_ordinal"])
        if first >= hz and not (first <= end and t is not None and r["form_ja"] in t and r["zh_origin"] in DECIDED):
            continue
        if t is not None and r["form_ja"] not in t and not (
                speakers and r["speaker_id"] in speakers and present is not None and r["addressee_id"] in present):
            continue
        if r["zh_origin"] != "human" and usage_of(conn, r) in NOT_USED:
            continue
        out.append(r)
    return sorted(out, key=lambda r: (vols.get(r["book_id"], 1.0), r["first_ordinal"]))


FORMS_PER_QUOTA = 12  # forms of address per QUOTA_UNITS paragraphs of a request (``retrieval.batch_quota``)


def context_items(conn: sqlite3.Connection, book_id: str, horizon: int, text: str, speakers: set[str],
                  names: dict[str, str], present: set[str] | None = None, limit: int = FORMS_PER_QUOTA,
                  upto: int | None = None) -> list[dict[str, Any]]:
    """Translation payload: forms occurring in the batch, plus how this batch's speakers usually call the
    characters present in the scene (the form they would use if they named them). ``limit``: the most used kept; a
    caller with a long batch raises it with the batch (``retrieval.batch_quota``, ADR-069)."""
    vols = volumes(conn)
    out = []
    for r in visible_terms(conn, book_id, horizon, text, speakers, present or set(), upto=upto):
        if r["speaker_id"] not in names or r["addressee_id"] not in names:
            continue
        item: dict[str, Any] = {"speaker": names[r["speaker_id"]],
                                "to": "（自稱）" if r["speaker_id"] == r["addressee_id"] else names[r["addressee_id"]],
                                "ja": r["form_ja"], "uses": r["uses"],
                                "since": f"第{vols.get(r['book_id'], 1.0):g}集"}
        if r["form_zh"]:
            item["zh"] = r["form_zh"]
        if r["zh_origin"] not in DECIDED and (rel := decided_variants(conn, r["speaker_id"], r["addressee_id"],
                                                                         r["form_ja"])):
            item["same_nickname_decided"] = rel
        if r["certainty"] == "inferred":
            item["certainty"] = "inferred"
        if (u := usage_of(conn, r)) in ("mention", "self"):
            item["usage"] = u  # talked about, not called (ADR-037)
        if r["note"]:
            item["note"] = r["note"]
        out.append(item)
    # most used first when trimming, but keep chronological order in the payload
    keep = {id(x) for x in sorted(out, key=lambda x: -x["uses"])[:limit]}
    return [x for x in out if id(x) in keep]


def forget_scene(conn: sqlite3.Connection, book_id: str, scene_id: str) -> None:
    """Withdraw the observations of a scene's lines (before its memory is read again); a form left with no use and
    nobody's decision goes."""
    obs = conn.execute("SELECT o.utterance_id, o.address_id FROM address_observations o JOIN utterances ut ON "
                       "ut.id=o.utterance_id WHERE ut.book_id=? AND ut.scene_id=?", (book_id, scene_id)).fetchall()
    for utt, aid in obs:
        conn.execute("UPDATE address_terms SET uses=uses-1 WHERE id=?", (aid,))
        conn.execute("DELETE FROM address_observations WHERE utterance_id=? AND address_id=?", (utt, aid))
    conn.execute("DELETE FROM address_terms WHERE uses<=0 AND origin IN ('program','llm') AND COALESCE(zh_origin,'') "
                 "NOT IN ('human','naming') AND id NOT IN (SELECT address_id FROM address_observations)")


def follow_speakers(conn: sqlite3.Connection, book_id: str, changed: dict[str, str | None]) -> int:
    """Lines whose speaker was changed (by the second look or a person): ``changed`` maps each line to its speaker
    before. A form recorded on that line under the old speaker goes with the line to the new speaker; a form the
    memory model had already given to someone else stays; a line now said by no one known keeps its forms (the memory
    model read them with the context). Only the data moves; the text is not searched. Returns the observations moved."""
    moved = 0
    for utt, old in changed.items():
        line = conn.execute("SELECT ut.speaker_id_nullable sp, ut.status, ut.unit_id, ut.quote_text, u.ordinal FROM "
                            "utterances ut JOIN units u ON u.id=ut.unit_id WHERE ut.id=?", (utt,)).fetchone()
        if line is None or not line["sp"] or line["sp"] == old or old is None:
            continue
        for r in conn.execute("SELECT a.* FROM address_observations o JOIN address_terms a ON a.id=o.address_id WHERE "
                              "o.utterance_id=? AND a.speaker_id=?", (utt, old)).fetchall():
            conn.execute("DELETE FROM address_observations WHERE utterance_id=? AND address_id=?", (utt, r["id"]))
            conn.execute("UPDATE address_terms SET uses=uses-1 WHERE id=?", (r["id"],))
            addressee = line["sp"] if r["speaker_id"] == r["addressee_id"] else r["addressee_id"]  # one's own name too
            upsert(conn, book_id, line["sp"], addressee, r["form_ja"], ordinal=line["ordinal"], unit_id=line["unit_id"],
                   origin=r["origin"],
                   certainty="explicit" if line["status"] in ("explicit", "human_verified") else "inferred",
                   form_zh=r["form_zh"] if r["zh_origin"] in ("llm", "naming", "human") else None,
                   quote=line["quote_text"], note=r["note"], utterance_id=utt)
            moved += 1
    conn.execute("DELETE FROM address_terms WHERE uses<=0 AND origin IN ('program','llm') AND COALESCE(zh_origin,'') "
                 "NOT IN ('human','naming') AND id NOT IN (SELECT address_id FROM address_observations)")
    return moved
