"""2.11: a new run on a book already done does not redo its whole-book steps; proofreading and the reader's look
only go over paragraphs translated since they last went over the whole book."""

import json

from ncte.llm.fake import FakeLLM
from ncte.reports.acceptance import acceptance

from ..helpers import run_book
from ..scripted import RESPONDERS

WHOLE_BOOK = ("verify", "relations", "impressions", "house_style", "naming")


class Seen(FakeLLM):
    def __init__(self) -> None:
        self.paragraphs: dict[str, set[str]] = {"proofread": set(), "reader": set()}
        super().__init__(responders={**RESPONDERS, "proofread": self._note("proofread"), "reader": self._note("reader")})

    def _note(self, task):
        def respond(payload, req):
            self.paragraphs[task] |= {p["unit_id"] for p in payload["paragraphs"]}
            return {"issues": []}
        return respond

    def stages(self) -> set[str]:
        return {c.stage.split(":")[0] for c in self.calls}


def _jobs(conn, rid):
    return {r["stage"]: (r["status"], json.loads(r["checkpoint_json"] or "{}")) for r in conn.execute(
        "SELECT stage, status, checkpoint_json FROM jobs WHERE run_id=? AND target_id=(SELECT book_id FROM runs "
        "WHERE id=?)", (rid, rid))}


def test_a_new_run_on_a_finished_book_skips_its_whole_book_steps(conn, cfg, fixtures):
    first = Seen()
    book, rid1, s = run_book(conn, cfg, fixtures["basic"], first, mode="context")
    assert s.status == "completed" and set(WHOLE_BOOK) & first.stages() and first.paragraphs["proofread"]
    jobs = _jobs(conn, rid1)
    assert jobs["proofread"][0] == "succeeded" and jobs["proofread"][1]["upto_translation_id"] > 0

    again = Seen()
    _, rid2, s = run_book(conn, cfg, fixtures["basic"], again, mode="context")
    assert s.status == "completed"
    assert not (set(WHOLE_BOOK) & again.stages()), again.stages()
    jobs = _jobs(conn, rid2)
    assert all(jobs[st][0] == "skipped" for st in WHOLE_BOOK if st in jobs)
    assert not again.paragraphs["proofread"] and not again.paragraphs["reader"]  # nothing new to look at
    assert jobs["proofread"][0] == "succeeded" and jobs["reader"][0] == "succeeded"
    assert acceptance(conn, book.id)["checks"]["proofread_complete"]["ok"]

    # one paragraph translated anew: only it is proofread and read
    uid = conn.execute("SELECT id FROM units WHERE book_id=? AND kind='text' AND translatable=1 ORDER BY ordinal "
                       "LIMIT 1 OFFSET 3", (book.id,)).fetchone()[0]
    conn.execute("DELETE FROM qa_findings WHERE unit_id=?", (uid,))
    conn.execute("DELETE FROM translations WHERE unit_id=?", (uid,))
    third = Seen()
    _, rid3, s = run_book(conn, cfg, fixtures["basic"], third, mode="context")
    assert s.status == "completed"
    assert third.paragraphs["proofread"] == {uid} and third.paragraphs["reader"] == {uid}
    assert not (set(WHOLE_BOOK) & third.stages())


def test_force_or_reading_since_the_step_runs_it_again(conn, cfg, fixtures):
    book, rid1, _ = run_book(conn, cfg, fixtures["basic"], Seen(), mode="context")
    forced = Seen()
    run_book(conn, cfg, fixtures["basic"], forced, mode="context", force=True)
    assert {"verify", "relations"} <= forced.stages() and forced.paragraphs["proofread"]

    # a run that read a scene after the steps last succeeded (and stopped before them): they are done again
    conn.execute("UPDATE jobs SET updated_at='2999-01-01T00:00:00Z' WHERE id=(SELECT MAX(id) FROM jobs WHERE "
                 "book_id=? AND stage='scene_analyze' AND status='succeeded')", (book.id,))
    later = Seen()
    run_book(conn, cfg, fixtures["basic"], later, mode="context")
    assert {"verify", "relations"} <= later.stages()


def test_a_book_never_proofread_whole_is_proofread_whole(conn, cfg, fixtures):
    cfg.context.proofread = False
    book, _, _ = run_book(conn, cfg, fixtures["basic"], Seen(), mode="context")
    cfg.context.proofread = True
    again = Seen()
    run_book(conn, cfg, fixtures["basic"], again, mode="context")
    from ncte.analysis import proofread
    from ncte.epub.matter import story_hrefs

    whole = {p["unit_id"] for part in proofread.chunks(conn, book.id, story=story_hrefs(conn, book.id)) for p in part}
    assert again.paragraphs["proofread"] == whole
