"""Post-read steps take an earlier run's answer to an identical request instead of asking again (ADR-094): a second
run over notes that did not change sends no post-read request; a request whose data changed is asked; with
``reuse_answers`` off (``--fresh``) everything is asked again; the prompt's text is part of the request, so a changed
rule is asked again."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any

from ncte.config import Config
from ncte.llm.cost import Pricing
from ncte.llm.fake import FakeLLM
from ncte.llm.prompt_loader import load_prompt
from ncte.translation.orchestrator import DbRecorder
from ncte.translation.translator import call_structured

from ..helpers import orchestrator_for, start_run
from ..scripted import RESPONDERS
from .test_parallel_passes import Watching, _db

READ_AFTER = ("verify", "relations", "impressions")  # the post-read steps the sample book asks with ``_ask_groups``


def _run(conn, cfg, fixtures, logs: list[str] | None = None, **kw: Any) -> tuple[Watching, Any]:
    client = Watching(responders=RESPONDERS)
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], client, mode="context", **kw)
    orch = orchestrator_for(conn, cfg, book, rid, opts, client, logs=logs)
    client.orch = orch
    return client, orch.run()


def _read_since(conn) -> None:
    """As if a scene was read after the post-read steps last succeeded (a scene re-read): they run again."""
    conn.execute("UPDATE jobs SET updated_at='2999-01-01T00:00:00Z' WHERE id=(SELECT MAX(id) FROM jobs WHERE "
                 "stage='scene_analyze' AND status='succeeded')")


def test_a_second_run_over_unchanged_notes_asks_no_post_read_request(cfg, fixtures, tmp_path):
    conn = _db(tmp_path, "a")
    first, s1 = _run(conn, cfg, fixtures)
    _read_since(conn)
    logs: list[str] = []
    second, s2 = _run(conn, cfg, fixtures, logs=logs)
    assert s1.status == s2.status == "completed"
    assert all(first.asked.get(st, 0) > 0 for st in READ_AFTER)
    assert all(second.asked.get(st, 0) == 0 for st in READ_AFTER), second.asked
    assert any("taken from an earlier run" in m for m in logs)


def test_fresh_or_force_asks_the_post_read_steps_again(cfg, fixtures, tmp_path):
    conn = _db(tmp_path, "b")
    first, _ = _run(conn, cfg, fixtures)
    want = {st: first.asked[st] for st in READ_AFTER}
    forced, _ = _run(conn, cfg, fixtures, force=True)  # `--force` redoes everything, asking included
    assert {st: forced.asked.get(st, 0) for st in READ_AFTER} == want
    cfg.context.reuse_answers = False  # `--fresh`
    _read_since(conn)
    fresh, _ = _run(conn, cfg, fixtures)
    assert {st: fresh.asked.get(st, 0) for st in READ_AFTER} == want


class Counting(FakeLLM):
    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    def complete(self, req: Any) -> Any:
        self.n += 1
        return super().complete(req)


def _ask(conn, book_id: str, run_id: str, client: Counting, payload: dict[str, Any], prompt: Any,
         *, reuse_runs: bool = True) -> None:
    pricing = Pricing(model="fake/model", prompt=0.0, completion=0.0, reasoning=0.0, cache_read=None,
                      context_length=None, supported_parameters=())
    rec = DbRecorder(conn, threading.RLock(), run_id, book_id, pricing, reuse=reuse_runs)
    call_structured(client, rec, stage="house_style", model="fake/model", prompt=prompt, payload=payload,
                    mode="json_object", cfg=Config(), unit_ids=[], est_cost=0.0, reuse=True)


def test_only_a_request_that_changed_is_asked_again(cfg, fixtures, tmp_path):
    conn = _db(tmp_path, "c")
    _run(conn, cfg, fixtures)
    book_id = conn.execute("SELECT id FROM books").fetchone()[0]
    rid = conn.execute("SELECT id FROM runs").fetchone()[0]  # requests are kept under a run that exists
    prompt = load_prompt("house_style")
    client = Counting()
    a, b = {"task": "house_style", "lines": ["一"]}, {"task": "house_style", "lines": ["二"]}
    _ask(conn, book_id, rid, client, a, prompt)
    assert client.n == 1
    _ask(conn, book_id, rid, client, a, prompt)  # identical: taken from the record
    assert client.n == 1
    _ask(conn, book_id, rid, client, b, prompt)  # its data changed
    assert client.n == 2
    _ask(conn, book_id, rid, client, a, replace(prompt, text=prompt.text + "\n- 新規則。\n"))  # a rule changed
    assert client.n == 3


def test_an_earlier_runs_answer_is_used_only_when_reuse_is_on(cfg, fixtures, tmp_path):
    conn = _db(tmp_path, "d")
    first, _ = _run(conn, cfg, fixtures)
    second, _ = _run(conn, cfg, fixtures, force=True)
    book_id = conn.execute("SELECT id FROM books").fetchone()[0]
    runs = [r[0] for r in conn.execute("SELECT id FROM runs ORDER BY rowid")]
    prompt = load_prompt("house_style")
    client = Counting()
    payload = {"task": "house_style", "lines": ["三"]}
    _ask(conn, book_id, runs[0], client, payload, prompt)
    _ask(conn, book_id, runs[1], client, payload, prompt, reuse_runs=False)  # another run, reuse off: asked
    assert client.n == 2
    _ask(conn, book_id, runs[1], client, payload, prompt)  # its own run's answer now exists: taken
    assert client.n == 2
    other = {"task": "house_style", "lines": ["四"]}
    _ask(conn, book_id, runs[0], client, other, prompt)
    _ask(conn, book_id, runs[1], client, other, prompt)  # an earlier run's answer, reuse on: taken
    assert client.n == 3
