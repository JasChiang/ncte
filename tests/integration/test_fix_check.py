"""A fix is a fix only when the source says so: review / proofreading / reader fixes are checked against the source,
and a unit the reviewer never saw (refused, failed) is not counted as reviewed and keeps what was found on it."""

from pathlib import Path

from ncte.errors import ContentBlockedError
from ncte.llm.fake import FakeLLM, default_translate
from ncte.samples import BookSpec, Doc, _section, write_book
from ncte.translation.cache import latest_texts

from ..helpers import run_book

LINES = ["　彼女は来なかった。", "　雨は朝から降り続いていた。", "　禁書庫の扉は固く閉ざされていた。"]
WRONG, RIGHT = "她來了。", "她沒有來。"


def small_book(tmp_path: Path, lines: list[str] = LINES) -> Path:
    doc = Doc("ch1", "Text/ch1.xhtml", "第一章", _section("第一章", lines, None, "c1"))
    return write_book(BookSpec("小さな本", "urn:uuid:6f1d2a1e-0b7c-4c55-9a43-0000000000f1", [doc]), tmp_path / "small.epub")


def unit_of(conn, book, needle):
    return conn.execute("SELECT id FROM units WHERE book_id=? AND plain_text LIKE ?", (book.id, f"%{needle}%")).fetchone()[0]


def translator(fixed_text: str | None, seen: list | None = None):
    """Renders 来なかった as an affirmative; on a retranslation it writes ``fixed_text`` (None: the same wrong text)."""
    def respond(payload, req):
        out = default_translate(payload, req)
        if seen is not None and "review_feedback" in payload:
            seen.append(payload["review_feedback"])
        for t, u in zip(out["translations"], payload["units"], strict=True):
            if "来なかった" in u["source"]:
                t["translated_text"] = fixed_text if fixed_text and "review_feedback" in payload else WRONG
        return out
    return respond


def negation_review(payload, req):
    return {"issues": [{"unit_id": i["unit_id"], "issue_type": "mistranslation", "severity": "high",
                        "evidence": "原文「来なかった」是否定，譯文卻是肯定", "suggestion": RIGHT, "needs_retranslation": True}
                       for i in payload["items"] if "来なかった" in i["source"]]}


def review_once(conn, cfg, epub, *, fixed_text, fix_check=None, seen=None):
    cfg.review.automatic_review = "all"
    responders = {"translate": translator(fixed_text, seen), "review": negation_review}
    if fix_check is not None:
        responders["fix_check"] = fix_check
    return run_book(conn, cfg, epub, FakeLLM(responders=responders))


def test_a_retranslation_that_repeats_the_error_is_not_fixed(conn, cfg, tmp_path):
    seen: list = []
    checks: list = []

    def still_wrong(payload, req):
        checks.append(payload)
        return {"checks": [{"unit_id": i["unit_id"], "verdict": "still_wrong", "new_error": "",
                            "reason": "原文「来なかった」是否定，譯文仍是「來了」"} for i in payload["items"]]}

    book, rid, _ = review_once(conn, cfg, small_book(tmp_path), fixed_text=None, fix_check=still_wrong, seen=seen)
    uid = unit_of(conn, book, "来なかった")
    # the translator was given the text to correct, not only the source and the suggestion
    assert seen and seen[0][0]["current_translation"] == WRONG
    # the checker saw the source, both texts and the issue
    item = checks[0]["items"][0]
    assert item["source"].strip() == "彼女は来なかった。" and item["before"] == WRONG and item["after"] == WRONG
    assert any("否定" in x for x in item["issues"])
    best = latest_texts(conn, book.id)[uid]
    assert best["text"] == WRONG and best["status"] == "needs_review"  # not passed off as succeeded
    issue = conn.execute("SELECT status FROM qa_findings WHERE unit_id=? AND type='review:mistranslation'", (uid,)).fetchone()
    assert issue["status"] == "open"  # not resolved
    assert conn.execute("SELECT 1 FROM qa_findings WHERE unit_id=? AND type='fix_still_wrong' AND status='open'",
                        (uid,)).fetchone()


def test_an_unchecked_fix_stays_open(conn, cfg, tmp_path):
    """No answer from the check (here the default fake cannot settle an unchanged text): the issue stays open."""
    book, rid, _ = review_once(conn, cfg, small_book(tmp_path), fixed_text=None)
    uid = unit_of(conn, book, "来なかった")
    assert latest_texts(conn, book.id)[uid]["status"] == "needs_review"
    assert conn.execute("SELECT status FROM qa_findings WHERE unit_id=? AND type='review:mistranslation'",
                        (uid,)).fetchone()["status"] == "open"
    assert conn.execute("SELECT 1 FROM qa_findings WHERE unit_id=? AND type='fix_undecided'", (uid,)).fetchone()


def test_fix_check_off_never_counts_a_fix_as_fixed(conn, cfg, tmp_path):
    cfg.review.fix_check = False
    book, rid, _ = review_once(conn, cfg, small_book(tmp_path), fixed_text=RIGHT)
    uid = unit_of(conn, book, "来なかった")
    assert latest_texts(conn, book.id)[uid]["text"] == RIGHT
    assert conn.execute("SELECT status FROM qa_findings WHERE unit_id=? AND type='review:mistranslation'",
                        (uid,)).fetchone()["status"] == "open"
    assert conn.execute("SELECT 1 FROM qa_findings WHERE unit_id=? AND type='fix_unverified'", (uid,)).fetchone()


def test_a_checked_fix_resolves_only_its_issue(conn, cfg, tmp_path):
    book, rid, _ = review_once(conn, cfg, small_book(tmp_path), fixed_text=RIGHT)
    uid = unit_of(conn, book, "来なかった")
    best = latest_texts(conn, book.id)[uid]
    assert best["text"] == RIGHT and best["status"] == "succeeded"
    assert conn.execute("SELECT status FROM qa_findings WHERE unit_id=? AND type='review:mistranslation'",
                        (uid,)).fetchone()["status"] == "resolved"


def test_a_fix_that_breaks_something_else_is_not_the_books_text(conn, cfg, tmp_path):
    def regressed(payload, req):
        return {"checks": [{"unit_id": i["unit_id"], "verdict": "fixed", "new_error": "主詞從「她」變成「他」",
                            "reason": "否定改對了"} for i in payload["items"]]}

    book, rid, _ = review_once(conn, cfg, small_book(tmp_path), fixed_text="他沒有來。", fix_check=regressed)
    uid = unit_of(conn, book, "来なかった")
    assert latest_texts(conn, book.id)[uid]["text"] == WRONG  # the old text stays until a person decides
    assert conn.execute("SELECT status FROM translations WHERE unit_id=? AND text='他沒有來。'", (uid,)).fetchone()[0] \
        == "rejected_fix"
    assert conn.execute("SELECT status FROM qa_findings WHERE unit_id=? AND type='review:mistranslation'",
                        (uid,)).fetchone()["status"] == "open"


def test_the_checker_can_say_the_issue_was_not_an_error(conn, cfg, tmp_path):
    def invalid(payload, req):
        return {"checks": [{"unit_id": i["unit_id"], "verdict": "issue_invalid", "new_error": "", "reason": "原文就是這樣"}
                           for i in payload["items"]]}

    book, rid, _ = review_once(conn, cfg, small_book(tmp_path), fixed_text=None, fix_check=invalid)
    uid = unit_of(conn, book, "来なかった")
    assert latest_texts(conn, book.id)[uid]["status"] == "succeeded"
    assert conn.execute("SELECT status FROM qa_findings WHERE unit_id=? AND type='review:mistranslation'",
                        (uid,)).fetchone()["status"] == "dismissed_by_check"


def test_a_refused_unit_is_not_reviewed_and_keeps_its_findings(conn, cfg, tmp_path):
    epub = small_book(tmp_path)
    book, _, _ = run_book(conn, cfg, epub, FakeLLM())  # translated, no review
    refused, other = unit_of(conn, book, "禁書庫"), unit_of(conn, book, "雨は")
    for uid in (refused, other):  # a serious program finding on both
        conn.execute("INSERT INTO qa_findings(book_id, unit_id, type, severity, evidence, created_at) VALUES "
                     "(?,?,?,?,?,?)", (book.id, uid, "possible_added_name", "high", "x", "2026-01-01T00:00:00Z"))
        conn.execute("UPDATE translations SET status='needs_review' WHERE unit_id=?", (uid,))

    def review(payload, req):
        if any("禁書庫" in i["source"] for i in payload["items"]):
            raise ContentBlockedError("HTTP 403 content filter: fake PROHIBITED_CONTENT")
        return {"issues": []}

    book, rid, _ = run_book(conn, cfg, epub, FakeLLM(responders={"review": review}), review_only=True)
    status = {r["unit_id"]: r["status"] for r in conn.execute(
        "SELECT unit_id, status FROM qa_findings WHERE type='possible_added_name'")}
    assert status[other] == "cleared_by_review"  # really reviewed, nothing found
    assert status[refused] == "open"  # never seen by the reviewer: nothing cleared
    assert latest_texts(conn, book.id)[refused]["status"] == "needs_review"
    jobs = {r["target_id"]: r["status"] for r in conn.execute("SELECT target_id, status FROM jobs WHERE run_id=? AND "
                                                             "stage='review'", (rid,))}
    assert jobs[refused] == "blocked" and jobs[other] == "succeeded"


def test_a_failed_review_request_reviews_nothing(conn, cfg, tmp_path):
    epub = small_book(tmp_path)
    book, _, _ = run_book(conn, cfg, epub, FakeLLM())
    uid = unit_of(conn, book, "雨は")
    conn.execute("INSERT INTO qa_findings(book_id, unit_id, type, severity, evidence, created_at) VALUES "
                 "(?,?,?,?,?,?)", (book.id, uid, "possible_added_name", "high", "x", "2026-01-01T00:00:00Z"))
    book, rid, _ = run_book(conn, cfg, epub, FakeLLM(faults={"review": ["rejected"] * 10}), review_only=True)
    assert conn.execute("SELECT status FROM qa_findings WHERE unit_id=? AND type='possible_added_name'",
                        (uid,)).fetchone()[0] == "open"
    assert conn.execute("SELECT status FROM jobs WHERE run_id=? AND stage='review' AND target_id=?",
                        (rid, uid)).fetchone()[0] == "failed"


def test_proofreading_counts_only_what_it_checked_and_checks_its_fixes(conn, cfg, tmp_path):
    checks: list = []

    def proofread(payload, req):
        if any("禁書庫" in p["ja"] for p in payload["paragraphs"]):
            raise ContentBlockedError("HTTP 403 content filter: fake PROHIBITED_CONTENT")
        return {"issues": [{"unit_id": p["unit_id"], "kind": "meaning", "severity": "high", "quote": p["zh"],
                            "problem": "原文是否定，譯文成了肯定", "fix": RIGHT}
                           for p in payload["paragraphs"] if "来なかった" in p["ja"]]}

    def check(payload, req):
        checks.append(payload)
        return {"checks": [{"unit_id": i["unit_id"], "verdict": "still_wrong", "new_error": "", "reason": "仍是肯定"}
                           for i in payload["items"]]}

    cfg.context.reader_pass = False
    lines = LINES[:2] + [f"　{n}番目の駅を過ぎた。" for n in range(1, 9)] + LINES[2:]  # long enough to be halved
    fake = FakeLLM(responders={"translate": translator(None), "proofread": proofread, "fix_check": check})
    book, rid, _ = run_book(conn, cfg, small_book(tmp_path, lines), fake, mode="context")
    uid = unit_of(conn, book, "来なかった")
    assert checks and checks[0]["items"][0]["unit_id"] == uid
    assert latest_texts(conn, book.id)[uid]["status"] == "needs_review"
    job = conn.execute("SELECT status, checkpoint_json FROM jobs WHERE run_id=? AND stage='proofread'", (rid,)).fetchone()
    assert job["status"] == "partial"  # one paragraph was refused: the pass did not check the whole book
    import json

    stats = json.loads(job["checkpoint_json"])
    assert stats["fixed"] == 0 and stats["pending"] == 1 and stats["resolved"] == 0
    # the refused stretch (the smallest part that still holds the refused paragraph) is not counted as checked
    assert 1 <= stats["blocked"] < len(lines) and stats["checked"] + stats["blocked"] == len(lines) + 1  # + heading


def test_an_exportable_book_is_not_called_done_while_a_fix_is_unsettled(conn, cfg, tmp_path):
    from ncte.exporter import export_book
    from ncte.reports.acceptance import acceptance

    clean = write_book(BookSpec("別の本", "urn:uuid:6f1d2a1e-0b7c-4c55-9a43-0000000000f2", [Doc(
        "ch1", "Text/ch1.xhtml", "第一章", _section("第一章", LINES[1:], None, "c1"))]), tmp_path / "clean.epub")
    other, _, _ = run_book(conn, cfg, clean, FakeLLM())  # baseline, nothing flagged
    assert acceptance(conn, other.id, proofread=False, reader=False)["acceptable"]
    book, _, _ = review_once(conn, cfg, small_book(tmp_path), fixed_text=None)  # the fix is undecided
    out = export_book(conn, cfg, book, run_epubcheck=False)
    assert out.validation.ok  # the export works …
    acc = out.acceptance
    assert acc is not None and not acc["acceptable"]  # … but the book is not done
    uid = unit_of(conn, book, "来なかった")
    assert uid in acc["checks"]["fixes_checked"]["units"] and uid in acc["checks"]["no_needs_review"]["units"]
    assert acc["checks"]["epub_valid"]["ok"] and acc["checks"]["translated"]["ok"]
    # proofreading never ran over this book: required in context mode, reported as such
    rep = acceptance(conn, book.id, proofread=True, reader=False)
    assert rep["checks"]["proofread_complete"] == {"ok": False, "count": 1, "units": [], "detail": "not run"}


def test_rechecking_the_rules_does_not_settle_an_unsettled_fix(conn, cfg, tmp_path):
    from ncte.qa.recheck import recheck_book

    book, _, _ = review_once(conn, cfg, small_book(tmp_path), fixed_text=None)
    uid = unit_of(conn, book, "来なかった")
    assert latest_texts(conn, book.id)[uid]["status"] == "needs_review"
    recheck_book(conn, book.id)  # the program's rules find nothing wrong with 「她來了。」
    assert latest_texts(conn, book.id)[uid]["status"] == "needs_review"


def test_a_judging_step_can_use_another_model(conn, cfg, tmp_path):
    cfg.openrouter.stage_models = {"fix_check": "fake/model"}
    cfg.review.automatic_review = "all"
    fake = FakeLLM(responders={"translate": translator(RIGHT), "review": negation_review})
    run_book(conn, cfg, small_book(tmp_path), fake)
    used = {c.stage: c.model for c in fake.calls}
    assert used["fix_check"] == "fake/model" and used["translate"] == used["review"] == cfg.openrouter.model
    rows = conn.execute("SELECT stage, model FROM llm_requests WHERE stage='fix_check'").fetchall()
    assert rows and all(r["model"] == "fake/model" for r in rows)  # recorded (and priced) as that model


def test_a_step_model_must_exist_and_cannot_be_the_translator(conn, cfg, tmp_path):
    import pytest

    cfg.openrouter.stage_models = {"proofread": "nobody/no-such-model"}
    _, _, s = run_book(conn, cfg, small_book(tmp_path), FakeLLM())
    assert s.status != "completed"  # never a silent substitute
    cfg.openrouter.stage_models = {"translate": "fake/model"}
    with pytest.raises(Exception, match="translating model"):
        run_book(conn, cfg, small_book(tmp_path, LINES[1:]), FakeLLM())


def test_the_fix_check_knows_who_speaks_to_whom(conn, cfg, fixtures):
    """A 你／妳 fix cannot be judged from the source alone: the check gets each line's speaker and listener, and the
    cards of those people."""
    from ..scripted import RESPONDERS

    seen: list = []

    def proofread(payload, req):
        return {"issues": [{"unit_id": p["unit_id"], "kind": "speaker", "severity": "high", "quote": p["zh"][:3],
                            "problem": "對象弄錯", "fix": "改"} for p in payload["paragraphs"] if "カイト、遅いですよ" in p["ja"]]}

    def check(payload, req):
        seen.append(payload)
        return {"checks": [{"unit_id": i["unit_id"], "verdict": "undecided", "new_error": "", "reason": "t"}
                           for i in payload["items"]]}

    cfg.context.reader_pass = False
    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "proofread": proofread, "fix_check": check}),
             mode="context")
    assert seen
    item = seen[0]["items"][0]
    assert item["lines"] and item["lines"][0].startswith("米娜")  # ミナ says it (explicit tag in the fixture)
    assert any(c["ja"] == "ミナ" for c in seen[0]["characters"])
