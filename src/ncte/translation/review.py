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

A retranslation that comes back as the text it was meant to change is not a fix to check: the translator's rules and
the finder's pull apart (a proofreader asking for 「」 where the source and the translator keep 『』). The text stays,
the finding becomes ``conflict`` with a ``rule_conflict`` beside it, and the same kind of issue on that unit is not
sent back again in the run.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..analysis.split_read import call_in_parts
from ..config import Config
from ..errors import FatalAPIError, LLMError, ModelOutputError
from ..llm.prompt_loader import load_prompt
from ..llm.schemas import FixCheck, FixCheckOutput, ReviewOutput
from ..memory.db import Transaction, utcnow
from ..qa.rules import CONFIRM_RULES, CONFIRMED, Finding, worst
from ..qa.typeset import typeset
from .cache import glossary_hash, latest_texts, latest_translation, relevant_glossary, settle_translation
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
                if notes := json.loads(tr["notes_json"] or "[]"):  # what the translator left for the reviewer
                    items[-1]["translator_notes"] = notes
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
        for key in ("address_terms", "style", "term_definitions"):
            if ctx.get(key):
                payload[key] = ctx[key]
        mem = ctx.get("memory") or {}
        if any(mem.get(k) for k in ("previous_chapter_summary", "this_chapter_so_far", "facts", "props")):
            payload["memory"] = mem  # what the translator had for this scene: known before it begins, chosen alike
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


def _addressees(conn: sqlite3.Connection, unit_id: str) -> list[dict[str, str]]:
    """Who the unit's lines are said to, with their gender, both as known at the unit (an inference marked so)."""
    from ..memory.repository import character_basics
    from ..memory.visibility import visible_names
    from ..series import pos

    unit = conn.execute("SELECT book_id, ordinal FROM units WHERE id=?", (unit_id,)).fetchone()
    if unit is None:
        return []
    names = visible_names(conn, unit["book_id"], unit["ordinal"])
    out: list[dict[str, str]] = []
    for r in conn.execute("SELECT addressee_ids_json to_, addressee_status ts FROM utterances WHERE unit_id=? ORDER BY "
                          "quote_index", (unit_id,)):
        for x in json.loads(r["to_"] or "[]"):
            if x not in names:
                continue
            item = {"name": names[x] + ("" if r["ts"] == "explicit" else "（推測）")}
            if g := character_basics(conn, x, pos(conn, unit["book_id"], unit["ordinal"])).get("gender"):
                item["gender"] = g
            if item not in out:
                out.append(item)
    return out


def _people(conn: sqlite3.Connection, unit_id: str) -> set[str]:
    """Who speaks a line of the unit and who it is said to."""
    out: set[str] = set()
    for r in conn.execute("SELECT speaker_id_nullable, addressee_ids_json FROM utterances WHERE unit_id=?", (unit_id,)):
        out |= {r[0]} if r[0] else set()
        out |= set(json.loads(r[1] or "[]"))
    return out


def _scene_names(conn: sqlite3.Connection, unit_id: str) -> dict[str, str]:
    """People named as known at the end of the unit's scene, as the scene was translated (a name may come a line
    after the quote)."""
    from ..memory.visibility import visible_names

    unit = conn.execute("SELECT book_id, scene_id, ordinal FROM units WHERE id=?", (unit_id,)).fetchone()
    end = conn.execute("SELECT MAX(ordinal) FROM units WHERE book_id=? AND scene_id=?",
                       (unit["book_id"], unit["scene_id"])).fetchone()[0] if unit["scene_id"] else None
    return visible_names(conn, unit["book_id"], end or unit["ordinal"])


def _lines(conn: sqlite3.Connection, unit_id: str) -> list[str]:
    """Each line of the unit as read: 「speaker → listener」, an inference marked （推測）."""
    rows = conn.execute("SELECT u.speaker_id_nullable sp, u.status, u.addressee_ids_json to_, u.addressee_status ts "
                        "FROM utterances u "
                        "WHERE u.unit_id=? ORDER BY u.quote_index", (unit_id,)).fetchall()
    if not rows:
        return []
    names = _scene_names(conn, unit_id)
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


def _fake_batch(orch: Any, rows: list[sqlite3.Row], *, whole_scene: bool = False, fix: bool = False) -> Any:
    from .orchestrator import Batch

    scene_ids = {r["scene_id"] for r in rows}
    return Batch(rows, False, next(iter(scene_ids)) if len(scene_ids) == 1 and orch.opts.mode == "context" else None,
                 whole_scene, fix)


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


def fix_groups(conn: sqlite3.Connection, cfg: Config, book_id: str, rows: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Units to retranslate, in story order. "window" scope (``translation.fix_scope``): one group per scene, each fix
    with its own window (``fix_windows``) — every request repeats the translation rules, and a request per run of
    paragraphs made 52 requests of 55 fixes; a scene past ``fix_batch_units`` paragraphs or ``fix_batch_chars``
    characters (the paragraphs and their windows) goes in even parts. "scene" scope, or no scene: one group per scene
    and run of adjacent paragraphs (the context window sits around a group, so two fixes far apart in a scene are sent
    apart: together, the paragraphs between them were neither translated nor shown)."""
    rows = sorted(rows, key=lambda r: r["ordinal"])
    if not rows:
        return []
    tc = cfg.translation
    batched = tc.fix_scope == "window" and tc.fix_context_chars > 0
    ords = [o for (o,) in conn.execute(
        "SELECT ordinal FROM units WHERE book_id=? AND translatable=1 AND kind IN ('text','heading') AND ordinal "
        "BETWEEN ? AND ? ORDER BY ordinal", (book_id, rows[0]["ordinal"], rows[-1]["ordinal"]))]
    pos = {o: i for i, o in enumerate(ords)}
    out: list[list[sqlite3.Row]] = []
    for group in scene_groups(rows):
        if batched and group[0]["scene_id"] is not None:
            out += _fix_parts(conn, cfg, book_id, group)
            continue
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


def _fix_parts(conn: sqlite3.Connection, cfg: Config, book_id: str,
               group: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """A scene's fixes in as few even parts as keep each within ``fix_batch_units`` paragraphs and ``fix_batch_chars``
    characters: the paragraphs, and each one's window as it is sent (windows that overlap count twice)."""
    from .context_builder import fix_window_rows

    def chars(part: list[sqlite3.Row]) -> int:
        ids = {r["id"] for r in part}
        return sum(len(r["plain_text"]) + sum(len(x["plain_text"]) for side in fix_window_rows(
            conn, cfg, book_id, r, ids - {r["id"]}) for x in side) for r in part)

    n = -(-len(group) // cfg.translation.fix_batch_units)
    while True:
        parts = [group[len(group) * k // n:len(group) * (k + 1) // n] for k in range(n)]
        if n >= len(group) or all(chars(p) <= cfg.translation.fix_batch_chars for p in parts):
            return parts
        n += 1


def _close_jobs(orch: Any, unit_ids: list[str], status: str) -> None:
    with orch.lock:
        orch.conn.executemany("UPDATE jobs SET status=?, updated_at=? WHERE run_id=? AND stage='review' AND target_id=?",
                              [(status, utcnow(), orch.run_id, u) for u in unit_ids])


# runs ``work(0)…work(n-1)`` side by side, ``done(i, result)`` for each; True when stopped before all were started
SideBySide = Callable[[int, Callable[[int], Any], Callable[[int, Any], None]], bool]


def _retranslate_with_feedback(orch: Any, rows: list[sqlite3.Row], feedback: dict[str, list[dict[str, Any]]],
                               stats: dict[str, int], *, side_by_side: SideBySide | None = None) -> None:
    """Send units back to the translator with what was found (and the current text, to change only what is wrong);
    then check each fix against the source (``fix_check``). ``feedback[unit]`` items may carry the ``finding_id`` they
    come from: only those findings are settled, and only by a fix the check accepts.

    ADR-102: the units are all a checker's batch asked to fix (a proofreading or reader's stretch, the same-form words,
    a review request), over any number of scenes. They are retranslated a scene at a time (``fix_groups``: each group
    keeps its scene's memory) and then checked together — one fix check request for up to FIX_CHECK_BATCH fixes,
    not one per scene. ``side_by_side`` (``ContextOrchestrator._side_by_side``) sends the groups at once, up to the
    run's concurrency; what comes back is kept in the groups' order, so the fixes are written and checked in story
    order whatever finished first."""
    _settle_fixes(orch, _write_fixes(orch, rows, feedback, stats, side_by_side=side_by_side), stats)


def _write_fixes(orch: Any, rows: list[sqlite3.Row], feedback: dict[str, list[dict[str, Any]]],
                 stats: dict[str, int], *, side_by_side: SideBySide | None = None) -> list[WrittenFix]:
    """The retranslations of ``_retranslate_with_feedback``, written as ``needs_review`` with their ``fix_unchecked``
    mark (and the record a later check needs, ADR-100): the fixes waiting for their check, in story order."""
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
        return []
    from ..exporter import exported_text

    # what the book says now, as exported (the text and a translator's note after it)
    before = {u: (exported_text(t["text"], t["translator_note"], t["status"]) if t is not None else "")
              for u, t in current.items()}
    stats["retranslated"] += len(todo)
    res = BatchResult()
    # "window" (translation.fix_scope): the paragraphs around each fix (fix_context_chars a side, at least two) and
    # what they need of the memory — the people in them, their lines, terms and facts; a fix request carrying the
    # whole scene's data cost more than the first translation of the book. A scene's fixes go in one request, each
    # with its own window (``around``: the request has no context before or after of its own). "scene": as much of
    # the scene as a retranslation of chosen units sees (targeted_scene_context_chars a side) and the people of the
    # whole scene, a request per run of paragraphs
    whole = orch.cfg.translation.fix_scope == "scene"
    with orch.lock:
        groups = fix_groups(conn, orch.cfg, orch.book.id, todo)

    def one(g: int) -> BatchResult | LLMError:
        group = groups[g]
        ctx = orch.batch_context(_fake_batch(orch, group, whole_scene=whole, fix=not whole))
        around = ctx.pop("around", {})
        ctx["review_feedback"] = [
            {"unit_id": r["id"], **({"current_translation": before[r["id"]]} if before[r["id"]] else {}),
             "issues": [{k: v for k, v in i.items() if k in ("issue_type", "evidence", "suggestion")}
                        for i in feedback[r["id"]]],
             # the text around, without ids: the model refers to none of it (ids would also number the request's
             # paragraphs out of story order)
             **({"around": {side: [{k: v for k, v in c.items() if k != "unit_id"} for c in around[r["id"]][side]]
                            for side in ("before", "after")}} if r["id"] in around else {})}
            for r in group]
        try:
            return orch.translator.translate([UnitInput.from_row(r) for r in group], ctx,
                                             qa_glossary=orch.qa_glossary())
        except FatalAPIError:
            raise
        except LLMError as exc:
            return exc

    got: list[BatchResult | LLMError | None] = [None] * len(groups)  # None: not sent (a stop)

    def keep(g: int, part: BatchResult | LLMError) -> None:
        got[g] = part

    if side_by_side is None:
        for g in range(len(groups)):
            keep(g, one(g))
    else:
        side_by_side(len(groups), one, keep)
    for group, part in zip(groups, got, strict=True):
        if part is None:
            continue  # not sent: what was found stays open
        if isinstance(part, LLMError):
            orch._finding(None, "review_retranslation_failed", "medium", str(part)[:300])
            stats["pending"] += len(group)
            stats["still_flagged"] += len(group)
            continue
        res.ok.update(part.ok)
        res.failed.update(part.failed)
    if not res.ok and not res.failed:
        return []
    by_id = {r["id"]: r for r in todo}
    # the translator kept the text it was asked to change (the same once typeset): nothing to check
    kept = {u: t["id"] for u, t in current.items() if t is not None and u in res.ok and typeset(
        before[u], by_id[u]["source_text"]) == typeset(exported_text(res.ok[u].text, res.ok[u].translator_note,
                                                                     "needs_review"), by_id[u]["source_text"])}
    if kept:
        _record_conflicts(orch, kept, {u: feedback[u] for u in kept}, stats)
        for u in kept:
            del res.ok[u]
    fixes: list[WrittenFix] = []
    with orch.lock, Transaction(orch.conn):
        for uid, ur in res.ok.items():
            r = by_id[uid]
            old = current[uid]
            # what the check needs if it does not answer now (ADR-100): the text replaced, the issues sent with their
            # finding ids, what the program's rules found in the new text
            record = {"before_id": old["id"] if old is not None else None, "before": before[uid],
                      "issues": feedback[uid], "findings": [{"type": f.type, "severity": f.severity,
                                                             "evidence": f.evidence} for f in ur.findings]}
            # not the book's text until the fix is checked
            tid = int(orch.conn.execute(
                "INSERT INTO translations(unit_id, target_lang, text, source_hash, prompt_version, model_id, "
                "glossary_version, mode, status, run_id, request_id, flags_json, notes_json, created_at, translator_note, "
                "fix_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, orch.cfg.app.language, ur.text, r["source_hash"], orch.prompt.version_id, orch.model,
                 glossary_hash(relevant_glossary(orch.glossary, r["plain_text"])), orch.opts.mode, "needs_review",
                 orch.run_id, ur.request_id, json.dumps(ur.flags + ["review_retranslation"], ensure_ascii=False),
                 json.dumps(ur.stored_notes(), ensure_ascii=False), utcnow(), ur.translator_note or None,
                 json.dumps(record, ensure_ascii=False))).lastrowid)
            # written with the new text: stopped before the check answers, the fix shows as not checked (2.7)
            mark = orch._finding(uid, "fix_unchecked", "medium", "the fix was written; its check against the "
                                 "source had not answered yet", translation_id=tid)
            fixes.append(WrittenFix(r, tid, mark, orch.run_id, old, before[uid],
                                    exported_text(ur.text, ur.translator_note, "needs_review"), feedback[uid],
                                    ur.findings))
        for uid in res.failed:
            stats["still_flagged"] += 1
            stats["pending"] += 1
            orch._finding(uid, "review_retranslation_failed", "high", res.failed[uid][:300])
    return fixes


@dataclass
class WrittenFix:
    """A fix written as ``needs_review``, waiting for its check: the unit, the new text (``tid``; ``after`` as it goes
    into the book) with its ``fix_unchecked`` mark and the run that wrote it, the text it replaced (``old``, ``before``),
    the issues it was sent to fix (with their finding ids) and what the program's rules found in it."""
    row: sqlite3.Row
    tid: int
    mark: int | None
    run_id: str
    old: sqlite3.Row | None
    before: str
    after: str
    issues: list[dict[str, Any]]
    findings: list[Finding]


def _settle_fixes(orch: Any, fixes: list[WrittenFix], stats: dict[str, int], *, again: bool = False) -> None:
    """Check each fix against the source and settle it by the answer. ``again``: fixes an earlier check left without
    an answer (ADR-100); the program's finds in the new text may be on record already and are not written twice."""
    if not fixes:
        return
    checking = orch.cfg.review.fix_check
    # what the word rules found in a new text goes to the fix check with it (it reads the source; 「村長」 in a line
    # that names him, a title the house style asks for): the program does not overrule the check on a word
    word_flags = {x.row["id"]: [f for f in x.findings if f.type in CONFIRM_RULES and f.severity in ("high", "error")]
                  for x in fixes} if checking else {}
    checks = _check_fixes(orch, [(x.row, x.before, x.after, x.issues, word_flags.get(x.row["id"], []))
                                 for x in fixes]) if checking else {}
    if checking:
        orch.fix_checks_asked.update(x.tid for x in fixes)
    with orch.lock, Transaction(orch.conn):
        for x in fixes:
            uid, tid, mark = x.row["id"], x.tid, x.mark
            fids = [i["finding_id"] for i in x.issues if i.get("finding_id")]
            judged = word_flags.get(uid, [])
            outcome = fix_outcome(checks.get(uid), program_high=worst(f for f in x.findings if f not in judged) == "high",
                                  checked=checking)
            if outcome == "issue_invalid":
                # the old text already said what the source says: it stays, the new one is not used (least
                # intervention — two readings the source allows are not an error to fix)
                orch.conn.execute("UPDATE translations SET status='unneeded_fix' WHERE id=?", (tid,))
                orch.conn.executemany("UPDATE qa_findings SET status='dismissed_by_check' WHERE id=? AND status='open'",
                                      [(f,) for f in fids + [mark] if f])
                old = orch.conn.execute("SELECT id, status FROM translations WHERE id=?",
                                        (x.old["id"],)).fetchone() if x.old is not None else None
                if old is not None and old["status"] == "needs_review":
                    # still waiting while another serious finding on the unit is open: the job waits with it
                    if not orch.conn.execute("SELECT 1 FROM qa_findings WHERE unit_id=? AND status='open' AND "
                                             "severity='high'", (uid,)).fetchone():
                        settle_translation(orch.conn, old["id"])
                else:
                    orch.conn.execute("UPDATE jobs SET status='succeeded' WHERE run_id=? AND stage='translate' AND "
                                      "target_id=?", (x.run_id, uid))
                stats["issue_invalid"] += 1
                stats["resolved"] += 1
                continue
            if outcome == "fixed":
                orch.conn.execute("UPDATE translations SET status='succeeded' WHERE id=?", (tid,))
                orch.conn.executemany(
                    "UPDATE qa_findings SET status=? WHERE id=? AND status='open'",
                    [("resolved", f) for f in fids + [mark] if f])
                orch.conn.execute("UPDATE jobs SET status='succeeded' WHERE run_id=? AND stage='translate' AND "
                                  "target_id=?", (x.run_id, uid))
                stats[outcome] += 1
                stats["resolved"] += 1
            else:
                if outcome == "regressed":  # the fix broke something else: the old text stays the book's
                    orch.conn.execute("UPDATE translations SET status='rejected_fix' WHERE id=?", (tid,))
                elif again:  # waiting, as it was written, even if a look since found nothing else on it
                    orch.conn.execute("UPDATE translations SET status='needs_review' WHERE id=? AND status='succeeded'",
                                      (tid,))
                stats["still_flagged"] += 1
                stats["pending"] += 1
                c = checks.get(uid)
                # the mark written with the new text becomes the outcome
                orch.conn.execute("UPDATE qa_findings SET type=?, severity=?, evidence=? WHERE id=?",
                                  (f"fix_{outcome}", "high" if outcome in ("still_wrong", "regressed") else "medium",
                                   (f"{c.reason} {c.new_error}".strip() if c else
                                    "the fix was not checked against the source" +
                                    (" (asked again after the run; still no answer)" if again else ""))[:400], mark))
            for f in x.findings:
                if f.severity in ("high", "medium"):
                    # a check that failed before wrote these already (a pause cut off before them did not)
                    had = orch.conn.execute(
                        "SELECT id FROM qa_findings WHERE unit_id=? AND translation_id=? AND type=? AND evidence=? AND "
                        "status='open'", (uid, tid, f"after_review:{f.type}", f.evidence)).fetchone() if again else None
                    fid = had[0] if had else orch._finding(uid, f"after_review:{f.type}", f.severity, f.evidence,
                                                           translation_id=tid)
                    if outcome == "fixed" and f in judged:  # the check saw it with the source and found no error
                        orch.conn.execute("UPDATE qa_findings SET status='dismissed_by_check' WHERE id=?", (fid,))


def check_waiting_fixes(orch: Any, unit_ids: set[str] | None = None) -> dict[str, int]:
    """ADR-100: fixes whose check never answered — it failed, or a pause cut it off — on texts that are still the
    book's (the ``fix_unchecked`` mark open; a later review that found nothing may have cleared ``needs_review``, the
    fix was still never checked against the source) are checked once more, together (as a checker's batch's fixes are,
    ADR-102), from what was recorded with the fix, and settled as the first check would have settled them. A fix whose check was asked in this
    process already (the first check and its one more try) is not asked again here; still without an answer, it stays
    open. A fix written before the record was kept (``fix_json`` empty) cannot be put to the check and stays open."""
    stats = new_stats(asked=0, unrecorded=0)
    if not orch.cfg.review.fix_check:
        return stats
    from ..exporter import exported_text

    conn: sqlite3.Connection = orch.conn
    with orch.lock:
        best = latest_texts(conn, orch.book.id)
        waiting = conn.execute(
            "SELECT t.*, f.id AS mark FROM translations t JOIN units u ON u.id=t.unit_id JOIN qa_findings f "
            "ON f.translation_id=t.id AND f.unit_id=t.unit_id WHERE u.book_id=? AND f.type='fix_unchecked' AND "
            "f.status='open' ORDER BY u.ordinal, t.id", (orch.book.id,)).fetchall()
        fixes: list[WrittenFix] = []
        for t in waiting:
            uid = t["unit_id"]
            # a newer text (another fix, a person's) is the book's now: this one is history
            if best.get(uid) is None or best[uid]["id"] != t["id"] or t["id"] in orch.fix_checks_asked or (
                    unit_ids is not None and uid not in unit_ids):
                continue
            if t["fix_json"] is None:
                stats["unrecorded"] += 1
                continue
            rec = json.loads(t["fix_json"])
            row = conn.execute("SELECT * FROM units WHERE id=?", (uid,)).fetchone()
            old = conn.execute("SELECT * FROM translations WHERE id=?",
                               (rec["before_id"],)).fetchone() if rec["before_id"] is not None else None
            fixes.append(WrittenFix(
                row, t["id"], t["mark"], t["run_id"], old, rec["before"],
                exported_text(t["text"], t["translator_note"], "needs_review"), rec["issues"],
                [Finding(f["type"], f["severity"], f["evidence"], uid) for f in rec["findings"]]))
    stats["asked"] += len(fixes)
    _settle_fixes(orch, fixes, stats, again=True)
    return stats

def _record_conflicts(orch: Any, tids: dict[str, int], feedback: dict[str, list[dict[str, Any]]],
                      stats: dict[str, int]) -> None:
    """Units whose retranslation came back unchanged (``tids``: the text that stays). What a model found on them
    (``review:*``) is a conflict between its rule and the translator's: the finding becomes ``conflict`` and a
    ``rule_conflict`` (the issue's kind and the finder's evidence) is recorded on the unit, both kept out of the open
    errors; a program rule's find stays open, since the text it was found in is still the book's, and so does a
    serious claim about the source (``SOURCE_CLAIMS``, high): a meaning error the translator would not change is for a
    person to decide, not a disagreement between rules."""
    fids = [i["finding_id"] for items in feedback.values() for i in items if i.get("finding_id")]
    with orch.lock, Transaction(orch.conn):
        rows = {r["id"]: r for r in orch.conn.execute(
            f"SELECT id, type, severity FROM qa_findings WHERE id IN ({','.join('?' * len(fids))})", fids)} if fids else {}
        kinds = {k: r["type"] for k, r in rows.items()}
        for uid, items in feedback.items():
            left = False
            for i in items:
                fid = i.get("finding_id")
                serious = fid in rows and rows[fid]["severity"] == "high" and i["issue_type"] in SOURCE_CLAIMS
                if not fid or not kinds.get(fid, "").startswith("review:") or serious:
                    left = True
                    continue
                orch._finding(uid, "rule_conflict", "medium", f"{i['issue_type']}: {i.get('evidence', '')}"[:600],
                              translation_id=tids[uid])
                orch.conn.execute("UPDATE qa_findings SET status='conflict' WHERE id=? AND status='open'", (fid,))
                stats["conflict"] = stats.get("conflict", 0) + 1
            if left:
                stats["still_flagged"] += 1
                stats["pending"] += 1


def in_conflict(orch: Any, unit_ids: list[str] | set[str]) -> set[tuple[str, str]]:
    """(unit, finding type) pairs that came back unchanged earlier in this run: not sent to the translator again."""
    if not unit_ids:
        return set()
    with orch.lock:
        return {(r[0], r[1]) for r in orch.conn.execute(
            f"SELECT unit_id, type FROM qa_findings WHERE run_id=? AND status='conflict' AND unit_id IN "
            f"({','.join('?' * len(unit_ids))})", (orch.run_id, *unit_ids))}


def supersede(orch: Any, checked: dict[str, int], reported: dict[str, set[str]], kinds: tuple[str, ...]) -> int:
    """Open findings a pass has outlived: on each unit it really checked (``checked[unit]``: the text it checked),
    a finding of ``kinds`` (``LIKE`` patterns) about an older text of the unit that this pass did not report again
    (``reported[unit]``: the finding types it reported there; a fix's outcome counts as reported when the pass reported
    anything there) is ``superseded`` — the paragraph was checked as it is now and the issue was not found. A finding
    about the text that was checked stays open: two looks at the same words that disagree are not settled here."""
    n = 0
    like = " OR ".join("type LIKE ?" for _ in kinds)
    with orch.lock, Transaction(orch.conn):
        for uid, tid in checked.items():
            for r in orch.conn.execute(f"SELECT id, type FROM qa_findings WHERE book_id=? AND unit_id=? AND "
                                       f"status='open' AND translation_id<? AND ({like})",
                                       (orch.book.id, uid, tid, *kinds)).fetchall():
                again = reported.get(uid, set())
                if r["type"] in again or (r["type"].startswith("fix_") and again):
                    continue
                orch.conn.execute("UPDATE qa_findings SET status='superseded' WHERE id=?", (r["id"],))
                n += 1
    return n


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
    """An issue as the fix check reads it: the kind only for a claim about the source; otherwise what the finder
    said and suggested (``proofread_expression`` too: how the Chinese reads is not a claim about the source) — without
    the instructions written for the translator (``proofread.FIX_NOTE``, ``proofread.FIX_NOTE_EXPRESSION``,
    ``reader.FIX_NOTE``, ``false_friends.FIX_NOTE``), which are not a suggestion."""
    from ..analysis import false_friends, proofread, reader

    kind = str(x.get("issue_type", ""))
    if kind in SOURCE_CLAIMS:
        return f"{kind}（只給類別；請自己對照原文判斷）"
    suggestion = str(x.get("suggestion", ""))
    for note in (proofread.FIX_NOTE, proofread.FIX_NOTE_EXPRESSION, reader.FIX_NOTE, false_friends.FIX_NOTE):
        suggestion = suggestion.replace(note, "")
    suggestion = suggestion.strip()
    return (f"{kind}: {x.get('evidence', '')}" + (f" → {suggestion}" if suggestion else ""))[:500]


FixItem = tuple[sqlite3.Row, str, str, list[dict[str, Any]], list[Finding]]


def _check_fixes(orch: Any, fixes: list[FixItem]) -> dict[str, FixCheck]:
    """One look per fix, against the source: did the new text fix what was found? A fix the check did not answer for
    (its request failed after the empty answers asked again, or the answer left it out) is sent once more on its own,
    as proofreading does with a paragraph left without a verdict (ADR-089, ADR-100); one the content filter refused is
    not. Units still without an answer are missing from the result and stay open."""
    out: dict[str, FixCheck] = {}
    if not fixes:
        return out
    prompt = load_prompt("fix_check")
    again: list[FixItem] = []
    failures: list[tuple[int | None, list[str]]] = []
    for i in range(0, len(fixes), FIX_CHECK_BATCH):
        chunk = fixes[i:i + FIX_CHECK_BATCH]
        left, failed = _ask_fix_checks(orch, prompt, chunk, out)
        if failed is not None:
            failures.append((failed, [r["id"] for r, *_ in chunk]))
        # a lone fix whose request failed would be the same request again: only a part of a larger one, or a fix
        # the answer left out, is asked on its own
        again += [x for x in left if len(chunk) > 1 or failed is None]
    if again:
        orch.log(f"  fix check: {len(again)} fix(es) without an answer, each asked once more on its own")
    for x in again:
        _ask_fix_checks(orch, prompt, [x], out, retry=True)
    with orch.lock:  # a failed request whose fixes were all answered on their own lost nothing
        orch.conn.executemany("UPDATE qa_findings SET status='resolved' WHERE id=? AND status='open'",
                              [(fid,) for fid, units in failures if fid and all(u in out for u in units)])
    return out


def _ask_fix_checks(orch: Any, prompt: Any, chunk: list[FixItem], out: dict[str, FixCheck], *,
                    retry: bool = False) -> tuple[list[FixItem], int | None]:
    """One fix check request about ``chunk`` (halved when the content filter refuses it); the answers go into
    ``out``. Returns the fixes asked but not answered (not the refused ones), and the ``fix_check_failed`` finding
    when the request failed."""
    from ..analysis import proofread
    from ..memory.repository import line_relations
    from ..memory.retrieval import unit_actions

    conn: sqlite3.Connection = orch.conn
    with orch.lock:
        items = []
        actions = unit_actions(conn, orch.book.id, [r["id"] for r, *_ in chunk])
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
                # how those two are related as of this paragraph: 你／您, honorifics, how heavy a form is
                if rel := line_relations(conn, orch.book.id, r["id"], r["ordinal"], _scene_names(conn, r["id"])):
                    item["relation"] = rel
            if r["id"] in actions:  # who does what where the source leaves the subject out, as read
                item["actions"] = actions[r["id"]]
            items.append(item)
        # the people of these lines, as known there (name, gender, narrator): the same cards proofreading reads
        who = {x for r, *_ in chunk for x in _people(conn, r["id"])}
        part = [{"ja": r["plain_text"], "_who": sorted(who), "_at": r["ordinal"]} for r, *_ in chunk]
        cast = proofread.cast(conn, orch.book.id, part)
        # the decided names of these paragraphs and how their speakers call their people, as proofreading has them
        names_and_forms = proofread.renderings(conn, orch.book.id, part, glossary=orch.glossary,
                                               volume=orch.volume, revealed=orch.link_revealed,
                                               compact=orch.cfg.translation.compact_payload)
    payload: dict[str, Any] = {"task": "fix_check", "items": items, **names_and_forms}
    if orch.cfg.context.term_definitions:  # what the terms in these paragraphs mean, as shown there (ADR-069)
        from ..memory.terms import definitions_for

        with orch.lock:
            defs = {d["ja"]: d for r, *_ in chunk for d in definitions_for(conn, orch.book.id, r["plain_text"],
                                                                           r["ordinal"])}
        if defs:
            payload["term_definitions"] = list(defs.values())
    if cast:
        # with the way of speaking and the house style's habits, as proofreading has them: a tone or voice fix
        # is judged against the same card it was found with
        payload["characters"] = [{k: v for k, v in c.items() if k in ("name", "ja", "gender", "narrator", "voice",
                                                                      "habits")} for c in cast]
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
        fid = orch._finding(None, "fix_check_failed", "medium",
                            f"{len(items)} units{' (asked again on its own)' if retry else ''}: {exc}"[:400])
        return list(chunk), fid
    if parsed is None:
        return [], None
    assert isinstance(parsed, FixCheckOutput)
    asked = {x["unit_id"] for x in items} - set(blocked)
    for c in parsed.checks:
        if c.unit_id in asked and c.unit_id not in out:
            out[c.unit_id] = c
    return [x for x in chunk if x[0]["id"] in asked and x[0]["id"] not in out], None
