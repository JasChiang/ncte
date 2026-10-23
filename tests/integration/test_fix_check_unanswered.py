"""ADR-100: a fix check that does not answer is asked again. An empty answer is asked again as every step asks it
(``translator.EMPTY_RETRIES``); a fix left out of an answer, or in a larger request that failed, is asked once more on
its own; a fix whose check a pause cut off, or that failed in an earlier run, is asked once on resume, from what was
recorded with the fix, and settled as the first check would have settled it. Still without an answer, it stays open
and the book is not done (ADR-055)."""

from ncte.errors import EmptyAnswerError
from ncte.llm.fake import FakeLLM, default_fix_check
from ncte.reports.acceptance import acceptance
from ncte.translation.cache import latest_texts
from ncte.translation.translator import EMPTY_RETRIES

from ..helpers import orchestrator_for, resume_run, run_book, start_run
from .test_fix_check import LINES, RIGHT, negation_review, review_once, small_book, translator, unit_of

TWO = ["　彼女は来なかった。", "　雨は朝から降り続いていた。", "　友達も来なかった。"]


def fix_checks(conn, rid):
    return conn.execute("SELECT status, unit_ids_json FROM llm_requests WHERE run_id=? AND stage='fix_check' ORDER BY "
                        "id", (rid,)).fetchall()


def settled(conn, book, uid):
    best = latest_texts(conn, book.id)[uid]
    marks = conn.execute("SELECT type, status FROM qa_findings WHERE unit_id=? AND translation_id=? AND type LIKE "
                         "'fix_%'", (uid, best["id"])).fetchall()
    issue = conn.execute("SELECT status FROM qa_findings WHERE unit_id=? AND type='review:mistranslation' ORDER BY id "
                         "DESC LIMIT 1", (uid,)).fetchone()
    return best["text"] == RIGHT and best["status"] == "succeeded" and [tuple(m) for m in marks] == \
        [("fix_unchecked", "resolved")] and issue["status"] == "resolved"


def open_failures(conn, book):
    return conn.execute("SELECT COUNT(*) FROM qa_findings WHERE book_id=? AND type='fix_check_failed' AND "
                        "status='open'", (book.id,)).fetchone()[0]


def test_an_empty_answer_is_asked_again_and_the_fix_settled(conn, cfg, tmp_path):
    epub = small_book(tmp_path)
    cfg.review.automatic_review = "all"
    fake = FakeLLM(responders={"translate": translator(RIGHT), "review": negation_review},
                   faults={"fix_check": ["empty_answer"] * EMPTY_RETRIES})
    book, rid, summary = run_book(conn, cfg, epub, fake)
    uid = unit_of(conn, book, "来なかった")
    assert [r["status"] for r in fix_checks(conn, rid)] == ["failed"] * EMPTY_RETRIES + ["succeeded"]
    assert settled(conn, book, uid) and not open_failures(conn, book)
    assert acceptance(conn, book.id, proofread=False, reader=False)["checks"]["fixes_checked"]["ok"]


def test_fixes_of_a_failed_request_are_each_asked_on_their_own(conn, cfg, tmp_path):
    cfg.review.automatic_review = "all"
    fake = FakeLLM(responders={"translate": translator(RIGHT), "review": negation_review},
                   faults={"fix_check": ["empty_answer"] * (EMPTY_RETRIES + 1)})
    book, rid, _ = run_book(conn, cfg, small_book(tmp_path, TWO), fake)
    asked = fix_checks(conn, rid)
    assert len(asked[0]["unit_ids_json"].split(",")) == 2 and [r["status"] for r in asked[-2:]] == ["succeeded"] * 2
    assert all(len(r["unit_ids_json"].split(",")) == 1 for r in asked[-2:])
    for needle in ("彼女は来なかった", "友達も来なかった"):
        assert settled(conn, book, unit_of(conn, book, needle))
    assert not open_failures(conn, book)  # the failed request lost nothing in the end


def test_a_fix_left_out_of_the_answer_is_asked_again(conn, cfg, tmp_path):
    def first_only(payload, req):
        out = default_fix_check(payload, req)
        return {"checks": out["checks"][:1]}

    cfg.review.automatic_review = "all"
    fake = FakeLLM(responders={"translate": translator(RIGHT), "review": negation_review, "fix_check": first_only})
    book, rid, _ = run_book(conn, cfg, small_book(tmp_path, TWO), fake)
    assert [len(r["unit_ids_json"].split(",")) for r in fix_checks(conn, rid)] == [2, 1]
    for needle in ("彼女は来なかった", "友達も来なかった"):
        assert settled(conn, book, unit_of(conn, book, needle))


def test_a_check_cut_off_by_a_pause_is_asked_on_resume(conn, cfg, tmp_path):
    book, rid, summary = review_once(conn, cfg, small_book(tmp_path), fixed_text=RIGHT, fix_check=killed)
    assert summary.status == "interrupted"
    uid = unit_of(conn, book, "来なかった")
    assert not acceptance(conn, book.id, proofread=False, reader=False)["acceptable"]
    # resumed: the review left running looks again and finds nothing; the fix it had made is still waiting
    summary = resume_run(conn, cfg, rid, FakeLLM())
    assert summary.status == "completed"
    asked = fix_checks(conn, rid)
    assert [r["status"] for r in asked] == ["interrupted", "succeeded"] and uid in asked[-1]["unit_ids_json"]
    assert settled(conn, book, uid)
    assert acceptance(conn, book.id, proofread=False, reader=False)["acceptable"]


def never(payload, req):
    raise EmptyAnswerError("empty answer")


def killed(payload, req):
    raise KeyboardInterrupt


def test_a_check_that_never_answers_stays_open_and_the_book_is_not_done(conn, cfg, tmp_path):
    book, rid, _ = review_once(conn, cfg, small_book(tmp_path), fixed_text=RIGHT,
                               fix_check=never)
    uid = unit_of(conn, book, "来なかった")
    # a lone fix: its request asked as every step asks an empty answer, not once more as the same request
    assert len(fix_checks(conn, rid)) == EMPTY_RETRIES + 1
    best = latest_texts(conn, book.id)[uid]
    assert best["text"] == RIGHT and best["status"] == "needs_review"
    rep = acceptance(conn, book.id, proofread=False, reader=False)
    assert not rep["acceptable"] and uid in rep["checks"]["fixes_checked"]["units"]
    # the next run asks it once more; still nothing: open, said so
    resume_run(conn, cfg, rid, FakeLLM(faults={"fix_check": ["empty_answer"] * 99}))
    assert len(fix_checks(conn, rid)) == 2 * (EMPTY_RETRIES + 1)
    mark = conn.execute("SELECT type, status, evidence FROM qa_findings WHERE translation_id=? AND type LIKE 'fix_%'",
                        (best["id"],)).fetchone()
    assert (mark["type"], mark["status"]) == ("fix_unchecked", "open") and "still no answer" in mark["evidence"]
    assert latest_texts(conn, book.id)[uid]["status"] == "needs_review"
    assert not acceptance(conn, book.id, proofread=False, reader=False)["acceptable"]


def test_an_answered_check_is_not_asked_again(conn, cfg, tmp_path):
    book, rid, _ = review_once(conn, cfg, small_book(tmp_path), fixed_text=RIGHT)
    uid = unit_of(conn, book, "来なかった")
    assert settled(conn, book, uid) and len(fix_checks(conn, rid)) == 1
    resume_run(conn, cfg, rid, FakeLLM())
    assert len(fix_checks(conn, rid)) == 1


def test_a_fix_without_its_record_is_left_open(conn, cfg, tmp_path):
    """A fix written before migration 36 has no record of the issues it was sent to fix: nothing to check it against
    (old books are test data, re-imported rather than repaired)."""
    from ncte.translation.review import check_waiting_fixes

    book, rid, _ = review_once(conn, cfg, small_book(tmp_path, LINES), fixed_text=RIGHT, fix_check=killed)
    uid = unit_of(conn, book, "来なかった")
    tid = latest_texts(conn, book.id)[uid]["id"]
    conn.execute("UPDATE translations SET fix_json=NULL WHERE id=?", (tid,))
    _, rid2, opts = start_run(conn, cfg, small_book(tmp_path, LINES), FakeLLM())
    orch = orchestrator_for(conn, cfg, book, rid2, opts, FakeLLM())
    orch.prepare()
    stats = check_waiting_fixes(orch)
    assert stats["unrecorded"] == 1 and stats["asked"] == 0
    assert conn.execute("SELECT status FROM qa_findings WHERE translation_id=? AND type='fix_unchecked'",
                        (tid,)).fetchone()[0] == "open"
