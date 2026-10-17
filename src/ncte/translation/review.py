"""Selective semantic review and minimal retranslation (spec §5.7).

Only units that program QA / speaker analysis flagged are sent to the (more expensive) review
call (``review.automatic_review = flagged_only``), the reviewer returns evidence + issue type +
suggestion only, and units marked ``needs_retranslation`` are retranslated **once** with that
feedback, then re-checked by program QA. At most one review pass per unit per run.

Passing program QA does not make a fix a fix: a retranslation that says the same wrong thing is still wrong. Each
fix (here, after proofreading and after the reader's look) is checked once against the source and the issue it was
meant to fix (``fix_check``): fixed, the issue was not an error, still wrong, or undecided. Only the first two settle
the issue; the others leave it open with the new text marked ``needs_review``. A unit the content filter refused, or
whose request failed, was not reviewed: it is counted as blocked / failed and nothing found on it is cleared.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..analysis.split_read import call_in_parts
from ..errors import FatalAPIError, LLMError, ModelOutputError
from ..llm.prompt_loader import load_prompt
from ..llm.schemas import FixCheck, FixCheckOutput, ReviewOutput
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
    stats = new_stats(actually_reviewed=0, blocked=0, failed=0, skipped=0)
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
    model = orch.model_for("review")
    for i in range(0, len(targets), REVIEW_BATCH):
        chunk = targets[i:i + REVIEW_BATCH]
        items = []
        tids: dict[str, int] = {}  # the version each unit is reviewed in
        with orch.lock:
            for uid in chunk:
                r = rows[uid]
                tr = latest_translation(conn, uid, r["source_hash"], include_human=False)
                if tr is None:
                    stats["skipped"] += 1
                    continue
                tids[uid] = tr["id"]
                items.append({"unit_id": uid, "source": r["source_text"], "translation": tr["text"],
                              "flags": reasons[uid]})
        _close_jobs(orch, [u for u in chunk if u not in tids], "skipped")
        if not items:
            continue
        with orch.lock:
            ctx_batch = _fake_batch(orch, [rows[i["unit_id"]] for i in items])
        ctx = orch.batch_context(ctx_batch)
        with orch.lock:
            around = _surroundings(conn, orch.book.id, [rows[i["unit_id"]] for i in items])
            for it in items:
                it["speakers"] = _speakers(conn, it["unit_id"])
        payload = {"task": "review", "items": items, "context": around,
                   "characters": ctx.get("characters", []), "scene": ctx.get("scene", {}),
                   "glossary": ctx.get("glossary", [])}
        for key in ("address_terms", "style"):
            if ctx.get(key):
                payload[key] = ctx[key]
        est = orch._estimate(payload, sum(len(x["source"]) for x in items) // 2, "review")
        orch.check_budget(est)
        def ask(pl: dict[str, Any], est: float = est) -> Any:
            return call_structured(orch.client, orch.recorder, stage="review", model=model, prompt=prompt, payload=pl,
                                   mode=orch.mode_for(model), cfg=cfg, unit_ids=[x["unit_id"] for x in pl["items"]],
                                   est_cost=est, on_downgrade=orch._note)[0]

        blocked: list[str] = []
        try:  # a content filter refusing the batch: reviewed in halves (split_read.call_in_parts)
            parsed = call_in_parts(ask, payload, "items", blocked, min_part=1)
        except FatalAPIError:
            raise
        except (LLMError, ModelOutputError) as exc:
            orch._finding(None, "review_failed", "medium", f"{len(items)} units: {exc}"[:400])
            stats["failed"] += len(items)
            _close_jobs(orch, [x["unit_id"] for x in items], "failed")
            continue
        refused = set(blocked) & set(tids)
        for uid in refused:
            orch._finding(uid, "review_blocked", "medium", "the content filter refused this passage in review",
                          translation_id=tids[uid])
        stats["blocked"] += len(refused)
        _close_jobs(orch, sorted(refused), "blocked")  # not reviewed: nothing found on it is cleared
        if parsed is None:
            continue
        assert isinstance(parsed, ReviewOutput)
        valid = {x["unit_id"] for x in items} - refused  # the units the reviewer really saw
        stats["actually_reviewed"] += len(valid)
        to_fix: dict[str, list[dict[str, Any]]] = {}
        for issue in parsed.issues:
            if issue.unit_id not in valid or not issue.evidence.strip():
                orch._finding(None, "review_rejected_issue", "low", json.dumps(issue.model_dump(), ensure_ascii=False)[:300])
                continue
            stats["issues"] += 1
            fid = orch._finding(issue.unit_id, f"review:{issue.issue_type}", SEV[issue.severity],
                                f"{issue.evidence} → {issue.suggestion}"[:600], translation_id=tids[issue.unit_id])
            # a concrete fix is acted on even when the reviewer called it "style" (a leftover ッ stayed otherwise);
            # the new translation goes through the program checks again
            if issue.severity in ("medium", "high") and (issue.needs_retranslation or issue.suggestion.strip()):
                to_fix.setdefault(issue.unit_id, []).append(
                    {"issue_type": issue.issue_type, "evidence": issue.evidence, "suggestion": issue.suggestion,
                     "finding_id": fid})
        # what the program already knows is wrong (a leftover ッ) is fixed whatever severity the reviewer gave it
        with orch.lock:
            for r in conn.execute(f"SELECT id, unit_id, type, evidence FROM qa_findings WHERE book_id=? AND status='open' "
                                  f"AND severity='error' AND unit_id IN ({','.join('?' * len(valid))})",
                                  (orch.book.id, *valid)) if valid else []:
                to_fix.setdefault(r["unit_id"], []).append(
                    {"issue_type": r["type"], "evidence": r["evidence"] or "", "suggestion": r["evidence"] or "",
                     "finding_id": r["id"]})
        if to_fix:
            _retranslate_with_feedback(orch, [rows[u] for u in to_fix], to_fix, stats)
        flagged_by_reviewer = {i.unit_id for i in parsed.issues if i.unit_id in valid and i.severity != "low"}
        _clear_confirmed(orch, {u: tids[u] for u in valid if u not in flagged_by_reviewer and u not in to_fix}, stats)
        _close_jobs(orch, sorted(valid), "succeeded")
    return stats


def new_stats(**extra: int) -> dict[str, int]:
    """Counts every pass that sends units back to the translator keeps (review, proofreading, the reader's look).

    ``resolved``: issues settled by a checked fix (fixed, or shown not to be an error); ``pending``: units whose issue
    is still open after this pass (still wrong, undecided, unverified, failed, or a person's text left alone)."""
    return {**extra, "issues": 0, "retranslated": 0, "fixed": 0, "issue_invalid": 0, "resolved": 0, "pending": 0,
            "still_flagged": 0}


REVIEW_CONTEXT = 5  # paragraphs on each side: who is talking to whom is often two or three lines away


def _speakers(conn: sqlite3.Connection, unit_id: str) -> list[str]:
    """Who says each quote of the unit, as the read pass judged it (an inference is marked so)."""
    return [(r["n"] or "？") + ("" if r["status"] in ("explicit", "human_verified") else "（推測）")
            for r in conn.execute("SELECT c.canonical_name n, u.status FROM utterances u LEFT JOIN characters c ON "
                                  "c.id=u.speaker_id_nullable WHERE u.unit_id=? ORDER BY u.quote_index", (unit_id,))]


def _surroundings(conn: sqlite3.Connection, book_id: str, rows: list[sqlite3.Row],
                  reach: int = REVIEW_CONTEXT) -> list[dict[str, Any]]:
    """The paragraphs around the reviewed ones (source, current translation, speakers), within reach of each."""
    wanted = {r["ordinal"] for r in rows}
    near = sorted({o for r in rows for o in range(r["ordinal"] - reach, r["ordinal"] + reach + 1)} - wanted)
    out = []
    for r in conn.execute(f"SELECT * FROM units WHERE book_id=? AND translatable=1 AND kind IN ('text','heading') AND "
                          f"ordinal IN ({','.join('?' * len(near))}) ORDER BY ordinal", (book_id, *near)) if near else []:
        tr = latest_translation(conn, r["id"], r["source_hash"])
        item: dict[str, Any] = {"source": r["source_text"]}
        if tr is not None:
            item["translation"] = tr["text"]
        sp = _speakers(conn, r["id"])
        if sp:
            item["speakers"] = sp
        out.append(item)
    return out


def _clear_confirmed(orch: Any, reviewed: dict[str, int], stats: dict[str, int]) -> None:
    """The reviewer saw the flagged unit (in translation ``reviewed[unit]``) and reported no (medium/high) issue: clear
    the program flag (ADR-018). A finding about a later version of the unit is not touched.

    The finding stays in the report with status ``cleared_by_review``."""
    if not reviewed:
        return
    with orch.lock, Transaction(orch.conn):
        for uid, tid in reviewed.items():
            # the program's flag, and an earlier reviewer's issue (another look found nothing: §1024 「カイルは何をした
            # の？」 asked to his face was called an "added 你" once, fine the second time)
            n = orch.conn.execute("UPDATE qa_findings SET status='cleared_by_review' WHERE book_id=? AND unit_id=? AND "
                                  "status='open' AND ((severity='high' AND type NOT LIKE 'review%') OR "
                                  "(type LIKE 'review%' AND run_id IS NOT ?)) AND "
                                  "(translation_id IS NULL OR translation_id<=?)",
                                  (orch.book.id, uid, orch.run_id, tid)).rowcount
            t = orch.conn.execute("SELECT id FROM translations WHERE id=? AND status='needs_review'", (tid,)).fetchone()
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


def _retranslate_with_feedback(orch: Any, rows: list[sqlite3.Row], feedback: dict[str, list[dict[str, Any]]],
                               stats: dict[str, int]) -> None:
    """Send units back to the translator with what was found (and the current text, to change only what is wrong);
    then check each fix against the source (``fix_check``). ``feedback[unit]`` items may carry the ``finding_id`` they
    come from: only those findings are settled, and only by a fix the check accepts."""
    conn = orch.conn
    with orch.lock:
        current = {r["id"]: latest_translation(conn, r["id"], r["source_hash"]) for r in rows}
    todo = []
    for r in rows:
        cur = current[r["id"]]
        if cur is not None and cur["status"] == "human":
            # a person's text is never replaced by a machine fix; what was found stays open for them
            stats["kept_human"] = stats.get("kept_human", 0) + 1
            stats["pending"] += 1
            continue
        todo.append(r)
    if not todo:
        return
    units = [UnitInput.from_row(r) for r in todo]
    before = {u: (t["text"] if t is not None else "") for u, t in current.items()}
    ctx = orch.batch_context(_fake_batch(orch, todo))
    ctx["review_feedback"] = [
        {"unit_id": r["id"], **({"current_translation": before[r["id"]]} if before[r["id"]] else {}),
         "issues": [{k: v for k, v in i.items() if k in ("issue_type", "evidence", "suggestion")} for i in feedback[r["id"]]]}
        for r in todo]
    stats["retranslated"] += len(units)
    try:
        res = orch.translator.translate(units, ctx, qa_glossary=orch.qa_glossary())
    except FatalAPIError:
        raise
    except LLMError as exc:
        orch._finding(None, "review_retranslation_failed", "medium", str(exc)[:300])
        stats["pending"] += len(units)
        stats["still_flagged"] += len(units)
        return
    by_id = {r["id"]: r for r in todo}
    new_tid: dict[str, int] = {}
    with orch.lock, Transaction(orch.conn):
        for uid, ur in res.ok.items():
            r = by_id[uid]
            # not the book's text until the fix is checked
            new_tid[uid] = int(orch.conn.execute(
                "INSERT INTO translations(unit_id, target_lang, text, source_hash, prompt_version, model_id, "
                "glossary_version, mode, status, run_id, request_id, flags_json, notes_json, created_at, translator_note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, orch.cfg.app.language, ur.text, r["source_hash"], orch.prompt.version_id, orch.model,
                 glossary_hash(relevant_glossary(orch.glossary, r["plain_text"])), orch.opts.mode, "needs_review",
                 orch.run_id, ur.request_id, json.dumps(ur.flags + ["review_retranslation"], ensure_ascii=False),
                 json.dumps(ur.notes, ensure_ascii=False), utcnow(), ur.translator_note or None)).lastrowid)
        for uid in res.failed:
            stats["still_flagged"] += 1
            stats["pending"] += 1
            orch._finding(uid, "review_retranslation_failed", "high", res.failed[uid][:300])
    checks = _check_fixes(orch, [(by_id[u], before[u], res.ok[u].text, feedback[u])
                                 for u in res.ok]) if orch.cfg.review.fix_check else {}
    with orch.lock, Transaction(orch.conn):
        for uid, ur in res.ok.items():
            tid = new_tid[uid]
            fids = [i["finding_id"] for i in feedback[uid] if i.get("finding_id")]
            outcome = fix_outcome(checks.get(uid), program_high=worst(ur.findings) == "high",
                                  checked=orch.cfg.review.fix_check)
            if outcome in ("fixed", "issue_invalid"):
                orch.conn.execute("UPDATE translations SET status='succeeded' WHERE id=?", (tid,))
                orch.conn.executemany(
                    "UPDATE qa_findings SET status=? WHERE id=? AND status='open'",
                    [("resolved" if outcome == "fixed" else "dismissed_by_check", f) for f in fids])
                orch.conn.execute("UPDATE jobs SET status='succeeded' WHERE run_id=? AND stage='translate' AND "
                                  "target_id=?", (orch.run_id, uid))
                stats[outcome] += 1
                stats["resolved"] += 1
            else:
                if outcome == "regressed":  # the fix broke something else: the old text stays the book's
                    orch.conn.execute("UPDATE translations SET status='rejected_fix' WHERE id=?", (tid,))
                stats["still_flagged"] += 1
                stats["pending"] += 1
                c = checks.get(uid)
                orch._finding(uid, f"fix_{outcome}", "high" if outcome in ("still_wrong", "regressed") else "medium",
                              (f"{c.reason} {c.new_error}".strip() if c else "the fix was not checked against the source")
                              [:400], translation_id=tid)
            for f in ur.findings:
                if f.severity in ("high", "medium"):
                    orch._finding(uid, f"after_review:{f.type}", f.severity, f.evidence, translation_id=tid)


def fix_outcome(check: FixCheck | None, *, program_high: bool, checked: bool = True) -> str:
    """What a fix settles: ``fixed`` / ``issue_invalid`` settle the issue; ``still_wrong``, ``undecided``,
    ``regressed`` (a new error), ``unchecked`` (the check failed or gave no answer) and ``unverified`` (the check is
    off) leave it open."""
    if check is None:
        return "unchecked" if checked else "unverified"
    if check.new_error.strip():
        return "regressed"
    if program_high and check.verdict in ("fixed", "issue_invalid"):
        return "still_wrong"  # the program still finds a serious problem in the new text
    return check.verdict


FIX_CHECK_BATCH = 20


def _check_fixes(orch: Any, fixes: list[tuple[sqlite3.Row, str, str, list[dict[str, Any]]]]) -> dict[str, FixCheck]:
    """One look per fix, against the source: did the new text fix what was found? Units the check could not answer
    for (refused, failed, left out) are missing from the result and stay open."""
    out: dict[str, FixCheck] = {}
    if not fixes:
        return out
    prompt = load_prompt("fix_check")
    conn: sqlite3.Connection = orch.conn
    for i in range(0, len(fixes), FIX_CHECK_BATCH):
        chunk = fixes[i:i + FIX_CHECK_BATCH]
        with orch.lock:
            items = [{"unit_id": r["id"], "source": r["source_text"], "before": before, "after": after,
                      "issues": [f"{x.get('issue_type', '')}: {x.get('evidence', '')} → {x.get('suggestion', '')}"[:500]
                                 for x in issues],
                      "around": [a["source"] for a in _surroundings(conn, orch.book.id, [r], reach=2)]}
                     for r, before, after, issues in chunk]
        payload = {"task": "fix_check", "items": items}
        est = orch._estimate(payload, 200 * len(items), "fix_check")
        orch.check_budget(est)  # out of budget: the run stops; the new texts stay needs_review and the issues open

        def ask(pl: dict[str, Any], est: float = est) -> Any:
            return call_structured(orch.client, orch.recorder, stage="fix_check", model=orch.model_for("fix_check"),
                                   prompt=prompt, payload=pl, mode=orch.mode_for(orch.model_for("fix_check")), cfg=orch.cfg,
                                   unit_ids=[x["unit_id"] for x in pl["items"]], est_cost=est, on_downgrade=orch._note)[0]

        blocked: list[str] = []
        try:
            parsed = call_in_parts(ask, payload, "items", blocked, min_part=1)
        except FatalAPIError:
            raise
        except (LLMError, ModelOutputError) as exc:
            orch._finding(None, "fix_check_failed", "medium", f"{len(items)} units: {exc}"[:400])
            continue
        if parsed is None:
            continue
        assert isinstance(parsed, FixCheckOutput)
        asked = {x["unit_id"] for x in items} - set(blocked)
        for c in parsed.checks:
            if c.unit_id in asked and c.unit_id not in out:
                out[c.unit_id] = c
    return out
