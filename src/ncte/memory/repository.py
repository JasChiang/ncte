"""Memory repository: characters, utterances, speech examples, facts, summaries, props."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from ..analysis.dialogue import speech_profile
from ..series import SERIES_SCOPE
from .db import utcnow


def character_id_for(book_id: str, name_ja: str) -> str:
    return "c_" + hashlib.sha256(f"{book_id}|{name_ja}".encode()).hexdigest()[:12]


def fact_id_for(book_id: str, scene_id: str, n: int, text: str) -> str:
    return "f_" + hashlib.sha256(f"{book_id}|{scene_id}|{n}|{text}".encode()).hexdigest()[:12]


def utterance_id_for(unit_id: str, quote_index: int) -> str:
    return f"q_{unit_id[2:]}_{quote_index}"


def characters(conn: sqlite3.Connection, book_id: str) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE} ORDER BY known_from_ordinal", (book_id,)).fetchall()


def alias_map(rows: list[sqlite3.Row]) -> dict[str, str]:
    """name/alias (ja) → character id. Earlier-known characters win on collisions."""
    out: dict[str, str] = {}
    for r in rows:
        for name in [r["canonical_name"], *json.loads(r["aliases_json"])]:
            if name and name not in out:
                out[name] = r["id"]
    return out


def upsert_character(conn: sqlite3.Connection, book_id: str, name_ja: str, *, name_zh: str | None,
                     aliases: list[str], unit_id: str, ordinal: int, origin: str) -> tuple[str, bool]:
    row = conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE} AND canonical_name=?", (book_id, name_ja)).fetchone()
    cid = row["id"] if row else character_id_for(book_id, name_ja)
    if row:
        # seen earlier in the story (e.g. volume 1 translated after volume 2): move the first appearance back
        from ..series import pos

        if pos(conn, book_id, ordinal) < pos(conn, row["book_id"], row["known_from_ordinal"]):
            conn.execute("UPDATE characters SET book_id=?, known_from_unit_id=?, known_from_ordinal=?, updated_at=? "
                         "WHERE id=?", (book_id, unit_id, ordinal, utcnow(), row["id"]))
        old = json.loads(row["aliases_json"])
        merged = old + [a for a in aliases if a not in old and a != name_ja]
        if merged != old:
            conn.execute("UPDATE characters SET aliases_json=?, updated_at=? WHERE id=?",
                         (json.dumps(merged, ensure_ascii=False), utcnow(), cid))
        return cid, False
    conn.execute(
        "INSERT INTO characters(id, book_id, canonical_name, display_name_zh, aliases_json, known_from_unit_id, "
        "known_from_ordinal, status, origin, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (cid, book_id, name_ja, name_zh, json.dumps([a for a in aliases if a != name_ja], ensure_ascii=False),
         unit_id, ordinal, "partial", origin, utcnow(), utcnow()))
    return cid, True


def ensure_glossary(conn: sqlite3.Connection, book_id: str, source: str, target: str, *, category: str,
                    origin: str, first_unit_id: str | None, verified: bool = False) -> bool:
    if not source or not target:
        return False
    row = conn.execute(f"SELECT id FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, source)).fetchone()
    if row:
        return False
    conn.execute("INSERT INTO glossary(book_id, source_term, target_term, category, verified, origin, first_unit_id, "
                 "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                 (book_id, source, target, category, int(verified), origin, first_unit_id, utcnow()))
    from .entities import ensure_entity

    ensure_entity(conn, book_id, source)
    refresh_readings(conn, book_id, [source])
    return True


def refresh_readings(conn: sqlite3.Connection, book_id: str, terms: list[str] | None = None) -> int:
    """Record on glossary entries (all of the series, or ``terms``) the readings the source annotates on them
    (``term_readings``: every reading on an occurrence of exactly the term, first appearance first), from the
    paragraphs of every volume of the series. A string match on the stored annotations; nothing is judged."""
    from ..epub.units import RUBY_OPEN, term_readings
    from ..series import scope_clause

    where, ids = scope_clause(conn, book_id, "u.book_id")
    texts = [r[0] for r in conn.execute(
        f"SELECT u.plain_text FROM units u JOIN books b ON b.id=u.book_id WHERE {where} AND u.kind IN ('text','heading') "
        "AND instr(u.plain_text, ?) > 0 ORDER BY b.volume_index, u.ordinal", (*ids, RUBY_OPEN))]
    rows = conn.execute(f"SELECT id, source_term, readings_json FROM glossary WHERE {SERIES_SCOPE}", (book_id,)).fetchall()
    n = 0
    for r in rows:
        if terms is not None and r["source_term"] not in terms:
            continue
        found = json.dumps(term_readings(texts, r["source_term"]), ensure_ascii=False)
        if found != r["readings_json"]:
            conn.execute("UPDATE glossary SET readings_json=? WHERE id=?", (found, r["id"]))
            n += 1
    return n


def readings_of(conn: sqlite3.Connection, book_id: str) -> dict[str, list[str]]:
    """Glossary term → the readings the source annotates on it (only terms that have some)."""
    return {r[0]: json.loads(r[1]) for r in conn.execute(
        f"SELECT source_term, readings_json FROM glossary WHERE {SERIES_SCOPE} AND readings_json!='[]'", (book_id,))}


def set_glossary(conn: sqlite3.Connection, book_id: str, source: str, target: str, *, category: str = "term",
                 verified: bool = True, note: str | None = None) -> dict[str, Any]:
    row = conn.execute(f"SELECT * FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, source)).fetchone()
    if row:
        conn.execute("UPDATE glossary SET target_term=?, verified=?, version=version+1, origin='user', note=?, "
                     "updated_at=? WHERE id=?", (target, int(verified), note, utcnow(), row["id"]))
        if row["category"] == "character" or category == "character":
            conn.execute(f"UPDATE characters SET display_name_zh=?, updated_at=? WHERE {SERIES_SCOPE} AND canonical_name=?",
                         (target, utcnow(), book_id, source))
        refresh_readings(conn, book_id, [source])
        return {"previous": row["target_term"], "version": row["version"] + 1}
    conn.execute("INSERT INTO glossary(book_id, source_term, target_term, category, verified, origin, note, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (book_id, source, target, category, int(verified), "user", note, utcnow()))
    from .entities import ensure_entity

    ensure_entity(conn, book_id, source)
    refresh_readings(conn, book_id, [source])
    return {"previous": None, "version": 1}


def add_speech_example(conn: sqlite3.Connection, character_id: str, unit_id: str, utterance_id: str, raw: str,
                       status: str) -> None:
    conn.execute("INSERT OR IGNORE INTO speech_examples(character_id, unit_id, utterance_id, raw_text, "
                 "verification_status, created_at) VALUES (?,?,?,?,?,?)",
                 (character_id, unit_id, utterance_id, raw, status, utcnow()))


def remove_speech_examples_for(conn: sqlite3.Connection, utterance_id: str) -> None:
    conn.execute("DELETE FROM speech_examples WHERE utterance_id=?", (utterance_id,))


def refresh_profiles(conn: sqlite3.Connection, book_id: str) -> None:
    for c in characters(conn, book_id):
        ex = [r["raw_text"] for r in conn.execute(
            "SELECT raw_text FROM speech_examples WHERE character_id=? AND verification_status IN "
            "('explicit','human_verified') ORDER BY id", (c["id"],))]
        prof = speech_profile(ex) if ex else {}
        conn.execute("UPDATE characters SET speech_profile_json=? WHERE id=?",
                     (json.dumps(prof, ensure_ascii=False), c["id"]))


def reject(conn: sqlite3.Connection, book_id: str, scene_id: str | None, stage: str, item: Any, reason: str) -> None:
    conn.execute("INSERT INTO memory_rejections(book_id, scene_id, stage, item_json, reason, created_at) VALUES "
                 "(?,?,?,?,?,?)", (book_id, scene_id, stage, json.dumps(item, ensure_ascii=False, default=str),
                                   reason, utcnow()))


def merge_characters(conn: sqlite3.Connection, keep_id: str, drop_id: str) -> None:
    """Fold character ``drop_id`` into ``keep_id``: utterances, hypotheses, speech examples, facts and entity
    references follow; the dropped name becomes an alias; the earliest first appearance is kept."""
    if keep_id == drop_id:
        return
    keep = conn.execute("SELECT * FROM characters WHERE id=?", (keep_id,)).fetchone()
    drop = conn.execute("SELECT * FROM characters WHERE id=?", (drop_id,)).fetchone()
    if keep is None or drop is None:
        return
    for sql in ("UPDATE utterances SET speaker_id_nullable=? WHERE speaker_id_nullable=?",
                "UPDATE speaker_hypotheses SET candidate_character_id=? WHERE candidate_character_id=?",
                "UPDATE OR IGNORE speech_examples SET character_id=? WHERE character_id=?",
                "UPDATE facts SET subject=? WHERE subject=?",
                "UPDATE facts SET object_ref=? WHERE object_ref=?",
                "UPDATE facts SET source_viewpoint=? WHERE source_viewpoint=?",  # whose belief it is
                "UPDATE relation_notes SET a_id=? WHERE a_id=?",
                "UPDATE relation_notes SET b_id=? WHERE b_id=?",
                "UPDATE OR IGNORE relations SET a_id=? WHERE a_id=?",
                "UPDATE OR IGNORE relations SET b_id=? WHERE b_id=?",
                "UPDATE entities SET character_id=? WHERE character_id=?",
                "UPDATE OR IGNORE group_members SET group_id=? WHERE group_id=?",
                "UPDATE OR IGNORE group_members SET member_id=? WHERE member_id=?"):
        conn.execute(sql, (keep_id, drop_id))
    conn.execute("DELETE FROM speech_examples WHERE character_id=?", (drop_id,))
    conn.execute("DELETE FROM group_members WHERE group_id=? OR member_id=? OR group_id=member_id", (drop_id, drop_id))
    conn.execute("DELETE FROM relations WHERE a_id=? OR b_id=?", (drop_id, drop_id))  # duplicates of kept ones
    # impressions: the kept card's stretches stay; the dropped card's come over only when the kept one has none (two
    # cards' stretches laid over each other would give a scene the wrong voice)
    if conn.execute("SELECT 1 FROM character_impressions WHERE character_id=? LIMIT 1", (keep_id,)).fetchone():
        conn.execute("DELETE FROM character_impressions WHERE character_id=?", (drop_id,))
    else:
        conn.execute("UPDATE character_impressions SET character_id=? WHERE character_id=?", (keep_id, drop_id))
    conn.execute("UPDATE characters SET is_narrator=1 WHERE id=? AND ?", (keep_id, int(drop["is_narrator"] or 0)))
    conn.execute("UPDATE prop_states SET holder_character_id=? WHERE holder_character_id=?", (keep_id, drop_id))
    _merge_address(conn, keep_id, drop_id)
    # who a line is said to (a list of ids) and how a form was judged (the second look's usage of a form of address)
    for row in conn.execute("SELECT id, addressee_ids_json FROM utterances WHERE addressee_ids_json LIKE ?",
                            (f'%"{drop_id}"%',)).fetchall():
        ids = [keep_id if x == drop_id else x for x in json.loads(row["addressee_ids_json"] or "[]")]
        conn.execute("UPDATE utterances SET addressee_ids_json=? WHERE id=?", (json.dumps(list(dict.fromkeys(ids))), row["id"]))
    for col in ("speaker_id", "addressee_id"):
        # a person's judgment on the dropped card wins over an automatic one on the kept card
        for h in conn.execute(f"SELECT * FROM address_usage WHERE {col}=? AND origin='human'", (drop_id,)).fetchall():
            sp = keep_id if h["speaker_id"] == drop_id else h["speaker_id"]
            ad = keep_id if h["addressee_id"] == drop_id else h["addressee_id"]
            conn.execute("DELETE FROM address_usage WHERE speaker_id=? AND addressee_id=? AND form_ja=? AND origin!='human'",
                         (sp, ad, h["form_ja"]))
        conn.execute(f"UPDATE OR IGNORE address_usage SET {col}=? WHERE {col}=?", (keep_id, drop_id))
    conn.execute("DELETE FROM address_usage WHERE speaker_id=? OR addressee_id=?", (drop_id, drop_id))  # duplicates
    g_keep = conn.execute("SELECT 1 FROM character_gender WHERE character_id=?", (keep_id,)).fetchone()
    g_drop = conn.execute("SELECT * FROM character_gender WHERE character_id=?", (drop_id,)).fetchone()
    if g_drop is not None:
        if g_keep is None:
            conn.execute("UPDATE character_gender SET character_id=? WHERE character_id=?", (keep_id, drop_id))
        else:
            set_gender(conn, g_drop["book_id"], keep_id, g_drop["gender"], ordinal=g_drop["ordinal"],
                       quote=g_drop["evidence_quote"], origin=g_drop["origin"], certainty=g_drop["certainty"])
            conn.execute("DELETE FROM character_gender WHERE character_id=?", (drop_id,))
    # scene states and naming proposals refer to characters by id
    for rid, blob in conn.execute("SELECT rowid, state_json FROM scenes WHERE instr(state_json, ?) > 0",
                                  (drop_id,)).fetchall():
        conn.execute("UPDATE scenes SET state_json=? WHERE rowid=?", (blob.replace(drop_id, keep_id), rid))
    conn.execute("UPDATE naming_proposals SET ref=? WHERE kind='character' AND ref=?", (keep_id, drop_id))
    aliases = json.loads(keep["aliases_json"])
    for a in [drop["canonical_name"], *json.loads(drop["aliases_json"])]:
        if a and a != keep["canonical_name"] and a not in aliases:
            aliases.append(a)
    conn.execute("UPDATE characters SET aliases_json=?, updated_at=? WHERE id=?",
                 (json.dumps(aliases, ensure_ascii=False), utcnow(), keep_id))
    from ..series import pos

    if pos(conn, drop["book_id"], drop["known_from_ordinal"]) < pos(conn, keep["book_id"], keep["known_from_ordinal"]):
        conn.execute("UPDATE characters SET book_id=?, known_from_unit_id=?, known_from_ordinal=? WHERE id=?",
                     (drop["book_id"], drop["known_from_unit_id"], drop["known_from_ordinal"], keep_id))
    conn.execute("DELETE FROM characters WHERE id=?", (drop_id,))
    refresh_profiles(conn, keep["book_id"])


def drop_aliases(conn: sqlite3.Connection, character_id: str, forms: list[str]) -> None:
    """These forms no longer name the character (a merge judged wrong): out of its aliases and name links."""
    row = conn.execute("SELECT * FROM characters WHERE id=?", (character_id,)).fetchone()
    if row is None:
        return
    gone = set(forms)
    aliases = [a for a in json.loads(row["aliases_json"] or "[]") if a not in gone]
    conn.execute("UPDATE characters SET aliases_json=?, updated_at=? WHERE id=?",
                 (json.dumps(aliases, ensure_ascii=False), utcnow(), character_id))
    conn.executemany("UPDATE entity_links SET status='rejected' WHERE book_id=? AND term=? AND target_term=?",
                     [(row["book_id"], f, row["canonical_name"]) for f in gone])
    refresh_profiles(conn, row["book_id"])


def set_gender(conn: sqlite3.Connection, book_id: str, character_id: str, gender: str, *, ordinal: int,
               quote: str | None, origin: str, certainty: str = "explicit") -> str:
    """Record a character's gender. ``explicit``: a word in the text states it (彼女, 少年 …); ``inferred``: speech
    style or similar. Explicit evidence outranks inferred; the earliest evidence of the same rank is kept (visible
    from there on); disagreeing evidence of the same rank makes it 'conflict' (shown to no one). A human setting
    wins over everything. Returns the stored value."""
    from ..series import pos

    rank = {"inferred": 0, "explicit": 1, "human": 2}
    new_rank = rank["human"] if origin == "human" else rank.get(certainty, 0)
    row = conn.execute("SELECT * FROM character_gender WHERE character_id=?", (character_id,)).fetchone()
    old_rank = -1 if row is None else rank["human"] if row["origin"] == "human" else rank.get(row["certainty"], 1)

    def put() -> None:
        conn.execute("INSERT OR REPLACE INTO character_gender(character_id, gender, book_id, ordinal, evidence_quote, "
                     "origin, updated_at, certainty) VALUES (?,?,?,?,?,?,?,?)",
                     (character_id, gender, book_id, ordinal, (quote or "")[:200] or None, origin, utcnow(),
                      "explicit" if origin == "human" else certainty))

    if row is None or new_rank > old_rank or origin == "human":
        put()
        return gender
    if new_rank < old_rank or row["gender"] == "conflict":
        return str(row["gender"])
    if row["gender"] != gender:
        conn.execute("UPDATE character_gender SET gender='conflict', updated_at=? WHERE character_id=?",
                     (utcnow(), character_id))
        return "conflict"
    if pos(conn, book_id, ordinal) < pos(conn, row["book_id"], row["ordinal"]):
        conn.execute("UPDATE character_gender SET book_id=?, ordinal=?, evidence_quote=?, updated_at=? "
                     "WHERE character_id=?", (book_id, ordinal, (quote or "")[:200] or None, utcnow(), character_id))
    return gender


def gender_at(conn: sqlite3.Connection, character_id: str, hz: tuple[float, int]) -> str | None:
    """'female' / 'male' once the evidence has appeared by position ``hz``; None if unknown, later or in conflict."""
    g = gender_view(conn, character_id, hz)
    return g[0] if g else None


def gender_view(conn: sqlite3.Connection, character_id: str, hz: tuple[float, int]) -> tuple[str, str] | None:
    """(gender, certainty) visible at ``hz``, or None."""
    from ..series import pos

    row = conn.execute("SELECT * FROM character_gender WHERE character_id=?", (character_id,)).fetchone()
    if row is None or row["gender"] not in ("female", "male") or pos(conn, row["book_id"], row["ordinal"]) > hz:
        return None
    return str(row["gender"]), str(row["certainty"])


def character_basics(conn: sqlite3.Connection, character_id: str, hz: tuple[float, int] | None = None) -> dict[str, Any]:
    """The facts about a character every task gets with the name (one place, so no task leaves them out): gender
    (「female (inferred)」 when only inferred), whether this is the first-person narrator, the other name forms.
    ``hz``: what is known by that point; None after the book is read."""
    c = conn.execute("SELECT * FROM characters WHERE id=?", (character_id,)).fetchone()
    if c is None:
        return {}
    out: dict[str, Any] = {}
    if g := gender_view(conn, character_id, hz or (float("inf"), 1 << 62)):
        out["gender"] = g[0] if g[1] == "explicit" else f"{g[0]} (inferred)"
    if c["is_narrator"]:
        out["narrator"] = True
    if aliases := json.loads(c["aliases_json"] or "[]")[:6]:
        out["aliases_ja"] = aliases
    return out


def at_period(text: str, period: str) -> str:
    """A relation word with the time it holds in (「前世：長官」): every task that shows the relation shows when, so
    the subordinate of a past life is not taken for one in the story's present."""
    return f"{period}：{text}" if period and text else text


def qualified_fact(f: Any, text: str, names: dict[str, str]) -> str:
    """``text`` (a fact as a task reads it) with whose view it is and how sure, so that a belief or a guess is never
    passed on as a plain fact: 「〔米娜認為〕海斗是她哥哥（推測）」, not 「海斗是她哥哥」."""
    pre = ""
    if f["fact_type"] == "character_belief":
        who = f["source_viewpoint"]
        pre = f"〔{names.get(who, who) if who else '某個角色'}認為〕"
    elif f["fact_type"] == "narrator_claim":
        pre = "〔敘述者的說法〕"
    if "period" in f.keys() and f["period"]:
        pre = f"〔{f['period']}〕" + pre  # 〔前世〕: not the story's present
    return pre + text + ("" if f["certainty"] == "explicit" else "（推測）")


def relation_guess(r: Any) -> str:
    """「（推測）」 after a relation read from the story rather than stated in it (none for older rows without one)."""
    return "（推測）" if r["certainty"] == "inferred" else ""


def speaker_changed_addressees(conn: sqlite3.Connection, utterance_id: str, speaker: str | None) -> None:
    """A line's speaker was changed: the new speaker is not among the people it is said to, and who it is said to was
    read with the old speaker in mind — an inference now."""
    row = conn.execute("SELECT addressee_ids_json FROM utterances WHERE id=?", (utterance_id,)).fetchone()
    if row is None:
        return
    to = [x for x in json.loads(row[0] or "[]") if x != speaker]
    conn.execute("UPDATE utterances SET addressee_ids_json=?, addressee_status=? WHERE id=?",
                 (json.dumps(to), "inferred" if to else None, utterance_id))


def impressions_of(conn: sqlite3.Connection, character_id: str) -> list[sqlite3.Row]:
    """A character's impression stretches, in story order (ADR-060)."""
    return conn.execute("SELECT * FROM character_impressions WHERE character_id=? ORDER BY since_ordinal, id",
                        (character_id,)).fetchall()


def impression_at(conn: sqlite3.Connection, character_id: str, upto: int | None) -> dict[str, Any] | None:
    """The impression in force at ``upto`` (None: after the book): the latest stretch of the story's present begun by
    then — or the earliest, as the way of speaking to start from, before any has begun. Personality and role only once
    the evidence they rest on is reached (what they say may come later in the story). A stretch about another time is
    given with that time, once begun."""
    rows = impressions_of(conn, character_id)
    if not rows:
        return None
    now = [r for r in rows if not r["period"]]
    begun = [r for r in now if upto is None or r["since_ordinal"] <= upto]
    r = begun[-1] if begun else (now[0] if now else rows[0])
    out: dict[str, Any] = {"voice": r["voice"]}
    if upto is None or r["revealed_ordinal"] <= upto:
        out.update(personality=r["personality"], role=r["role"])
    other = [x for x in rows if x["period"] and (upto is None or x["since_ordinal"] <= upto)]
    if other:
        out["other_times"] = [f"〔{x['period']}〕{x['voice']}" for x in other]
    return out


def pair_relations(conn: sqlite3.Connection, book_id: str, a: str, b: str, names: dict[str, str],
                   at: int | None = None) -> list[str]:
    """How two people are related, as read (「席爾法德是艾恩的祖父」), for tasks that render how one addresses the other.
    ``at``: as of that paragraph (begun by then and not yet replaced); None: the relations in force now."""
    out = []
    when = ("AND until_ordinal IS NULL", ()) if at is None else (
        "AND since_ordinal<=? AND (until_ordinal IS NULL OR until_ordinal>?)", (at, at))
    for r in conn.execute("SELECT * FROM relations WHERE book_id=? AND ((a_id=? AND b_id=?) OR (a_id=? AND b_id=?)) "
                          f"{when[0]}", (book_id, a, b, b, a, *when[1])):
        for x, y, role in ((r["a_id"], r["b_id"], r["a_is"]), (r["b_id"], r["a_id"], r["b_is"])):
            if role:
                out.append(f"{names.get(x, '?')}是{names.get(y, '?')}的{role}{relation_guess(r)}")
        if not (r["a_is"] or r["b_is"]):
            out.append(f"{names.get(r['a_id'], '?')}與{names.get(r['b_id'], '?')}：{r['relation_zh']}{relation_guess(r)}")
    return list(dict.fromkeys(out))[:4]


LINE_PAIRS = 3  # pairs of speaker and listener per paragraph whose relation is given


def line_relations(conn: sqlite3.Connection, book_id: str, unit_id: str, at: int, names: dict[str, str]) -> list[str]:
    """How the speaker and the listener of each line of the paragraph are related as of ``at`` (at most
    ``LINE_PAIRS`` pairs), named by ``names`` (a pair with someone not named there is left out): for a step judging
    你／您, honorifics and forms of address. Only those two; nothing about anyone else in the scene."""
    pairs: list[tuple[str, str]] = []
    for u in conn.execute("SELECT speaker_id_nullable sp, addressee_ids_json to_ FROM utterances WHERE unit_id=? "
                          "ORDER BY quote_index", (unit_id,)):
        for x in json.loads(u["to_"] or "[]"):
            if u["sp"] and x != u["sp"] and u["sp"] in names and x in names and \
                    (u["sp"], x) not in pairs and (x, u["sp"]) not in pairs:
                pairs.append((u["sp"], x))
    out: list[str] = []
    for a, b in pairs[:LINE_PAIRS]:
        out += pair_relations(conn, book_id, a, b, names, at=at)
    return list(dict.fromkeys(out))


def _merge_address(conn: sqlite3.Connection, keep_id: str, drop_id: str) -> None:
    """Address terms of a merged character: moved, or folded into the same speaker/addressee/form row."""
    for col in ("speaker_id", "addressee_id"):
        for r in conn.execute(f"SELECT * FROM address_terms WHERE {col}=?", (drop_id,)).fetchall():
            sp = keep_id if r["speaker_id"] == drop_id else r["speaker_id"]
            ad = keep_id if r["addressee_id"] == drop_id else r["addressee_id"]
            twin = conn.execute("SELECT * FROM address_terms WHERE speaker_id=? AND addressee_id=? AND form_ja=? AND id!=?",
                                (sp, ad, r["form_ja"], r["id"])).fetchone()
            if twin is None:
                conn.execute("UPDATE address_terms SET speaker_id=?, addressee_id=? WHERE id=?", (sp, ad, r["id"]))
                continue
            conn.execute("UPDATE OR IGNORE address_observations SET address_id=? WHERE address_id=?", (twin["id"], r["id"]))
            conn.execute("DELETE FROM address_observations WHERE address_id=?", (r["id"],))
            conn.execute("UPDATE address_terms SET uses=uses+?, form_zh=COALESCE(form_zh, ?) WHERE id=?",
                         (r["uses"], r["form_zh"], twin["id"]))
            conn.execute("UPDATE naming_proposals SET status='superseded' WHERE kind='address' AND ref=?", (str(r["id"]),))
            conn.execute("DELETE FROM address_terms WHERE id=?", (r["id"],))
