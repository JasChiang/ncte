"""Selective semantic review and minimal retranslation (spec §5.7).

Only units that program QA / speaker analysis flagged are sent to the (more expensive) review
call (``review.automatic_review = flagged_only``), the reviewer returns evidence + issue type +
suggestion only, and units marked ``needs_retranslation`` are retranslated **once** with that
feedback, then re-checked by program QA. At most one review pass per unit per run.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..errors import FatalAPIError, LLMError, ModelOutputError
from ..llm.prompt_loader import load_prompt
from ..llm.schemas import ReviewOutput
from ..memory.db import Transaction, utcnow
from ..qa.rules import worst
from .cache import glossary_hash, latest_translation, relevant_glossary
from .translator import UnitInput, call_structured

SEV = {"low": "low", "medium": "medium", "high": "high"}
REVIEW_BATCH = 20


def flagged_units(conn: sqlite3.Connection, book_id: str, run_id: str | None, unit_ids: list[str]) -> dict[str, list[str]]:
    """unit_id → reasons, for units in scope that need a semantic look (``run_id=None``: findings of any run)."""
    reasons: dict[str, list[str]] = {}
    if not unit_ids:
        return reasons
    ph = ",".join("?" * len(unit_ids))
    run_clause = "run_id=?" if run_id else "book_id=?"
    for r in conn.execute(f"SELECT unit_id, type, evidence FROM qa_findings WHERE {run_clause} AND unit_id IN ({ph}) AND "
                          "status='open' AND (severity IN ('high','error') OR (type LIKE 'review%' AND "
                          "severity IN ('medium','high')))",
                          (run_id or book_id, *unit_ids)):
        # a reviewer's earlier issue that is still open (found but not fixed) is looked at again, with its note
        reasons.setdefault(r["unit_id"], []).append(f"{r['type']}: {(r['evidence'] or '')[:160]}")
    for r in conn.execute(f"SELECT unit_id, notes_json, status FROM utterances WHERE book_id=? AND unit_id IN ({ph})",
                          (book_id, *unit_ids)):
        notes = json.loads(r["notes_json"])
        if any("conflict" in n or "contradicted" in n for n in notes):
            reasons.setdefault(r["unit_id"], []).append("speaker evidence conflict: " + "; ".join(notes)[:160])
    for r in conn.execute(f"SELECT unit_id, flags_json FROM translations WHERE {run_clause} AND unit_id IN ({ph})"
                          if run_id else f"SELECT unit_id, flags_json FROM translations WHERE unit_id IN ({ph})",
                          (run_id, *unit_ids) if run_id else tuple(unit_ids)):
        flags = set(json.loads(r["flags_json"]))
        if flags & {"possible_source_error"}:
            reasons.setdefault(r["unit_id"], []).append("translator flag: possible_source_error")
    return reasons


def review_scope(orch: Any, unit_ids: list[str], *, any_run: bool = False) -> dict[str, int]:
    """Run the review stage for ``unit_ids`` (already translated in this run). Returns stats."""
    cfg = orch.cfg
    stats = {"reviewed": 0, "issues": 0, "retranslated": 0, "fixed": 0, "still_flagged": 0}
    if cfg.review.automatic_review == "off" or cfg.review.maximum_review_passes < 1 or not unit_ids:
        return stats
    conn: sqlite3.Connection = orch.conn
    with orch.lock:
        done = {r[0] for r in conn.execute("SELECT target_id FROM jobs WHERE run_id=? AND stage='review'", (orch.run_id,))}
        ids = [u for u in unit_ids if u not in done]
        if cfg.review.automatic_review == "all":
            reasons = {u: ["full review"] for u in ids}
            reasons.update(flagged_units(conn, orch.book.id, None if any_run else orch.run_id, ids))
        else:
            reasons = flagged_units(conn, orch.book.id, None if any_run else orch.run_id, ids)
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT * FROM units WHERE id IN ({','.join('?' * len(reasons))})", list(reasons))} if reasons else {}
        with Transaction(conn):
            for u in reasons:
                conn.execute("INSERT OR IGNORE INTO jobs(book_id, run_id, stage, target_id, status, updated_at) "
                             "VALUES (?,?,?,?,?,?)", (orch.book.id, orch.run_id, "review", u, "pending", utcnow()))
    targets = [u for u in reasons if u in rows]
    if not targets:
        return stats
    prompt = load_prompt("review")
    model = cfg.openrouter.review_model or orch.model
    for i in range(0, len(targets), REVIEW_BATCH):
        chunk = targets[i:i + REVIEW_BATCH]
        items = []
        with orch.lock:
            for uid in chunk:
                r = rows[uid]
                tr = latest_translation(conn, uid, r["source_hash"], include_human=False)
                if tr is None:
                    continue
                items.append({"unit_id": uid, "source": r["source_text"], "translation": tr["text"],
                              "flags": reasons[uid]})
            if not items:
                continue
            ctx_batch = _fake_batch(orch, [rows[i["unit_id"]] for i in items])
        ctx = orch.batch_context(ctx_batch)
        payload = {"task": "review", "items": items,
                   "context": ctx.get("context_before", []) + ctx.get("context_after", []),
                   "characters": ctx.get("characters", []), "scene": ctx.get("scene", {}),
                   "glossary": ctx.get("glossary", [])}
        for key in ("address_terms", "style"):
            if ctx.get(key):
                payload[key] = ctx[key]
        est = orch._estimate(payload, sum(len(x["source"]) for x in items) // 2)
        orch.check_budget(est)
        try:
            parsed, _rid, _mode = call_structured(orch.client, orch.recorder, stage="review", model=model, prompt=prompt,
                                                  payload=payload, mode=orch.translator.mode, cfg=cfg,
                                                  unit_ids=[x["unit_id"] for x in items], est_cost=est,
                                                  on_downgrade=orch._note)
        except FatalAPIError:
            raise
        except (LLMError, ModelOutputError) as exc:
            orch._finding(None, "review_failed", "medium", f"{len(items)} units: {exc}"[:400])
            _close_jobs(orch, [x["unit_id"] for x in items], "failed")
            continue
        assert isinstance(parsed, ReviewOutput)
        stats["reviewed"] += len(items)
        valid = {x["unit_id"] for x in items}
        to_fix: dict[str, list[dict[str, str]]] = {}
        for issue in parsed.issues:
            if issue.unit_id not in valid or not issue.evidence.strip():
                orch._finding(None, "review_rejected_issue", "low", json.dumps(issue.model_dump(), ensure_ascii=False)[:300])
                continue
            stats["issues"] += 1
            orch._finding(issue.unit_id, f"review:{issue.issue_type}", SEV[issue.severity],
                          f"{issue.evidence} → {issue.suggestion}"[:600])
            # a concrete fix is acted on even when the reviewer called it "style" (a leftover ッ stayed otherwise);
            # the new translation goes through the program checks again
            if issue.severity in ("medium", "high") and (issue.needs_retranslation or issue.suggestion.strip()):
                to_fix.setdefault(issue.unit_id, []).append(
                    {"issue_type": issue.issue_type, "evidence": issue.evidence, "suggestion": issue.suggestion})
        # what the program already knows is wrong (a leftover ッ) is fixed whatever severity the reviewer gave it
        with orch.lock:
            for r in conn.execute(f"SELECT unit_id, type, evidence FROM qa_findings WHERE book_id=? AND status='open' "
                                  f"AND severity='error' AND unit_id IN ({','.join('?' * len(valid))})",
                                  (orch.book.id, *valid)):
                to_fix.setdefault(r["unit_id"], []).append(
                    {"issue_type": r["type"], "evidence": r["evidence"] or "", "suggestion": r["evidence"] or ""})
        if to_fix:
            _retranslate_with_feedback(orch, [rows[u] for u in to_fix], to_fix, stats)
        flagged_by_reviewer = {i.unit_id for i in parsed.issues if i.unit_id in valid and i.severity != "low"}
        _clear_confirmed(orch, [x["unit_id"] for x in items if x["unit_id"] not in flagged_by_reviewer], stats)
        _close_jobs(orch, [x["unit_id"] for x in items], "succeeded")
    return stats


def _clear_confirmed(orch: Any, unit_ids: list[str], stats: dict[str, int]) -> None:
    """The reviewer saw the flagged unit and reported no (medium/high) issue: clear the program flag (ADR-018).

    The finding stays in the report with status ``cleared_by_review``."""
    if not unit_ids:
        return
    with orch.lock, Transaction(orch.conn):
        for uid in unit_ids:
            # the program's flag, and an earlier reviewer's issue (another look found nothing: §1024 「レイドは何をした
            # の？」 asked to his face was called an "added 你" once, fine the second time)
            n = orch.conn.execute("UPDATE qa_findings SET status='cleared_by_review' WHERE book_id=? AND unit_id=? AND "
                                  "status='open' AND ((severity='high' AND type NOT LIKE 'review%') OR "
                                  "(type LIKE 'review%' AND run_id IS NOT ?))",
                                  (orch.book.id, uid, orch.run_id)).rowcount
            t = orch.conn.execute("SELECT id FROM translations WHERE unit_id=? AND status='needs_review' ORDER BY id "
                                  "DESC LIMIT 1", (uid,)).fetchone()
            if t is not None:
                orch.conn.execute("UPDATE translations SET status='succeeded' WHERE id=?", (t[0],))
                orch.conn.execute("UPDATE jobs SET status='succeeded' WHERE run_id=? AND stage='translate' AND "
                                  "target_id=? AND status='needs_review'", (orch.run_id, uid))
            if n or t is not None:
                stats["cleared"] = stats.get("cleared", 0) + 1


def _fake_batch(orch: Any, rows: list[sqlite3.Row]) -> Any:
    from .orchestrator import Batch

    scene_ids = {r["scene_id"] for r in rows}
    return Batch(rows, False, next(iter(scene_ids)) if len(scene_ids) == 1 and orch.opts.mode == "context" else None)


def _close_jobs(orch: Any, unit_ids: list[str], status: str) -> None:
    with orch.lock:
        orch.conn.executemany("UPDATE jobs SET status=?, updated_at=? WHERE run_id=? AND stage='review' AND target_id=?",
                              [(status, utcnow(), orch.run_id, u) for u in unit_ids])


def _retranslate_with_feedback(orch: Any, rows: list[sqlite3.Row], feedback: dict[str, list[dict[str, str]]],
                               stats: dict[str, int]) -> None:
    units = [UnitInput.from_row(r) for r in rows]
    ctx = orch.batch_context(_fake_batch(orch, rows))
    ctx["review_feedback"] = [{"unit_id": u, "issues": v} for u, v in feedback.items()]
    stats["retranslated"] += len(units)
    try:
        res = orch.translator.translate(units, ctx, qa_glossary=orch.qa_glossary())
    except FatalAPIError:
        raise
    except LLMError as exc:
        orch._finding(None, "review_retranslation_failed", "medium", str(exc)[:300])
        return
    by_id = {r["id"]: r for r in rows}
    with orch.lock, Transaction(orch.conn):
        for uid, ur in res.ok.items():
            r = by_id[uid]
            sev = worst(ur.findings)
            status = "needs_review" if sev == "high" else "succeeded"
            orch.conn.execute(
                "INSERT INTO translations(unit_id, target_lang, text, source_hash, prompt_version, model_id, "
                "glossary_version, mode, status, run_id, request_id, flags_json, notes_json, created_at, translator_note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, orch.cfg.app.language, ur.text, r["source_hash"], orch.prompt.version_id, orch.model,
                 glossary_hash(relevant_glossary(orch.glossary, r["plain_text"])), orch.opts.mode, status,
                 orch.run_id, ur.request_id, json.dumps(ur.flags + ["review_retranslation"], ensure_ascii=False),
                 json.dumps(ur.notes, ensure_ascii=False), utcnow(), ur.translator_note or None))
            if status == "succeeded":
                stats["fixed"] += 1
                # what was found on the old text (by any run, the reviewer's own issue included) is settled by the
                # new translation; anything the new one still has is recorded below as after_review
                orch.conn.execute("UPDATE qa_findings SET status='resolved' WHERE book_id=? AND unit_id=? AND "
                                  "status='open' AND (severity IN ('high','error') OR type LIKE 'review%')",
                                  (orch.book.id, uid))
                orch.conn.execute("UPDATE jobs SET status='succeeded' WHERE run_id=? AND stage='translate' AND "
                                  "target_id=?", (orch.run_id, uid))
            else:
                stats["still_flagged"] += 1
            for f in ur.findings:
                if f.severity in ("high", "medium"):
                    orch._finding(uid, f"after_review:{f.type}", f.severity, f.evidence)
        for uid in res.failed:
            stats["still_flagged"] += 1
            orch._finding(uid, "review_retranslation_failed", "high", res.failed[uid][:300])
