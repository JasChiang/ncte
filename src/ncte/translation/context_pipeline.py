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
from ..analysis.scene import apply_scene_analysis, build_analysis_payload, forget_scene_cards, get_scene
from ..analysis.split_read import MIN_PART, call_in_parts, merge_outputs, subset_payload
from ..errors import ContentBlockedError, FatalAPIError, LLMError, ModelOutputError
from ..llm.prompt_loader import load_prompt
from ..llm.schemas import MemoryUpdateOutput, NamingOutput, SceneAnalysisOutput, VerifyOutput
from ..memory.db import Transaction, utcnow
from ..memory.retrieval import memory_context
from .context_builder import BODY_KINDS
from .orchestrator import Batch, Orchestrator, make_batches, scene_batches
from .translator import call_structured

VERIFY_BATCH = 25  # items per second-look request


# rules that read words, not meaning: what they find is checked with context before it goes to review
CONFIRM_RULES = ("offensive_term", "narration_first_person", "narrator_self_form", "latin_word", "possible_added_name",
                 "second_person_gender", "narrator_name_leak")


class ContextOrchestrator(Orchestrator):
    def prepare(self) -> None:
        super().prepare()
        self.analyze_prompt = load_prompt("scene_analyze")
        self.memory_prompt = load_prompt("memory_update")
        self.naming_prompt = load_prompt("naming")

    def process_batch(self, batch: Batch) -> dict[str, int]:
        counts = super().process_batch(batch)
        from ..analysis.narration import first_person_slip, self_form_slip
        from ..qa import second_person

        third = False
        self_forms: dict[str, str] = {}
        if not batch.meta and batch.scene_id:
            from ..analysis.narration import for_scene

            with self.lock:
                who = for_scene(self.conn, self.book.id, get_scene(self.conn, self.book.id, batch.scene_id))
            third = who is not None and who["person"] == "third"
            if who is not None and who["person"] == "first":
                self_forms = {f["ja"]: f["zh"] for f in who.get("self_forms") or []}

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
                if self_forms and (ev := self_form_slip(r["plain_text"], t["text"], self_forms)):
                    found.append(("narrator_self_form", ev))
                if "敘述者" in t["text"] and not any(w in r["plain_text"] for w in ("語り手", "ナレーター", "叙述")):
                    found.append(("narrator_name_leak", "「敘述者」 is the card's placeholder for the unnamed narrator; "
                                  "in the text the narrator is 「我」"))
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
            # a forced re-read of the whole book chooses the scene breaks again (books cut before ADR-036 too)
            self.choose_scene_breaks(recut=self.opts.force)
        super().ensure_jobs()

    def rebreak(self) -> None:
        """``ncte scenes --rebreak``: the scenes whose range changes are read again by the next run."""
        stats = self.choose_scene_breaks(recut=True)
        with self.lock:
            unread = self.conn.execute("SELECT COUNT(*) FROM scenes WHERE book_id=? AND version=0", (self.book.id,)).fetchone()[0]
        self.log(f"scene breaks chosen again: {stats}; {unread} scenes to read (the next translate run reads them)")

    def choose_scene_breaks(self, *, recut: bool = False) -> dict[str, int]:
        """Before anything is read (ADR-036): the model divides long stretches the author left undivided.

        ``recut``: choose again for a book already read or divided; scenes whose range changes lose what reading them
        left in memory (``segment_book``) and are read again."""
        from ..analysis import scene_breaks as sb
        from ..analysis.scene import segment_book
        from ..llm.schemas import SceneBreaksOutput

        max_chars = self.cfg.translation.scene_max_chars
        with self.lock:
            if recut:
                with Transaction(self.conn):
                    self.conn.execute("DELETE FROM scene_breaks WHERE book_id=?", (self.book.id,))
                segment_book(self.conn, self.cfg, self.book.id)  # the author's markers and size cuts only
            elif self.conn.execute("SELECT 1 FROM scenes WHERE book_id=? AND version>0 LIMIT 1", (self.book.id,)).fetchone() \
                    or self.conn.execute("SELECT 1 FROM scene_breaks WHERE book_id=? LIMIT 1", (self.book.id,)).fetchone():
                return {}  # already read (scene ids are in use) or already divided
            todo = sb.stretches(self.conn, self.book.id, max_chars)
        if not todo:
            return {}
        prompt, size = load_prompt("scenes"), self.cfg.memory.max_scene_units
        stats = {"stretches": len(todo), "breaks": 0, "kept_program_cuts": 0, "asked_again": 0}
        for rows in todo:
            req = sb.payload(rows, size, max_chars)
            try:
                blocked: list[str] = []  # a refused stretch is looked at in halves (breaks stay inside each half)
                parsed = call_in_parts(lambda pl: self._llm("scenes", prompt, pl, [p["unit_id"] for p in pl["paragraphs"]]),
                                       req, "paragraphs", blocked, min_part=8)
                if parsed is None:
                    stats["kept_program_cuts"] += 1
                    continue
                assert isinstance(parsed, SceneBreaksOutput)
                if again := sb.runs_on(rows, [b.unit_id for b in parsed.breaks]):
                    # the paragraph before ends mid-sentence: the model looks once more, and its answer stands
                    stats["asked_again"] += 1
                    parsed = self._llm("scenes", prompt, {**req, "reconsider": {
                        "breaks": again, "why": "這些換場點的前一段沒有結束（以「――」「、」等結尾），句子接到下一段；"
                                                "請確認是否真的在這裡換場，不是的話改選或拿掉"}}, [r["id"] for r in rows])
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
        return stats

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
            self.conn.execute(
                "INSERT INTO jobs(book_id, run_id, stage, target_id, seq, status, updated_at) VALUES (?,?,?,?,?,?,?)",
                (self.book.id, self.run_id, "impressions", self.book.id, len(scene_ids), "pending", utcnow()))
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
            if not self._stop_reason:
                self.impressions_pass()
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
            self.run_batches(self.scene_batches(pending, positions), concurrency=1)
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

    def verify_pass(self, *, record_job: bool = True) -> None:
        """Pass 1b (ADR-033): a second look at what the read pass left in doubt, with the surrounding text."""
        from ..analysis import verify

        job = self._job("verify", self.book.id)
        if record_job and (job is None or job["status"] not in ("pending", "running")):
            return
        with self.lock:
            items = verify.collect(self.conn, self.book.id)
        stats: dict[str, int] = {"items": len(items)}
        prompt = load_prompt("verify")
        for i in range(0, len(items), VERIFY_BATCH):
            chunk = items[i:i + VERIFY_BATCH]
            try:
                blocked: list[str] = []  # the content filter may refuse an item's passages: the rest are asked in halves
                parsed = call_in_parts(lambda pl: self._llm("verify", prompt, pl, []), {"task": "verify", "items": [
                    {k: v for k, v in it.items() if not k.startswith("_")} for it in chunk]}, "items", blocked, min_part=1)
                for ref in blocked:
                    self._finding(None, "verify_blocked", "low", f"the content filter refused the second look at {ref}")
                if parsed is None:
                    continue
            except FatalAPIError:
                raise
            except LLMError as exc:  # the read pass's answers stand
                self._finding(None, "verify_failed", "low", str(exc)[:400])
                continue
            assert isinstance(parsed, VerifyOutput)
            with self.lock, Transaction(self.conn):
                for k, v in verify.apply(self.conn, self.book.id, chunk, parsed, self._finding).items():
                    stats[k] = stats.get(k, 0) + v
        if record_job:
            self._set_job("verify", self.book.id, "succeeded", None, stats)
        self.log(f"second look: {stats}")

    def relations_pass(self, *, record_job: bool = True) -> int:
        """After reading (ADR-039, ADR-045): each pair of characters with enough material is put to the model, a few
        pairs per request; pairs that should be related but came back empty are asked once more."""
        from ..analysis import relations
        from ..llm.schemas import RelationsOutput

        job = self._job("relations", self.book.id)
        if record_job and (job is None or job["status"] not in ("pending", "running")):
            return 0
        with self.lock:
            pairs, chars = relations.candidate_pairs(self.conn, self.book.id)
        prompt = load_prompt("relations")
        results: list[tuple[RelationsOutput, dict[str, Any]]] = []

        def ask(groups: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
            for req, lookup in groups:
                try:
                    blocked: list[str] = []
                    parsed = call_in_parts(lambda pl: self._llm("relations", prompt, pl, []), req, "pairs", blocked,
                                           min_part=1)
                    if blocked:
                        self._finding(None, "relations_blocked", "low",
                                      f"the content filter refused the material of {len(blocked)} pair(s)")
                    if parsed is None:
                        continue
                except FatalAPIError:
                    raise
                except LLMError as exc:  # those pairs keep no relation this time
                    self._finding(None, "relations_failed", "low", str(exc)[:300])
                    continue
                assert isinstance(parsed, RelationsOutput)
                results.append((parsed, lookup))

        ask(relations.requests(pairs, chars))
        if again := relations.unanswered(pairs, results):
            ask(relations.requests(again, chars, recheck=True))
        n = 0
        if results:
            with self.lock:
                n = relations.apply(self.conn, self.book.id, results)
        if record_job:
            self._set_job("relations", self.book.id, "succeeded", None, {"relations": n, "pairs": len(pairs)})
        self.log(f"relations: {n} ({len(pairs)} pairs asked, {len(again)} asked again)")
        return n

    def impressions_pass(self, *, record_job: bool = True) -> int:
        """After relations (ADR-045): personality, way of speaking and place in the story of the main characters."""
        from ..analysis import impressions
        from ..llm.schemas import ImpressionsOutput

        job = self._job("impressions", self.book.id)
        if record_job and (job is None or job["status"] not in ("pending", "running")):
            return 0
        with self.lock:
            groups = impressions.requests(self.conn, self.book.id)
        prompt, results = load_prompt("impressions"), []
        for req, lookup in groups:
            try:
                blocked: list[str] = []
                parsed = call_in_parts(lambda pl: self._llm("impressions", prompt, pl, []), req, "characters", blocked,
                                       min_part=1)
                if parsed is None:
                    continue
            except FatalAPIError:
                raise
            except LLMError as exc:
                self._finding(None, "impressions_failed", "low", str(exc)[:300])
                continue
            assert isinstance(parsed, ImpressionsOutput)
            results.append((parsed, lookup))
        n = 0
        if results:
            with self.lock:
                n = impressions.apply(self.conn, self.book.id, results, self.run_id)
        if record_job:
            self._set_job("impressions", self.book.id, "succeeded", None, {"impressions": n})
        self.log(f"impressions: {n}")
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
            blocked: list[str] = []
            try:  # 120 paragraphs at a time: the content filter may refuse a part; it is then read in halves
                parsed = call_in_parts(lambda pl: self._llm("reader", prompt, pl, [p["unit_id"] for p in pl["paragraphs"]]),
                                       {"task": "reader", "paragraphs": part, "characters": cast}, "paragraphs", blocked)
            except FatalAPIError:
                raise
            except LLMError as exc:
                self._finding(None, "reader_failed", "low", str(exc)[:300])
                continue
            for uid in blocked:
                self._finding(uid, "reader_blocked", "low", "the content filter refused this passage in the reader's look")
            if parsed is None:
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
                blocked: list[str] = []  # renderings of a refused part stay as the read pass proposed them
                parsed = call_in_parts(lambda pl: self._llm("naming", self.naming_prompt, pl, []), model_view(payload),
                                       ("names", "address", "characters_to_check"), blocked, min_part=1)
                if parsed is None:
                    raise ContentBlockedError("the content filter refused every part of the naming request")
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

    def scene_batches(self, pending: list[sqlite3.Row], positions: dict[int, int]) -> list[Batch]:
        if self.cfg.translation.scene_max_chars:  # one request per scene, like a translator reading it whole
            return scene_batches(pending, self.cfg.translation.scene_max_chars, position=positions)
        return make_batches(pending, self.cfg.translation.unit_target_japanese_chars, by_scene=True, position=positions)

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
            self.run_batches(self.scene_batches(pending, positions), concurrency=1)
            self.after_scene(sid)

    def after_scene(self, sid: str) -> None:
        ids = self.translated_unit_ids(sid)
        self.confirm_flags(ids)
        self.review(ids)

    def confirm_flags(self, unit_ids: list[str]) -> dict[str, int]:
        """What simple rules found (a slur, 「我」 in third-person narration…) is checked once more by the model with
        the source and the lines around it; only the real problems go on to review (「阿三」 a maid's name, 「棒子」 a
        stick, 「你一言我一語」 an idiom are left alone). One small request per scene with flags."""
        from ..analysis import reader
        from ..llm.schemas import ConfirmOutput
        from .review import _speakers, _surroundings

        if not unit_ids:
            return {}
        ph = ",".join("?" * len(unit_ids))
        with self.lock:
            found = self.conn.execute(
                f"SELECT f.unit_id, f.type, f.evidence FROM qa_findings f WHERE f.run_id=? AND f.status='open' AND "
                f"f.severity IN ('high','error') AND f.type IN ({','.join('?' * len(CONFIRM_RULES))}) AND f.unit_id IN ({ph})",
                (self.run_id, *CONFIRM_RULES, *unit_ids)).fetchall()
            if not found:
                return {}
            items = []
            for f in found:
                row = self.conn.execute("SELECT * FROM units WHERE id=?", (f["unit_id"],)).fetchone()
                tr = self.conn.execute("SELECT text FROM translations WHERE unit_id=? AND run_id=? ORDER BY id DESC LIMIT 1",
                                       (f["unit_id"], self.run_id)).fetchone()
                items.append({"unit_id": f["unit_id"], "rule": f["type"], "evidence": (f["evidence"] or "")[:300],
                              "source": row["source_text"], "translation": tr["text"] if tr else "",
                              "speakers": _speakers(self.conn, row["id"]),
                              "around": _surroundings(self.conn, self.book.id, [row])})
            cast = reader.characters(self.conn, self.book.id)
        stats = {"checked": len(items), "dismissed": 0}
        blocked: list[str] = []
        try:
            parsed = call_in_parts(lambda pl: self._llm("confirm", load_prompt("confirm"), pl,
                                                        sorted({i["unit_id"] for i in pl["items"]})),
                                   {"task": "confirm", "items": items, "characters": cast}, "items", blocked, min_part=1)
        except FatalAPIError:
            raise
        except LLMError as exc:  # the flags stay and go to review as before
            self._finding(None, "confirm_failed", "low", str(exc)[:300])
            return stats
        if parsed is None:
            return stats
        assert isinstance(parsed, ConfirmOutput)
        flagged = {(f["unit_id"], f["type"]) for f in found}
        with self.lock, Transaction(self.conn):
            for c in parsed.checks:
                if c.real or (c.unit_id, c.rule) not in flagged:
                    continue
                stats["dismissed"] += 1
                self.conn.execute("UPDATE qa_findings SET status='dismissed_by_check', evidence=evidence || ? WHERE "
                                  "run_id=? AND unit_id=? AND type=? AND status='open'",
                                  (f" | check: {c.reason[:200]}", self.run_id, c.unit_id, c.rule))
                if not self.conn.execute("SELECT 1 FROM qa_findings WHERE run_id=? AND unit_id=? AND status='open' AND "
                                         "severity IN ('high','error')", (self.run_id, c.unit_id)).fetchone():
                    self.conn.execute("UPDATE translations SET status='succeeded' WHERE id=(SELECT id FROM translations "
                                      "WHERE unit_id=? AND run_id=? ORDER BY id DESC LIMIT 1) AND status='needs_review'",
                                      (c.unit_id, self.run_id))
        if stats["dismissed"]:
            self.log(f"  rule flags checked: {stats}")
        return stats

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
                mem = self._job("memory_update", sid)
                with Transaction(self.conn):  # a re-read starts without the cards only the last reading of it made
                    if gone := forget_scene_cards(self.conn, self.book.id, scene,
                                                  memory_too=mem is not None and mem["status"] in ("pending", "running")):
                        self.log(f"  re-read: {len(gone)} card(s) only the last reading of this scene made were dropped")
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
