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
# what a content filter says (Gemini PROHIBITED_CONTENT / SAFETY, OpenAI content_policy, OpenRouter moderation);
# a bare "blocked" is not enough: "Account blocked" is an account problem, not a passage to split and resend
_BLOCK_MARKERS = ("content_policy", "prohibited_content", "content_filter", "safety", "moderation", "flagged",
                  "blocklist", "recitation", "blocked the request")
_NOT_CONTENT = ("account", "api key", "credits", "billing", "unauthorized")
_REFUSALS = {"content_filter", "refusal", "safety", "prohibited_content", "recitation", "blocklist"}


def _error_obj(body: object) -> dict[str, object]:
    """One shape for the error forms seen: {"error": {...}} (OpenRouter), {"error": "..."}, {"detail": "..."} and
    {"detail": [{"msg": ...}]} (fal)."""
    if not isinstance(body, dict):
        return {}
    err = body.get("error")
    if isinstance(err, dict):
        return err
    if isinstance(err, str) and err:
        return {"message": err}
    detail = body.get("detail")
    if isinstance(detail, str) and detail:
        return {"message": detail}
    if isinstance(detail, list) and detail:
        return {"message": "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)}
    if isinstance(detail, dict):
        return {"message": str(detail.get("message") or detail)}
    return {}


# Upstream routing problems reported as HTTP 400 that succeed when retried (another provider node / region),
# e.g. Google AI Studio «User location is not supported for the API use» (FAILED_PRECONDITION).
_TRANSIENT_400 = ("location is not supported", "failed_precondition", "overloaded", "temporarily unavailable")


def is_transient_rejection(err: dict[str, object]) -> bool:
    meta = err.get("metadata")
    values = list(meta.values()) if isinstance(meta, dict) else []
    blob = " ".join(str(x) for x in (err.get("message"), *values)).lower()
    return any(m in blob for m in _TRANSIENT_400)


def is_content_block(err: dict[str, object]) -> bool:
    meta = err.get("metadata")
    values = list(meta.values()) if isinstance(meta, dict) else []
    blob = " ".join(str(x) for x in (err.get("message"), err.get("code"), *values)).lower()
    if "prohibited_content" in blob:  # an explicit code wins
        return True
    if any(m in blob for m in _NOT_CONTENT):
        return False
    return any(m in blob for m in _BLOCK_MARKERS)


def is_in_flight_wait(err: dict[str, object]) -> bool:
    """HTTP 402 because requests still running may use the remaining credit: wait and send again, not out of money."""
    meta = err.get("metadata")
    values = list(meta.values()) if isinstance(meta, dict) else []
    return "in_flight" in " ".join(str(x) for x in (err.get("message"), *values)).lower()


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
    def _catalog(self) -> tuple[str, dict[str, str]]:
        return f"{self.base_url}/models", ({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})

    def list_models(self) -> list[dict[str, Any]]:
        url, headers = self._catalog()
        try:
            resp = self._http.get(url, headers=headers)
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

    def balance(self) -> dict[str, Any]:
        """Credits left on the account (/credits) and on this key's own limit (/key); either can stop a run."""
        out: dict[str, Any] = {}
        for path in ("credits", "key"):
            try:
                resp = self._http.get(f"{self.base_url}/{path}", headers=self._auth())
            except httpx.HTTPError:
                continue
            data = resp.json().get("data") if resp.status_code == 200 else None
            if not isinstance(data, dict):
                continue
            if path == "credits" and data.get("total_credits") is not None:
                out["account_usd"] = round(float(data["total_credits"]) - float(data.get("total_usage") or 0), 2)
            if path == "key" and data.get("limit_remaining") is not None:
                out["key_limit_usd"] = round(float(data["limit_remaining"]), 2)
        return out

    def build_payload(self, req: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": req.model, "messages": self._cached(req), "stream": False}
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
        if self.name == "openrouter" and req.model.startswith("google/"):
            # explicit caching works on Google AI Studio, not on Vertex (measured 2026-10-15); others stay as fallback
            payload["provider"] = {**payload.get("provider", {}), "order": ["google-ai-studio"], "allow_fallbacks": True}
        return payload

    def _cached(self, req: LLMRequest) -> list[dict[str, Any]]:
        """The system prompt (the same rules on every request of a task) marked for prompt caching.

        Gemini caches nothing on its own here: three identical prompts were all billed in full; with the marker the
        repeated ~1,750 tokens were read from the cache and a request cost 1/6–1/9 as much. Dynamic data stays in
        the user message after it. Only on direct OpenRouter: fal passing the marker on is not verified."""
        msgs: list[dict[str, Any]] = [dict(m) for m in req.messages]
        if self.name == "openrouter" and msgs and msgs[0].get("role") == "system" and isinstance(msgs[0]["content"], str):
            text = msgs[0]["content"]
            cut = req.cached_prefix_chars if 0 < req.cached_prefix_chars < len(text) else len(text)
            parts: list[dict[str, Any]] = [{"type": "text", "text": text[:cut], "cache_control": {"type": "ephemeral"}}]
            if cut < len(text):  # this request's own rules and any correction, after the cached part
                parts.append({"type": "text", "text": text[cut:]})
            msgs[0]["content"] = parts
        return msgs

    @staticmethod
    def _path(payload: dict[str, Any]) -> str:
        """A rendered prompt goes to /completions (a local server), messages to /chat/completions."""
        return "/completions" if "prompt" in payload else "/chat/completions"

    def complete(self, req: LLMRequest) -> LLMResponse:
        payload = self.build_payload(req)
        headers = self._auth()
        last_exc: Exception | None = None
        possibly_billed = False
        maybe_billed_attempts = 0  # attempts that may have been billed before the one that answered
        for attempt in range(1, self.max_attempts + 1):
            t0 = time.monotonic()
            try:
                resp = self._http.post(f"{self.base_url}{self._path(payload)}", json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                possibly_billed = True  # the provider may have completed (and billed) the request
                maybe_billed_attempts += 1
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
                    body = None
                if not isinstance(body, dict):  # not JSON, or JSON of another shape (a list…)
                    last_exc = RetryableAPIError("HTTP 200 with a body that is not a JSON object")
                    possibly_billed = True
                    maybe_billed_attempts += 1
                    if attempt < self.max_attempts:
                        self.sleep(self._backoff(attempt, None))
                    continue
                if body.get("error") or (body.get("detail") and not body.get("choices")):
                    err = _error_obj(body)
                    code = err.get("code")
                    msg = redact(str(err)[:500], self.api_key)
                    if is_content_block(err):
                        raise ContentBlockedError(f"content filter: {msg}")
                    if isinstance(code, int) and code in FATAL and not (code == 402 and is_in_flight_wait(err)):
                        raise FatalAPIError(f"OpenRouter error {code}: {msg}")
                    last_exc = RetryableAPIError(f"OpenRouter error in body: {msg}")
                    if attempt < self.max_attempts:
                        self.sleep(self._backoff(attempt, None))
                    continue
                resp_ok = self._parse(body, req, latency, attempt)
                resp_ok.maybe_billed_attempts = maybe_billed_attempts
                return resp_ok
            text = self._err_text(resp)
            if status in (400, 403, 422) and is_content_block(self._err_obj(resp)):
                raise ContentBlockedError(f"HTTP {status} content filter: {text}")
            if status == 402 and is_in_flight_wait(self._err_obj(resp)):
                # the credit is held by requests still running: wait as told, then send again
                last_exc = RetryableAPIError(f"HTTP 402 (waiting for running requests to settle): {text}")
                if attempt < self.max_attempts:
                    self.sleep(self._backoff(attempt, resp.headers.get("retry-after")))
                continue
            if status in FATAL:
                hint = {401: "invalid API key", 402: "insufficient credits", 403: "forbidden / moderation",
                        404: "model or endpoint not found"}[status]
                raise FatalAPIError(f"HTTP {status} ({hint}): {text}")
            if status in REJECTED:
                if is_transient_rejection(self._err_obj(resp)):
                    last_exc = RetryableAPIError(f"HTTP {status} (transient upstream routing): {text}")
                    if attempt < self.max_attempts:
                        self.sleep(self._backoff(attempt, resp.headers.get("retry-after")))
                    continue
                raise RequestRejectedError(f"HTTP {status}: {text}")
            if status in RETRYABLE or status >= 500:
                last_exc = RetryableAPIError(f"HTTP {status}: {text}")
                if attempt < self.max_attempts:
                    self.sleep(self._backoff(attempt, resp.headers.get("retry-after")))
                continue
            raise LLMError(f"Unexpected HTTP {status}: {text}")
        gave_up = RetryableAPIError(f"giving up after {self.max_attempts} attempts: "
                                    f"{redact(str(last_exc), self.api_key)}")
        gave_up.possibly_billed = possibly_billed  # type: ignore[attr-defined]
        raise gave_up

    def _parse(self, body: dict[str, Any], req: LLMRequest, latency: int, attempt: int) -> LLMResponse:
        choices = body.get("choices") or []
        if not choices:
            raise RetryableAPIError("response has no choices")
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):  # content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        finish = str(choices[0].get("finish_reason") or choices[0].get("native_finish_reason") or "").lower()
        if (finish in _REFUSALS or msg.get("refusal")) and not (content or "").strip():
            # answered with a refusal instead of an error: the content filter, not a malformed answer
            raise ContentBlockedError(f"content filter (finish_reason={finish or 'refusal'}): "
                                      f"{redact(str(msg.get('refusal') or '')[:300], self.api_key)}")
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
