"""The user's ChatGPT plan as the model: each request is one fresh `codex exec` call (OpenAI Codex CLI, non-interactive).

Built like the Claude plan's client (``claude_cli``). The step's fixed rules replace Codex's own base instructions
(``-c model_instructions_file=…``); the batch's conditional rules, a retry's correction and the request's JSON come on
stdin; the step's JSON schema goes as ``--output-schema`` and the answer is read from the last-message file (``-o``).
The user's Codex settings and rules are not read (``--ignore-user-config``,
``--ignore-rules``), the sandbox is read-only and each call runs in its own empty temporary directory.

Codex sends its own tool descriptions with every request: measured 2026-10-20 (codex-cli 0.162.1), a one-sentence
request carried about 23,000 input tokens. Replacing the base instructions and turning off (``--disable``) every
feature ``codex features list`` shows as enabled brought it to about 10,400, and two calls in a row read nothing from
a cache. The feature list is asked once per client; if that fails nothing is turned off (one line in the log).

Nothing is billed through NCTE: ``cost_usd`` is 0. Codex reports token counts but no price, so ``list_cost_usd`` is 0
too (there is no list price to convert with); the token counts are kept in ``usage``. How much of the plan is used
comes from the session file Codex writes for the call (``~/.codex/sessions/…/rollout-…-<thread id>.jsonl``, written
because the call is not ``--ephemeral``): its last ``rate_limits`` goes to ``usage["plan"]`` (``{"windows": {name:
{"used": 0–1, "resets_at": epoch}}, "source": "codex"}``) and the file is removed (only that thread's). A missing or
unreadable file leaves ``plan`` out and the answer stands.

Failures follow the Claude plan's waits (``claude_cli``): a usage limit is asked again at a fixed interval, anything
else goes down the same ladder; no login, or a command line Codex rejects, stops at once. What a failure looks like
(the JSONL events, stderr) was not seen yet: a call counts as failed on a non-zero exit, a missing ``turn.completed``
or any event whose type says error or failed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..errors import FatalAPIError, ModelOutputError, RetryableAPIError
from .base import LLMRequest, LLMResponse
from .claude_cli import EFFORTS, LIMIT_MAX, LIMIT_WAIT, RETRY_WAITS, ClaudeCLIClient, effort_of

PREFIX = "codex/"
# used when Codex's model cache cannot be read (the account's models as of 2026-10-20)
BUILT_IN_MODELS = ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna")
MODELS_CACHE = Path("~/.codex/models_cache.json")
SESSIONS = Path("~/.codex/sessions")
# the words of a usage limit are not known yet (never seen); these are guesses, and any other failure waits too
USAGE_LIMIT = ("usage limit", "rate limit", "limit reached", "quota", "try again at", "resets ", "429")
NOT_LOGGED_IN = ("not logged in", "login", "unauthorized", "401")
# a command line Codex rejects (clap: "unexpected argument"; a feature or config value it does not know) is the same
# on every try
BAD_COMMAND = ("unexpected argument", "unknown option", "unknown argument", "unknown flag", "unknown feature",
               "unknown variant", "unknown field", "unrecognized")
USAGE_ERROR_EXIT = 2
FEATURES_TIMEOUT = 30


def parse_features(text: str) -> list[str]:
    """The features ``codex features list`` shows as enabled and not removed (each line: name, stage, true/false;
    a stage may be more than one word)."""
    out: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[-1] not in ("true", "false"):
            continue
        stage = " ".join(parts[1:-1]).lower()
        if parts[-1] == "true" and stage != "removed":
            out.append(parts[0])
    return out


def window_name(minutes: Any) -> str:
    """A rate-limit window by its length: 10080 minutes is "weekly", 1440 "daily", whole hours "5h", else "90m"."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return "window"
    return {10080: "weekly", 1440: "daily"}.get(m) or (f"{m // 60}h" if m % 60 == 0 else f"{m}m")


def plan_usage(limits: dict[str, Any]) -> dict[str, Any] | None:
    """``rate_limits`` from a session file → ``usage["plan"]``; None when it says nothing usable."""
    windows: dict[str, Any] = {}
    for key in ("primary", "secondary"):
        w = limits.get(key)
        if not isinstance(w, dict) or not isinstance(w.get("used_percent"), (int, float)):
            continue
        windows[window_name(w.get("window_minutes"))] = {"used": round(float(w["used_percent"]) / 100, 6),
                                                         "resets_at": w.get("resets_at")}
    return {"windows": windows, "source": "codex"} if windows else None


def _last_rate_limits(text: str) -> dict[str, Any] | None:
    found: dict[str, Any] | None = None

    def walk(x: Any) -> None:
        nonlocal found
        if isinstance(x, dict):
            if isinstance(x.get("rate_limits"), dict):
                found = x["rate_limits"]
            for v in x.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for line in text.splitlines():
        try:
            walk(json.loads(line))
        except ValueError:
            continue
    return found


def _toml(value: str) -> str:
    """A TOML basic string (``-c key=value`` takes TOML)."""
    return json.dumps(value, ensure_ascii=False)


class CodexCLIClient:
    name = "codex"
    catalog_source = "codex"

    def __init__(self, *, models: list[str] | None = None, timeout: float = 1800,
                 waits: tuple[float, ...] = RETRY_WAITS, binary: str | None = None, sleep: Any = time.sleep,
                 log: Callable[[str], None] | None = None, limit_wait: float = LIMIT_WAIT,
                 limit_max: float = LIMIT_MAX, models_cache: str | Path | None = None,
                 sessions: str | Path | None = None):
        self.extra_models = [m for m in models or [] if m and m.startswith(PREFIX)]
        self.log = log  # told before each wait and when the feature list cannot be read; None: silent
        self.timeout = timeout
        self.waits = waits
        self.limit_wait, self.limit_max = limit_wait, limit_max
        self.binary = binary or os.environ.get("NCTE_CODEX_BIN") or "codex"
        self.sleep = sleep
        self.models_cache = Path(models_cache or os.environ.get("NCTE_CODEX_MODELS_CACHE") or MODELS_CACHE).expanduser()
        self.sessions = Path(sessions or os.environ.get("NCTE_CODEX_SESSIONS") or SESSIONS).expanduser()
        self._features: list[str] | None = None  # asked on the first request (not when the client is only built)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ catalog
    def _cached_models(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.models_cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        models = data.get("models") if isinstance(data, dict) else data
        return [m for m in models or [] if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list"]

    def _efforts(self) -> dict[str, list[str]]:
        """slug → the reasoning levels Codex says the model takes (from its cache)."""
        out: dict[str, list[str]] = {}
        for m in self._cached_models():
            levels = [str(x.get("effort")) for x in m.get("supported_reasoning_levels") or [] if isinstance(x, dict)]
            out[str(m["slug"])] = [e for e in levels if e and e != "None"]
        return out

    def list_models(self) -> list[dict[str, Any]]:
        """The models Codex lists for this account (its model cache; a small built-in list when it cannot be read),
        and any configured ``codex/<id>``. Prices are 0: nothing is billed here."""
        cached = self._cached_models()
        rows: dict[str, dict[str, Any]] = {}
        for m in cached or [{"slug": s} for s in BUILT_IN_MODELS]:
            rows[PREFIX + str(m["slug"])] = m
        for extra in self.extra_models:
            rows.setdefault(extra, {"slug": extra[len(PREFIX):]})
        out = []
        for mid, m in rows.items():
            levels = [str(x.get("effort")) for x in m.get("supported_reasoning_levels") or [] if isinstance(x, dict)]
            out.append({"id": mid, "name": f"{m.get('display_name') or m['slug']} through Codex",
                        "context_length": int(m.get("context_window") or 272_000),
                        "supported_parameters": ["max_tokens", "response_format", "structured_outputs", "reasoning"],
                        "reasoning": {"mandatory": False, "supported_efforts": levels or list(EFFORTS),
                                      "default_effort": m.get("default_reasoning_level") or "medium"},
                        "pricing": {"prompt": "0", "completion": "0"}})
        return out

    def get_generation(self, generation_id: str) -> dict[str, Any] | None:
        return None

    def balance(self) -> dict[str, Any]:
        return {}

    # ------------------------------------------------------------ requests
    def features(self) -> list[str]:
        """The features to turn off on every request: the enabled ones, asked once."""
        with self._lock:
            if self._features is None:
                try:
                    proc = subprocess.run([self.binary, "features", "list"], capture_output=True, text=True,
                                          timeout=FEATURES_TIMEOUT)
                    if proc.returncode != 0:
                        raise RuntimeError((proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:200])
                    self._features = parse_features(proc.stdout or "")
                except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                    self._features = []
                    if self.log:
                        self.log(f"Codex：讀不到 `codex features list`，不關閉任何功能（每個請求的固定負擔較大）：{exc}")
            return self._features

    def args(self, req: LLMRequest, instructions: Path, schema: Path | None, last: Path) -> list[str]:
        model = req.model[len(PREFIX):] if req.model.startswith(PREFIX) else req.model
        out = [self.binary, "exec", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
               "-s", "read-only"]
        eff = effort_of(req)
        takes = self._efforts().get(model)
        if eff and (takes is None or eff in takes):  # a level the model does not take is not sent
            out += ["-c", f"model_reasoning_effort={_toml(eff)}"]
        out += ["-c", f"model_instructions_file={_toml(str(instructions))}"]
        for feature in self.features():
            out += ["--disable", feature]
        if schema is not None:
            out += ["--output-schema", str(schema)]
        return out + ["--json", "-o", str(last), "-m", model, "-"]

    split = staticmethod(ClaudeCLIClient.split)  # fixed rules → instructions; the batch's own rules → stdin first

    @staticmethod
    def read_events(text: str) -> tuple[dict[str, Any] | None, str | None, list[str]]:
        """(``turn.completed``'s usage or None, the thread id, the messages of error/failed events)."""
        usage: dict[str, Any] | None = None
        thread: str | None = None
        errors: list[str] = []
        for line in text.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = str(ev.get("type") or "")
            if kind == "thread.started":
                thread = ev.get("thread_id") or thread
            elif kind == "turn.completed":
                usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            if "error" in kind or "failed" in kind:
                err = ev.get("error")
                msg = err.get("message") if isinstance(err, dict) else err
                errors.append(str(msg or ev.get("message") or line)[:500])
        return usage, thread, errors

    def session_files(self, thread: str) -> list[Path]:
        """The session files of this thread (Codex names them ``rollout-<time>-<thread id>.jsonl``, by day)."""
        if not thread or not self.sessions.is_dir():
            return []
        pattern = f"rollout-*{thread}*.jsonl"
        try:
            now = time.time()
            days = {time.strftime("%Y/%m/%d", t(now + d)) for d in (-86400, 0, 86400) for t in (time.localtime, time.gmtime)}
            near = [p for d in sorted(days) for p in (self.sessions / d).glob(pattern) if p.is_file()]
            return sorted(near) or sorted(p for p in self.sessions.rglob(pattern) if p.is_file())  # today first
        except OSError:
            return []

    def take_plan(self, thread: str | None) -> dict[str, Any] | None:
        """The plan's usage from this thread's session file (its last ``rate_limits``); the file is removed."""
        plan = None
        for path in self.session_files(thread or ""):
            try:
                limits = _last_rate_limits(path.read_text(encoding="utf-8", errors="replace"))
                plan = (plan_usage(limits) if limits else None) or plan
            except OSError:
                pass
            path.unlink(missing_ok=True)
        return plan

    def complete(self, req: LLMRequest) -> LLMResponse:
        system, user = self.split(req)
        start = time.monotonic()
        last = ""
        ladder = iter(self.waits)
        limit_waited = 0.0
        attempt = 0

        def later(limit: bool) -> None:
            nonlocal limit_waited
            if limit:
                limit_waited += self.limit_wait
                if limit_waited > self.limit_max:
                    raise RetryableAPIError(f"Codex's usage limit did not lift in {self.limit_max / 3600:.0f} h: {last[:300]}")
                wait = self.limit_wait
            else:
                nxt = next(ladder, None)
                if nxt is None:
                    raise RetryableAPIError(f"Codex kept failing: {last[:300]}")
                wait = nxt
            if self.log:
                self.log(f"Codex：等待 {wait:.0f} 秒後重試（第 {attempt + 1} 次）：{last[:200]}")
            self.sleep(wait)

        with tempfile.TemporaryDirectory(prefix="ncte-codex-") as tmp:
            base = Path(tmp)
            work = base / "work"  # where codex runs: empty
            work.mkdir()
            instructions = base / "instructions.md"
            instructions.write_text(system, encoding="utf-8")
            schema = None
            if req.json_schema:
                schema = base / "schema.json"
                schema.write_text(json.dumps(req.json_schema, ensure_ascii=False), encoding="utf-8")
            answer = base / "last_message.txt"
            while True:
                attempt += 1
                answer.unlink(missing_ok=True)
                try:
                    proc = subprocess.run(self.args(req, instructions, schema, answer), input=user,
                                          capture_output=True, text=True, timeout=self.timeout, cwd=str(work))
                except subprocess.TimeoutExpired:
                    last = f"no answer within {self.timeout:.0f}s"
                    later(False)
                    continue
                except FileNotFoundError as exc:
                    raise FatalAPIError(f"Codex (`{self.binary}`) is not installed or not on PATH") from exc
                stderr = (proc.stderr or "").strip()
                usage_in, thread, errors = self.read_events(proc.stdout or "")
                try:
                    plan = self.take_plan(thread)  # also for a failed try: its session file is not left behind
                except Exception:  # noqa: BLE001 — never lets the request fail
                    plan = None
                failed = proc.returncode != 0 or usage_in is None or bool(errors)
                if failed and (proc.returncode == USAGE_ERROR_EXIT or any(s in stderr.lower() for s in BAD_COMMAND)):
                    raise FatalAPIError(f"Codex rejected the command (exit {proc.returncode}): {stderr[:300]}")
                said = "\n".join([*errors, stderr]).strip()
                if failed and any(s in said.lower() for s in NOT_LOGGED_IN):  # never in a successful answer
                    raise FatalAPIError(f"Codex is not logged in (run `codex login`): {said[:300]}")
                if failed:
                    last = said[:500] or f"codex exited {proc.returncode} without finishing the turn"
                    later(any(s in said.lower() for s in USAGE_LIMIT))
                    continue
                try:
                    result = answer.read_text(encoding="utf-8")
                except OSError:
                    result = ""
                if not result.strip():
                    raise ModelOutputError("empty answer")
                u = usage_in or {}
                usage: dict[str, Any] = {
                    "prompt_tokens": int(u.get("input_tokens") or 0),
                    "completion_tokens": int(u.get("output_tokens") or 0),
                    "reasoning_tokens": int(u.get("reasoning_output_tokens") or 0),
                    "cache_read_tokens": int(u.get("cached_input_tokens") or 0),
                    "cache_write_tokens": int(u.get("cache_write_input_tokens") or 0),
                    "list_cost_usd": 0, "effort": effort_of(req)}
                if plan:
                    usage["plan"] = plan
                return LLMResponse(content=result.strip(), model=req.model, provider="codex", generation_id=thread,
                                   usage=usage, cost_usd=0.0, latency_ms=int((time.monotonic() - start) * 1000),
                                   finish_reason="stop", attempts=attempt, structured_mode=req.structured_mode)


def found() -> str | None:
    """Where the `codex` command is (for the settings page), or None."""
    return shutil.which(os.environ.get("NCTE_CODEX_BIN") or "codex")
