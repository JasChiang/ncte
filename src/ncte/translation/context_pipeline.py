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
from ..analysis.scene import apply_scene_analysis, build_analysis_payload, get_scene
from ..errors import FatalAPIError, LLMError, ModelOutputError
from ..llm.prompt_loader import load_prompt
from ..llm.schemas import MemoryUpdateOutput, SceneAnalysisOutput
from ..memory.db import Transaction, utcnow
from ..memory.retrieval import memory_context
from .context_builder import BODY_KINDS
from .orchestrator import Batch, Orchestrator, make_batches
from .translator import call_structured


class ContextOrchestrator(Orchestrator):
    def prepare(self) -> None:
        super().prepare()
        self.analyze_prompt = load_prompt("scene_analyze")
        self.memory_prompt = load_prompt("memory_update")

    # ------------------------------------------------------------ jobs
    def create_extra_jobs(self) -> None:
        scene_ids = [r["scene_id"] for r in self.conn.execute(
            "SELECT DISTINCT u.scene_id, MIN(u.ordinal) o FROM jobs j JOIN units u ON u.id=j.target_id WHERE "
            "j.run_id=? AND j.stage='translate' AND u.scene_id IS NOT NULL GROUP BY u.scene_id ORDER BY o",
            (self.run_id,))]
        for seq, sid in enumerate(scene_ids):
            scene = get_scene(self.conn, self.book.id, sid)
            analyzed = int(scene["version"]) > 0
            has_memory = self.conn.execute("SELECT 1 FROM chapter_summaries WHERE book_id=? AND scene_id=?",
                                           (self.book.id, sid)).fetchone() is not None
            a_status = "pending" if (not analyzed or (self.opts.force and not self.opts.unit_ids)) else "skipped"
            m_status = "pending" if (self.cfg.context.memory_update and (not has_memory or (
                self.opts.force and not self.opts.unit_ids))) else "skipped"
            for stage, st in (("scene_analyze", a_status), ("memory_update", m_status)):
                self.conn.execute(
                    "INSERT INTO jobs(book_id, run_id, stage, target_id, seq, status, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (self.book.id, self.run_id, stage, sid, seq, st, utcnow()))

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
            if self._stop_reason or not self.naming_pass():
                return
            self.translate_pass(scenes)
        else:
            self.interleaved(scenes)
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

    def naming_pass(self) -> bool:
        """Pass 2: propose renderings. False when the run stops here for the user's choices."""
        return True

    def translate_pass(self, scenes: list[str]) -> None:
        positions = self.body_positions()
        for i, sid in enumerate(scenes):
            if self._stop_reason:
                break
            pending, _ = self._scene_pending(sid)
            if not pending:
                continue
            self.log(f"translate {sid} ({i + 1}/{len(scenes)})")
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
                parsed = self._llm("scene_analyze", self.analyze_prompt, payload,
                                   [u["unit_id"] for u in payload["units"]])
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
            parsed = self._llm("memory_update", self.memory_prompt, payload,
                               [u["unit_id"] for u in payload["units"]])
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
