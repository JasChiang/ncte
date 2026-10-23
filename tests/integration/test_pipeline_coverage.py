"""ADR-102: a whole-book step whose request failed is partial, not done, and a resumed run asks only what it left; the
same-form words are asked side by side; the fixes one checker's batch asked for are checked together; an empty answer
keeps what the turn took."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from ncte.analysis import false_friends as ff
from ncte.analysis import relations
from ncte.llm.fake import FakeLLM, default_translate
from ncte.reports.acceptance import acceptance
from ncte.reports.cost_report import cost_report
from ncte.translation import review

from ..helpers import resume_run, run_book
from ..scripted import RESPONDERS

# step → (the request's stage, the key of what it asks about, the item's own key in the request)
STEPS = {"verify": ("verify", "items", "ref"), "gender_gaps": ("verify:genders", "characters", "name"),
         "relations": ("relations", "pairs", "pair"), "impressions": ("impressions", "characters", "name"),
         "house_style": ("house_style", "characters", "name"), "terms": ("terms", "terms", "ja"),
         "false_friends": ("false_friends", "words", "ja")}


def _job(conn, rid: str, stage: str) -> tuple[str, dict[str, Any]]:
    row = conn.execute("SELECT status, checkpoint_json FROM jobs WHERE run_id=? AND stage=?", (rid, stage)).fetchone()
    return row["status"], json.loads(row["checkpoint_json"])


def _asked(client: FakeLLM, stage: str, key: str, own: str) -> list[Any]:
    # a request's own items, not the pairs that came back empty asked once more (``recheck``)
    return [i.get(own) for c, p in zip(client.calls, client.payloads, strict=True) if c.stage == stage
            and not p.get("recheck") for i in p.get(key) or []]


@pytest.mark.parametrize("step", list(STEPS))
def test_a_step_whose_request_never_answers_is_partial_and_a_resume_asks_only_what_it_left(conn, cfg, fixtures, step):
    """The ginga run: one of three second-look requests came back empty on every try, its items were never asked, and
    the step said succeeded. Now the step is partial with those items listed, the acceptance report says the book is
    not done, and a resumed run asks those items and nothing else."""
    stage, key, own = STEPS[step]
    first = FakeLLM(responders=RESPONDERS, faults={stage: ["empty_answer"] * 99})
    book, rid, s = run_book(conn, cfg, fixtures["basic"], first, mode="context")
    assert s.status == "completed"
    status, cp = _job(conn, rid, step)
    assert status == "partial" and cp["unasked_ids"] and cp["unasked"] == len(cp["unasked_ids"])
    asked_first = _asked(first, stage, key, own)
    assert asked_first  # sent (every try came back empty)
    rep = acceptance(conn, book.id)
    assert not rep["checks"]["steps_complete"]["ok"] and step in rep["checks"]["steps_complete"]["detail"]
    others = [st for st in STEPS if st != step]
    before = {st: _job(conn, rid, st) for st in others}

    again = FakeLLM(responders=RESPONDERS)
    assert resume_run(conn, cfg, rid, again).status == "completed"
    asked = _asked(again, stage, key, own)
    assert asked and set(asked) <= set(asked_first) and len(asked) == len(set(asked))  # those only, each once
    status, cp = _job(conn, rid, step)
    assert status == "succeeded" and cp["unasked"] == 0 and "unasked_ids" not in cp
    assert {st: _job(conn, rid, st) for st in others} == before  # the other steps were not done again
    assert not [c for c in again.calls if c.stage.split(":")[0] in {STEPS[st][0].split(":")[0] for st in others}
                and c.stage != stage]
    assert acceptance(conn, book.id)["checks"]["steps_complete"]["ok"]


def test_a_step_partial_only_for_what_was_refused_is_not_asked_again(conn, cfg, fixtures):
    """The content filter refusing it again is all asking again would give (ADR-098 does the same)."""
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    conn.execute("UPDATE jobs SET status='partial', checkpoint_json=? WHERE run_id=? AND stage='gender_gaps'",
                 (json.dumps({"asked": 1, "blocked": 1, "failed": 0, "unasked": 0}), rid))
    again = FakeLLM(responders=RESPONDERS)
    resume_run(conn, cfg, rid, again)
    assert not [c for c in again.calls if c.stage == "verify:genders"]
    assert _job(conn, rid, "gender_gaps")[0] == "partial"


def test_a_second_look_left_partial_asks_only_the_items_of_its_failed_request(conn, cfg, fixtures, monkeypatch):
    from ncte.translation import context_pipeline

    monkeypatch.setattr(context_pipeline, "VERIFY_BATCH", 1)  # one request per item
    sent: list[list[str]] = []
    failing: list[str] = []

    def verify(payload: dict[str, Any], req: Any) -> dict[str, Any]:
        refs = [i["ref"] for i in payload["items"]]
        sent.append(refs)
        if not failing:
            failing.append(refs[0])
        if refs == failing:
            from ncte.errors import EmptyAnswerError

            raise EmptyAnswerError("empty answer")
        return {"items": []}

    book, rid, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "verify": verify}),
                            mode="context")
    status, cp = _job(conn, rid, "verify")
    assert len({r for refs in sent for r in refs}) > 1  # the others were answered
    assert status == "partial" and cp["unasked_ids"] == failing
    f = conn.execute("SELECT severity FROM qa_findings WHERE run_id=? AND type='verify_failed'", (rid,)).fetchone()
    assert f["severity"] == "medium"
    again: list[list[str]] = []

    def answers(payload: dict[str, Any], req: Any) -> dict[str, Any]:
        again.append([i["ref"] for i in payload["items"]])
        return {"items": []}

    resume_run(conn, cfg, rid, FakeLLM(responders={**RESPONDERS, "verify": answers}))
    assert again == [failing] and _job(conn, rid, "verify")[0] == "succeeded"


def test_relations_asked_again_replace_only_their_own_pairs(conn, cfg, fixtures):
    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    rows = conn.execute("SELECT id FROM characters WHERE book_id=? ORDER BY known_from_ordinal LIMIT 3",
                        (book.id,)).fetchall()
    a, b, c = (r["id"] for r in rows)
    for x, y in ((a, b), (a, c)):
        conn.execute("INSERT INTO relations(book_id, a_id, b_id, relation_zh, since_ordinal, fact_ids_json, reason, "
                     "created_at, origin) VALUES (?,?,?,?,?,?,?,?,?)", (book.id, x, y, "同伴", 1, "[]", "", "t", "llm"))
    relations.apply(conn, book.id, [], only=[(c, a)])  # asked again about a–c, nothing came back
    left = {(r["a_id"], r["b_id"]) for r in conn.execute("SELECT * FROM relations WHERE book_id=?", (book.id,))}
    assert left == {(a, b)}


def _differs(payload: dict[str, Any], req: Any) -> dict[str, Any]:
    """Every word reads otherwise in Taiwan Chinese."""
    return {"words": [{"ref": w["ref"], "verdict": "differs", "suggestion": w["zh"] + "兒", "reason": "t",
                       "evidence": [w["examples"][0]["ref"]]} for w in payload["words"]]}


def test_same_form_words_are_asked_side_by_side_with_the_same_requests(conn, cfg, fixtures, monkeypatch):
    """5 requests one after another took 13.5 minutes in the ginga run: they go side by side now, as the other steps
    after reading send theirs, and each request is the one the program built (so a kept answer is reused, ADR-094)."""
    monkeypatch.setattr(ff, "PER_REQUEST", 5)
    built: list[dict[str, Any]] = []
    real = ff.requests

    def requests(words: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        out = real(words)
        built.extend(req for req, _ in out)
        return out

    monkeypatch.setattr(ff, "requests", requests)
    lock = threading.Lock()
    flying = [0, 0]  # in flight now, most at once
    both = threading.Barrier(2, timeout=10)  # the first two meet only when both are in flight (not a timing guess)
    first = [0]

    def answer(payload: dict[str, Any], req: Any) -> dict[str, Any]:
        with lock:
            flying[0] += 1
            flying[1] = max(flying)
            first[0] += 1
            wait = first[0] <= 2
        if wait:
            both.wait()
        with lock:
            flying[0] -= 1
        return {"words": [{"ref": w["ref"], "verdict": "same", "reason": "t", "evidence": [w["examples"][0]["ref"]]}
                          for w in payload["words"]]}

    client = FakeLLM(responders={**RESPONDERS, "false_friends": answer})
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    sent = [p for c, p in zip(client.calls, client.payloads, strict=True) if c.stage == "false_friends"]
    assert len(built) > 2 and flying[1] == cfg.openrouter.max_concurrency == 2
    key = lambda p: json.dumps(p, sort_keys=True, ensure_ascii=False)  # noqa: E731
    assert sorted(map(key, sent)) == sorted(map(key, built))


def test_same_form_fixes_go_side_by_side_written_in_story_order_and_checked_together(conn, cfg, fixtures):
    """32 minutes in the ginga run, a scene at a time, each one's fixes checked before the next was sent. The scenes'
    fixes are retranslated side by side now — what comes back first is still written in story order — and checked
    together, as many fixes to a request as the check takes."""
    first_scene: list[str] = []

    def translate(payload: dict[str, Any], req: Any) -> dict[str, Any]:
        if payload.get("review_feedback") and not first_scene:
            first_scene.append(payload["units"][0]["unit_id"])
            time.sleep(0.2)  # the first scene's fixes come back last
        return default_translate(payload, req)

    client = FakeLLM(responders={**RESPONDERS, "false_friends": _differs, "translate": translate})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    fixes = conn.execute("SELECT t.id, u.ordinal, u.scene_id FROM translations t JOIN units u ON u.id=t.unit_id "
                         "WHERE t.run_id=? AND t.flags_json LIKE '%review_retranslation%' ORDER BY t.id",
                         (rid,)).fetchall()
    assert len({f["scene_id"] for f in fixes}) >= 3
    assert [f["ordinal"] for f in fixes] == sorted(f["ordinal"] for f in fixes)  # written in story order
    checks = [p for c, p in zip(client.calls, client.payloads, strict=True) if c.stage == "fix_check"]
    first_pass = [x for p in checks for x in p["items"]]
    assert len(checks) == -(-len(fixes) // review.FIX_CHECK_BATCH)  # every fix checked, as few requests as can be
    assert {i["unit_id"] for i in first_pass} == {r[0] for r in conn.execute(
        "SELECT unit_id FROM translations WHERE run_id=? AND flags_json LIKE '%review_retranslation%'", (rid,))}
    f = conn.execute("SELECT COUNT(*) FROM qa_findings WHERE run_id=? AND type='review:false_friend' AND "
                     "status='open'", (rid,)).fetchone()[0]
    assert f == 0  # every fix settled by its check


def test_one_checker_batch_with_fixes_in_three_scenes_makes_one_fix_check_request(conn, cfg, fixtures):
    """The ginga run made 48 fix check requests of about two paragraphs each: one per scene a stretch fixed. The
    reader's look reads the sample book in one request; points in three scenes now make one fix check, not three."""
    picked: list[str] = []

    def reader(payload: dict[str, Any], req: Any) -> dict[str, Any]:
        paras = payload["paragraphs"]
        if not picked:
            picked.extend(paras[i]["unit_id"] for i in (1, 14, 22))  # 13 + 8 + 12 + 8 paragraphs in four scenes
        return {"issues": [{"unit_id": u, "kind": "unclear", "severity": "medium", "problem": "讀者看不懂"}
                           for u in picked if u in {p["unit_id"] for p in paras}]}

    client = FakeLLM(responders={**RESPONDERS, "reader": reader})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    scenes = {r[0] for r in conn.execute(f"SELECT scene_id FROM units WHERE id IN ({','.join('?' * len(picked))})",
                                         picked)}
    assert len(scenes) == 3
    sent = [p for c, p in zip(client.calls, client.payloads, strict=True) if c.stage == "translate"
            and p.get("review_feedback")]
    assert len(sent) == 3  # retranslated a scene at a time, each with its scene's memory
    checks = [p for c, p in zip(client.calls, client.payloads, strict=True) if c.stage == "fix_check"]
    assert len(checks) == 1 and sorted(i["unit_id"] for i in checks[0]["items"]) == sorted(picked)
    st = conn.execute("SELECT status FROM qa_findings WHERE run_id=? AND type LIKE 'review:reader_%'", (rid,)).fetchall()
    assert len(st) == 3 and all(r["status"] == "resolved" for r in st)
    _, cp = _job(conn, rid, "reader")
    assert cp["issues"] == 3 and cp["fixed"] == 3 and cp["retranslated"] == 3


def test_a_proofreading_stretch_over_two_scenes_checks_its_fixes_in_one_request(conn, cfg, fixtures):
    flagged: list[str] = []

    def proofread(payload: dict[str, Any], req: Any) -> dict[str, Any]:
        paras = payload["paragraphs"]
        if not flagged:
            flagged.extend(paras[i]["unit_id"] for i in (2, 15))  # the first chapter: 13 + 8 paragraphs, two scenes
        return {"issues": [{"unit_id": p["unit_id"], "kind": "meaning", "severity": "high", "quote": p["zh"][:2],
                            "problem": "原文是否定，譯文成了肯定", "fix": "改成否定"}
                           for p in paras if p["unit_id"] in flagged]}

    client = FakeLLM(responders={**RESPONDERS, "proofread": proofread})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    assert len({r[0] for r in conn.execute("SELECT scene_id FROM units WHERE id IN (?,?)", flagged)}) == 2
    checks = [p for c, p in zip(client.calls, client.payloads, strict=True) if c.stage == "fix_check"]
    # the first pass's stretch: one request for its two scenes' fixes (a later pass finds them again on the new text)
    assert sorted(i["unit_id"] for i in checks[0]["items"]) == sorted(flagged)


def test_fixes_whose_check_never_answered_are_asked_again_together(conn, cfg, fixtures):
    """ADR-100's check before the run ends asks the fixes still waiting together too, not a scene at a time."""
    picked: list[str] = []

    def reader(payload: dict[str, Any], req: Any) -> dict[str, Any]:
        if not picked:
            picked.extend(payload["paragraphs"][i]["unit_id"] for i in (1, 14, 22))
        return {"issues": [{"unit_id": u, "kind": "unclear", "severity": "medium", "problem": "讀者看不懂"}
                           for u in picked]}

    # the batch, then each fix once more on its own (ADR-100): empty on every try (1 + EMPTY_RETRIES each)
    client = FakeLLM(responders={**RESPONDERS, "reader": reader}, faults={"fix_check": ["empty_answer"] * 16})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    checks = [p for c, p in zip(client.calls, client.payloads, strict=True) if c.stage == "fix_check"]
    assert [len(p["items"]) for p in checks] == [3] * 4 + [1] * 12
    assert len(conn.execute("SELECT 1 FROM qa_findings WHERE run_id=? AND type='fix_unchecked' AND status='open'",
                            (rid,)).fetchall()) == 3
    again = FakeLLM(responders=RESPONDERS)
    resume_run(conn, cfg, rid, again)
    checks = [p for c, p in zip(again.calls, again.payloads, strict=True) if c.stage == "fix_check"]
    assert [len(p["items"]) for p in checks] == [3]  # resumed: the three asked together
    assert not conn.execute("SELECT 1 FROM qa_findings WHERE run_id=? AND type='fix_unchecked' AND status='open'",
                            (rid,)).fetchone()


def test_an_empty_answer_keeps_what_the_turn_took_on_its_row(conn, cfg, fixtures):
    """The ginga run's 27 empty answers had no tokens and no time on their rows: the plan they used was invisible."""
    client = FakeLLM(responders=RESPONDERS, faults={"verify": ["empty_answer"]})
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    row = conn.execute("SELECT * FROM llm_requests WHERE run_id=? AND stage='verify' ORDER BY id", (rid,)).fetchone()
    u = json.loads(row["usage_json"])
    assert row["status"] == "failed" and row["error"].startswith("EmptyAnswerError")
    assert row["latency_ms"] == 7 and u["prompt_tokens"] > 0 and u["completion_tokens"] == 40 and u["payload_chars"]
    assert _job(conn, rid, "verify")[0] == "succeeded"  # asked again (translator.EMPTY_RETRIES) and answered
    tot = cost_report(conn, run_id=rid)["totals"]
    assert tot["failed_requests"] >= 1 and tot["latency_ms_total"] >= 7
