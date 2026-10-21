"""Configuration loading (spec §3.2).

Priority: explicit --config file > ./ncte.yaml > ./config.yaml > built-in defaults.
API keys are read from OPENROUTER_API_KEY (direct) or FAL_KEY (fal), or a local
``.env`` file; it is never stored in the config model dump, logs or reports.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

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
    # "claude": the user's Claude plan through Claude Code (`claude -p`), no key, nothing billed (ADR-062)
    # "agent": an agent in a conversation answers each request as a task (`ncte agent`, the MCP server)
    # "agent" (a run answered task by task) is set by `ncte agent` itself, never chosen by NCTE_LLM_PROVIDER
    # "local": a model served on this computer (LM Studio, llama-server, mlx_lm.server), nothing leaves the machine
    # "codex": the user's ChatGPT plan through the Codex CLI (`codex exec`), no key, nothing billed (ADR-071)
    # "agy": the user's Antigravity plan through the Antigravity CLI (`agy -p`), no key, nothing billed (ADR-082)
    provider: Literal["openrouter", "fal", "claude", "agent", "local", "codex", "agy"] = "claude"
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
    # per step (scenes, scene_analyze, memory_update, verify, naming, relations, translate, review, reader…):
    # "low" | "medium" | "high" | "default" (the model's own) | "max:N" (at most N reasoning tokens)
    stage_reasoning: dict[str, str] = Field(default_factory=dict)
    # per step, another model (checked against /models at start, priced on its own): a step that judges whether
    # something is wrong (fix_check, proofread, review…) need not share the translator's blind spots, and reading
    # steps may run on a cheaper model (the Claude provider reads on Sonnet, ADR-062). "translate" here is refused:
    # the run's model translates (one voice).
    stage_models: dict[str, str] = Field(default_factory=dict)
    max_output_tokens: int | None = None
    app_title: str = "NCTE"
    # the local source: the server's OpenAI-compatible address, where model folders are (their chat templates switch
    # thinking off), and a template file per server model id when discovery finds none
    local_base_url: str = "http://localhost:1234"
    local_models_dir: str | None = None
    local_chat_templates: dict[str, str] = Field(default_factory=dict)


class TranslationConfig(_Strict):
    style: str = "natural_traditional_taiwan"
    unit_target_japanese_chars: int = Field(1400, ge=1)
    # context mode: a whole scene per translation request up to this many Japanese characters (longer scenes are
    # split at a quiet point, each part seeing the whole scene); 0 = batches of unit_target_japanese_chars
    scene_max_chars: int = Field(6000, ge=0)
    unit_hard_max_japanese_chars: int = Field(2500, ge=100)
    include_previous_units: int = Field(2, ge=0, le=20)
    # a request that begins a scene sees the end of the scene before (its source, and its translation when there is
    # one) up to this many Japanese characters (at least include_previous_units paragraphs): two paragraphs were often
    # a title page or one line, and the scene goes on from there (a chapter that opens mid-action)
    scene_start_context_chars: int = Field(600, ge=0, le=5000)
    include_next_units: int = Field(1, ge=0, le=20)
    # retranslating chosen units: the rest of their scene is given as context (not retranslated), up to this many
    # Japanese characters on each side — a lone short sentence otherwise loses the scene it belongs to
    targeted_scene_context_chars: int = Field(2500, ge=0, le=20000)
    # a fix (after review / proofreading / the reader's look): "window" = the paragraphs around it, up to
    # fix_context_chars Japanese characters a side (at least two paragraphs when there are), and only the memory of
    # that window (who is in it, its lines, its terms); "scene" = as much of the scene as a retranslation of chosen
    # units sees, and the people of the whole scene (the earlier behaviour, kept to compare)
    fix_scope: Literal["window", "scene"] = "window"
    fix_context_chars: int = Field(1200, ge=0, le=20000)
    # "window": a scene's fixes go in one request, each with its own window; split into even parts past this many
    # paragraphs, or past this many Japanese characters of the paragraphs and their windows
    fix_batch_units: int = Field(12, ge=1, le=200)
    fix_batch_chars: int = Field(8000, ge=1, le=100000)
    preserve_ambiguity: bool = True
    skip_already_translated: bool = True
    # ADR-064: before its translations the model lists where later paragraphs of the batch constrain how an earlier
    # line is translated (the answer's first field, so the batch is read through first); kept in the notes of the
    # request's first paragraph for a reviewer, never acted on. Off until a check shows it helps (instructions §5.4)
    read_through: bool = False
    # the memory blocks of a request in fewer characters, nothing the prompts use left out: a line's quote and a fact's
    # source as short excerpts, defaults omitted (a paragraph's only line, a stated speaker), two speech examples a
    # character, no volume on a form of address; off: the earlier form, kept to compare
    compact_payload: bool = True


class ReviewConfig(_Strict):
    automatic_review: Literal["off", "flagged_only", "all"] = "flagged_only"
    maximum_review_passes: int = Field(1, ge=0, le=1)
    # a fix made after review / proofreading / the reader's look is checked against the source and the issue it was
    # meant to fix; off: a fix is kept but stays unverified (never counted as fixed)
    fix_check: bool = True
    # ADR-082: the last proofreading pass on another company's model (``agy/gemini-flash``), whatever the run's
    # source; off unless chosen (it spends another plan's quota and sends the book to that company). Fixes and their
    # checks stay on the run's own model. Also NCTE_CROSS_PROOFREAD (empty or "off": none)
    cross_proofread_model: str | None = None


class MemoryConfig(_Strict):
    fts_enabled: bool = True
    vector_rag_enabled: bool = False
    max_retrieved_evidence: int = Field(5, ge=0, le=50)  # facts per 25 paragraphs of a batch (up to 4×), most related first
    future_knowledge_policy: Literal["up_to_current_scene"] = "up_to_current_scene"
    max_scene_units: int = Field(80, ge=5)


class ContextConfig(_Strict):
    """Feature switches for --mode context (also used by A/B experiments)."""

    scene_analysis: bool = True  # LLM scene / speaker analysis (program-only quote analysis always runs)
    memory_update: bool = True  # LLM memory extraction after each scene
    include_memory: bool = True  # inject characters / summaries / facts into translation requests
    # ADR-036: inside a long stretch the author did not divide, the model chooses where scenes begin (program: size)
    scene_breaks: Literal["llm", "program"] = "llm"
    # ADR-038: after translating, read the Chinese like a reader; what a reader cannot follow is fixed with the source
    reader_pass: bool = True
    # ADR-051: after translating, every paragraph is checked against its source; real errors go back to the translator
    proofread: bool = True
    proofread_chunk_units: int = 60  # paragraphs per proofreading request
    # ADR-066: passes over the whole book; each later one checks the current translation again after the one before
    # has retranslated and checked its fixes (a pass found about half of what three found together). None: the
    # source's default (``proofread_passes``): 2 on the Claude plan, 1 where a pass costs money
    proofread_passes: int | None = Field(None, ge=1, le=3)
    # each paragraph gets a verdict (what the source says, and whether the translation says it), so none is skipped
    proofread_verdicts: bool = False
    read_first: bool = True  # ADR-023: analyse + remember every scene first, then name, then translate
    # ADR-065: with read_first, a scene's analysis and memory update come from one request (prompts/scene_read);
    # off until an A/B shows the quality holds
    read_in_one_request: bool = False
    # ADR-023/035: propose renderings; auto (default) = keep the model's choice after program checks and go on
    # (change them later in the web UI); review = pause for a person's choices
    naming: Literal["review", "auto", "off"] = "auto"
    # ADR-069: after reading, the model says what the book's recurring terms mean (from example sentences and the facts
    # that mention them); the definitions go with every request that translates or judges a passage where the term
    # occurs, from where their evidence is. Off: nothing is asked and no definition is sent
    term_definitions: bool = True
    # ADR-073: before defining, the model names terms the reading left out of the glossary (from facts and summaries)
    term_candidates: bool = True
    # after the second look: characters who matter and still have no gender are asked once more, with sentences
    # spread over the book where they speak, are named or are spoken to; an answer counts only with the sentences cited
    gender_gaps: bool = True
    # after the first translation of the book, before proofreading: kanji words carried over unchanged into the Chinese
    # (found by string match) are put to the model, which says which ones read otherwise in Taiwan Chinese; those get
    # a glossary entry and their paragraphs go back to the translator
    false_friends: bool = True
    # facts given with a fix in the "window" scope (``translation.fix_scope``): chosen for the fixed paragraphs and the
    # window around them
    fix_fact_quota: int = Field(4, ge=0, le=50)
    skip_book_matter: bool = True  # ADR-024: no story analysis for covers, TOC, afterword, colophon (still translated)


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


PROVIDERS = ("openrouter", "fal", "claude", "codex", "agy", "local")  # what NCTE_LLM_PROVIDER and the settings page choose


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
    provider = os.environ.get("NCTE_LLM_PROVIDER") or _read_dotenv(Path(".env")).get("NCTE_LLM_PROVIDER")
    if provider:
        if provider not in PROVIDERS:
            raise ConfigError("NCTE_LLM_PROVIDER must be openrouter, fal, claude, codex, agy or local "
                              "(agent is set by `ncte agent` itself for the runs it starts)")
        cfg.openrouter.provider = provider  # type: ignore[assignment]
    cross = os.environ.get("NCTE_CROSS_PROOFREAD")
    if cross is None:
        cross = _read_dotenv(Path(".env")).get("NCTE_CROSS_PROOFREAD")
    if cross is not None:
        cfg.review.cross_proofread_model = cross.strip() if cross.strip() not in ("", "off") else None
    if cfg.openrouter.provider == "claude":
        use_claude_models(cfg)
    elif cfg.openrouter.provider == "local":
        use_local_models(cfg)
    elif cfg.openrouter.provider == "codex":
        use_codex_models(cfg)
    elif cfg.openrouter.provider == "agy":
        use_agy_models(cfg)
    return cfg


# Reading steps run on Sonnet, translating and every check on Opus (ADR-062): one whole book each way, checked by
# Opus against the source, left 2 real errors with Sonnet reading against 4 all-Opus and 28 all-Sonnet, and reading
# on Sonnet costs about a third. Naming stays on the translating model: Sonnet named a woman with a man's name.
# Verify and relations run on Opus too: a handful of requests per book that settle speakers, gender and relations,
# which every later 你／妳 and form of address leans on.
CLAUDE_MODEL = "claude/opus"
CLAUDE_READ_MODEL = "claude/sonnet"
CLAUDE_READ_STAGES = ("scene_windows", "scene_analyze", "memory_update", "impressions", "house_style")


# A model id with one of these prefixes names its own source, whatever the run's source is: a step may run on the
# Claude plan while the rest runs locally, or the other way round (the client routes by prefix)
ROUTED_PREFIXES = ("claude/", "local/", "codex/", "agy/")
LOCAL_MODEL = "local/default"  # the model the local server has loaded (else the first it lists)


def _routed(model: str | None) -> bool:
    return bool(model) and str(model).startswith(ROUTED_PREFIXES)


def use_claude_models(cfg: Config) -> None:
    """Models for the Claude provider: a configured ``claude/…`` (or ``local/…``) model is kept; an API model id (the
    OpenRouter defaults, a review or fallback model) has no meaning here and is replaced or dropped."""
    o = cfg.openrouter
    if not o.model.startswith("claude/"):
        o.model = CLAUDE_MODEL
        # the reading steps read on Sonnet unless set otherwise: one step given to another source (proofreading on
        # Codex, reading on the local server) leaves the other steps as they are by default
        for stage in CLAUDE_READ_STAGES:
            if not _routed(o.stage_models.get(stage)):
                o.stage_models[stage] = CLAUDE_READ_MODEL
    o.stage_models = {k: v for k, v in o.stage_models.items() if _routed(v)}
    o.review_model = o.review_model if _routed(o.review_model) else None
    o.fallback_models = [m for m in o.fallback_models if _routed(m)]
    o.temperature = None  # Claude Code takes no temperature
    o.reasoning_effort = o.reasoning_effort or "medium"  # as NCTE runs by default (ncte.yaml), wherever it is run


CODEX_MODEL = "codex/gpt-6.1-sol"


def use_codex_models(cfg: Config) -> None:
    """Models for the Codex provider (ADR-071): a configured ``codex/…`` model is kept; any other model id (an API
    model, another source's) is replaced or dropped. Codex takes no temperature; every step says its level."""
    o = cfg.openrouter

    def codex(m: str | None) -> bool:
        return bool(m) and str(m).startswith("codex/")

    if not codex(o.model):
        o.model = CODEX_MODEL
    o.stage_models = {k: v for k, v in o.stage_models.items() if codex(v)}
    o.review_model = o.review_model if codex(o.review_model) else None
    o.fallback_models = [m for m in o.fallback_models if codex(m)]
    o.temperature = None
    o.reasoning_effort = o.reasoning_effort or "medium"


AGY_MODEL = "agy/gemini-flash"  # the newest Gemini Flash agy lists (a family name survives a model retiring)


def use_agy_models(cfg: Config) -> None:
    """Models for the Antigravity provider (ADR-082): a configured ``agy/…`` model is kept; any other model id is
    replaced or dropped. agy takes no temperature; a step's level picks the listed model of that level."""
    o = cfg.openrouter

    def agy(m: str | None) -> bool:
        return bool(m) and str(m).startswith("agy/")

    if not agy(o.model):
        o.model = AGY_MODEL
    o.stage_models = {k: v for k, v in o.stage_models.items() if agy(v)}
    o.review_model = o.review_model if agy(o.review_model) else None
    o.fallback_models = [m for m in o.fallback_models if agy(m)]
    o.temperature = None
    o.reasoning_effort = o.reasoning_effort or "medium"


def use_local_models(cfg: Config) -> None:
    """Models for the local provider: a ``local/…`` (or ``claude/…``) model is kept, an API model id is replaced by the
    model the server has loaded. No reasoning level by default: locally a thinking model spends its whole budget
    thinking (measured 2026-10-19), so thinking is on only where ``stage_reasoning`` asks for it."""
    o = cfg.openrouter
    if not _routed(o.model):
        o.model = LOCAL_MODEL
    o.stage_models = {k: v for k, v in o.stage_models.items() if _routed(v)}
    o.review_model = o.review_model if _routed(o.review_model) else None
    o.fallback_models = [m for m in o.fallback_models if _routed(m)]
    o.reasoning_effort = None if o.reasoning_effort in (None, "", "default", "medium") else o.reasoning_effort


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


def get_api_key(provider: str = "openrouter") -> str | None:
    name = "FAL_KEY" if provider == "fal" else "OPENROUTER_API_KEY"
    key = os.environ.get(name)
    if key:
        return key.strip() or None
    return _read_dotenv(Path(".env")).get(name) or None


def redact(text: str, key: str | None = None) -> str:
    """Remove the API key (and anything that looks like an OpenRouter key) from text."""
    import re

    for secret in (key, get_api_key(), get_api_key("fal")):
        if secret:
            text = text.replace(secret, "***")
    return re.sub(r"sk-or-[A-Za-z0-9_\-]{8,}", "sk-or-***", text)


# On the Claude plan (and the ChatGPT plan through Codex) proofreading does two passes unless the configuration says otherwise (ADR-066, 2026-10-20): in
# the whole-book test three passes over the same translation overlapped in 5 places, and a second pass found 6–8 more
# each time; there a pass costs quota, not money. Elsewhere (OpenRouter, fal: a pass is paid for) it stays one. If the
# next check finds the second pass adds only one or two, this goes back to one.
CLAUDE_PROOFREAD_PASSES = 2


def proofread_passes(cfg: Config) -> int:
    """How many proofreading passes a run does: ``context.proofread_passes`` when set (``ncte proofread --passes``
    sets it too), else the source's default."""
    n = cfg.context.proofread_passes
    if n is not None:
        return n
    return CLAUDE_PROOFREAD_PASSES if cfg.openrouter.provider in ("claude", "codex", "agy") else 1


def reasoning_setting(cfg: Config, stage: str) -> str | None:
    """The reasoning setting a step's requests use: its own (``stage_reasoning``) or the run's (``reasoning_effort``).
    None = send nothing (the model's default)."""
    stage = stage.split(":")[0]
    if stage == "scene_read" and stage not in cfg.openrouter.stage_reasoning:
        stage = "scene_analyze"  # the one-request read (ADR-065) thinks like the scene analysis unless set itself
    value = cfg.openrouter.stage_reasoning.get(stage, cfg.openrouter.reasoning_effort)
    return None if value in (None, "", "default") else value


JUDGING_STAGES = ("fix_check", "review", "proofread", "reader", "confirm", "verify")


CROSS_PROOFREAD = "proofread:cross"  # the step name of a proofreading pass on another company's model (ADR-082)


def stage_model(cfg: Config, stage: str, default: str) -> str:
    """The model a step's requests use: ``stage_models``, else ``review_model`` for review and its fix check, else the
    run's model."""
    if stage == CROSS_PROOFREAD and cfg.review.cross_proofread_model:
        return cfg.review.cross_proofread_model
    stage = stage.split(":")[0]
    if m := cfg.openrouter.stage_models.get(stage):
        return m
    if stage == "scene_read" and (m := cfg.openrouter.stage_models.get("scene_analyze")):
        return m  # the one-request read (ADR-065) is a reading step: unless set itself, it reads like scene_analyze
    if stage in ("review", "fix_check") and cfg.openrouter.review_model:
        return cfg.openrouter.review_model
    return default


def reasoning_param(setting: str | None) -> dict[str, Any] | None:
    if setting is None:
        return None
    if setting.startswith("max:"):
        return {"max_tokens": int(setting[4:])}
    return {"effort": setting}
