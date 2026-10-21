"""The user's Antigravity plan as the model: each request is one fresh `agy -p` call (Antigravity CLI, print mode).

Built like the ChatGPT plan's client (``codex_cli``). agy has no way to replace its own instructions and reads the
prompt only from the command line (``--print=<prompt>``; stdin needs a stream-json protocol), so the step's fixed
rules, the step's JSON schema (written out as for Codex: ``codex_cli.schema_rule``), the batch's own rules and the
request's JSON go into that one prompt, in this order. A prompt too long for the command line (``ARG_LIMIT`` bytes)
is written to ``request.md`` in the call's empty working directory and the prompt asks the model to read it.
``--json-schema`` is not used: NCTE validates the answer against the schema as for every source. Each call runs in its
own empty temporary directory with ``--sandbox`` (terminal restricted) and slash commands off. The answer is read
from ``--output-format stream-json``'s ``result`` event and the process is then stopped (``_run_turn``: agy
sometimes never exits after the turn).

agy sends its own instructions and tool descriptions with every request: measured 2026-10-21 (agy 1.4.4), a
one-sentence request carried about 7,600 input tokens, some 60 tool descriptions among them (browser, files, shell,
web search, subagents). A custom agent can name the tools it has (``tools`` in ``.agents/agents/<name>/agent.md`` of
the working directory); each call writes ``PLAIN_AGENT`` there, with only ``finish`` (and ``view_file`` when the
request is in ``request.md``), and passes ``--agent``: the same one-sentence request then carried about 2,400 input
tokens; the model's thinking did not change. The agent's body is a one-line instruction to answer directly; NCTE's
rules stay in the prompt. ``openrouter.agy_plain_agent: false`` turns it off; when agy does not know the agent (a
CLI update), the call says so once in the log and goes on without it. The user's own rules (``~/.gemini/GEMINI.md``)
are read by agy too; NCTE cannot turn that off.

Model ids are ``agy/<name>``: ``agy models`` lists each model once per reasoning level (``gemini-3.8-flash-high``),
so ``agy/gemini-3.8-flash`` with a step's level becomes ``gemini-3.8-flash-<level>`` when agy lists that (else the
listed id as given, else medium). A family name without the version (``agy/gemini-flash``) is the newest model of
that family agy lists when the run starts, so a setting survives a model being retired; the id actually used is kept
in ``usage["agy_model"]``, and the level that id carries in ``usage["effort"]`` (the step's own, e.g. ``high`` for a
fix check, not the run's; ``usage["effort_asked"]`` when the id given carried another level). The level is the model
id: ``--effort`` with an id that carries another level is refused by agy, so it is not sent. A versioned id agy no longer lists is not in the catalog, so a run naming it stops before
it starts (as for any step model the catalog does not list). Nothing is billed through NCTE: ``cost_usd`` and ``list_cost_usd`` are 0; the
token counts agy prints are kept in ``usage``. How much of the plan is used comes from ``agy -p /quota`` (answered
without a model turn and without spending quota), read after a request at most every ``QUOTA_EVERY`` seconds: the
group of the model (Gemini models; Claude and GPT models) goes to ``usage["plan"]`` (``{"windows": {"5h"|"weekly":
{"used": 0–1, "resets_at": epoch}}, "source": "agy"}``). agy keeps every conversation on disk
(``~/.gemini/antigravity-cli/{conversations,annotations,presence,brain}/<conversation id>…``); NCTE removes those of
its own calls (only that conversation's).

Failures follow the Claude plan's waits (``claude_cli``): a usage limit is asked again at a fixed interval, anything
else goes down the same ladder; no login, or a command line agy rejects, stops at once. agy exits 3 and prints an
``AGY_ERROR`` line when a model or agent error ends the turn.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..errors import EmptyAnswerError, FatalAPIError, RetryableAPIError
from .base import LLMRequest, LLMResponse
from .claude_cli import EFFORTS, LIMIT_MAX, LIMIT_WAIT, RETRY_WAITS, ClaudeCLIClient, effort_of
from .codex_cli import schema_rule

PREFIX = "agy/"
# used when `agy models` cannot be read (the account's models as of 2026-10-21)
BUILT_IN_MODELS = ("gemini-3.8-flash", "gemini-4-argon")
HOME = Path("~/.gemini/antigravity-cli")
ARG_LIMIT = 600_000  # bytes of prompt on the command line (macOS ARG_MAX is 1 MiB, the environment counts too)
QUOTA_EVERY = 60.0
SMALL_TIMEOUT = 60
USAGE_LIMIT = ("resource exhausted", "resource_exhausted", "quota", "rate limit", "usage limit", "limit reached",
               "429", "try again")
NOT_LOGGED_IN = ("not signed in", "sign in", "not logged in", "login", "unauthenticated", "401")
BAD_COMMAND = ("flag provided but not defined", "unknown flag", "flag needs an argument", "invalid value",
               "took \"--", "unknown model", "model not found")
PLAIN_AGENT_NAME = "ncte-plain"
# tool names must be agy's own (a name agy does not have may hang the agent, per its docs)
PLAIN_AGENT_TOOLS = ("finish",)
FILE_AGENT_TOOLS = ("view_file", "finish")  # the request is in request.md: the model has to read it
PLAIN_AGENT = ("---\nname: {name}\ndescription: Answers one request with one JSON object; uses no tools.\n"
               "tools:\n{tools}mainAgent: true\nsubagent: false\n---\n"
               "Answer the request in the prompt directly with the requested output only. Do not use tools{but}.\n")
UNKNOWN_AGENT = ("unknown agent", "no custom agents", "agent not found")
READ_FILE = ("這次請求的完整內容在工作目錄的 `request.md`（{n} 字元）。請先完整讀取這個檔案（從頭到尾，"
             "不要只讀一部分），再照檔案裡的指示作答；不要修改任何檔案，只輸出答案。")


def split_level(name: str) -> tuple[str, str | None]:
    """``gemini-3.8-flash-high`` → (``gemini-3.8-flash``, ``high``); a name without a level → (name, None)."""
    base, _, last = name.rpartition("-")
    return (base, last) if base and last in EFFORTS else (name, None)


_VERSIONED = re.compile(r"^(?P<a>[a-z]+)-(?P<v>\d+(?:\.\d+)*)-(?P<b>[a-z][a-z0-9-]*)$")


def family_of(base: str) -> tuple[str, tuple[int, ...]] | None:
    """``gemini-3.8-flash`` → (``gemini-flash``, (3, 8)): the model's family name without its version, and the
    version; None for a name without a version in the middle."""
    m = _VERSIONED.match(base)
    return (f"{m['a']}-{m['b']}", tuple(int(x) for x in m["v"].split("."))) if m else None


def newest_of(family: str, bases: list[str]) -> str | None:
    """The listed model of that family with the highest version (``gemini-flash`` → ``gemini-3.8-flash``)."""
    found = [(f[1], b) for b in bases if (f := family_of(b)) and f[0] == family]
    return max(found)[1] if found else None


def _epoch(stamp: Any) -> int | None:
    try:
        return calendar.timegm(time.strptime(str(stamp).replace("Z", "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def plan_usage(data: dict[str, Any], model: str) -> dict[str, Any] | None:
    """``/quota``'s groups → ``usage["plan"]`` for the group the model is in ("Gemini 4 Argon" has its own;
    "Gemini Models" holds Flash and Pro; "Claude and GPT models" the rest); None when it says nothing usable."""
    def mine(g: dict[str, Any]) -> bool:
        name = str(g.get("name") or "").lower()
        if "argon" in model:
            return "argon" in name
        if model.startswith("gemini"):
            return "gemini" in name and "argon" not in name
        return "claude" in name or "gpt" in name

    windows: dict[str, Any] = {}
    for g in data.get("groups") or []:
        if not isinstance(g, dict) or not mine(g):
            continue
        for b in g.get("buckets") or []:
            if isinstance(b, dict) and isinstance(b.get("remaining_fraction"), (int, float)):
                windows.setdefault(str(b.get("window") or "window"),
                                   {"used": round(1 - float(b["remaining_fraction"]), 6),
                                    "resets_at": _epoch(b.get("reset_time"))})
        break
    return {"windows": windows, "source": "agy"} if windows else None


class AgyCLIClient:
    name = "agy"
    catalog_source = "agy"

    def __init__(self, *, models: list[str] | None = None, timeout: float = 1800,
                 waits: tuple[float, ...] = RETRY_WAITS, binary: str | None = None, sleep: Any = time.sleep,
                 log: Callable[[str], None] | None = None, limit_wait: float = LIMIT_WAIT,
                 limit_max: float = LIMIT_MAX, home: str | Path | None = None, plain_agent: bool = True):
        self.extra_models = [m for m in models or [] if m and m.startswith(PREFIX)]
        self.log = log
        self.timeout = timeout
        self.waits = waits
        self.limit_wait, self.limit_max = limit_wait, limit_max
        self.binary = binary or os.environ.get("NCTE_AGY_BIN") or "agy"
        self.sleep = sleep
        self.home = Path(home or os.environ.get("NCTE_AGY_HOME") or HOME).expanduser()
        self._listed: list[str] | None = None
        self._quota: tuple[float, dict[str, Any] | None] = (0.0, None)
        self._lock = threading.Lock()
        self.plain_agent = plain_agent  # set off for the client's life when agy does not know the agent

    # ------------------------------------------------------------ catalog
    def listed(self) -> list[str]:
        """The ids ``agy models`` prints (one per level), asked once; empty when it cannot be read."""
        with self._lock:
            if self._listed is None:
                try:
                    proc = subprocess.run([self.binary, "models"], capture_output=True, text=True,
                                          timeout=SMALL_TIMEOUT)
                    self._listed = [line.split("\t")[0].strip() for line in (proc.stdout or "").splitlines()
                                    if line.strip() and "\t" in line] if proc.returncode == 0 else []
                except (OSError, subprocess.SubprocessError):
                    self._listed = []
            return self._listed

    def list_models(self) -> list[dict[str, Any]]:
        """One row per model (its levels together), and any configured ``agy/<id>``. Prices are 0."""
        levels: dict[str, list[str]] = {}
        for mid in self.listed() or [f"{m}-medium" for m in BUILT_IN_MODELS]:
            base, level = split_level(mid)
            levels.setdefault(base, [])
            if level:
                levels[base].append(level)
        listed = bool(self.listed())
        for extra in self.extra_models:  # a configured model agy no longer lists is not offered (retired: say so)
            name = split_level(extra[len(PREFIX):])[0]
            if not listed and family_of(name) is not None:
                levels.setdefault(name, [])
        rows = dict(levels)
        for base in list(levels):  # a family name (agy/gemini-flash) is the newest listed model of that family
            if (fam := family_of(base)) and newest_of(fam[0], list(levels)) == base:
                rows.setdefault(fam[0], levels[base])
        return [{"id": PREFIX + base, "name": f"{base} through Antigravity" + (
                     f" (now {newest_of(base, list(levels))})" if base not in levels else ""),
                 "context_length": 1_000_000,
                 "supported_parameters": ["max_tokens", "response_format", "structured_outputs", "reasoning"],
                 "reasoning": {"mandatory": False, "supported_efforts": lv or ["medium"],
                               "default_effort": "medium" if "medium" in lv or not lv else lv[0]},
                 "pricing": {"prompt": "0", "completion": "0"}} for base, lv in rows.items()]

    def get_generation(self, generation_id: str) -> dict[str, Any] | None:
        return None

    def balance(self) -> dict[str, Any]:
        return {}

    def model_for(self, req: LLMRequest) -> str:
        """The id to give ``--model``: the configured name with the step's level when agy lists that."""
        name = req.model[len(PREFIX):] if req.model.startswith(PREFIX) else req.model
        listed = self.listed()
        bases = sorted({split_level(x)[0] for x in listed})
        if family_of(name) is None and (newest := newest_of(name, bases)):
            name = newest  # a family name: its newest listed model, recorded in usage["agy_model"]
        if name in listed or split_level(name)[1]:
            return name
        for level in (effort_of(req), "medium"):
            if level and f"{name}-{level}" in listed:
                return f"{name}-{level}"
        return name

    # ------------------------------------------------------------ requests
    def args(self, model: str, prompt: str, *, agent: str | None = None) -> list[str]:
        return [self.binary, "--model", model, "--output-format", "stream-json", "--sandbox",
                "--disable-slash-commands", *(["--agent", agent] if agent else []), f"--print={prompt}"]

    @staticmethod
    def write_agent(work: Path, *, reads_file: bool) -> str:
        """``PLAIN_AGENT`` in the call's working directory; its name, for ``--agent``."""
        tools = FILE_AGENT_TOOLS if reads_file else PLAIN_AGENT_TOOLS
        folder = work / ".agents" / "agents" / PLAIN_AGENT_NAME
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "agent.md").write_text(PLAIN_AGENT.format(
            name=PLAIN_AGENT_NAME, tools="".join(f"  - {t}\n" for t in tools),
            but=" except reading request.md" if reads_file else ""), encoding="utf-8")
        return PLAIN_AGENT_NAME

    def quota(self, model: str) -> dict[str, Any] | None:
        """The plan's usage for this model's group, read at most every ``QUOTA_EVERY`` seconds."""
        with self._lock:
            at, data = self._quota
            if time.monotonic() - at >= QUOTA_EVERY or data is None:
                try:
                    proc = subprocess.run([self.binary, "-p", "/quota", "--output-format", "json"],
                                          capture_output=True, text=True, timeout=SMALL_TIMEOUT)
                    out = json.loads(proc.stdout or "{}")
                    data = ((out.get("command") or {}).get("data") or {}) if isinstance(out, dict) else {}
                except (OSError, subprocess.SubprocessError, ValueError):
                    data = {}
                self._quota = (time.monotonic(), data)
        return plan_usage(data or {}, model)

    def forget(self, conversation: str | None) -> None:
        """Remove what agy kept of this conversation (only this one's files)."""
        if not conversation or "/" in conversation or conversation.startswith("."):
            return
        for path in (self.home / "conversations" / f"{conversation}.db",
                     self.home / "conversations" / f"{conversation}.db-wal",
                     self.home / "conversations" / f"{conversation}.db-shm",
                     self.home / "annotations" / f"{conversation}.pbtxt",
                     self.home / "presence" / f"{conversation}.lock"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        brain = self.home / "brain" / conversation
        if brain.is_dir() and brain.parent == self.home / "brain":
            shutil.rmtree(brain, ignore_errors=True)

    @staticmethod
    def read_result(text: str) -> dict[str, Any]:
        """The JSON object agy prints (the last line that parses as one)."""
        for line in reversed((text or "").strip().splitlines()):
            try:
                out = json.loads(line)
            except ValueError:
                continue
            if isinstance(out, dict):
                return out
        try:
            out = json.loads(text)
            return out if isinstance(out, dict) else {}
        except ValueError:
            return {}

    split = staticmethod(ClaudeCLIClient.split)

    def complete(self, req: LLMRequest) -> LLMResponse:
        system, user = self.split(req)
        prompt = system + schema_rule(req.json_schema) + "\n\n" + user
        model = self.model_for(req)
        start = time.monotonic()
        last = ""
        ladder = iter(self.waits)
        limit_waited = 0.0
        attempt = 0
        spent: dict[str, int] = {}

        def later(limit: bool) -> None:
            nonlocal limit_waited
            if limit:
                limit_waited += self.limit_wait
                if limit_waited > self.limit_max:
                    raise RetryableAPIError(f"Antigravity's usage limit did not lift in {self.limit_max / 3600:.0f} h: "
                                            f"{last[:300]}")
                wait = self.limit_wait
            else:
                nxt = next(ladder, None)
                if nxt is None:
                    raise RetryableAPIError(f"agy kept failing: {last[:300]}")
                wait = nxt
            if self.log:
                self.log(f"Antigravity：等待 {wait:.0f} 秒後重試（第 {attempt + 1} 次）：{last[:200]}")
            self.sleep(wait)

        def count(u: Any) -> None:
            for k, v in (u or {}).items() if isinstance(u, dict) else ():
                if isinstance(v, int) and k != "total_tokens":
                    spent[k] = spent.get(k, 0) + v

        with tempfile.TemporaryDirectory(prefix="ncte-agy-") as tmp:
            work = Path(tmp)
            reads_file = len(prompt.encode("utf-8")) > ARG_LIMIT
            if reads_file:
                (work / "request.md").write_text(prompt, encoding="utf-8")
                prompt = READ_FILE.format(n=len(prompt))
            while True:
                attempt += 1
                agent = self.write_agent(work, reads_file=reads_file) if self.plain_agent else None
                try:
                    proc = _run_turn(self.args(model, prompt, agent=agent), timeout=self.timeout, cwd=str(work))
                except subprocess.TimeoutExpired:
                    last = f"no answer within {self.timeout:.0f}s"
                    later(False)
                    continue
                except FileNotFoundError as exc:
                    raise FatalAPIError(f"Antigravity CLI (`{self.binary}`) is not installed or not on PATH") from exc
                out = self.read_result(proc.stdout or "")
                conversation = out.get("conversation_id") or None
                self.forget(conversation)
                stderr = (proc.stderr or "").strip()
                said = "\n".join(x for x in (str(out.get("error") or ""), stderr) if x).strip()
                failed = proc.returncode != 0 or out.get("status") != "SUCCESS"
                if failed:
                    count(out.get("usage"))
                    if agent and any(s in said.lower() for s in UNKNOWN_AGENT):
                        self.plain_agent = False  # a CLI that does not read the agent: on without it, at once
                        if self.log:
                            self.log(f"Antigravity：agy 不認得 NCTE 的精簡 agent，改用 agy 預設（每個請求多約 5,000 "
                                     f"token 的工具說明）：{said[:200]}")
                        attempt -= 1
                        continue
                    if any(s in said.lower() for s in BAD_COMMAND):
                        raise FatalAPIError(f"agy rejected the command (exit {proc.returncode}): {said[:300]}")
                    if any(s in said.lower() for s in NOT_LOGGED_IN) and not any(
                            s in said.lower() for s in USAGE_LIMIT):
                        raise FatalAPIError(f"Antigravity is not signed in (run `agy` once to sign in): {said[:300]}")
                    last = said[:500] or f"agy exited {proc.returncode} ({out.get('status') or 'no result'})"
                    later(any(s in said.lower() for s in USAGE_LIMIT))
                    continue
                result = str(out.get("response") or "").strip()
                if not result:
                    raise EmptyAnswerError("empty answer")
                raw = out.get("usage")
                u: dict[str, Any] = raw if isinstance(raw, dict) else {}
                failed_tries = dict(spent)

                def n(key: str, u: dict[str, Any] = u, f: dict[str, int] = failed_tries) -> int:
                    return int(u.get(key) or 0) + f.get(key, 0)

                usage: dict[str, Any] = {
                    "prompt_tokens": n("input_tokens"),
                    "completion_tokens": n("output_tokens"),  # thinking included (a one-line answer: 1,368)
                    "reasoning_tokens": n("thinking_tokens"),
                    "cache_read_tokens": n("cache_read_tokens"),
                    "cache_write_tokens": 0,
                    "list_cost_usd": 0, "effort": split_level(model)[1] or effort_of(req), "agy_model": model}
                if agent:
                    usage["agy_agent"] = agent
                if (asked := effort_of(req)) and asked != usage["effort"]:
                    usage["effort_asked"] = asked
                if failed_tries:
                    usage["failed_tries"] = failed_tries
                try:
                    plan = self.quota(model)
                except Exception:  # noqa: BLE001 — never lets the request fail
                    plan = None
                if plan:
                    usage["plan"] = plan
                return LLMResponse(content=result, model=req.model, provider="agy", generation_id=conversation,
                                   usage=usage, cost_usd=0.0, latency_ms=int((time.monotonic() - start) * 1000),
                                   finish_reason="stop", attempts=attempt, structured_mode=req.structured_mode)


def _run_turn(args: list[str], *, timeout: float, cwd: str) -> subprocess.CompletedProcess[str]:
    """One print-mode turn, read as it streams: the ``result`` event is the answer, and the process is stopped once it
    has come. Measured 2026-10-21 (agy 1.2.x): for some requests agy finishes the turn and prints the result event,
    then never exits (with ``--output-format json``, which prints only at exit, it then prints nothing at all); it
    ignores SIGTERM. ``stdout`` of the returned process is the result's JSON (as ``--output-format json`` prints
    it); with no result event, the error events seen, or nothing."""
    import codecs
    import select

    decode = codecs.getincrementaldecoder("utf-8")("replace")  # a character split between two reads stays whole
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, stdin=subprocess.DEVNULL)
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    result: dict[str, Any] | None = None
    errors: list[str] = []
    buf = ""
    try:
        while result is None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            ready, _, _ = select.select([proc.stdout], [], [], min(left, 5.0))
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            raw = os.read(proc.stdout.fileno(), 1 << 16)
            if not raw:
                break
            buf += decode.decode(raw)
            *lines, buf = buf.split("\n")
            for line in lines:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("event") == "result" and isinstance(ev.get("result"), dict):
                    result = ev["result"]
                    break
                if "error" in str(ev.get("event") or ""):
                    errors.append(json.dumps(ev, ensure_ascii=False)[:500])
    finally:
        if proc.poll() is None:
            proc.kill()  # the turn is over (or timed out): agy may never exit on its own
        try:
            _, raw_err = proc.communicate(timeout=10)
            err = (raw_err or b"").decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            err = ""
    out = json.dumps(result, ensure_ascii=False) if result is not None else "\n".join(errors)
    code = 0 if result is not None and result.get("status") == "SUCCESS" else (proc.returncode or 1)
    return subprocess.CompletedProcess(args, code, out, err or "")


def found() -> str | None:
    """Where the `agy` command is (for the settings page), or None."""
    return shutil.which(os.environ.get("NCTE_AGY_BIN") or "agy")
