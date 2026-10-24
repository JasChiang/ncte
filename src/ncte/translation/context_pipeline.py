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
import queue
import sqlite3
import threading
from collections.abc import Callable
from typing import Any

from ..analysis.memory_update import apply_memory_update, build_memory_payload
from ..analysis.naming import apply_naming, auto_settle, build_naming_payload, model_view, pending
from ..analysis.scene import apply_scene_analysis, build_analysis_payload, forget_scene_cards, get_scene
from ..analysis.split_read import MIN_PART, _label, call_in_parts, even_parts, merge_outputs, subset_payload
from ..errors import ContentBlockedError, EmptyAnswerError, FatalAPIError, LLMError, ModelOutputError
from ..llm.prompt_loader import load_prompt
from ..llm.schemas import MemoryUpdateOutput, NamingOutput, SceneAnalysisOutput, VerifyOutput
from ..memory.db import Transaction, utcnow
from ..memory.retrieval import memory_context
from ..qa.rules import CONFIRM_RULES, CONFIRMED
from .cache import settle_translation
from .context_builder import BODY_KINDS, shown_context
from .orchestrator import CONFIRM_MODEL_FLAGS, Batch, Orchestrator, make_batches, scene_batches
from .translator import call_structured

VERIFY_BATCH = 25  # items per second-look request
CANDIDATES = "(naming missing terms)"  # the terms step's request for terms the reading left out, when it went unasked


PART_BEFORE = 4  # paragraphs of the part before, given to a later part of a long scene
# what one request of a step after reading came to (``_ask_groups``): the answer (None: nothing), the items the
# content filter refused, the error that left items not asked, and those items
Asked = tuple[Any, list[str], LLMError | None, list[str]]


class ContextOrchestrator(Orchestrator):
    SCENE_START_CONTEXT = True

    def prepare(self) -> None:
        super().prepare()
        self.analyze_prompt = load_prompt("scene_analyze")
        self.memory_prompt = load_prompt("memory_update")
        self.read_prompt = load_prompt("scene_read")
        self.naming_prompt = load_prompt("naming")

    def process_batch(self, batch: Batch) -> dict[str, int]:
        counts = super().process_batch(batch)
        from ..analysis.narration import first_person_slip, self_form_slip
        from ..qa import second_person

        third = False
        self_forms: dict[str, str] = {}
        other_voice: set[str] = set()
        if not batch.meta and batch.scene_id:
            from ..analysis.narration import for_scene

            with self.lock:
                who = for_scene(self.conn, self.book.id, get_scene(self.conn, self.book.id, batch.scene_id))
            third = who is not None and who.get("person") == "third"
            if who is not None and who.get("person") == "first":
                self_forms = {f["ja"]: f["zh"] for f in who.get("self_forms") or []}
            # a stretch the reading found told in another voice (a storyteller, a letter) is not the scene's narration
            other_voice = {u for g in (who or {}).get("segments") or [] for u in g["units"]}

        with self.lock, Transaction(self.conn):
            for r in batch.rows:
                t = self.conn.execute("SELECT id, text FROM translations WHERE unit_id=? AND run_id=? ORDER BY id DESC "
                                      "LIMIT 1", (r["id"], self.run_id)).fetchone()
                if t is None:
                    continue
                found = []
                if ev := second_person.check(self.conn, self.book.id, r, t["text"]):
                    found.append(("second_person_gender", ev))
                if third and r["id"] not in other_voice and first_person_slip(r["plain_text"], t["text"]):
                    found.append(("narration_first_person", "the narration is third person, but this paragraph says 我 "
                                  "outside quotes: use the focal character's name, 他／她, or leave the subject out"))
                if self_forms and r["id"] not in other_voice and \
                        (ev := self_form_slip(r["plain_text"], t["text"], self_forms)):
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
        if fresh and self.cfg.context.scene_breaks == "llm" and not self.opts.unit_ids:
            # a forced re-read of the whole book chooses the scene breaks again (books cut before ADR-036 too); a sample
            # (--limit-units) divides only the stretches it reaches, a later run the rest
            self.choose_scene_breaks(recut=self.opts.force)
        super().ensure_jobs()

    def rebreak(self) -> None:
        """``ncte scenes --rebreak``: the scenes whose range changes are read again by the next run."""
        stats = self.choose_scene_breaks(recut=True)
        with self.lock:
            unread = self.conn.execute("SELECT COUNT(*) FROM scenes WHERE book_id=? AND version=0", (self.book.id,)).fetchone()[0]
        self.log(f"scene breaks chosen again: {stats}; {unread} scenes to read (the next translate run reads them)")

    def sample_end(self) -> int | None:
        """The last paragraph a sample (--limit-units) reaches; None for the whole book."""
        if self.opts.limit is None:
            return None
        rows = self.target_units()
        return max((r["ordinal"] for r in rows), default=None)

    def choose_scene_breaks(self, *, recut: bool = False) -> dict[str, int]:
        """Before anything is read: chapters from the TOC, scenes inside them cut by the model (ADR-057).
        ``recut``: cut again; scenes whose range changes lose what reading them left and are read again."""
        return self.cut_chapters(recut=recut)

    def cut_chapters(self, *, recut: bool = False) -> dict[str, int]:
        """Each chapter the run reaches, cut into scenes by the model in overlapping windows (``scene_windows``);
        book matter (cover, TOC, afterword) keeps the program's cut. A book without a TOC gets its chapters from the
        breaks the model marks as a new chapter."""
        from ..analysis import scene_windows as sw
        from ..analysis.scene import segment_book
        from ..epub.chapters import save_chapters
        from ..llm.schemas import SceneWindowsOutput

        end = self.sample_end()
        with self.lock:
            if self.conn.execute("SELECT 1 FROM chapters WHERE book_id=? LIMIT 1", (self.book.id,)).fetchone() is None:
                segment_book(self.conn, self.cfg, self.book.id)  # imported before chapters came from the TOC
            if recut:
                with Transaction(self.conn):
                    self.conn.execute("DELETE FROM scene_breaks WHERE book_id=?", (self.book.id,))
            chapters = self.conn.execute("SELECT * FROM chapters WHERE book_id=? ORDER BY seq", (self.book.id,)).fetchall()
            decided = {r[0] for r in self.conn.execute("SELECT unit_id FROM scene_breaks WHERE book_id=? AND "
                                                       "origin='chapter'", (self.book.id,))}
            story: set[str] | None = None
            if self.cfg.context.skip_book_matter:
                from ..epub.matter import story_hrefs

                story = story_hrefs(self.conn, self.book.id)
        prompt = load_prompt("scene_windows")
        stats = {"chapters": 0, "windows": 0, "breaks": 0, "unsure": 0, "found_chapters": 0}
        todo: list[tuple[sqlite3.Row, list[sqlite3.Row]]] = []
        for ch in chapters:
            if end is not None and ch["start_ordinal"] > end:
                break
            with self.lock:
                rows = sw.chapter_rows(self.conn, self.book.id, ch)
            if not rows or rows[0]["id"] in decided or (story is not None and not {r["resource_href"] for r in rows} & story):
                continue
            todo.append((ch, rows))

        def cut(i: int) -> tuple[list[tuple[int, Any]], str]:
            """One chapter's windows, in order (each starts from the scene the one before left open). Chapters need
            nothing from each other — cutting reads only the chapter's own paragraphs — so they run side by side."""
            ch, rows = todo[i]

            def ask(pl: dict[str, Any], label: str = ch["label"]) -> SceneWindowsOutput | None:
                with self.lock:
                    stats["windows"] += 1
                try:
                    out = self._llm("scene_windows", prompt, pl, [p["unit_id"] for p in pl["paragraphs"]])
                except FatalAPIError:
                    raise
                except LLMError as exc:
                    self._finding(None, "scene_windows_failed", "low", f"{label}: {exc}"[:300])
                    return None
                assert isinstance(out, SceneWindowsOutput)
                return out

            return sw.cut_chapter(rows, ask, title=ch["label"], known=ch["source"] == "toc",
                                  max_units=self.cfg.memory.max_scene_units)

        def keep(i: int, result: tuple[list[tuple[int, Any]], str]) -> None:
            ch, rows = todo[i]
            breaks, first_kind = result
            with self.lock:
                found = sw.save(self.conn, self.book.id, rows, breaks, first_kind, ch["source"])
                if found and ch["source"] == "none":  # chapters the model found in a book without a TOC
                    starts = [(rows[0]["ordinal"], ch["label"])] + [
                        (r["ordinal"], "") for r in rows if r["id"] in set(found)]
                    with Transaction(self.conn):
                        save_chapters(self.conn, self.book.id, starts, "model")
                        self.conn.executemany("UPDATE scene_breaks SET origin='chapter', reason=? || reason "
                                              "WHERE book_id=? AND unit_id=?",
                                              [(sw.CHAPTER_START["model"] + "：", self.book.id, u) for u in found])
            stats["chapters"] += 1
            stats["breaks"] += len(breaks)
            stats["unsure"] += sum(not b.sure for _, b in breaks)
            stats["found_chapters"] += len(found)

        if self._side_by_side(len(todo), cut, keep, "cutting scenes"):
            self.log(f"scenes cut by chapter (stopped): {stats}")
            return stats  # the chapters not cut yet are cut on resume
        with self.lock:
            segment_book(self.conn, self.cfg, self.book.id)
        self.log(f"scenes cut by chapter: {stats}")
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
        # nothing to read in this run: a whole-book step done since the book was last read is not done again (2.11)
        reading = self.conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND stage IN ('scene_analyze','memory_update') "
                                    "AND status='pending'", (self.run_id,)).fetchone() is not None
        last_read = self.conn.execute("SELECT MAX(updated_at) FROM jobs WHERE book_id=? AND stage IN "
                                      "('scene_analyze','memory_update') AND status='succeeded'",
                                      (self.book.id,)).fetchone()[0]

        def add(stage: str, seq: int) -> None:
            status = "pending"
            cp: dict[str, Any] = {}
            # the step's latest state (a later run that skipped it says nothing about it): one a failed request left
            # partial is not done — due now, asking only what it left; read again, the whole step is (ADR-102)
            last = self.conn.execute("SELECT status, checkpoint_json FROM jobs WHERE book_id=? AND stage=? AND "
                                     "target_id=? AND status!='skipped' ORDER BY id DESC LIMIT 1",
                                     (self.book.id, stage, self.book.id)).fetchone()
            left = json.loads(last["checkpoint_json"] or "{}") if last is not None and last["status"] == "partial" \
                else {}
            if "unasked_ids" in left and not self.opts.force and not reading:
                cp = left
            elif not self.opts.force:
                done = self.conn.execute("SELECT updated_at, checkpoint_json FROM jobs WHERE book_id=? AND stage=? AND "
                                         "target_id=? AND status='succeeded' ORDER BY id DESC LIMIT 1",
                                         (self.book.id, stage, self.book.id)).fetchone()
                if done is not None and stage in ("proofread", "reader", "false_friends"):
                    # only what was translated since the book was last gone over whole
                    upto = json.loads(done["checkpoint_json"] or "{}").get("upto_translation_id")
                    if upto is not None:
                        cp = {"since_translation_id": upto}
                elif done is not None and not reading and (last_read is None or done["updated_at"] >= last_read):
                    status = "skipped"
            self.conn.execute(
                "INSERT INTO jobs(book_id, run_id, stage, target_id, seq, status, checkpoint_json, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.book.id, self.run_id, stage, self.book.id, seq, status, json.dumps(cp), utcnow()))

        if self.cfg.context.read_first and not self.opts.unit_ids:
            for stage in ("verify", "relations", "impressions", "house_style"):
                add(stage, len(scene_ids))
            if self.cfg.context.term_definitions:
                add("terms", len(scene_ids))
            if self.cfg.context.gender_gaps:
                add("gender_gaps", len(scene_ids))
        if self.cfg.context.false_friends and not self.opts.unit_ids:
            add("false_friends", len(scene_ids) + 1)
        if self.cfg.context.proofread and not self.opts.unit_ids:
            add("proofread", len(scene_ids) + 1)
        if self.cfg.context.reader_pass and not self.opts.unit_ids:
            add("reader", len(scene_ids) + 1)
        if self.cfg.context.read_first and self.cfg.context.naming != "off" and not self.opts.unit_ids:
            # naming is a whole-volume step; retranslating chosen units keeps the existing renderings
            add("naming", len(scene_ids))

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

    def _begin_job(self, stage: str) -> dict[str, Any]:
        """A whole-book step starts: its job is running (a run killed now leaves it so, to be done on resume) and what
        an earlier, stopped attempt saved is returned (``done_until_ordinal`` and the counts so far)."""
        with self.lock:
            row = self.conn.execute("SELECT checkpoint_json FROM jobs WHERE run_id=? AND stage=? AND target_id=?",
                                    (self.run_id, stage, self.book.id)).fetchone()
            self.conn.execute("UPDATE jobs SET status='running', updated_at=? WHERE run_id=? AND stage=? AND target_id=?",
                              (utcnow(), self.run_id, stage, self.book.id))
        return json.loads(row["checkpoint_json"] or "{}") if row is not None else {}

    def _step_due(self, stage: str, record_job: bool) -> bool:
        """A whole-book step after reading is due: its job is pending or running, or ``partial`` with items its failed
        requests left unasked (a resumed run asks only those, ADR-102). Not recorded as a job: always."""
        if not record_job:
            return True
        job = self._job(stage, self.book.id)
        if job is not None and job["status"] == "partial":  # partial for what was refused only: nothing to ask again
            return "unasked_ids" in json.loads(job["checkpoint_json"] or "{}")
        return job is not None and job["status"] in ("pending", "running")

    def _end_step(self, stage: str, stats: dict[str, Any], unasked: list[str], record_job: bool,
                  extra: dict[str, Any] | None = None, answered: int | None = None, renew: bool = True) -> None:
        """A whole-book step after reading is done: ``succeeded``, or ``partial`` when a request failed (after the tries
        every request gets, and its items asked once more in halves) and left items unasked, with them in the
        checkpoint (``unasked_ids``) for a resumed run to ask, and nothing else (ADR-102). Not answered is not looked
        at: the step is not done, and the acceptance report says so. What the content filter refused is not unasked:
        it is a known limit, a finding of its own (``*_blocked``), and asking again would be refused again.
        ``answered``: items a resumed run asked again and got answers to (None: the step was done whole, and what an
        earlier run's look at it was refused is history); when the translation has already read the book without
        them, a finding says so (``*_after_translation``) — proofreading reads the book with them, and a new run
        translates with them. The same-form words come after the translation by design: never said of them.
        ``renew``: the step looked at everything (not only what was translated since a mark), so an earlier run's
        refusals are history; otherwise they stay."""
        stats["unasked"] = len(unasked)
        if record_job:
            self._set_job(stage, self.book.id, "partial" if unasked else "succeeded", None,
                          {**stats, **(extra or {}), **({"unasked_ids": unasked} if unasked else {})})
        if answered is None and renew:
            with self.lock:
                self.conn.execute("UPDATE qa_findings SET status='superseded' WHERE book_id=? AND type=? AND "
                                  "status='open' AND run_id!=?", (self.book.id, f"{stage}_blocked", self.run_id))
        elif answered and stage != "false_friends" and self._translated_before():
            # the book's newest translation when they came: a paragraph translated after it had them
            self._finding(None, f"{stage}_after_translation", "low",
                          f"{answered} item(s) of {stage} were answered after the book was translated: the translation "
                          f"did not have them (proofreading reads the book with them; a new run translates with them)",
                          translation_id=self._last_translation_id())
            self.log(f"  {stage}: {answered} item(s) answered after the translation, which did not have them")
        if unasked:
            self.log(f"  {stage}: {len(unasked)} item(s) not asked (the request failed)"
                     + (f": ncte resume --run-id {self.run_id} asks only them" if record_job else ""))

    def _late_answers_translated(self) -> None:
        """Answers a resumed run got after the translation (``*_after_translation``, marked with the book's newest
        translation then) are history once every paragraph's machine translation is newer than that: the book has
        been translated with them. A person's text does not count (it never read the notes); a paragraph translated
        before them keeps them open."""
        from .cache import latest_texts

        with self.lock:
            made = [t["id"] for t in latest_texts(self.conn, self.book.id).values() if t["status"] != "human"]
            if made:
                self.conn.execute("UPDATE qa_findings SET status='superseded' WHERE book_id=? AND status='open' AND "
                                  "unit_id IS NULL AND type LIKE '%\\_after\\_translation' ESCAPE '\\' AND "
                                  "translation_id < ?", (self.book.id, min(made)))

    def _translated_before(self) -> bool:
        """The translation has read the book before now: this run translated some of it, or translates nothing more
        and an earlier run did."""
        with self.lock:
            if self.conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND stage='translate' AND status='succeeded' "
                                 "LIMIT 1", (self.run_id,)).fetchone():
                return True
            if self.conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND stage='translate' AND status IN "
                                 "('pending','running') LIMIT 1", (self.run_id,)).fetchone():
                return False
            return self.conn.execute("SELECT 1 FROM translations t JOIN units u ON u.id=t.unit_id WHERE u.book_id=? "
                                     "LIMIT 1", (self.book.id,)).fetchone() is not None

    def _paused(self, what: str) -> bool:
        """Stopped (paused from the page, or a stop reason set) before the next batch: the job stays running."""
        if self._stop_reason:
            self.log(f"{what}: stopped ({self._stop_reason}); resume goes on from here")
            return True
        return False

    def _side_by_side(self, n: int, work: Callable[[int], Any], done: Callable[[int, Any], None],
                      what: str, halt: threading.Event | None = None) -> bool:
        """Runs ``work(0)…work(n-1)`` up to ``openrouter.max_concurrency`` at a time (ADR-065), started in order, each
        in a daemon thread as ``run_batches`` does; ``done(i, result)`` is called here, in this thread, as each one
        finishes (in any order). A stop (pause) or a failure starts nothing more; what was started is waited for and
        its result kept (``done``), then the failure is raised — ``FatalAPIError``, ``BudgetExceededError`` or anything
        else, the first one. ``halt`` is set on a failure, for a ``work`` that goes through several items to stop at
        the next one. Returns True when stopped before all ``n`` were started."""
        conc = max(1, self.cfg.openrouter.max_concurrency)
        done_q: queue.Queue[tuple[int, Any, BaseException | None]] = queue.Queue()
        active = idx = 0
        failure: BaseException | None = None
        stopped = False

        def worker(i: int) -> None:
            try:
                done_q.put((i, work(i), None))
            except BaseException as exc:  # noqa: BLE001 – forwarded to this thread
                done_q.put((i, None, exc))

        while idx < n or active:
            while active < conc and idx < n and failure is None and not stopped:
                if self._paused(what):
                    stopped = True
                    break
                threading.Thread(target=worker, args=(idx,), daemon=True).start()
                active += 1
                idx += 1
            if active == 0:
                break
            try:
                i, result, exc = done_q.get(timeout=0.25)
            except queue.Empty:
                continue
            active -= 1
            if exc is not None:
                failure = failure or exc
                if halt is not None:
                    halt.set()
                continue
            done(i, result)
        if failure is not None:
            raise failure
        return stopped

    def _ask_groups(self, stage: str, prompt: Any, payloads: list[dict[str, Any]], key: str, what: str
                    ) -> tuple[list[Asked | None], bool]:
        """A post-read step's requests, built from one snapshot of the notes before any of them is asked, sent side by
        side (``max_concurrency``) and each refused one asked in halves (``call_in_parts`` on ``key``). Every answer
        is in the run's record as soon as it arrives; a resumed run sends again only the requests it has no answer
        for (the same snapshot gives the same requests: ``reuse``), and an identical request an earlier run of the book
        got an answer to is not asked again either (ADR-094). A request that failed (after the empty answers asked
        again) has its items asked once more right away, in two halves (one item: as it was) — these answers feed the
        translation, which comes next (ADR-102); a request that answered is sent as it was built. Returns, in the
        requests' order, ``(parsed or None, refused items, the LLM error, items not asked)`` for each one asked (None
        for one not started because of a stop) — items by their label (``split_read._label``), the error only when
        some are not asked — and whether it was stopped. A fatal error is raised once the requests already started
        are back."""
        out: list[Asked | None] = [None] * len(payloads)

        def ask(pl: dict[str, Any], blocked: list[str], failures: list[tuple[dict[str, Any], LLMError]]) -> Any:
            return call_in_parts(lambda p: self._llm(stage, prompt, p, [], reuse=True), pl, key, blocked, min_part=1,
                                 failures=failures)

        def work(i: int) -> Asked:
            blocked: list[str] = []
            failures: list[tuple[dict[str, Any], LLMError]] = []
            got = [p] if (p := ask(payloads[i], blocked, failures)) is not None else []
            if not failures:
                return (got[0] if got else None), blocked, None, []
            # what failed (the request, or a part of it the content filter's halving made), asked once more in
            # halves; what answered is kept and not asked again
            again = [part for pl, _ in failures for items in [pl.get(key) or []]
                     for part in ([items[:(len(items) + 1) // 2], items[(len(items) + 1) // 2:]] if len(items) > 1
                                  else [items])]
            error = failures[-1][1]
            n = sum(len(p) for p in again)
            self.log(f"  {what}: a request failed ({type(error).__name__}); its {n} item(s) not answered asked once "
                     "more" + (" in halves" if len(again) > len(failures) else ""))
            failed: list[str] = []
            for part in again:
                if self._stop_reason:  # paused meanwhile: left for the resumed run, as any item not asked
                    failed += [x for x in map(_label, part) if x not in blocked]
                    continue
                still: list[tuple[dict[str, Any], LLMError]] = []
                if (p := ask({**payloads[i], key: part}, blocked, still)) is not None:
                    got.append(p)
                for pl, exc in still:
                    error = exc
                    failed += [x for x in map(_label, pl.get(key) or []) if x not in blocked]
            blocked[:] = dict.fromkeys(blocked)
            return (merge_outputs(got) if got else None), blocked, error if failed else None, failed

        def done(i: int, r: Asked) -> None:
            out[i] = r

        before = dict(getattr(self.recorder, "reused", {}))
        stopped = self._side_by_side(len(payloads), work, done, what)
        after = getattr(self.recorder, "reused", {})
        earlier = after.get("earlier_run", 0) - before.get("earlier_run", 0)
        if earlier:  # ADR-094: identical requests an earlier run already got answers to were not asked again
            self.log(f"  {what}: {earlier} of {len(payloads)} answers taken from an earlier run (identical request)")
        return out, stopped or any(r is None for r in out)

    def _ask_once_more(self, stage: str, prompt: Any, payload: dict[str, Any], what: str) -> Any:
        """A step's single request (the house style, naming the terms the reading left out): a failure (after the
        empty answers asked again) is asked once more as it was, before the translation reads what it is for
        (ADR-102). A refusal is not: the content filter would refuse it again."""
        try:
            return self._llm(stage, prompt, payload, [])
        except (FatalAPIError, ContentBlockedError):
            raise
        except LLMError as exc:
            self.log(f"  {what}: the request failed ({type(exc).__name__}); asked once more")
        return self._llm(stage, prompt, payload, [])

    def _stretches(self, stage: str, what: str, parts: list[list[dict[str, Any]]],
                   befores: list[list[dict[str, Any]]], one: Callable[..., None], stats: dict[str, Any],
                   after: int | None, done_beyond: list[str], record_job: bool,
                   fresh: Callable[[], dict[str, int]], mark: dict[str, Any] | None = None) -> bool:
        """Proofreading's or the reader's stretches side by side (4.1, ADR-065): ``one(part, before, counts)`` does a
        stretch with its retranslations and their checks into counts of its own, added to ``stats`` when it is done.
        The checkpoint's ``done_until_ordinal`` moves only as far as the stretches done without a gap; a stretch done
        past a gap is kept in ``done_beyond`` (its paragraphs) and not done again on resume; ``mark`` is kept in the
        checkpoint as it is (proofreading's pass, ADR-066); a list in the counts (the paragraphs left without a verdict,
        ADR-098) is joined, not added. Returns True when stopped (paused) before the end."""
        beyond = set(done_beyond)  # paragraphs of stretches done past the gap by an earlier attempt
        todo = [k for k, p in enumerate(parts) if not {x["unit_id"] for x in p} <= beyond]
        finished: set[int] = set()
        at = {x["unit_id"]: x["_at"] for p in parts for x in p}
        until = after
        frontier = 0  # todo[:frontier] are done

        def work(i: int) -> dict[str, Any]:
            counts: dict[str, Any] = dict(fresh())
            k = todo[i]
            one(parts[k], befores[k], counts)
            return counts

        def done(i: int, counts: dict[str, Any]) -> None:
            nonlocal frontier, until
            for key, v in counts.items():
                stats[key] = [*stats.get(key, []), *v] if isinstance(v, list) else stats.get(key, 0) + v
            finished.add(i)
            beyond.update(x["unit_id"] for x in parts[todo[i]])
            while frontier in finished:
                frontier += 1
            if frontier:
                until = parts[todo[frontier - 1]][-1]["_at"]
            if record_job:
                left = sorted(u for u in beyond if until is None or at.get(u, -1) > until)
                self._set_job(stage, self.book.id, "running", None,
                              {**({"done_until_ordinal": until} if until is not None else {}),
                               **({"done_beyond": left} if left else {}), **stats, **(mark or {})})

        return self._side_by_side(len(todo), work, done, what) or len(finished) < len(todo)

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
                self.gender_gaps_pass()  # after the second look has dropped the doubtful inferences
            if not self._stop_reason:
                self.relations_pass()
            if not self._stop_reason:
                self.impressions_pass()
            if not self._stop_reason:
                self.house_style_pass()  # before naming: forms of address follow the honorific policy
            if not self._stop_reason:
                self.terms_pass()  # once everything is read; before naming, which may pause for a person (ADR-069)
            if self._stop_reason or not self.naming_pass():
                return
            self.style_follows_naming()
            self.translate_pass(scenes)
        else:
            self.interleaved(scenes)
        if not self._stop_reason:
            self.review_leftovers()
        if not self._stop_reason:
            self.false_friends_pass()  # the book's first translation is done: its same-form words, before proofreading
        if not self._stop_reason:
            self.proofread_pass()  # against the source first; the reader then reads the corrected Chinese
        if not self._stop_reason:
            self.reader_pass()
        if not self._stop_reason:
            self.run_meta()
        self._late_answers_translated()  # what this run translated and fixed had the answers it was given

    REVIEW_TRIES = 3  # runs a failed review is tried in before it is left for a person

    def review_leftovers(self) -> dict[str, int]:
        """Reviews an earlier run left undone (failed, or interrupted while pending) on paragraphs in this run's range:
        done now, even when nothing is left to translate. A passage the content filter refused is not asked again (it
        stays a finding); a review failed in REVIEW_TRIES runs is left for a person."""
        from collections import Counter

        from .review import review_scope

        if self.cfg.review.automatic_review == "off":
            return {}
        with self.lock:
            scope = {r["id"] for r in self.target_units()}
            latest: dict[str, sqlite3.Row] = {}
            fails: Counter[str] = Counter()
            for r in self.conn.execute("SELECT target_id, status, run_id FROM jobs WHERE book_id=? AND stage='review' "
                                       "ORDER BY id", (self.book.id,)):
                latest[r["target_id"]] = r
                fails[r["target_id"]] += r["status"] == "failed"
            # left pending or running (also by this run, resumed); failed in an earlier run, up to REVIEW_TRIES runs
            due = [u for u, r in latest.items() if u in scope and (
                r["status"] in ("pending", "running")
                or (r["status"] == "failed" and r["run_id"] != self.run_id and fails[u] < self.REVIEW_TRIES))]
        if not due:
            return {}
        stats = review_scope(self, due, any_run=True)
        with self.lock:  # nothing left to review on them now (no flag any more): not pending for ever
            self.conn.executemany("UPDATE jobs SET status='skipped', updated_at=? WHERE book_id=? AND stage='review' AND "
                                  "target_id=? AND run_id!=? AND status IN ('pending','running')",
                                  [(utcnow(), self.book.id, u, self.run_id) for u in due])
        self.log(f"  review left by an earlier run: {stats}")
        return stats

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
            if self._no_story(sid):
                continue
            if self.cfg.context.read_in_one_request and self.stage_scene_read(sid):
                continue
            self.stage_scene_analyze(sid)
            self.stage_memory_update(sid)
        if due:
            self.refresh_readings()

    def _no_story(self, sid: str) -> bool:
        """A scene with no story paragraph (a chapter heading alone, where a person split it off or the chapter has
        nothing else) is not sent to be read: there is nothing to analyse or remember. It gets the program's analysis
        (no lines, no people) and both reading steps are done. A model's cut right after a heading no longer makes
        such a scene (``segment_units``)."""
        with self.lock:
            if self.conn.execute("SELECT 1 FROM units WHERE book_id=? AND scene_id=? AND kind='text' LIMIT 1",
                                 (self.book.id, sid)).fetchone():
                return False
            scene = get_scene(self.conn, self.book.id, sid)
            with Transaction(self.conn):
                apply_scene_analysis(self.conn, self.book.id, scene, None, run_id=self.run_id)
        for stage in ("scene_analyze", "memory_update"):
            job = self._job(stage, sid)
            if job is not None and job["status"] in ("pending", "running"):
                self._set_job(stage, sid, "succeeded", None, {"mode": "no_story"})
        self.log("  no story paragraph (a heading only): not sent to be read")
        return True

    def refresh_readings(self) -> None:
        """The readings the source annotates on each glossary term, collected again (the program's, from the text)."""
        from ..memory.repository import refresh_readings

        with self.lock, Transaction(self.conn):
            refresh_readings(self.conn, self.book.id)
        self.reload_glossary()

    def verify_pass(self, *, record_job: bool = True) -> None:
        """Pass 1b (ADR-033): a second look at what the read pass left in doubt, with the surrounding text."""
        from ..analysis import verify

        if not self._step_due("verify", record_job):
            return
        saved = self._begin_job("verify") if record_job else {}
        only = saved.pop("unasked_ids", None)  # resumed after a partial look: only the items it did not ask
        with self.lock:
            items = verify.collect(self.conn, self.book.id, max_actions=self.cfg.context.verify_action_items)
        if only is not None:
            items = [i for i in items if i["ref"] in set(only)]  # one answered since, or changed, is gone
        stats: dict[str, int] = {**saved, "items": len(items)} if only is None else {**saved, "unasked": 0}
        prompt = load_prompt("verify")
        chunks = [items[i:i + VERIFY_BATCH] for i in range(0, len(items), VERIFY_BATCH)]
        payloads = [{"task": "verify", "items": [{k: v for k, v in it.items() if not k.startswith("_")} for it in chunk]}
                    for chunk in chunks]
        # all asked from the same snapshot, side by side; applied in order once every one is back (a stop applies
        # nothing: the resumed run collects the same items and reuses the answers it has)
        answers, cut = self._ask_groups("verify", prompt, payloads, "items", "second look")
        if cut and self._paused("second look"):
            return
        unasked: list[str] = []
        refused = 0
        went: dict[str, str] = {}
        for chunk, got in zip(chunks, answers, strict=True):
            if got is None:
                continue
            parsed, blocked, exc, failed = got
            refused += len(blocked)
            for ref in blocked:  # the content filter refused an item's passages: the rest were asked in halves
                self._finding(None, "verify_blocked", "low", f"the content filter refused the second look at {ref}")
            if exc is not None:  # the read pass's answers stand; the items were not looked at again (ADR-102)
                self._finding(None, "verify_failed", "medium", f"{len(failed)} item(s) not asked: {str(exc)[:360]}")
                unasked += failed
            if parsed is None:
                continue
            assert isinstance(parsed, VerifyOutput)
            with self.lock, Transaction(self.conn):
                # a merge an earlier batch made is followed by the later ones; an answer left with nothing to do is
                # asked again (ADR-103)
                for k, v in verify.apply(self.conn, self.book.id, chunk, parsed, self._finding, unapplied=unasked,
                                         went=went).items():
                    stats[k] = stats.get(k, 0) + v
        self._end_step("verify", stats, unasked, record_job,
                       answered=None if only is None else len(items) - len(unasked) - refused)
        self.log(f"second look: {stats}")

    def gender_gaps_pass(self, *, record_job: bool = True) -> dict[str, int]:
        """After the second look: characters who matter and still have no gender, asked with sentences spread over
        the book (``analysis.gender_gaps``), on the second look's model. A request the content filter refuses is
        asked in halves, down to one character."""
        from ..analysis import gender_gaps
        from ..llm.schemas import GenderGapsOutput

        if not self.cfg.context.gender_gaps or not self._step_due("gender_gaps", record_job):
            return {}
        saved = self._begin_job("gender_gaps") if record_job else {}
        only = saved.pop("unasked_ids", None)  # resumed after a partial step: only the characters it did not ask
        with self.lock:
            groups = gender_gaps.requests(self.conn, self.book.id, None if only is None else set(only))
        prompt = load_prompt("gender_gaps")
        asked = sum(len(lk["_chars"]) for _, lk in groups)
        stats: dict[str, int] = {"asked": asked, "blocked": 0, "failed": 0} if only is None else \
            {**saved, "failed": 0}
        unasked: list[str] = []
        refused = 0
        results: list[tuple[GenderGapsOutput, dict[str, Any]]] = []
        answers, cut = self._ask_groups("verify:genders", prompt, [req for req, _ in groups], "characters", "genders")
        if cut and self._paused("genders"):  # written all at once: answers so far reused on resume
            return stats
        for (req, lk), got in zip(groups, answers, strict=True):
            if got is None:
                continue
            parsed, blocked, exc, failed = got
            name = {c["ref"]: c["name"] for c in req["characters"]}
            if blocked:  # refused again if asked again: a known limit, not left for a resume (ADR-102)
                self._finding(None, "gender_gaps_blocked", "low", f"內容審查拒絕了 {len(blocked)} 個角色的性別補問"
                              f"（{'、'.join(name.get(r, r) for r in blocked)[:200]}）")
            if exc is not None:  # those characters stay without a gender: said in the report, and the step is partial
                self._finding(None, "gender_gaps_failed", "medium",
                              f"{len(failed)} 個角色沒問到性別（{'、'.join(name.get(r, r) for r in failed)[:200]}）："
                              f"{str(exc)[:200]}")
                stats["failed"] += len(failed)
                unasked += [lk["_chars"][r] for r in failed if r in lk["_chars"]]
            stats["blocked"] = stats.get("blocked", 0) + len(blocked)
            refused += len(blocked)
            if parsed is not None:
                assert isinstance(parsed, GenderGapsOutput)
                results.append((parsed, lk))
        if results:
            with self.lock:
                for k, v in gender_gaps.apply(self.conn, self.book.id, results).items():
                    stats[k] = stats.get(k, 0) + v
        self._end_step("gender_gaps", stats, unasked, record_job,
                       answered=None if only is None else asked - len(unasked) - refused)
        self.log(f"genders asked again: {stats}")
        if stats["failed"]:
            self.log(f"  {stats['failed']} characters were not asked (the request failed): they stay without a gender")
        return stats

    def relations_pass(self, *, record_job: bool = True) -> int:
        """After reading (ADR-039, ADR-045): each pair of characters with enough material is put to the model, a few
        pairs per request; pairs that should be related but came back empty are asked once more."""
        from ..analysis import relations
        from ..llm.schemas import RelationsOutput

        if not self._step_due("relations", record_job):
            return 0
        saved = self._begin_job("relations") if record_job else {}
        only = saved.pop("unasked_ids", None)  # resumed after a partial step: only the pairs it did not ask
        with self.lock:
            pairs, chars = relations.candidate_pairs(self.conn, self.book.id)
        if only is not None:
            pairs = [p for p in pairs if f"{p['a']}|{p['b']}" in set(only)]
        prompt = load_prompt("relations")
        results: list[tuple[RelationsOutput, dict[str, Any]]] = []
        cut: list[bool] = []  # stopped with pairs left unasked
        failed: set[str] = set()  # pairs of a request that failed, …
        answered: set[str] = set()  # … unless a later request about them (asked again) answered
        refused: set[str] = set()  # … or refused (a known limit, in either round: never unasked)

        def ask(groups: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
            answers, stopped = self._ask_groups("relations", prompt, [req for req, _ in groups], "pairs", "relations")
            for (_req, lookup), got in zip(groups, answers, strict=True):
                if got is None:
                    continue
                parsed, blocked, exc, left = got
                if blocked:
                    self._finding(None, "relations_blocked", "low",
                                  f"the content filter refused the material of {len(blocked)} pair(s)")
                ids = {k: "|".join(v) for k, v in lookup["_pairs"].items()}
                refused.update(ids[k] for k in blocked if k in ids)  # by the pair: the codes differ in the recheck
                if exc is not None:  # those pairs keep no relation this time
                    self._finding(None, "relations_failed", "medium",
                                  f"{len(left)} pair(s) not asked: {str(exc)[:260]}")
                    failed.update(ids[k] for k in left if k in ids)
                if parsed is None:
                    continue
                assert isinstance(parsed, RelationsOutput)
                answered.update(v for k, v in ids.items() if k not in blocked and k not in left)
                results.append((parsed, lookup))
            if stopped:
                cut.append(True)

        from ..memory.repository import character_basics

        basics = {cid: character_basics(self.conn, cid) for cid in chars}
        ask(relations.requests(pairs, chars, basics=basics))
        again = [] if cut else relations.unanswered(pairs, results)
        if again:
            ask(relations.requests(again, chars, recheck=True, basics=basics))
        if cut and self._paused("relations"):  # put together from all pairs at once: answers so far reused
            return 0
        n = 0
        stats: dict[str, int] = {}
        if results:
            with self.lock:
                n = relations.apply(self.conn, self.book.id, results, stats=stats,
                                    only=None if only is None else [(p["a"], p["b"]) for p in pairs])
        counts = {"relations": n, "pairs": len(pairs), **stats} if only is None else \
            {**saved, "relations": saved.get("relations", 0) + n, "pairs_asked_again": len(pairs)}
        self._end_step("relations", counts, sorted(failed - answered - refused), record_job,
                       answered=None if only is None else len(answered))
        missing = stats.get("known_from_missing", 0)
        self.log(f"relations: {n} ({len(pairs)} pairs asked, {len(again)} asked again"
                 + (f"; {missing} without where they can be known: shown from their latest evidence" if missing else "")
                 + ")")
        return n

    def house_style_pass(self, *, record_job: bool = True) -> dict[str, int]:
        """After the impressions (ADR-053): honorific policy, the main characters' speech habits and house rules."""
        from ..analysis import house_style
        from ..llm.schemas import HouseStyleOutput

        if not self._step_due("house_style", record_job):  # partial: its one request failed, asked again whole
            return {}
        job = self._job("house_style", self.book.id) if record_job else None
        resumed = job is not None and "unasked_ids" in json.loads(job["checkpoint_json"] or "{}")
        parsed = None
        with self.lock:
            req, refs = house_style.request(self.conn, self.book.id)
            seen = house_style.names_seen(self.conn, self.book.id)  # naming may change them afterwards (2.12)
        stats: dict[str, int] = {}
        unasked: list[str] = []
        if req["characters"]:
            try:
                parsed = self._ask_once_more("house_style", load_prompt("house_style"), req, "house style")
            except FatalAPIError:
                raise
            except ContentBlockedError as exc:  # refused again if asked again: a known limit (ADR-102)
                self._finding(None, "house_style_blocked", "low", f"the content filter refused the house style: "
                              f"{str(exc)[:240]}")
                parsed = None
            except LLMError as exc:
                self._finding(None, "house_style_failed", "medium", str(exc)[:300])
                parsed = None
                unasked = ["house_style"]
            if parsed is not None:
                assert isinstance(parsed, HouseStyleOutput)
                with self.lock, Transaction(self.conn):
                    stats = house_style.apply(self.conn, self.book.id, parsed, refs)
        self._end_step("house_style", stats, unasked, record_job, extra={"names_seen": seen},
                       answered=int(parsed is not None) if resumed else None)
        self.log(f"house style: {stats}")
        return stats

    def terms_pass(self, *, record_job: bool = True) -> dict[str, int]:
        """After reading (ADR-069): the recurring terms of the glossary, with example sentences and the facts that
        mention them (found by string match), put to the model a few dozen at a time; it says what each means in this
        book, citing them. A request the content filter refuses is asked in halves, down to one term."""
        from ..analysis import terms
        from ..llm.schemas import TermsOutput

        if not self.cfg.context.term_definitions or not self._step_due("terms", record_job):
            return {}
        saved = self._begin_job("terms") if record_job else {}
        only = saved.pop("unasked_ids", None)  # resumed after a partial step: only what it did not ask
        unasked: list[str] = []
        named: list[str] = []
        named_asked = 0  # the request for missing terms, answered (refused, failed or not needed: not)
        if self.cfg.context.term_candidates and (only is None or CANDIDATES in only):
            # ADR-073: terms the reading left out of the glossary
            with self.lock:
                ask = terms.candidate_request(self.conn, self.book.id)
            if ask is not None:
                try:
                    got = self._ask_once_more("terms", load_prompt("terms_candidates"), ask[0], "naming missing terms")
                    with self.lock:
                        named = terms.take_candidates(self.conn, self.book.id, got, ask[1])
                    named_asked = 1
                except FatalAPIError:
                    raise
                except ContentBlockedError as exc:  # refused again if asked again: a known limit (ADR-102)
                    self._finding(None, "terms_blocked", "low", f"the content filter refused naming missing terms: "
                                  f"{str(exc)[:240]}")
                except LLMError as exc:  # the glossary's terms are still defined
                    self._finding(None, "terms_failed", "medium", f"naming missing terms: {str(exc)[:280]}")
                    unasked.append(CANDIDATES)
        with self.lock:
            groups = terms.requests(self.conn, self.book.id, [*named, *(t for t in only or [] if t != CANDIDATES)],
                                    only=None if only is None else {*named, *only})
        prompt = load_prompt("terms")
        asked = sum(len(lk["_terms"]) for _, lk in groups)
        stats: dict[str, int] = {"named": len(named), "asked": asked, "blocked": 0, "failed": 0} if only is None else \
            {**saved, "named": saved.get("named", 0) + len(named), "failed": 0}
        results: list[tuple[TermsOutput, dict[str, Any]]] = []
        refused = 0
        answers, cut = self._ask_groups("terms", prompt, [req for req, _ in groups], "terms", "terms")
        for (_req, lk), got in zip(groups, answers, strict=True):
            if got is None:
                continue
            parsed, blocked, err, failed = got
            if err is not None:  # those terms stay as they were
                self._finding(None, "terms_failed", "medium", f"{len(failed)} term(s) not asked: {str(err)[:260]}")
                stats["failed"] += len(failed)
                unasked += [lk["_terms"][ref] for ref in failed if ref in lk["_terms"]]
            for ref in blocked:
                self._finding(None, "terms_blocked", "low",
                              f"the content filter refused the material of 「{lk['_terms'].get(ref, ref)}」")
            stats["blocked"] = stats.get("blocked", 0) + len(blocked)
            refused += len(blocked)
            if parsed is not None:  # what was not asked (refused, or its half failed) keeps its definitions
                assert isinstance(parsed, TermsOutput)
                results.append((parsed, {**lk, "_blocked": {*blocked, *failed}}))
        if cut and self._paused("terms"):  # written all at once: answers so far reused on resume
            return stats
        if results:
            with self.lock:
                for k, v in terms.apply(self.conn, self.book.id, results, self.run_id).items():
                    stats[k] = stats.get(k, 0) + v
        self._end_step("terms", stats, unasked, record_job, answered=None if only is None else
                       asked + named_asked - len([t for t in unasked if t != CANDIDATES]) - refused)
        self.log(f"terms: {stats}")
        return stats

    def style_follows_naming(self) -> dict[str, str]:
        """After naming: a name the house style wrote as it was then, rendered differently now, is replaced in the
        style's voices (2.12). What it saw is then the current glossary, so a resumed run does not replace again."""
        from ..analysis import house_style

        job = self._job("house_style", self.book.id)
        if job is None or job["status"] != "succeeded":
            return {}
        cp = json.loads(job["checkpoint_json"] or "{}")
        if not cp.get("names_seen"):
            return {}
        with self.lock, Transaction(self.conn):
            changed = house_style.follow_renames(self.conn, self.book.id, cp["names_seen"])
            if changed:
                cp["names_seen"] = house_style.names_seen(self.conn, self.book.id)
                self.conn.execute("UPDATE jobs SET checkpoint_json=? WHERE id=?", (json.dumps(cp, ensure_ascii=False),
                                                                                    job["id"]))
        if changed:
            self.log(f"house style follows naming: {changed}")
        return changed

    def impressions_pass(self, *, record_job: bool = True) -> int:
        """After relations (ADR-045): personality, way of speaking and place in the story of the main characters."""
        from ..analysis import impressions
        from ..llm.schemas import ImpressionsOutput

        if not self._step_due("impressions", record_job):
            return 0
        saved = self._begin_job("impressions") if record_job else {}
        only = saved.pop("unasked_ids", None)  # resumed after a partial step: only the characters it did not ask
        with self.lock:
            groups = impressions.requests(self.conn, self.book.id, None if only is None else set(only))
        prompt, results = load_prompt("impressions"), []
        unasked: list[str] = []
        refused = 0
        answers, cut = self._ask_groups("impressions", prompt, [req for req, _ in groups], "characters", "impressions")
        for (req, lookup), got in zip(groups, answers, strict=True):
            if got is None:
                continue
            parsed, blocked, exc, failed = got
            name = {c["ref"]: c["name"] for c in req["characters"]}
            if blocked:  # refused again if asked again: a known limit, not left for a resume (ADR-102)
                self._finding(None, "impressions_blocked", "low", f"the content filter refused the impressions of "
                              f"{'、'.join(name.get(r, r) for r in blocked)[:240]}")
                refused += len(blocked)
            if exc is not None:  # those characters keep the impressions they had
                self._finding(None, "impressions_failed", "medium",
                              f"{len(failed)} character(s) not asked: {str(exc)[:260]}")
                unasked += [lookup["_chars"][r] for r in failed if r in lookup["_chars"]]
            if parsed is None:
                continue
            assert isinstance(parsed, ImpressionsOutput)
            results.append((parsed, lookup))
        if cut and self._paused("impressions"):  # written all at once: answers so far reused on resume
            return 0
        n = 0
        if results:
            with self.lock:
                n = impressions.apply(self.conn, self.book.id, results, self.run_id)
        self._end_step("impressions", {**saved, "impressions": saved.get("impressions", 0) + n}, unasked, record_job,
                       answered=None if only is None else
                       sum(len(lk["_chars"]) for _, lk in groups) - len(unasked) - refused)
        self.log(f"impressions: {n}")
        return n

    def reader_pass(self, *, record_job: bool = True) -> dict[str, int]:
        """Pass 4 (ADR-038): read the Chinese like a reader; what a reader cannot follow goes back to the translator.
        Recorded as a job, it keeps how far it got after each stretch (with its retranslations and their checks):
        paused or killed, the resumed run reads on from there."""
        from ..analysis import reader
        from .review import new_stats

        job = self._job("reader", self.book.id)
        if record_job and (job is None or job["status"] not in ("pending", "running")):
            return {}
        saved = self._begin_job("reader") if record_job else {}
        after = saved.pop("done_until_ordinal", None)
        beyond = saved.pop("done_beyond", [])  # stretches done past a gap by a stopped attempt (4.1)
        wanted = set(self.opts.unit_ids) if self.opts.unit_ids else None
        with self.lock:
            story = self._story_files()
            parts = reader.chunks(self.conn, self.book.id, wanted, after=after, story=story,
                                  since=saved.get("since_translation_id"))
            prev = reader.tail(self.conn, self.book.id, after, wanted, story=story) if after is not None else []
        prompt = load_prompt("reader")
        stats = {**new_stats(read=0, blocked=0, failed=0), **saved}
        befores = []
        for part in parts:
            # the end of the stretch before, in the same chapter: read to follow the story, not reported on
            befores.append([p for p in prev[-reader.BEFORE_UNITS:] if p["_href"] == part[0]["_href"]])
            prev = part
        if self._stretches("reader", "reader's look", parts, befores,
                           lambda part, before, counts: self._reader_part(part, prompt, counts, before),
                           stats, after, beyond, record_job, lambda: new_stats(read=0, blocked=0, failed=0)):
            return stats
        if record_job:  # a stretch refused or failed was not read: the pass is partial, not done
            self._set_job("reader", self.book.id, self._pass_status(stats), None,
                          {**stats, "upto_translation_id": self._last_translation_id()})
        self.log(f"reader's look: {stats}")
        return stats

    def _last_translation_id(self) -> int:
        """The newest translation of this book: a pass done whole looks next time only at what is newer (2.11)."""
        with self.lock:
            return int(self.conn.execute("SELECT COALESCE(MAX(t.id), 0) FROM translations t JOIN units u ON "
                                         "u.id=t.unit_id WHERE u.book_id=?", (self.book.id,)).fetchone()[0])

    def _story_files(self) -> set[str] | None:
        """The files proofreading and the reader's look go through: the story only (ADR-024) unless book matter is
        asked for too."""
        if not self.cfg.context.skip_book_matter:
            return None
        from ..epub.matter import story_hrefs

        return story_hrefs(self.conn, self.book.id)

    def _pass_status(self, stats: dict[str, int]) -> str:
        # a stretch refused or failed was not looked at, nor a paragraph left without the verdict asked for (ADR-055);
        # with chosen paragraphs only, the book was not looked at whole
        return "partial" if stats["blocked"] or stats["failed"] or stats.get("no_verdict") or self.opts.unit_ids \
            else "succeeded"

    def _reader_part(self, part: list[dict[str, Any]], prompt: Any, stats: dict[str, int],
                     before: list[dict[str, Any]] | None = None) -> None:
        from ..analysis import reader
        from ..llm.schemas import ReaderOutput
        from .review import in_conflict

        blocked: list[str] = []
        with self.lock:
            cast = reader.characters(self.conn, self.book.id, min(p["_at"] for p in part), max(p["_at"] for p in part))
        try:  # 120 paragraphs at a time: the content filter may refuse a part; it is then read in halves
            parsed = call_in_parts(lambda pl: self._llm("reader", prompt, pl, [p["unit_id"] for p in pl["paragraphs"]]),
                                   {"task": "reader", "paragraphs": [{k: v for k, v in p.items() if not k.startswith("_")}
                                                                     for p in part], "characters": cast,
                                    **({"context_before": [{"zh": b["zh"]} for b in before]} if before else {})},
                                   "paragraphs", blocked)
        except FatalAPIError:
            raise
        except LLMError as exc:
            self._finding(None, "reader_failed", "low", str(exc)[:300])
            stats["failed"] += len(part)
            return
        tids = {p["unit_id"]: p["_tid"] for p in part}
        for uid in blocked:
            self._finding(uid, "reader_blocked", "low", "the content filter refused this passage in the reader's look",
                          translation_id=tids.get(uid))
        stats["blocked"] += len(blocked)
        if parsed is None:
            return
        assert isinstance(parsed, ReaderOutput)
        valid = {p["unit_id"] for p in part} - set(blocked)  # what the reader really read
        stats["read"] += len(valid)
        _outlived(self, stats, {u: tids[u] for u in valid}, parsed.issues, "reader", ("review:reader_%",))
        conflicts = in_conflict(self, valid)
        feedback: dict[str, list[dict[str, Any]]] = {}
        for it in parsed.issues:
            if it.unit_id not in valid or it.severity == "low" or not it.problem.strip():
                continue
            if (it.unit_id, f"review:reader_{it.kind}") in conflicts:  # came back unchanged earlier in this run
                stats["conflict_skipped"] = stats.get("conflict_skipped", 0) + 1
                continue
            stats["issues"] += 1
            fid = self._finding(it.unit_id, f"review:reader_{it.kind}", it.severity, it.problem[:400],
                                translation_id=tids[it.unit_id])
            feedback.setdefault(it.unit_id, []).append(
                {"issue_type": f"reader_{it.kind}", "evidence": it.problem, "suggestion": reader.FIX_NOTE,
                 "finding_id": fid})
        if feedback:
            self._fix_found(feedback, stats)

    def _fix_found(self, feedback: dict[str, list[dict[str, Any]]], stats: dict[str, Any], *,
                   side_by_side: bool = False) -> None:
        """What one checker's batch found (a stretch, the same-form words), back to the translator and then checked
        against the source (ADR-055) all together: retranslated a scene at a time — each keeps that scene's memory —
        and the fixes of every scene put to the fix check in as few requests as its batch allows (ADR-102).
        ``side_by_side``: the scenes are retranslated at once, up to the run's concurrency (a step on its own; a
        proofreading or reader's stretch is already one of several side by side and asks its scenes in turn)."""
        from .review import _retranslate_with_feedback

        with self.lock:
            rows = sorted(self.conn.execute(f"SELECT * FROM units WHERE id IN ({','.join('?' * len(feedback))})",
                                            list(feedback)), key=lambda r: r["ordinal"])

        def run(n: int, work: Callable[[int], Any], done: Callable[[int, Any], None]) -> bool:
            return self._side_by_side(n, work, done, "fixes")

        _retranslate_with_feedback(self, rows, {r["id"]: feedback[r["id"]] for r in rows}, stats,
                                   side_by_side=run if side_by_side else None)

    def false_friends_pass(self, *, record_job: bool = True) -> dict[str, int]:
        """After the first translation of the book (``analysis.false_friends``): kanji words carried over unchanged are
        put to the translating model; one that reads otherwise in Taiwan Chinese gets a glossary entry and every
        paragraph that carried it goes back to the translator with the suggestion, each fix checked against the source.
        Recorded as a job, a later run looks only at what was translated since (2.11). ADR-102: the requests about the
        words are sent side by side, as the steps after reading send theirs (the same requests: an answer kept for one
        is not asked again, ADR-094), the scenes' fixes are retranslated side by side and checked together; a request
        that failed leaves the step partial with its words listed, and a resumed run asks only those."""
        from ..analysis import false_friends as ff
        from ..llm.schemas import FalseFriendsOutput
        from .review import in_conflict, new_stats

        if not self.cfg.context.false_friends or not self._step_due("false_friends", record_job):
            return {}
        saved = self._begin_job("false_friends") if record_job else {}
        only = saved.pop("unasked_ids", None)  # resumed after a partial step: only the words it did not ask
        since = saved.get("since_translation_id")
        with self.lock:
            words = ff.collect(self.conn, self.book.id, self._story_files(), since)
        if only is not None:
            words = [w for w in words if w["ja"] in set(only)]
        prompt = load_prompt("false_friends")
        stats: dict[str, int] = {**new_stats(words=len(words), blocked=0, failed=0)} if only is None else \
            {**saved, "failed": 0}
        unasked: list[str] = []
        results: list[tuple[FalseFriendsOutput, dict[str, Any]]] = []
        groups = ff.requests(words)
        answers, cut = self._ask_groups("false_friends", prompt, [req for req, _ in groups], "words", "same-form words")
        if cut and self._paused("same-form words"):  # nothing kept yet: the answers so far are reused on resume
            return stats
        for (_req, lk), got in zip(groups, answers, strict=True):
            if got is None:
                continue
            parsed, blocked, exc, failed = got
            if blocked:  # refused again if asked again: a known limit, not left for a resume (ADR-102)
                self._finding(None, "false_friends_blocked", "low", "the content filter refused the same-form words "
                              + "、".join(lk["_words"][r]["ja"] for r in blocked if r in lk["_words"])[:240])
            if exc is not None:  # those words stay as translated; proofreading still reads them
                self._finding(None, "false_friends_failed", "medium",
                              f"{len(failed)} word(s) not asked: {str(exc)[:260]}")
                stats["failed"] += len(failed)
                unasked += [lk["_words"][r]["ja"] for r in failed if r in lk["_words"]]
            stats["blocked"] = stats.get("blocked", 0) + len(blocked)
            if parsed is not None:
                assert isinstance(parsed, FalseFriendsOutput)
                results.append((parsed, lk))
        differs: list[dict[str, Any]] = []
        if results:
            with self.lock:
                found, differs = ff.apply(self.conn, self.book.id, results)
            for k, v in found.items():
                stats[k] = stats.get(k, 0) + v
            self.reload_glossary()  # the fixes are translated with the new entries
        feedback: dict[str, list[dict[str, Any]]] = {}
        hits = {h["unit_id"] for w in differs for h in w["hits"]}
        conflicts = in_conflict(self, hits)
        for w in differs:
            for h in w["hits"]:
                if (h["unit_id"], "review:false_friend") in conflicts:  # came back unchanged earlier in this run
                    stats["conflict_skipped"] = stats.get("conflict_skipped", 0) + 1
                    continue
                stats["issues"] += 1
                fid = self._finding(h["unit_id"], "review:false_friend", "medium",
                                    f"「{w['zh']}」({w['ja']}): {w['reason'][:240]} → {w['suggestion']}",
                                    translation_id=h["tid"])
                feedback.setdefault(h["unit_id"], []).append(
                    {"issue_type": "false_friend",
                     "evidence": f"譯文照搬了原文的「{w['ja']}」，寫成「{w['zh']}」：{w['reason']}",
                     "suggestion": f"{w['suggestion']}\n{ff.FIX_NOTE}", "finding_id": fid})
        if feedback:
            # the scenes side by side, each keeping its memory; what was written is checked even when paused meanwhile
            self._fix_found(feedback, stats, side_by_side=True)
            if self._paused("same-form words"):  # the words fixed so far are no longer carried over on resume
                return stats
        # resumed after a partial step: what the step looked at whole is as far as it went then
        upto = saved.get("upto_translation_id") if only is not None else None
        self._end_step("false_friends", stats, unasked, record_job,
                       extra={"upto_translation_id": upto if upto is not None else self._last_translation_id(),
                              **({"since_translation_id": since} if since is not None else {})},
                       answered=None if only is None else len(words) - len(unasked), renew=since is None)
        self.log(f"same-form words: {stats}")
        return stats

    def proofread_pass(self, *, record_job: bool = True) -> dict[str, Any]:
        """Pass 3b (ADR-051): every paragraph against its source; real errors go back to the translator. Recorded as
        a job, it keeps how far it got after each stretch (with its retranslations and their checks): paused or
        killed, the resumed run checks on from there. With more than one pass (``config.proofread_passes``: the
        setting, else 2 on the Claude plan and 1 elsewhere; ADR-066), each later pass checks the whole current
        translation again once the pass before has retranslated and checked its fixes;
        the checkpoint also keeps which pass it is in (``pass``) and each finished pass's counts (``passes``), and
        the job's counts are the passes added up."""
        from .review import new_stats

        job = self._job("proofread", self.book.id)
        if record_job and job is not None and job["status"] == "partial":  # resumed: only what had no verdict (ADR-098)
            return self._fill_unanswered(json.loads(job["checkpoint_json"] or "{}"))
        if record_job and (job is None or job["status"] not in ("pending", "running")):
            return {}
        from ..config import proofread_passes

        saved = self._begin_job("proofread") if record_job else {}
        n = proofread_passes(self.cfg)
        k = int(saved.pop("pass", 1))
        done: list[dict[str, Any]] = list(saved.pop("passes", []))
        since = saved.get("since_translation_id")  # an earlier whole-book pass: only what is newer (2.11)
        prompt = load_prompt("proofread")
        from ..config import CROSS_PROOFREAD

        cross = self.cfg.review.cross_proofread_model
        while True:
            mark = {"pass": k, "passes": done} if n > 1 else {}
            # ADR-082: the last pass reads with another company's eyes when chosen (fixes stay on the run's model)
            self._proofread_stage = CROSS_PROOFREAD if cross and k == n else "proofread"
            stats, stopped = self._proofread_once(saved, prompt, record_job, mark)
            if stopped:
                return stats
            if k >= n:
                break
            done = [*done, _one_pass(stats)]
            k += 1
            saved = {**new_stats(checked=0, blocked=0, failed=0),
                     **({"since_translation_id": since} if since is not None else {})}
            if record_job:  # the next pass begins: a run stopped now starts it again on resume, not the one before
                self._set_job("proofread", self.book.id, "running", None, {**saved, "pass": k, "passes": done})
            self.log(f"proofreading pass {k - 1} of {n}: {done[-1]}; pass {k} checks the whole current translation")
        if n > 1:
            passes = [*done, _one_pass(stats)]
            stats = {**_added_up(passes), **({"since_translation_id": since} if since is not None else {}),
                     "passes": passes}
        if record_job:  # a stretch refused or failed was not checked: the pass is partial, not done
            self._set_job("proofread", self.book.id, self._pass_status(stats), None,
                          {**stats, "upto_translation_id": self._last_translation_id()})
        self.log(f"proofreading: {_counts(stats)}")
        if record_job and (left := sum(map(len, _unanswered(stats)))):
            self.log(f"  {left} paragraph(s) without a verdict: ncte resume --run-id {self.run_id} asks only them again")
        return stats

    def _fill_unanswered(self, cp: dict[str, Any]) -> dict[str, Any]:
        """ADR-098: a resumed run whose proofreading ended partial sends again only the paragraphs a pass left without
        a verdict (``no_verdict_ids``), each pass's own on that pass's step (the last on the cross model when chosen),
        in pass order, against the current translation; the paragraphs with a verdict are not asked again. They go
        through the same flow as a stretch (asked again alone once, fixes retranslated and checked against the source,
        ADR-055), with the paragraphs before and after them to read. What comes back with a verdict is added to its pass; what still has none stays listed with a record
        of its own (one more try on each resume, never a loop in one run); a request that failed or was not sent
        because of a stop leaves its paragraphs listed. The pass is complete when nothing is left (nor refused or
        failed before). The later passes' counts and ``upto_translation_id`` stay as they were."""
        from ..analysis import proofread
        from ..config import CROSS_PROOFREAD
        from .review import new_stats

        passes = [dict(p) for p in cp["passes"]] if "passes" in cp else [dict(cp)]
        if not any(p.get("no_verdict_ids") for p in passes):
            return {}
        prompt = load_prompt("proofread")
        cross = self.cfg.review.cross_proofread_model
        with self.lock:
            story = self._story_files()
            before_max = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM qa_findings").fetchone()[0]
        asked: set[str] = set()  # sent again and answered for (a verdict, a refusal, or still none: a new record)
        stopped = False
        for k, p in enumerate(passes, 1):
            gaps = set(p.get("no_verdict_ids") or [])
            if not gaps or stopped:
                continue
            self._proofread_stage = CROSS_PROOFREAD if cross and k == len(passes) else "proofread"
            with self.lock:
                parts, befores, afters = self._gap_stretches(proofread, gaps, story)
            came: set[str] = set()
            following = {id(part): a for part, a in zip(parts, afters, strict=True)}

            def one(part: list[dict[str, Any]], before: list[dict[str, Any]], counts: dict[str, Any],
                    came: set[str] = came, following: dict[int, Any] = following) -> None:
                self._proofread_part(part, before, prompt, counts, following[id(part)])
                if counts["failed"]:  # not looked at: still without a verdict, asked on the next resume
                    counts["failed"] = 0
                    return
                with self.lock:
                    came.update(x["unit_id"] for x in part)

            got: dict[str, Any] = new_stats(checked=0, blocked=0, failed=0)
            stopped = self._stretches("proofread", "proofreading (paragraphs without a verdict)", parts, befores, one,
                                      got, None, [], False, lambda: new_stats(checked=0, blocked=0, failed=0))
            left = (gaps - came) | set(got.get("no_verdict_ids", []))
            for key, v in got.items():  # sent already in this pass: only what came back is added
                if isinstance(v, int) and key not in ("sent", "no_verdict", "failed"):
                    p[key] = p.get(key, 0) + v
            p["verdicts_filled"] = p.get("verdicts_filled", 0) + got["checked"]
            p["no_verdict"] = len(left)
            if left:
                p["no_verdict_ids"] = sorted(left)
            else:
                p.pop("no_verdict_ids", None)
            asked |= came
            self.log(f"proofreading pass {k}: {len(gaps)} paragraph(s) without a verdict asked again; "
                     f"{got['checked']} answered, {len(left)} still without one")
        if asked:  # the record of the earlier try is replaced by what this one found (a verdict, or a new record)
            with self.lock, Transaction(self.conn):
                for uid in sorted(asked):
                    self.conn.execute("UPDATE qa_findings SET status='superseded' WHERE book_id=? AND unit_id=? AND "
                                      "type='proofread_no_verdict' AND status='open' AND id<=?",
                                      (self.book.id, uid, before_max))
        stats = {**_added_up(passes), **({"since_translation_id": cp["since_translation_id"]}
                                         if "since_translation_id" in cp else {}), "passes": passes} \
            if "passes" in cp else passes[0]
        self._set_job("proofread", self.book.id, self._pass_status(stats), None,
                      {**stats, "upto_translation_id": cp.get("upto_translation_id", self._last_translation_id())})
        self.log(f"proofreading (paragraphs without a verdict): {_counts(stats)}")
        return stats

    def _gap_stretches(self, proofread: Any, gaps: set[str], story: set[str] | None
                       ) -> tuple[list[list[dict[str, Any]]], list[list[dict[str, Any]]], list[list[dict[str, Any]]]]:
        """The paragraphs without a verdict as stretches: ones next to each other go together (up to the stretch
        size), each stretch with the paragraphs just before and just after it in the same chapter to read (not
        checked; as many as a stretch gets before it)."""
        size = self.cfg.context.proofread_chunk_units
        parts: list[list[dict[str, Any]]] = []
        befores: list[list[dict[str, Any]]] = []
        for x in [x for part in proofread.chunks(self.conn, self.book.id, gaps, size=1 << 30, story=story)
                  for x in part]:
            prev = [b for b in proofread.tail(self.conn, self.book.id, x["_at"] - 1, story=story)
                    if b["_href"] == x["_href"]]
            if parts and len(parts[-1]) < size and prev and prev[-1]["unit_id"] == parts[-1][-1]["unit_id"]:
                parts[-1].append(x)
            else:
                parts.append([x])
                befores.append(prev)
        afters = [[a for a in proofread.head(self.conn, self.book.id, part[-1]["_at"], story=story)
                   if a["_href"] == part[-1]["_href"]] for part in parts]
        return parts, befores, afters

    def _proofread_once(self, saved: dict[str, Any], prompt: Any, record_job: bool,
                        mark: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """One pass over the book (or the chosen paragraphs), from where ``saved`` says a stopped attempt got to.
        Returns its counts and whether it was stopped (paused) before the end."""
        from ..analysis import proofread
        from .review import new_stats

        saved = dict(saved)
        after = saved.pop("done_until_ordinal", None)
        beyond = saved.pop("done_beyond", [])  # stretches done past a gap by a stopped attempt (4.1)
        wanted = set(self.opts.unit_ids) if self.opts.unit_ids else None
        with self.lock:
            story = self._story_files()
            parts = proofread.chunks(self.conn, self.book.id, wanted, size=self.cfg.context.proofread_chunk_units,
                                     after=after, story=story, since=saved.get("since_translation_id"))
            prev = proofread.tail(self.conn, self.book.id, after, wanted, story=story) if after is not None else []
        stats = {**new_stats(checked=0, blocked=0, failed=0), **saved}
        befores = []
        for part in parts:
            # the end of the stretch before, in the same chapter: read for who is talking and about what, not checked
            befores.append([p for p in prev[-proofread.BEFORE_UNITS:] if p["_href"] == part[0]["_href"]])
            prev = part
        stopped = self._stretches("proofread", "proofreading", parts, befores,
                                  lambda part, before, counts: self._proofread_part(part, before, prompt, counts),
                                  stats, after, beyond, record_job, lambda: new_stats(checked=0, blocked=0, failed=0),
                                  mark=mark)
        return stats, stopped

    def _verdicts_asked_again(self, parsed: Any, valid: set[str], visible: list[dict[str, Any]],
                              cast: Any, extra: dict[str, Any], stage: str, prompt: Any) -> Any:
        """With verdicts asked for: the paragraphs a stretch's answer left without one are sent again, alone (once);
        the rest is not checked twice. What still has none is counted as not looked at."""
        from ..llm.schemas import ProofreadOutput

        answered = {c.unit_id for c in parsed.checked} | {it.unit_id for it in parsed.issues}
        missing = [p for p in visible if p["unit_id"] in valid - answered]
        if not missing:
            return parsed
        blocked: list[str] = []
        try:
            again = call_in_parts(lambda pl: self._llm(stage, prompt, pl, [p["unit_id"] for p in pl["paragraphs"]]),
                                  {"task": "proofread", "paragraphs": missing, "characters": cast, **extra,
                                   "verdicts": True}, "paragraphs", blocked)
        except FatalAPIError:
            raise
        except LLMError:
            return parsed  # counted as without a verdict by the caller
        if again is None:
            return parsed
        assert isinstance(again, ProofreadOutput)
        asked = {p["unit_id"] for p in missing} - set(blocked)
        return ProofreadOutput(checked=[*parsed.checked, *(c for c in again.checked if c.unit_id in asked)],
                               issues=[*parsed.issues, *(it for it in again.issues if it.unit_id in asked)])

    def _proofread_part(self, part: list[dict[str, Any]], before: list[dict[str, Any]], prompt: Any,
                        stats: dict[str, Any], after: list[dict[str, Any]] | None = None) -> None:
        from ..analysis import proofread
        from ..llm.schemas import ProofreadOutput
        from .review import in_conflict

        with self.lock:
            cast = proofread.cast(self.conn, self.book.id, part)
            extra = proofread.notes(self.conn, self.book.id, part, before, glossary=self.glossary,
                                    volume=self.volume, revealed=self.link_revealed,
                                    definitions=self.cfg.context.term_definitions,
                                    compact=self.cfg.translation.compact_payload)
        if after:  # what follows, to read and not to check (a stretch of paragraphs asked again, ADR-098)
            extra["context_after"] = [{"ja": a["ja"], "zh": a["zh"]} for a in after]
        visible = [{k: v for k, v in p.items() if not k.startswith("_")} for p in part]
        blocked: list[str] = []
        try:  # the content filter may refuse a stretch; it is then checked in halves
            stage = getattr(self, "_proofread_stage", "proofread")
            parsed = call_in_parts(lambda pl: self._llm(stage, prompt, pl, [p["unit_id"] for p in pl["paragraphs"]]),
                                   {"task": "proofread", "paragraphs": visible, "characters": cast, **extra,
                                    **({"verdicts": True} if self.cfg.context.proofread_verdicts else {})},
                                   "paragraphs", blocked)
        except FatalAPIError:
            raise
        except LLMError as exc:
            self._finding(None, "proofread_failed", "low", str(exc)[:300])
            stats["failed"] += len(part)
            return
        tids = {p["unit_id"]: p["_tid"] for p in part}
        for uid in blocked:
            self._finding(uid, "proofread_blocked", "low", "the content filter refused this passage in proofreading",
                          translation_id=tids.get(uid))
        stats["blocked"] += len(blocked)
        if parsed is None:
            return
        assert isinstance(parsed, ProofreadOutput)
        valid = {p["unit_id"] for p in part} - set(blocked)  # what was really checked against the source
        looked = valid
        if self.cfg.context.proofread_verdicts:  # paragraphs the model gave no verdict for were not looked at (ADR-055)
            parsed = self._verdicts_asked_again(parsed, valid, visible, cast, extra, stage, prompt)
            looked = valid & ({c.unit_id for c in parsed.checked} | {it.unit_id for it in parsed.issues})
            stats["sent"] = stats.get("sent", 0) + len(valid)
            stats["no_verdict"] = stats.get("no_verdict", 0) + len(valid - looked)
            for uid in sorted(valid - looked):
                self._finding(uid, "proofread_no_verdict", "low", "proofreading gave no verdict for this paragraph, "
                              "asked again on its own", translation_id=tids.get(uid))
            if valid - looked:  # kept with the pass: a resumed run asks them again (ADR-098)
                stats["no_verdict_ids"] = sorted(valid - looked)
            valid = looked  # only what was answered for counts as checked
        stats["checked"] += len(valid)
        _outlived(self, stats, {u: tids[u] for u in looked}, parsed.issues, "proofread",
                  ("review:proofread_%", "fix_still_wrong", "fix_undecided", "fix_regressed"))
        conflicts = in_conflict(self, valid)
        feedback: dict[str, list[dict[str, Any]]] = {}
        polished: set[str] = set()
        if asked := sorted({it.unit_id for it in parsed.issues if it.kind == "expression" and it.unit_id in valid}):
            # polished once in this run: an earlier pass found how a paragraph reads and its text has changed since
            # (the finding was about another version; a fix shown unneeded or rejected left the text as it was)
            with self.lock:
                polished = {r["unit_id"] for r in self.conn.execute(
                    f"SELECT unit_id, translation_id FROM qa_findings WHERE run_id=? AND "
                    f"type='review:proofread_expression' AND status!='dismissed_by_check' AND translation_id IS NOT "
                    f"NULL AND unit_id IN ({','.join('?' * len(asked))})", [self.run_id, *asked])
                    if r["translation_id"] != tids[r["unit_id"]]}
        for it in parsed.issues:
            if it.unit_id in valid and it.kind == "ambiguity" and it.problem.strip() and it.severity == "low":
                # two readings the source allows (ADR-063), and Chinese cannot keep both: kept for a person to look at
                # (one the Chinese can keep open goes back to the translator, ADR-084)
                stats["ambiguities"] = stats.get("ambiguities", 0) + 1
                self._finding(it.unit_id, "review:proofread_ambiguity", "low",
                              f"{it.problem[:300]}" + (f" → {it.fix[:160]}" if it.fix.strip() else ""),
                              translation_id=tids[it.unit_id])
                continue
            if it.unit_id not in valid or it.severity == "low" or not it.problem.strip():
                continue  # a small slip is not worth a retranslation
            if (it.unit_id, f"review:proofread_{it.kind}") in conflicts:
                # came back unchanged earlier in this run: the rules pull apart, asking again gives the same text
                stats["conflict_skipped"] = stats.get("conflict_skipped", 0) + 1
                continue
            if it.kind == "expression" and it.unit_id in polished:
                # not polished twice: a later pass rewording a reworded sentence only drifts (a meaning issue still
                # goes back)
                stats["expression_skipped"] = stats.get("expression_skipped", 0) + 1
                continue
            stats["issues"] += 1
            fid = self._finding(it.unit_id, f"review:proofread_{it.kind}", it.severity,
                                f"{it.problem[:300]}" + (f" → {it.fix[:160]}" if it.fix.strip() else ""),
                                translation_id=tids[it.unit_id])
            note = proofread.FIX_NOTE_EXPRESSION if it.kind == "expression" else \
                proofread.FIX_NOTE_AMBIGUITY if it.kind == "ambiguity" else proofread.FIX_NOTE
            feedback.setdefault(it.unit_id, []).append(
                {"issue_type": f"proofread_{it.kind}", "evidence": f"{it.problem}（譯文：{it.quote}）",
                 "suggestion": f"{it.fix}\n{note}", "finding_id": fid})
        if feedback:
            self._fix_found(feedback, stats)

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
        self.refresh_readings()  # names are chosen knowing the readings the book gives them
        with self.lock:
            payload = build_naming_payload(self.conn, self.book.id,
                                           definitions=self.cfg.context.term_definitions)
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
        """Chapters side by side (``openrouter.max_concurrency`` at a time, 4.2, ADR-065), the scenes of a chapter in
        order, each with its rule check and review. Trade-off: a chapter's first batch may not see the end of the
        chapter before translated yet (its ``context_before`` then has the source only)."""
        positions = self.body_positions()
        groups = self._chapter_groups(scenes)
        order = {sid: i for i, sid in enumerate(scenes)}
        halt = threading.Event()  # another chapter failed (a fatal error, the budget): stop at the next scene

        def work(g: int) -> None:
            for sid in groups[g]:
                if self._stop_reason or halt.is_set():
                    break
                pending, _ = self._scene_pending(sid)
                if not pending:
                    continue
                self.log(f"translate {sid} ({order[sid] + 1}/{len(scenes)})")
                with self.lock:
                    self.reload_glossary()  # a rendering changed while the run goes on is used from the next scene
                self.run_batches(self.scene_batches(pending, positions), concurrency=1)
                self.after_scene(sid)

        self._side_by_side(len(groups), work, lambda g, _r: None, "translation", halt)

    def _chapter_groups(self, scenes: list[str]) -> list[list[str]]:
        """The scenes by chapter (``chapters``), in reading order; a scene outside every chapter goes with the one
        before it. Without chapters the book is one group."""
        with self.lock:
            bounds = [(r["start_ordinal"], r["end_ordinal"]) for r in self.conn.execute(
                "SELECT start_ordinal, end_ordinal FROM chapters WHERE book_id=? ORDER BY seq", (self.book.id,))]
            starts = {r["id"]: r["start_ordinal"] for r in self.conn.execute(
                "SELECT id, start_ordinal FROM scenes WHERE book_id=?", (self.book.id,))}
        groups: list[list[str]] = []
        last: int | None = None
        for sid in scenes:
            at = starts.get(sid)
            ch = next((k for k, (a, b) in enumerate(bounds) if at is not None and a <= at <= b), None)
            if groups and (ch is None or ch == last):
                groups[-1].append(sid)
            else:
                groups.append([sid])
            if ch is not None:
                last = ch
        return groups

    def after_scene(self, sid: str) -> None:
        ids = self.translated_unit_ids(sid)
        self.confirm_flags(ids)
        self.review(ids)

    def confirm_flags(self, unit_ids: list[str]) -> dict[str, int]:
        """What simple rules found (a slur, 「我」 in third-person narration…) is checked once more by the model with
        the source and the lines around it; only the real problems go on to review (「阿三」 a maid's name, 「棒子」 a
        stick, 「你一言我一語」 an idiom are left alone). The translator's own doubts (a subject left out, an uncertain
        speaker or term: ``CONFIRM_MODEL_FLAGS``) are asked about the same way. One small request per scene with flags."""
        from ..analysis import reader
        from ..llm.schemas import ConfirmOutput
        from .context_builder import glossary_payload
        from .review import _addressees, _speakers, _surroundings

        if not unit_ids:
            return {}
        ph = ",".join("?" * len(unit_ids))
        rules = (*CONFIRM_RULES, *CONFIRM_MODEL_FLAGS)  # the program's word rules and the translator's own doubts
        with self.lock:
            found = self.conn.execute(
                f"SELECT f.unit_id, f.type, f.evidence FROM qa_findings f WHERE f.run_id=? AND f.status='open' AND "
                f"f.severity IN ('high','error') AND f.type IN ({','.join('?' * len(rules))}) AND f.unit_id IN ({ph})",
                (self.run_id, *rules, *unit_ids)).fetchall()
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
                # what the rule needs: the people's renderings around an added name; who a 你 is said to
                if f["type"] == "possible_added_name":
                    text = " ".join([row["plain_text"], *(a["source"] for a in items[-1]["around"])])
                    if people := [g for g in glossary_payload(self.glossary, text, upto=(self.volume, row["ordinal"]),
                                                              revealed=self.link_revealed)
                                  if g["category"] == "character"]:
                        items[-1]["glossary"] = people
                elif f["type"] == "second_person_gender" and (to := _addressees(self.conn, row["id"])):
                    items[-1]["to"] = to
            # as known at the earliest line checked: nothing revealed after it is shown
            cast = reader.characters(self.conn, self.book.id, min(self.conn.execute(
                "SELECT ordinal FROM units WHERE id=?", (f["unit_id"],)).fetchone()[0] for f in found))
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
                if (c.unit_id, c.rule) not in flagged:
                    continue
                if c.real:  # known to be wrong now: review fixes it whatever else it finds (review.CONFIRMED)
                    self.conn.execute("UPDATE qa_findings SET evidence=evidence || ? WHERE run_id=? AND unit_id=? AND "
                                      "type=? AND status='open'",
                                      (f"{CONFIRMED}{c.reason[:200]}", self.run_id, c.unit_id, c.rule))
                    continue
                stats["dismissed"] += 1
                self.conn.execute("UPDATE qa_findings SET status='dismissed_by_check', evidence=evidence || ? WHERE "
                                  "run_id=? AND unit_id=? AND type=? AND status='open'",
                                  (f" | check: {c.reason[:200]}", self.run_id, c.unit_id, c.rule))
                if not self.conn.execute("SELECT 1 FROM qa_findings WHERE run_id=? AND unit_id=? AND status='open' AND "
                                         "severity IN ('high','error')", (self.run_id, c.unit_id)).fetchone():
                    latest = self.conn.execute("SELECT id FROM translations WHERE unit_id=? AND run_id=? ORDER BY id "
                                               "DESC LIMIT 1", (c.unit_id, self.run_id)).fetchone()
                    if latest is not None:
                        settle_translation(self.conn, latest[0])
        if stats["dismissed"]:
            self.log(f"  rule flags checked: {stats}")
        return stats

    def _llm(self, stage: str, prompt: Any, payload: dict[str, Any], unit_ids: list[str], *,
             reuse: bool = False) -> Any:
        assert self.translator is not None and self.pricing is not None
        out_chars = sum(len(u.get("source", "")) for u in payload.get("units", []))
        est = self._estimate(payload, out_chars, stage)
        self.check_budget(est)
        correction = None
        model = self.model_for(stage)
        wrong = 0
        while True:
            try:
                parsed, _rid, mode = call_structured(
                    self.client, self.recorder, stage=stage, model=model, prompt=prompt, payload=payload,
                    mode=self.mode_for(model), cfg=self.cfg, unit_ids=unit_ids, est_cost=est, correction=correction,
                    on_downgrade=self._note, reuse=reuse and correction is None)
                if model == self.model:
                    self.translator.mode = mode
                return parsed
            except EmptyAnswerError:
                raise  # asked again in call_structured already (translator.EMPTY_RETRIES): nothing to correct
            except ModelOutputError as exc:
                wrong += 1
                if wrong == 2:
                    raise
                correction = str(exc)

    LONG_SCENE = 1.5  # a scene longer than this many times max_scene_units is read in even parts (it stays one scene)

    def _read(self, stage: str, prompt: Any, payload: dict[str, Any], sid: str) -> Any:
        """A read request; when the content filter refuses the scene, read it in halves (``split_read``). A scene the
        model cut that is too long for one request is read in even consecutive parts and the answers joined: it is
        still one scene (ADR-059: the program does not cut the model's scenes).

        Units no part could read are reported one by one; None when nothing could be read.
        """
        blocked: list[str] = []
        ids = [u["unit_id"] for u in payload["units"]]
        size = self.cfg.memory.max_scene_units
        if len(ids) > size * self.LONG_SCENE:
            src = {u["unit_id"]: u for u in payload["units"]}
            parts = []
            before: list[str] = []
            for keep in even_parts(payload["units"], size):
                pl = subset_payload(payload, keep)
                if before:  # a later part: what comes right before it in this scene, not the previous scene's end
                    pl.pop("previous_scene_end", None)
                    pl["part_before"] = [{"unit_id": u, "source": src[u].get("source", "")} for u in before[-PART_BEFORE:]]
                if (p := self._read_parts(stage, prompt, pl, blocked)) is not None:
                    parts.append(p)
                before = [u for u in keep if u not in blocked]  # a refused paragraph is not shown again
            parsed = merge_outputs(parts) if parts else None
        else:
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
        src = {u["unit_id"]: u for u in payload["units"]}
        parts = []
        for k, keep in enumerate((ids[:half], ids[half:])):
            pl = subset_payload(payload, keep)
            if k and "part_before" in payload:  # the second half of a part: what comes right before it is the first half
                prior = [u for u in ids[:half] if u not in blocked][-PART_BEFORE:]
                pl["part_before"] = [{"unit_id": u, "source": src[u].get("source", "")} for u in prior]
            if (p := self._read_parts(stage, prompt, pl, blocked)) is not None:
                parts.append(p)
        return merge_outputs(parts) if parts else None

    def stage_scene_read(self, sid: str) -> bool:
        """``context.read_in_one_request`` (ADR-065): the scene's analysis and memory update from one request
        (``prompts/scene_read``), applied by the same two functions as two requests are — the analysis first; the
        memory's ids of people new in this scene (the analysis's ``temp_id``) become the cards the analysis made.
        False when the scene is read with two requests as before: one of the two steps is not due or is switched
        off, the scene is long enough to be read in parts, or the one request failed (refused, bad answer)."""
        from ..analysis.memory_update import scene_rows
        from ..llm.schemas import SceneReadOutput

        a_job, m_job = self._job("scene_analyze", sid), self._job("memory_update", sid)
        if not (self.cfg.context.scene_analysis and self.cfg.context.memory_update) or any(
                j is None or j["status"] not in ("pending", "running") for j in (a_job, m_job)):
            return False
        scene = get_scene(self.conn, self.book.id, sid)
        with self.lock:
            if len(scene_rows(self.conn, self.book.id, sid)) > self.cfg.memory.max_scene_units * self.LONG_SCENE:
                return False
            with Transaction(self.conn):  # a re-read starts without the cards only the last reading of it made
                if gone := forget_scene_cards(self.conn, self.book.id, scene, memory_too=True):
                    self.log(f"  re-read: {len(gone)} card(s) only the last reading of this scene made were dropped")
            gl = [{"ja": g["source_term"], "zh": g["target_term"]} for g in self.glossary]
            analysis_payload = build_analysis_payload(self.conn, self.book.id, scene, gl)
            payload = scene_read_payload(analysis_payload, build_memory_payload(self.conn, self.book.id, scene, gl))
        try:
            parsed = self._llm("scene_read", self.read_prompt, payload, [u["unit_id"] for u in payload["units"]])
        except FatalAPIError:
            raise
        except LLMError as exc:
            self.log(f"  one-request read failed ({type(exc).__name__}); read with two requests")
            return False
        assert isinstance(parsed, SceneReadOutput)
        parsed = parsed.model_copy(update={"analysis": self._fill_missing_lines(analysis_payload, parsed.analysis)})
        refs: dict[str, str] = {}
        with self.lock, Transaction(self.conn):
            stats = apply_scene_analysis(self.conn, self.book.id, scene, parsed.analysis, run_id=self.run_id, refs=refs)
        self.reload_glossary()
        self._set_job("scene_analyze", sid, "succeeded", None, {**stats, "mode": "llm", "one_request": True})
        self.log(f"  analysis: {stats}")
        memory = MemoryUpdateOutput.model_validate(with_cards(parsed.memory.model_dump(), refs))
        try:
            with self.lock, Transaction(self.conn):
                mstats = apply_memory_update(self.conn, self.book.id, scene, memory, run_id=self.run_id)
        except sqlite3.Error as exc:  # memory failure must not affect committed translations
            self._set_job("memory_update", sid, "failed", f"db error: {exc}")
            return True
        self.reload_glossary()
        self._set_job("memory_update", sid, "succeeded", None, {**mstats, "one_request": True})
        self.log(f"  memory: {mstats}")
        return True

    def _fill_missing_lines(self, payload: dict[str, Any], parsed: SceneAnalysisOutput) -> SceneAnalysisOutput:
        """The lines the program found that the reading's answer says nothing about (an empty ``utterances`` left nine
        lines without a speaker): asked again, those only, in one small request with the scene and the people the
        reading made new here — like the proofreading's verdict coverage (ADR-089). What is still unanswered is marked
        for the second look when the analysis is applied (``apply_scene_analysis``), so it is asked there (ADR-102)."""
        got = {(u.unit_id, u.quote_index) for u in parsed.utterances}
        # (the rest of a speech running on from the paragraph before follows its start: not asked on its own)
        missing = [q for q in payload.get("quotes", []) if (q["unit_id"], q["quote_index"]) not in got
                   and not q.get("continues_previous_quote")]
        if not missing:
            return parsed
        small = {**payload, "quotes": missing, "only_quotes": True,
                 "new_characters": [{"temp_id": n.temp_id, "name_ja": n.name_ja} for n in parsed.new_characters]}
        try:
            extra = self._llm("scene_analyze", self.analyze_prompt, small, [u["unit_id"] for u in payload["units"]])
        except FatalAPIError:
            raise
        except LLMError as exc:
            self.log(f"  {len(missing)} line(s) the reading left out could not be asked again ({type(exc).__name__})")
            return parsed
        assert isinstance(extra, SceneAnalysisOutput)
        keys = {(q["unit_id"], q["quote_index"]) for q in missing}
        add = [u for u in extra.utterances if (u.unit_id, u.quote_index) in keys and (u.unit_id, u.quote_index) not in got]
        self.log(f"  {len(missing)} line(s) the reading left out asked again: {len(add)} answered")
        return parsed.model_copy(update={"utterances": [*parsed.utterances, *add]})

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
                parsed = self._fill_missing_lines(payload, parsed)
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
            window = None
            if batch.fix:  # a fix: the memory of the paragraphs it is shown (each fix's window), not of the whole scene
                ids = [c["unit_id"] for c in shown_context(ctx)]
                window = self.conn.execute(f"SELECT * FROM units WHERE id IN ({','.join('?' * len(ids))})",
                                           ids).fetchall() if ids else []
            mem = memory_context(self.conn, self.book.id, scene=scene, batch_rows=batch.rows,
                                 max_evidence=self.cfg.memory.max_retrieved_evidence, window_rows=window,
                                 fix_facts=self.cfg.context.fix_fact_quota,
                                 compact=self.cfg.translation.compact_payload)
        ctx.update(mem)
        if self.cfg.context.term_definitions:  # what the book's terms mean, once their evidence is reached (ADR-069)
            from ..memory.terms import definitions_for

            text = " ".join([r["plain_text"] for r in batch.rows] + [c["source"] for c in shown_context(ctx)])
            with self.lock:
                if defs := definitions_for(self.conn, self.book.id, text, batch.rows[-1]["ordinal"]):
                    ctx["term_definitions"] = defs
        from ..analysis.narration import for_scene

        with self.lock:  # ADR-040: whose voice tells this scene (the read pass's judgement, else the whole book's)
            from ..memory.visibility import visible_names

            # named as known at this point of the story (the focal 「神祕少女」 is not called by a name revealed later)
            zh_names = visible_names(self.conn, self.book.id, batch.rows[-1]["ordinal"])
            who = for_scene(self.conn, self.book.id, scene, zh_names)
        if who is not None and who.get("segments"):  # only the paragraphs this request shows (ADR-083)
            shown = {r["id"] for r in batch.rows} | {c["unit_id"] for c in shown_context(ctx)}
            segs = [{**g, "units": [u for u in g["units"] if u in shown]} for g in who["segments"]]
            who = {**who, "segments": [g for g in segs if g["units"]]}
            if not who["segments"]:
                del who["segments"]
        if who:
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
    (「たかぎちゃん」 → 小高木) does not also go out as an unconfirmed glossary entry (高木醬) that contradicts it.
    Confirmed glossary entries stay (the human chose them)."""
    forms = {a["ja"] for a in address if a.get("zh")}
    return [g for g in glossary if g.get("verified") or g["ja"] not in forms
            or all(a.get("zh") == g.get("zh") for a in address if a["ja"] == g["ja"])]


def scene_read_payload(analysis: dict[str, Any], memory: dict[str, Any]) -> dict[str, Any]:
    """The one request's input (ADR-065): the analysis request's, with the memory request's units (they may carry a
    translation), its earlier facts, and what it shows of each known person (Chinese name, members) added to the
    analysis's view of them. The memory's ``scene`` is left out: the analysis is in the same answer."""
    out = {**analysis, "task": "scene_read", "units": memory["units"]}
    extra = {c["character_id"]: c for c in memory.get("known_characters", [])}
    people = []
    for c in analysis.get("known_characters", []):
        people.append({**extra.pop(c["character_id"], {}), **c})
    out["known_characters"] = people + list(extra.values())
    if memory.get("known_facts"):
        out["known_facts"] = memory["known_facts"]
    return out


def with_cards(value: Any, refs: dict[str, str]) -> Any:
    """The memory answer with each person id (``*_ref``, ``*_refs``, ``source_viewpoint``) the analysis turned into
    a card replaced by that card's id; anything else unchanged (what is not a known id is rejected later as
    before). Id bookkeeping only: nothing about who is who is decided here."""
    if isinstance(value, list):
        return [with_cards(v, refs) for v in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for k, v in value.items():
        if isinstance(v, str) and (k.endswith("_ref") or k == "source_viewpoint"):
            out[k] = refs.get(v, v)
        elif isinstance(v, list) and k.endswith("_refs"):
            out[k] = [refs.get(x, x) if isinstance(x, str) else x for x in v]
        else:
            out[k] = with_cards(v, refs)
    return out


def _outlived(orch: Any, stats: dict[str, Any], checked: dict[str, int], issues: list[Any], stage: str,
              kinds: tuple[str, ...]) -> None:
    """After a stretch of proofreading or the reader's look: what it really looked at (``checked``) and did not find
    again closes the findings about older texts of those paragraphs (``review.supersede``)."""
    from .review import supersede

    reported: dict[str, set[str]] = {}
    for it in issues:
        reported.setdefault(it.unit_id, set()).add(f"review:{stage}_{it.kind}")
    if n := supersede(orch, checked, reported, kinds):
        stats["superseded"] = stats.get("superseded", 0) + n


def _unanswered(stats: dict[str, Any]) -> list[list[str]]:
    """Each proofreading pass's paragraphs left without a verdict (ADR-098)."""
    return [p.get("no_verdict_ids", []) for p in stats.get("passes") or [stats]]


def _counts(stats: dict[str, Any]) -> dict[str, Any]:
    """The counts for the log line, without the lists of paragraphs."""
    return {k: ([_counts(p) for p in v] if k == "passes" else v) for k, v in stats.items() if k != "no_verdict_ids"}


def _one_pass(stats: dict[str, Any]) -> dict[str, Any]:
    """A proofreading pass's own counts, as kept in ``passes`` (ADR-066)."""
    return {k: v for k, v in stats.items() if k not in ("since_translation_id", "passes", "pass")}


def _added_up(passes: list[dict[str, Any]]) -> dict[str, Any]:
    """The counts of several proofreading passes added up (ADR-066)."""
    total: dict[str, Any] = {}
    for p in passes:
        for k, v in p.items():
            if isinstance(v, int) and not isinstance(v, bool):
                total[k] = total.get(k, 0) + v
    return total
