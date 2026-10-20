"""A model served on this computer (`local` source): the catalog, the rendered prompt with thinking off, the grammar,
the answer, templates from a folder or a GGUF header, and routing a step to another source."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import httpx
import pytest

from ncte.cli import make_client
from ncte.config import Config, load_config, use_local_models
from ncte.errors import LLMError
from ncte.llm.base import LLMRequest
from ncte.llm.local import ANSWER_RULE, LocalClient, find_template, gguf_chat_template
from ncte.llm.routing import RoutingClient

TEMPLATE = ("{%- for m in messages %}{{ '<|im_start|>' + m.role + '\\n' + m.content + '<|im_end|>\\n' }}{%- endfor %}"
            "{%- if add_generation_prompt %}{{ '<|im_start|>assistant\\n' }}"
            "{%- if enable_thinking is defined and enable_thinking is false %}{{ '<think>\\n\\n</think>\\n\\n' }}"
            "{%- else %}{{ '<think>\\n' }}{%- endif %}{%- endif %}")
LMS_MODELS = {"data": [
    {"id": "text-embedding-nomic", "type": "embeddings", "state": "not-loaded"},
    {"id": "qwen3.6-35b-a3b-mlx", "type": "llm", "state": "not-loaded", "max_context_length": 262144, "arch": "qwen3_5_moe"},
    {"id": "gemma-4-26b", "type": "llm", "state": "loaded", "max_context_length": 131072}]}


def _models_dir(tmp_path: Path) -> Path:
    d = tmp_path / "models" / "lmstudio-community" / "Qwen3.6-35B-A3B-MLX-4bit"
    d.mkdir(parents=True)
    (d / "chat_template.jinja").write_text(TEMPLATE, encoding="utf-8")
    return tmp_path / "models"


def _req(**kw: Any) -> LLMRequest:
    base = {"stage": "translate", "model": "local/qwen3.6-35b-a3b-mlx", "schema_name": "ncte_translate",
            "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": '{"units": []}'}],
            "json_schema": {"type": "object", "properties": {}, "additionalProperties": False}, "temperature": 0.3,
            "max_tokens": 4000}
    return LLMRequest(**{**base, **kw})


def _server(seen: list[httpx.Request], text: str = '{"translations": []}'):
    def handle(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        if r.url.path == "/api/v0/models":
            return httpx.Response(200, json=LMS_MODELS)
        if r.url.path in ("/v1/completions", "/api/v0/completions"):
            return httpx.Response(200, json={"choices": [{"text": text, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 6877, "completion_tokens": 2455},
                                             "stats": {"tokens_per_second": 70.6, "time_to_first_token": 1.17}})
        if r.url.path in ("/v1/chat/completions", "/api/v0/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        return httpx.Response(404, json={"error": "no"})
    return handle


def test_catalog_lists_chat_models_at_price_zero_with_a_default_alias(tmp_path):
    seen: list[httpx.Request] = []
    c = LocalClient("http://localhost:1234/", models_dir=str(_models_dir(tmp_path)), transport=httpx.MockTransport(_server(seen)))
    models = c.list_models()
    ids = [m["id"] for m in models]
    assert ids == ["local/default", "local/qwen3.6-35b-a3b-mlx", "local/gemma-4-26b"]  # the embedding model is not a chat model
    assert all(m["pricing"] == {"prompt": "0", "completion": "0"} for m in models)
    assert "structured_outputs" in models[1]["supported_parameters"] and models[1]["context_length"] == 262144
    assert c.server_id("local/default") == "gemma-4-26b"  # the loaded one
    assert "authorization" not in seen[0].headers


def test_the_prompt_is_rendered_with_thinking_off_and_sent_with_the_grammar(tmp_path):
    seen: list[httpx.Request] = []
    c = LocalClient(models_dir=str(_models_dir(tmp_path)), transport=httpx.MockTransport(_server(seen, "<think>\n\n</think>\n\n{\"translations\": []}")))
    r = c.complete(_req())
    body = json.loads(seen[-1].content)
    assert seen[-1].url.path == "/api/v0/completions" and body["model"] == "qwen3.6-35b-a3b-mlx"  # LM Studio: stats
    assert body["prompt"].endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")  # thinking off: nothing asked
    assert "SYS" + ANSWER_RULE in body["prompt"] and '{"units": []}' in body["prompt"]
    assert body["response_format"]["json_schema"]["strict"] is True and "provider" not in body and "reasoning" not in body
    assert body["temperature"] == 0.3 and body["max_tokens"] == 4000
    assert r.content == '{"translations": []}' and r.cost_usd == 0.0 and r.provider == "local"
    assert r.usage["completion_tokens"] == 2455 and r.usage["tokens_per_second"] == 70.6
    assert r.generation_id is None and r.structured_mode == "json_schema" and c.get_generation("x") is None
    c.complete(_req(extra={"reasoning": {"effort": "high"}}))  # a step that asks to think: the template opens <think>
    assert json.loads(seen[-1].content)["prompt"].endswith("<|im_start|>assistant\n<think>\n")


def test_json_object_sends_no_format_and_is_recorded_as_prompt_only(tmp_path):
    seen: list[httpx.Request] = []
    c = LocalClient(models_dir=str(_models_dir(tmp_path)), transport=httpx.MockTransport(_server(seen)))
    r = c.complete(_req(structured_mode="json_object", json_schema=None))
    assert "response_format" not in json.loads(seen[-1].content) and r.structured_mode == "prompt_only"


def test_without_a_template_the_chat_endpoint_is_used_and_noted(tmp_path):
    seen: list[httpx.Request] = []
    c = LocalClient(models_dir=str(tmp_path / "nothing"), transport=httpx.MockTransport(_server(seen)))
    r = c.complete(_req(model="local/gemma-4-26b"))
    body = json.loads(seen[-1].content)
    assert seen[-1].url.path.endswith("/chat/completions") and body["messages"][0]["content"].endswith(ANSWER_RULE)
    assert r.content == '{"translations": []}' and any("gemma-4-26b" in n for n in c.notes)


def test_no_server_is_a_clear_error():
    def refuse(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="no local model server"):
        LocalClient(transport=httpx.MockTransport(refuse)).list_models()


def _gguf(path: Path, template: str) -> None:
    def s(x: str) -> bytes:
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    kv = [(s("general.architecture") + struct.pack("<I", 8) + s("qwen3")),
          (s("tokenizer.ggml.tokens") + struct.pack("<I", 9) + struct.pack("<I", 8) + struct.pack("<Q", 3) + s("a") + s("b") + s("c")),
          (s("tokenizer.ggml.scores") + struct.pack("<I", 9) + struct.pack("<I", 6) + struct.pack("<Q", 2) + struct.pack("<ff", 1.0, 2.0)),
          (s("qwen3.block_count") + struct.pack("<I", 4) + struct.pack("<I", 36)),
          (s("tokenizer.chat_template") + struct.pack("<I", 8) + s(template))]
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(kv)) + b"".join(kv) + b"\0" * 64)


def test_templates_come_from_the_folder_or_the_gguf_header(tmp_path):
    models = _models_dir(tmp_path)
    g = models / "mradermacher" / "Murasaki-4B-v0.3-GGUF"
    g.mkdir(parents=True)
    _gguf(g / "Murasaki-4B-v0.3.Q8_0.gguf", "GGUF-TEMPLATE {{ messages[0].content }}")
    assert gguf_chat_template(g / "Murasaki-4B-v0.3.Q8_0.gguf").startswith("GGUF-TEMPLATE")
    assert find_template("qwen3.6-35b-a3b-mlx", models) == TEMPLATE  # LM Studio's id: the folder name without the quant
    assert find_template("murasaki-4b-v0.3", models).startswith("GGUF-TEMPLATE")
    assert find_template(str(models / "lmstudio-community" / "Qwen3.6-35B-A3B-MLX-4bit"), models) == TEMPLATE  # a path (mlx_lm)
    assert find_template("unknown-model", models) is None


def test_config_local_source_and_routing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NCTE_LLM_PROVIDER", "local")
    cfg = load_config()
    assert cfg.openrouter.provider == "local" and cfg.openrouter.model == "local/default"
    assert cfg.openrouter.reasoning_effort is None  # thinking off unless a step asks
    client = make_client(cfg)
    assert isinstance(client, LocalClient) and client.server == "http://localhost:1234"
    # a Claude step on a local run, and a local step on a Claude run, get their own client by prefix
    cfg.openrouter.stage_models = {"proofread": "claude/opus"}
    routed = make_client(cfg)
    assert isinstance(routed, RoutingClient) and routed.name == "local"
    assert routed.client_for("claude/opus").name == "claude" and routed.client_for("local/default").name == "local"
    c2 = Config()
    c2.openrouter.provider = "claude"
    c2.openrouter.stage_models = {"scene_analyze": "local/qwen"}
    from ncte.config import use_claude_models
    use_claude_models(c2)
    assert c2.openrouter.stage_models["scene_analyze"] == "local/qwen"  # kept: it names its own source
    assert c2.openrouter.stage_models["memory_update"] == "claude/sonnet"  # the other reading steps as by default
    routed2 = make_client(c2)
    assert routed2.name == "claude" and routed2.client_for("local/qwen").name == "local"
    c3 = Config()
    c3.openrouter.provider = "local"
    c3.openrouter.model = "google/gemini-3.8-flash"
    c3.openrouter.stage_models = {"verify": "google/gemini-3.8-flash", "reader": "claude/sonnet"}
    use_local_models(c3)
    assert c3.openrouter.model == "local/default" and c3.openrouter.stage_models == {"reader": "claude/sonnet"}
