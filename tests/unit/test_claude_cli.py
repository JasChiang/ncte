"""The Claude plan as the model (`claude -p`): what is sent, what is kept, waiting out a usage limit, and a whole book."""

from __future__ import annotations

import json
import subprocess
import threading
from typing import Any

import pytest

from ncte.config import CLAUDE_READ_STAGES, load_config
from ncte.errors import FatalAPIError, RetryableAPIError
from ncte.llm.base import LLMRequest
from ncte.llm.claude_cli import ClaudeCLIClient
from ncte.llm.fake import FakeLLM


def _req(**kw: Any) -> LLMRequest:
    base = {"stage": "translate", "model": "claude/opus", "schema_name": "x",
            "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": '{"units": []}'}],
            "json_schema": {"type": "object"}}
    return LLMRequest(**{**base, **kw})


def _ok(result: str = '{"translations": []}', **extra: Any) -> subprocess.CompletedProcess[str]:
    out = {"type": "result", "subtype": "success", "is_error": False, "result": result, "total_cost_usd": 0.042,
           "session_id": "s1", "usage": {"input_tokens": 3, "cache_creation_input_tokens": 1000,
                                         "cache_read_input_tokens": 500, "output_tokens": 70}, **extra}
    return subprocess.CompletedProcess([], 0, json.dumps(out), "")


class Calls:
    def __init__(self, *answers: subprocess.CompletedProcess[str]):
        self.answers = list(answers)
        self.args: list[list[str]] = []
        self.inputs: list[str] = []

    def __call__(self, args: list[str], *, input: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        self.args.append(args)
        self.inputs.append(input)
        return self.answers.pop(0)


def test_a_request_is_one_plain_call_at_the_steps_level_and_nothing_is_billed(monkeypatch):
    calls = Calls(_ok(), _ok())
    monkeypatch.setattr(subprocess, "run", calls)
    c = ClaudeCLIClient()
    r = c.complete(_req(extra={"reasoning": {"effort": "high"}}))
    a = calls.args[0]
    assert a[a.index("--model") + 1] == "opus" and a[a.index("--effort") + 1] == "high"
    assert a[a.index("--tools") + 1] == "" and "--no-session-persistence" in a  # no tools, nothing kept
    assert a[a.index("--system-prompt") + 1].startswith("SYS")  # the step's prompt replaces Claude Code's
    assert calls.inputs[0].startswith('{"units": []}') and "JSON schema" in calls.inputs[0]
    assert r.cost_usd == 0.0 and r.usage["list_cost_usd"] == 0.042
    assert r.usage["prompt_tokens"] == 1503 and r.usage["completion_tokens"] == 70
    c.complete(_req(extra={"reasoning": {"max_tokens": 3000}}))  # a token cap has no level: the model's default
    assert "--effort" not in calls.args[1]


def test_a_usage_limit_is_waited_out_and_a_missing_login_stops_at_once(monkeypatch):
    limit = subprocess.CompletedProcess([], 1, json.dumps({"subtype": "success", "is_error": True,
                                                           "result": "Usage limit reached · resets 9am"}), "")
    calls = Calls(limit, limit, _ok())
    monkeypatch.setattr(subprocess, "run", calls)
    waited: list[float] = []
    r = ClaudeCLIClient(waits=(30, 60, 120), sleep=waited.append).complete(_req())
    assert waited == [30, 60] and r.attempts == 3

    monkeypatch.setattr(subprocess, "run", Calls(limit, limit))
    with pytest.raises(RetryableAPIError):
        ClaudeCLIClient(waits=(1,), sleep=lambda s: None).complete(_req())

    login = subprocess.CompletedProcess([], 1, json.dumps({"is_error": True, "result": "Not logged in · Please run /login"}), "")
    calls = Calls(login)
    monkeypatch.setattr(subprocess, "run", calls)
    with pytest.raises(FatalAPIError):
        ClaudeCLIClient(waits=(1, 1), sleep=lambda s: None).complete(_req())
    assert len(calls.args) == 1


def test_the_claude_provider_reads_on_sonnet_and_translates_and_checks_on_opus(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NCTE_LLM_PROVIDER", "claude")
    cfg = load_config()
    o = cfg.openrouter
    assert o.provider == "claude" and o.model == "claude/opus" and o.temperature is None
    assert o.reasoning_effort == "medium"  # every step says its level, also outside the repo (no ncte.yaml)
    assert o.stage_models == dict.fromkeys(CLAUDE_READ_STAGES, "claude/sonnet")
    assert "naming" not in o.stage_models and "proofread" not in o.stage_models
    (tmp_path / "ncte.yaml").write_text("openrouter:\n  model: claude/sonnet\n  review_model: google/x\n"
                                        "  stage_models: {proofread: claude/opus, reader: google/y}\n", encoding="utf-8")
    o = load_config(tmp_path / "ncte.yaml").openrouter  # a chosen Claude model is kept; API model ids are dropped
    assert o.model == "claude/sonnet" and o.stage_models == {"proofread": "claude/opus"} and o.review_model is None


class FakeBacked(ClaudeCLIClient):
    """The real client with `claude -p` answered by the offline fake model (which needs the request's stage)."""

    def __init__(self) -> None:
        super().__init__(waits=())
        self.fake = FakeLLM(models=["claude/opus", "claude/sonnet"])
        self.local = threading.local()
        self.seen: list[tuple[str, str, str | None]] = []

    def complete(self, req: LLMRequest) -> Any:
        self.local.req = req
        return super().complete(req)

    def run(self, args: list[str], *, input: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        req = self.local.req
        self.seen.append((req.stage.split(":")[0], args[args.index("--model") + 1],
                          args[args.index("--effort") + 1] if "--effort" in args else None))
        return _ok(self.fake.complete(req).content)


def test_a_whole_book_runs_on_the_claude_provider(monkeypatch, tmp_path, fixtures):
    from ncte.cli import make_client
    from ncte.memory.db import connect, migrate

    from ..helpers import run_book

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NCTE_LLM_PROVIDER", "claude")
    cfg = load_config(workspace=str(tmp_path / "ws"))
    assert isinstance(make_client(cfg), ClaudeCLIClient)
    client = FakeBacked()
    monkeypatch.setattr(subprocess, "run", client.run)
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    book, rid, summary = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    assert summary.status == "completed", summary
    by_stage = {st: (m, e) for st, m, e in client.seen}
    assert by_stage["scene_analyze"][0] == "sonnet" and by_stage["translate"] == ("opus", "medium")
    assert by_stage["proofread"] == ("opus", "high")  # a judging step thinks at high
    rows = conn.execute("SELECT cost_usd, usage_json FROM llm_requests WHERE run_id=?", (rid,)).fetchall()
    assert rows and all(r[0] == 0 for r in rows)  # nothing billed; what the plan used is in the usage
    assert all(json.loads(r[1])["list_cost_usd"] == 0.042 for r in rows)
