"""Pricing snapshots and conservative cost estimates (spec §3.1, §3.3).

Prices are *never* hard-coded: they come from ``GET /models`` (stored as a snapshot in SQLite).
Estimates are deliberately pessimistic and only used for budget pre-checks and dry runs; final
numbers come from the API's reported ``usage.cost`` or ``/generation`` ``total_cost``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..errors import FatalAPIError
from ..memory.db import utcnow

# Rough token model for CJK-heavy text. 1 token per CJK char is pessimistic for Gemini-class
# tokenizers; ASCII ~4 chars/token.
OUTPUT_SAFETY_FACTOR = 2.0  # covers reasoning tokens / verbose JSON


@dataclass(frozen=True)
class Pricing:
    model: str
    prompt: float  # USD per token
    completion: float
    reasoning: float
    cache_read: float | None
    context_length: int | None
    supported_parameters: tuple[str, ...]
    reasoning_efforts: tuple[str, ...] = ()
    reasoning_default: str | None = None

    @classmethod
    def from_model_json(cls, m: dict[str, Any]) -> Pricing:
        p = m.get("pricing") or {}

        def f(key: str) -> float | None:
            v = p.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        prompt = f("prompt")
        completion = f("completion")
        if prompt is None or completion is None or prompt < 0 or completion < 0:
            # OpenRouter uses -1 for "variable" pricing (routers). We refuse to guess.
            raise FatalAPIError(f"Model {m.get('id')} has no fixed per-token pricing; choose an explicit model")
        r = m.get("reasoning") or {}
        return cls(m["id"], prompt, completion, f("internal_reasoning") or completion, f("input_cache_read"),
                   m.get("context_length"), tuple(m.get("supported_parameters") or ()),
                   tuple(r.get("supported_efforts") or ()), r.get("default_effort"))

    def per_million(self) -> tuple[float, float]:
        return self.prompt * 1e6, self.completion * 1e6

    def cost(self, prompt_tokens: int, completion_tokens: int, reasoning_tokens: int = 0) -> float:
        # OpenRouter's completion_tokens already include reasoning tokens.
        return prompt_tokens * self.prompt + completion_tokens * self.completion

    @property
    def structured_mode(self) -> str:
        if "structured_outputs" in self.supported_parameters:
            return "json_schema"
        if "response_format" in self.supported_parameters:
            return "json_object"
        return "prompt_only"


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
    other = len(text) - cjk
    return cjk + other // 4 + 1


def estimate_request_cost(pricing: Pricing, prompt_text: str, expected_output_chars: int) -> tuple[int, int, float]:
    pin = estimate_tokens(prompt_text)
    pout = int(expected_output_chars * 1.1 * OUTPUT_SAFETY_FACTOR) + 200
    return pin, pout, pricing.cost(pin, pout)


def save_snapshot(conn: sqlite3.Connection, models: list[dict[str, Any]], source: str) -> None:
    slim = [{k: m.get(k) for k in ("id", "name", "pricing", "context_length", "supported_parameters",
                                    "canonical_slug", "reasoning")} for m in models]
    conn.execute("INSERT INTO model_snapshots(fetched_at, source, models_json) VALUES (?,?,?)",
                 (utcnow(), source, json.dumps(slim, ensure_ascii=False)))


def latest_snapshot(conn: sqlite3.Connection) -> tuple[str, list[dict[str, Any]]] | None:
    row = conn.execute("SELECT fetched_at, models_json FROM model_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    return row["fetched_at"], json.loads(row["models_json"])


def find_model(models: list[dict[str, Any]], model_id: str) -> dict[str, Any] | None:
    for m in models:
        if m.get("id") == model_id:
            return m
    return None
