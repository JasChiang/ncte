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
    rows = conn.execute("SELECT u.ordinal, ut.quote_text FROM address_observations o JOIN utterances ut ON "
                        "ut.id=o.utterance_id JOIN units u ON u.id=ut.unit_id WHERE o.address_id=? ORDER BY u.ordinal",
                        (address_id,)).fetchall()
    if len(rows) > limit:
        rows = [rows[i * (len(rows) - 1) // (limit - 1)] for i in range(limit)]
    return [{"at": r[0], "text": match_text(r[1])[:90]} for r in rows]


_HIRA = "\u3041-\u309f"
_KATA = "\u30a0-\u30ff\u31f0-\u31ff"


def term_pattern(term: str) -> re.Pattern[str]:
    """A kana-only name must not match inside a longer kana word (「のん」 in 「のんびり」)."""
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
    for a in conn.execute(f"SELECT * FROM address_terms WHERE {where} AND (zh_origin IS NULL OR zh_origin!='human') "
                          "AND uses>0 AND (book_id=? OR last_book_id=?) ORDER BY uses DESC", (*ids, book_id, book_id)):
        if str(a["id"]) in done_a:
            continue
        address.append({"ref": str(a["id"]), "speaker": who.get(a["speaker_id"], a["speaker_id"]),
                        "to": "（自稱）" if a["speaker_id"] == a["addressee_id"] else who.get(a["addressee_id"], a["addressee_id"]),
                        "ja": a["form_ja"], "zh": a["form_zh"], "uses": a["uses"],
                        **({"note": a["note"]} if a["note"] else {}),
                        **({"sample": a["evidence_quote"][:80]} if a["evidence_quote"] else {}),
                        "_context": _quotes(conn, a["id"]) or ([{"at": a["first_ordinal"], "text": a["evidence_quote"][:90]}]
                                                               if a["evidence_quote"] else [])})
    main = conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE} ORDER BY known_from_ordinal LIMIT 30",
                        (book_id,)).fetchall()
    payload: dict[str, Any] = {
        "task": "naming",
        "characters": [{"name_ja": c["canonical_name"], "name_zh": c["display_name_zh"],
                        "aliases_ja": json.loads(c["aliases_json"] or "[]")} for c in main],
        "names": names[:MAX_NAMES], "address": address[:MAX_ADDRESS]}
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
                needs = importance >= REVIEW_ADDRESS_USES and len(options) > 1
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
    return stats


def _set_name(conn: sqlite3.Connection, book_id: str, term: str, zh: str, *, human: bool = False) -> None:
    """Change a glossary rendering (never one a human set unless ``human``) and the character's display name."""
    g = conn.execute(f"SELECT * FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, term)).fetchone()
    if g is None or (not human and (g["verified"] or g["origin"] == "user")):
        return
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
    zh = _clean(zh)
    if not zh:
        raise ValueError("empty rendering")
    if row["kind"] == "name":
        _set_name(conn, book_id, row["ref"], zh, human=True)
    else:
        conn.execute("UPDATE address_terms SET form_zh=?, zh_origin='human', updated_at=? WHERE id=?",
                     (zh, utcnow(), int(row["ref"])))
    conn.execute("UPDATE naming_proposals SET status='decided', decided_zh=?, decided_at=? WHERE id=?",
                 (zh, utcnow(), pid))
    return {"id": pid, "label": row["label"], "zh": zh}


def suspicious_characters(conn: sqlite3.Connection, book_id: str) -> list[dict[str, str]]:
    """Characters whose name is a form of address (「安達さん」): usually someone named only by how others address
    them, and the same form also addresses another person — such a character takes over the other person's lines."""
    from ..memory.address import _HONORIFIC_END

    out = []
    for c in conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book_id,)):
        base = _HONORIFIC_END.sub("", c["canonical_name"])
        if base != c["canonical_name"] and len(base) >= 1:
            other = conn.execute(f"SELECT canonical_name FROM characters WHERE {SERIES_SCOPE} AND id!=? AND "
                                 "(canonical_name=? OR instr(aliases_json, ?) > 0)",
                                 (book_id, c["id"], base, json.dumps(base, ensure_ascii=False))).fetchone()
            out.append({"id": c["id"], "name": c["canonical_name"], "zh": c["display_name_zh"] or "",
                        "also": other[0] if other else ""})
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
