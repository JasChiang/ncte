"""A second look at the read pass's disputed judgments, before naming and translation (ADR-033).

Only what the first pass left in doubt is asked again, with the surrounding text: lines whose speaker the model and
the program disagreed on (the model's answer was kept as ``inferred``) or that ended ``unknown`` because adjacent
narration pointed at someone else, genders known only by inference, and characters merged when a name was
revealed, and every form of address the program found by matching a name in someone's line (ADR-037: is it calling
that person, talking about them, the speaker about themselves, a quote of what others call them, or a word that only
looks like the name?). The answers never become facts: a speaker stays ``inferred``; a doubtful inferred gender is dropped (the
translation then uses 你); a merge judged "different" stops counting the merged description as this character
(its forms leave the aliases) and becomes a finding for the user — lines moved by the merge are not moved back.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..llm.schemas import VerifyOutput
from ..memory import repository as repo
from ..memory.db import utcnow

CONTEXT_UNITS = 4  # paragraphs before and after a disputed line
MAX_ITEMS = 150
MAX_ADDRESS_ITEMS = 300  # forms found by the program's string match (ADR-037)
ADDRESS_LINES = 2  # lines shown per form


def _names(conn: sqlite3.Connection, book_id: str) -> dict[str, str]:
    from ..series import SERIES_SCOPE

    return {r["id"]: r["canonical_name"] + (f"（{r['display_name_zh']}）" if r["display_name_zh"] else "")
            for r in conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book_id,))}


def _window(conn: sqlite3.Connection, book_id: str, ordinal: int, mark: str | None = None) -> list[str]:
    rows = conn.execute("SELECT ordinal, plain_text FROM units WHERE book_id=? AND translatable=1 AND ordinal BETWEEN ? "
                        "AND ? ORDER BY ordinal", (book_id, ordinal - CONTEXT_UNITS, ordinal + CONTEXT_UNITS)).fetchall()
    return [("▶ " if r["ordinal"] == ordinal else "") + match_text(r["plain_text"])[:200] for r in rows]


def collect(conn: sqlite3.Connection, book_id: str) -> list[dict[str, Any]]:
    """Items to look at again. Keys starting with ``_`` stay out of the request."""
    names = _names(conn, book_id)
    items: list[dict[str, Any]] = []
    for u in conn.execute(
            "SELECT ut.*, un.ordinal FROM utterances ut JOIN units un ON un.id=ut.unit_id WHERE ut.book_id=? AND "
            "ut.status IN ('inferred','unknown') AND (ut.notes_json LIKE '%(LLM kept)%' OR ut.notes_json LIKE "
            "'%conflict: action evidence%') ORDER BY un.ordinal", (book_id,)):
        cands = sorted({h["candidate_character_id"] for h in conn.execute(
            "SELECT candidate_character_id FROM speaker_hypotheses WHERE utterance_id=?", (u["id"],))} & names.keys())
        items.append({"ref": f"q:{u['id']}", "kind": "speaker", "quote": match_text(u["quote_text"])[:120],
                      "context": _window(conn, book_id, u["ordinal"]),
                      "candidates": {c: names[c] for c in cands},
                      "first_answer": names.get(u["speaker_id_nullable"]) if u["speaker_id_nullable"] else None,
                      "_ordinal": u["ordinal"]})
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
                      "samples": [match_text(r["plain_text"])[:160] for r in spread]})
    for link in conn.execute("SELECT * FROM entity_links WHERE book_id=? AND origin='scene_analysis' AND "
                             "status='accepted' AND form_type='official'", (book_id,)):
        c = conn.execute("SELECT * FROM characters WHERE book_id=? AND canonical_name=?",
                         (book_id, link["target_term"])).fetchone()
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
                      "_ordinal": link["revealed_ordinal"]})
    for sc in conn.execute("SELECT * FROM scenes WHERE book_id=? AND state_json LIKE '%\"check\": \"conflict\"%' "
                           "ORDER BY seq", (book_id,)):
        narr = json.loads(sc["state_json"]).get("narration") or {}
        if narr.get("check") != "conflict":
            continue
        paras = [match_text(r["plain_text"])[:140] for r in conn.execute(
            "SELECT plain_text FROM units WHERE book_id=? AND scene_id=? AND kind='text' ORDER BY ordinal LIMIT 40",
            (book_id, sc["id"])) if not r[0].lstrip(" 　").startswith(("「", "『"))][:12]
        items.append({"ref": f"n:{sc['id']}", "kind": "narration", "first_answer": narr["person"], "paragraphs": paras})
    items = items[:MAX_ITEMS]
    items += narrator_items(conn, book_id, names)
    items += identity_items(conn, book_id, names)
    items += address_items(conn, book_id, names)[:MAX_ADDRESS_ITEMS]
    return items


MAX_IDENTITY_ITEMS = 12


def identity_items(conn: sqlite3.Connection, book_id: str, names: dict[str, str]) -> list[dict[str, Any]]:
    """Characters known only by a common or relational word (「姉ちゃん」「母親」) — is this a character already known by
    name? — and group characters (「跟班兩人」) — who are they, and are two groups one? The model answers with the
    passages; the program only finds the cards to ask about (ADR-046)."""
    from ..lexicon import names_a_group
    from ..series import SERIES_SCOPE

    proper = {r[0] for r in conn.execute(f"SELECT source_term FROM glossary WHERE {SERIES_SCOPE} AND category='character'",
                                         (book_id,))}
    rows = conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM utterances u WHERE u.speaker_id_nullable=c.id) n, "
        "(SELECT COUNT(*) FROM relation_notes r WHERE r.a_id=c.id OR r.b_id=c.id) k FROM characters c "
        "WHERE c.book_id=? AND c.is_narrator=0 ORDER BY c.known_from_ordinal", (book_id,)).fetchall()
    named = {r["id"]: names[r["id"]] for r in rows if r["canonical_name"] in proper and r["id"] in names}
    groups = {r["id"]: names[r["id"]] for r in rows if names_a_group(r["canonical_name"]) and r["id"] in names}
    out = []
    for c in rows:
        if c["id"] not in names or c["canonical_name"] in proper or not (c["n"] or c["k"]):
            continue
        ctx = _window(conn, book_id, c["known_from_ordinal"])
        called_by = [f"{names.get(a['speaker_id'], '?')}：{a['form_ja']}" for a in conn.execute(
            "SELECT speaker_id, form_ja FROM address_terms WHERE book_id=? AND addressee_id=? LIMIT 5", (book_id, c["id"]))]
        if c["id"] in groups:
            out.append({"ref": f"p:{c['id']}", "kind": "group", "name": names[c["id"]], "context": ctx,
                        "candidates": named, "other_groups": {k: v for k, v in groups.items() if k != c["id"]}})
        else:
            out.append({"ref": f"i:{c['id']}", "kind": "identity", "name": names[c["id"]], "context": ctx,
                        "called_by": called_by, "candidates": named})
    return out[:MAX_IDENTITY_ITEMS]


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


def address_items(conn: sqlite3.Connection, book_id: str, names: dict[str, str]) -> list[dict[str, Any]]:
    """Forms the program matched (not confirmed by the memory model, not a person's choice, not classified yet)."""
    from ..memory.address import usage_of

    out = []
    for a in conn.execute("SELECT * FROM address_terms WHERE book_id=? AND origin='program' AND uses>0 AND "
                          "(zh_origin IS NULL OR zh_origin!='human') ORDER BY uses DESC", (book_id,)).fetchall():
        if usage_of(conn, a) is not None or a["speaker_id"] not in names or a["addressee_id"] not in names:
            continue
        lines = []
        for r in conn.execute("SELECT ut.quote_text, un.ordinal FROM address_observations o JOIN utterances ut ON "
                              "ut.id=o.utterance_id JOIN units un ON un.id=ut.unit_id WHERE o.address_id=? ORDER BY "
                              "un.ordinal LIMIT ?", (a["id"], ADDRESS_LINES)):
            lines.append({"quote": match_text(r["quote_text"])[:160], "context": _window(conn, book_id, r["ordinal"])[1:-1]})
        if not lines and a["evidence_quote"]:
            lines.append({"quote": match_text(a["evidence_quote"])[:160], "context": []})
        if not lines:
            continue
        out.append({"ref": f"a:{a['id']}", "kind": "address", "speaker": names[a["speaker_id"]],
                    "person": names[a["addressee_id"]], "form": a["form_ja"],
                    "self": a["speaker_id"] == a["addressee_id"], "lines": lines})
    return out


def apply(conn: sqlite3.Connection, book_id: str, items: list[dict[str, Any]], parsed: VerifyOutput,
          finding: Any) -> dict[str, int]:
    """Apply the second look. ``finding(unit_id, type, severity, evidence)`` records what needs the user."""
    from ..memory.address import reharvest_scene

    by_ref = {i["ref"]: i for i in items}
    stats = {"speakers_changed": 0, "speakers_unknown": 0, "speakers_kept": 0, "genders_dropped": 0,
             "genders_kept": 0, "merges_doubted": 0, "ignored": 0}
    from ..memory.address import USAGES, set_usage
    scenes: set[str] = set()
    for a in parsed.items:
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
                new_id, status = answer, "inferred"
            elif answer == "unknown":
                new_id, status = None, "unknown"
            else:
                stats["ignored"] += 1
                continue
            if new_id == u["speaker_id_nullable"] and status == u["status"]:
                stats["speakers_kept"] += 1
            else:
                stats["speakers_changed" if new_id else "speakers_unknown"] += 1
                if u["scene_id"]:
                    scenes.add(u["scene_id"])
            notes = json.loads(u["notes_json"]) + [note]
            conn.execute("UPDATE utterances SET speaker_id_nullable=?, status=?, resolution_source='verify', "
                         "notes_json=?, updated_at=? WHERE id=?",
                         (new_id, status, json.dumps(notes, ensure_ascii=False), utcnow(), key))
            repo.remove_speech_examples_for(conn, key)
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
        elif kind == "a":
            row = conn.execute("SELECT * FROM address_terms WHERE id=?", (int(key),)).fetchone()
            if row is None or answer not in USAGES:
                stats["ignored"] += 1
                continue
            set_usage(conn, book_id, row, answer, a.reason)
            stats[f"address_{answer}"] = stats.get(f"address_{answer}", 0) + 1
        elif kind == "i":
            if answer in item["candidates"] and answer != key:  # 「姉ちゃん」 in the sister's story is 島村
                _narrator_is(conn, book_id, key, answer)
                stats["identities_merged"] = stats.get("identities_merged", 0) + 1
        elif kind == "p":
            if answer.startswith("same:") and answer[5:].strip() in item["other_groups"]:
                repo.merge_characters(conn, answer[5:].strip(), key)  # one group known by two descriptions
                stats["groups_merged"] = stats.get("groups_merged", 0) + 1
            else:
                members = [m.strip() for m in answer.split(",") if m.strip() in item["candidates"]]
                for m in members:
                    conn.execute("INSERT OR IGNORE INTO group_members(group_id, member_id, book_id, reason, created_at) "
                                 "VALUES (?,?,?,?,?)", (key, m, book_id, a.reason[:200], utcnow()))
                if members:
                    stats["groups_with_members"] = stats.get("groups_with_members", 0) + 1
        elif kind == "r":
            if answer in item["candidates"]:  # the narrator is this named character: one card from here on
                _narrator_is(conn, book_id, key, answer)
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
    for sid in scenes:
        reharvest_scene(conn, book_id, sid)
    repo.refresh_profiles(conn, book_id)
    return stats


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
