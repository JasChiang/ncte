"""The task queue between the engine and an agent that answers for it (provider ``agent``, ADR-062).

The run does not call a model. Every request it would send becomes a task file; the agent (Claude Code, Codex, any
agent with the skill or the MCP server) takes the next task, answers it as the model, and submits the answer.
The engine waits for the answer, checks it the way it checks any model answer, and goes on.

Layout under ``<workspace>/agent/``::

    ids/<id>            the id taken (created exclusively, so two requests at once never share one)
    tasks/<id>.json     what the engine asks: stage, instructions (system prompt), input (the request's JSON)
    claims/<id>         taken by an agent (a claim older than ``RECLAIM_SECONDS`` is free again)
    answers/<id>.json   the agent's answer, checked against the stage's schema before it is written
    state.json          the run this workspace is serving
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RECLAIM_SECONDS = 1800
_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.S)


def _write(path: Path, data: str) -> None:
    """Atomic: a reader never sees half a file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def strip_fence(text: str) -> str:
    m = _FENCE.match(text)
    return m.group(1) if m else text.strip()


@dataclass
class Task:
    id: str
    stage: str
    instructions: str
    input: str
    schema: dict[str, Any] | None
    model: str
    id_codes: dict[str, str]
    created: float
    effort: str | None = None  # the step's reasoning level (ncte's per-step setting), None = the model's default

    def for_agent(self) -> dict[str, Any]:
        """What the agent needs to answer (no internal ids)."""
        return {"task_id": self.id, "stage": self.stage, "effort": self.effort, "instructions": self.instructions,
                "input": self.input, "output_schema": self.schema}


class TaskQueue:
    def __init__(self, workspace: str | Path):
        self.root = Path(workspace).expanduser().resolve() / "agent"
        for d in ("ids", "tasks", "claims", "answers", "work"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ engine side
    def put(self, *, stage: str, model: str, messages: list[dict[str, str]], schema: dict[str, Any] | None,
            id_codes: dict[str, str], effort: str | None = None) -> str:
        tid = self._new_id()
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        user = "\n".join(m["content"] for m in messages if m["role"] == "user")
        _write(self.root / "tasks" / f"{tid}.json", json.dumps(
            {"id": tid, "stage": stage, "model": model, "instructions": system, "input": user, "schema": schema,
             "id_codes": id_codes, "created": time.time(), "effort": effort}, ensure_ascii=False))
        return tid

    def _new_id(self) -> str:
        """The engine sends several requests at once: an id is taken by creating ``ids/<id>`` exclusively."""
        n = len(list((self.root / "tasks").glob("*.json"))) + 1
        while True:
            tid = f"t{n:05d}"
            if not (self.root / "tasks" / f"{tid}.json").exists():
                try:
                    os.close(os.open(self.root / "ids" / tid, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                    return tid
                except FileExistsError:
                    pass
            n += 1

    def wait(self, tid: str, *, poll: float = 0.5, timeout: float | None = None,
             stop: Any = None) -> str:
        """The answer's text once submitted. ``stop()`` returning true ends the wait (the run is being stopped)."""
        path = self.root / "answers" / f"{tid}.json"
        start = time.monotonic()
        while not path.exists():
            if stop is not None and stop():
                raise InterruptedError(f"stopped while waiting for {tid}")
            if timeout is not None and time.monotonic() - start > timeout:
                raise TimeoutError(f"no answer to {tid} within {timeout:.0f}s")
            time.sleep(poll)
        return str(json.loads(path.read_text(encoding="utf-8"))["text"])

    # ------------------------------------------------------------ agent side
    def load(self, tid: str) -> Task:
        d = json.loads((self.root / "tasks" / f"{tid}.json").read_text(encoding="utf-8"))
        return Task(d["id"], d["stage"], d["instructions"], d["input"], d.get("schema"), d.get("model", ""),
                    d.get("id_codes") or {}, d.get("created", 0.0), d.get("effort"))

    def open_tasks(self) -> list[str]:
        """Tasks with no answer yet, oldest first."""
        answered = {p.stem for p in (self.root / "answers").glob("*.json")}
        return sorted(p.stem for p in (self.root / "tasks").glob("*.json") if p.stem not in answered)

    def claim_next(self, *, now: float | None = None) -> Task | None:
        now = time.time() if now is None else now
        for tid in self.open_tasks():
            claim = self.root / "claims" / tid
            try:
                fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(now).encode())
                os.close(fd)
            except FileExistsError:
                try:
                    taken = float(claim.read_text() or 0)
                except (OSError, ValueError):
                    taken = 0.0
                if now - taken < RECLAIM_SECONDS:
                    continue
                claim.write_text(str(now))  # an abandoned claim
            return self.load(tid)
        return None

    def submit(self, tid: str, text: str) -> list[str]:
        """Check the answer against the stage's schema; write it only when it passes. Returns the problems found
        (empty: accepted). The engine checks it again (unit ids, counts) and may send a corrected task."""
        from ..llm.schemas import SCHEMAS

        if not (self.root / "tasks" / f"{tid}.json").exists():
            return [f"no task {tid}"]
        if (self.root / "answers" / f"{tid}.json").exists():
            return [f"{tid} is already answered"]
        task = self.load(tid)
        body = strip_fence(text)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            return [f"not valid JSON: {exc}"]
        model = SCHEMAS.get(task.stage.split(":")[0])
        if model is not None:
            try:
                model.model_validate(data)
            except Exception as exc:  # pydantic.ValidationError: shown to the agent to correct
                return [str(exc)[:2000]]
        _write(self.root / "answers" / f"{tid}.json", json.dumps({"text": body, "at": time.time()}, ensure_ascii=False))
        (self.root / "claims" / tid).unlink(missing_ok=True)
        return []

    # ------------------------------------------------------------ run state
    def state(self) -> dict[str, Any]:
        p = self.root / "state.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def set_state(self, **kw: Any) -> None:
        _write(self.root / "state.json", json.dumps({**self.state(), **kw}, ensure_ascii=False))

    def counts(self) -> dict[str, int]:
        tasks = len(list((self.root / "tasks").glob("*.json")))
        answered = len(list((self.root / "answers").glob("*.json")))
        return {"tasks": tasks, "answered": answered, "open": tasks - answered}
