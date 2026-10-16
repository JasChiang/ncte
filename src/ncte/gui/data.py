"""Read models for the local web UI: plain dicts/lists, JSON-ready. All reads; writes live in ``actions``."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from typing import Any

from ..epub.matter import book_matter, toc_labels
from ..epub.units import RUBY_NOTATION, match_text
from ..library import Book
from ..memory.address import find_forms, name_forms
from ..series import SERIES_SCOPE
from ..translation.cache import latest_texts

PH = re.compile(r"⟦/?\d+/?⟧")
RUBY = RUBY_NOTATION  # readings only; the author's own 《》 brackets are text
EDGE = re.compile(r"^((?:⟦\d+/?⟧)*)(.*?)((?:⟦/\d+⟧)*)$", re.S)


def plain(text: str | None) -> str:
    return PH.sub("", text or "")


def split_edges(text: str) -> tuple[str, str, str]:
    """「⟦1⟧…⟦/1⟧」 → (「⟦1⟧」, 「…」, 「⟦/1⟧」): the wrapper every paragraph carries is not shown for editing."""
    m = EDGE.match(text or "")
    return (m.group(1), m.group(2), m.group(3)) if m else ("", text or "", "")


# ---------------------------------------------------------------- library / overview
def library(ws: Any) -> list[dict[str, Any]]:
    out = []
    for s in ws.list_series():
        vols = []
        for v in s["volumes"]:
            try:
                conn, book = ws.open_book(v["book_id"])
            except Exception:  # a volume whose folder is missing is listed without progress
                vols.append({**v, "translated": None, "translatable": None})
                continue
            total = conn.execute("SELECT COUNT(*) FROM units WHERE book_id=? AND translatable=1", (book.id,)).fetchone()[0]
            read = conn.execute("SELECT COUNT(*) FROM scenes WHERE book_id=? AND version>0", (book.id,)).fetchone()[0]
            vols.append({**v, "translatable": total, "translated": len(latest_texts(conn, book.id)), "read": read})
        out.append({**s, "volumes": vols})
    return out


def overview(conn: sqlite3.Connection, book: Book) -> dict[str, Any]:
    from ..analysis.naming import pending
    from ..reports.qa_report import book_status

    st = book_status(conn, book.id)
    best = latest_texts(conn, book.id)
    flagged = conn.execute("SELECT COUNT(DISTINCT unit_id) FROM qa_findings WHERE book_id=? AND status='open' AND "
                           "severity!='low' AND unit_id IS NOT NULL", (book.id,)).fetchone()[0]
    unknown = conn.execute("SELECT COUNT(*) FROM utterances WHERE book_id=? AND status='unknown'", (book.id,)).fetchone()[0]
    return {"book_id": book.id, "title": book.title, **st, "spent_usd": round(sum(r["cost_usd"] or 0 for r in st["runs"]), 4),
            "flagged_units": flagged, "unknown_speakers": unknown, "pending_names": len(pending(conn, book.id)),
            "notes": sum(1 for t in best.values() if t["translator_note"] and t["status"] != "human"),
            "human_edits": sum(1 for t in best.values() if t["status"] == "human")}


def _read_range(conn: sqlite3.Connection, book_id: str) -> dict[str, Any]:
    """How much of the book the reading covered: an impression from a sample is not the whole book's."""
    last = conn.execute("SELECT MAX(end_ordinal) FROM scenes WHERE book_id=? AND version>0", (book_id,)).fetchone()[0]
    unread = conn.execute("SELECT COUNT(*) FROM scenes s WHERE book_id=? AND version=0 AND EXISTS (SELECT 1 FROM units u "
                          "WHERE u.scene_id=s.id AND u.translatable=1 AND u.kind IN ('text','heading'))", (book_id,)
                          ).fetchone()[0]
    return {"read_to": last, "read_all": not unread}


# ---------------------------------------------------------------- chapters / units
def chapters(conn: sqlite3.Connection, book: Book) -> list[dict[str, Any]]:
    matter = book_matter(conn, book.id)
    try:
        labels = toc_labels(book.stored_path)
    except Exception:
        labels = {}
    zh_label = {r[0]: plain(r[1]) for r in conn.execute(
        "SELECT u.plain_text, (SELECT text FROM translations t WHERE t.unit_id=u.id ORDER BY rowid DESC LIMIT 1) "
        "FROM units u WHERE u.book_id=? AND u.kind IN ('nav','ncx')", (book.id,)) if r[1]}
    from ..translation.context_builder import heading_hints

    titles = {h["ja"]: plain(h["zh"]) for h in heading_hints(conn, book.id)}  # the TOC not translated yet (a sample)
    out = []
    carry = ""  # a chapter's title page (TOC target, almost no text) names the file that follows it
    for i, (href, kind) in enumerate(matter.items()):
        r = conn.execute("SELECT MIN(ordinal), MAX(ordinal), COUNT(*) FROM units WHERE book_id=? AND resource_href=? AND "
                         "translatable=1 AND kind IN ('text','heading')", (book.id, href)).fetchone()
        own = next((v for k, v in labels.items() if k.endswith(href)), "")
        if not r[2] or (own and r[2] <= 3 and kind == "story"):
            carry = own or carry
            if not r[2]:
                continue
        label, carry = (own or carry), ""
        first = conn.execute("SELECT plain_text FROM units WHERE book_id=? AND resource_href=? AND kind IN "
                             "('text','heading') ORDER BY ordinal LIMIT 1", (book.id, href)).fetchone()
        title = zh_label.get(label) or titles.get(label) or label or (RUBY.sub("", first[0])[:16] if first else href)
        out.append({"index": i, "href": href, "kind": kind, "title": title, "start": r[0], "end": r[1], "units": r[2]})
    return out


def units(conn: sqlite3.Connection, book: Book, *, start: int | None = None, end: int | None = None,
          filt: str = "all", limit: int = 400) -> dict[str, Any]:
    best = latest_texts(conn, book.id)
    names = {r["id"]: r["display_name_zh"] or r["canonical_name"]
             for r in conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book.id,))}
    q = ("SELECT * FROM units WHERE book_id=? AND translatable=1 AND kind IN ('text','heading') AND spine_index IS NOT NULL")
    params: list[Any] = [book.id]
    if start is not None and end is not None:
        q += " AND ordinal BETWEEN ? AND ?"
        params += [start, end]
    rows = conn.execute(q + " ORDER BY ordinal", params).fetchall()
    findings: dict[str, list[dict[str, str]]] = defaultdict(list)
    for f in conn.execute("SELECT unit_id, type, severity, evidence FROM qa_findings WHERE book_id=? AND status='open' "
                          "AND unit_id IS NOT NULL", (book.id,)):
        findings[f["unit_id"]].append({"type": f["type"], "severity": f["severity"], "evidence": (f["evidence"] or "")[:300]})
    quotes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for u in conn.execute("SELECT id, unit_id, quote_text, speaker_id_nullable sp, status, notes_json FROM utterances "
                          "WHERE book_id=? ORDER BY unit_id, quote_index", (book.id,)):
        # the program and the model disagreed (ADR-031) and / or the line got a second look (ADR-033)
        notes = json.loads(u["notes_json"] or "[]")
        second = next((n[len("second look: "):] for n in reversed(notes) if n.startswith("second look: ")), None)
        quotes[u["unit_id"]].append({"id": u["id"], "speaker": names.get(u["sp"]) if u["sp"] else None,
                                     "status": u["status"], "quote": match_text(u["quote_text"])[:40],
                                     "disputed": any("(LLM kept)" in n or "conflict:" in n for n in notes),
                                     **({"second": second[:160]} if second else {})})
    found: dict[str, list[dict[str, str]]] = defaultdict(list)
    for w in conn.execute("SELECT unit_id, term, explanation, note_zh FROM reader_notes WHERE book_id=? AND "
                          "status!='dropped'", (book.id,)):
        found[w["unit_id"]].append({"term": w["term"], "explanation": w["explanation"], "note": w["note_zh"] or ""})
    out = []
    for r in rows:
        tr = best.get(r["id"])
        f = findings.get(r["id"], [])
        serious = [x for x in f if x["severity"] != "low"]
        if filt == "flagged" and not serious:
            continue
        if filt == "unknown" and not any(q["status"] == "unknown" for q in quotes.get(r["id"], [])):
            continue
        if filt == "notes" and not (tr is not None and tr["translator_note"] or found.get(r["id"])):
            continue
        if filt == "human" and not (tr is not None and tr["status"] == "human"):
            continue
        pre, inner, post = split_edges(tr["text"] if tr is not None else "")
        out.append({"id": r["id"], "ordinal": r["ordinal"], "kind": r["kind"], "ja": RUBY.sub("", plain(r["plain_text"])),
                    "zh": plain(tr["text"]) if tr is not None else None, "edit": inner, "edit_pre": pre, "edit_post": post,
                    "status": tr["status"] if tr is not None else None,
                    "note": (tr["translator_note"] or "") if tr is not None else "",
                    "quotes": quotes.get(r["id"], []), "findings": serious, "minor": len(f) - len(serious),
                    "wordplay": found.get(r["id"], [])})
        if len(out) >= limit:
            break
    return {"units": out, "truncated": len(out) >= limit}


# ---------------------------------------------------------------- reading: scenes, characters, names
def story(conn: sqlite3.Connection, book: Book) -> dict[str, Any]:
    matter = book_matter(conn, book.id)
    chs = chapters(conn, book)
    ch_of = {c["href"]: c["index"] for c in chs}
    summ = {r[0]: r[1] for r in conn.execute("SELECT scene_id, summary FROM chapter_summaries WHERE book_id=?", (book.id,))}
    spk: dict[str, set[str]] = defaultdict(set)
    for u in conn.execute("SELECT scene_id, speaker_id_nullable sp FROM utterances WHERE book_id=? AND sp IS NOT NULL",
                          (book.id,)):
        spk[u["scene_id"]].add(u["sp"])
    by_scene: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute("SELECT id, scene_id, plain_text, resource_href, ordinal FROM units WHERE book_id=? AND "
                          "translatable=1 AND kind IN ('text','heading') ORDER BY ordinal", (book.id,)):
        by_scene[r["scene_id"]].append(r)
    scenes = []
    for s in conn.execute("SELECT * FROM scenes WHERE book_id=? ORDER BY start_ordinal", (book.id,)):
        rows = by_scene.get(s["id"], [])
        if not rows:
            continue
        href = rows[0]["resource_href"]
        forms, _ = name_forms(conn, book.id, s["end_ordinal"])  # only characters known by then (ADR-022 fix)
        named = {cid for r in rows for _, cid in find_forms(r["plain_text"], forms)}
        scenes.append({"id": s["id"], "a": s["start_ordinal"], "b": s["end_ordinal"], "ch": ch_of.get(href),
                       "story": matter.get(href) == "story", "summary": summ.get(s["id"], ""),
                       "speakers": sorted(spk[s["id"]]), "who": sorted(spk[s["id"]] | named)})
    return {"chapters": chs, "scenes": scenes}


def characters(conn: sqlite3.Connection, book: Book, *, series: bool = False) -> dict[str, Any]:
    """Characters, address terms and scenes for the reading pages. ``series``: earlier volumes' story scenes are
    included too (always visible: they come before anything in this volume, marked with negative positions)."""
    from ..library import resolve_book

    st = story(conn, book)
    if series:
        from ..series import volume_of

        here_v = volume_of(conn, book.id)
        from ..series import scope_clause

        where, ids = scope_clause(conn, book.id, "id")
        earlier = [r[0] for r in conn.execute(f"SELECT id FROM books WHERE {where} AND volume_index<? "
                                              "ORDER BY volume_index", (*ids, here_v))]
        prior = []
        for bid in earlier:
            prior += [{**s, "a": -1, "b": -1, "ch": None, "vol": bid} for s in story(conn, resolve_book(conn, bid))["scenes"]
                      if s["story"]]
        st = {**st, "scenes": prior + st["scenes"]}
    story_scenes = [s for s in st["scenes"] if s["story"]]
    scene_count = Counter(c for s in story_scenes for c in s["who"])
    in_story = {s["id"] for s in story_scenes}
    lines = Counter(u["sp"] for u in conn.execute(
        f"SELECT speaker_id_nullable sp, scene_id FROM utterances WHERE {SERIES_SCOPE} AND sp IS NOT NULL", (book.id,))
        if u["scene_id"] in in_story)
    chars = {r["id"]: r for r in conn.execute(f"SELECT * FROM characters WHERE {SERIES_SCOPE}", (book.id,))}
    # every source quote shown with the translation of its paragraph, so the page reads in both languages
    best = latest_texts(conn, book.id)
    texts = {u["ordinal"]: (RUBY.sub("", plain(u["plain_text"])), plain(best[u["id"]]["text"]) if u["id"] in best else None)
             for u in conn.execute("SELECT id, ordinal, plain_text FROM units WHERE book_id=?", (book.id,))}

    def zh_of(quote: str | None, at: int | None) -> str | None:
        """The translation of the paragraph the quote is from: near where it was cited (a note may quote a later
        paragraph than the one it starts at), else the paragraph at that position."""
        if at is None or at < 0:
            return None
        key = RUBY.sub("", plain(quote or "")).strip("「」『』　 ")[:12]
        if key:
            for o in sorted(range(at - 3, at + 80), key=lambda o: abs(o - at)):
                t = texts.get(o)
                if t and key in t[0] and t[1]:
                    return _excerpt(t[0], t[0].find(key), len(quote or ""), t[1])
        return (texts.get(at) or (None, None))[1]
    rendering = {g["source_term"]: g["target_term"] for g in conn.execute(
        f"SELECT source_term, target_term FROM glossary WHERE {SERIES_SCOPE}", (book.id,))}
    facts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    from ..series import volume_of as _vol
    from ..series import volumes as _vols

    _v, _here = _vols(conn), _vol(conn, book.id)
    _ids = re.compile(r"(?<![0-9A-Za-z_])c_[0-9a-f]{12}(?![0-9a-f])")

    def _names(text: str) -> str:  # facts sometimes mention a character by its internal id
        return _ids.sub(lambda m: chars[m.group(0)]["display_name_zh"] or chars[m.group(0)]["canonical_name"]
                        if m.group(0) in chars else m.group(0), text or "")
    for f in conn.execute(f"SELECT * FROM facts WHERE {SERIES_SCOPE} AND retracted_by IS NULL ORDER BY "
                          "first_revealed_ordinal", (book.id,)):
        fv = _v.get(f["book_id"], 1.0)
        if f["subject"] in chars and (fv == _here or (series and fv < _here)):
            facts[f["subject"]].append({"at": f["first_revealed_ordinal"] if fv == _here else -1,
                                        "predicate": _names(f["predicate"]), "object": _names(f["object"]),
                                        "type": f["fact_type"], "certainty": f["certainty"],
                                        "viewpoint": _names(f["source_viewpoint"] or "") or None, "quote": RUBY.sub("", f["evidence_quote"] or ""),
                                        "quote_zh": zh_of(f["evidence_quote"], f["first_revealed_ordinal"]) if fv == _here else None})
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in conn.execute("SELECT e.character_id, e.raw_text, u.ordinal FROM speech_examples e JOIN units u ON "
                          "u.id=e.unit_id WHERE u.book_id=? ORDER BY u.ordinal", (book.id,)):
        examples[e["character_id"]].append({"at": e["ordinal"], "text": RUBY.sub("", e["raw_text"]), "zh": zh_of(e["raw_text"], e["ordinal"])})
    matter_first = {c["start"]: c["kind"] for c in st["chapters"]}
    from ..series import volume_of, volumes

    vols, here = volumes(conn), volume_of(conn, book.id)

    def info(ordinal: int) -> bool:
        kinds = [k for s, k in sorted(matter_first.items()) if s <= ordinal]
        return bool(kinds) and kinds[-1] != "story"

    from ..analysis.dialogue import FIRST_PERSON

    # how each one says "I" in their lines (all lines the read pass gave them, inferred too)
    fp_re = re.compile("|".join(sorted((re.escape(w) for w in FIRST_PERSON if w not in ("自分", "うち", "ウチ")),
                                       key=len, reverse=True)))
    first_person: dict[str, Counter[str]] = defaultdict(Counter)
    for u in conn.execute("SELECT speaker_id_nullable sp, quote_text FROM utterances WHERE book_id=? AND sp IS NOT NULL",
                          (book.id,)):
        first_person[u["sp"]].update(fp_re.findall(RUBY.sub("", u["quote_text"] or "")))
    genders = {g["character_id"]: g for g in conn.execute(
        f"SELECT g.* FROM character_gender g JOIN characters c ON c.id=g.character_id WHERE c.{SERIES_SCOPE}",
        (book.id,))}
    out = {}
    for cid, c in chars.items():
        cv = vols.get(c["book_id"], 1.0)
        if cv > here or (c["book_id"] != book.id and not scene_count.get(cid) and not series):
            continue
        # the name as the translation writes it: the glossary's rendering (your choice included) over the card's own
        out[cid] = {"ja": c["canonical_name"], "zh": rendering.get(c["canonical_name"]) or c["display_name_zh"] or c["canonical_name"],
                    "aka": [{"ja": a, "zh": rendering.get(a)} for a in json.loads(c["aliases_json"] or "[]")], "from": c["known_from_ordinal"] if cv == here else -1,
                    "scenes": scene_count.get(cid, 0), "lines": lines.get(cid, 0), "facts": facts.get(cid, []),
                    "examples": examples.get(cid, [])[:6], "first_person": first_person[cid].most_common(4),
                    "info": cv == here and info(c["known_from_ordinal"])}  # earlier volumes: never book matter here
        if c["is_narrator"]:
            out[cid]["narrator"] = True
        imp = conn.execute("SELECT * FROM character_impressions WHERE character_id=?", (cid,)).fetchone()
        if imp is not None:
            out[cid]["impression"] = {"personality": imp["personality"], "voice": imp["voice"], "role": imp["role"],
                                      **_read_range(conn, book.id)}
        for key, sql in (("members", "SELECT member_id FROM group_members WHERE group_id=?"),
                         ("member_of", "SELECT group_id FROM group_members WHERE member_id=?")):
            if ids := [r[0] for r in conn.execute(sql, (cid,)) if r[0] in chars]:
                out[cid][key] = ids  # a group and who it is made of (ADR-046)
        g = genders.get(cid)
        if g is not None and vols.get(g["book_id"], 1.0) <= here:
            out[cid]["gender"] = {"g": g["gender"], "sure": "human" if g["origin"] == "human" else g["certainty"],
                                  "quote": RUBY.sub("", g["evidence_quote"] or ""), "quote_zh": zh_of(g["evidence_quote"], g["ordinal"]),
                                  "at": g["ordinal"] if vols.get(g["book_id"], 1.0) == here else -1}
    from ..memory.address import usage_of

    address = []
    for a in conn.execute(f"SELECT * FROM address_terms WHERE {SERIES_SCOPE} ORDER BY uses DESC", (book.id,)):
        v = vols.get(a["book_id"], 1.0)
        usage = usage_of(conn, a)
        if v > here or usage == "unrelated":  # later volume: not known yet; a look-alike word: not a form at all
            continue
        address.append({"speaker": a["speaker_id"], "to": a["addressee_id"], "ja": a["form_ja"], "zh": a["form_zh"],
                        "usage": usage,
                        "uses": a["uses"], "at": a["first_ordinal"] if v == here else -1,
                        "certainty": a["certainty"], "origin": a["zh_origin"]})
    quotes = {}  # evidence shown on the character page: the note or fact quoted from the source
    for r in conn.execute("SELECT id, evidence_quote, first_ordinal FROM relation_notes WHERE book_id=?", (book.id,)):
        quotes[("note", r["id"])] = (r["evidence_quote"], r["first_ordinal"])
    for r in conn.execute("SELECT id, evidence_quote, first_revealed_ordinal FROM facts WHERE book_id=?", (book.id,)):
        quotes[("fact", r["id"])] = (r["evidence_quote"], r["first_revealed_ordinal"])
    rels = []
    for r in conn.execute(f"SELECT * FROM relations WHERE {SERIES_SCOPE}", (book.id,)):
        if vols.get(r["book_id"], 1.0) > here or r["a_id"] not in out or r["b_id"] not in out:
            continue
        this = r["book_id"] == book.id
        for e in json.loads(r["evidence_json"] or "[]"):
            if e["kind"] == "passage" and ("passage", e["id"]) not in quotes:  # a paragraph cited as it stands
                u = conn.execute("SELECT plain_text, ordinal FROM units WHERE id=?", (e["id"],)).fetchone()
                if u:
                    quotes[("passage", e["id"])] = (RUBY.sub("", plain(u[0]))[:200], u[1])
        ev = [quotes[(e["kind"], e["id"])] for e in json.loads(r["evidence_json"] or "[]") if (e["kind"], e["id"]) in quotes]
        rels.append({"a": r["a_id"], "b": r["b_id"], "rel": r["relation_zh"], "a_is": r["a_is"], "b_is": r["b_is"],
                     "at": r["since_ordinal"] if this else -1,
                     "until": (r["until_ordinal"] if this else -1) if r["until_ordinal"] is not None else None,
                     "kind": r["kind"] or "identity", "certainty": r["certainty"] or "explicit", "origin": r["origin"],
                     "reason": r["reason"], "evidence": [{"quote": q, "zh": zh_of(q, o) if this else None, "at": o if this else -1}
                                                          for q, o in ev[:3]]})
    return {"characters": out, "address": address, "relations": rels, "scenes": st["scenes"], "chapters": st["chapters"]}


def _excerpt(ja: str, at: int, n: int, zh: str, limit: int = 120) -> str:
    """A long translated paragraph cut to the sentences at the same place as the quote in the source (display only:
    the quote is a part of the paragraph, so the translation shown is the matching part, with … where cut)."""
    if len(zh) <= limit:
        return zh
    import re as _re

    sentences = [x for x in _re.split(r"(?<=[。！？!?」』])", zh) if x]
    mid = (at + n / 2) / max(1, len(ja)) * len(zh)
    pos, pick = 0, 0
    for i, x in enumerate(sentences):
        if pos + len(x) >= mid:
            pick = i
            break
        pos += len(x)
    lo = hi = pick
    want = max(40, int(n * 1.3))
    while sum(len(x) for x in sentences[lo:hi + 1]) < want and (lo > 0 or hi < len(sentences) - 1):
        if hi < len(sentences) - 1:
            hi += 1
        if sum(len(x) for x in sentences[lo:hi + 1]) < want and lo > 0:
            lo -= 1
    text = "".join(sentences[lo:hi + 1]).strip()[:limit]
    return ("…" if lo > 0 else "") + text + ("…" if hi < len(sentences) - 1 else "")


def glossary(conn: sqlite3.Connection, book: Book) -> list[dict[str, Any]]:
    return [{"ja": g["source_term"], "zh": g["target_term"], "category": g["category"], "verified": bool(g["verified"]),
             "origin": g["origin"], "entity": g["entity_id"], "form": g["form_type"]}
            for g in conn.execute(f"SELECT * FROM glossary WHERE {SERIES_SCOPE} ORDER BY category, source_term", (book.id,))]


def proposals(conn: sqlite3.Connection, book: Book, *, include_done: bool = False) -> list[dict[str, Any]]:
    """Proposals with their example sentences; each example also carries the current translation of its paragraph
    (when it has been translated), so a choice can be checked against how the line reads in Chinese now."""
    from ..analysis.naming import pending

    best = latest_texts(conn, book.id)
    out = []
    for r in pending(conn, book.id, include_done=include_done):
        for c in r["context"]:
            uid = c.get("unit")
            if not uid:
                row = conn.execute("SELECT id FROM units WHERE book_id=? AND ordinal=?", (r["book_id"], c["at"])).fetchone()
                uid = row[0] if row else None
            tr = best.get(uid) if uid else None
            if tr is None and uid:
                t = conn.execute("SELECT text FROM translations t JOIN units u ON u.id=t.unit_id WHERE t.unit_id=? AND "
                                 "t.source_hash=u.source_hash ORDER BY t.id DESC LIMIT 1", (uid,)).fetchone()
                c["zh"] = plain(t[0]) if t else ""
            else:
                c["zh"] = plain(tr["text"]) if tr is not None else ""
        item = {k: v for k, v in r.items() if k not in ("options_json", "context_json")}
        if r["kind"] == "address":  # other spellings of the same nickname already decided (「みずのっつぃー」→ 水野仔～～)
            from ..memory.address import decided_variants

            a = conn.execute("SELECT * FROM address_terms WHERE id=?", (int(r["ref"]),)).fetchone()
            if a is not None:
                item["related"] = decided_variants(conn, a["speaker_id"], a["addressee_id"], a["form_ja"])
        out.append(item)
    return out
