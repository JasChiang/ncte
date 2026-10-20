"""One client per model prefix: ``claude/…`` to the Claude plan, ``local/…`` to the local server, ``codex/…`` to the
ChatGPT plan through Codex, the rest to the run's source. Lets a run read on one source and translate on another (``stage_models``) without the steps knowing."""

from __future__ import annotations

from typing import Any

from .base import LLMClient, LLMRequest, LLMResponse


class RoutingClient:
    def __init__(self, primary: LLMClient, routes: dict[str, LLMClient]):
        self.primary = primary
        self.routes = dict(routes)  # prefix → client
        self.name = primary.name  # the run's source: what the records and the provider-specific paths see
        self.catalog_source = getattr(primary, "catalog_source", primary.name)

    def client_for(self, model: str) -> LLMClient:
        for prefix, client in self.routes.items():
            if model.startswith(prefix):
                return client
        return self.primary

    def complete(self, req: LLMRequest) -> LLMResponse:
        return self.client_for(req.model).complete(req)

    def list_models(self) -> list[dict[str, Any]]:
        out = list(self.primary.list_models())
        for client in self.routes.values():
            if client is not self.primary:
                out.extend(client.list_models())
        return out

    def get_generation(self, generation_id: str) -> dict[str, Any] | None:
        return self.primary.get_generation(generation_id)

    def balance(self) -> dict[str, Any]:
        fn = getattr(self.primary, "balance", None)
        return fn() if fn else {}

    @property
    def notes(self) -> list[str]:
        out: list[str] = []
        for client in (self.primary, *self.routes.values()):
            out.extend(getattr(client, "notes", []))
        return out
