"""The Antigravity plan as the model (`agy -p`): what is sent, what is kept, waiting, stopping, and quota."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ncte.config import Config, use_agy_models
from ncte.errors import FatalAPIError, RetryableAPIError
from ncte.llm import agy_cli
from ncte.llm.agy_cli import AgyCLIClient, plan_usage, split_level
from ncte.llm.base import LLMRequest

MODELS = "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\ngemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n" \
         "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\nclaude-opus-5-5-high\tClaude Opus 5.5 (High)\n" \
         "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n"
QUOTA = {"command": {"name": "usage", "data": {"groups": [
    {"name": "Gemini Models", "buckets": [
        {"window": "weekly", "remaining_fraction": 0.75, "reset_time": "2026-10-28T03:30:44Z"},
        {"window": "5h", "remaining_fraction": 0.5, "reset_time": "2026-10-21T08:30:44Z"}]},
    {"name": "Claude and GPT models", "buckets": [{"window": "weekly", "remaining_fraction": 0.97}]},
    {"name": "Gemini 4 Argon", "buckets": [{"window": "weekly", "remaining_fraction": 1.0}]}]}}}
USAGE = {"input_tokens": 7700, "output_tokens": 300, "thinking_tokens": 250, "cache_read_tokens": 100,
         "total_tokens": 8000}


def _req(**kw: Any) -> LLMRequest:
    level = kw.pop("level", "high")
    base = {"stage": "translate", "model": "agy/gemini-3.8-flash", "schema_name": "x",
            "extra": {"reasoning": {"effort": level}},
            "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": '{"units": []}'}],
            "json_schema": {"type": "object"}}
    return LLMRequest(**{**base, **kw})


class Fake:
    """Answers `agy models`, `agy -p /quota` and model turns (one outcome per turn, in order)."""

    def __init__(self, turns: list[tuple[int, dict[str, Any], str]]):
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        self.calls.append({"args": args, **kw})
        if args[1:] == ["models"]:
            return subprocess.CompletedProcess(args, 0, MODELS, "")
        if "/quota" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps(QUOTA), "")
        if kw.get("cwd"):  # what the call left for the model to read
            req = Path(kw["cwd"]) / "request.md"
            self.calls[-1]["file"] = req.read_text(encoding="utf-8") if req.exists() else None
            agent = Path(kw["cwd"]) / ".agents" / "agents" / "ncte-plain" / "agent.md"
            self.calls[-1]["agent"] = agent.read_text(encoding="utf-8") if agent.exists() else None
            self.calls[-1]["left"] = sorted(p.name for p in Path(kw["cwd"]).iterdir())
        code, out, err = self.turns.pop(0)
        return subprocess.CompletedProcess(args, code, json.dumps(out), err)

    def turns_sent(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if any(a.startswith("--print=") for a in c["args"])]


def ok(text: str = '{"translations": []}', cid: str = "c-1") -> tuple[int, dict[str, Any], str]:
    return 0, {"conversation_id": cid, "status": "SUCCESS", "response": text + "\n", "usage": USAGE}, ""


def _client(monkeypatch, tmp_path, fake: Fake, **kw: Any) -> AgyCLIClient:
    monkeypatch.setattr(agy_cli.subprocess, "run", fake)
    monkeypatch.setattr(agy_cli, "_run_turn", lambda args, timeout, cwd: fake(args, cwd=cwd))
    return AgyCLIClient(home=tmp_path / "home", sleep=lambda s: None, waits=(1, 2), limit_wait=10, limit_max=25, **kw)


def test_a_request_is_one_print_call_with_rules_schema_and_data_in_one_prompt(monkeypatch, tmp_path):
    fake = Fake([ok()])
    resp = _client(monkeypatch, tmp_path, fake).complete(_req())
    (call,) = fake.turns_sent()
    args = call["args"]
    assert args[args.index("--model") + 1] == "gemini-3.8-flash-high"  # the step's level, as agy lists it
    assert "--sandbox" in args and "--disable-slash-commands" in args and "--json-schema" not in args
    prompt = next(a for a in args if a.startswith("--print="))[len("--print="):]
    assert prompt.index("SYS") < prompt.index("JSON Schema") < prompt.index('{"units": []}')
    assert call["left"] == [".agents"]  # an empty directory but for the agent NCTE runs as
    assert args[args.index("--agent") + 1] == "ncte-plain"
    assert resp.content == '{"translations": []}' and resp.provider == "agy" and resp.cost_usd == 0
    u = resp.usage
    assert u["prompt_tokens"] == 7700 and u["completion_tokens"] == 300 and u["reasoning_tokens"] == 250
    assert u["cache_read_tokens"] == 100 and u["list_cost_usd"] == 0 and u["agy_model"] == "gemini-3.8-flash-high"
    assert u["effort"] == "high" and "effort_asked" not in u and u["agy_agent"] == "ncte-plain"
    assert u["plan"] == {"source": "agy", "windows": {"weekly": {"used": 0.25, "resets_at": 1793158244},
                                                      "5h": {"used": 0.5, "resets_at": 1792571444}}}


def test_a_level_agy_does_not_list_falls_back_to_medium_and_a_full_id_is_kept(monkeypatch, tmp_path):
    fake = Fake([ok(), ok()])
    c = _client(monkeypatch, tmp_path, fake)
    c.complete(_req(level="max:2000"))
    c.complete(_req(model="agy/claude-opus-5-5-high"))
    first, second = (t["args"] for t in fake.turns_sent())
    assert first[first.index("--model") + 1] == "gemini-3.8-flash-medium"
    assert second[second.index("--model") + 1] == "claude-opus-5-5-high"


def test_the_usage_says_the_level_the_model_id_really_carries(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, Fake([ok(), ok()]))
    u1 = c.complete(_req(level="max:2000")).usage  # a token cap has no level: the medium id is what ran
    u2 = c.complete(_req(model="agy/claude-opus-5-5-high", level="medium")).usage  # the id carries its own level
    assert u1["effort"] == "medium" and "effort_asked" not in u1
    assert (u2["effort"], u2["effort_asked"], u2["agy_model"]) == ("high", "medium", "claude-opus-5-5-high")


def test_each_call_runs_as_an_agent_with_no_tools_but_finish(monkeypatch, tmp_path):
    fake = Fake([ok()])
    _client(monkeypatch, tmp_path, fake).complete(_req())
    (call,) = fake.turns_sent()
    text = call["agent"]
    assert text.startswith("---\nname: ncte-plain\n") and "tools:\n  - finish\nmainAgent: true\n" in text
    assert "view_file" not in text and "SYS" not in text  # NCTE's rules stay in the prompt


def test_a_request_in_a_file_lets_the_agent_read_it(monkeypatch, tmp_path):
    monkeypatch.setattr(agy_cli, "ARG_LIMIT", 100)
    fake = Fake([ok()])
    _client(monkeypatch, tmp_path, fake).complete(_req(messages=[
        {"role": "system", "content": "SYS"}, {"role": "user", "content": "長" * 200}]))
    (call,) = fake.turns_sent()
    assert "  - view_file\n  - finish\n" in call["agent"]


def test_an_agy_that_does_not_know_the_agent_goes_on_without_it_and_says_so(monkeypatch, tmp_path):
    unknown = (1, {"conversation_id": "", "status": "ERROR", "response": "",
                   "error": 'unknown agent "ncte-plain" (no custom agents are available)', "usage": {}}, "")
    fake = Fake([unknown, ok(), ok()])
    said: list[str] = []
    c = _client(monkeypatch, tmp_path, fake, log=said.append)
    resp = c.complete(_req())
    c.complete(_req())
    first, second, third = (t["args"] for t in fake.turns_sent())
    assert "--agent" in first and "--agent" not in second and "--agent" not in third
    assert resp.attempts == 1 and "agy_agent" not in resp.usage and len(said) == 1 and "agent" in said[0]


def test_the_plain_agent_can_be_turned_off(monkeypatch, tmp_path):
    fake = Fake([ok()])
    _client(monkeypatch, tmp_path, fake, plain_agent=False).complete(_req())
    (call,) = fake.turns_sent()
    assert "--agent" not in call["args"] and call["agent"] is None


def test_the_cli_never_builds_the_agy_source(monkeypatch):
    """ADR-105: the adapter is kept (built directly above); a run asking for it stops before any request."""
    from ncte.cli import make_client
    from ncte.config import AGY_DISABLED
    from ncte.errors import NCTEError

    monkeypatch.setattr(agy_cli.subprocess, "run", lambda *a, **k: pytest.fail("agy was run"))
    cfg = Config()
    cfg.openrouter.provider = "agy"
    use_agy_models(cfg)
    with pytest.raises(NCTEError, match="Antigravity FAQ") as err:
        make_client(cfg)
    assert str(err.value) == AGY_DISABLED and "Gemini API" in AGY_DISABLED


def test_a_long_prompt_goes_to_a_file_the_model_is_asked_to_read(monkeypatch, tmp_path):
    monkeypatch.setattr(agy_cli, "ARG_LIMIT", 100)
    fake = Fake([ok()])
    _client(monkeypatch, tmp_path, fake).complete(_req(messages=[
        {"role": "system", "content": "SYS"}, {"role": "user", "content": json.dumps({"units": ["長" * 200]}, ensure_ascii=False)}]))
    (call,) = fake.turns_sent()
    assert call["file"] and "長" * 200 in call["file"] and call["file"].startswith("SYS")
    prompt = next(a for a in call["args"] if a.startswith("--print="))
    assert "request.md" in prompt and "長" not in prompt


def test_the_conversation_agy_kept_is_removed_and_nothing_else(monkeypatch, tmp_path):
    home = tmp_path / "home"
    for d in ("conversations", "annotations", "presence", "brain/c-1/logs", "brain/other"):
        (home / d).mkdir(parents=True, exist_ok=True)
    for f in ("conversations/c-1.db", "conversations/c-1.db-wal", "annotations/c-1.pbtxt", "presence/c-1.lock",
              "brain/c-1/logs/transcript.jsonl", "conversations/other.db"):
        (home / f).write_text("x")
    _client(monkeypatch, tmp_path, Fake([ok(cid="c-1")])).complete(_req())
    left = sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file())
    assert left == ["conversations/other.db"] and (home / "brain" / "other").is_dir()


def test_a_usage_limit_waits_and_asks_again(monkeypatch, tmp_path):
    limit = (3, {"status": "ERROR", "error": "RESOURCE_EXHAUSTED: quota exceeded", "usage": {"input_tokens": 7000}}, "")
    fake = Fake([limit, ok()])
    slept: list[float] = []
    monkeypatch.setattr(agy_cli.subprocess, "run", fake)
    monkeypatch.setattr(agy_cli, "_run_turn", lambda args, timeout, cwd: fake(args, cwd=cwd))
    c = AgyCLIClient(home=tmp_path, sleep=slept.append, waits=(1, 2), limit_wait=10, limit_max=25)
    resp = c.complete(_req())
    assert slept == [10] and resp.attempts == 2
    assert resp.usage["failed_tries"] == {"input_tokens": 7000} and resp.usage["prompt_tokens"] == 14700


def test_other_failures_go_down_the_ladder_then_stop(monkeypatch, tmp_path):
    bad = (3, {"status": "ERROR", "error": "AGY_ERROR: model overloaded"}, "")
    with pytest.raises(RetryableAPIError, match="kept failing"):
        _client(monkeypatch, tmp_path, Fake([bad, bad, bad])).complete(_req())


def test_no_sign_in_or_a_rejected_command_stops_at_once(monkeypatch, tmp_path):
    with pytest.raises(FatalAPIError, match="signed in"):
        _client(monkeypatch, tmp_path, Fake([(1, {}, "Error: not signed in")])).complete(_req())
    with pytest.raises(FatalAPIError, match="rejected"):
        _client(monkeypatch, tmp_path, Fake([(2, {}, "flag provided but not defined: -x")])).complete(_req())


def test_quota_is_read_at_most_once_a_minute(monkeypatch, tmp_path):
    fake = Fake([ok(), ok(), ok()])
    c = _client(monkeypatch, tmp_path, fake)
    for _ in range(3):
        c.complete(_req())
    assert sum("/quota" in x["args"] for x in fake.calls) == 1


def test_quota_groups_follow_the_model():
    data = QUOTA["command"]["data"]
    assert plan_usage(data, "claude-opus-5-5-high")["windows"] == {"weekly": {"used": 0.03, "resets_at": None}}
    assert plan_usage(data, "gemini-4-argon-high")["windows"]["weekly"]["used"] == 0.0
    assert set(plan_usage(data, "gemini-3.8-flash-low")["windows"]) == {"weekly", "5h"}
    assert plan_usage({}, "gemini-3.8-flash") is None


def test_models_are_listed_once_per_model_with_their_levels(monkeypatch, tmp_path):
    rows = {r["id"]: r for r in _client(monkeypatch, tmp_path, Fake([])).list_models()}
    assert set(rows) == {"agy/gemini-3.8-flash", "agy/gemini-flash", "agy/claude-opus-5-5", "agy/gpt-oss-120b"}
    assert rows["agy/gemini-3.8-flash"]["reasoning"]["supported_efforts"] == ["high", "medium", "low"]
    assert split_level("gemini-3.8-flash") == ("gemini-3.8-flash", None)


def test_the_agy_provider_keeps_agy_models_only():
    cfg = Config()
    cfg.openrouter.provider = "agy"
    cfg.openrouter.stage_models = {"proofread": "agy/gemini-4-argon", "translate": "google/gemini-3.8-flash"}
    use_agy_models(cfg)
    assert cfg.openrouter.model == "agy/gemini-flash"
    assert cfg.openrouter.stage_models == {"proofread": "agy/gemini-4-argon"} and cfg.openrouter.temperature is None


def test_a_family_name_is_the_newest_listed_model_and_a_retired_id_is_not_offered(monkeypatch, tmp_path):
    from ncte.llm.agy_cli import family_of, newest_of

    assert family_of("gemini-3.8-flash") == ("gemini-flash", (3, 8)) and family_of("claude-opus-5-5") is None
    assert newest_of("gemini-flash", ["gemini-3.8-flash", "gemini-3.10-flash", "gemini-4-argon"]) == "gemini-3.10-flash"
    fake = Fake([ok()])
    c = _client(monkeypatch, tmp_path, fake, models=["agy/gemini-3.5-flash", "agy/gemini-flash"])
    ids = {r["id"] for r in c.list_models()}
    assert "agy/gemini-3.5-flash" not in ids and "agy/gemini-flash" in ids  # retired: the run stops before starting
    resp = c.complete(_req(model="agy/gemini-flash"))
    (call,) = fake.turns_sent()
    assert call["args"][call["args"].index("--model") + 1] == "gemini-3.8-flash-high"
    assert resp.usage["agy_model"] == "gemini-3.8-flash-high" and resp.model == "agy/gemini-flash"


def test_a_turn_is_read_from_its_result_event_and_a_process_that_never_exits_is_stopped(tmp_path):
    """agy sometimes prints the result event and then never exits: the answer is taken and the process stopped."""
    import sys
    import time

    script = tmp_path / "agy_hangs.py"
    script.write_text("import json, sys, time\n"
                      "print(json.dumps({'event': 'init', 'conversation_id': 'c-9'}), flush=True)\n"
                      "print(json.dumps({'event': 'step_update', 'step_update': {'text_delta': '{'}}), flush=True)\n"
                      "print(json.dumps({'event': 'result', 'result': {'conversation_id': 'c-9', 'status': 'SUCCESS', "
                      "'response': '{\\\"ok\\\": true}', 'usage': {'input_tokens': 5}}}), flush=True)\n"
                      "time.sleep(600)\n", encoding="utf-8")
    t = time.monotonic()
    proc = agy_cli._run_turn([sys.executable, str(script)], timeout=60, cwd=str(tmp_path))
    assert time.monotonic() - t < 30 and proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["conversation_id"] == "c-9" and out["response"] == '{"ok": true}'
    silent = tmp_path / "agy_silent.py"
    silent.write_text("import time\ntime.sleep(600)\n", encoding="utf-8")
    with pytest.raises(subprocess.TimeoutExpired):
        agy_cli._run_turn([sys.executable, str(silent)], timeout=2, cwd=str(tmp_path))



def test_the_cost_report_shows_the_level_each_step_really_ran_at(tmp_path):
    from ncte.memory.db import connect, migrate
    from ncte.reports.cost_report import cost_report

    conn = connect(tmp_path / "t.db")
    migrate(conn)
    rows = [("translate", {"effort": "medium", "agy_model": "gemini-3.8-flash-medium"}, None),
            ("fix_check", {"effort": "high", "agy_model": "gemini-3.8-flash-high"}, None),
            ("fix_check", {"effort": "high", "agy_model": "gemini-3.8-flash-high"}, None),
            ("review", {}, "low")]
    for i, (stage, usage, eff) in enumerate(rows):
        conn.execute("INSERT INTO llm_requests(run_id, stage, request_hash, model, usage_json, status, created_at, "
                     "reasoning_effort) VALUES ('r', ?, ?, 'agy/gemini-flash', ?, 'succeeded', '2026-10-21T00:00:00Z', ?)",
                     (stage, f"h{i}", json.dumps(usage), eff))
    st = cost_report(conn, run_id="r")["by_stage"]
    assert st["fix_check"]["efforts"] == {"high": 2} and st["fix_check"]["models_used"] == ["gemini-3.8-flash-high"]
    assert st["translate"]["efforts"] == {"medium": 1} and st["review"]["efforts"] == {"low": 1}
    assert "models_used" not in st["review"]


def test_the_plain_agent_is_used_only_by_the_listed_steps(monkeypatch, tmp_path):
    fake = Fake([ok(), ok()])
    client = _client(monkeypatch, tmp_path, fake)
    client.complete(_req(stage="scene_analyze"))
    client.complete(_req(stage="translate"))
    reading, translating = fake.turns_sent()
    assert "--agent" not in reading["args"] and reading["agent"] is None
    assert "--agent" in translating["args"]


def test_agy_sends_five_at_a_time_unless_the_number_is_set():
    cfg = Config()
    use_agy_models(cfg)
    assert cfg.openrouter.max_concurrency == 5
    cfg = Config.model_validate({"openrouter": {"max_concurrency": 2}})
    use_agy_models(cfg)
    assert cfg.openrouter.max_concurrency == 2


def test_an_empty_answer_carries_what_the_turn_took(monkeypatch, tmp_path):
    """ADR-102: a turn that came back empty still ran and was counted against the plan (27 in a whole-book run, none of
    them in the reports): its tokens, the plan's windows and its time go with the error, for the request's row."""
    from ncte.errors import EmptyAnswerError

    fake = Fake([(0, {"conversation_id": "c-1", "status": "SUCCESS", "response": "\n", "usage": USAGE}, "")])
    with pytest.raises(EmptyAnswerError) as err:
        _client(monkeypatch, tmp_path, fake).complete(_req())
    u = err.value.usage
    assert u is not None and u["prompt_tokens"] == 7700 and u["completion_tokens"] == 300
    assert u["reasoning_tokens"] == 250 and u["plan"]["source"] == "agy"
    assert err.value.latency_ms is not None
