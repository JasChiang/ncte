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
    assert calls.inputs[0] == '{"units": []}'  # the request's data only; the schema is in the system prompt
    assert r.cost_usd == 0.0 and r.usage["list_cost_usd"] == 0.042
    assert r.usage["prompt_tokens"] == 1503 and r.usage["completion_tokens"] == 70
    c.complete(_req(extra={"reasoning": {"max_tokens": 3000}}))  # a token cap has no level: the model's default
    assert "--effort" not in calls.args[1]


def test_the_system_prompt_is_the_same_on_every_request_of_a_step_so_it_is_cached(monkeypatch):
    """Claude Code caches the system prompt as one block: only a byte-identical one is read from the cache. The fixed
    rules and the step's JSON schema go there; this batch's conditional rules and a retry's correction go to the
    user message, ahead of the data."""
    calls = Calls(_ok(), _ok(), _ok())
    monkeypatch.setattr(subprocess, "run", calls)
    c = ClaudeCLIClient()
    schema = {"type": "object", "properties": {"translations": {"type": "array"}}}
    fixed = "FIXED RULES\n"

    def req(own: str) -> LLMRequest:
        return _req(messages=[{"role": "system", "content": fixed + own},
                              {"role": "user", "content": '{"units": [1]}'}],
                    json_schema=schema, cached_prefix_chars=len(fixed))

    c.complete(req("\n## 本批資料的附加說明\nCOND A\n"))
    c.complete(req("\n## 本批資料的附加說明\nCOND B\n"))
    c.complete(req("\n## 本批資料的附加說明\nCOND A\n\n## 重要更正\n上一次的輸出未通過程式驗證：missing P3\n"))
    systems = [a[a.index("--system-prompt") + 1] for a in calls.args]
    assert systems[0] == systems[1] == systems[2]
    assert systems[0].startswith("FIXED RULES") and json.dumps(schema, ensure_ascii=False) in systems[0]
    assert "COND" not in systems[0] and "重要更正" not in systems[0]
    assert all('"properties"' not in u for u in calls.inputs)  # the schema is never in the user message
    assert "COND A" in calls.inputs[0] and "COND B" in calls.inputs[1]
    assert "重要更正" in calls.inputs[2] and "missing P3" in calls.inputs[2]
    assert all(u.endswith('{"units": [1]}') for u in calls.inputs)  # the data comes last


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
    monkeypatch.delenv("NCTE_LLM_PROVIDER")  # nothing chosen: the Claude plan is the default (ADR-062)
    cfg = load_config()
    o = cfg.openrouter
    assert o.provider == "claude" and o.model == "claude/opus" and o.temperature is None
    assert o.reasoning_effort == "medium"  # every step says its level, also outside the repo (no ncte.yaml)
    assert o.stage_models == dict.fromkeys(CLAUDE_READ_STAGES, "claude/sonnet")
    assert "naming" not in o.stage_models and "proofread" not in o.stage_models
    assert "verify" not in o.stage_models and "relations" not in o.stage_models  # speakers and relations on Opus
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
        self.systems: dict[tuple[str, str], set[str]] = {}

    def complete(self, req: LLMRequest) -> Any:
        self.local.req = req
        return super().complete(req)

    def run(self, args: list[str], *, input: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        req = self.local.req
        self.seen.append((req.stage.split(":")[0], args[args.index("--model") + 1],
                          args[args.index("--effort") + 1] if "--effort" in args else None))
        self.systems.setdefault((req.stage.split(":")[0], req.model), set()).add(args[args.index("--system-prompt") + 1])
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
    # every request of a step sends the same system prompt (cacheable), whatever its batch's conditional rules
    assert all(len(s) == 1 for s in client.systems.values()), {k: len(s) for k, s in client.systems.items()}
    rows = conn.execute("SELECT cost_usd, usage_json FROM llm_requests WHERE run_id=?", (rid,)).fetchall()
    assert rows and all(r[0] == 0 for r in rows)  # nothing billed; what the plan used is in the usage
    assert all(json.loads(r[1])["list_cost_usd"] == 0.042 for r in rows)


def test_a_wrong_command_stops_at_once_and_says_why(monkeypatch):
    """Exit 2 or a rejected option is the same on every try: no hours of waiting."""
    for bad in (subprocess.CompletedProcess([], 2, "", "error: unknown option '--frobnicate'\n(Did you mean …?)"),
                subprocess.CompletedProcess([], 1, "", "error: too many arguments. Expected 0 arguments but got 1.")):
        calls = Calls(bad, _ok())
        monkeypatch.setattr(subprocess, "run", calls)
        waited: list[float] = []
        with pytest.raises(FatalAPIError) as err:
            ClaudeCLIClient(waits=(30, 60), sleep=waited.append).complete(_req())
        assert len(calls.args) == 1 and not waited
        assert (bad.stderr or "")[:40] in str(err.value)


def test_an_answer_that_mentions_logging_in_is_an_answer(monkeypatch):
    """The not-logged-in words are looked for in a failure only, never in the model's own answer."""
    text = '{"translations": [{"unit_id": "P1", "text": "Please run /login 後再試一次（authentication 失敗）"}]}'
    calls = Calls(_ok(text))
    monkeypatch.setattr(subprocess, "run", calls)
    r = ClaudeCLIClient(waits=(1,), sleep=lambda s: None).complete(_req())
    assert r.content == text and r.attempts == 1


def test_each_wait_is_said_before_it_starts(monkeypatch):
    limit = subprocess.CompletedProcess([], 1, json.dumps({"subtype": "success", "is_error": True,
                                                           "result": "Usage limit reached · resets 9am"}), "")
    monkeypatch.setattr(subprocess, "run", Calls(limit, _ok()))
    said: list[str] = []
    ClaudeCLIClient(waits=(30,), sleep=lambda s: None, log=said.append).complete(_req())
    assert len(said) == 1 and "30" in said[0] and "Usage limit reached" in said[0]
    monkeypatch.setattr(subprocess, "run", Calls(limit, _ok()))
    ClaudeCLIClient(waits=(30,), sleep=lambda s: None).complete(_req())  # no log given: nothing printed, no error


def test_the_command_line_and_the_page_hear_the_waits(monkeypatch, tmp_path):
    from ncte.cli import make_client
    from ncte.gui.runner import Runner

    monkeypatch.setenv("NCTE_LLM_PROVIDER", "claude")
    cfg = load_config(workspace=str(tmp_path / "ws"))
    said: list[str] = []
    assert make_client(cfg, log=said.append).log == said.append
    runner = Runner(cfg, client_factory=lambda c: ClaudeCLIClient())
    client = runner.make_client(cfg)
    client.log("等待 30 秒：額度")
    assert list(runner.log)[-1] == "等待 30 秒：額度"


READ_LABELS = {"scene_windows": "切場景", "scene_analyze": "讀場景", "memory_update": "記憶整理",
               "impressions": "角色印象", "house_style": "體例"}
OPUS_LABELS = ("覆核", "人物關係")  # few requests that decide speakers, gender and relations (ADR-062 補充)


def test_the_page_and_the_readme_say_which_steps_run_on_sonnet():
    """What the settings page and the README say about the Claude plan's models follows the configuration."""
    from pathlib import Path

    import ncte

    root = Path(ncte.__file__).resolve().parents[2]
    assert set(READ_LABELS) == set(CLAUDE_READ_STAGES)
    for path in (root / "src/ncte/gui/static/app.js", root / "README.md"):
        text = path.read_text(encoding="utf-8")
        said = text[text.index("讀書的步驟"):]
        said = said[:said.index("用 Sonnet")]
        assert all(label in said for label in READ_LABELS.values()), (path.name, said)
        assert not any(label in said for label in OPUS_LABELS), (path.name, said)
    js = (root / "src/ncte/gui/static/app.js").read_text(encoding="utf-8")
    first = js[js.index("async function libraryView"):js.index("async function", js.index("async function libraryView") + 1)]
    assert "找到 Claude Code" in first  # the first step on the Claude plan is not an OpenRouter key


def test_agent_is_not_a_source_chosen_by_environment(monkeypatch):
    from ncte.errors import ConfigError

    monkeypatch.setenv("NCTE_LLM_PROVIDER", "agent")
    with pytest.raises(ConfigError, match="ncte agent"):
        load_config()


def _model_usage(model_id: str) -> dict[str, Any]:
    # the shape of the result's ``modelUsage`` in the installed Claude Code (2.1.303): model id → per-model counts
    return {model_id: {"inputTokens": 3, "outputTokens": 70, "cacheReadInputTokens": 500,
                       "cacheCreationInputTokens": 1000, "webSearchRequests": 0, "costUSD": 0.042}}


def test_the_model_claude_code_really_used_is_kept(monkeypatch):
    """2.8: an alias (opus, sonnet) names whatever model Claude Code maps it to; the result's ``modelUsage`` says
    which, and it is kept with the usage. No ``modelUsage``: nothing is made up."""
    monkeypatch.setattr(subprocess, "run", Calls(_ok(modelUsage=_model_usage("claude-opus-test-1")), _ok()))
    c = ClaudeCLIClient(waits=())
    assert c.complete(_req()).usage["models"] == ["claude-opus-test-1"]
    assert "models" not in c.complete(_req()).usage


class ModelReporting(FakeBacked):
    def run(self, args: list[str], *, input: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        alias = args[args.index("--model") + 1]
        return _ok(self.fake.complete(self.local.req).content, modelUsage=_model_usage(f"claude-{alias}-test-1"))


def test_reports_and_the_page_show_the_models_really_used(monkeypatch, tmp_path, fixtures):
    from ncte.memory.db import connect, migrate
    from ncte.reports.cost_report import cost_report
    from ncte.reports.html_report import render_html
    from ncte.reports.qa_report import book_status, qa_report

    from ..helpers import run_book

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NCTE_LLM_PROVIDER", "claude")
    cfg = load_config(workspace=str(tmp_path / "ws"))
    client = ModelReporting()
    monkeypatch.setattr(subprocess, "run", client.run)
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    book, rid, summary = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    cost = cost_report(conn, run_id=rid)
    assert cost["by_model"]["claude/opus"]["models"] == ["claude-opus-test-1"]
    assert cost["by_model"]["claude/sonnet"]["models"] == ["claude-sonnet-test-1"]
    assert "claude-opus-test-1" in render_html("t", cost, qa_report(conn, book_id=book.id))
    run = next(r for r in book_status(conn, book.id)["runs"] if r["id"] == rid)
    assert run["models_used"] == ["claude-opus-test-1", "claude-sonnet-test-1"]
    from pathlib import Path

    import ncte
    js = (Path(ncte.__file__).resolve().parents[2] / "src/ncte/gui/static/app.js").read_text(encoding="utf-8")
    assert "models_used" in js


def test_the_claude_plan_gets_a_list_price_estimate_from_earlier_runs(monkeypatch, tmp_path, fixtures):
    """2.9: nothing is billed on the Claude plan (``cost_usd`` 0), so the rates come from what each request would
    have cost at list price (``usage.list_cost_usd``), each step on its own model (reading on Sonnet); the page says
    「照定價約 US$X–Y（不另收費）」. No history: no figure (as before)."""
    import functools

    from ncte.gui.runner import estimate_view
    from ncte.memory.db import connect, migrate
    from ncte.translation import dryrun
    from ncte.translation.orchestrator import RunOptions

    from ..helpers import run_book

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NCTE_LLM_PROVIDER", "claude")
    cfg = load_config(workspace=str(tmp_path / "ws"))
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    client = FakeBacked()
    opts = RunOptions(mode="context", model=cfg.openrouter.model, force=True, client="claude")
    before = dryrun.dry_run(conn, cfg, *_book(conn, cfg, fixtures), opts, client, log=lambda m: None)
    assert estimate_view(before, "claude").get("plan_low_usd") is None  # no history: nothing to say
    monkeypatch.setattr(subprocess, "run", client.run)
    book, rid, summary = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    monkeypatch.setattr(dryrun, "run_rates", functools.partial(dryrun.run_rates, min_chars=1))  # the sample is small
    plan = dryrun.dry_run(conn, cfg, book, opts, client, log=lambda m: None)
    assert plan.stage_estimates["translate"]["from"].endswith("earlier run(s)")
    assert plan.stage_estimates["scene_analyze"]["from"].endswith("earlier run(s)")  # Sonnet's requests count
    assert plan.estimate_high_usd >= plan.estimate_usd > 0
    view = estimate_view(plan, "claude")
    assert view["plan_low_usd"] == round(plan.estimate_usd, 2) and view["plan_high_usd"] == round(plan.estimate_high_usd, 2)
    from pathlib import Path

    import ncte
    js = (Path(ncte.__file__).resolve().parents[2] / "src/ncte/gui/static/app.js").read_text(encoding="utf-8")
    assert "plan_low_usd" in js and "照定價約" in js


def _book(conn, cfg, fixtures):
    from ncte.library import import_book

    return (import_book(conn, cfg, fixtures["basic"])[0],)
