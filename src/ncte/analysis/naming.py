"""Naming before translating (ADR-023).

After the read pass every name and address term of the volume is known, with counts and example sentences. One
model call proposes a rendering for each (with alternatives and a reason); the program applies the proposals as
defaults and keeps the important ones with real alternatives as *pending* for the user. Human decisions are
never overwritten and are not proposed again in later volumes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..llm.schemas import NamingOutput
from ..memory.db import utcnow
from ..series import SERIES_SCOPE, scope_clause

NAME_CATEGORIES = ("character", "place", "organization", "item", "skill")
REVIEW_NAME_COUNT = 5  # a name used at least this often is worth a decision
REVIEW_ADDRESS_USES = 3
MAX_NAMES = 80
MAX_ADDRESS = 60
MAX_ZH = 24


def _book_texts(conn: sqlite3.Connection, book_id: str) -> list[tuple[int, str]]:
    """(ordinal, text) of the story (front/back matter left out, ADR-024)."""
    from ..epub.matter import story_hrefs

    story = story_hrefs(conn, book_id)
    return [(r[0], match_text(r[1])) for r in conn.execute(
        "SELECT ordinal, plain_text, resource_href FROM units WHERE book_id=? AND translatable=1 AND kind IN "
        "('text','heading') AND spine_index IS NOT NULL ORDER BY ordinal", (book_id,)) if r[2] in story]


def _quotes(conn: sqlite3.Connection, address_id: int, limit: int = 3) -> list[dict[str, Any]]:
    """Example lines for an address term: the quotes it was observed in, spread over the book."""
    rows = conn.execute("SELECT u.ordinal, ut.quote_text, u.id FROM address_observations o JOIN utterances ut ON "
                        "ut.id=o.utterance_id JOIN units u ON u.id=ut.unit_id WHERE o.address_id=? ORDER BY u.ordinal",
                        (address_id,)).fetchall()
    if len(rows) > limit:
        rows = [rows[i * (len(rows) - 1) // (limit - 1)] for i in range(limit)]
    return [{"at": r[0], "text": match_text(r[1])[:90], "unit": r[2]} for r in rows]


_HIRA = "\u3041-\u309f"
_KATA = "\u30a0-\u30ff\u31f0-\u31ff"


def term_pattern(term: str) -> re.Pattern[str]:
    """A short kana-only name must not match inside a longer kana word (「のん」 in 「のんびり」). Longer names are
    matched as they are: particles are kana too (「ところでしまむらちゃん」「しまむらは」)."""
    if len(term) > 2:
        return re.compile(re.escape(term))
    for cls in (_HIRA, _KATA):
        if re.fullmatch(f"[{cls}]+", term):
            return re.compile(f"(?<![{cls}]){re.escape(term)}(?![{cls}])")
    return re.compile(re.escape(term))


def _proposed(conn: sqlite3.Connection, kind: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT ref FROM naming_proposals WHERE kind=? AND status!='superseded'",
                                       (kind,))}


def _excerpt(text: str, term: str, width: int = 36) -> str:
    i = text.find(term)
    return text[max(0, i - width // 2): i + len(term) + width // 2].strip()


def char_names(conn: sqlite3.Connection, book_id: str) -> dict[str, str]:
    return {r["id"]: r["display_name_zh"] or r["canonical_name"]
            for r in conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book_id,))}


def build_naming_payload(conn: sqlite3.Connection, book_id: str) -> dict[str, Any]:
    """Names and address terms of this volume that have no decision yet, most used first."""
    from ..memory.style import load_style, style_payload

    texts = _book_texts(conn, book_id)
    done = _proposed(conn, "name")
    suspects = [c for c in suspicious_characters(conn, book_id) if c["also"] and c["id"] not in _proposed(conn, "character")]
    done |= {c["name"] for c in suspects}  # a name that is someone else's form of address is checked as a character
    by_entity: dict[str, list[sqlite3.Row]] = {}
    rows = conn.execute(f"SELECT * FROM glossary WHERE {SERIES_SCOPE}", (book_id,)).fetchall()
    for g in rows:
        if g["entity_id"]:
            by_entity.setdefault(g["entity_id"], []).append(g)
    names = []
    for g in rows:
        term = g["source_term"]
        if g["category"] not in NAME_CATEGORIES or g["verified"] or g["origin"] == "user" or term in done:
            continue
        pat = term_pattern(term)
        hits = [(o, t) for o, t in texts if pat.search(t)]
        if not hits:
            continue
        spread = hits if len(hits) <= 3 else [hits[i * (len(hits) - 1) // 2] for i in range(3)]
        sib = [{"ja": o["source_term"], "zh": o["target_term"], "form": o["form_type"] or "official"}
               for o in by_entity.get(g["entity_id"] or "", []) if o["source_term"] != term]
        names.append({"ref": term, "ja": term, "zh": g["target_term"], "category": g["category"], "count": len(hits),
                      **({"same_entity_as": sib} if sib else {}),
                      "samples": [_excerpt(t, term) for _, t in spread[:2]],
                      "_context": [{"at": o, "text": _excerpt(t, term, 70)} for o, t in spread]})
    names.sort(key=lambda n: -n["count"])

    who = char_names(conn, book_id)
    done_a = _proposed(conn, "address")
    where, ids = scope_clause(conn, book_id)
    address = []
    from ..memory.address import _PRONOUNS

    for a in conn.execute(f"SELECT * FROM address_terms WHERE {where} AND (zh_origin IS NULL OR zh_origin!='human') "
                          "AND uses>0 AND (book_id=? OR last_book_id=?) ORDER BY uses DESC", (*ids, book_id, book_id)):
        if str(a["id"]) in done_a:
            continue
        address.append({"ref": str(a["id"]), "speaker": who.get(a["speaker_id"], a["speaker_id"]),
                        **({"pronoun": True} if a["form_ja"] in _PRONOUNS else {}),
                        "to": "（自稱）" if a["speaker_id"] == a["addressee_id"] else who.get(a["addressee_id"], a["addressee_id"]),
                        "ja": a["form_ja"], "zh": a["form_zh"], "uses": a["uses"],
                        **({"note": a["note"]} if a["note"] else {}),
                        **({"sample": a["evidence_quote"][:80]} if a["evidence_quote"] else {}),
                        "_context": _quotes(conn, a["id"]) or ([{"at": a["first_ordinal"], "text": a["evidence_quote"][:90]}]
                                                               if a["evidence_quote"] else [])})
    main = conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE} ORDER BY known_from_ordinal LIMIT 30",
                        (book_id,)).fetchall()
    to_check = []
    for c in suspects:
        pat = term_pattern(c["name"])
        hits = [(o, t) for o, t in texts if pat.search(t)]
        if not hits:
            continue
        spread = hits if len(hits) <= 4 else [hits[i * (len(hits) - 1) // 3] for i in range(4)]
        to_check.append({"ref": c["id"], "name_ja": c["name"], "name_zh": c["zh"], "overlaps_with": c["also"],
                         "count": len(hits), "samples": [_excerpt(t, c["name"], 60) for _, t in spread],
                         "_context": [{"at": o, "text": _excerpt(t, c["name"], 70)} for o, t in spread],
                         "_also_id": c["also_id"]})
    payload: dict[str, Any] = {
        "task": "naming",
        "characters": [{"name_ja": c["canonical_name"], "name_zh": c["display_name_zh"],
                        "aliases_ja": json.loads(c["aliases_json"] or "[]")} for c in main],
        "names": names[:MAX_NAMES], "address": address[:MAX_ADDRESS]}
    if to_check:
        payload["characters_to_check"] = to_check
    style = style_payload(load_style(conn, book_id), "")
    if style:
        payload["style"] = style
    return payload


def model_view(payload: dict[str, Any]) -> dict[str, Any]:
    """The request without the program's private keys (``_context``: example sentences kept for the user)."""
    return {k: ([{f: v for f, v in it.items() if not f.startswith("_")} for it in val] if isinstance(val, list) else val)
            for k, val in payload.items()}


def _clean(zh: str) -> str:
    return zh.strip()[:MAX_ZH]


def apply_naming(conn: sqlite3.Connection, book_id: str, payload: dict[str, Any], parsed: NamingOutput, *,
                 run_id: str | None = None) -> dict[str, int]:
    """Apply proposals as defaults; record each with its options; mark important open choices as pending."""
    stats = {"names": 0, "address": 0, "pending": 0, "rejected": 0}
    asked_n = {n["ref"]: n for n in payload.get("names", [])}
    asked_a = {a["ref"]: a for a in payload.get("address", [])}
    for kind, items, asked in (("name", parsed.names, asked_n), ("address", parsed.address, asked_a)):
        for it in items:
            src = asked.get(it.ref)
            zh = _clean(it.zh)
            if src is None or not zh:
                stats["rejected"] += 1
                continue
            options = [{"zh": zh, "reason": it.reason[:200]}]
            for alt in it.alternatives[:3]:
                a = _clean(alt.zh)
                if a and all(a != o["zh"] for o in options):
                    options.append({"zh": a, "reason": alt.reason[:200]})
            if kind == "name":
                importance = int(src["count"])
                single = src["category"] == "character" and len(zh) == 1
                needs = importance >= REVIEW_NAME_COUNT and (len(options) > 1 or single)
                label = src["ja"]
                _set_name(conn, book_id, src["ja"], zh)
            else:
                importance = int(src["uses"])
                # a pronoun's rendering is a choice about the addressee (妳／你／您): always worth a look when used often
                needs = importance >= REVIEW_ADDRESS_USES and (len(options) > 1 or bool(src.get("pronoun")))
                label = f"{src['speaker']} → {src['to']}：{src['ja']}"
                conn.execute("UPDATE address_terms SET form_zh=?, zh_origin='naming', updated_at=? WHERE id=? AND "
                             "(zh_origin IS NULL OR zh_origin!='human')", (zh, utcnow(), int(it.ref)))
            conn.execute(
                "INSERT INTO naming_proposals(book_id, kind, ref, label, previous_zh, chosen_zh, options_json, reason, "
                "importance, needs_review, status, run_id, created_at, context_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(kind, ref) DO UPDATE SET chosen_zh=excluded.chosen_zh, options_json=excluded.options_json, "
                "reason=excluded.reason, importance=excluded.importance, needs_review=excluded.needs_review, "
                "status=excluded.status, run_id=excluded.run_id, context_json=excluded.context_json",
                (book_id, kind, it.ref, label, src.get("zh"), zh, json.dumps(options, ensure_ascii=False),
                 it.reason[:200], importance, int(needs), "pending" if needs else "applied", run_id, utcnow(),
                 json.dumps(src.get("_context", []), ensure_ascii=False)))
            stats["names" if kind == "name" else "address"] += 1
            stats["pending"] += int(needs)
    for ch in parsed.characters:
        src = {c["ref"]: c for c in payload.get("characters_to_check", [])}.get(ch.ref)
        if src is None:
            stats["rejected"] += 1
            continue
        form, other = src["name_ja"], src["overlaps_with"]
        rename = {"action": "rename", "name_ja": ch.name_ja.strip()[:24] or form, "name_zh": _clean(ch.name_zh) or form,
                  "form": form, "other_id": src["_also_id"]}
        rename["zh"] = f"改名為「{rename['name_zh']}」，「{form}」設為與{other}共用的稱呼"
        merge = {"action": "merge", "other_id": src["_also_id"], "zh": f"與{other}是同一人（合併）"}
        keep = {"action": "keep", "zh": "維持現狀"}
        options = {"different": [rename, keep], "same": [merge, keep]}.get(ch.verdict, [rename, merge, keep])
        for o in options:
            o["reason"] = ch.reason[:200] if o is options[0] else ""
        conn.execute(
            "INSERT INTO naming_proposals(book_id, kind, ref, label, previous_zh, chosen_zh, options_json, reason, "
            "importance, needs_review, status, run_id, created_at, context_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(kind, ref) DO UPDATE SET chosen_zh=excluded.chosen_zh, options_json=excluded.options_json, "
            "reason=excluded.reason, status=excluded.status, context_json=excluded.context_json",
            (book_id, "character", ch.ref, f"角色「{form}」（與{other}的稱呼重疊）", src["name_zh"], options[0]["zh"],
             json.dumps(options, ensure_ascii=False), ch.reason[:200], int(src["count"]), 1, "pending", run_id, utcnow(),
             json.dumps(src["_context"], ensure_ascii=False)))
        stats["pending"] += 1
    stats["pending"] += flag_collisions(conn, book_id)
    return stats


def _fix_character(conn: sqlite3.Connection, book_id: str, cid: str, option: dict[str, Any]) -> None:
    """Apply a decided character fix: rename + shared form, merge into the other character, or nothing."""
    from ..memory import repository as repo
    from ..memory.entities import add_sense

    act = option.get("action")
    if act == "merge" and option.get("other_id"):
        repo.merge_characters(conn, option["other_id"], cid)
    elif act == "rename":
        c = conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
        if c is None:
            return
        form = option.get("form") or c["canonical_name"]
        aliases = [a for a in json.loads(c["aliases_json"] or "[]") if a != option["name_ja"]]
        if form not in aliases:
            aliases.append(form)
        conn.execute("UPDATE characters SET canonical_name=?, display_name_zh=?, aliases_json=?, updated_at=? WHERE id=?",
                     (option["name_ja"], option["name_zh"], json.dumps(aliases, ensure_ascii=False), utcnow(), cid))
        other = conn.execute("SELECT canonical_name, display_name_zh FROM characters WHERE id=?",
                             (option.get("other_id"),)).fetchone()
        if other is not None:
            repo.ensure_glossary(conn, book_id, form, option["name_zh"], category="character", origin="naming",
                                 first_unit_id=None)
            repo.ensure_glossary(conn, book_id, other[0], other[1] or other[0], category="character", origin="naming",
                                 first_unit_id=None)
            add_sense(conn, book_id, form, other[0], origin="human", note=f"也用來稱呼{option['name_zh']}")
        # address observations made while the form meant only this person are redone (it is ambiguous now)
        from ..memory.address import reharvest_scene
        from ..series import scope_clause as _sc

        where, ids = _sc(conn, book_id, "book_id")
        for bid, sid in conn.execute(f"SELECT DISTINCT book_id, scene_id FROM units WHERE {where} AND scene_id IS NOT "
                                     "NULL AND instr(plain_text, ?) > 0", (*ids, form)).fetchall():
            reharvest_scene(conn, bid, sid)
        # the form is no longer this person's name: a pending name proposal for it is moot
        conn.execute("UPDATE naming_proposals SET status='superseded' WHERE kind='name' AND ref=? AND status!='decided'",
                     (form,))


def _variant_key(form: str) -> str:
    """「あだっち」「あだっちー」 are one nickname; 「安達ちゃん」 and 「あだっちー」 are not."""
    from ..memory.address import _HONORIFIC_END

    return _HONORIFIC_END.sub("", form).replace("ー", "").replace("～", "")


def flag_collisions(conn: sqlite3.Connection, book_id: str) -> int:
    """Different forms of address for the same person rendered alike (島村媽媽's 「安達ちゃん」 and 日野's 「あだっちー」
    both → 「小安達」) lose a distinction the original makes: such proposals become pending, with a note naming the
    other form. Plain names (「安達」 for 安達) and spelling variants of one nickname are not collisions."""
    who = char_names(conn, book_id)
    where, ids = scope_clause(conn, book_id)
    rows = conn.execute(f"SELECT * FROM address_terms WHERE {where} AND form_zh IS NOT NULL", ids).fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    from ..memory.address import _PRONOUNS

    for r in rows:
        if r["speaker_id"] == r["addressee_id"] or r["form_zh"] == who.get(r["addressee_id"]) \
                or r["form_ja"] in _PRONOUNS:  # 「あんた」「お前」 to the same girl are both 「妳」: no distinction lost
            continue
        groups.setdefault((r["addressee_id"], r["form_zh"]), []).append(r)
    n = 0
    for (_, zh), rs in groups.items():
        if len({_variant_key(r["form_ja"]) for r in rs}) < 2:
            continue
        for r in rs:
            others = sorted({f"{who.get(o['speaker_id'], '?')}的「{o['form_ja']}」" for o in rs
                             if _variant_key(o["form_ja"]) != _variant_key(r["form_ja"])})
            note = f"與{'、'.join(others)}同譯為「{zh}」，原文的區別會消失"
            n += conn.execute("UPDATE naming_proposals SET needs_review=1, status='pending', reason=? WHERE kind='address' "
                              "AND ref=? AND status='applied'", (note, str(r["id"]))).rowcount
    return n


def _set_full_names(conn: sqlite3.Connection, book_id: str, term: str, old_zh: str, zh: str) -> None:
    """A given name decided alone (エリーゼ) also changes the full names that contain it (エリーゼ・ランメル
    愛麗絲·朗梅爾 → 艾莉潔·朗梅爾, ヴェガルタ魔法王国): unconfirmed glossary entries and character display names."""
    if not old_zh or old_zh == zh:
        return

    def swap(ja: str, cur: str | None) -> str | None:
        if cur and ja != term and ja.startswith(term) and cur.startswith(old_zh) and "・" not in ja:
            return zh + cur[len(old_zh):]  # ヴェガルタ魔法王国 威加爾達魔法王國 → 維加爾達魔法王國
        ja_parts, zh_parts = re.split(r"[・･·]", ja), re.split(r"[・･·]", cur or "")
        if len(ja_parts) < 2 or term not in ja_parts or len(zh_parts) != len(ja_parts):
            return None
        i = ja_parts.index(term)
        if zh_parts[i] != old_zh:
            return None
        zh_parts[i] = zh
        return "·".join(zh_parts)

    for r in conn.execute(f"SELECT id, source_term, target_term FROM glossary WHERE {SERIES_SCOPE} AND verified=0 "
                          "AND origin!='user' AND instr(source_term, ?) > 0", (book_id, term)).fetchall():
        new = swap(r["source_term"], r["target_term"])
        if new:
            conn.execute("UPDATE glossary SET target_term=?, origin='naming', updated_at=? WHERE id=?",
                         (new, utcnow(), r["id"]))
    for r in conn.execute(f"SELECT id, canonical_name, display_name_zh FROM characters WHERE {SERIES_SCOPE} "
                          "AND instr(canonical_name, ?) > 0", (book_id, term)).fetchall():
        new = swap(r["canonical_name"], r["display_name_zh"])
        if new:
            conn.execute("UPDATE characters SET display_name_zh=?, updated_at=? WHERE id=?", (new, utcnow(), r["id"]))


def _set_name(conn: sqlite3.Connection, book_id: str, term: str, zh: str, *, human: bool = False) -> None:
    """Change a glossary rendering (never one a human set unless ``human``) and the character's display name."""
    g = conn.execute(f"SELECT * FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, term)).fetchone()
    if g is None or (not human and (g["verified"] or g["origin"] == "user")):
        return
    _set_full_names(conn, book_id, term, g["target_term"], zh)
    if human:
        conn.execute("UPDATE glossary SET target_term=?, verified=1, origin='user', version=version+1, updated_at=? "
                     "WHERE id=?", (zh, utcnow(), g["id"]))
    elif g["target_term"] != zh:
        conn.execute("UPDATE glossary SET target_term=?, origin='naming', updated_at=? WHERE id=?",
                     (zh, utcnow(), g["id"]))
    conn.execute(f"UPDATE characters SET display_name_zh=?, updated_at=? WHERE {SERIES_SCOPE} AND canonical_name=?",
                 (zh, utcnow(), book_id, term))


def pending(conn: sqlite3.Connection, book_id: str, *, include_done: bool = False) -> list[dict[str, Any]]:
    where, ids = scope_clause(conn, book_id)
    q = f"SELECT * FROM naming_proposals WHERE {where}" + ("" if include_done else " AND status='pending'")
    return [{**dict(r), "options": json.loads(r["options_json"]), "context": json.loads(r["context_json"])}
            for r in conn.execute(q + " ORDER BY status='pending' DESC, importance DESC, id", ids)]


def decide(conn: sqlite3.Connection, book_id: str, pid: int, zh: str) -> dict[str, Any]:
    """A human choice: becomes the verified rendering (glossary) or the human address rendering."""
    row = conn.execute("SELECT * FROM naming_proposals WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise KeyError(f"unknown proposal id {pid}")
    zh = zh.strip() if row["kind"] == "character" else _clean(zh)  # a fix option is a sentence, not a name
    if not zh:
        raise ValueError("empty rendering")
    old: str | None = None  # the rendering in use until now (to find translations that still carry it)
    if row["kind"] == "name":
        g = conn.execute(f"SELECT target_term FROM glossary WHERE {SERIES_SCOPE} AND source_term=?",
                         (book_id, row["ref"])).fetchone()
        old = g[0] if g else None
    elif row["kind"] == "address":
        a = conn.execute("SELECT form_zh FROM address_terms WHERE id=?", (int(row["ref"]),)).fetchone()
        old = a[0] if a else None
    if row["kind"] == "character":
        opts = json.loads(row["options_json"])
        opt = next((o for o in opts if o["zh"] == zh), None)
        if opt is None:  # own text: rename to it
            base = next((o for o in opts if o.get("action") == "rename"), {"form": None, "other_id": None})
            opt = {**base, "action": "rename", "name_ja": zh, "name_zh": zh}
        _fix_character(conn, book_id, row["ref"], opt)
    elif row["kind"] == "name":
        _set_name(conn, book_id, row["ref"], zh, human=True)
        # the same form addressed to the same person follows (unless a person decided that pair differently).
        # Only that person: 「安達さん」 named as 安達's mother must not rename classmates' 「安達さん」 for 安達.
        from ..memory.entities import character_for_form

        owner = character_for_form(conn, book_id, row["ref"])
        where, ids = scope_clause(conn, book_id)
        same = [r[0] for r in conn.execute(f"SELECT id FROM address_terms WHERE {where} AND form_ja=? AND addressee_id=? "
                                           "AND (zh_origin IS NULL OR zh_origin!='human')",
                                           (*ids, row["ref"], owner))] if owner else []
        for aid in same:
            conn.execute("UPDATE address_terms SET form_zh=?, zh_origin='human', updated_at=? WHERE id=?",
                         (zh, utcnow(), aid))
            conn.execute("UPDATE naming_proposals SET status='decided', decided_zh=?, decided_at=? WHERE kind='address' "
                         "AND ref=? AND status!='decided'", (zh, utcnow(), str(aid)))
    else:
        conn.execute("UPDATE address_terms SET form_zh=?, zh_origin='human', updated_at=? WHERE id=?",
                     (zh, utcnow(), int(row["ref"])))
        # an unconfirmed glossary rendering of the same form would contradict the decision in translation requests:
        # when only this pair uses the form, the glossary follows (a form several people use keeps its own entry)
        a = conn.execute("SELECT form_ja FROM address_terms WHERE id=?", (int(row["ref"]),)).fetchone()
        if a is not None:
            where, ids = scope_clause(conn, book_id)
            users = conn.execute(f"SELECT COUNT(*) FROM address_terms WHERE {where} AND form_ja=?", (*ids, a[0])).fetchone()[0]
            plain_name = conn.execute(f"SELECT 1 FROM characters WHERE {SERIES_SCOPE} AND (canonical_name=? OR "
                                      "EXISTS (SELECT 1 FROM json_each(aliases_json) WHERE value=?))",
                                      (book_id, a[0], a[0])).fetchone()
            # a person's bare name (ファレグ in 「ファレグの坊主」) keeps its own rendering: the glossary is the name
            if users == 1 and not plain_name:
                conn.execute(f"UPDATE glossary SET target_term=?, origin='naming', updated_at=? WHERE {SERIES_SCOPE} AND "
                             "source_term=? AND verified=0 AND origin!='user'", (zh, utcnow(), book_id, a[0]))
    conn.execute("UPDATE naming_proposals SET status='decided', decided_zh=?, decided_at=? WHERE id=?",
                 (zh, utcnow(), pid))
    out: dict[str, Any] = {"id": pid, "label": row["label"], "zh": zh, "old": old, "affected": 0}
    if old and old != zh:
        from ..translation.rename import for_proposal

        out["affected"] = len(for_proposal(conn, book_id, pid, old))
    return out


def suspicious_characters(conn: sqlite3.Connection, book_id: str) -> list[dict[str, str]]:
    """Characters whose name is a form of address (「安達さん」): usually someone named only by how others address
    them, and the same form also addresses another person — such a character takes over the other person's lines."""
    from ..memory.address import _HONORIFIC_END

    out = []
    for c in conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book_id,)):
        base = _HONORIFIC_END.sub("", c["canonical_name"])
        if base != c["canonical_name"] and len(base) >= 1:
            other = conn.execute(f"SELECT canonical_name, id FROM characters WHERE {SERIES_SCOPE} AND id!=? AND "
                                 "(canonical_name=? OR instr(aliases_json, ?) > 0)",
                                 (book_id, c["id"], base, json.dumps(base, ensure_ascii=False))).fetchone()
            out.append({"id": c["id"], "name": c["canonical_name"], "zh": c["display_name_zh"] or "",
                        "also": other[0] if other else "", "also_id": other[1] if other else ""})
    return out


def refresh_context(conn: sqlite3.Connection, book_id: str) -> int:
    """(Re)compute the example sentences of every proposal of this book (proposals made before they were kept)."""
    texts = _book_texts(conn, book_id)
    n = 0
    for r in conn.execute("SELECT id, kind, ref FROM naming_proposals WHERE book_id=?", (book_id,)).fetchall():
        if r["kind"] == "name":
            pat = term_pattern(r["ref"])
            hits = [(o, t) for o, t in texts if pat.search(t)]
            spread = hits if len(hits) <= 3 else [hits[i * (len(hits) - 1) // 2] for i in range(3)]
            ctx = [{"at": o, "text": _excerpt(t, r["ref"], 70)} for o, t in spread]
        else:
            ctx = _quotes(conn, int(r["ref"]))
        conn.execute("UPDATE naming_proposals SET context_json=? WHERE id=?", (json.dumps(ctx, ensure_ascii=False), r["id"]))
        n += 1
    return n
