"""ADR-098: a run whose proofreading ended partial because paragraphs came back without a verdict asks only those
paragraphs again when it is resumed; the pass is complete when every one has a verdict, and still partial (with a
record per paragraph) when some have none."""

from __future__ import annotations

import json
from typing import Any

from ncte.llm.fake import FakeLLM

from ..helpers import resume_run, run_book
from ..scripted import RESPONDERS


class Proofreader:
    """Gives a verdict on the first paragraph of each request only while ``mode`` is "first", on every one while it
    is "all"; a paragraph in ``flag`` gets a meaning error. What each request was shown (and on which step) is kept."""

    def __init__(self, mode: str = "first") -> None:
        self.mode = mode
        self.flag: set[str] = set()
        self.asked: list[tuple[str, list[str]]] = []  # (step, paragraphs)

    def __call__(self, payload: dict[str, Any], req: Any) -> dict[str, Any]:
        paras = payload["paragraphs"]
        self.asked.append((req.stage, [p["unit_id"] for p in paras]))
        # "first2": the first pass leaves gaps, the second (on the cross model) answers for every paragraph
        first = self.mode == "first" or (self.mode == "first2" and req.stage == "proofread")
        given = paras[:1] if first else paras
        return {"checked": [{"unit_id": p["unit_id"], "gist": "g", "ok": True} for p in given
                            if p["unit_id"] not in self.flag],
                "issues": [{"unit_id": p["unit_id"], "kind": "meaning", "severity": "high", "quote": p["zh"][:2],
                            "problem": "原文是否定，譯文成了肯定", "fix": "改成否定"} for p in given
                           if p["unit_id"] in self.flag]}


def _job(conn, rid) -> tuple[str, dict[str, Any]]:
    row = conn.execute("SELECT status, checkpoint_json FROM jobs WHERE run_id=? AND stage='proofread'", (rid,)).fetchone()
    return row["status"], json.loads(row["checkpoint_json"])


def _open_records(conn, rid) -> list[str]:
    return sorted(r[0] for r in conn.execute("SELECT unit_id FROM qa_findings WHERE run_id=? AND "
                                             "type='proofread_no_verdict' AND status='open'", (rid,)))


def _partial_run(conn, cfg, fixtures) -> tuple[str, Proofreader, set[str], dict[str, Any]]:
    cfg.context.proofread_verdicts = True
    pr = Proofreader("first")
    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "proofread": pr}),
                            mode="context")
    status, cp = _job(conn, rid)
    gaps = set(cp["no_verdict_ids"])
    assert s.status == "completed" and status == "partial" and gaps and cp["no_verdict"] == len(gaps)
    assert _open_records(conn, rid) == sorted(gaps)
    return rid, pr, gaps, cp


def test_resume_asks_only_the_paragraphs_without_a_verdict_and_completes_the_pass(conn, cfg, fixtures):
    rid, _, gaps, before = _partial_run(conn, cfg, fixtures)
    pr = Proofreader("all")
    flagged = sorted(gaps)[0]
    pr.flag = {flagged}  # an error found now goes back to the translator and its fix is checked (ADR-055)
    checks: list[str] = []

    def fix_check(payload: dict[str, Any], req: Any) -> dict[str, Any]:
        checks.extend(i["unit_id"] for i in payload["items"])
        return {"checks": [{"unit_id": i["unit_id"], "gist": "g", "verdict": "fixed", "new_error": "", "reason": "r"}
                           for i in payload["items"]]}

    s = resume_run(conn, cfg, rid, FakeLLM(responders={**RESPONDERS, "proofread": pr, "fix_check": fix_check}))
    assert s.status == "completed"
    sent = [u for _, ids in pr.asked for u in ids]
    assert sent and set(sent) == gaps and len(sent) == len(gaps)  # each once; none of those with a verdict
    status, cp = _job(conn, rid)
    assert status == "succeeded" and cp["no_verdict"] == 0 and "no_verdict_ids" not in cp
    assert cp["checked"] == before["checked"] + len(gaps) and cp["verdicts_filled"] == len(gaps)
    assert cp["sent"] == before["sent"]  # sent once in the pass already
    assert cp["upto_translation_id"] == before["upto_translation_id"]
    assert cp["retranslated"] == before["retranslated"] + 1
    assert checks == [flagged]
    assert _open_records(conn, rid) == []  # the records of the earlier try are replaced
    # resumed again: nothing left to ask
    again = Proofreader("all")
    resume_run(conn, cfg, rid, FakeLLM(responders={**RESPONDERS, "proofread": again}))
    assert again.asked == []


def test_paragraphs_still_without_a_verdict_stay_listed_with_a_record_and_the_pass_stays_partial(conn, cfg, fixtures):
    rid, _, gaps, before = _partial_run(conn, cfg, fixtures)
    pr = Proofreader("first")  # answers again for the first paragraph of each request only
    resume_run(conn, cfg, rid, FakeLLM(responders={**RESPONDERS, "proofread": pr}))
    sent = {u for _, ids in pr.asked for u in ids}
    assert sent == gaps
    status, cp = _job(conn, rid)
    left = set(cp["no_verdict_ids"])
    assert status == "partial" and left and left < gaps and cp["no_verdict"] == len(left)
    assert cp["checked"] == before["checked"] + len(gaps) - len(left)
    assert _open_records(conn, rid) == sorted(left)  # one open record each, the earlier ones closed
    # one more try per resume: the next one asks only what is still left
    nxt = Proofreader("all")
    resume_run(conn, cfg, rid, FakeLLM(responders={**RESPONDERS, "proofread": nxt}))
    assert {u for _, ids in nxt.asked for u in ids} == left
    assert _job(conn, rid)[0] == "succeeded" and _open_records(conn, rid) == []


def test_a_stop_during_the_fill_keeps_what_was_not_asked_for_the_next_resume(conn, cfg, fixtures):
    rid, _, gaps, _ = _partial_run(conn, cfg, fixtures)
    cfg.context.proofread_chunk_units = 1  # one request per paragraph

    class Pausing(Proofreader):
        orch: Any = None

        def __call__(self, payload: dict[str, Any], req: Any) -> dict[str, Any]:
            out = super().__call__(payload, req)
            self.orch._stop_reason = "interrupted"  # paused after the first answer
            return out

    from ncte.library import resolve_book
    from ncte.translation.orchestrator import load_run

    from ..helpers import orchestrator_for

    row, rcfg, opts = load_run(conn, rid)
    rcfg.app.workspace = cfg.app.workspace
    rcfg.context.proofread_chunk_units = 1
    rcfg.openrouter.max_concurrency = 1
    pr = Pausing("all")
    fake = FakeLLM(responders={**RESPONDERS, "proofread": pr})
    orch = orchestrator_for(conn, rcfg, resolve_book(conn, row["book_id"]), rid, opts, fake)
    pr.orch = orch
    orch.run()
    asked = {u for _, ids in pr.asked for u in ids}
    status, cp = _job(conn, rid)
    assert len(asked) == 1 and status == "partial" and set(cp["no_verdict_ids"]) == gaps - asked
    assert _open_records(conn, rid) == sorted(gaps - asked)


def test_a_later_pass_does_not_stand_in_for_the_one_that_left_paragraphs_without_a_verdict(conn, cfg, fixtures):
    """Two passes, the second on the cross model: the first left gaps, the second answered for every paragraph. The
    resumed run asks the first pass's gaps on the first pass's step; the second pass's counts stay as they were."""
    cfg.context.proofread_verdicts = True
    cfg.context.proofread_passes = 2
    cfg.review.cross_proofread_model = "fake/model"
    pr = Proofreader("first2")
    _, rid, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "proofread": pr}),
                         mode="context")
    status, cp = _job(conn, rid)
    gaps = set(cp["passes"][0]["no_verdict_ids"])
    assert status == "partial" and gaps and "no_verdict_ids" not in cp["passes"][1]
    second = cp["passes"][1]
    fill = Proofreader("all")
    resume_run(conn, cfg, rid, FakeLLM(responders={**RESPONDERS, "proofread": fill}))
    assert fill.asked and {st for st, _ in fill.asked} == {"proofread"}
    assert {u for _, ids in fill.asked for u in ids} == gaps
    status, cp = _job(conn, rid)
    assert status == "succeeded" and cp["passes"][1] == second and cp["passes"][0]["no_verdict"] == 0
    assert cp["no_verdict"] == 0 and cp["checked"] == sum(p["checked"] for p in cp["passes"])


def test_the_web_page_offers_to_ask_them_again_on_a_finished_run(conn, cfg, fixtures):
    """A run that finished has no 「繼續翻譯」; with paragraphs left without a verdict the page says how many, and
    continuing asks only them (GUI self-service)."""
    from ncte.gui.runner import Runner

    rid, _, gaps, _ = _partial_run(conn, cfg, fixtures)
    book_id = conn.execute("SELECT book_id FROM runs WHERE id=?", (rid,)).fetchone()[0]
    runner = Runner(cfg, lambda c: FakeLLM(), client="fake")
    run = runner.progress(None, conn, book_id)["run"]
    assert run["status"] == "completed" and run["unanswered"] == len(gaps)
    resume_run(conn, cfg, rid, FakeLLM(responders={**RESPONDERS, "proofread": Proofreader("all")}))
    assert runner.progress(None, conn, book_id)["run"]["unanswered"] == 0
