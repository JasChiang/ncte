"""The Gemini API (Google AI Studio key) as a source: ``generateContent``, non-streaming (ADR-105).

Only what the Gemini API reference states (https://ai.google.dev/api/generate-content, /api/models, the thinking,
caching and API-errors guides, read 2026-10-24):

* ``POST {base}/models/{model}:generateContent`` with the key in the ``x-goog-api-key`` header (never in the URL, never
  logged); ``systemInstruction`` holds the step's rules, ``contents`` the request (roles ``user`` / ``model``)
* structured output: ``generationConfig.responseFormat.text`` = ``{mimeType: APPLICATION_JSON, schema: <JSON Schema>}``
  (the reference marks ``responseSchema`` / ``_responseJsonSchema`` deprecated, "Use responseFormat instead");
  ``json_object`` mode sends ``responseMimeType: application/json`` alone
* reasoning: a step's level → ``thinkingConfig.thinkingLevel`` (MINIMAL / LOW / MEDIUM / HIGH; "Recommended for Gemini
  3 or later models"), ``max:N`` → ``thinkingBudget`` N; nothing set → the model's default
* ``usageMetadata``: ``promptTokenCount`` (includes cached tokens), ``cachedContentTokenCount``,
  ``candidatesTokenCount``, ``thoughtsTokenCount`` — recorded in the OpenRouter form the reports read
  (``prompt_tokens``; ``completion_tokens`` = answer + thoughts, "response pricing is the sum of output tokens and
  thinking tokens"; ``prompt_tokens_details.cached_tokens``; ``completion_tokens_details.reasoning_tokens``)
* caching: implicit caching is on by default for Gemini 2.5 and newer and discounts automatically; the shared rules are
  the first thing sent (``systemInstruction``), as the guide advises. Explicit cache objects are not used.
* a blocked prompt (``promptFeedback.blockReason``) or a candidate stopped by a filter (``finishReason`` SAFETY,
  RECITATION, BLOCKLIST, PROHIBITED_CONTENT, SPII, …) → :class:`ContentBlockedError`; ``PUP_LIMITED_DISABLED`` (the
  account is limited) → :class:`FatalAPIError`; ``MAX_TOKENS`` → ``finish_reason="length"`` (the translator raises the
  cap); no text otherwise → :class:`EmptyAnswerError` with the usage
* 408/409/429/499/5xx/timeouts → backoff + jitter (honours Retry-After); 400 FAILED_PRECONDITION (billing, region),
  401/402/403/404 → :class:`FatalAPIError`; other 400/413/422 → :class:`RequestRejectedError`

Prices: the Gemini API returns none. The catalog (:meth:`list_models`) takes each listed model's prices from
OpenRouter's public ``/models`` (``google/<model>``, no key) as the fal source does; a model OpenRouter does not list
has no price, and a run on it is refused (its cost would be unknown and the budget could not hold). Costs are then
estimated from the tokens Google reports (``estimated_from_tokens``); Google's own bill is the real figure.
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
from ..errors import (
    ContentBlockedError,
    EmptyAnswerError,
    FatalAPIError,
    LLMError,
    RequestRejectedError,
    RetryableAPIError,
)
from .base import LLMRequest, LLMResponse
from .openrouter import OpenRouterClient, _error_obj, is_content_block

PREFIX = "gemini/"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
PRICES_URL = "https://openrouter.ai/api/v1"  # public catalog, no key sent
RETRYABLE = {408, 409, 425, 429, 499, 500, 502, 503, 504}
FATAL = {401, 402, 403, 404}
REJECTED = {400, 413, 422}
# FinishReason values the reference describes as a filter stopping the answer
BLOCKED_FINISH = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY",
                  "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION"}
ACCOUNT_FINISH = {"PUP_LIMITED_DISABLED"}  # "the account is limited or disabled for Prohibited Use Policy violations"
# what a request may ask when a model's catalog entry has no OpenRouter counterpart to say so
BASE_PARAMS = ["max_tokens", "temperature", "response_format", "structured_outputs"]


def model_name(model: str) -> str:
    """``gemini/gemini-3.8-flash`` → ``gemini-3.8-flash`` (the name the API takes)."""
    return model[len(PREFIX):] if model.startswith(PREFIX) else model


class GeminiClient:
    name = "gemini"
    catalog_source = "gemini"

    def __init__(self, api_key: str | None, *, base_url: str = BASE_URL, prices_url: str = PRICES_URL,
                 timeout: float = 120, max_attempts: int = 4, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, backoff_base: float = 1.5):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.backoff_base = backoff_base
        self.notes: list[str] = []
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, connect=20),
                                  headers={"User-Agent": f"ncte/{__version__}"}, transport=transport)
        # the public OpenRouter catalog, for prices only: no key of any kind is sent there
        self._prices = OpenRouterClient(None, base_url=prices_url, timeout=60, transport=transport)

    # ------------------------------------------------------------------ helpers
    def _auth(self) -> dict[str, str]:
        if not self.api_key:
            raise FatalAPIError("GEMINI_API_KEY is not set")
        return {"x-goog-api-key": self.api_key}

    def _err(self, resp: httpx.Response) -> dict[str, object]:
        try:
            return _error_obj(resp.json())
        except ValueError:
            return {}

    def _err_text(self, resp: httpx.Response) -> str:
        err = self._err(resp)
        text = " ".join(str(x) for x in (err.get("status"), err.get("message")) if x) or resp.text
        return redact(str(text)[:800], self.api_key)

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 120.0)
            except ValueError:
                pass
        return min(self.backoff_base * (2 ** (attempt - 1)), 60.0) * (0.5 + random.random())

    # ------------------------------------------------------------------ catalog
    def _gemini_models(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(50):  # pages
            params: dict[str, Any] = {"pageSize": 1000, **({"pageToken": token} if token else {})}
            try:
                resp = self._http.get(f"{self.base_url}/models", params=params, headers=self._auth())
            except httpx.HTTPError as exc:
                raise RetryableAPIError(f"Gemini model list request failed: {redact(str(exc), self.api_key)}") from exc
            if resp.status_code in FATAL:
                raise FatalAPIError(f"Gemini GET /models → HTTP {resp.status_code}: {self._err_text(resp)}")
            if resp.status_code != 200:
                raise LLMError(f"Gemini GET /models → HTTP {resp.status_code}: {self._err_text(resp)}")
            body = resp.json()
            models = body.get("models") if isinstance(body, dict) else None
            if not isinstance(models, list):
                raise LLMError("Gemini GET /models: unexpected response shape")
            out.extend(m for m in models if isinstance(m, dict))
            token = body.get("nextPageToken") or None
            if not token:
                break
        return out

    def list_models(self) -> list[dict[str, Any]]:
        """The models this key may call (``models.list``, those that take ``generateContent``), each in the catalog
        form the run reads, with OpenRouter's public prices for ``google/<model>`` where it lists one."""
        listed = self._gemini_models()
        try:
            prices = {str(m.get("id")): m for m in self._prices.list_models()}
        except LLMError as exc:  # no prices: every model's cost is unknown (a run is refused, never guessed)
            prices = {}
            self.notes.append(f"OpenRouter's public price list could not be read ({exc}); Gemini prices unknown")
        out: list[dict[str, Any]] = []
        for m in listed:
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            name = str(m.get("name") or "")
            mid = name.split("/", 1)[1] if name.startswith("models/") else name
            if not mid:
                continue
            ref = prices.get(f"google/{mid}")
            params = list((ref or {}).get("supported_parameters") or BASE_PARAMS)
            thinking = m.get("thinking")
            if thinking is False:
                params = [p for p in params if p != "reasoning"]
            elif thinking is True and "reasoning" not in params:
                params.append("reasoning")
            entry: dict[str, Any] = {
                "id": PREFIX + mid, "name": m.get("displayName") or mid, "context_length": m.get("inputTokenLimit"),
                "supported_parameters": params, "reasoning": (ref or {}).get("reasoning"),
                "gemini": {k: m.get(k) for k in ("version", "inputTokenLimit", "outputTokenLimit", "thinking")},
            }
            if ref and ref.get("pricing"):
                entry["pricing"] = ref["pricing"]
                entry["price_source"] = f"OpenRouter public /models: google/{mid}"
            else:
                entry["pricing_missing"] = (
                    f"{PREFIX}{mid}: the Gemini API reports no prices and OpenRouter's public /models lists no "
                    f"google/{mid}, so its cost is unknown; NCTE does not start a paid run it cannot keep within the "
                    "budget. Choose a model OpenRouter lists, or see Google's pricing page.")
            out.append(entry)
        return out

    def get_generation(self, generation_id: str) -> dict[str, Any] | None:
        return None  # the Gemini API has no per-request bill to look up

    def balance(self) -> dict[str, Any]:
        return {}  # no balance endpoint is documented

    # ------------------------------------------------------------------ requests
    def build_payload(self, req: LLMRequest) -> dict[str, Any]:
        system = "\n\n".join(str(m["content"]) for m in req.messages if m.get("role") == "system")
        contents = [{"role": "model" if m.get("role") == "assistant" else "user", "parts": [{"text": str(m["content"])}]}
                    for m in req.messages if m.get("role") != "system"]
        config: dict[str, Any] = {}
        if req.temperature is not None:
            config["temperature"] = req.temperature
        if req.max_tokens:
            config["maxOutputTokens"] = req.max_tokens
        if req.structured_mode == "json_schema" and req.json_schema is not None:
            config["responseFormat"] = {"text": {"mimeType": "APPLICATION_JSON", "schema": req.json_schema}}
        elif req.structured_mode in ("json_schema", "json_object"):
            config["responseMimeType"] = "application/json"
        reasoning = req.extra.get("reasoning") or {}
        if reasoning.get("effort"):
            config["thinkingConfig"] = {"thinkingLevel": str(reasoning["effort"]).upper()}
        elif reasoning.get("max_tokens") is not None:
            config["thinkingConfig"] = {"thinkingBudget": int(reasoning["max_tokens"])}
        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if config:
            payload["generationConfig"] = config
        return payload

    def complete(self, req: LLMRequest) -> LLMResponse:
        payload = self.build_payload(req)
        url = f"{self.base_url}/models/{model_name(req.model)}:generateContent"
        headers = self._auth()
        last_exc: Exception | None = None
        possibly_billed = False
        maybe_billed_attempts = 0
        for attempt in range(1, self.max_attempts + 1):
            t0 = time.monotonic()
            try:
                resp = self._http.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                possibly_billed = True  # Google may have completed (and billed) the request
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
                if not isinstance(body, dict):
                    last_exc = RetryableAPIError("HTTP 200 with a body that is not a JSON object")
                    possibly_billed = True
                    maybe_billed_attempts += 1
                    if attempt < self.max_attempts:
                        self.sleep(self._backoff(attempt, None))
                    continue
                out = self._parse(body, req, latency, attempt)
                out.maybe_billed_attempts = maybe_billed_attempts
                return out
            text = self._err_text(resp)
            err = self._err(resp)
            if status in REJECTED and is_content_block(err):
                raise ContentBlockedError(f"HTTP {status} content filter: {text}")
            if status == 400 and str(err.get("status") or "").upper() == "FAILED_PRECONDITION":
                # "a prerequisite isn't met, such as billing" (or the region): every request would fail the same way
                raise FatalAPIError(f"HTTP 400 FAILED_PRECONDITION (billing or region not set up for this key): {text}")
            if status in FATAL:
                hint = {401: "invalid API key", 402: "payment required", 403: "permission denied",
                        404: "model not found"}[status]
                raise FatalAPIError(f"HTTP {status} ({hint}): {text}")
            if status in REJECTED:
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

    @staticmethod
    def usage(meta: dict[str, Any]) -> dict[str, Any]:
        """``usageMetadata`` in the form the reports read; the counts as Google gave them kept under ``gemini``."""
        prompt = int(meta.get("promptTokenCount") or 0)
        answer = int(meta.get("candidatesTokenCount") or 0)
        thoughts = int(meta.get("thoughtsTokenCount") or 0)
        cached = int(meta.get("cachedContentTokenCount") or 0)
        return {"prompt_tokens": prompt, "completion_tokens": answer + thoughts,
                "total_tokens": int(meta.get("totalTokenCount") or prompt + answer + thoughts),
                "prompt_tokens_details": {"cached_tokens": cached},
                "completion_tokens_details": {"reasoning_tokens": thoughts},
                "gemini": {k: v for k, v in meta.items() if isinstance(v, (int, float))}}

    def _parse(self, body: dict[str, Any], req: LLMRequest, latency: int, attempt: int) -> LLMResponse:
        usage = self.usage(body.get("usageMetadata") or {})
        if body.get("modelVersion"):
            usage["model_version"] = body["modelVersion"]
        feedback = body.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise ContentBlockedError(f"content filter: prompt blocked ({feedback['blockReason']})")
        candidates = body.get("candidates") or []
        if not candidates:
            raise EmptyAnswerError("empty answer (no candidates)", usage=usage, latency_ms=latency)
        cand = candidates[0] if isinstance(candidates[0], dict) else {}
        finish = str(cand.get("finishReason") or "").upper()
        if finish in ACCOUNT_FINISH:
            raise FatalAPIError(f"Gemini stopped: {finish} (the account is limited or disabled)")
        if finish in BLOCKED_FINISH:
            raise ContentBlockedError(f"content filter (finishReason={finish})")
        parts = ((cand.get("content") or {}).get("parts")) or []
        text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        if finish == "MAX_TOKENS":
            reason = "length"  # the translator raises the cap and asks again
        elif not text.strip():
            raise EmptyAnswerError(f"empty answer (finishReason={finish or 'none'})", usage=usage, latency_ms=latency)
        else:
            reason = finish.lower() or "stop"
        return LLMResponse(content=text, model=req.model, provider="gemini", generation_id=body.get("responseId"),
                           usage=usage, cost_usd=None, latency_ms=latency, finish_reason=reason, attempts=attempt,
                           structured_mode=req.structured_mode)
