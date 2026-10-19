"""The user's Claude plan as the model: each request is one fresh `claude -p` call (Claude Code in print mode).

The step's prompt becomes the system prompt (Claude Code's own is replaced; no tools, no settings, no MCP, no session
kept), the request's JSON is the user message: as close to a plain model call as the CLI allows, on the user's own
Claude login. Nothing is billed through NCTE: ``cost_usd`` is 0. What the CLI reports as the call's list-price cost
is kept in ``usage["list_cost_usd"]`` (with the token counts) so a run can say what it used.

A failed call (a usage limit reached, the network) is tried again later: a plan's usage window resets in hours, so
the waits grow to an hour; the run just waits meanwhile.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

from ..errors import FatalAPIError, ModelOutputError, RetryableAPIError
from .base import LLMRequest, LLMResponse

PREFIX = "claude/"
MODELS = ("opus", "sonnet", "haiku")  # Claude Code's aliases; a full model id after the prefix is passed as is
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ANSWER_RULE = ("\n\n## 回答方式\n只輸出一個 JSON 物件（不要說明文字、不要 code fence），格式照上面的輸出說明；"
               "輸入中的代號（P1、C2…）照原樣使用。")
RETRY_WAITS = (30, 60, 120, 300, 600, 900, 1800, 1800, 1800, 3600, 3600)  # seconds
NOT_LOGGED_IN = ("not logged in", "please run /login", "invalid api key", "authentication")


def effort_of(req: LLMRequest) -> str | None:
    """The level the engine asked this step to think at; a token cap (``max:N``) has no level here."""
    eff = (req.extra.get("reasoning") or {}).get("effort")
    return str(eff) if eff in EFFORTS else None


class ClaudeCLIClient:
    name = "claude"
    catalog_source = "claude"

    def __init__(self, *, models: list[str] | None = None, timeout: float = 1800,
                 waits: tuple[float, ...] = RETRY_WAITS, binary: str | None = None, sleep: Any = time.sleep):
        self.extra_models = [m for m in models or [] if m and m.startswith(PREFIX)]
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

    def complete(self, req: LLMRequest) -> LLMResponse:
        system = "\n".join(m["content"] for m in req.messages if m["role"] == "system") + ANSWER_RULE
        user = "\n".join(m["content"] for m in req.messages if m["role"] == "user")
        if req.json_schema:
            user += "\n\n輸出必須符合這個 JSON schema：\n" + json.dumps(req.json_schema, ensure_ascii=False)
        start = time.monotonic()
        last = ""
        for attempt, wait in enumerate((0.0, *self.waits), start=1):
            if wait:
                self.sleep(wait)
            try:
                proc = subprocess.run(self.args(req, system), input=user, capture_output=True, text=True,
                                      timeout=self.timeout)
            except subprocess.TimeoutExpired:
                last = f"no answer within {self.timeout:.0f}s"
                continue
            except FileNotFoundError as exc:
                raise FatalAPIError(f"Claude Code (`{self.binary}`) is not installed or not on PATH") from exc
            text = (proc.stdout or "").strip()
            try:
                out = json.loads(text) if text else {}
            except json.JSONDecodeError:
                out = {}
            message = str(out.get("result") or proc.stderr or text)[:500]
            if any(s in message.lower() for s in NOT_LOGGED_IN):
                raise FatalAPIError(f"Claude Code is not logged in: {message[:200]}")
            if proc.returncode != 0 or not out or out.get("is_error") or out.get("subtype", "success") != "success":
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
