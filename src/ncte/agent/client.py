"""The client whose "model" is an agent answering tasks (provider ``agent``): nothing is billed through NCTE."""

from __future__ import annotations

import time
from typing import Any

from ..llm.base import LLMRequest, LLMResponse
from ..llm.claude_cli import EFFORTS, effort_of
from .tasks import TaskQueue

MODEL_ID = "agent/model"


class AgentClient:
    """``complete`` writes the request as a task (with the step's reasoning level) and waits for the agent's answer.
    The cost the engine records is 0; the agent's own usage is whatever its host counts."""

    name = "agent"
    catalog_source = "agent"

    def __init__(self, queue: TaskQueue, *, model_id: str = MODEL_ID, timeout: float | None = None,
                 stop: Any = None):
        self.queue = queue
        self.model_id = model_id
        self.timeout = timeout
        self.stop = stop

    def list_models(self) -> list[dict[str, Any]]:
        return [{"id": self.model_id, "name": "The agent answering the tasks", "context_length": 1_000_000,
                 "supported_parameters": ["max_tokens", "response_format", "structured_outputs", "reasoning"],
                 "reasoning": {"mandatory": False, "supported_efforts": list(EFFORTS), "default_effort": "medium"},
                 "pricing": {"prompt": "0", "completion": "0"}}]

    def get_generation(self, generation_id: str) -> dict[str, Any] | None:
        return None

    def balance(self) -> dict[str, Any]:
        return {}

    def complete(self, req: LLMRequest) -> LLMResponse:
        start = time.monotonic()
        tid = self.queue.put(stage=req.stage, model=req.model, messages=req.messages, schema=req.json_schema,
                             id_codes=req.id_codes, effort=effort_of(req))
        text = self.queue.wait(tid, timeout=self.timeout, stop=self.stop)
        return LLMResponse(content=text, model=req.model, provider="agent", generation_id=tid, usage={},
                           cost_usd=0.0, latency_ms=int((time.monotonic() - start) * 1000),
                           structured_mode=req.structured_mode)
