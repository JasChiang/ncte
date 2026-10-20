"""The user's Claude plan as the model: each request is one fresh `claude -p` call (Claude Code in print mode).

The step's prompt becomes the system prompt (Claude Code's own is replaced; no tools, no settings, no MCP, no session
kept), the request's JSON is the user message: as close to a plain model call as the CLI allows, on the user's own
Claude login. Nothing is billed through NCTE: ``cost_usd`` is 0. What the CLI reports as the call's list-price cost
is kept in ``usage["list_cost_usd"]`` (with the token counts) so a run can say what it used.

How much of the plan is used: the call prints its events (``--output-format stream-json --verbose``), and one of them
(``rate_limit_event``, seen 2026-10-20) gives the share used of each window (``unifiedWindows``: ``five_hour``,
``seven_day``: ``utilization`` 0–1, ``resetsAt``). The last one goes to ``usage["plan"]`` in the same form as the
ChatGPT plan's (``codex_cli``): ``{"windows": {"5h": {"used", "resets_at"}, "weekly": {…}}, "source": "claude"}``.
The share is the whole account's (other use of Claude in the same hours counts too); without the event ``plan`` is
left out and the answer stands.

Each call runs in its own empty temporary directory: Claude Code reads the CLAUDE.md files of the directory it runs in
and its parents even with ``--setting-sources ""`` (measured 2026-10-20: a call from inside a project carried about
1,300 more input tokens and the model knew the project's name). ``--bare`` would skip them but takes only an API key,
not the plan's login. The user's own ``~/.claude/CLAUDE.md`` is still read.

A failed call is tried again later and the run says so before each wait (``log``). A usage limit lifts at a set time:
it is asked again every quarter hour for up to eight hours, however many times that takes. Any other failure (the
network, an overloaded service) goes down a ladder of growing waits that ends after about four hours. A call that can
never work (Claude Code rejects its own command line: exit 2, an unknown option) or no login stops at once.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from typing import Any

from ..errors import FatalAPIError, ModelOutputError, RetryableAPIError
from .base import LLMRequest, LLMResponse

PREFIX = "claude/"
MODELS = ("opus", "sonnet", "haiku")  # Claude Code's aliases; a full model id after the prefix is passed as is
EFFORTS = ("low", "medium", "high", "xhigh", "max")
RETRY_WAITS = (30, 60, 120, 300, 600, 900, 1800, 1800, 1800, 3600, 3600)  # seconds, for failures of other kinds
# A usage limit is not a failure that goes away by itself: the window resets at a set time (a whole-book run met one at
# 15:30 that reset at 19:00, longer than the ladder above). It is asked again every LIMIT_WAIT seconds, as long as it
# takes up to LIMIT_MAX, without using up the ladder.
LIMIT_WAIT = 900
LIMIT_MAX = 8 * 3600
USAGE_LIMIT = ("usage limit", "session limit", "rate limit", "resets ")
NOT_LOGGED_IN = ("not logged in", "please run /login", "invalid api key", "authentication")
BAD_COMMAND = ("unknown option", "unknown argument", "too many arguments")  # the same on every try
# the step's JSON schema is passed as `--json-schema` (Claude Code checks the answer against it): a schema it will not
# take is the same on every try
BAD_SCHEMA = ("invalid json schema", "invalid schema", "schema is invalid", "not a valid json schema",
              "schema is not valid", "invalid --json-schema")
USAGE_ERROR_EXIT = 2  # the CLI's exit code for a command line it rejects


def effort_of(req: LLMRequest) -> str | None:
    """The level the engine asked this step to think at; a token cap (``max:N``) has no level here."""
    eff = (req.extra.get("reasoning") or {}).get("effort")
    return str(eff) if eff in EFFORTS else None


class ClaudeCLIClient:
    name = "claude"
    catalog_source = "claude"

    def __init__(self, *, models: list[str] | None = None, timeout: float = 1800,
                 waits: tuple[float, ...] = RETRY_WAITS, binary: str | None = None, sleep: Any = time.sleep,
                 log: Callable[[str], None] | None = None, limit_wait: float = LIMIT_WAIT, limit_max: float = LIMIT_MAX):
        self.extra_models = [m for m in models or [] if m and m.startswith(PREFIX)]
        self.log = log  # told before each wait (the run's own log); None: silent
        self.timeout = timeout
        self.waits = waits
        self.limit_wait, self.limit_max = limit_wait, limit_max
        self.binary = binary or os.environ.get("NCTE_CLAUDE_BIN") or "claude"
        self.sleep = sleep

    # ------------------------------------------------------------ catalog
    def list_models(self) -> list[dict[str, Any]]:
        """Claude Code's model aliases (and any configured ``claude/<id>``). Prices are 0: nothing is billed here."""
        ids = dict.fromkeys([PREFIX + m for m in MODELS] + self.extra_models)
        return [{"id": i, "name": f"Claude ({i[len(PREFIX):]}) through Claude Code", "context_length": 1_000_000,
                 "supported_parameters": ["max_tokens", "response_format", "structured_outputs", "reasoning"],
                 "reasoning": {"mandatory": False, "supported_efforts": list(EFFORTS), "default_effort": "medium"},
                 "pricing": {"prompt": "0", "completion": "0"}} for i in ids]

    def get_generation(self, generation_id: str) -> dict[str, Any] | None:
        return None

    def balance(self) -> dict[str, Any]:
        return {}

    # ------------------------------------------------------------ requests
    def args(self, req: LLMRequest, system: str) -> list[str]:
        model = req.model[len(PREFIX):] if req.model.startswith(PREFIX) else req.model
        out = [self.binary, "-p", "--model", model, "--output-format", "stream-json", "--verbose", "--tools", "", "--system-prompt", system,
               "--no-session-persistence", "--strict-mcp-config", "--setting-sources", "",
               "--exclude-dynamic-system-prompt-sections"]
        if eff := effort_of(req):
            out += ["--effort", eff]
        if req.json_schema:  # the answer is checked by Claude Code too; the program's own validation still runs
            out += ["--json-schema", json.dumps(req.json_schema, ensure_ascii=False)]
        return out

    @staticmethod
    def split(req: LLMRequest) -> tuple[str, str]:
        """(system prompt, user message). Claude Code caches the system prompt as one block, read back only when it
        is byte-identical: it holds only the rules every request of the step shares (``cached_prefix_chars``); the
        step's JSON schema goes as ``--json-schema`` (``args``). This batch's conditional rules and a retry's
        correction go to the user message, before the data."""
        system = "\n".join(m["content"] for m in req.messages if m["role"] == "system")
        cut = req.cached_prefix_chars if 0 < req.cached_prefix_chars < len(system) else len(system)
        fixed, own = system[:cut], system[cut:].strip()
        user = "\n".join(m["content"] for m in req.messages if m["role"] == "user")
        return fixed, (own + "\n\n" + user if own else user)

    def complete(self, req: LLMRequest) -> LLMResponse:
        system, user = self.split(req)
        start = time.monotonic()
        last = ""
        ladder = iter(self.waits)
        limit_waited = 0.0
        attempt = 0

        def later(limit: bool) -> None:
            """Wait before the next try: a usage limit every ``limit_wait`` seconds up to ``limit_max``; anything else
            down the ladder, which ends."""
            nonlocal limit_waited
            if limit:
                limit_waited += self.limit_wait
                if limit_waited > self.limit_max:
                    raise RetryableAPIError(f"Claude Code's usage limit did not lift in {self.limit_max / 3600:.0f} h: {last[:300]}")
                wait = self.limit_wait
            else:
                nxt = next(ladder, None)
                if nxt is None:
                    raise RetryableAPIError(f"Claude Code kept failing: {last[:300]}")
                wait = nxt
            if self.log:
                self.log(f"Claude Code：等待 {wait:.0f} 秒後重試（第 {attempt + 1} 次）：{last[:200]}")
            self.sleep(wait)

        while True:
            attempt += 1
            try:
                with tempfile.TemporaryDirectory(prefix="ncte-claude-") as work:  # no project's CLAUDE.md
                    proc = subprocess.run(self.args(req, system), input=user, capture_output=True, text=True,
                                          timeout=self.timeout, cwd=work)
            except subprocess.TimeoutExpired:
                last = f"no answer within {self.timeout:.0f}s"
                later(False)
                continue
            except FileNotFoundError as exc:
                raise FatalAPIError(f"Claude Code (`{self.binary}`) is not installed or not on PATH") from exc
            stderr = (proc.stderr or "").strip()
            if proc.returncode == USAGE_ERROR_EXIT or any(s in stderr.lower() for s in BAD_COMMAND):
                raise FatalAPIError(f"Claude Code rejected the command (exit {proc.returncode}): {stderr[:200]}")
            text = (proc.stdout or "").strip()
            out, plan = read_stream(text)
            failed = proc.returncode != 0 or not out or out.get("is_error") or out.get("subtype", "success") != "success"
            message = str(out.get("result") or stderr or text)[:500]
            if failed and any(s in message.lower() for s in NOT_LOGGED_IN):  # never in a successful answer
                raise FatalAPIError(f"Claude Code is not logged in: {message[:200]}")
            said = f"{stderr}\n{out.get('result') or ''}".lower()
            if failed and req.json_schema and any(s in said for s in BAD_SCHEMA):
                raise FatalAPIError(f"Claude Code rejected the JSON schema (--json-schema): {message[:300]}")
            if failed:
                last = message or f"claude exited {proc.returncode}"
                later(any(s in message.lower() for s in USAGE_LIMIT))  # a usage limit, an overloaded service: later
                continue
            structured = out.get("structured_output")
            if isinstance(structured, (dict, list)):  # the parsed answer `--json-schema` asked for
                result = json.dumps(structured, ensure_ascii=False)
            else:
                result = str(out.get("result") or "")
            if not result.strip():
                raise ModelOutputError("empty answer")
            u = out.get("usage") or {}
            cache_write = int(u.get("cache_creation_input_tokens") or 0)
            cache_read = int(u.get("cache_read_input_tokens") or 0)
            usage: dict[str, Any] = {"prompt_tokens": int(u.get("input_tokens") or 0) + cache_write + cache_read,
                     "completion_tokens": int(u.get("output_tokens") or 0),
                     "cache_write_tokens": cache_write, "cache_read_tokens": cache_read,
                     "list_cost_usd": float(out.get("total_cost_usd") or 0), "effort": effort_of(req)}
            # the model ids the alias was really run on, as the result's per-model counts name them (when given)
            if isinstance(out.get("modelUsage"), dict) and out["modelUsage"]:
                usage["models"] = [str(k) for k in out["modelUsage"]]
            if plan:
                usage["plan"] = plan
            return LLMResponse(content=result, model=req.model, provider="claude", generation_id=out.get("session_id"),
                               usage=usage, cost_usd=0.0, latency_ms=int((time.monotonic() - start) * 1000),
                               finish_reason="stop", attempts=attempt, structured_mode=req.structured_mode)


WINDOW_NAMES = {"five_hour": "5h", "seven_day": "weekly"}  # the names the ChatGPT plan's windows get (codex_cli)


def read_stream(text: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(the ``result`` event, or one JSON object as ``--output-format json`` prints it; the plan's usage from the last
    ``rate_limit_event``, or None)."""
    result: dict[str, Any] = {}
    plan: dict[str, Any] | None = None
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if kind == "result" or (kind is None and not result):
            result = ev
        elif kind == "rate_limit_event":
            plan = plan_of(ev.get("rate_limit_info")) or plan
    if not result:
        try:
            whole = json.loads(text) if text else {}
            result = whole if isinstance(whole, dict) else {}
        except ValueError:
            pass
    return result, plan


def plan_of(info: Any) -> dict[str, Any] | None:
    windows: dict[str, Any] = {}
    for key, w in ((info or {}).get("unifiedWindows") or {}).items() if isinstance(info, dict) else ():
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            windows[WINDOW_NAMES.get(key, key)] = {"used": round(float(w["utilization"]), 6),
                                                   "resets_at": w.get("resetsAt")}
    return {"windows": windows, "source": "claude"} if windows else None
