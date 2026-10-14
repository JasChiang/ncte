"""Batch translation with strict ID alignment and fallback splitting (spec §4.2, §6.3).

Flow for one batch:
  strict JSON parse → (local fence repair) → schema validation → exact unit-ID set check →
  per-unit program QA. Failures: one corrective retry of the failing subset, then split the
  batch in halves recursively; a single unit that still fails is reported as failed (the
  original Japanese stays in the book). A translation is never mapped to a different unit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from ..config import Config
from ..epub.units import InlineSpec
from ..errors import ContentBlockedError, ModelOutputError, RequestRejectedError
from ..llm.base import LLMClient, LLMRequest, LLMResponse, StructuredMode
from ..llm.cost import estimate_tokens
from ..llm.prompt_loader import Prompt, load_prompt
from ..llm.schemas import SCHEMAS, TranslationBatchOutput, strict_json_schema
from ..qa.rules import Finding, check_batch, check_translation

_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```\s*$", re.S)


class Recorder(Protocol):
    def start(self, req: LLMRequest, unit_ids: list[str], est_cost: float) -> int: ...

    def success(self, rid: int, resp: LLMResponse, parse_status: str) -> None: ...

    def failure(self, rid: int, exc: Exception) -> None: ...


class NullRecorder:
    def __init__(self) -> None:
        self.n = 0
        self.log: list[tuple[str, Any]] = []

    def start(self, req: LLMRequest, unit_ids: list[str], est_cost: float) -> int:
        self.n += 1
        self.log.append(("start", req.stage))
        return self.n

    def success(self, rid: int, resp: LLMResponse, parse_status: str) -> None:
        self.log.append(("success", parse_status))

    def failure(self, rid: int, exc: Exception) -> None:
        self.log.append(("failure", str(exc)))


@dataclass
class UnitInput:
    unit_id: str
    kind: str
    source_text: str
    plain_text: str
    inline_map: dict[int, InlineSpec]
    ruby_count: int = 0

    @classmethod
    def from_row(cls, r: Any) -> UnitInput:
        imap = {int(k): InlineSpec.from_json(v) for k, v in json.loads(r["inline_map_json"]).items()}
        return cls(r["id"], r["kind"], r["source_text"], r["plain_text"], imap, r["ruby_count"])


def clean_note(note: str) -> str:
    """A translator's note is plain reader-facing text: no placeholders, no brackets of its own, kept short."""
    note = re.sub(r"⟦/?\d+/?⟧", "", note or "").strip().strip("（）()")
    return note[:120]


SLIM_KEYS = ("context_before", "context_after", "glossary", "style", "known_headings", "note", "review_feedback")


@dataclass
class UnitResult:
    text: str
    flags: list[str]
    notes: list[str]
    findings: list[Finding]
    request_id: int | None
    translator_note: str = ""


@dataclass
class BatchResult:
    ok: dict[str, UnitResult] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    failure_findings: list[Finding] = field(default_factory=list)
    blocked: set[str] = field(default_factory=set)
    requests: int = 0
    structured_mode: str = ""


def parse_json_output(content: str, model: type[BaseModel]) -> tuple[BaseModel, str]:
    """Strict parse; the only local repair is removing a single Markdown code fence."""
    text = content.strip()
    status = "strict"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        m = _FENCE.match(text)
        if not m:
            raise ModelOutputError(f"invalid JSON: {exc}") from exc
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError as exc2:
            raise ModelOutputError(f"invalid JSON inside code fence: {exc2}") from exc2
        status = "repaired_fence"
    try:
        return model.model_validate(data), status
    except ValidationError as exc:
        raise ModelOutputError(f"schema validation failed: {str(exc)[:400]}") from exc


AUTO_MAX_TOKENS_FLOOR = 16384
AUTO_MAX_TOKENS_CEIL = 65536


def auto_max_tokens(user_content: str) -> int:
    """Output cap when the config sets none. Without one OpenRouter reserves credit for the model's full
    output limit (65,536 for Gemini Flash), which a key with a low spending limit cannot afford even when
    the real answer is a few thousand tokens. Output (translation + reasoning) stays well under 4× the
    payload, which already includes the context; a truncated answer is retried once with a doubled cap."""
    return max(AUTO_MAX_TOKENS_FLOOR, min(AUTO_MAX_TOKENS_CEIL, 4 * estimate_tokens(user_content)))


def call_structured(client: LLMClient, recorder: Recorder, *, stage: str, model: str, prompt: Prompt,
                    payload: dict[str, Any], mode: StructuredMode, cfg: Config, unit_ids: list[str],
                    est_cost: float, correction: str | None = None,
                    on_downgrade: Callable[[str], None] | None = None) -> tuple[BaseModel, int, StructuredMode]:
    """One logical LLM call with schema validation. Returns (parsed, request_id, mode_used)."""
    schema_model = SCHEMAS[stage.split(":")[0]]
    system = prompt.render(payload)
    if correction:
        system += f"\n\n## 重要更正\n上一次的輸出未通過程式驗證：{correction}\n請嚴格依照規則與輸出格式重新輸出。\n"
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    current: StructuredMode = mode
    max_tokens = cfg.openrouter.max_output_tokens
    auto_cap = max_tokens is None
    if auto_cap:
        max_tokens = auto_max_tokens(messages[1]["content"])
    while True:
        req = LLMRequest(stage=stage, model=model, messages=messages, schema_name=f"ncte_{stage.split(':')[0]}",
                         json_schema=strict_json_schema(schema_model) if current == "json_schema" else None,
                         structured_mode=current, temperature=cfg.openrouter.temperature,
                         max_tokens=max_tokens, prompt_version=prompt.version_id,
                         extra={"reasoning": {"effort": cfg.openrouter.reasoning_effort}}
                         if cfg.openrouter.reasoning_effort else {})
        rid = recorder.start(req, unit_ids, est_cost)
        try:
            resp = client.complete(req)
        except RequestRejectedError as exc:
            recorder.failure(rid, exc)
            # Structured-output parameters rejected → explicit, recorded downgrade (spec §3.3)
            if current == "json_schema" and ("response_format" in str(exc) or "schema" in str(exc).lower()):
                current = "json_object"
                if on_downgrade:
                    on_downgrade(f"json_schema rejected by provider; downgraded to json_object: {exc}")
                continue
            raise
        except Exception as exc:
            recorder.failure(rid, exc)
            raise
        if resp.finish_reason == "length":
            recorder.success(rid, resp, "truncated")
            if auto_cap and max_tokens is not None and max_tokens < AUTO_MAX_TOKENS_CEIL:
                max_tokens = min(AUTO_MAX_TOKENS_CEIL, max_tokens * 2)
                continue
            raise ModelOutputError("output truncated (finish_reason=length)")
        try:
            parsed, status = parse_json_output(resp.content, schema_model)
        except ModelOutputError:
            recorder.success(rid, resp, "invalid")
            raise
        recorder.success(rid, resp, status)
        return parsed, rid, current


class Translator:
    def __init__(self, client: LLMClient, cfg: Config, recorder: Recorder, *, model: str,
                 structured_mode: StructuredMode, estimate: Callable[[dict[str, Any], int], float],
                 prompt: Prompt | None = None, on_downgrade: Callable[[str], None] | None = None):
        self.client = client
        self.cfg = cfg
        self.recorder = recorder
        self.model = model
        self.mode: StructuredMode = structured_mode
        self.estimate = estimate
        self.prompt = prompt or load_prompt("translate")
        self.on_downgrade = on_downgrade

    def _downgrade(self, msg: str) -> None:
        if self.on_downgrade:
            self.on_downgrade(msg)

    def translate(self, units: list[UnitInput], context: dict[str, Any], *,
                  qa_glossary: list[dict[str, Any]] = (),  # type: ignore[assignment]
                  allowed_kana: list[str] = ()) -> BatchResult:  # type: ignore[assignment]
        result = BatchResult()
        self._run(units, context, result, qa_glossary, allowed_kana, depth=0)
        result.structured_mode = self.mode
        return result

    def _payload(self, units: list[UnitInput], context: dict[str, Any]) -> dict[str, Any]:
        context = {k: v for k, v in context.items() if not k.startswith("_")}
        return {"task": "translate", "target_language": "zh-TW", **context,
                "units": [{"unit_id": u.unit_id, "source": u.source_text} for u in units]}

    def _run(self, units: list[UnitInput], context: dict[str, Any], result: BatchResult,
             qa_glossary: Any, allowed_kana: Any, depth: int) -> None:
        group = list(units)
        correction: str | None = None
        last_error = "unknown"
        rejected = False
        for _attempt in range(2):
            payload = self._payload(group, context)
            est = self.estimate(payload, sum(len(u.plain_text) for u in group))
            try:
                parsed, rid, mode = call_structured(
                    self.client, self.recorder, stage="translate", model=self.model, prompt=self.prompt,
                    payload=payload, mode=self.mode, cfg=self.cfg, unit_ids=[u.unit_id for u in group],
                    est_cost=est, correction=correction, on_downgrade=self._downgrade)
                self.mode = mode
            except ModelOutputError as exc:
                result.requests += 1
                last_error = str(exc)
                correction = last_error
                continue
            except RequestRejectedError as exc:
                # deterministic refusal (content filter / provider 400): never resend the same group
                result.requests += 1
                kind = "content_blocked" if isinstance(exc, ContentBlockedError) else "request_rejected"
                last_error = f"{kind}: {exc}"
                rejected = True
                break
            result.requests += 1
            assert isinstance(parsed, TranslationBatchOutput)
            want = [u.unit_id for u in group]
            got = [t.unit_id for t in parsed.translations]
            if sorted(got) != sorted(want) or len(set(got)) != len(got):
                missing = sorted(set(want) - set(got))
                extra = sorted(set(got) - set(want))
                dup = sorted({x for x in got if got.count(x) > 1})
                last_error = f"unit_id mismatch (missing={missing[:5]}, extra={extra[:5]}, duplicated={dup[:5]})"
                correction = last_error
                continue
            by_id = {t.unit_id: t for t in parsed.translations}
            bad: list[UnitInput] = []
            texts: dict[str, str] = {}
            pending_ok: dict[str, UnitResult] = {}
            for u in group:
                t = by_id[u.unit_id]
                findings = check_translation(unit_id=u.unit_id, kind=u.kind, source_text=u.source_text,
                                             plain_text=u.plain_text, inline_map=u.inline_map,
                                             ruby_count=u.ruby_count, text=t.translated_text,
                                             glossary=qa_glossary, allowed_kana=allowed_kana)
                errors = [f for f in findings if f.severity == "error"]
                if errors:
                    bad.append(u)
                    last_error = f"{u.unit_id}: " + "; ".join(f"{e.type}: {e.evidence}" for e in errors)
                    result.failure_findings.extend(errors)
                else:
                    texts[u.unit_id] = t.translated_text
                    pending_ok[u.unit_id] = UnitResult(t.translated_text, list(t.flags), list(t.notes),
                                                       findings, rid, clean_note(t.translator_note))
            for f in check_batch(texts, {u.unit_id: u.source_text for u in group}):
                if f.unit_id in pending_ok:
                    pending_ok[f.unit_id].findings.append(f)
            result.ok.update(pending_ok)
            if not bad:
                return
            group = bad
            correction = last_error
        # a content filter may react to the memory sent along (scene facts, summaries), not to the text itself:
        # retry once with the text, its neighbours and the glossary only
        if rejected and last_error.startswith("content_blocked") and not context.get("_slim"):
            slim = {k: v for k, v in context.items() if k in SLIM_KEYS}
            self._run(group, {**slim, "_slim": True}, result, qa_glossary, allowed_kana, depth)
            return
        # still failing after one corrective retry (or refused) → split to isolate, or give up
        if rejected and len(group) == 1:
            result.blocked.add(group[0].unit_id)
        if len(group) > 1:
            mid = len(group) // 2
            self._run(group[:mid], context, result, qa_glossary, allowed_kana, depth + 1)
            self._run(group[mid:], context, result, qa_glossary, allowed_kana, depth + 1)
        else:
            result.failed[group[0].unit_id] = last_error
