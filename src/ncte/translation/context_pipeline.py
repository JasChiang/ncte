"""Context-aware translation mode (spec §2.3, §5): understand → remember → translate → verify.

Read first (ADR-023, default): pass 1 runs steps 1 and 3 for every scene in reading order (no translation yet,
so memory comes from the source only), pass 2 proposes renderings for names and address terms (``naming``) and
may pause for the user's choices, pass 3 runs steps 2 and 4 scene by scene. With ``context.read_first`` off the
steps interleave per scene:
  1. ``scene_analyze`` job  – LLM proposes characters / speakers / omitted arguments; the program
     validates everything against the source and reconciles speakers (explicit only via dialogue
     tags). If the LLM step fails, a program-only analysis is stored instead.
  2. ``translate`` jobs      – sequential batches inside the scene, each with visibility-filtered
     memory (characters, verified speech examples, speaker status, summaries, facts, props).
  3. ``memory_update`` job  – facts / glossary / summary with verbatim evidence checks.
  4. ``review`` (M5)        – selective review of flagged units, at most one pass.
Each step commits separately: a failing memory step never touches committed translations.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..analysis.memory_update import apply_memory_update, build_memory_payload
from ..analysis.naming import apply_naming, auto_settle, build_naming_payload, model_view, pending
from ..analysis.scene import apply_scene_analysis, build_analysis_payload, get_scene
from ..analysis.split_read import MIN_PART, merge_outputs, subset_payload
from ..errors import ContentBlockedError, FatalAPIError, LLMError, ModelOutputError
from ..llm.prompt_loader import load_prompt
from ..llm.schemas import MemoryUpdateOutput, NamingOutput, SceneAnalysisOutput, VerifyOutput
from ..memory.db import Transaction, utcnow
from ..memory.retrieval import memory_context
from .context_builder import BODY_KINDS
from .orchestrator import Batch, Orchestrator, make_batches
from .translator import call_structured

VERIFY_BATCH = 25  # items per second-look request


class ContextOrchestrator(Orchestrator):
    def prepare(self) -> None:
        super().prepare()
        self.analyze_prompt = load_prompt("scene_analyze")
        self.memory_prompt = load_prompt("memory_update")
        self.naming_prompt = load_prompt("naming")

    def process_batch(self, batch: Batch) -> dict[str, int]:
        counts = super().process_batch(batch)
        from ..analysis.narration import first_person_slip
        from ..qa import second_person

        third = False
        if not batch.meta and batch.scene_id:
            from ..analysis.narration import for_scene

            with self.lock:
                who = for_scene(self.conn, self.book.id, get_scene(self.conn, self.book.id, batch.scene_id))
            third = who is not None and who["person"] == "third"

        with self.lock, Transaction(self.conn):
            for r in batch.rows:
                t = self.conn.execute("SELECT id, text FROM translations WHERE unit_id=? AND run_id=? ORDER BY id DESC "
                                      "LIMIT 1", (r["id"], self.run_id)).fetchone()
                if t is None:
                    continue
                found = []
                if ev := second_person.check(self.conn, self.book.id, r, t["text"]):
                    found.append(("second_person_gender", ev))
                if third and first_person_slip(r["plain_text"], t["text"]):
                    found.append(("narration_first_person", "the narration is third person, but this paragraph says 我 "
                                  "outside quotes: use the focal character's name, 他／她, or leave the subject out"))
                for typ, ev in found:  # the scene's review looks at it with the context and fixes it
                    self._finding(r["id"], typ, "high", ev)
                if found:
                    self.conn.execute("UPDATE translations SET status='needs_review' WHERE id=?", (t["id"],))
        return counts

    # ------------------------------------------------------------ jobs
    def ensure_jobs(self) -> None:
        with self.lock:
            fresh = not self.conn.execute("SELECT 1 FROM jobs WHERE run_id=?", (self.run_id,)).fetchone()
        if fresh and self.cfg.context.scene_breaks == "llm" and not self.opts.unit_ids:  # a sample (--limit-units) too
            self.choose_scene_breaks()
        super().ensure_jobs()

    def choose_scene_breaks(self) -> None:
        """Before anything is read (ADR-036): the model divides long stretches the author left undivided."""
        from ..analysis import scene_breaks as sb
        from ..analysis.scene import segment_book
        from ..llm.schemas import SceneBreaksOutput

        with self.lock:
            if self.conn.execute("SELECT 1 FROM scenes WHERE book_id=? AND version>0 LIMIT 1", (self.book.id,)).fetchone() \
                    or self.conn.execute("SELECT 1 FROM scene_breaks WHERE book_id=? LIMIT 1", (self.book.id,)).fetchone():
                return  # already read (scene ids are in use) or already divided
            todo = sb.stretches(self.conn, self.book.id)
        if not todo:
            return
        prompt, size = load_prompt("scenes"), self.cfg.memory.max_scene_units
        stats = {"stretches": len(todo), "breaks": 0, "kept_program_cuts": 0}
        for rows in todo:
            try:
                parsed = self._llm("scenes", prompt, sb.payload(rows, size), [r["id"] for r in rows])
            except FatalAPIError:
                raise
            except LLMError as exc:
                self._finding(None, "scene_breaks_failed", "low", str(exc)[:300])
                stats["kept_program_cuts"] += 1
                continue
            assert isinstance(parsed, SceneBreaksOutput)
            ok = sb.check(rows, [b.unit_id for b in parsed.breaks], size)
            if ok is None:
                stats["kept_program_cuts"] += 1
                continue
            with self.lock:
                sb.save(self.conn, self.book.id, rows, ok, {b.unit_id: b.reason for b in parsed.breaks})
            stats["breaks"] += len(ok)
        with self.lock:
            segment_book(self.conn, self.cfg, self.book.id)
        self.log(f"scene breaks: {stats}")

    def create_extra_jobs(self) -> None:
        scene_ids = [r["scene_id"] for r in self.conn.execute(
            "SELECT DISTINCT u.scene_id, MIN(u.ordinal) o FROM jobs j JOIN units u ON u.id=j.target_id WHERE "
            "j.run_id=? AND j.stage='translate' AND u.scene_id IS NOT NULL GROUP BY u.scene_id ORDER BY o",
            (self.run_id,))]
        matter: set[str] = set()
        if self.cfg.context.skip_book_matter:
            from ..epub.matter import story_hrefs

            story = story_hrefs(self.conn, self.book.id)
            matter = {sid for sid in scene_ids if not {r[0] for r in self.conn.execute(
                "SELECT DISTINCT resource_href FROM units WHERE book_id=? AND scene_id=?", (self.book.id, sid))} & story}
        for seq, sid in enumerate(scene_ids):
            scene = get_scene(self.conn, self.book.id, sid)
            analyzed = int(scene["version"]) > 0 or sid in matter  # book matter: translated, not analysed
            has_memory = self.conn.execute("SELECT 1 FROM chapter_summaries WHERE book_id=? AND scene_id=?",
                                           (self.book.id, sid)).fetchone() is not None
            a_status = "pending" if sid not in matter and (not analyzed or (self.opts.force and not self.opts.unit_ids)) \
                else "skipped"
            m_status = "pending" if (self.cfg.context.memory_update and sid not in matter and (not has_memory or (
                self.opts.force and not self.opts.unit_ids))) else "skipped"
            for stage, st in (("scene_analyze", a_status), ("memory_update", m_status)):
                self.conn.execute(
                    "INSERT INTO jobs(book_id, run_id, stage, target_id, seq, status, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (self.book.id, self.run_id, stage, sid, seq, st, utcnow()))
        if self.cfg.context.read_first and not self.opts.unit_ids:
            self.conn.execute(
                "INSERT INTO jobs(book_id, run_id, stage, target_id, seq, status, updated_at) VALUES (?,?,?,?,?,?,?)",
                (self.book.id, self.run_id, "verify", self.book.id, len(scene_ids), "pending", utcnow()))
        if self.cfg.context.read_first and not self.opts.unit_ids:
            self.conn.execute(
                "INSERT INTO jobs(book_id, run_id, stage, target_id, seq, status, updated_at) VALUES (?,?,?,?,?,?,?)",
                (self.book.id, self.run_id, "relations", self.book.id, len(scene_ids), "pending", utcnow()))
        if self.cfg.context.reader_pass and not self.opts.unit_ids:
            self.conn.execute(
                "INSERT INTO jobs(book_id, run_id, stage, target_id, seq, status, updated_at) VALUES (?,?,?,?,?,?,?)",
                (self.book.id, self.run_id, "reader", self.book.id, len(scene_ids) + 1, "pending", utcnow()))
        if self.cfg.context.read_first and self.cfg.context.naming != "off" and not self.opts.unit_ids:
            # naming is a whole-volume step; retranslating chosen units keeps the existing renderings
            self.conn.execute(
                "INSERT INTO jobs(book_id, run_id, stage, target_id, seq, status, updated_at) VALUES (?,?,?,?,?,?,?)",
                (self.book.id, self.run_id, "naming", self.book.id, len(scene_ids), "pending", utcnow()))

    def _job(self, stage: str, sid: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("SELECT * FROM jobs WHERE run_id=? AND stage=? AND target_id=?",
                                     (self.run_id, stage, sid)).fetchone()

    def _set_job(self, stage: str, sid: str, status: str, error: str | None = None,
                 checkpoint: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.conn.execute("UPDATE jobs SET status=?, error=?, attempts=attempts+?, checkpoint_json=?, updated_at=? "
                              "WHERE run_id=? AND stage=? AND target_id=?",
                              (status, error, 1 if status == "failed" else 0, json.dumps(checkpoint or {}),
                               utcnow(), self.run_id, stage, sid))

    # ------------------------------------------------------------ execution
    def execute(self) -> None:
        with self.lock:
            scenes = [r["target_id"] for r in self.conn.execute(
                "SELECT target_id FROM jobs WHERE run_id=? AND stage IN ('scene_analyze','memory_update') "
                "GROUP BY target_id ORDER BY MIN(seq)", (self.run_id,))]
        if self.cfg.context.read_first:
            self.read_pass(scenes)
            if not self._stop_reason:
                self.verify_pass()
            if not self._stop_reason:
                self.relations_pass()
            if self._stop_reason or not self.naming_pass():
                return
            self.translate_pass(scenes)
        else:
            self.interleaved(scenes)
        if not self._stop_reason:
            self.reader_pass()
        if not self._stop_reason:
            self.run_meta()

    def _scene_pending(self, sid: str) -> tuple[list[sqlite3.Row], bool]:
        pending = [r for r in self.pending_rows(meta=False) if r["scene_id"] == sid]
        jobs = (self._job("scene_analyze", sid), self._job("memory_update", sid))
        return pending, any(j is not None and j["status"] in ("pending", "running") for j in jobs)

    def interleaved(self, scenes: list[str]) -> None:
        positions = self.body_positions()
        for i, sid in enumerate(scenes):
            if self._stop_reason:
                break
            pending, analysis_due = self._scene_pending(sid)
            if not pending and not analysis_due:
                continue
            self.log(f"scene {sid} ({i + 1}/{len(scenes)})")
            self.stage_scene_analyze(sid)
            self.run_batches(make_batches(pending, self.cfg.translation.unit_target_japanese_chars, by_scene=True,
                                          position=positions), concurrency=1)
            self.stage_memory_update(sid)
            self.after_scene(sid)

    def read_pass(self, scenes: list[str]) -> None:
        """Pass 1: understand and remember every scene before anything is translated."""
        due = [sid for sid in scenes if self._scene_pending(sid)[1]]
        for i, sid in enumerate(due):
            if self._stop_reason:
                break
            self.log(f"read {sid} ({i + 1}/{len(due)})")
            self.stage_scene_analyze(sid)
            self.stage_memory_update(sid)

    def verify_pass(self) -> None:
        """Pass 1b (ADR-033): a second look at what the read pass left in doubt, with the surrounding text."""
        from ..analysis import verify

        job = self._job("verify", self.book.id)
        if job is None or job["status"] not in ("pending", "running"):
            return
        with self.lock:
            items = verify.collect(self.conn, self.book.id)
        stats: dict[str, int] = {"items": len(items)}
        prompt = load_prompt("verify")
        for i in range(0, len(items), VERIFY_BATCH):
            chunk = items[i:i + VERIFY_BATCH]
            try:
                parsed = self._llm("verify", prompt, {"task": "verify", "items": [
                    {k: v for k, v in it.items() if not k.startswith("_")} for it in chunk]}, [])
            except FatalAPIError:
                raise
            except LLMError as exc:  # the read pass's answers stand
                self._finding(None, "verify_failed", "low", str(exc)[:400])
                continue
            assert isinstance(parsed, VerifyOutput)
            with self.lock, Transaction(self.conn):
                for k, v in verify.apply(self.conn, self.book.id, chunk, parsed, self._finding).items():
                    stats[k] = stats.get(k, 0) + v
        self._set_job("verify", self.book.id, "succeeded", None, stats)
        self.log(f"second look: {stats}")

    def relations_pass(self, *, record_job: bool = True) -> int:
        """Pass 1c (ADR-039): relations between characters, from the facts gathered while reading."""
        from ..analysis import relations
        from ..llm.schemas import RelationsOutput

        job = self._job("relations", self.book.id)
        if record_job and (job is None or job["status"] not in ("pending", "running")):
            return 0
        with self.lock:
            req, lookup = relations.payload(self.conn, self.book.id)
        n = 0
        if req["facts"]:
            try:
                parsed = self._llm("relations", load_prompt("relations"), req, [])
            except FatalAPIError:
                raise
            except LLMError as exc:  # the graph falls back to scenes shared
                self._finding(None, "relations_failed", "low", str(exc)[:300])
                parsed = None
            if parsed is not None:
                assert isinstance(parsed, RelationsOutput)
                with self.lock:
                    n = relations.apply(self.conn, self.book.id, parsed, lookup)
        if record_job:
            self._set_job("relations", self.book.id, "succeeded", None, {"relations": n})
        self.log(f"relations: {n}")
        return n

    def reader_pass(self, *, record_job: bool = True) -> dict[str, int]:
        """Pass 4 (ADR-038): read the Chinese like a reader; what a reader cannot follow goes back to the translator."""
        from ..analysis import reader
        from ..llm.schemas import ReaderOutput
        from .review import _retranslate_with_feedback

        job = self._job("reader", self.book.id)
        if record_job and (job is None or job["status"] not in ("pending", "running")):
            return {}
        with self.lock:
            parts = reader.chunks(self.conn, self.book.id, set(self.opts.unit_ids) if self.opts.unit_ids else None)
            cast = reader.characters(self.conn, self.book.id)
        prompt = load_prompt("reader")
        stats = {"read": 0, "issues": 0, "retranslated": 0, "fixed": 0, "still_flagged": 0}
        for part in parts:
            try:
                parsed = self._llm("reader", prompt, {"task": "reader", "paragraphs": part, "characters": cast},
                                   [p["unit_id"] for p in part])
            except FatalAPIError:
                raise
            except LLMError as exc:
                self._finding(None, "reader_failed", "low", str(exc)[:300])
                continue
            assert isinstance(parsed, ReaderOutput)
            stats["read"] += len(part)
            valid = {p["unit_id"] for p in part}
            feedback: dict[str, list[dict[str, str]]] = {}
            for it in parsed.issues:
                if it.unit_id not in valid or it.severity == "low" or not it.problem.strip():
                    continue
                stats["issues"] += 1
                self._finding(it.unit_id, f"review:reader_{it.kind}", it.severity, it.problem[:400])
                feedback.setdefault(it.unit_id, []).append(
                    {"issue_type": f"reader_{it.kind}", "evidence": it.problem, "suggestion": reader.FIX_NOTE})
            if not feedback:
                continue
            with self.lock:
                rows = {r["id"]: r for r in self.conn.execute(
                    f"SELECT * FROM units WHERE id IN ({','.join('?' * len(feedback))})", list(feedback))}
            by_scene: dict[str, list[sqlite3.Row]] = {}
            for uid in feedback:
                if uid in rows:
                    by_scene.setdefault(rows[uid]["scene_id"] or "", []).append(rows[uid])
            for group in by_scene.values():  # one scene at a time: the translation keeps that scene's memory
                _retranslate_with_feedback(self, group, {r["id"]: feedback[r["id"]] for r in group}, stats)
        if record_job:
            self._set_job("reader", self.book.id, "succeeded", None, stats)
        self.log(f"reader's look: {stats}")
        return stats

    def classify_address(self) -> None:
        """``ncte names address-check``: the ADR-037 look at program-matched forms for a book already read."""
        from ..analysis import verify

        with self.lock:
            names = verify._names(self.conn, self.book.id)
            items = verify.address_items(self.conn, self.book.id, names)
        stats: dict[str, int] = {"items": len(items)}
        prompt = load_prompt("verify")
        for i in range(0, len(items), VERIFY_BATCH):
            chunk = items[i:i + VERIFY_BATCH]
            try:
                parsed = self._llm("verify", prompt, {"task": "verify", "items": chunk}, [])
            except FatalAPIError:
                raise
            except LLMError as exc:
                self._finding(None, "verify_failed", "low", str(exc)[:400])
                continue
            assert isinstance(parsed, VerifyOutput)
            with self.lock, Transaction(self.conn):
                for k, v in verify.apply(self.conn, self.book.id, chunk, parsed, self._finding).items():
                    stats[k] = stats.get(k, 0) + v
        self.log(f"address check: {stats}")

    def naming_pass(self) -> bool:
        """Pass 2: propose renderings. False when the run stops here for the user's choices."""
        if self.cfg.context.naming == "off":
            return True
        job = self._job("naming", self.book.id)
        if job is not None and job["status"] in ("pending", "running"):
            self.stage_naming()
        if self.cfg.context.naming == "auto" and not self.opts.unit_ids:
            with self.lock, Transaction(self.conn):
                settled = auto_settle(self.conn, self.book.id)
            self.reload_glossary()
            if any(settled.values()):
                self.log(f"naming (no pause): {settled}")
        if self.cfg.context.naming == "review" and not self.opts.unit_ids:
            with self.lock:
                left = pending(self.conn, self.book.id)
            if left:
                self._stop_reason = "awaiting_names"
                self.log(f"⏸ {len(left)} name/address renderings wait for your choice: ncte names proposals "
                         f"{self.book.id}  →  ncte names decide …  →  ncte resume --run-id {self.run_id}")
                return False
        return True

    def stage_naming(self, *, record_job: bool = True) -> dict[str, int]:
        with self.lock:
            payload = build_naming_payload(self.conn, self.book.id)
        if not payload["names"] and not payload["address"] and not payload.get("characters_to_check"):
            stats = {"names": 0, "address": 0, "pending": 0, "rejected": 0}
        else:
            try:
                parsed = self._llm("naming", self.naming_prompt, model_view(payload), [])
            except FatalAPIError:
                raise
            except LLMError as exc:  # renderings then stay as proposed by the read pass
                if record_job:
                    self._set_job("naming", self.book.id, "failed", f"{type(exc).__name__}: {exc}")
                self._finding(None, "naming_failed", "medium", str(exc)[:400])
                return {"names": 0, "address": 0, "pending": 0, "rejected": 0}
            assert isinstance(parsed, NamingOutput)
            with self.lock, Transaction(self.conn):
                stats = apply_naming(self.conn, self.book.id, payload, parsed, run_id=self.run_id)
            self.reload_glossary()
        if record_job:
            self._set_job("naming", self.book.id, "succeeded", None, stats)
        self.log(f"naming: {stats}")
        return stats

    def propose_names(self) -> None:
        """``ncte names propose``: naming for a volume whose read pass is already done."""
        stats = self.stage_naming(record_job=False)
        self.log(f"proposals: {stats}")

    def scan_genders(self) -> None:
        """``ncte names gender-scan``: for a volume read before genders were recorded, re-ask each analysed scene's
        memory update and keep only the genders (facts, names and summaries stay as they are)."""
        from ..analysis.memory_update import apply_genders
        from ..analysis.scene import scene_rows

        sids = [r[0] for r in self.conn.execute(
            "SELECT s.id FROM scenes s JOIN chapter_summaries c ON c.scene_id=s.id AND c.book_id=s.book_id "
            "WHERE s.book_id=? ORDER BY s.start_ordinal", (self.book.id,))]
        total = 0
        for i, sid in enumerate(sids):
            if self._stop_reason:
                break
            scene = get_scene(self.conn, self.book.id, sid)
            with self.lock:
                payload = build_memory_payload(self.conn, self.book.id, scene,
                                               [{"ja": g["source_term"], "zh": g["target_term"]} for g in self.glossary])
            try:
                parsed = self._llm("memory_update", self.memory_prompt, payload, [u["unit_id"] for u in payload["units"]])
            except FatalAPIError:
                raise
            except LLMError as exc:
                self.log(f"  {sid}: {exc}")
                continue
            rows = scene_rows(self.conn, self.book.id, sid)
            with self.lock, Transaction(self.conn):
                n = apply_genders(self.conn, self.book.id, sid, {r["id"]: r["plain_text"] for r in rows},
                                  {r["id"]: r["ordinal"] for r in rows}, parsed.genders)
            total += n
            self.log(f"gender {sid} ({i + 1}/{len(sids)}): +{n}")
        self.log(f"genders recorded: {total}")

    def translate_pass(self, scenes: list[str]) -> None:
        positions = self.body_positions()
        for i, sid in enumerate(scenes):
            if self._stop_reason:
                break
            pending, _ = self._scene_pending(sid)
            if not pending:
                continue
            self.log(f"translate {sid} ({i + 1}/{len(scenes)})")
            self.reload_glossary()  # a rendering changed while the run goes on is used from the next scene
            self.run_batches(make_batches(pending, self.cfg.translation.unit_target_japanese_chars, by_scene=True,
                                          position=positions), concurrency=1)
            self.after_scene(sid)

    def after_scene(self, sid: str) -> None:
        self.review(self.translated_unit_ids(sid))

    def _llm(self, stage: str, prompt: Any, payload: dict[str, Any], unit_ids: list[str]) -> Any:
        assert self.translator is not None and self.pricing is not None
        out_chars = sum(len(u.get("source", "")) for u in payload.get("units", []))
        est = self._estimate(payload, out_chars)
        self.check_budget(est)
        correction = None
        for attempt in range(2):
            try:
                parsed, _rid, mode = call_structured(
                    self.client, self.recorder, stage=stage, model=self.model, prompt=prompt, payload=payload,
                    mode=self.translator.mode, cfg=self.cfg, unit_ids=unit_ids, est_cost=est, correction=correction,
                    on_downgrade=self._note)
                self.translator.mode = mode
                return parsed
            except ModelOutputError as exc:
                if attempt == 1:
                    raise
                correction = str(exc)
        raise AssertionError("unreachable")

    def _read(self, stage: str, prompt: Any, payload: dict[str, Any], sid: str) -> Any:
        """A read request; when the content filter refuses the scene, read it in halves (``split_read``).

        Units no part could read are reported one by one; None when nothing could be read.
        """
        blocked: list[str] = []
        parsed = self._read_parts(stage, prompt, payload, blocked)
        for uid in blocked:
            self._finding(uid, "read_blocked", "medium",
                          f"{sid} {stage}: the content filter refused this passage; it was not read "
                          "(speakers and memory here come from the program only)")
        if blocked and parsed is not None:
            self.log(f"  {stage}: content filter; read in parts, {len(blocked)} units not read")
        return parsed

    def _read_parts(self, stage: str, prompt: Any, payload: dict[str, Any], blocked: list[str]) -> Any:
        ids = [u["unit_id"] for u in payload["units"]]
        try:
            return self._llm(stage, prompt, payload, ids)
        except ContentBlockedError:
            if len(ids) <= MIN_PART:
                blocked.extend(ids)
                return None
        half = len(ids) // 2
        parts = [p for keep in (ids[:half], ids[half:])
                 if (p := self._read_parts(stage, prompt, subset_payload(payload, keep), blocked)) is not None]
        return merge_outputs(parts) if parts else None

    def stage_scene_analyze(self, sid: str) -> None:
        job = self._job("scene_analyze", sid)
        if job is None or job["status"] not in ("pending", "running"):
            return
        scene = get_scene(self.conn, self.book.id, sid)
        parsed: SceneAnalysisOutput | None = None
        error = None
        if self.cfg.context.scene_analysis:
            with self.lock:
                payload = build_analysis_payload(self.conn, self.book.id, scene,
                                                 [{"ja": g["source_term"], "zh": g["target_term"]} for g in self.glossary])
            try:
                parsed = self._read("scene_analyze", self.analyze_prompt, payload, sid)
                if parsed is None:
                    raise ContentBlockedError("content filter refused every part of the scene")
            except FatalAPIError:
                raise
            except LLMError as exc:
                error = f"{type(exc).__name__}: {exc}"
                self._finding(None, "scene_analysis_failed", "medium", f"{sid}: {error[:300]} (program-only analysis used)")
        with self.lock, Transaction(self.conn):
            stats = apply_scene_analysis(self.conn, self.book.id, scene, parsed, run_id=self.run_id)
        self.reload_glossary()
        self._set_job("scene_analyze", sid, "succeeded" if error is None else "failed", error,
                      {**stats, "mode": "llm" if parsed else "program_only"})
        self.log(f"  analysis: {stats}")

    def stage_memory_update(self, sid: str) -> None:
        job = self._job("memory_update", sid)
        if job is None or job["status"] not in ("pending", "running") or not self.cfg.context.memory_update:
            return
        scene = get_scene(self.conn, self.book.id, sid)
        with self.lock:
            payload = build_memory_payload(self.conn, self.book.id, scene,
                                           [{"ja": g["source_term"], "zh": g["target_term"]} for g in self.glossary])
        try:
            parsed = self._read("memory_update", self.memory_prompt, payload, sid)
            if parsed is None:
                raise ContentBlockedError("content filter refused every part of the scene")
        except FatalAPIError:
            raise
        except LLMError as exc:
            self._set_job("memory_update", sid, "failed", f"{type(exc).__name__}: {exc}")
            self._finding(None, "memory_update_failed", "medium", f"{sid}: {exc}"[:400])
            return
        assert isinstance(parsed, MemoryUpdateOutput)
        try:
            with self.lock, Transaction(self.conn):
                stats = apply_memory_update(self.conn, self.book.id, scene, parsed, run_id=self.run_id)
        except sqlite3.Error as exc:  # memory failure must not affect committed translations
            self._set_job("memory_update", sid, "failed", f"db error: {exc}")
            return
        self.reload_glossary()
        self._set_job("memory_update", sid, "succeeded", None, stats)
        self.log(f"  memory: {stats}")

    # ------------------------------------------------------------ context
    def batch_context(self, batch: Batch) -> dict[str, Any]:
        ctx = super().batch_context(batch)
        if batch.meta or not batch.scene_id or not self.cfg.context.include_memory:
            return ctx
        with self.lock:
            scene = get_scene(self.conn, self.book.id, batch.scene_id)
            mem = memory_context(self.conn, self.book.id, scene=scene, batch_rows=batch.rows,
                                 max_evidence=self.cfg.memory.max_retrieved_evidence)
        ctx.update(mem)
        from ..analysis.narration import for_scene

        with self.lock:  # ADR-040: whose voice tells this scene (the read pass's judgement, else the whole book's)
            zh_names = {r["id"]: r["display_name_zh"] or r["canonical_name"] for r in self.conn.execute(
                "SELECT id, canonical_name, display_name_zh FROM characters WHERE book_id=?", (self.book.id,))}
            who = for_scene(self.conn, self.book.id, scene, zh_names)
        if who is not None:
            ctx["narration"] = who
        if ctx.get("address_terms") and ctx.get("glossary"):
            ctx["glossary"] = drop_address_forms(ctx["glossary"], ctx["address_terms"])
        if "style" in ctx:  # voice notes also for characters present in the scene but not named in the batch
            from ..memory.style import load_style, style_payload

            names = [n for c in mem.get("characters", []) for n in (c["name_ja"], c.get("name_zh") or "")]
            with self.lock:
                style = load_style(self.conn, self.book.id)
            ctx["style"] = style_payload(style, " ".join(r["plain_text"] for r in batch.rows), names) or ctx["style"]
        return ctx

    def target_units(self) -> list[sqlite3.Row]:
        rows = super().target_units()
        return [r for r in rows if r["kind"] not in BODY_KINDS or r["scene_id"] is not None]


def drop_address_forms(glossary: list[dict[str, Any]], address: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One source per form of address in a request: a form with a speaker-specific rendering in ``address_terms``
    (「しまむらちゃん」 → 小島村) does not also go out as an unconfirmed glossary entry (島村醬) that contradicts it.
    Confirmed glossary entries stay (the human chose them)."""
    forms = {a["ja"] for a in address if a.get("zh")}
    return [g for g in glossary if g.get("verified") or g["ja"] not in forms
            or all(a.get("zh") == g.get("zh") for a in address if a["ja"] == g["ja"])]
