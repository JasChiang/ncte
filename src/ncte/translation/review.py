"""Selective semantic review and minimal retranslation (spec §5.7).

Only units that program QA / speaker analysis flagged are sent to the (more expensive) review
call (``review.automatic_review = flagged_only``), the reviewer returns evidence + issue type +
suggestion only, and units marked ``needs_retranslation`` are retranslated **once** with that
feedback, then re-checked by program QA. At most one review pass per unit per run.

Passing program QA does not make a fix a fix: a retranslation that says the same wrong thing is still wrong. Each
fix (here, after proofreading and after the reader's look) is checked once against the source and the issue it was
meant to fix (``fix_check``): fixed, the issue was not an error, still wrong, or undecided. Only the first two settle
the issue (when it was not an error, the old text stays and the new one is not used); the others leave it open with the new text marked ``needs_review``. A unit the content filter refused, or
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
from ..qa.rules import CONFIRM_RULES, CONFIRMED, Finding, worst
from .cache import glossary_hash, latest_translation, relevant_glossary, settle_translation
from .translator import BatchResult, UnitInput, call_structured

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
        # done in this run: finished one way or another (a review still pending or running when the run stopped is
        # done when it resumes)
        done = {r[0] for r in conn.execute("SELECT target_id FROM jobs WHERE run_id=? AND stage='review' AND status NOT IN "
                                           "('pending','running')", (orch.run_id,))}
        ids = [u for u in unit_ids if u not in done]
        if cfg.review.automatic_review == "all":
            from ..epub.matter import story_hrefs

            story = story_hrefs(conn, orch.book.id)  # a table of contents has nothing to review line by line
            href = {r[0]: r[1] for r in conn.execute(
                f"SELECT id, resource_href FROM units WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
            reasons = {u: ["full review"] for u in ids if not story or href.get(u) in story}
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
    # one scene per request: the reviewer, and the fixes it asks for, get that scene's people and who speaks to whom
    ordered = scene_groups(sorted((rows[u] for u in targets), key=lambda r: r["ordinal"]))
    chunks = [[r["id"] for r in part] for g in ordered for part in even_parts(g, REVIEW_BATCH)]
    for chunk in chunks:
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
        ctx = _scene_context(orch, ctx_batch, {i["unit_id"] for i in items})
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
        # what is already known to be wrong — a leftover ッ, or a word rule's find the model confirmed with context
        # (「我們」 in third-person narration) — is fixed whatever the reviewer gave it, or found instead
        with orch.lock:
            for r in conn.execute(f"SELECT id, unit_id, type, evidence FROM qa_findings WHERE book_id=? AND status='open' "
                                  f"AND (severity='error' OR (severity='high' AND instr(evidence, ?) > 0)) "
                                  f"AND unit_id IN ({','.join('?' * len(valid))})",
                                  (orch.book.id, CONFIRMED, *valid)) if valid else []:
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
    from ..memory.visibility import visible_names

    unit = conn.execute("SELECT book_id, ordinal FROM units WHERE id=?", (unit_id,)).fetchone()
    names = visible_names(conn, unit["book_id"], unit["ordinal"]) if unit else {}  # a name revealed later stays out
    return [(names.get(r["sp"]) or "？" if r["sp"] else "？") + ("" if r["status"] in ("explicit", "human_verified")
                                                               else "（推測）")
            for r in conn.execute("SELECT speaker_id_nullable sp, status FROM utterances WHERE unit_id=? ORDER BY "
                                  "quote_index", (unit_id,))]


def _people(conn: sqlite3.Connection, unit_id: str) -> set[str]:
    """Who speaks a line of the unit and who it is said to."""
    out: set[str] = set()
    for r in conn.execute("SELECT speaker_id_nullable, addressee_ids_json FROM utterances WHERE unit_id=?", (unit_id,)):
        out |= {r[0]} if r[0] else set()
        out |= set(json.loads(r[1] or "[]"))
    return out


def _lines(conn: sqlite3.Connection, unit_id: str) -> list[str]:
    """Each line of the unit as read: 「speaker → listener」, an inference marked （推測）."""
    from ..memory.visibility import visible_names

    rows = conn.execute("SELECT u.speaker_id_nullable sp, u.status, u.addressee_ids_json to_, u.addressee_status ts "
                        "FROM utterances u "
                        "WHERE u.unit_id=? ORDER BY u.quote_index", (unit_id,)).fetchall()
    if not rows:
        return []
    unit = conn.execute("SELECT book_id, scene_id, ordinal FROM units WHERE id=?", (unit_id,)).fetchone()
    # named as known at the end of the scene, as the scene was translated (a name may come a line after the quote)
    end = conn.execute("SELECT MAX(ordinal) FROM units WHERE book_id=? AND scene_id=?",
                       (unit["book_id"], unit["scene_id"])).fetchone()[0] if unit["scene_id"] else None
    names = visible_names(conn, unit["book_id"], end or unit["ordinal"])
    out = []
    for r in rows:
        who = names.get(r["sp"], "？") if r["sp"] else "？"
        to = "、".join(names.get(x, "？") for x in json.loads(r["to_"] or "[]"))
        guess = "" if r["status"] in ("explicit", "human_verified") else "（推測）"
        out.append(f"{who}{guess}" + (f" → {to}" + ("" if r["ts"] == "explicit" else "（推測）") if to else ""))
    return out


def _surroundings(conn: sqlite3.Connection, book_id: str, rows: list[sqlite3.Row],
                  reach: int = REVIEW_CONTEXT) -> list[dict[str, Any]]:
    """The paragraphs around the reviewed ones (source, current translation, speakers), within reach of each, each
    saying where it is: ``before`` the first reviewed paragraph, ``after`` the last, or ``between`` two of them."""
    wanted = {r["ordinal"] for r in rows}
    lo, hi = min(wanted, default=0), max(wanted, default=0)
    near = sorted({o for r in rows for o in range(r["ordinal"] - reach, r["ordinal"] + reach + 1)} - wanted)
    out = []
    for r in conn.execute(f"SELECT * FROM units WHERE book_id=? AND translatable=1 AND kind IN ('text','heading') AND "
                          f"ordinal IN ({','.join('?' * len(near))}) ORDER BY ordinal", (book_id, *near)) if near else []:
        tr = latest_translation(conn, r["id"], r["source_hash"])
        item: dict[str, Any] = {"at": "before" if r["ordinal"] < lo else "after" if r["ordinal"] > hi else "between",
                                "source": r["source_text"]}
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
            settled = settle_translation(orch.conn, tid)
            if n or settled:
                stats["cleared"] = stats.get("cleared", 0) + 1


def _fake_batch(orch: Any, rows: list[sqlite3.Row], *, whole_scene: bool = False) -> Any:
    from .orchestrator import Batch

    scene_ids = {r["scene_id"] for r in rows}
    return Batch(rows, False, next(iter(scene_ids)) if len(scene_ids) == 1 and orch.opts.mode == "context" else None,
                 whole_scene)


def even_parts(rows: list[Any], size: int) -> list[list[Any]]:
    """``rows`` in parts of at most ``size``, as even as possible: 43 → 15/14/14, not 20/20/3 (a part of three at the
    end of a scene has almost nothing of the scene around it)."""
    n = -(-len(rows) // size) if rows else 0
    return [rows[len(rows) * k // n:len(rows) * (k + 1) // n] for k in range(n)]


def _scene_context(orch: Any, batch: Any, unit_ids: set[str]) -> dict[str, Any]:
    """What a reviewer of ``unit_ids`` knows: the people of their whole scene, as the translator had them (a woman
    named a paragraph after the part reviewed was missing, and the 妳 said to her looked wrong); the lines and
    actions are the reviewed units' own."""
    if batch.scene_id is None:
        return orch.batch_context(batch)  # type: ignore[no-any-return]
    from .orchestrator import Batch

    with orch.lock:
        scene = orch.conn.execute(
            "SELECT * FROM units WHERE book_id=? AND scene_id=? AND translatable=1 AND kind IN ('text','heading') "
            "ORDER BY ordinal", (orch.book.id, batch.scene_id)).fetchall()
    ctx: dict[str, Any] = orch.batch_context(Batch(scene or batch.rows, False, batch.scene_id))
    sc = ctx.get("scene") or {}
    for key in ("utterances", "omitted_arguments", "uncertainties"):
        if key in sc:
            sc[key] = [x for x in sc[key] if x.get("unit_id") in unit_ids or set(x.get("unit_ids", [])) & unit_ids]
    return ctx


def scene_groups(rows: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """``rows`` (in story order) split where the scene changes: a request about one scene gets that scene's memory
    (characters, who speaks to whom, address table); one spanning two gets none."""
    out: list[list[sqlite3.Row]] = []
    for r in rows:
        if out and out[-1][-1]["scene_id"] == r["scene_id"]:
            out[-1].append(r)
        else:
            out.append([r])
    return out


def fix_groups(conn: sqlite3.Connection, book_id: str, rows: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Units to retranslate, one group per scene and run of adjacent paragraphs: the context window sits around a
    group, so two fixes far apart in a scene are sent apart (together, the paragraphs between them were neither
    translated nor shown)."""
    rows = sorted(rows, key=lambda r: r["ordinal"])
    if not rows:
        return []
    ords = [o for (o,) in conn.execute(
        "SELECT ordinal FROM units WHERE book_id=? AND translatable=1 AND kind IN ('text','heading') AND ordinal "
        "BETWEEN ? AND ? ORDER BY ordinal", (book_id, rows[0]["ordinal"], rows[-1]["ordinal"]))]
    pos = {o: i for i, o in enumerate(ords)}
    out: list[list[sqlite3.Row]] = []
    for group in scene_groups(rows):
        run = [group[0]]
        for r in group[1:]:
            at = pos.get(r["ordinal"])
            if at is not None and at == pos.get(run[-1]["ordinal"], -2) + 1:
                run.append(r)
            else:
                out.append(run)
                run = [r]
        out.append(run)
    return out


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
    from ..exporter import exported_text

    # what the book says now, as exported (the text and a translator's note after it)
    before = {u: (exported_text(t["text"], t["translator_note"], t["status"]) if t is not None else "")
              for u, t in current.items()}
    stats["retranslated"] += len(todo)
    res = BatchResult()
    # the fix sees as much of its scene as the first translation did (the scene around it, up to
    # targeted_scene_context_chars a side), not only the two paragraphs before and one after
    for group in fix_groups(conn, orch.book.id, todo):
        ctx = orch.batch_context(_fake_batch(orch, group, whole_scene=True))
        ctx["review_feedback"] = [
            {"unit_id": r["id"], **({"current_translation": before[r["id"]]} if before[r["id"]] else {}),
             "issues": [{k: v for k, v in i.items() if k in ("issue_type", "evidence", "suggestion")}
                        for i in feedback[r["id"]]]}
            for r in group]
        try:
            part = orch.translator.translate([UnitInput.from_row(r) for r in group], ctx, qa_glossary=orch.qa_glossary())
        except FatalAPIError:
            raise
        except LLMError as exc:
            orch._finding(None, "review_retranslation_failed", "medium", str(exc)[:300])
            stats["pending"] += len(group)
            stats["still_flagged"] += len(group)
            continue
        res.ok.update(part.ok)
        res.failed.update(part.failed)
    if not res.ok and not res.failed:
        return
    by_id = {r["id"]: r for r in todo}
    new_tid: dict[str, int] = {}
    mark: dict[str, int | None] = {}
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
                 json.dumps(ur.stored_notes(), ensure_ascii=False), utcnow(), ur.translator_note or None)).lastrowid)
            # written with the new text: stopped before the check answers, the fix shows as not checked (2.7)
            mark[uid] = orch._finding(uid, "fix_unchecked", "medium", "the fix was written; its check against the "
                                      "source had not answered yet", translation_id=new_tid[uid])
        for uid in res.failed:
            stats["still_flagged"] += 1
            stats["pending"] += 1
            orch._finding(uid, "review_retranslation_failed", "high", res.failed[uid][:300])
    checking = orch.cfg.review.fix_check
    # what the word rules found in a new text goes to the fix check with it (it reads the source; 「村長」 in a line
    # that names him, a title the house style asks for): the program does not overrule the check on a word
    word_flags = {u: [f for f in ur.findings if f.type in CONFIRM_RULES and f.severity in ("high", "error")]
                  for u, ur in res.ok.items()} if checking else {}
    checks = _check_fixes(orch, [(by_id[u], before[u], exported_text(res.ok[u].text, res.ok[u].translator_note,
                                                                     "needs_review"), feedback[u], word_flags.get(u, []))
                                 for u in res.ok]) if checking else {}
    with orch.lock, Transaction(orch.conn):
        for uid, ur in res.ok.items():
            tid = new_tid[uid]
            fids = [i["finding_id"] for i in feedback[uid] if i.get("finding_id")]
            judged = word_flags.get(uid, [])
            outcome = fix_outcome(checks.get(uid), program_high=worst(f for f in ur.findings if f not in judged) == "high",
                                  checked=checking)
            if outcome == "issue_invalid":
                # the old text already said what the source says: it stays, the new one is not used (least
                # intervention — two readings the source allows are not an error to fix)
                orch.conn.execute("UPDATE translations SET status='unneeded_fix' WHERE id=?", (tid,))
                orch.conn.executemany("UPDATE qa_findings SET status='dismissed_by_check' WHERE id=? AND status='open'",
                                      [(f,) for f in fids + [mark[uid]] if f])
                old = current.get(uid)
                if old is not None and old["status"] == "needs_review":
                    # still waiting while another serious finding on the unit is open: the job waits with it
                    if not orch.conn.execute("SELECT 1 FROM qa_findings WHERE unit_id=? AND status='open' AND "
                                             "severity='high'", (uid,)).fetchone():
                        settle_translation(orch.conn, old["id"])
                else:
                    orch.conn.execute("UPDATE jobs SET status='succeeded' WHERE run_id=? AND stage='translate' AND "
                                      "target_id=?", (orch.run_id, uid))
                stats["issue_invalid"] += 1
                stats["resolved"] += 1
                continue
            if outcome == "fixed":
                orch.conn.execute("UPDATE translations SET status='succeeded' WHERE id=?", (tid,))
                orch.conn.executemany(
                    "UPDATE qa_findings SET status=? WHERE id=? AND status='open'",
                    [("resolved", f) for f in fids + [mark[uid]] if f])
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
                # the mark written with the new text becomes the outcome
                orch.conn.execute("UPDATE qa_findings SET type=?, severity=?, evidence=? WHERE id=?",
                                  (f"fix_{outcome}", "high" if outcome in ("still_wrong", "regressed") else "medium",
                                   (f"{c.reason} {c.new_error}".strip() if c else
                                    "the fix was not checked against the source")[:400], mark[uid]))
            for f in ur.findings:
                if f.severity in ("high", "medium"):
                    fid = orch._finding(uid, f"after_review:{f.type}", f.severity, f.evidence, translation_id=tid)
                    if outcome == "fixed" and f in judged:  # the check saw it with the source and found no error
                        orch.conn.execute("UPDATE qa_findings SET status='dismissed_by_check' WHERE id=?", (fid,))


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
# what the source says (meaning, who, what is missing or added): the check reads the source itself, without the
# finder's reasoning or suggested wording, so one model's misreading is not passed on to the check that judges it
SOURCE_CLAIMS = {"mistranslation", "omission", "addition", "speaker_error", "subject_object_error",
                 "proofread_meaning", "proofread_omission", "proofread_addition", "proofread_speaker"}


def _claim(x: dict[str, Any]) -> str:
    kind = str(x.get("issue_type", ""))
    if kind in SOURCE_CLAIMS:
        return f"{kind}（只給類別；請自己對照原文判斷）"
    return f"{kind}: {x.get('evidence', '')} → {x.get('suggestion', '')}"[:500]


def _check_fixes(orch: Any, fixes: list[tuple[sqlite3.Row, str, str, list[dict[str, Any]], list[Finding]]]
                 ) -> dict[str, FixCheck]:
    """One look per fix, against the source: did the new text fix what was found? Units the check could not answer
    for (refused, failed, left out) are missing from the result and stay open."""
    out: dict[str, FixCheck] = {}
    if not fixes:
        return out
    from ..analysis import proofread

    prompt = load_prompt("fix_check")
    conn: sqlite3.Connection = orch.conn
    for i in range(0, len(fixes), FIX_CHECK_BATCH):
        chunk = fixes[i:i + FIX_CHECK_BATCH]
        with orch.lock:
            items = []
            for r, before, after, issues, flags in chunk:
                item: dict[str, Any] = {
                    "unit_id": r["id"], "source": r["source_text"], "before": before, "after": after,
                    "issues": [_claim(x) for x in issues],
                    # as far as review and the retranslation see, with the current text and who speaks
                    "around": _surroundings(conn, orch.book.id, [r])}
                if flags:
                    item["program_flags"] = [{"rule": f.type, "evidence": f.evidence[:200]} for f in flags]
                if lines := _lines(conn, r["id"]):  # 你／妳 and who-did-what issues need who speaks to whom
                    item["lines"] = lines
                items.append(item)
            # the people of these lines, as known there (name, gender, narrator): the same cards proofreading reads
            who = {x for r, *_ in chunk for x in _people(conn, r["id"])}
            cast = proofread.cast(conn, orch.book.id, [{"ja": r["plain_text"], "_who": sorted(who), "_at": r["ordinal"]}
                                                       for r, *_ in chunk])
        payload: dict[str, Any] = {"task": "fix_check", "items": items}
        if cast:
            payload["characters"] = [{k: v for k, v in c.items() if k in ("name", "ja", "gender", "narrator")} for c in cast]
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
