"""Whether a book is ready to be called done (not whether it exports: an incomplete book can always be exported).

Each check says what it found and where; a book is acceptable only when every check passes. None of this proves the
translation is good — it says the pipeline did everything it promised and nothing failed silently:

* every paragraph to translate has a translation;
* nothing is waiting for review (``needs_review``) and no serious finding is open;
* every fix made after review / proofreading / the reader's look was checked against the source and settled;
* the passes that check the book (proofreading, the reader's look) ran over all of it — a refused or failed stretch
  is not checked — and no review job was blocked or failed on the text that is now current;
* every person's edit is still the text that will be exported (its source unchanged);
* the exported EPUB passed validation (when an export result is given).

A finding a later look outlived (``superseded``: the paragraph was checked again as it is now and the issue was not
found) is closed. A rule conflict (a retranslation that came back unchanged: the finding is ``conflict``, a
``rule_conflict`` beside it) is not an open error either: it is listed under ``conflicts`` for a person to look at.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..translation.cache import latest_texts

FIX_OPEN = ("fix_still_wrong", "fix_regressed", "fix_undecided", "fix_unchecked", "fix_unverified")
NOT_SEEN = ("review_blocked", "proofread_blocked", "reader_blocked")
CONFLICT = "rule_conflict"
SAMPLE = 20


def _check(units: list[str] | set[str], *, detail: str = "") -> dict[str, Any]:
    ids = sorted(units)
    return {"ok": not ids, "count": len(ids), "units": ids[:SAMPLE], **({"detail": detail} if detail else {})}


def is_current(finding: sqlite3.Row, best: dict[str, sqlite3.Row]) -> bool:
    """A finding about the unit's text as it is now (or about no version in particular): a finding about a version that
    is no longer the book's text (and is not a person's) is history, not a problem."""
    t = best.get(finding["unit_id"])
    return finding["translation_id"] is None or (t is not None and t["id"] == finding["translation_id"])


def acceptance(conn: sqlite3.Connection, book_id: str, *, proofread: bool = True, reader: bool = True,
               validation_ok: bool | None = None) -> dict[str, Any]:
    best = latest_texts(conn, book_id)
    units = {r["id"]: r for r in conn.execute("SELECT id, translatable, source_hash FROM units WHERE book_id=?", (book_id,))}
    checks: dict[str, dict[str, Any]] = {}
    checks["translated"] = _check({u for u, r in units.items() if r["translatable"] and u not in best})
    checks["no_needs_review"] = _check({u for u, t in best.items() if t["status"] == "needs_review"})
    open_rows = conn.execute("SELECT unit_id, type, severity, translation_id FROM qa_findings WHERE book_id=? AND "
                             "status='open' AND unit_id IS NOT NULL", (book_id,)).fetchall()
    current = {f for f in open_rows if f["type"] != CONFLICT and is_current(f, best)}
    checks["no_open_serious_findings"] = _check({f["unit_id"] for f in current if f["severity"] in ("high", "error")
                                                 and f["type"] not in FIX_OPEN + NOT_SEEN})
    checks["fixes_checked"] = _check({f["unit_id"] for f in current if f["type"] in FIX_OPEN})
    checks["all_seen"] = _check({f["unit_id"] for f in current if f["type"] in NOT_SEEN}
                                | {r[0] for r in conn.execute(
                                    "SELECT j.target_id FROM jobs j WHERE j.book_id=? AND j.stage='review' AND "
                                    "j.status IN ('blocked','failed') AND j.id=(SELECT MAX(id) FROM jobs k WHERE "
                                    "k.book_id=j.book_id AND k.stage='review' AND k.target_id=j.target_id)", (book_id,))})
    for stage, wanted in (("proofread", proofread), ("reader", reader)):
        if not wanted:
            continue
        last = conn.execute("SELECT status FROM jobs WHERE book_id=? AND stage=? ORDER BY id DESC LIMIT 1",
                            (book_id, stage)).fetchone()
        status = last["status"] if last else "not run"
        checks[f"{stage}_complete"] = {"ok": status == "succeeded", "count": 0 if status == "succeeded" else 1,
                                       "units": [], "detail": status}
    lost = {r["unit_id"] for r in conn.execute(
        "SELECT t.unit_id FROM translations t JOIN units u ON u.id=t.unit_id WHERE u.book_id=? AND t.status='human' AND "
        "t.source_hash!=u.source_hash AND NOT EXISTS (SELECT 1 FROM translations x WHERE x.unit_id=t.unit_id AND "
        "x.status='human' AND x.source_hash=u.source_hash)", (book_id,))}
    checks["human_edits_kept"] = _check(lost, detail="a person's edit whose source paragraph has changed since")
    if validation_ok is not None:
        checks["epub_valid"] = {"ok": validation_ok, "count": 0 if validation_ok else 1, "units": []}
    conflicts = {f["unit_id"] for f in open_rows if f["type"] == CONFLICT and is_current(f, best)}
    return {"acceptable": all(c["ok"] for c in checks.values()), "checks": checks,
            "conflicts": {"count": len(conflicts), "units": sorted(conflicts)[:SAMPLE]},
            "note": "Acceptable means the pipeline finished every step and nothing failed silently; it is not a "
                    "judgement of translation quality."}


def summary(rep: dict[str, Any]) -> str:
    """One line: 「acceptable」 or what is missing."""
    n = rep.get("conflicts", {}).get("count", 0)
    listed = f"; {n} rule conflict(s) listed" if n else ""
    if rep["acceptable"]:
        return f"acceptance: OK (pipeline complete; not a quality judgement){listed}"
    bad = [f"{k}={c.get('detail') or c['count']}" for k, c in rep["checks"].items() if not c["ok"]]
    return "acceptance: NOT YET — " + ", ".join(bad) + listed
