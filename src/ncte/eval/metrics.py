"""Evaluation metrics against human annotations (spec §11). No LLM-judged "preference" scores.

Translation-level metrics (all experiments):
* ``unknown_false_assertion_rate``  gold-unknown utterances whose translation names a character
                                    that is not in the source sentence
* ``omitted_argument_error_rate``   annotated units containing a forbidden addition (name, gender pronoun …)
* ``omission_rate`` / ``omission_addition_rate``  required content missing / forbidden content added
* ``entity_terminology_consistency``  share of character mentions rendered with the majority allowed name
* ``spoiler_violations``            forbidden reveal terms in translations before the reveal
System-state metrics (context experiments only; baseline has no speaker state → ``None``):
* ``speaker_accuracy_explicit``     gold speakers matched by the system's speaker (explicit/inferred)
* ``speaker_explicit_precision``    system ``explicit`` attributions that are correct
* ``unknown_asserted_rate``         gold-unknown utterances the system marked ``explicit``
* ``weak_evidence_asserted``        style-only lines the system marked ``explicit`` (should be 0)
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any

from ..translation.cache import latest_texts
from .dataset import Dataset, item_doc_href


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def line_units(conn: sqlite3.Connection, book_id: str, ds: Dataset) -> dict[tuple[str, int], sqlite3.Row]:
    out: dict[tuple[str, int], sqlite3.Row] = {}
    for item in ds.items:
        href = item_doc_href(item)
        rows = conn.execute("SELECT * FROM units WHERE book_id=? AND resource_href LIKE ? AND kind='text' ORDER BY ordinal",
                            (book_id, f"%{href}")).fetchall()
        if len(rows) != len(item.lines):
            raise ValueError(f"{item.id}: {len(rows)} units for {len(item.lines)} lines (dataset/EPUB mismatch)")
        for i, r in enumerate(rows):
            out[(item.id, i)] = r
    return out


def compute_metrics(conn: sqlite3.Connection, book_id: str, ds: Dataset, *, has_speaker_state: bool) -> dict[str, Any]:
    lu = line_units(conn, book_id, ds)
    best = latest_texts(conn, book_id)

    def tr(item_id: str, line: int) -> str:
        r = lu[(item_id, line)]
        t = best.get(r["id"])
        return t["text"] if t else ""

    all_zh = {c.name_ja: c.allowed_zh for c in ds.characters}
    # ---------------- translation level
    unk_total = unk_bad = 0
    ann = forb_units = forb_bad = omissions = additions = 0
    spoiler_bad = 0
    details: list[dict[str, Any]] = []
    for item in ds.items:
        for g in item.gold.utterances:
            if g.speaker is None:
                unk_total += 1
                src = lu[(item.id, g.line)]["plain_text"]
                text = tr(item.id, g.line)
                named = [zh for ja, zhs in all_zh.items() if ja not in src for zh in zhs if zh in text]
                if named:
                    unk_bad += 1
                    details.append({"item": item.id, "line": g.line, "issue": "unknown speaker named", "names": named})
        for gu in item.gold.units:
            ann += 1
            text = tr(item.id, gu.line)
            bad = [w for w in gu.forbidden if w in text]
            if gu.forbidden:
                forb_units += 1
                if bad:
                    forb_bad += 1
            additions += len(bad)
            missing = [grp for grp in gu.required_any if not any(w in text for w in grp)]
            omissions += len(missing)
            if bad or missing:
                details.append({"item": item.id, "line": gu.line, "forbidden_found": bad, "required_missing": missing,
                                "translation": text[:120]})
        if item.gold.spoiler:
            for line in range(item.gold.spoiler.before_line):
                text = tr(item.id, line)
                leaked = [w for w in item.gold.spoiler.terms if w in text]
                if leaked:
                    spoiler_bad += 1
                    details.append({"item": item.id, "line": line, "issue": "spoiler", "terms": leaked})
    # terminology consistency over every unit mentioning a character
    renders: dict[str, Counter[str]] = {c.name_ja: Counter() for c in ds.characters}
    for (_item, _line), r in lu.items():
        text = best[r["id"]]["text"] if r["id"] in best else ""
        for c in ds.characters:
            if c.name_ja in r["plain_text"]:
                used = next((z for z in c.allowed_zh if z in text), "<other>")
                renders[c.name_ja][used] += 1
    mentions = sum(sum(c.values()) for c in renders.values())
    consistent = sum(max((n for k, n in c.items() if k != "<other>"), default=0) for c in renders.values())
    required_groups = sum(len(gu.required_any) for it in ds.items for gu in it.gold.units)
    out: dict[str, Any] = {
        "unknown_false_assertion_rate": _ratio(unk_bad, unk_total),
        "omitted_argument_error_rate": _ratio(forb_bad, forb_units),
        "omission_rate": _ratio(omissions, required_groups),
        "omission_addition_rate": _ratio(omissions + additions, ann),
        "entity_terminology_consistency": _ratio(consistent, mentions),
        "spoiler_violations": spoiler_bad,
        "translated_units": sum(1 for r in lu.values() if r["id"] in best),
        "annotated_units": ann,
        "terminology_renderings": {k: dict(v) for k, v in renders.items() if v},
    }
    # ---------------- system speaker state
    if has_speaker_state:
        named_total = named_ok = sys_explicit = sys_explicit_ok = unk_asserted = weak_asserted = 0
        for item in ds.items:
            for g in item.gold.utterances:
                r = lu[(item.id, g.line)]
                u = conn.execute(
                    "SELECT u.status, c.canonical_name AS name FROM utterances u LEFT JOIN characters c ON "
                    "c.id=u.speaker_id_nullable WHERE u.unit_id=? AND u.quote_index=?", (r["id"], g.quote_index)).fetchone()
                status, name = (u["status"], u["name"]) if u else ("missing", None)
                if g.speaker is not None:
                    named_total += 1
                    if name == g.speaker and status in ("explicit", "inferred", "human_verified"):
                        named_ok += 1
                elif status == "explicit":
                    unk_asserted += 1
                if status == "explicit":
                    sys_explicit += 1
                    sys_explicit_ok += int(name == g.speaker)
                if g.weak_only and status == "explicit":
                    weak_asserted += 1
                if name != g.speaker:
                    details.append({"item": item.id, "line": g.line, "issue": "speaker", "gold": g.speaker,
                                    "system": name, "status": status})
        out.update({
            "speaker_accuracy_explicit": _ratio(named_ok, named_total),
            "speaker_explicit_precision": _ratio(sys_explicit_ok, sys_explicit),
            "unknown_asserted_rate": _ratio(unk_asserted, unk_total),
            "weak_evidence_asserted": weak_asserted,
        })
    else:
        out.update({"speaker_accuracy_explicit": None, "speaker_explicit_precision": None,
                    "unknown_asserted_rate": None, "weak_evidence_asserted": None})
    out["details"] = details
    return out
