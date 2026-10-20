"""The ChatGPT plan as the model (`codex exec`): what is sent, what is kept, waiting, stopping, and a whole book."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from ncte.errors import FatalAPIError, RetryableAPIError
from ncte.llm.base import LLMRequest
from ncte.llm.codex_cli import CodexCLIClient, parse_features
from ncte.llm.fake import FakeLLM

FEATURES = """\
undo                         stable             true
shell_tool                   stable             true
web_search_request           stable             false
unified_exec                 experimental       true
old_thing                    removed            true
rmcp_client                  under development  true
"""

@pytest.fixture(autouse=True)
def _no_model_cache(monkeypatch, tmp_path):
    """Never read the user's own Codex model cache: the built-in list unless a test writes one."""
    monkeypatch.setenv("NCTE_CODEX_MODELS_CACHE", str(tmp_path / "no-models-cache.json"))
    monkeypatch.setenv("NCTE_CODEX_SESSIONS", str(tmp_path / "sessions"))  # never the user's own sessions


TURN_USAGE = {"input_tokens": 10400, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 80,
              "reasoning_output_tokens": 30}


def _req(**kw: Any) -> LLMRequest:
    base = {"stage": "translate", "model": "codex/gpt-6.1-sol", "schema_name": "x",
            "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": '{"units": []}'}],
            "json_schema": {"type": "object"}}
    return LLMRequest(**{**base, **kw})


def _events(*events: dict[str, Any]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


OK_EVENTS = ({"type": "thread.started", "thread_id": "t-1"}, {"type": "turn.started"},
             {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "…"}},
             {"type": "turn.completed", "usage": TURN_USAGE})


class Answer:
    """One `codex exec` outcome: what it writes to the -o file, prints on stdout (JSONL) and stderr, and exits with."""

    def __init__(self, last: str | None = '{"translations": []}', events: tuple[dict[str, Any], ...] = OK_EVENTS,
                 code: int = 0, stderr: str = ""):
        self.last, self.events, self.code, self.stderr = last, events, code, stderr


class Codex:
    """Stands in for `subprocess.run`: answers `codex features list` and each `codex exec`, keeping what was sent
    (the arguments, stdin, the files the arguments name, the working directory)."""

    def __init__(self, *answers: Answer,
                 features: str | Exception | subprocess.CompletedProcess[str] = FEATURES):
        self.answers = list(answers)
        self.features = features
        self.feature_calls = 0
        self.args: list[list[str]] = []
        self.inputs: list[str] = []
        self.files: list[dict[str, str]] = []
        self.cwds: list[str] = []

    def __call__(self, args: list[str], *, input: str | None = None, cwd: str | None = None,
                 **kw: Any) -> subprocess.CompletedProcess[str]:
        if args[1:3] == ["features", "list"]:
            self.feature_calls += 1
            if isinstance(self.features, Exception):
                raise self.features
            if isinstance(self.features, subprocess.CompletedProcess):
                return self.features
            return subprocess.CompletedProcess(args, 0, self.features, "")
        self.args.append(args)
        self.inputs.append(input or "")
        self.cwds.append(cwd or "")
        assert cwd and Path(cwd).is_dir() and not any(Path(cwd).iterdir())  # run in an empty directory
        seen = {"instructions": Path(_config(args, "model_instructions_file")).read_text(encoding="utf-8")}
        if "--output-schema" in args:
            seen["schema"] = Path(args[args.index("--output-schema") + 1]).read_text(encoding="utf-8")
        self.files.append(seen)
        a = self.answers.pop(0)
        if a.last is not None:
            Path(args[args.index("-o") + 1]).write_text(a.last, encoding="utf-8")
        return subprocess.CompletedProcess(args, a.code, _events(*a.events), a.stderr)


def _config(args: list[str], key: str) -> str:
    """The value of ``-c key=<TOML string>``."""
    for i, a in enumerate(args):
        if a == "-c" and args[i + 1].startswith(key + "="):
            return json.loads(args[i + 1].split("=", 1)[1])
    raise KeyError(key)


def test_features_list_is_parsed_enabled_and_not_removed():
    assert parse_features(FEATURES) == ["undo", "shell_tool", "unified_exec", "rmcp_client"]
    assert parse_features("Feature  Stage  Enabled\n\nnonsense\n") == []


def test_a_request_is_one_plain_codex_exec_with_the_steps_schema_model_and_effort(monkeypatch):
    codex = Codex(Answer(), Answer())
    monkeypatch.setattr(subprocess, "run", codex)
    c = CodexCLIClient()
    r = c.complete(_req(extra={"reasoning": {"effort": "high"}}))
    a = codex.args[0]
    assert a[:2] == ["codex", "exec"] and a[-1] == "-"  # the prompt comes on stdin
    assert "--ephemeral" not in a  # the session file says how much of the plan is used (read, then removed)
    for flag in ("--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "--json"):
        assert flag in a
    assert a[a.index("-s") + 1] == "read-only"
    assert a[a.index("-m") + 1] == "gpt-6.1-sol"  # the part after codex/
    assert _config(a, "model_reasoning_effort") == "high"
    disabled = [a[i + 1] for i, x in enumerate(a) if x == "--disable"]
    assert disabled == ["undo", "shell_tool", "unified_exec", "rmcp_client"]  # every enabled feature
    assert json.loads(codex.files[0]["schema"]) == {"type": "object"}
    assert r.content == '{"translations": []}' and r.generation_id == "t-1" and r.provider == "codex"
    assert r.cost_usd == 0.0 and r.usage["list_cost_usd"] == 0
    assert r.usage["prompt_tokens"] == 10400 and r.usage["completion_tokens"] == 80
    assert r.usage["reasoning_tokens"] == 30 and r.usage["cache_read_tokens"] == 0
    c.complete(_req(extra={"reasoning": {"max_tokens": 3000}}))  # a token cap has no level: the model's default
    assert not any(x.startswith("model_reasoning_effort=") for x in codex.args[1])
    assert codex.feature_calls == 1  # asked once per client, not per request
    assert all(not Path(d).exists() for d in codex.cwds)  # each request's temporary directory is removed


def test_an_effort_the_model_does_not_take_is_not_sent(monkeypatch, tmp_path):
    cache = tmp_path / "models_cache.json"
    cache.write_text(json.dumps({"models": [{"slug": "gpt-6-luna", "visibility": "list", "context_window": 272000,
                                             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]}]}),
                     encoding="utf-8")
    codex = Codex(Answer())
    monkeypatch.setattr(subprocess, "run", codex)
    CodexCLIClient(models_cache=cache).complete(_req(model="codex/gpt-6-luna", extra={"reasoning": {"effort": "high"}}))
    assert not any(x.startswith("model_reasoning_effort=") for x in codex.args[0])


def test_the_fixed_rules_are_the_instructions_and_the_batchs_own_rules_go_first_on_stdin(monkeypatch):
    codex = Codex(Answer(), Answer(), Answer())
    monkeypatch.setattr(subprocess, "run", codex)
    c = CodexCLIClient()
    fixed = "FIXED RULES\n"

    def req(own: str) -> LLMRequest:
        return _req(messages=[{"role": "system", "content": fixed + own}, {"role": "user", "content": '{"units": [1]}'}],
                    cached_prefix_chars=len(fixed))

    c.complete(req("\n## 本批資料的附加說明\nCOND A\n"))
    c.complete(req("\n## 本批資料的附加說明\nCOND B\n"))
    c.complete(req("\n## 本批資料的附加說明\nCOND A\n\n## 重要更正\n上一次的輸出未通過程式驗證：missing P3\n"))
    instructions = [f["instructions"] for f in codex.files]
    assert instructions == [fixed] * 3  # the same on every request of a step: only the fixed rules
    assert "COND A" in codex.inputs[0] and "COND B" in codex.inputs[1]
    assert "重要更正" in codex.inputs[2] and "missing P3" in codex.inputs[2]
    assert all(u.endswith('{"units": [1]}') for u in codex.inputs)  # the data comes last


def test_without_a_schema_no_output_schema_is_sent(monkeypatch):
    codex = Codex(Answer('{"a": 1}'))
    monkeypatch.setattr(subprocess, "run", codex)
    r = CodexCLIClient().complete(_req(json_schema=None))
    assert "--output-schema" not in codex.args[0] and r.content == '{"a": 1}'


def test_when_features_list_fails_nothing_is_disabled_and_it_is_said(monkeypatch):
    for broken in (FileNotFoundError("codex"), subprocess.TimeoutExpired("codex", 30),
                   subprocess.CompletedProcess([], 2, "", "error: unrecognized subcommand 'features'")):
        codex = Codex(Answer(), features=broken)
        monkeypatch.setattr(subprocess, "run", codex)
        said: list[str] = []
        CodexCLIClient(log=said.append).complete(_req())
        assert "--disable" not in codex.args[0]
        assert len(said) == 1 and "features" in said[0]


def test_a_failure_is_waited_out_and_a_usage_limit_at_a_fixed_interval(monkeypatch):
    limit = Answer(None, ({"type": "thread.started", "thread_id": "t"},
                          {"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again at 9:00."}}),
                   code=1)
    codex = Codex(*([limit] * 20 + [Answer()]))
    monkeypatch.setattr(subprocess, "run", codex)
    waited: list[float] = []
    r = CodexCLIClient(waits=(30, 60), sleep=waited.append, limit_wait=15).complete(_req())
    assert waited == [15] * 20 and r.attempts == 21
    monkeypatch.setattr(subprocess, "run", Codex(*([limit] * 5)))
    with pytest.raises(RetryableAPIError, match="did not lift"):
        CodexCLIClient(waits=(1,), sleep=lambda s: None, limit_wait=15, limit_max=40).complete(_req())

    # anything else goes down the ladder: an error event, no turn.completed, a non-zero exit
    busy = Answer(None, ({"type": "error", "message": "stream disconnected before completion"},), code=1)
    unfinished = Answer('{"a": 1}', ({"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}))
    exited = Answer('{"a": 1}', OK_EVENTS, code=1, stderr="something went wrong")
    errored = Answer('{"a": 1}', (*OK_EVENTS, {"type": "error", "message": "reconnecting"}))
    codex = Codex(busy, unfinished, exited, errored, Answer())
    monkeypatch.setattr(subprocess, "run", codex)
    waited = []
    said: list[str] = []
    r = CodexCLIClient(waits=(30, 60, 120, 300), sleep=waited.append, log=said.append).complete(_req())
    assert waited == [30, 60, 120, 300] and r.attempts == 5
    assert "stream disconnected" in said[0] and all(s.startswith("Codex") for s in said)
    monkeypatch.setattr(subprocess, "run", Codex(busy, busy))
    with pytest.raises(RetryableAPIError, match="kept failing"):
        CodexCLIClient(waits=(1,), sleep=lambda s: None).complete(_req())


def test_a_timeout_is_waited_out(monkeypatch):
    calls = {"n": 0}
    codex = Codex(Answer())

    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if args[1] == "exec" and calls["n"] == 0:
            calls["n"] += 1
            raise subprocess.TimeoutExpired(args, 5)
        return codex(args, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    waited: list[float] = []
    r = CodexCLIClient(waits=(30,), sleep=waited.append).complete(_req())
    assert waited == [30] and r.attempts == 2


@pytest.mark.parametrize("answer", [
    Answer(None, ({"type": "error", "message": "Not logged in. Run `codex login`."},), code=1),
    Answer(None, (), code=1, stderr="Error: unexpected status 401 Unauthorized"),
    Answer(None, ({"type": "turn.failed", "error": {"message": "please login again"}},), code=1),
])
def test_no_login_stops_at_once(monkeypatch, answer):
    codex = Codex(answer, Answer())
    monkeypatch.setattr(subprocess, "run", codex)
    waited: list[float] = []
    with pytest.raises(FatalAPIError, match="(?i)log"):
        CodexCLIClient(waits=(30, 60), sleep=waited.append).complete(_req())
    assert len(codex.args) == 1 and not waited


@pytest.mark.parametrize("answer", [
    Answer(None, (), code=2, stderr="error: unexpected argument '--frob' found\n\nUsage: codex exec [OPTIONS]"),
    Answer(None, (), code=1, stderr="Error: unknown feature `shell_tool`"),
])
def test_a_rejected_command_line_stops_at_once_and_says_why(monkeypatch, answer):
    codex = Codex(answer, Answer())
    monkeypatch.setattr(subprocess, "run", codex)
    waited: list[float] = []
    with pytest.raises(FatalAPIError) as err:
        CodexCLIClient(waits=(30, 60), sleep=waited.append).complete(_req())
    assert len(codex.args) == 1 and not waited
    assert answer.stderr[:30] in str(err.value)


def test_codex_not_installed_stops_at_once(monkeypatch):
    def run(args: list[str], **kw: Any) -> Any:
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(FatalAPIError, match="not installed"):
        CodexCLIClient(sleep=lambda s: None).complete(_req())


def test_an_answer_that_mentions_logging_in_is_an_answer(monkeypatch):
    text = '{"translations": [{"unit_id": "P1", "text": "請先 login（401 unauthorized）"}]}'
    codex = Codex(Answer(text))
    monkeypatch.setattr(subprocess, "run", codex)
    r = CodexCLIClient(waits=(1,), sleep=lambda s: None).complete(_req())
    assert r.content == text and r.attempts == 1


def test_an_empty_answer_is_a_model_output_error(monkeypatch):
    from ncte.errors import ModelOutputError

    monkeypatch.setattr(subprocess, "run", Codex(Answer("  ")))
    with pytest.raises(ModelOutputError):
        CodexCLIClient(waits=()).complete(_req())


def test_the_catalog_comes_from_codexs_model_cache_or_a_small_built_in_list(tmp_path):
    cache = tmp_path / "models_cache.json"
    cache.write_text(json.dumps({"models": [
        {"slug": "gpt-6.1-sol", "visibility": "list", "context_window": 500000,
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
        {"slug": "gpt-reserve", "visibility": "hide", "supported_reasoning_levels": [{"effort": "low"}]},
        {"slug": "gpt-6-luna", "visibility": "list", "supported_reasoning_levels": [{"effort": "low"}]}]}),
        encoding="utf-8")
    models = CodexCLIClient(models_cache=cache, models=["codex/gpt-9-test", "claude/opus"]).list_models()
    ids = [m["id"] for m in models]
    assert ids == ["codex/gpt-6.1-sol", "codex/gpt-6-luna", "codex/gpt-9-test"]  # hidden ones left out
    first = models[0]
    assert first["pricing"] == {"prompt": "0", "completion": "0"} and first["context_length"] == 500000
    assert {"response_format", "structured_outputs", "reasoning"} <= set(first["supported_parameters"])
    assert first["reasoning"]["supported_efforts"] == ["low", "medium", "high", "xhigh", "max", "ultra"]
    assert models[1]["reasoning"]["supported_efforts"] == ["low"]
    built_in = [m["id"] for m in CodexCLIClient(models_cache=tmp_path / "missing.json").list_models()]
    assert built_in == ["codex/gpt-6.1-sol", "codex/gpt-6-sol", "codex/gpt-6-luna"]
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    assert [m["id"] for m in CodexCLIClient(models_cache=tmp_path / "broken.json").list_models()] == built_in


class FakeBacked(CodexCLIClient):
    """The real client with `codex exec` answered by the offline fake model (which needs the request's stage)."""

    def __init__(self, models: list[str]) -> None:
        super().__init__(waits=())
        self.fake = FakeLLM(models=models)
        self.local = threading.local()
        self.seen: list[tuple[str, str, str | None]] = []

    def complete(self, req: LLMRequest) -> Any:
        self.local.req = req
        return super().complete(req)

    def run(self, args: list[str], *, input: str | None = None, cwd: str | None = None,
            **kw: Any) -> subprocess.CompletedProcess[str]:
        if args[1:3] == ["features", "list"]:
            return subprocess.CompletedProcess(args, 0, FEATURES, "")
        req = self.local.req
        effort = next((json.loads(x.split("=", 1)[1]) for x in args if x.startswith("model_reasoning_effort=")), None)
        self.seen.append((req.stage.split(":")[0], args[args.index("-m") + 1], effort))
        Path(args[args.index("-o") + 1]).write_text(self.fake.complete(req).content, encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, _events(*OK_EVENTS), "")


def test_the_codex_provider_runs_on_codex_models_only(monkeypatch, tmp_path):
    from ncte.cli import make_client
    from ncte.config import load_config, proofread_passes

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NCTE_LLM_PROVIDER", "codex")
    cfg = load_config()
    o = cfg.openrouter
    assert o.provider == "codex" and o.model == "codex/gpt-6.1-sol" and o.temperature is None
    assert o.reasoning_effort == "medium" and o.stage_models == {} and o.review_model is None
    assert proofread_passes(cfg) == 2  # a pass costs quota, not money (as on the Claude plan)
    said: list[str] = []
    client = make_client(cfg, log=said.append)
    assert isinstance(client, CodexCLIClient) and client.log == said.append
    (tmp_path / "ncte.yaml").write_text(
        "openrouter:\n  model: codex/gpt-6-luna\n  review_model: google/x\n  fallback_models: [google/y, codex/gpt-6-sol]\n"
        "  stage_models: {proofread: codex/gpt-6-sol, reader: google/y, scene_analyze: claude/sonnet}\n",
        encoding="utf-8")
    o = load_config(tmp_path / "ncte.yaml").openrouter  # a chosen Codex model is kept; others are dropped
    assert o.model == "codex/gpt-6-luna" and o.stage_models == {"proofread": "codex/gpt-6-sol"}
    assert o.review_model is None and o.fallback_models == ["codex/gpt-6-sol"]


def test_a_claude_run_can_give_one_step_to_codex_and_the_claude_defaults_stay(monkeypatch, tmp_path):
    from ncte.cli import make_client
    from ncte.config import CLAUDE_READ_STAGES, load_config
    from ncte.llm.claude_cli import ClaudeCLIClient
    from ncte.llm.routing import RoutingClient

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NCTE_LLM_PROVIDER", "claude")
    plain = load_config().openrouter
    assert plain.stage_models == dict.fromkeys(CLAUDE_READ_STAGES, "claude/sonnet")  # Codex only when chosen
    assert not any(m.startswith("codex/") for m in plain.stage_models.values())
    (tmp_path / "only.yaml").write_text("openrouter:\n  stage_models: {proofread: codex/gpt-6.1-sol}\n", encoding="utf-8")
    one = load_config(tmp_path / "only.yaml").openrouter  # one step on Codex: the reading steps stay on Sonnet
    assert one.model == "claude/opus"
    assert one.stage_models == {**dict.fromkeys(CLAUDE_READ_STAGES, "claude/sonnet"), "proofread": "codex/gpt-6.1-sol"}
    (tmp_path / "ncte.yaml").write_text("openrouter:\n  model: claude/opus\n"
                                        "  stage_models: {proofread: codex/gpt-6.1-sol}\n", encoding="utf-8")
    cfg = load_config(tmp_path / "ncte.yaml")
    assert cfg.openrouter.stage_models == {"proofread": "codex/gpt-6.1-sol"}  # kept: it names its own source
    client = make_client(cfg)
    assert isinstance(client, RoutingClient) and client.name == "claude"
    assert isinstance(client.client_for("codex/gpt-6.1-sol"), CodexCLIClient)
    assert isinstance(client.client_for("claude/opus"), ClaudeCLIClient)
    assert "codex/gpt-6.1-sol" in [m["id"] for m in client.list_models()]


def test_a_whole_book_runs_on_the_codex_provider(monkeypatch, tmp_path, fixtures):
    from ncte.config import load_config
    from ncte.memory.db import connect, migrate

    from ..helpers import run_book

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NCTE_LLM_PROVIDER", "codex")
    cfg = load_config(workspace=str(tmp_path / "ws"))
    client = FakeBacked(["codex/gpt-6.1-sol"])
    monkeypatch.setattr(subprocess, "run", client.run)
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    book, rid, summary = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    assert summary.status == "completed", summary
    by_stage = {st: (m, e) for st, m, e in client.seen}
    assert by_stage["translate"] == ("gpt-6.1-sol", "medium") and by_stage["scene_analyze"][0] == "gpt-6.1-sol"
    assert by_stage["proofread"] == ("gpt-6.1-sol", "high")  # a judging step thinks at high
    assert sum(1 for st, _, _ in client.seen if st == "proofread") >= 2  # two passes
    rows = conn.execute("SELECT cost_usd, usage_json, model FROM llm_requests WHERE run_id=?", (rid,)).fetchall()
    assert rows and all(r[0] == 0 for r in rows)  # nothing billed
    assert all(json.loads(r[1])["list_cost_usd"] == 0 and json.loads(r[1])["prompt_tokens"] == 10400 for r in rows)


def test_a_claude_run_proofreads_on_codex_when_set(monkeypatch, tmp_path, fixtures):
    from ncte.config import load_config
    from ncte.llm.routing import RoutingClient
    from ncte.memory.db import connect, migrate

    from ..helpers import run_book
    from .test_claude_cli import FakeBacked as ClaudeBacked

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NCTE_LLM_PROVIDER", "claude")
    (tmp_path / "ncte.yaml").write_text("openrouter:\n  stage_models: {proofread: codex/gpt-6.1-sol}\n", encoding="utf-8")
    cfg = load_config(tmp_path / "ncte.yaml", workspace=str(tmp_path / "ws"))
    claude, codex = ClaudeBacked(), FakeBacked(["codex/gpt-6.1-sol"])

    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return (codex.run if args[0] == "codex" else claude.run)(args, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    book, rid, summary = run_book(conn, cfg, fixtures["basic"], RoutingClient(claude, {"codex/": codex}), mode="context")
    assert summary.status == "completed", summary
    assert {st for st, _, _ in codex.seen} == {"proofread"}
    assert {e for st, _, e in codex.seen} == {"high"}
    assert "proofread" not in {st for st, _, _ in claude.seen} and "translate" in {st for st, _, _ in claude.seen}


def test_the_settings_page_offers_codex_and_shows_no_bill_for_it():
    import ncte

    js = (Path(ncte.__file__).resolve().parents[2] / "src/ncte/gui/static/app.js").read_text(encoding="utf-8")
    assert '<option value="codex"' in js and "Codex（ChatGPT 方案）" in js
    assert "Codex 方案（不另收費）" in js and 'provider === "codex"' in js
    card = js[js.index("const codexCard"):]
    card = card[:card.index("</div>`;")]
    for said in ("找到 Codex", "找不到 Codex", "ChatGPT 訂閱", "不另外收費", "固定負擔"):
        assert said in card, said


def _rollout(sessions: Path, thread: str, *limits: dict[str, Any] | None) -> Path:
    """A session file as Codex writes it without --ephemeral: events, some carrying the plan's rate limits."""
    day = sessions / "2026" / "10" / "20"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-10-20T10-00-00-{thread}.jsonl"
    lines = [{"type": "session_meta", "payload": {"id": thread}}]
    for lim in limits:
        lines.append({"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": lim}})
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def test_the_plans_usage_is_read_from_the_threads_session_file_which_is_then_removed(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions"
    other = _rollout(sessions, "t-other", {"limit_id": "codex", "primary": {"used_percent": 99.0}})
    early = {"limit_id": "codex", "primary": {"used_percent": 16.0, "window_minutes": 10080, "resets_at": 1}}
    late = {"limit_id": "codex", "primary": {"used_percent": 17.0, "window_minutes": 10080, "resets_at": 1792000000},
            "secondary": {"used_percent": 42.5, "window_minutes": 300, "resets_at": 1791000000}}
    codex = Codex(Answer(), Answer())
    real = codex.__call__

    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        out = real(args, **kw)
        if args[1] == "exec" and len(codex.args) == 1:
            _rollout(sessions, "t-1", early, None, late)
        return out

    monkeypatch.setattr(subprocess, "run", run)
    c = CodexCLIClient()
    r = c.complete(_req())
    assert r.usage["plan"] == {"windows": {"weekly": {"used": 0.17, "resets_at": 1792000000},
                                           "5h": {"used": 0.425, "resets_at": 1791000000}}, "source": "codex"}
    assert not list(sessions.rglob("*t-1*")) and other.exists()  # only this thread's file is removed
    r2 = c.complete(_req())  # no session file: nothing filled, the answer stands
    assert "plan" not in r2.usage and r2.content == '{"translations": []}'


def test_an_unreadable_session_file_leaves_the_plan_out(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions"
    codex = Codex(Answer())
    real = codex.__call__

    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        out = real(args, **kw)
        if args[1] == "exec":
            p = _rollout(sessions, "t-1")
            p.write_text("not json\n{", encoding="utf-8")
        return out

    monkeypatch.setattr(subprocess, "run", run)
    r = CodexCLIClient().complete(_req())
    assert "plan" not in r.usage and r.content == '{"translations": []}'


def test_schemas_are_sent_in_openai_strict_form_and_the_nulls_of_optional_fields_are_dropped():
    """OpenAI's strict structured output wants every property required (real run, 2026-10-20: "'required' is required
    to include every key in properties"). An optional field is sent as required-but-nullable, and a null the model
    gives for it is dropped before NCTE's own validation, which stays as strict as before. A field that may itself be
    null (a speaker nobody can name) keeps its null."""
    from ncte.llm.codex_cli import from_openai_answer, to_openai_strict
    from ncte.llm.schemas import SceneWindowsOutput, strict_json_schema

    ours = strict_json_schema(SceneWindowsOutput)
    sent = to_openai_strict(ours)

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)
    check(sent)
    item = ours["properties"]["breaks"]["items"]
    optional = [k for k in item["properties"] if k not in item["required"]]
    assert optional  # the case that was rejected
    answer = {"first_kind": "present", "breaks": [{**{k: None for k in optional},
              **{k: ("x" if k == "unit_id" else "present" if k == "kind" else True) for k in item["required"]}}]}
    back = from_openai_answer(answer, ours)
    assert all(k not in back["breaks"][0] for k in optional)
    SceneWindowsOutput.model_validate(back)
    assert from_openai_answer({"a": None}, {"type": "object", "properties": {"a": {"type": ["string", "null"]}},
                                            "required": ["a"]}) == {"a": None}


def test_a_schema_codex_rejects_stops_at_once(monkeypatch, tmp_path):
    bad = {"type": "error", "error": {"message": "Invalid schema for response_format 'codex_output_schema': x"}}
    calls = []

    def run(args, **kw):
        if "features" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, json.dumps(bad) + "\n", "")
    monkeypatch.setattr(subprocess, "run", run)
    c = CodexCLIClient(waits=(1, 1), sleep=lambda s: None, sessions=tmp_path)
    with pytest.raises(FatalAPIError, match="schema"):
        c.complete(_req())
    assert len(calls) == 1
