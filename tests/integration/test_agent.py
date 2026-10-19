"""An agent as the model (`ncte agent`, the MCP server): the engine runs a whole book while a stand-in agent takes
each task, answers it with the offline fake model and submits it."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ncte.agent import runner
from ncte.agent.tasks import TaskQueue
from ncte.llm.base import LLMRequest
from ncte.llm.fake import FakeLLM


def answer_with_fake(queue: TaskQueue, done: threading.Event, seen: list[tuple[str, str | None]]) -> None:
    fake = FakeLLM(models=["agent/model"])
    spoiled = False
    while not done.is_set():
        t = queue.claim_next()
        if t is None:
            time.sleep(0.02)
            continue
        seen.append((t.stage.split(":")[0], t.effort))
        req = LLMRequest(stage=t.stage, model=t.model, schema_name="x", json_schema=t.schema, id_codes=t.id_codes,
                         messages=[{"role": "system", "content": t.instructions}, {"role": "user", "content": t.input}])
        text = fake.complete(req).content
        if not spoiled:  # an answer that does not fit the schema is refused and can be corrected
            spoiled = True
            assert queue.submit(t.id, '{"nothing": 1}')
        assert queue.submit(t.id, text) == [], t.stage


def test_a_book_is_translated_with_an_agent_as_the_model(tmp_path: Path, fixtures):
    ws = tmp_path / "ws"
    queue = TaskQueue(ws)
    done = threading.Event()
    seen: list[tuple[str, str | None]] = []
    agent = threading.Thread(target=answer_with_fake, args=(queue, done, seen), daemon=True)
    agent.start()
    try:
        summary = runner.serve(ws, fixtures["basic"])
    finally:
        done.set()
    assert summary["status"] == "completed", summary
    efforts = dict(seen)
    assert {"scene_analyze", "memory_update", "translate", "proofread"} <= set(efforts)
    assert efforts["translate"] == "medium" and efforts["proofread"] == "high"  # each task at its step's level
    st = runner.status(ws)
    assert st["open"] == 0 and st["run_status"] == "completed" and st["translated"] > 0
    out = runner.export(ws, tmp_path / "out.epub")
    assert Path(out["output"]).exists() and out["ok"]
    from ncte.workspace import Workspace

    w = Workspace(runner.engine_config(ws))
    conn = w.find_run(st["run_id"])
    rows = conn.execute("SELECT model, cost_usd FROM llm_requests WHERE run_id=?", (st["run_id"],)).fetchall()
    w.close()
    assert rows and all(r[0] == "agent/model" and r[1] == 0 for r in rows)  # nothing went to an API


def test_requests_sent_at_once_get_their_own_tasks(tmp_path):
    q = TaskQueue(tmp_path)

    def put(i: int) -> str:
        return q.put(stage="translate", model="m", messages=[{"role": "user", "content": f"req {i}"}],
                     schema=None, id_codes={})

    with ThreadPoolExecutor(16) as pool:
        ids = list(pool.map(put, range(200)))
    assert len(set(ids)) == 200
    assert sorted(q.load(t).input for t in ids) == sorted(f"req {i}" for i in range(200))


def test_an_abandoned_claim_is_taken_again_and_an_answer_is_not_overwritten(tmp_path):
    q = TaskQueue(tmp_path)
    tid = q.put(stage="translate", model="agent/model", messages=[{"role": "user", "content": "{}"}], schema=None,
                id_codes={})
    assert q.claim_next(now=1000.0).id == tid
    assert q.claim_next(now=1001.0) is None  # taken
    assert q.claim_next(now=1000.0 + 3600).id == tid  # given up on: free again
    ok = json.dumps({"translations": [{"unit_id": "P1", "translated_text": "好"}]})
    assert q.submit(tid, f"```json\n{ok}\n```") == []
    assert q.submit(tid, ok) == [f"{tid} is already answered"]
    assert q.wait(tid) == ok


def test_an_agent_run_is_not_continued_through_an_api(tmp_path):
    from ncte.cli import make_client
    from ncte.errors import NCTEError

    with pytest.raises(NCTEError, match="ncte agent resume"):
        make_client(runner.engine_config(tmp_path))


def test_the_mcp_tools_drive_the_same_queue(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from ncte.agent import mcp_server

    q = TaskQueue(tmp_path)
    tid = q.put(stage="naming", model="m", messages=[{"role": "user", "content": "{}"}], schema=None, id_codes={},
                effort="high")
    monkeypatch.setattr(runner, "status", lambda ws: {"status": "running", "engine_alive": True})
    task = mcp_server.ncte_next_task(str(tmp_path))
    assert task["task_id"] == tid and task["effort"] == "high"
    assert mcp_server.ncte_submit(str(tmp_path), tid, "not json")["accepted"] is False
    assert mcp_server.ncte_next_task(str(tmp_path))["task_id"] is None


def test_the_engine_runs_in_its_own_process_between_the_agents_calls(tmp_path, fixtures):
    ws = tmp_path / "ws"
    with pytest.raises(FileNotFoundError):
        runner.start_detached(ws, tmp_path / "missing.epub")
    runner.start_detached(ws, fixtures["basic"], limit=6)
    done = threading.Event()
    seen: list[tuple[str, str | None]] = []
    agent = threading.Thread(target=answer_with_fake, args=(TaskQueue(ws), done, seen), daemon=True)
    agent.start()
    try:
        for _ in range(600):
            st = runner.status(ws)
            if st.get("status") not in ("starting", "running"):
                break
            time.sleep(0.1)
    finally:
        done.set()
    assert st["status"] == "completed" and st["run_status"] == "completed", st.get("log_tail")
