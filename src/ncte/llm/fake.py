"""Deterministic offline LLM used by tests, ``--fake`` runs and evaluation dry-runs.

Its "translation" is NOT a translation: kana are replaced by 〇-like filler and glossary terms are
substituted, so pipelines/QA/EPUB reassembly can be exercised end-to-end without a network or
API key. Results produced with FakeLLM are always labelled ``client=fake`` in reports.

Fault injection (``faults={"translate": ["bad_json", "retryable", ...]}``) consumes one entry per
call for that stage:
  retryable | timeout | fatal | rejected | bad_json | fenced | missing_id | extra_id | dup_id |
  bad_placeholder | empty | untranslated | ok
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from typing import Any

from ..errors import ContentBlockedError, FatalAPIError, RequestRejectedError, RetryableAPIError
from .base import LLMRequest, LLMResponse
from .cost import estimate_tokens

KANA = re.compile(r"[぀-ヿｦ-ﾟ]")
RUBY = re.compile(r"《[^》]*》")
FAKE_PROMPT_PRICE = 0.000001
FAKE_COMPLETION_PRICE = 0.000002

Responder = Callable[[dict[str, Any], LLMRequest], dict[str, Any]]


def fake_translate_text(src: str, glossary: list[dict[str, Any]]) -> str:
    out = RUBY.sub("", src)
    for g in sorted(glossary, key=lambda g: -len(g.get("ja", ""))):
        if g.get("ja"):
            out = out.replace(g["ja"], g.get("zh", g["ja"]))
    return KANA.sub("文", out)


def default_translate(payload: dict[str, Any], req: LLMRequest) -> dict[str, Any]:
    gl = payload.get("glossary", [])
    return {"translations": [
        {"unit_id": u["unit_id"], "translated_text": fake_translate_text(u["source"], gl), "flags": [], "notes": [],
         "translator_note": ""}
        for u in payload.get("units", [])]}


def default_scene_analyze(payload: dict[str, Any], req: LLMRequest) -> dict[str, Any]:
    utts = []
    for q in payload.get("quotes", []):
        ps = q.get("program_speaker")
        utts.append({"unit_id": q["unit_id"], "quote_index": q["quote_index"],
                     "speaker_candidates": ([{"character_ref": ps, "evidence_types": ["dialogue_tag"],
                                              "evidence_unit_ids": [q["unit_id"]], "note": ""}] if ps else []),
                     "resolution_status": "explicit" if ps else "unknown", "speaker_ref": ps,
                     "contradictions": []})
    return {"pov": None, "location": None, "time": None, "new_characters": [], "participants": [],
            "utterances": utts, "actions": [], "props": [], "uncertainties": []}


def default_memory_update(payload: dict[str, Any], req: LLMRequest) -> dict[str, Any]:
    n = len(payload.get("units", []))
    return {"scene_summary": f"（FakeLLM 摘要）本場景共 {n} 個單元。", "facts": [], "glossary_candidates": [],
            "prop_states": [], "name_links": [], "address_terms": [], "wordplay": []}


def default_naming(payload: dict[str, Any], req: LLMRequest) -> dict[str, Any]:
    return {"names": [], "address": [], "characters": []}


def default_review(payload: dict[str, Any], req: LLMRequest) -> dict[str, Any]:
    return {"issues": []}


def default_verify(payload: dict[str, Any], req: Any) -> dict[str, Any]:
    """Confirms nothing and changes nothing (a second look with no opinion)."""
    return {"items": []}


DEFAULTS: dict[str, Responder] = {
    "translate": default_translate,
    "scene_analyze": default_scene_analyze,
    "memory_update": default_memory_update,
    "naming": default_naming,
    "review": default_review,
    "verify": default_verify,
}


class FakeLLM:
    name = "fake"

    def __init__(self, *, models: list[str] | None = None, responders: dict[str, Responder] | None = None,
                 faults: dict[str, list[str]] | None = None, structured: bool = True,
                 blocked_terms: list[str] | None = None, temperature: bool = True):
        self.temperature = temperature
        self.models = models or ["google/gemini-3.8-flash", "fake/model"]
        self.responders = {**DEFAULTS, **(responders or {})}
        self.faults = {k: list(v) for k, v in (faults or {}).items()}
        self.structured = structured
        self.blocked_terms = blocked_terms or []
        self.calls: list[LLMRequest] = []
        self.payloads: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._n = 0

    def list_models(self) -> list[dict[str, Any]]:
        params = ["max_tokens", "response_format", "reasoning"] + (["temperature"] if self.temperature else [])
        if self.structured:
            params.append("structured_outputs")
        return [{"id": m, "name": f"Fake {m}", "context_length": 1_000_000, "supported_parameters": params,
                 "reasoning": {"mandatory": True, "supported_efforts": ["high", "medium", "low"],
                               "default_effort": "medium"},
                 "pricing": {"prompt": str(FAKE_PROMPT_PRICE), "completion": str(FAKE_COMPLETION_PRICE)}}
                for m in self.models]

    def get_generation(self, generation_id: str) -> dict[str, Any] | None:
        return None

    def _fault(self, stage: str) -> str:
        with self._lock:
            q = self.faults.get(stage) or self.faults.get("*")
            return q.pop(0) if q else "ok"

    def complete(self, req: LLMRequest) -> LLMResponse:
        stage = req.stage if req.stage in self.responders else req.stage.split(":")[0]
        payload = json.loads(req.messages[-1]["content"])
        with self._lock:
            self.calls.append(req)
            self.payloads.append(payload)
            self._n += 1
            n = self._n
        if req.model not in self.models:
            raise FatalAPIError(f"HTTP 404 (model or endpoint not found): {req.model}")
        if any(t in req.messages[-1]["content"] for t in self.blocked_terms):
            raise ContentBlockedError("HTTP 403 content filter: fake PROHIBITED_CONTENT")
        fault = self._fault(stage)
        if fault == "retryable":
            raise RetryableAPIError("HTTP 429: fake rate limit")
        if fault == "timeout":
            err = RetryableAPIError("fake timeout")
            err.possibly_billed = True  # type: ignore[attr-defined]
            raise err
        if fault == "fatal":
            raise FatalAPIError("HTTP 402 (insufficient credits): fake")
        if fault == "rejected":
            raise RequestRejectedError("HTTP 400: fake rejection")
        data = self.responders[stage](payload, req)
        if stage == "translate" and data.get("translations"):
            tr = data["translations"]
            if fault == "missing_id":
                tr.pop()
            elif fault == "extra_id":
                tr.append({"unit_id": "u_bogus", "translated_text": "多出來的", "flags": [], "notes": []})
            elif fault == "dup_id":
                tr.append(dict(tr[0]))
            elif fault == "bad_placeholder":
                tr[0]["translated_text"] = re.sub(r"⟦/?\d+/?⟧", "", tr[0]["translated_text"]) + "⟦99⟧"
            elif fault == "empty":
                tr[0]["translated_text"] = ""
            elif fault == "untranslated":
                src = {u["unit_id"]: u["source"] for u in payload["units"]}
                tr[0]["translated_text"] = src[tr[0]["unit_id"]]
        content = json.dumps(data, ensure_ascii=False)
        if fault == "bad_json":
            content = content[: len(content) // 2]
        elif fault == "fenced":
            content = f"```json\n{content}\n```"
        pin = sum(estimate_tokens(m["content"]) for m in req.messages)
        pout = estimate_tokens(content)
        cost = pin * FAKE_PROMPT_PRICE + pout * FAKE_COMPLETION_PRICE
        return LLMResponse(content=content, model=req.model, provider="fake", generation_id=f"gen-fake-{n}",
                           usage={"prompt_tokens": pin, "completion_tokens": pout, "total_tokens": pin + pout,
                                  "cost": cost}, cost_usd=cost, latency_ms=1, finish_reason="stop",
                           structured_mode=req.structured_mode)
