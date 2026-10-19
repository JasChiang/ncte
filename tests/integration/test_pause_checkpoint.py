"""A pause (the web page's button) or a kill in the passes after translating: each stops before its next batch, and
proofreading and the reader's look keep how far they got (``jobs.checkpoint_json``) so a resumed run goes on from
there instead of checking the whole book again."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ncte.analysis import proofread as pr
from ncte.analysis import reader as rd
from ncte.llm.fake import FakeLLM

from ..helpers import orchestrator_for, resume_run, start_run
from ..scripted import RESPONDERS


def _flag_first(key: str):
    def answer(payload: dict[str, Any], req: Any) -> dict[str, Any]:
        p = payload["paragraphs"][0]
        if key == "reader":
            return {"issues": [{"unit_id": p["unit_id"], "kind": "unclear", "severity": "high", "problem": "看不懂"}]}
        return {"issues": [{"unit_id": p["unit_id"], "kind": "meaning", "severity": "high", "problem": "原文是否定",
                            "quote": p["zh"][:2], "fix": "改成否定"}]}
    return answer


class Stopping(FakeLLM):
    """Answers like the fake model; at the k-th request of ``stage`` it pauses the run after answering (``how`` =
    "pause", as the web page does) or is killed while asking ("kill")."""

    def __init__(self, stage: str, k: int, how: str = "pause", **kw: Any):
        super().__init__(**kw)
        self.stage, self.k, self.how = stage, k, how
        self.asked: dict[str, int] = {}
        self.orch: Any = None

    def complete(self, req: Any) -> Any:
        st = req.stage.split(":")[0]
        self.asked[st] = self.asked.get(st, 0) + 1
        if st == self.stage and self.asked[st] == self.k and self.how == "kill":
            raise KeyboardInterrupt
        resp = super().complete(req)
        if st == self.stage and self.asked[st] == self.k and self.how == "pause":
            self.orch._stop_reason = "interrupted"
        return resp


class Counting(FakeLLM):
    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.asked: dict[str, int] = {}
        self.orch: Any = None

    def complete(self, req: Any) -> Any:
        st = req.stage.split(":")[0]
        self.asked[st] = self.asked.get(st, 0) + 1
        return super().complete(req)


def _run(conn, cfg, fixtures, client):
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], client, mode="context")
    orch = orchestrator_for(conn, cfg, book, rid, opts, client)
    client.orch = orch
    return book, rid, orch.run()


def _job(conn, rid, stage):
    return conn.execute("SELECT status, checkpoint_json FROM jobs WHERE run_id=? AND stage=?", (rid, stage)).fetchone()


@pytest.mark.parametrize("how,done", [("pause", 2), ("kill", 1)])
def test_proofreading_paused_or_killed_goes_on_from_its_checkpoint(conn, cfg, fixtures, how, done):
    cfg.context.proofread_chunk_units = 3
    cfg.openrouter.max_concurrency = 1  # one stretch at a time (side by side: test_parallel_passes)
    responders = {**RESPONDERS, "proofread": _flag_first("proofread")}
    client = Stopping("proofread", 2, how, responders=responders)
    book, rid, summary = _run(conn, cfg, fixtures, client)
    assert summary.status == "interrupted"
    total = len(pr.chunks(conn, book.id, size=3))
    assert total > 3
    job = _job(conn, rid, "proofread")
    cp = json.loads(job["checkpoint_json"])
    assert job["status"] == "running"  # not done: the resumed run picks it up
    last = pr.chunks(conn, book.id, size=3)[done - 1][-1]["_at"]
    assert cp["done_until_ordinal"] == last and cp["checked"] == sum(
        len(p) for p in pr.chunks(conn, book.id, size=3)[:done])
    assert _job(conn, rid, "reader")["status"] == "pending"  # a paused run starts nothing after the pause

    again = Counting(responders=responders)
    summary = resume_run(conn, cfg, rid, again)
    assert summary.status == "completed", summary
    assert again.asked.get("proofread") == total - done  # only the stretches not checked yet
    found = [r[0] for r in conn.execute("SELECT unit_id FROM qa_findings WHERE run_id=? AND type LIKE "
                                        "'review:proofread%'", (rid,))]
    assert len(found) == len(set(found)) == total  # one per stretch, none found twice
    job = _job(conn, rid, "proofread")
    cp = json.loads(job["checkpoint_json"])
    assert job["status"] == "succeeded" and cp["checked"] == sum(len(p) for p in pr.chunks(conn, book.id, size=3))
    assert cp["retranslated"] == total  # the counts add up over both runs


def test_the_readers_look_goes_on_from_its_checkpoint(conn, cfg, fixtures, monkeypatch):
    monkeypatch.setattr(rd, "CHUNK_UNITS", 3)
    cfg.openrouter.max_concurrency = 1  # one stretch at a time (side by side: test_parallel_passes)
    responders = {**RESPONDERS, "reader": _flag_first("reader")}
    client = Stopping("reader", 1, responders=responders)
    book, rid, summary = _run(conn, cfg, fixtures, client)
    assert summary.status == "interrupted"
    total = len(rd.chunks(conn, book.id))
    job = _job(conn, rid, "reader")
    assert job["status"] == "running" and json.loads(job["checkpoint_json"])["done_until_ordinal"] == \
        rd.chunks(conn, book.id)[0][-1]["_at"]
    again = Counting(responders=responders)
    assert resume_run(conn, cfg, rid, again).status == "completed"
    assert again.asked.get("reader") == total - 1
    job = _job(conn, rid, "reader")
    assert job["status"] == "succeeded" and json.loads(job["checkpoint_json"])["read"] == sum(
        len(p) for p in rd.chunks(conn, book.id))


@pytest.mark.parametrize("stage,size", [("verify", "ncte.translation.context_pipeline.VERIFY_BATCH"),
                                        ("relations", "ncte.analysis.relations.PAIRS_PER_REQUEST"),
                                        ("impressions", "ncte.analysis.impressions.PER_REQUEST")])
def test_a_pause_in_a_whole_book_step_is_kept_and_the_step_is_done_on_resume(conn, cfg, fixtures, monkeypatch,
                                                                            stage, size, tmp_path):
    from ncte.memory.db import connect, migrate

    monkeypatch.setattr(size, 1)  # one item per request: a pause can fall between two
    probe = Counting(responders=dict(RESPONDERS))
    other = connect(tmp_path / "probe.db")  # its own database: a book done there would not redo the step (2.11)
    migrate(other)
    _run(other, cfg, fixtures, probe)
    if probe.asked.get(stage, 0) < 2:
        pytest.skip(f"the sample book asks {stage} fewer than two times")
    client = Stopping(stage, 1, responders=dict(RESPONDERS))
    book, rid, summary = _run(conn, cfg, fixtures, client)
    assert summary.status == "interrupted"
    assert client.asked[stage] == 1  # nothing more asked after the pause
    assert _job(conn, rid, stage)["status"] == "running"  # not marked done with its work cut short
    assert "translate" not in client.asked
    again = Counting(responders=dict(RESPONDERS))
    assert resume_run(conn, cfg, rid, again).status == "completed"
    assert again.asked.get(stage) and _job(conn, rid, stage)["status"] == "succeeded"
