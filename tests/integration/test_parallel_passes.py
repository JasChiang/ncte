"""4.1 (ADR-065): proofreading and the reader's look send their stretches side by side (``openrouter.max_concurrency``
at a time), each stretch with its retranslations and their checks; the outcome is the same as one at a time, and the
checkpoint moves only as far as the stretches done without a gap."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from ncte.analysis import proofread as pr
from ncte.analysis import reader as rd
from ncte.llm.fake import FakeLLM
from ncte.memory.db import connect, migrate

from ..helpers import orchestrator_for, resume_run, start_run
from ..scripted import RESPONDERS


def _flag(payload: dict[str, Any], req: Any) -> dict[str, Any]:
    """A finding on the first paragraph of every stretch, and an ambiguity on the second."""
    ps = payload["paragraphs"]
    if payload.get("task") == "reader":
        return {"issues": [{"unit_id": ps[0]["unit_id"], "kind": "unclear", "severity": "high", "problem": "看不懂"}]}
    out = [{"unit_id": ps[0]["unit_id"], "kind": "meaning", "severity": "high", "problem": "原文是否定",
            "quote": ps[0]["zh"][:2], "fix": "改成否定"}]
    if len(ps) > 1:
        out.append({"unit_id": ps[1]["unit_id"], "kind": "ambiguity", "severity": "medium", "problem": "兩可",
                    "quote": ps[1]["zh"][:2], "fix": "另一讀法"})
    return {"issues": out}


RESP = {**RESPONDERS, "proofread": _flag, "reader": _flag}


class Watching(FakeLLM):
    """The fake model, a little slow, counting how many requests of a step are in flight at once; optionally killed
    (KeyboardInterrupt) or paused at the k-th request of ``stop_at`` = (stage, k, how)."""

    def __init__(self, stop_at: tuple[str, int, str] | None = None, **kw: Any):
        super().__init__(**kw)
        self.stop_at = stop_at
        self.asked: dict[str, int] = {}
        self.now: dict[str, int] = {}
        self.most: dict[str, int] = {}
        self.guard = threading.Lock()
        self.orch: Any = None

    def complete(self, req: Any) -> Any:
        st = req.stage.split(":")[0]
        # a scene's first translation, not a fix sent back by a later pass
        first = st == "translate" and "review_feedback" not in req.messages[-1]["content"]
        with self.guard:
            self.asked[st] = self.asked.get(st, 0) + 1
            n = self.asked[st]
            for key in (st, "first") if first else (st,):
                self.now[key] = self.now.get(key, 0) + 1
                self.most[key] = max(self.most.get(key, 0), self.now[key])
        try:
            if self.stop_at and self.stop_at[:2] == (st, n) and self.stop_at[2] == "kill":
                time.sleep(0.3)  # the stretches started with it finish first: some are done past the gap
                raise KeyboardInterrupt
            if st in ("proofread", "reader", "translate", "fix_check"):
                time.sleep(0.02)
            resp = super().complete(req)
            if self.stop_at and self.stop_at[:2] == (st, n) and self.stop_at[2] == "pause":
                self.orch._stop_reason = "interrupted"
            return resp
        finally:
            with self.guard:
                for key in (st, "first") if first else (st,):
                    self.now[key] -= 1


def _book(conn, cfg, fixtures, client):
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], client, mode="context")
    orch = orchestrator_for(conn, cfg, book, rid, opts, client)
    client.orch = orch
    return book, rid, orch.run()


def _outcome(conn, rid):
    """What the passes left: findings, translations, the jobs' counts (ids and times aside)."""
    def rows(sql: str) -> list[tuple[Any, ...]]:
        return sorted((tuple(r) for r in conn.execute(sql, (rid,))), key=lambda t: tuple(str(x) for x in t))

    findings = rows("SELECT unit_id, type, severity, status, evidence FROM qa_findings WHERE run_id=?")
    texts = rows("SELECT unit_id, text, status, flags_json FROM translations WHERE run_id=?")
    counts = {}
    for stage in ("proofread", "reader"):
        row = conn.execute("SELECT status, checkpoint_json FROM jobs WHERE run_id=? AND stage=?", (rid, stage)).fetchone()
        cp = json.loads(row["checkpoint_json"])
        cp.pop("upto_translation_id", None)
        counts[stage] = (row["status"], cp)
    return findings, texts, counts


def _db(tmp_path, name):
    c = connect(tmp_path / f"{name}.db")
    migrate(c)
    return c


def test_side_by_side_the_passes_leave_what_one_at_a_time_leaves(cfg, fixtures, tmp_path, monkeypatch):
    cfg.context.proofread_chunk_units = 3
    monkeypatch.setattr(rd, "CHUNK_UNITS", 3)
    results = {}
    for conc in (1, 3):
        cfg.openrouter.max_concurrency = conc
        conn = _db(tmp_path, f"c{conc}")
        client = Watching(responders=RESP)
        _book_, rid, summary = _book(conn, cfg, fixtures, client)
        assert summary.status == "completed", summary
        results[conc] = (_outcome(conn, rid), dict(client.asked), client.most)
        total = len(pr.chunks(conn, _book_.id, size=3))
        conn.close()
    one, three = results[1], results[3]
    assert total > 3
    assert one[0][0] == three[0][0]  # the same findings (and their outcomes)
    assert one[0][1] == three[0][1]  # the same translations, retranslations included
    assert one[0][2] == three[0][2]  # the same counts: issues, retranslated, fixed, ambiguities…
    assert one[0][2]["proofread"][1]["retranslated"] > 0 and one[0][2]["proofread"][1]["ambiguities"] > 0
    assert one[1] == three[1]  # the same requests, step by step
    assert one[2]["proofread"] == 1 and one[2]["reader"] == 1
    assert three[2]["proofread"] > 1 and three[2]["reader"] > 1  # really side by side


@pytest.mark.parametrize("how", ["pause", "kill"])
def test_stopped_side_by_side_the_resumed_run_does_no_stretch_twice(conn, cfg, fixtures, how):
    cfg.context.proofread_chunk_units = 3
    cfg.openrouter.max_concurrency = 3
    client = Watching(("proofread", 2, how), responders=RESP)
    book, rid, summary = _book(conn, cfg, fixtures, client)
    assert summary.status == "interrupted"
    parts = pr.chunks(conn, book.id, size=3)
    total = len(parts)
    job = conn.execute("SELECT status, checkpoint_json FROM jobs WHERE run_id=? AND stage='proofread'", (rid,)).fetchone()
    cp = json.loads(job["checkpoint_json"])
    assert job["status"] == "running"
    done_first = client.asked["proofread"] - (1 if how == "kill" else 0)  # the killed request was not answered
    # the checkpoint never runs ahead of a stretch not done: every stretch up to done_until is done
    upto = [p for p in parts if p[-1]["_at"] <= cp.get("done_until_ordinal", -1)]
    past = [p for p in parts[len(upto):] if {x["unit_id"] for x in p} <= set(cp.get("done_beyond", []))]
    assert len(upto) + len(past) == done_first  # every stretch answered is kept, before the gap or past it
    assert all(p[-1]["_at"] > cp.get("done_until_ordinal", -1) for p in past)
    if how == "kill":
        assert past  # the killed stretch left a gap with stretches done after it
    again = Watching(responders=RESP)
    again.orch = None
    summary = resume_run(conn, cfg, rid, again)
    assert summary.status == "completed", summary
    assert done_first + again.asked.get("proofread", 0) == total  # nothing asked twice, nothing left out
    found = [r[0] for r in conn.execute("SELECT unit_id FROM qa_findings WHERE run_id=? AND type LIKE "
                                        "'review:proofread_meaning'", (rid,))]
    assert len(found) == len(set(found)) == total  # one per stretch, none found twice
    cp = json.loads(conn.execute("SELECT checkpoint_json FROM jobs WHERE run_id=? AND stage='proofread'",
                                 (rid,)).fetchone()[0])
    assert cp["checked"] == sum(len(p) for p in parts) and cp["retranslated"] == total
    assert "done_beyond" not in cp


def _first_texts(conn, rid):
    """Each paragraph's first translation in the run (the scene pass, before any fix), with its scene."""
    return conn.execute("SELECT t.unit_id, t.text, t.id, u.scene_id FROM translations t JOIN units u ON u.id=t.unit_id "
                        "WHERE t.run_id=? AND t.id=(SELECT MIN(id) FROM translations WHERE unit_id=t.unit_id AND "
                        "run_id=t.run_id)", (rid,)).fetchall()


def test_chapters_side_by_side_translate_as_one_at_a_time(cfg, fixtures, tmp_path):
    """4.2 (ADR-065): chapters are translated side by side, the scenes of a chapter in order."""
    results = {}
    for conc in (1, 3):
        cfg.openrouter.max_concurrency = conc
        conn = _db(tmp_path, f"t{conc}")
        client = Watching(responders=RESP)
        book, rid, summary = _book(conn, cfg, fixtures, client)
        assert summary.status == "completed", summary
        first = _first_texts(conn, rid)
        results[conc] = (_outcome(conn, rid), sorted((r["unit_id"], r["text"]) for r in first), dict(client.asked),
                         client.most.get("first", 0))
        # inside a chapter the scenes go in reading order: every paragraph of a scene is written before the next's
        scenes = conn.execute("SELECT s.id, s.start_ordinal, c.seq FROM scenes s JOIN chapters c ON c.book_id=s.book_id "
                              "AND s.start_ordinal BETWEEN c.start_ordinal AND c.end_ordinal WHERE s.book_id=? "
                              "ORDER BY s.start_ordinal", (book.id,)).fetchall()
        ids = {}
        for r in first:
            ids.setdefault(r["scene_id"], []).append(r["id"])
        for a, b in zip(scenes, scenes[1:], strict=False):
            if a["seq"] == b["seq"] and a["id"] in ids and b["id"] in ids:
                assert max(ids[a["id"]]) < min(ids[b["id"]])
        assert len({r["seq"] for r in scenes}) > 1
        conn.close()
    one, three = results[1], results[3]
    assert one[1] == three[1]  # the same translations
    assert one[0] == three[0]  # and the same findings, fixes and counts after them
    assert one[2] == three[2]  # the same requests, step by step
    assert one[3] == 1 and three[3] > 1  # chapters really side by side


def test_a_pause_while_chapters_go_side_by_side_resumes_without_translating_twice(conn, cfg, fixtures):
    cfg.openrouter.max_concurrency = 3
    client = Watching(("translate", 1, "pause"), responders=RESP)
    book, rid, summary = _book(conn, cfg, fixtures, client)
    assert summary.status == "interrupted"
    asked = client.asked["translate"]
    again = Watching(responders=RESP)
    assert resume_run(conn, cfg, rid, again).status == "completed"
    n = conn.execute("SELECT COUNT(*) FROM translations WHERE run_id=? AND flags_json NOT LIKE '%review_retranslation%'",
                     (rid,)).fetchone()[0]
    assert n == conn.execute("SELECT COUNT(DISTINCT unit_id) FROM translations WHERE run_id=?", (rid,)).fetchone()[0]
    assert asked + again.asked["translate"] >= 4  # every scene asked once at least; none written twice (above)


def test_a_fatal_error_in_one_chapter_stops_the_others_at_their_next_scene(conn, cfg, fixtures):
    from ncte.errors import FatalAPIError

    cfg.openrouter.max_concurrency = 3

    class Fatal(Watching):
        """The second chapter's first request fails for good; the first chapter's requests are slow, so it is still
        in its first scene then."""

        def complete(self, req: Any) -> Any:
            if req.stage.split(":")[0] == "translate":
                with self.orch.lock:
                    scenes = {r[0] for r in self.orch.conn.execute(
                        f"SELECT scene_id FROM units WHERE id IN ({','.join('?' * len(req.id_codes))})",
                        list(req.id_codes.values()))}
                if "s_001_01" in scenes:
                    raise FatalAPIError("no key")
                time.sleep(0.3)
            return super().complete(req)

    client = Fatal(responders=RESP)
    book, rid, summary = _book(conn, cfg, fixtures, client)
    assert summary.status == "stopped_fatal"
    groups = client.orch._chapter_groups([r[0] for r in conn.execute(
        "SELECT id FROM scenes WHERE book_id=? ORDER BY seq", (book.id,))])
    later = [sid for g in groups for sid in g[1:]]  # scenes that wait for another in their chapter
    assert later
    done = {r[0] for r in conn.execute("SELECT DISTINCT u.scene_id FROM translations t JOIN units u ON u.id=t.unit_id "
                                       "WHERE t.run_id=?", (rid,))}
    assert not done & set(later)  # no chapter went on to its next scene after the failure
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=? AND stage='proofread' AND status='pending'",
                        (rid,)).fetchone()[0] == 1  # nothing after translating was started
