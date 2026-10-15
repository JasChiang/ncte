import json

import httpx
import pytest

from ncte.errors import FatalAPIError, RequestRejectedError, RetryableAPIError
from ncte.llm.base import LLMRequest
from ncte.llm.openrouter import OpenRouterClient
from ncte.llm.schemas import TranslationBatchOutput, strict_json_schema

KEY = "sk-or-v1-unit-test-secret-key"


def ok_body(content: str, cost: float | None = 0.00123):
    usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
             "prompt_tokens_details": {"cached_tokens": 10}, "completion_tokens_details": {"reasoning_tokens": 5}}
    if cost is not None:
        usage["cost"] = cost
    return {"id": "gen-abc123", "model": "google/gemini-3.8-flash", "provider": "Google",
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": usage}


def make(handler, **kw):
    return OpenRouterClient(KEY, transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw)


def req():
    return LLMRequest(stage="translate", model="google/gemini-3.8-flash",
                      messages=[{"role": "user", "content": "{}"}], schema_name="ncte_translate",
                      json_schema=strict_json_schema(TranslationBatchOutput), structured_mode="json_schema")


def test_success_and_payload():
    seen = {}

    def h(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=ok_body('{"translations": []}'))

    resp = make(h).complete(req())
    assert resp.cost_usd == pytest.approx(0.00123)
    assert resp.generation_id == "gen-abc123" and resp.provider == "Google"
    rf = seen["body"]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert seen["body"]["provider"]["require_parameters"] is True
    assert seen["auth"] == f"Bearer {KEY}"


def test_retry_then_success():
    calls = {"n": 0}

    def h(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429 if calls["n"] == 1 else 503, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=ok_body("{}"))

    resp = make(h).complete(req())
    assert calls["n"] == 3 and resp.attempts == 3


def test_retries_exhausted():
    with pytest.raises(RetryableAPIError):
        make(lambda r: httpx.Response(502, text="bad gateway"), max_attempts=2).complete(req())


@pytest.mark.parametrize("code", [401, 402, 403, 404])
def test_fatal_codes_stop_immediately(code):
    calls = {"n": 0}

    def h(request):
        calls["n"] += 1
        return httpx.Response(code, json={"error": {"message": f"nope {KEY}"}})

    with pytest.raises(FatalAPIError) as ei:
        make(h).complete(req())
    assert calls["n"] == 1
    assert KEY not in str(ei.value)


def test_rejected_400():
    with pytest.raises(RequestRejectedError):
        make(lambda r: httpx.Response(400, json={"error": {"message": "response_format unsupported"}})).complete(req())


def test_timeout_marks_possibly_billed():
    def h(request):
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(RetryableAPIError) as ei:
        make(h, max_attempts=2).complete(req())
    assert getattr(ei.value, "possibly_billed", False) is True


def test_error_in_200_body_is_retried():
    calls = {"n": 0}

    def h(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"error": {"code": 502, "message": "upstream"}})
        return httpx.Response(200, json=ok_body("{}"))

    make(h).complete(req())
    assert calls["n"] == 2


def test_no_key_is_fatal():
    c = OpenRouterClient(None, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(FatalAPIError):
        c.complete(req())


def test_list_models_without_key_and_generation():
    def h(request):
        if request.url.path.endswith("/models"):
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"data": [{"id": "m", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]})
        return httpx.Response(200, json={"data": {"total_cost": 0.5, "model": "m"}})

    c = OpenRouterClient(None, transport=httpx.MockTransport(h))
    assert c.list_models()[0]["id"] == "m"
    c.api_key = KEY
    assert c.get_generation("gen-1")["total_cost"] == 0.5


def test_strict_schema_shape():
    s = strict_json_schema(TranslationBatchOutput)

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "object":
                assert n["additionalProperties"] is False
                assert set(n["required"]) == set(n["properties"])
            assert "$ref" not in n
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(s)


def test_content_policy_403_is_not_fatal():
    from ncte.errors import ContentBlockedError

    body = {"error": {"message": "Gemini blocked the request: PROHIBITED_CONTENT", "code": 403,
                      "metadata": {"error_type": "content_policy_violation", "provider_code": "PROHIBITED_CONTENT"}}}
    with pytest.raises(ContentBlockedError):
        make(lambda r: httpx.Response(403, json=body)).complete(req())
    with pytest.raises(ContentBlockedError):  # same error delivered inside a 200 body
        make(lambda r: httpx.Response(200, json=body)).complete(req())
    with pytest.raises(FatalAPIError):  # a plain 403 is still fatal
        make(lambda r: httpx.Response(403, json={"error": {"message": "Forbidden"}})).complete(req())


def test_provider_400_keeps_upstream_details():
    body = {"error": {"message": "Provider returned error", "code": 400,
                      "metadata": {"provider_name": "Google AI Studio", "raw": "candidate blocked: SAFETY"}}}
    from ncte.errors import ContentBlockedError

    with pytest.raises(ContentBlockedError) as ei:
        make(lambda r: httpx.Response(400, json=body)).complete(req())
    assert "Google AI Studio" in str(ei.value)
    with pytest.raises(RequestRejectedError) as ei:
        make(lambda r: httpx.Response(400, json={"error": {"message": "Provider returned error",
                                                           "metadata": {"raw": "invalid argument"}}})).complete(req())
    assert "invalid argument" in str(ei.value)


def test_provider_location_error_is_retried():
    """Real-book regression: Google AI Studio intermittently answered «User location is not supported»."""
    calls = {"n": 0}
    body = {"error": {"message": "Provider returned error", "code": 400, "metadata": {
        "raw": '{"error": {"code": 400, "message": "User location is not supported for the API use.", '
               '"status": "FAILED_PRECONDITION"}}', "provider_name": "Google AI Studio"}}}

    def h(request):
        calls["n"] += 1
        return httpx.Response(400, json=body) if calls["n"] == 1 else httpx.Response(200, json=ok_body("{}"))

    make(h).complete(req())
    assert calls["n"] == 2


def test_system_prompt_marked_for_caching_and_google_prefers_ai_studio():
    from ncte.llm.fal_openrouter import FalOpenRouterClient

    req = LLMRequest(stage="translate", model="google/gemini-3.8-flash", schema_name="t", json_schema={"type": "object"},
                     messages=[{"role": "system", "content": "rules"}, {"role": "user", "content": "{}"}])
    body = OpenRouterClient("k").build_payload(req)
    assert body["messages"][0]["content"] == [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}]
    assert body["messages"][1]["content"] == "{}"  # the data stays plain, after the cached part
    assert body["provider"] == {"require_parameters": True, "order": ["google-ai-studio"], "allow_fallbacks": True}
    assert req.messages[0]["content"] == "rules"  # the request itself (and its hash) is unchanged
    other = OpenRouterClient("k").build_payload(LLMRequest(stage="t", model="openai/gpt-6-luna", schema_name="t",
                                                            messages=[{"role": "system", "content": "r"}]))
    assert "order" not in other.get("provider", {})
    fal = FalOpenRouterClient("k").build_payload(req)  # not verified through fal: sent as before
    assert fal["messages"][0]["content"] == "rules" and "order" not in fal["provider"]
