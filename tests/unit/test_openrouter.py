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


def test_shared_rules_cached_and_the_batch_rules_after():
    from ncte.llm.prompt_loader import load_prompt

    p = load_prompt("translate")
    a_shared, a_own = p.render_parts({"units": [], "narration": {"person": "third"}})
    b_shared, b_own = p.render_parts({"units": [{"source": "⟦1⟧強調⟦/1⟧"}]})
    assert a_shared == b_shared and "narration" not in a_shared and "## 翻譯規則" in a_shared
    assert "`narration`" in a_own and "⟦數字⟧" in b_own and "`narration`" not in b_own
    assert p.render_parts({"units": []})[1] == ""
    system = a_shared + "\n" + a_own
    req = LLMRequest(stage="translate", model="google/gemini-3.8-flash", schema_name="t", cached_prefix_chars=len(a_shared),
                     messages=[{"role": "system", "content": system}, {"role": "user", "content": "{}"}])
    parts = OpenRouterClient("k").build_payload(req)["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": a_shared, "cache_control": {"type": "ephemeral"}}
    assert parts[1]["text"].strip() == a_own.strip() and "cache_control" not in parts[1]


def _client(responses):
    seen = []

    def handle(r):
        seen.append(r)
        status, body, headers = responses[min(len(seen) - 1, len(responses) - 1)]
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    return OpenRouterClient(KEY, transport=httpx.MockTransport(handle), sleep=lambda _: None), seen


def _req():
    return LLMRequest(stage="scene_analyze", model="m", messages=[{"role": "user", "content": "{}"}], schema_name="t")


OK = (200, {"id": "g", "model": "m", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"cost": 0.001, "prompt_tokens": 1, "completion_tokens": 1}}, {})


def test_error_forms_from_fal_and_others():
    from ncte.errors import ContentBlockedError

    # fal: {"detail": "...PROHIBITED_CONTENT"} on HTTP 400 → content filter (so the reading is split and retried)
    c, _ = _client([(400, {"detail": "Gemini blocked the request: PROHIBITED_CONTENT"}, {})])
    with pytest.raises(ContentBlockedError):
        c.complete(_req())
    # 403 + detail content filter: a passage, not the account
    c, _ = _client([(403, {"detail": "PROHIBITED_CONTENT"}, {})])
    with pytest.raises(ContentBlockedError):
        c.complete(_req())
    # an account problem is never taken for a passage to split
    c, _ = _client([(403, {"error": {"message": "Account blocked"}}, {})])
    with pytest.raises(FatalAPIError):
        c.complete(_req())
    # 400 + detail overloaded: transient, retried
    c, seen = _client([(400, {"detail": "The model is overloaded"}, {}), OK])
    assert c.complete(_req()).cost_usd == 0.001 and len(seen) == 2
    # 200 + error string with PROHIBITED_CONTENT: content filter, not resent
    c, seen = _client([(200, {"error": "PROHIBITED_CONTENT"}, {})])
    with pytest.raises(ContentBlockedError):
        c.complete(_req())
    assert len(seen) == 1
    # 200 + finish_reason content_filter with no content: content filter, not a JSON problem
    c, _ = _client([(200, {"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]}, {})])
    with pytest.raises(ContentBlockedError):
        c.complete(_req())
    # 200 + a JSON list: retried like any malformed answer
    c, seen = _client([(200, [1, 2], {}), OK])
    resp = c.complete(_req())
    assert len(seen) == 2 and resp.maybe_billed_attempts == 1


def test_in_flight_credit_waits_instead_of_stopping():
    body = {"error": {"message": "This request would exceed your available credits given your current in-flight requests",
                      "code": 402, "metadata": {"reason": "in_flight_budget_exhausted"}}}
    c, seen = _client([(402, body, {"retry-after": "1"}), OK])
    assert c.complete(_req()).cost_usd == 0.001 and len(seen) == 2
    c, _ = _client([(402, {"error": {"message": "Insufficient credits", "code": 402}}, {})])
    with pytest.raises(FatalAPIError):
        c.complete(_req())


def test_a_timed_out_attempt_is_kept_as_possibly_billed(conn, cfg, fixtures):
    from ncte.llm.fake import FakeLLM

    from ..helpers import run_book

    class Flaky(FakeLLM):
        def complete(self, req):
            r = super().complete(req)
            if req.stage == "translate" and not getattr(self, "_once", False):
                self._once = True
                r.maybe_billed_attempts = 1
            return r

    book, rid, s = run_book(conn, cfg, fixtures["basic"], Flaky())
    extra = conn.execute("SELECT COUNT(*) FROM llm_requests WHERE run_id=? AND cost_status='unknown' AND "
                         "error LIKE 'earlier attempt timed out%'", (rid,)).fetchone()[0]
    assert extra == 1 and s.unknown_cost_requests >= 1
