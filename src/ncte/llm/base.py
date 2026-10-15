"""Provider-neutral LLM interface. Real (OpenRouter) and fake clients implement :class:`LLMClient`."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

StructuredMode = Literal["json_schema", "json_object", "prompt_only"]


@dataclass
class LLMRequest:
    stage: str  # translate | scene_analyze | memory_update | review | repair
    model: str
    messages: list[dict[str, str]]
    schema_name: str
    json_schema: dict[str, Any] | None = None
    structured_mode: StructuredMode = "json_schema"
    temperature: float | None = None
    max_tokens: int | None = None
    prompt_version: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    id_codes: dict[str, str] = field(default_factory=dict)  # short code → real id (never sent; for offline fakes)
    cached_prefix_chars: int = 0  # the system prompt's first N characters are the same on every request (cacheable)

    def request_hash(self) -> str:
        blob = json.dumps({"model": self.model, "messages": self.messages, "schema": self.json_schema,
                           "mode": self.structured_mode, "temperature": self.temperature,
                           "max_tokens": self.max_tokens, "extra": self.extra},
                          sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class LLMResponse:
    content: str
    model: str
    provider: str | None = None
    generation_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None  # as reported by the API, None if not reported
    latency_ms: int = 0
    finish_reason: str | None = None
    attempts: int = 1
    structured_mode: StructuredMode = "json_schema"
    maybe_billed_attempts: int = 0  # earlier attempts (timeouts…) that may have been billed too


class LLMClient(Protocol):
    name: str  # "openrouter" | "fal" | "fake"

    def complete(self, req: LLMRequest) -> LLMResponse: ...

    def list_models(self) -> list[dict[str, Any]]: ...

    def get_generation(self, generation_id: str) -> dict[str, Any] | None: ...
