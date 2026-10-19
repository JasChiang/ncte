"""A model served on this computer (LM Studio, llama-server, mlx_lm.server…) through its OpenAI-compatible API.

Measured on 2026-10-19 (M4 Pro, Qwen3.6-35B-A3B, Gemma 4 26B-A4B): what a local server needs that OpenRouter does not.

* **Thinking is switched off by the chat template, not by a request field.** LM Studio ignores
  ``chat_template_kwargs``; a thinking model then reasons for thousands of tokens and never answers a 50-paragraph
  batch. So when the model's ``chat_template.jinja`` (or the template inside its GGUF) can be found, the prompt is
  rendered here with ``enable_thinking`` and sent to ``/completions``; the server's own ``response_format``
  (grammar) still applies there. Thinking is on only when the step asks for a reasoning level.
* **Nothing is billed**: prices are 0, there is no ``/generation`` to reconcile, ``cost_usd`` is 0.
* **Answers take minutes**, not seconds: a translation batch is 1,000–3,000 output tokens at 40–70 tokens/s.
* **A 4-bit model slips on JSON quotes** (writes ``」`` for the closing ``"``) unless the answer rule says so.

Model ids are ``local/<server id>``; ``local/default`` is the model the server has loaded (else the first it lists).
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from typing import Any

import httpx

from ..errors import LLMError
from .base import LLMRequest, LLMResponse
from .openrouter import OpenRouterClient

PREFIX = "local/"
DEFAULT_ALIAS = PREFIX + "default"
DEFAULT_BASE_URL = "http://localhost:1234"
DEFAULT_MODELS_DIR = "~/.lmstudio/models"
# Every parameter NCTE may send: a local server takes them all (response_format is a grammar on LM Studio and
# llama-server; "reasoning" is honoured here through the chat template)
PARAMS = ["temperature", "max_tokens", "response_format", "structured_outputs", "reasoning"]
# How to answer (one JSON object, codes as given) is in every step's instructions (translator.COMPACT_JSON); a 4-bit
# local model still closes JSON strings with 」, so this adds the one line about the string delimiter. It names the
# delimiter, not the content: an earlier wording ("strings begin and end with \"") had the model start translations
# with an ASCII quote.
ANSWER_RULE = ("\n（JSON 語法提醒：字串值外面的界定符號是 JSON 的 \" 而不是 」；譯文內容裡的「」只照原文有對話引號的句子寫，"
               "敘述句不要加。）\n")


def wants_thinking(req: LLMRequest) -> bool:
    """A step that asks for a reasoning level (effort or a token cap) thinks; the default (nothing asked) does not:
    locally a thinking model spends its whole token budget before answering."""
    return bool(req.extra.get("reasoning"))


# ---------------------------------------------------------------- chat templates
def gguf_chat_template(path: Path) -> str | None:
    """``tokenizer.chat_template`` from a GGUF file's header (read key by key; the tensors are never touched)."""
    sizes = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
    with path.open("rb") as f:
        def rd(fmt: str) -> Any:
            return struct.unpack("<" + fmt, f.read(struct.calcsize(fmt)))[0]

        def rstr() -> str:
            return f.read(rd("Q")).decode("utf-8", "replace")

        def skip(t: int) -> str | None:
            if t == 8:
                return rstr()
            if t == 9:
                et, n = rd("I"), rd("Q")
                if et == 8:
                    for _ in range(n):
                        f.seek(rd("Q"), 1)
                elif et == 9:
                    for _ in range(n):
                        skip(9)
                else:
                    f.seek(struct.calcsize(sizes[et]) * n, 1)
                return None
            f.seek(struct.calcsize(sizes[t]), 1)
            return None

        if f.read(4) != b"GGUF":
            return None
        rd("I")  # version
        rd("Q")  # tensor count
        for _ in range(rd("Q")):
            key, t = rstr(), rd("I")
            value = skip(t)
            if key == "tokenizer.chat_template":
                return value
    return None


def find_template(server_id: str, models_dir: Path) -> str | None:
    """The chat template of the model the server calls ``server_id``: its folder's ``chat_template.jinja`` (MLX) or the
    template inside its GGUF. LM Studio names a model after its folder, lower-cased and without the quantization
    suffix, so the folder whose name starts with the id is the one; a path (mlx_lm.server) is used as is."""
    as_path = Path(server_id).expanduser()
    candidates = [as_path] if as_path.is_dir() else []
    if not candidates and models_dir.is_dir():
        wanted = server_id.lower()
        for pub in sorted(models_dir.iterdir()):
            if not pub.is_dir():
                continue
            for d in sorted(pub.iterdir()):
                if d.is_dir() and d.name.lower().startswith(wanted):
                    candidates.append(d)
        candidates.sort(key=lambda d: len(d.name))
    for d in candidates:
        jinja = d / "chat_template.jinja"
        if jinja.is_file():
            return jinja.read_text(encoding="utf-8")
        for g in sorted(d.glob("*.gguf")):
            try:
                if tpl := gguf_chat_template(g):
                    return tpl
            except (OSError, struct.error):
                continue
    return None


def render(template: str, messages: list[dict[str, Any]], *, thinking: bool) -> str:
    import jinja2

    env = jinja2.Environment(loader=jinja2.BaseLoader(), keep_trailing_newline=True,
                             extensions=["jinja2.ext.loopcontrols"], autoescape=False)  # noqa: S701 (not HTML)
    env.filters["tojson"] = lambda v, **kw: json.dumps(v, ensure_ascii=False)
    return env.from_string(template).render(messages=messages, add_generation_prompt=True, enable_thinking=thinking)


# ---------------------------------------------------------------- client
class LocalClient(OpenRouterClient):
    name = "local"
    catalog_source = "local"

    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, models_dir: str | None = None,
                 templates: dict[str, str] | None = None, timeout: float = 1800, **kwargs: Any):
        self.server = base_url.rstrip("/")
        super().__init__(None, base_url=self.server + "/v1", timeout=timeout, **kwargs)
        self.models_dir = Path(models_dir or os.environ.get("NCTE_LOCAL_MODELS_DIR") or DEFAULT_MODELS_DIR).expanduser()
        self.templates = dict(templates or {})  # server id → template path (set by hand when discovery fails)
        self._template_cache: dict[str, str | None] = {}
        self._ids: dict[str, str] = {}  # NCTE id → server id
        self.notes: list[str] = []

    def _auth(self) -> dict[str, str]:
        return {}

    # ------------------------------------------------------------ catalog
    def list_models(self) -> list[dict[str, Any]]:
        """The server's chat models, as the catalog NCTE expects: price 0, the context length when the server says
        it (LM Studio's /api/v0/models), every parameter accepted."""
        entries: list[dict[str, Any]] = []
        try:
            resp = self._http.get(f"{self.server}/api/v0/models")
            if resp.status_code == 200:
                entries = [m for m in resp.json().get("data", []) if m.get("type") in (None, "llm", "vlm")]
                self.base_url = f"{self.server}/api/v0"  # LM Studio: the same requests, plus speed in `stats`
            if not entries:
                resp = self._http.get(f"{self.base_url}/models")
                if resp.status_code != 200:
                    raise LLMError(f"GET /v1/models → HTTP {resp.status_code}")
                entries = list(resp.json().get("data", []))
        except httpx.HTTPError as exc:
            raise LLMError(f"no local model server at {self.server} ({exc.__class__.__name__}): start it first "
                           "(LM Studio: `lms server start`; or llama-server / mlx_lm.server)") from exc
        entries = [m for m in entries if "embed" not in str(m.get("id", "")).lower()]
        if not entries:
            raise LLMError(f"the local server at {self.server} lists no chat model")
        out: list[dict[str, Any]] = []
        for m in entries:
            sid = str(m["id"])
            self._ids[PREFIX + sid] = sid
            out.append({"id": PREFIX + sid, "name": f"{sid} on {self.server}", "context_length": m.get("max_context_length"),
                        "supported_parameters": list(PARAMS), "pricing": {"prompt": "0", "completion": "0"},
                        "local": {"state": m.get("state"), "arch": m.get("arch"), "quantization": m.get("quantization")}})
        loaded = [m for m in out if m["local"].get("state") == "loaded"]
        first = (loaded or out)[0]
        self._ids[DEFAULT_ALIAS] = self._ids[first["id"]]
        out.insert(0, {**first, "id": DEFAULT_ALIAS, "name": f"{first['name']} (local/default)"})
        return out

    def get_generation(self, generation_id: str) -> dict[str, Any] | None:
        return None

    def balance(self) -> dict[str, Any]:
        return {}

    def server_id(self, model: str) -> str:
        if not self._ids:  # first contact: learn the server's ids (and whether it is LM Studio) once
            try:
                self.list_models()
            except LLMError:
                if model == DEFAULT_ALIAS:
                    raise
        if model in self._ids:
            return self._ids[model]
        return model[len(PREFIX):] if model.startswith(PREFIX) else model

    def template_for(self, sid: str) -> str | None:
        if sid not in self._template_cache:
            tpl: str | None = None
            if path := self.templates.get(sid):
                tpl = Path(path).expanduser().read_text(encoding="utf-8")
            else:
                tpl = find_template(sid, self.models_dir)
            if tpl is None:
                self.notes.append(f"no chat template found for {sid}: thinking cannot be switched off "
                                  f"(put the model's chat_template.jinja under {self.models_dir} or set local_chat_templates)")
            self._template_cache[sid] = tpl
        return self._template_cache[sid]

    # ------------------------------------------------------------ requests
    def build_payload(self, req: LLMRequest) -> dict[str, Any]:
        sid = self.server_id(req.model)
        system = "\n".join(m["content"] for m in req.messages if m["role"] == "system") + ANSWER_RULE
        messages = [{"role": "system", "content": system}] + [dict(m) for m in req.messages if m["role"] != "system"]
        payload: dict[str, Any] = {"model": sid, "stream": False}
        if (tpl := self.template_for(sid)) is not None:
            payload["prompt"] = render(tpl, messages, thinking=wants_thinking(req))
        else:
            payload["messages"] = messages
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.max_tokens:
            payload["max_tokens"] = req.max_tokens
        if req.structured_mode == "json_schema" and req.json_schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": req.schema_name, "strict": True, "schema": req.json_schema}}
        # json_object: local servers take no such mode; the answer rule asks for JSON and it is validated here
        return payload

    def _parse(self, body: dict[str, Any], req: LLMRequest, latency: int, attempt: int) -> LLMResponse:
        choices = body.get("choices") or []
        if choices and "text" in choices[0]:  # /completions
            content = str(choices[0].get("text") or "")
            if "</think>" in content:
                content = content.split("</think>", 1)[1]
            finish = choices[0].get("finish_reason")
        else:
            resp = super()._parse(body, req, latency, attempt)
            content, finish = resp.content, resp.finish_reason
            if "</think>" in content:
                content = content.split("</think>", 1)[1]
        usage = dict(body.get("usage") or {})
        stats = body.get("stats") or {}
        for k in ("tokens_per_second", "time_to_first_token"):
            if isinstance(stats.get(k), (int, float)):
                usage[k] = round(float(stats[k]), 2)
        return LLMResponse(content=content.strip(), model=req.model, provider="local", generation_id=None,
                           usage=usage, cost_usd=0.0, latency_ms=latency, finish_reason=finish, attempts=attempt,
                           structured_mode="prompt_only" if req.structured_mode == "json_object" else req.structured_mode)
