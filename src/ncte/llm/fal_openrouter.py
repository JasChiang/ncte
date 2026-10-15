"""OpenRouter chat through fal's OpenAI-compatible endpoint.

Only chat requests use fal authentication. Catalog/pricing is public OpenRouter
metadata, not a promise of fal availability or final billing. No fal credentials
are ever forwarded to OpenRouter. Account and generation APIs are not assumed.
"""
from __future__ import annotations

from typing import Any

from ..errors import FatalAPIError
from .openrouter import OpenRouterClient


class FalOpenRouterClient(OpenRouterClient):
    name = "fal"
    catalog_source = "openrouter"

    def __init__(self, api_key: str | None, **kwargs: Any):
        super().__init__(api_key, base_url="https://fal.run/openrouter/router/openai/v1", **kwargs)

    def _auth(self) -> dict[str, str]:
        if not self.api_key:
            raise FatalAPIError("FAL_KEY is not set")
        return {"Authorization": f"Key {self.api_key}"}

    def _catalog(self) -> tuple[str, dict[str, str]]:
        return "https://openrouter.ai/api/v1/models", {}

    def get_generation(self, generation_id: str) -> dict[str, Any] | None:
        return None

    def balance(self) -> dict[str, Any]:
        return {}
