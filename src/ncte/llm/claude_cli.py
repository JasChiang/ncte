"""The user's Claude plan as the model: each request is one fresh `claude -p` call (Claude Code in print mode).

The step's prompt becomes the system prompt (Claude Code's own is replaced; no tools, no settings, no MCP, no session
kept), the request's JSON is the user message: as close to a plain model call as the CLI allows, on the user's own
Claude login. Nothing is billed through NCTE: ``cost_usd`` is 0. What the CLI reports as the call's list-price cost
is kept in ``usage["list_cost_usd"]`` (with the token counts) so a run can say what it used.

A failed call (a usage limit reached, the network) is tried again later: a plan's usage window resets in hours, so
the waits grow to an hour; the run just waits meanwhile, and says so before each wait (``log``). A call that can never
work (Claude Code rejects its own command line: exit 2, an unknown option) or no login stops at once.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from typing import Any

from ..errors import FatalAPIError, ModelOutputError, RetryableAPIError
from .base import LLMRequest, LLMResponse

PREFIX = "claude/"
MODELS = ("opus", "sonnet", "haiku")  # Claude Code's aliases; a full model id after the prefix is passed as is
EFFORTS = ("low", "medium", "high", "xhigh", "max")
SCHEMA_HEAD = "\n\n## 輸出的 JSON schema\n"  # how to answer is said once, in the shared rules (translator.COMPACT_JSON)
RETRY_WAITS = (30, 60, 120, 300, 600, 900, 1800, 1800, 1800, 3600, 3600)  # seconds
NOT_LOGGED_IN = ("not logged in", "please run /login", "invalid api key", "authentication")
BAD_COMMAND = ("unknown option", "unknown argument", "too many arguments")  # the same on every try
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
                 log: Callable[[str], None] | None = None):
        self.extra_models = [m for m in models or [] if m and m.startswith(PREFIX)]
        self.log = log  # told before each wait (the run's own log); None: silent
        self.timeout = timeout
        self.waits = waits
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
        out = [self.binary, "-p", "--model", model, "--output-format", "json", "--tools", "", "--system-prompt", system,
               "--no-session-persistence", "--strict-mcp-config", "--setting-sources", "",
               "--exclude-dynamic-system-prompt-sections"]
        if eff := effort_of(req):
            out += ["--effort", eff]
        return out

    @staticmethod
    def split(req: LLMRequest) -> tuple[str, str]:
        """(system prompt, user message). Claude Code caches the system prompt as one block, read back only when it
        is byte-identical: it holds the rules every request of the step shares (``cached_prefix_chars``) and the step's
        JSON schema. This batch's conditional rules and a retry's correction go to the user message, before the data."""
        system = "\n".join(m["content"] for m in req.messages if m["role"] == "system")
        cut = req.cached_prefix_chars if 0 < req.cached_prefix_chars < len(system) else len(system)
        fixed, own = system[:cut], system[cut:].strip()
        if req.json_schema:
            fixed = fixed.rstrip() + SCHEMA_HEAD + json.dumps(req.json_schema, ensure_ascii=False)
        user = "\n".join(m["content"] for m in req.messages if m["role"] == "user")
        return fixed, (own + "\n\n" + user if own else user)

    def complete(self, req: LLMRequest) -> LLMResponse:
        system, user = self.split(req)
        start = time.monotonic()
        last = ""
        for attempt, wait in enumerate((0.0, *self.waits), start=1):
            if wait:
                if self.log:
                    self.log(f"Claude Code：等待 {wait:.0f} 秒後重試（第 {attempt} 次）：{last[:200]}")
                self.sleep(wait)
            try:
                proc = subprocess.run(self.args(req, system), input=user, capture_output=True, text=True,
                                      timeout=self.timeout)
            except subprocess.TimeoutExpired:
                last = f"no answer within {self.timeout:.0f}s"
                continue
            except FileNotFoundError as exc:
                raise FatalAPIError(f"Claude Code (`{self.binary}`) is not installed or not on PATH") from exc
            stderr = (proc.stderr or "").strip()
            if proc.returncode == USAGE_ERROR_EXIT or any(s in stderr.lower() for s in BAD_COMMAND):
                raise FatalAPIError(f"Claude Code rejected the command (exit {proc.returncode}): {stderr[:200]}")
            text = (proc.stdout or "").strip()
            try:
                out = json.loads(text) if text else {}
            except json.JSONDecodeError:
                out = {}
            failed = proc.returncode != 0 or not out or out.get("is_error") or out.get("subtype", "success") != "success"
            message = str(out.get("result") or stderr or text)[:500]
            if failed and any(s in message.lower() for s in NOT_LOGGED_IN):  # never in a successful answer
                raise FatalAPIError(f"Claude Code is not logged in: {message[:200]}")
            if failed:
                last = message or f"claude exited {proc.returncode}"
                continue  # a usage limit reached, an overloaded service: later
            result = str(out.get("result") or "")
            if not result.strip():
                raise ModelOutputError("empty answer")
            u = out.get("usage") or {}
            cache_write = int(u.get("cache_creation_input_tokens") or 0)
            cache_read = int(u.get("cache_read_input_tokens") or 0)
            usage = {"prompt_tokens": int(u.get("input_tokens") or 0) + cache_write + cache_read,
                     "completion_tokens": int(u.get("output_tokens") or 0),
                     "cache_write_tokens": cache_write, "cache_read_tokens": cache_read,
                     "list_cost_usd": float(out.get("total_cost_usd") or 0), "effort": effort_of(req)}
            return LLMResponse(content=result, model=req.model, provider="claude", generation_id=out.get("session_id"),
                               usage=usage, cost_usd=0.0, latency_ms=int((time.monotonic() - start) * 1000),
                               finish_reason="stop", attempts=attempt, structured_mode=req.structured_mode)
        raise RetryableAPIError(f"Claude Code kept failing: {last[:300]}")
