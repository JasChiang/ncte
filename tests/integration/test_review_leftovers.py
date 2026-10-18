"""A review left failed (or interrupted) by one run is done by the next, even when nothing is left to translate; a
passage the content filter refused is not asked again and again; failures stop being retried after a few runs."""

from ncte.errors import ContentBlockedError, RequestRejectedError
from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS


def _review_jobs(conn, rid):
    return {r[0]: r[1] for r in conn.execute("SELECT target_id, status FROM jobs WHERE run_id=? AND stage='review'", (rid,))}


def test_a_failed_review_is_done_by_the_next_run(conn, cfg, fixtures):
    cfg.review.automatic_review = "all"

    def broken(payload, req):
        raise RequestRejectedError("the service failed")

    book, rid1, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "review": broken}),
                             mode="context")
    failed = {u for u, st in _review_jobs(conn, rid1).items() if st == "failed"}
    assert failed
    asked: list[str] = []

    def review(payload, req):
        asked.extend(i["unit_id"] for i in payload["items"])
        return {"issues": []}

    _, rid2, s2 = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "review": review}),
                           mode="context")
    assert s2.status == "completed"
    assert failed <= set(asked)  # nothing to translate, the review still done
    assert all(st == "succeeded" for u, st in _review_jobs(conn, rid2).items() if u in failed)


def test_refused_passages_are_not_asked_again_and_failures_stop(conn, cfg, fixtures):
    cfg.review.automatic_review = "all"

    def refused(payload, req):
        raise ContentBlockedError("refused")

    book, rid1, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "review": refused}),
                             mode="context")
    assert any(st == "blocked" for st in _review_jobs(conn, rid1).values())
    asked: list[str] = []

    def review(payload, req):
        asked.extend(i["unit_id"] for i in payload["items"])
        return {"issues": []}

    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "review": review}), mode="context")
    assert not asked  # a refusal is kept as a finding, not retried run after run

    def broken(payload, req):
        raise RequestRejectedError("the service failed")

    conn.execute("UPDATE jobs SET status='failed' WHERE stage='review'")
    for _ in range(4):
        run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "review": broken}), mode="context")
    asked.clear()
    run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "review": review}), mode="context")
    assert not asked  # failed in three runs already: left for a person
