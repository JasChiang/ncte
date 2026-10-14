"""Configuration loading (spec §3.2).

Priority: explicit --config file > ./ncte.yaml > ./config.yaml > built-in defaults.
The API key is *only* read from the OPENROUTER_API_KEY environment variable or a local
``.env`` file; it is never stored in the config model dump, logs or reports.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .errors import ConfigError

DEFAULT_MODEL = "google/gemini-3.8-flash"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppConfig(_Strict):
    workspace: str = "./workspace"
    language: str = "zh-TW"
    mode: Literal["baseline", "context"] = "baseline"
    lookahead_scope: Literal["none", "current_scene"] = "current_scene"


class OpenRouterConfig(_Strict):
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = DEFAULT_MODEL
    review_model: str | None = None
    fallback_models: list[str] = Field(default_factory=list)
    timeout_seconds: float = 120
    max_concurrency: int = Field(2, ge=1, le=16)
    max_attempts: int = Field(4, ge=1, le=10)
    per_book_budget_usd: float = Field(5.0, ge=0)
    require_schema: bool = True
    temperature: float | None = 0.3
    # OpenRouter `reasoning.effort` (e.g. low | medium | high); None = model default. Reasoning tokens are
    # billed as output tokens. Validated against the model's `reasoning.supported_efforts` from /models.
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None
    app_title: str = "NCTE"


class TranslationConfig(_Strict):
    style: str = "natural_traditional_taiwan"
    unit_target_japanese_chars: int = Field(1400, ge=1)
    unit_hard_max_japanese_chars: int = Field(2500, ge=100)
    include_previous_units: int = Field(2, ge=0, le=20)
    include_next_units: int = Field(1, ge=0, le=20)
    preserve_ambiguity: bool = True
    skip_already_translated: bool = True


class ReviewConfig(_Strict):
    automatic_review: Literal["off", "flagged_only", "all"] = "flagged_only"
    maximum_review_passes: int = Field(1, ge=0, le=1)


class MemoryConfig(_Strict):
    fts_enabled: bool = True
    vector_rag_enabled: bool = False
    max_retrieved_evidence: int = Field(8, ge=0, le=50)
    future_knowledge_policy: Literal["up_to_current_scene"] = "up_to_current_scene"
    max_scene_units: int = Field(80, ge=5)


class ContextConfig(_Strict):
    """Feature switches for --mode context (also used by A/B experiments)."""

    scene_analysis: bool = True  # LLM scene / speaker analysis (program-only quote analysis always runs)
    memory_update: bool = True  # LLM memory extraction after each scene
    include_memory: bool = True  # inject characters / summaries / facts into translation requests
    read_first: bool = True  # ADR-023: analyse + remember every scene first, then name, then translate
    naming: Literal["review", "auto", "off"] = "review"  # ADR-023: propose renderings; review = pause for choices


class ExportConfig(_Strict):
    translate_nav_titles: bool = True
    preserve_assets: bool = True
    epubcheck_path: str | None = None


class Config(_Strict):
    app: AppConfig = Field(default_factory=lambda: AppConfig())
    openrouter: OpenRouterConfig = Field(default_factory=lambda: OpenRouterConfig())
    translation: TranslationConfig = Field(default_factory=lambda: TranslationConfig())
    review: ReviewConfig = Field(default_factory=lambda: ReviewConfig())
    memory: MemoryConfig = Field(default_factory=lambda: MemoryConfig())
    context: ContextConfig = Field(default_factory=lambda: ContextConfig())
    export: ExportConfig = Field(default_factory=lambda: ExportConfig())

    @property
    def workspace(self) -> Path:
        return Path(self.app.workspace).expanduser().resolve()

    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def load_config(path: str | Path | None = None, *, workspace: str | None = None) -> Config:
    candidates: list[Path] = []
    if path:
        p = Path(path).expanduser()
        if not p.exists():
            raise ConfigError(f"Config file not found: {p}")
        candidates = [p]
    else:
        candidates = [p for p in (Path("ncte.yaml"), Path("config.yaml")) if p.exists()]
    data: dict = {}
    if candidates:
        try:
            loaded = yaml.safe_load(candidates[0].read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in {candidates[0]}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"Config root must be a mapping: {candidates[0]}")
        data = loaded
    try:
        cfg = Config.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError
        raise ConfigError(f"Invalid configuration: {exc}") from exc
    if workspace:
        cfg.app.workspace = workspace
    env_ws = os.environ.get("NCTE_WORKSPACE")
    if env_ws and not workspace:
        cfg.app.workspace = env_ws
    return cfg


def _read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get_api_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key.strip() or None
    return _read_dotenv(Path(".env")).get("OPENROUTER_API_KEY") or None


def redact(text: str, key: str | None = None) -> str:
    """Remove the API key (and anything that looks like an OpenRouter key) from text."""
    import re

    if key:
        text = text.replace(key, "***")
    return re.sub(r"sk-or-[A-Za-z0-9_\-]{8,}", "sk-or-***", text)
