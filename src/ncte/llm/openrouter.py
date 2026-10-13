"""OpenRouter Chat Completions adapter (spec §3).

* non-streaming, ``response_format`` json_schema (strict) when the model advertises
  ``structured_outputs``; ``provider.require_parameters=true`` so routing never silently drops it
* 408/429/5xx/524/529/timeouts → exponential backoff + jitter (honours Retry-After)
* 401/402/403/404 → :class:`FatalAPIError` (stop the run)
* 400/413/422 → :class:`RequestRejectedError`
* never logs the API key
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from .. import __version__
from ..config import redact
from ..errors import ContentBlockedError, FatalAPIError, LLMError, RequestRejectedError, RetryableAPIError
from .base import LLMRequest, LLMResponse

RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}
FATAL = {401, 402, 403, 404}
REJECTED = {400, 413, 422}
_BLOCK_MARKERS = ("content_policy", "prohibited_content", "safety", "blocked", "moderation", "blocklist", "recitation")


def _error_obj(body: object) -> dict[str, object]:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"]  # type: ignore[return-value]
    return {}


def is_content_block(err: dict[str, object]) -> bool:
    meta = err.get("metadata")
    values = list(meta.values()) if isinstance(meta, dict) else []
    blob = " ".join(str(x) for x in (err.get("message"), *values)).lower()
    return any(m in blob for m in _BLOCK_MARKERS)


class OpenRouterClient:
    name = "openrouter"

    def __init__(self, api_key: str | None, *, base_url: str = "https://openrouter.ai/api/v1",
                 timeout: float = 120, max_attempts: int = 4, app_title: str = "NCTE",
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, backoff_base: float = 1.5):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.backoff_base = backoff_base
        headers = {"User-Agent": f"ncte/{__version__}", "X-Title": app_title}
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, connect=20), headers=headers, transport=transport)

    # ------------------------------------------------------------------ helpers
    def _auth(self) -> dict[str, str]:
        if not self.api_key:
            raise FatalAPIError("OPENROUTER_API_KEY is not set")
        return {"Authorization": f"Bearer {self.api_key}"}

    def _err_text(self, resp: httpx.Response) -> str:
        try:
            err = _error_obj(resp.json())
            text = str(err.get("message") or resp.text)
            meta = err.get("metadata")
            if isinstance(meta, dict) and meta:  # e.g. provider_name / raw upstream error: keep for diagnosis
                text += " | " + str(meta)
        except ValueError:
            text = resp.text
        return redact(str(text)[:800], self.api_key)

    def _err_obj(self, resp: httpx.Response) -> dict[str, object]:
        try:
            return _error_obj(resp.json())
        except ValueError:
            return {}

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 120.0)
            except ValueError:
                pass
        return min(self.backoff_base * (2 ** (attempt - 1)), 60.0) * (0.5 + random.random())

    # ------------------------------------------------------------------ API
    def list_models(self) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            resp = self._http.get(f"{self.base_url}/models", headers=headers)
        except httpx.HTTPError as exc:
            raise RetryableAPIError(f"model list request failed: {redact(str(exc), self.api_key)}") from exc
        if resp.status_code != 200:
            raise LLMError(f"GET /models → HTTP {resp.status_code}: {self._err_text(resp)}")
        data = resp.json().get("data")
        if not isinstance(data, list):
            raise LLMError("GET /models: unexpected response shape")
        return data

    def get_generation(self, generation_id: str) -> dict[str, Any] | None:
        try:
            resp = self._http.get(f"{self.base_url}/generation", params={"id": generation_id}, headers=self._auth())
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        data = resp.json().get("data")
        return data if isinstance(data, dict) else None

    def build_payload(self, req: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": req.model, "messages": req.messages, "stream": False}
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.max_tokens:
            payload["max_tokens"] = req.max_tokens
        if req.structured_mode == "json_schema" and req.json_schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": req.schema_name, "strict": True, "schema": req.json_schema}}
            payload["provider"] = {"require_parameters": True}
        elif req.structured_mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        payload.update(req.extra)
        return payload

    def complete(self, req: LLMRequest) -> LLMResponse:
        payload = self.build_payload(req)
        headers = self._auth()
        last_exc: Exception | None = None
        possibly_billed = False
        for attempt in range(1, self.max_attempts + 1):
            t0 = time.monotonic()
            try:
                resp = self._http.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                possibly_billed = True  # the provider may have completed (and billed) the request
                last_exc = exc
                if attempt < self.max_attempts:
                    self.sleep(self._backoff(attempt, None))
                continue
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self.max_attempts:
                    self.sleep(self._backoff(attempt, None))
                continue
            latency = int((time.monotonic() - t0) * 1000)
            status = resp.status_code
            if status == 200:
                try:
                    body = resp.json()
                except (ValueError, json.JSONDecodeError):
                    last_exc = RetryableAPIError("HTTP 200 with non-JSON body")
                    possibly_billed = True
                    if attempt < self.max_attempts:
                        self.sleep(self._backoff(attempt, None))
                    continue
                if isinstance(body, dict) and body.get("error"):
                    err = body["error"]
                    code = err.get("code") if isinstance(err, dict) else None
                    msg = redact(str(err)[:500], self.api_key)
                    if isinstance(err, dict) and is_content_block(err):
                        raise ContentBlockedError(f"content filter: {msg}")
                    if isinstance(code, int) and code in FATAL:
                        raise FatalAPIError(f"OpenRouter error {code}: {msg}")
                    last_exc = RetryableAPIError(f"OpenRouter error in body: {msg}")
                    if attempt < self.max_attempts:
                        self.sleep(self._backoff(attempt, None))
                    continue
                return self._parse(body, req, latency, attempt)
            text = self._err_text(resp)
            if status in (400, 403, 422) and is_content_block(self._err_obj(resp)):
                raise ContentBlockedError(f"HTTP {status} content filter: {text}")
            if status in FATAL:
                hint = {401: "invalid API key", 402: "insufficient credits", 403: "forbidden / moderation",
                        404: "model or endpoint not found"}[status]
                raise FatalAPIError(f"HTTP {status} ({hint}): {text}")
            if status in REJECTED:
                raise RequestRejectedError(f"HTTP {status}: {text}")
            if status in RETRYABLE or status >= 500:
                last_exc = RetryableAPIError(f"HTTP {status}: {text}")
                if attempt < self.max_attempts:
                    self.sleep(self._backoff(attempt, resp.headers.get("retry-after")))
                continue
            raise LLMError(f"Unexpected HTTP {status}: {text}")
        err = RetryableAPIError(f"giving up after {self.max_attempts} attempts: "
                                f"{redact(str(last_exc), self.api_key)}")
        err.possibly_billed = possibly_billed  # type: ignore[attr-defined]
        raise err

    def _parse(self, body: dict[str, Any], req: LLMRequest, latency: int, attempt: int) -> LLMResponse:
        choices = body.get("choices") or []
        if not choices:
            raise RetryableAPIError("response has no choices")
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):  # content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        usage = body.get("usage") or {}
        cost = usage.get("cost")
        return LLMResponse(
            content=content or "",
            model=body.get("model") or req.model,
            provider=body.get("provider"),
            generation_id=body.get("id"),
            usage=usage,
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            latency_ms=latency,
            finish_reason=choices[0].get("finish_reason"),
            attempts=attempt,
            structured_mode=req.structured_mode,
        )
