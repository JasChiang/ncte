"""The Gemini API source (ADR-105), offline with MockTransport: request shape, structured output, thinking levels,
usage (thinking and cached tokens), retries, filters, empty answers, the catalog and its prices from OpenRouter."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ncte.config import GEMINI_MODEL, Config, load_config, use_gemini_models
from ncte.errors import (
    ContentBlockedError,
    EmptyAnswerError,
    FatalAPIError,
    NCTEError,
    RequestRejectedError,
    RetryableAPIError,
)
from ncte.llm.base import LLMRequest
from ncte.llm.cost import Pricing
from ncte.llm.gemini import GeminiClient
from ncte.llm.schemas import TranslationBatchOutput, strict_json_schema

KEY = "AIza" + "unit-test-secret-key-0123456789abc"
USAGE = {"promptTokenCount": 1200, "cachedContentTokenCount": 800, "candidatesTokenCount": 150,
         "thoughtsTokenCount": 90, "totalTokenCount": 1440}


def answer(text: str = '{"translations": []}', finish: str = "STOP", **body: Any) -> dict[str, Any]:
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}],
            "usageMetadata": USAGE, "modelVersion": "gemini-3.8-flash", "responseId": "resp-1", **body}


def make(handler, **kw) -> GeminiClient:
    return GeminiClient(KEY, transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw)


def req(**kw: Any) -> LLMRequest:
    base: dict[str, Any] = dict(
        stage="translate", model="gemini/gemini-3.8-flash", schema_name="ncte_translate",
        messages=[{"role": "system", "content": "RULES"}, {"role": "user", "content": '{"units": []}'}],
        json_schema=strict_json_schema(TranslationBatchOutput), structured_mode="json_schema", temperature=0.3,
        max_tokens=4000, extra={"reasoning": {"effort": "medium"}})
    return LLMRequest(**{**base, **kw})


def test_request_shape_structured_output_and_thinking_level():
    seen: dict[str, Any] = {}

    def h(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=answer())

    resp = make(h).complete(req())
    assert seen["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
    assert seen["key"] == KEY and KEY not in seen["url"]  # the key goes in the header, never the URL
    body = seen["body"]
    assert body["systemInstruction"] == {"parts": [{"text": "RULES"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": '{"units": []}'}]}]
    gc = body["generationConfig"]
    assert gc["responseFormat"] == {"text": {"mimeType": "APPLICATION_JSON",
                                             "schema": strict_json_schema(TranslationBatchOutput)}}
    assert "responseMimeType" not in gc and "responseSchema" not in gc
    assert gc["thinkingConfig"] == {"thinkingLevel": "MEDIUM"}
    assert gc["temperature"] == 0.3 and gc["maxOutputTokens"] == 4000
    assert resp.content == '{"translations": []}' and resp.finish_reason == "stop"
    assert resp.provider == "gemini" and resp.generation_id == "resp-1" and resp.cost_usd is None


@pytest.mark.parametrize("extra, want", [
    ({"reasoning": {"effort": "high"}}, {"thinkingLevel": "HIGH"}),
    ({"reasoning": {"effort": "low"}}, {"thinkingLevel": "LOW"}),
    ({"reasoning": {"effort": "minimal"}}, {"thinkingLevel": "MINIMAL"}),
    ({"reasoning": {"max_tokens": 2048}}, {"thinkingBudget": 2048}),
    ({}, None),  # the model's own default: nothing sent
])
def test_reasoning_levels_map_to_the_thinking_config(extra, want):
    gc = GeminiClient(KEY).build_payload(req(extra=extra))["generationConfig"]
    assert gc.get("thinkingConfig") == want


def test_json_object_mode_and_other_roles():
    p = GeminiClient(KEY).build_payload(req(structured_mode="json_object", json_schema=None, temperature=None,
                                            max_tokens=None, extra={}, messages=[
                                                {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                                                {"role": "user", "content": "c"}]))
    assert p["generationConfig"] == {"responseMimeType": "application/json"}
    assert "systemInstruction" not in p
    assert [c["role"] for c in p["contents"]] == ["user", "model", "user"]
    assert "generationConfig" not in GeminiClient(KEY).build_payload(req(
        structured_mode="prompt_only", json_schema=None, temperature=None, max_tokens=None, extra={}))


def test_usage_counts_thinking_as_output_and_keeps_cached_tokens():
    resp = make(lambda r: httpx.Response(200, json=answer())).complete(req())
    u = resp.usage
    assert u["prompt_tokens"] == 1200 and u["completion_tokens"] == 240  # answer 150 + thoughts 90
    assert u["prompt_tokens_details"] == {"cached_tokens": 800}
    assert u["completion_tokens_details"] == {"reasoning_tokens": 90}
    assert u["gemini"]["thoughtsTokenCount"] == 90 and u["model_version"] == "gemini-3.8-flash"


def test_thought_parts_are_not_part_of_the_answer():
    body = answer()
    body["candidates"][0]["content"]["parts"] = [{"text": "thinking…", "thought": True}, {"text": '{"x": 1}'}]
    assert make(lambda r: httpx.Response(200, json=body)).complete(req()).content == '{"x": 1}'


def test_429_and_5xx_are_retried_honouring_retry_after():
    calls, slept = {"n": 0}, []

    def h(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "7"},
                                  json={"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
        if calls["n"] == 2:
            return httpx.Response(503, json={"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})
        return httpx.Response(200, json=answer())

    client = GeminiClient(KEY, transport=httpx.MockTransport(h), sleep=slept.append)
    resp = client.complete(req())
    assert resp.attempts == 3 and calls["n"] == 3 and slept[0] == 7.0


def test_giving_up_after_the_attempts():
    client = make(lambda r: httpx.Response(500, json={"error": {"code": 500, "message": "boom"}}), max_attempts=2)
    with pytest.raises(RetryableAPIError, match="giving up after 2"):
        client.complete(req())


@pytest.mark.parametrize("status, err, exc", [
    (400, {"code": 400, "message": "Invalid JSON payload", "status": "INVALID_ARGUMENT"}, RequestRejectedError),
    (400, {"code": 400, "message": "billing not enabled", "status": "FAILED_PRECONDITION"}, FatalAPIError),
    (401, {"code": 401, "message": "API key not valid", "status": "UNAUTHENTICATED"}, FatalAPIError),
    (403, {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}, FatalAPIError),
    (404, {"code": 404, "message": "models/x is not found", "status": "NOT_FOUND"}, FatalAPIError),
])
def test_errors_that_are_not_retried(status, err, exc):
    calls = {"n": 0}

    def h(request):
        calls["n"] += 1
        return httpx.Response(status, json={"error": err})

    with pytest.raises(exc):
        make(h).complete(req())
    assert calls["n"] == 1


def test_the_key_is_never_in_an_error_message():
    def h(request):
        return httpx.Response(400, json={"error": {"code": 400, "message": f"bad key {KEY}",
                                                   "status": "INVALID_ARGUMENT"}})

    with pytest.raises(RequestRejectedError) as e:
        make(h).complete(req())
    assert KEY not in str(e.value)


def test_a_blocked_prompt_is_the_content_filter():
    body = {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}, "usageMetadata": {"promptTokenCount": 10}}
    with pytest.raises(ContentBlockedError, match="PROHIBITED_CONTENT"):
        make(lambda r: httpx.Response(200, json=body)).complete(req())


@pytest.mark.parametrize("finish", ["SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"])
def test_a_candidate_stopped_by_a_filter_is_the_content_filter(finish):
    with pytest.raises(ContentBlockedError, match=finish):
        make(lambda r: httpx.Response(200, json=answer('{"transl', finish=finish))).complete(req())


def test_an_account_limited_for_policy_stops_the_run():
    with pytest.raises(FatalAPIError):
        make(lambda r: httpx.Response(200, json=answer("", finish="PUP_LIMITED_DISABLED"))).complete(req())


def test_an_empty_answer_is_an_empty_answer_with_its_usage():
    with pytest.raises(EmptyAnswerError) as e:
        make(lambda r: httpx.Response(200, json=answer("  "))).complete(req())
    assert e.value.usage is not None and e.value.usage["completion_tokens"] == 240
    with pytest.raises(EmptyAnswerError):
        make(lambda r: httpx.Response(200, json={"candidates": [], "usageMetadata": USAGE})).complete(req())


def test_max_tokens_is_a_truncated_answer():
    resp = make(lambda r: httpx.Response(200, json=answer('{"transl', finish="MAX_TOKENS"))).complete(req())
    assert resp.finish_reason == "length"  # the translator raises the cap and asks again
    resp = make(lambda r: httpx.Response(200, json=answer("", finish="MAX_TOKENS"))).complete(req())
    assert resp.finish_reason == "length" and resp.content == ""


def test_no_key_stops_before_a_request():
    called = []
    client = GeminiClient(None, transport=httpx.MockTransport(lambda r: called.append(r) or httpx.Response(200)))
    with pytest.raises(FatalAPIError, match="GEMINI_API_KEY"):
        client.complete(req())
    assert not called


# ---------------------------------------------------------------- catalog and prices
OR_MODELS = {"data": [
    {"id": "google/gemini-3.8-flash", "name": "Google: Gemini 3.8 Flash", "context_length": 1048576,
     "pricing": {"prompt": "0.0000005", "completion": "0.000003", "input_cache_read": "0.00000005"},
     "supported_parameters": ["max_tokens", "temperature", "reasoning", "response_format", "structured_outputs"],
     "reasoning": {"supported_efforts": ["low", "medium", "high"], "default_effort": "medium"}},
    {"id": "openai/gpt-x", "pricing": {"prompt": "1", "completion": "1"}}]}
GEMINI_LIST = [
    {"models": [{"name": "models/gemini-3.8-flash", "displayName": "Gemini 3.8 Flash", "inputTokenLimit": 1048576,
                 "outputTokenLimit": 65536, "supportedGenerationMethods": ["generateContent", "countTokens"],
                 "thinking": True}],
     "nextPageToken": "p2"},
    {"models": [{"name": "models/gemini-new-unlisted", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/text-embedding-x", "supportedGenerationMethods": ["embedContent"]}]}]


def catalog_handler(seen: list[httpx.Request]):
    def h(request: httpx.Request):
        seen.append(request)
        if request.url.host == "openrouter.ai":
            return httpx.Response(200, json=OR_MODELS)
        page = 1 if request.url.params.get("pageToken") == "p2" else 0
        return httpx.Response(200, json=GEMINI_LIST[page])
    return h


def test_the_catalog_lists_gemini_models_with_openrouter_public_prices():
    seen: list[httpx.Request] = []
    models = make(catalog_handler(seen)).list_models()
    by_id = {m["id"]: m for m in models}
    assert set(by_id) == {"gemini/gemini-3.8-flash", "gemini/gemini-new-unlisted"}  # embeddings left out
    flash = by_id["gemini/gemini-3.8-flash"]
    assert flash["pricing"] == OR_MODELS["data"][0]["pricing"]
    assert flash["price_source"] == "OpenRouter public /models: google/gemini-3.8-flash"
    p = Pricing.from_model_json(flash)
    assert p.prompt == pytest.approx(5e-7) and p.cache_read == pytest.approx(5e-8)
    assert p.structured_mode == "json_schema" and p.reasoning_efforts == ("low", "medium", "high")
    # no key of any kind goes to OpenRouter; Google gets its key in the header
    to_or = [r for r in seen if r.url.host == "openrouter.ai"]
    assert to_or and all("authorization" not in r.headers and "x-goog-api-key" not in r.headers for r in to_or)
    assert all(r.headers["x-goog-api-key"] == KEY for r in seen if r.url.host != "openrouter.ai")
    assert len([r for r in seen if r.url.host != "openrouter.ai"]) == 2  # both pages read


def test_a_model_without_a_public_price_has_an_unknown_cost_and_is_refused():
    models = make(catalog_handler([])).list_models()
    unlisted = next(m for m in models if m["id"] == "gemini/gemini-new-unlisted")
    assert "pricing" not in unlisted
    with pytest.raises(FatalAPIError, match="cost is unknown"):
        Pricing.from_model_json(unlisted)


def test_prices_unreadable_means_every_cost_unknown_not_guessed():
    def h(request):
        if request.url.host == "openrouter.ai":
            return httpx.Response(503, json={"error": {"message": "down"}})
        return httpx.Response(200, json=GEMINI_LIST[1])

    client = make(h)
    models = client.list_models()
    assert all("pricing" not in m for m in models) and client.notes


def test_a_run_resolves_its_price_from_the_gemini_catalog(tmp_path):
    from ncte.llm.cost import latest_snapshot
    from ncte.memory import db
    from ncte.translation.orchestrator import resolve_pricing

    conn = db.connect(tmp_path / "a.db")
    client = make(catalog_handler([]))
    pricing, source = resolve_pricing(conn, client, "gemini/gemini-3.8-flash", log=lambda m: None)
    assert source == "gemini:/models (live)" and pricing.completion == pytest.approx(3e-6)
    snap = latest_snapshot(conn)
    assert snap is not None
    kept = {m["id"]: m for m in snap[1]}
    assert kept["gemini/gemini-3.8-flash"]["price_source"].startswith("OpenRouter public /models")
    assert "pricing_missing" in kept["gemini/gemini-new-unlisted"]
    with pytest.raises(FatalAPIError, match="cost is unknown"):
        resolve_pricing(conn, client, "gemini/gemini-new-unlisted", log=lambda m: None)


def test_cached_tokens_are_priced_at_the_cache_read_price():
    p = Pricing.from_model_json({**OR_MODELS["data"][0], "id": "gemini/gemini-3.8-flash"})
    full = p.cost(1200, 240)
    cached = p.cost(1200, 240, cached_tokens=800)
    assert cached == pytest.approx(400 * 5e-7 + 800 * 5e-8 + 240 * 3e-6) and cached < full


# ---------------------------------------------------------------- configuration
def test_the_gemini_source_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NCTE_LLM_PROVIDER", "gemini")
    cfg = load_config()
    assert cfg.openrouter.provider == "gemini" and cfg.openrouter.model == GEMINI_MODEL
    cfg = Config()
    cfg.openrouter.provider = "gemini"
    cfg.openrouter.stage_models = {"proofread": "google/gemini-x", "scene_analyze": "gemini/gemini-3.6-flash",
                                   "reader": "agy/gemini-flash"}
    use_gemini_models(cfg)
    assert cfg.openrouter.stage_models == {"scene_analyze": "gemini/gemini-3.6-flash"}


def test_make_client_needs_the_key_and_routes_gemini_steps(monkeypatch):
    from ncte.cli import make_client
    from ncte.llm.routing import RoutingClient

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir("/")  # no ./.env
    cfg = Config()
    cfg.openrouter.provider = "gemini"
    use_gemini_models(cfg)
    with pytest.raises(NCTEError, match="GEMINI_API_KEY"):
        make_client(cfg)
    assert isinstance(make_client(cfg, need_key=False), GeminiClient)  # a dry run lists without failing here
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    claude = Config()  # a Claude-plan run whose cross-proofreading is on the Gemini API
    claude.review.cross_proofread_model = GEMINI_MODEL
    client = make_client(claude)
    assert isinstance(client, RoutingClient) and isinstance(client.client_for(GEMINI_MODEL), GeminiClient)


@pytest.mark.parametrize("where", ["provider", "stage", "cross"])
def test_agy_anywhere_stops_before_any_request(monkeypatch, tmp_path, where):
    from ncte.cli import make_client
    from ncte.config import AGY_DISABLED

    monkeypatch.chdir(tmp_path)
    if where == "provider":
        monkeypatch.setenv("NCTE_LLM_PROVIDER", "agy")
    elif where == "cross":
        monkeypatch.setenv("NCTE_CROSS_PROOFREAD", "agy/gemini-flash")
    cfg = load_config()
    if where == "stage":
        cfg.openrouter.stage_models["proofread"] = "agy/gemini-flash"
    with pytest.raises(NCTEError) as e:
        make_client(cfg)
    assert str(e.value) == AGY_DISABLED
    assert "https://antigravity.google/docs/faq/" in AGY_DISABLED
