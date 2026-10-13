"""QA findings / review queue aggregation."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..translation.cache import latest_texts


def qa_report(conn: sqlite3.Connection, *, book_id: str, run_id: str | None = None, limit: int = 500) -> dict[str, Any]:
    params: list[Any] = [book_id]
    q = "SELECT * FROM qa_findings WHERE book_id=?"
    if run_id:
        q += " AND run_id=?"
        params.append(run_id)
    rows = conn.execute(q + " ORDER BY id", params).fetchall()
    by_type: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    for r in rows:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        by_sev[r["severity"]] = by_sev.get(r["severity"], 0) + 1
    units = {r["id"]: r for r in conn.execute("SELECT * FROM units WHERE book_id=?", (book_id,))}
    best = latest_texts(conn, book_id)
    items = []
    for r in rows[:limit]:
        u = units.get(r["unit_id"]) if r["unit_id"] else None
        t = best.get(r["unit_id"]) if r["unit_id"] else None
        items.append({"id": r["id"], "unit_id": r["unit_id"], "type": r["type"], "severity": r["severity"],
                      "status": r["status"], "evidence": r["evidence"],
                      "source": u["plain_text"] if u else None, "translation": t["text"] if t else None})
    review_queue = [{"unit_id": uid, "source": units[uid]["plain_text"], "translation": t["text"]}
                    for uid, t in best.items() if t["status"] == "needs_review"]
    translatable = [u for u in units.values() if u["translatable"]]
    untranslated = [{"unit_id": u["id"], "source": u["plain_text"][:120]} for u in translatable if u["id"] not in best]
    unsafe = [{"unit_id": u["id"], "reason": u["skip_reason"], "source": u["plain_text"][:120]}
              for u in units.values() if not u["translatable"] and u["skip_reason"] != "no_letters"]
    return {"findings_total": len(rows), "by_type": by_type, "by_severity": by_sev, "findings": items,
            "needs_review": review_queue, "untranslated": untranslated, "unsafe_units": unsafe}


def book_status(conn: sqlite3.Connection, book_id: str) -> dict[str, Any]:
    units = conn.execute("SELECT kind, translatable, COUNT(*) n FROM units WHERE book_id=? GROUP BY kind, translatable",
                         (book_id,)).fetchall()
    best = latest_texts(conn, book_id)
    statuses: dict[str, int] = {}
    for t in best.values():
        statuses[t["status"]] = statuses.get(t["status"], 0) + 1
    translatable = sum(r["n"] for r in units if r["translatable"])
    runs = [dict(r) for r in conn.execute(
        "SELECT r.id, r.mode, r.model, r.status, r.started_at, r.finished_at, r.budget_usd, "
        "(SELECT COUNT(*) FROM jobs j WHERE j.run_id=r.id AND j.stage='translate' AND j.status='pending') AS pending, "
        "(SELECT COUNT(*) FROM jobs j WHERE j.run_id=r.id AND j.stage='translate' AND j.status='failed') AS failed, "
        "(SELECT COALESCE(SUM(COALESCE(q.cost_usd, CASE WHEN q.cost_status IN ('unknown','pending') "
        "THEN q.est_cost_usd ELSE 0 END)),0) FROM llm_requests q WHERE q.run_id=r.id) AS cost_usd "
        "FROM runs r WHERE r.book_id=? ORDER BY r.started_at", (book_id,))]
    return {"units_by_kind": {f"{r['kind']}{'' if r['translatable'] else ' (kept original)'}": r["n"] for r in units},
            "translatable": translatable, "translated": len(best), "remaining": translatable - len(best),
            "translation_status": statuses, "runs": runs,
            "scenes": conn.execute("SELECT COUNT(*) FROM scenes WHERE book_id=?", (book_id,)).fetchone()[0]}
