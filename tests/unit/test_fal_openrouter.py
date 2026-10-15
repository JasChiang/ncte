import json

import httpx
import pytest

from ncte.cli import make_client
from ncte.config import Config, get_api_key, load_config, redact
from ncte.errors import FatalAPIError
from ncte.llm.base import LLMRequest
from ncte.llm.fal_openrouter import FalOpenRouterClient
from ncte.llm.openrouter import OpenRouterClient

KEY = "unit-test-fal:secret-only"


def test_fal_chat_and_catalog_are_separate():
    seen = []

    def handle(r):
        seen.append(r)
        if r.url.host == "openrouter.ai":
            return httpx.Response(200, json={"data": [{"id": "test/model"}]})
        return httpx.Response(200, json={"id": "gen-fal", "model": "test/model",
            "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
            "usage": {"cost": 0.002, "prompt_tokens": 12, "completion_tokens": 8}})

    client = FalOpenRouterClient(KEY, transport=httpx.MockTransport(handle))
    req = LLMRequest(stage="translate", model="test/model", messages=[{"role": "user", "content": "test"}],
                     schema_name="test", json_schema={"type": "object"},
                     extra={"reasoning": {"effort": "low"}}, max_tokens=100)
    resp = client.complete(req)
    assert resp.cost_usd == 0.002 and resp.generation_id == "gen-fal"
    assert str(seen[0].url) == "https://fal.run/openrouter/router/openai/v1/chat/completions"
    assert seen[0].headers["authorization"] == f"Key {KEY}"
    body = json.loads(seen[0].content)
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["reasoning"] == {"effort": "low"} and body["max_tokens"] == 100
    assert client.list_models() == [{"id": "test/model"}]
    assert "authorization" not in seen[1].headers and KEY not in str(seen[1].url)
    assert client.balance() == {} and client.get_generation("gen-fal") is None
    assert len(seen) == 2


def test_fal_retry_and_secret_redaction():
    calls = []

    def handle(r):
        calls.append(r)
        return httpx.Response(429 if len(calls) == 1 else 401, json={"error": {"message": KEY}})

    with pytest.raises(FatalAPIError) as exc:
        FalOpenRouterClient(KEY, transport=httpx.MockTransport(handle), sleep=lambda _: None).complete(
            LLMRequest(stage="test", model="test/model", messages=[], schema_name="test"))
    assert len(calls) == 2 and KEY not in str(exc.value)


def test_factory_selects_credentials_without_network(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "direct-secret")
    monkeypatch.setenv("FAL_KEY", KEY)
    cfg = Config()
    direct = make_client(cfg)
    assert type(direct) is OpenRouterClient and direct.api_key == "direct-secret"
    cfg.openrouter.provider = "fal"
    fal = make_client(cfg)
    assert isinstance(fal, FalOpenRouterClient) and fal.api_key == KEY
    assert make_client(cfg, fake=True).name == "fake"
    assert get_api_key("fal") == KEY and KEY not in redact(f"error {KEY}")
    monkeypatch.delenv("FAL_KEY")
    with pytest.raises(Exception, match="FAL_KEY is not set"):
        make_client(cfg)


def test_provider_setting_and_legacy_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NCTE_LLM_PROVIDER", raising=False)
    assert load_config().openrouter.provider == "openrouter"
    (tmp_path / ".env").write_text("NCTE_LLM_PROVIDER=fal\nFAL_KEY=" + KEY)
    assert load_config().openrouter.provider == "fal"
    assert get_api_key("fal") == KEY


def test_cost_history_does_not_cross_routes(conn, cfg, fixtures):
    from ncte.llm.fake import FakeLLM
    from ncte.translation.dryrun import calibrate
    from ncte.translation.orchestrator import reconcile_costs

    from ..helpers import run_book

    book, rid, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM())
    kw = {"min_requests": 1, "min_chars": 1}
    assert calibrate([conn], cfg.openrouter.model, None, **kw)
    assert not calibrate([conn], cfg.openrouter.model, None, provider="fal", **kw)
    row = conn.execute("SELECT config_json FROM runs WHERE id=?", (rid,)).fetchone()
    data = json.loads(row[0])
    data["openrouter"]["provider"] = "fal"
    conn.execute("UPDATE runs SET config_json=? WHERE id=?", (json.dumps(data), rid))
    assert not calibrate([conn], cfg.openrouter.model, None, **kw)
    assert calibrate([conn], cfg.openrouter.model, None, provider="fal", **kw)
    conn.execute("UPDATE llm_requests SET generation_id='gen-fal', cost_status='unknown' WHERE run_id=?", (rid,))

    def no_network(request):
        raise AssertionError(f"fal generation leaked to direct API: {request.url}")

    assert reconcile_costs(conn, OpenRouterClient("test", transport=httpx.MockTransport(no_network)), run_id=rid) == 0
    del data["openrouter"]["provider"]
    conn.execute("UPDATE runs SET config_json=? WHERE id=?", (json.dumps(data), rid))
    # Legacy run records are still treated as direct OpenRouter.
    assert calibrate([conn], cfg.openrouter.model, None, **kw) == {}  # unknown costs excluded
    conn.execute("UPDATE llm_requests SET cost_status='reported' WHERE run_id=?", (rid,))
    assert calibrate([conn], cfg.openrouter.model, None, **kw)
