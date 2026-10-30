"""Run orchestration: jobs, batching, budget, concurrency, checkpoint/resume (spec §3.3, §13).

Every LLM unit of work is a row in ``jobs`` (pending → running → succeeded | failed |
needs_review | skipped). Translations are committed per batch in their own transaction, so an
interruption (Ctrl+C, crash, budget stop, fatal API error) never loses committed work and
``ncte resume`` continues with the remaining jobs.
"""

from __future__ import annotations

import hashlib
import json
import queue
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import Config, stage_model
from ..errors import (
    BudgetExceededError,
    FatalAPIError,
    LLMError,
    NCTEError,
)
from ..library import Book
from ..llm.base import LLMClient, LLMRequest, LLMResponse, StructuredMode
from ..llm.cost import Pricing, estimate_request_cost, find_model, latest_snapshot, save_snapshot
from ..llm.prompt_loader import load_prompt
from ..memory.db import Transaction, utcnow
from ..memory.style import load_style, style_payload
from ..qa.rules import Finding, worst
from .cache import glossary_entries, glossary_hash, is_cached, latest_translation, relevant_glossary
from .context_builder import (
    BODY_KINDS,
    META_KINDS,
    glossary_payload,
    heading_hints,
    neighbor_context,
    repeated_earlier,
    shown_context,
)
from .translator import Translator, UnitInput

MAX_UNITS_PER_BATCH = 60
MAX_JOB_ATTEMPTS = 3
# the translator's own doubts (its `flags`), checked like a word rule's find: with the source and the lines around it,
# in the scene's rule check (``confirm_flags``); only what that check finds real goes on to review (ADR-067). Named
# ``model_flag:…`` so they never mix with the program's word rules (``qa.rules.CONFIRM_RULES``).
MODEL_FLAGS_TO_CONFIRM = ("subject_omitted_kept", "speaker_uncertain", "term_uncertain")
CONFIRM_MODEL_FLAGS = tuple(f"model_flag:{f}" for f in MODEL_FLAGS_TO_CONFIRM)


@dataclass
class RunOptions:
    mode: str = "baseline"
    model: str = ""
    budget_usd: float = 5.0
    unit_ids: list[str] | None = None
    force: bool = False
    limit: int | None = None
    client: str = "openrouter"
    include_meta: bool = True
    review_only: bool = False
    naming_only: bool = False  # ADR-023: `ncte names propose` (context mode, read pass already done)
    gender_only: bool = False  # `ncte names gender-scan` (context mode, read pass already done)
    reader_only: bool = False  # `ncte reader-check` (ADR-038, a book already translated)
    proofread_only: bool = False  # `ncte proofread` (ADR-051, a book already translated)
    house_style_only: bool = False  # `ncte style auto` (ADR-053, a book already read)
    relations_only: bool = False  # `ncte names relations` (ADR-039, a book already read)
    verify_only: bool = False  # `ncte names verify` (ADR-033/046, a book already read)
    impressions_only: bool = False  # `ncte names impressions` (ADR-045, a book already read)
    terms_only: bool = False  # `ncte names terms` (ADR-069, a book already read)
    rebreak_only: bool = False  # `ncte scenes --rebreak`: choose the scene breaks again (ADR-036; books cut before it)


@dataclass
class RunSummary:
    run_id: str
    status: str
    counts: dict[str, int] = field(default_factory=dict)
    spent_usd: float = 0.0
    unknown_cost_requests: int = 0
    requests: int = 0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- pricing


def resolve_pricing(conn: sqlite3.Connection, client: LLMClient, model: str, *, allow_snapshot: bool = False,
                    log: Callable[[str], None] = print) -> tuple[Pricing, str]:
    """Fetch /models (and store a snapshot). Returns (pricing, source)."""
    try:
        models = client.list_models()
        with Transaction(conn):
            save_snapshot(conn, models, getattr(client, "catalog_source", client.name))
        source = f"{getattr(client, 'catalog_source', client.name)}:/models (live)"
    except LLMError as exc:
        snap = latest_snapshot(conn) if allow_snapshot else None
        if not snap:
            raise NCTEError(f"cannot fetch model list: {exc}") from exc
        log(f"⚠ model list unavailable ({exc}); using snapshot from {snap[0]}")
        models = snap[1]
        source = f"snapshot {snap[0]}"
    m = find_model(models, model)
    if m is None:
        raise FatalAPIError(f"model '{model}' is not listed by {client.name} /models — refusing to continue")
    return Pricing.from_model_json(m), source


def reasoning_label(param: dict[str, Any] | None) -> str | None:
    """How a request's reasoning setting is recorded (and matched when estimates are calibrated)."""
    if not param:
        return None
    return str(param["effort"]) if "effort" in param else f"max:{param.get('max_tokens')}"


# Steps that judge from material gathered over the whole book (relations, the second look) think more: with "medium"
# the model often chose not to think at all, and a relation that takes one step of inference was missed. They are a
# handful of requests per book. Proofreading too: with "medium" it caught fewer real errors (two books, 300 paragraphs
# each: 4 → 6 and 1–3 → 5), none more wrongly changed; about +US$1 a book. Used when the model offers "high" and the step
# has no setting of its own.
DEFAULT_STAGE_REASONING = {"relations": "high", "verify": "high", "fix_check": "high", "proofread": "high"}
# Sonnet translating on the Claude plan thinks at high (ADR-064): in a small test (four whole scenes, two runs each) it
# listed the later paragraph that settles a sentence and still translated it wrong at medium, also with read_through on;
# at high it got those sentences right. Opus translating is left as it is. A step's own setting wins.
CLAUDE_SONNET_TRANSLATE = ("claude/sonnet", "high")


def check_reasoning_effort(cfg: Config, pricing: Pricing) -> None:
    if "reasoning" in pricing.supported_parameters and (not pricing.reasoning_efforts or "high" in pricing.reasoning_efforts):
        for stage, level in DEFAULT_STAGE_REASONING.items():
            cfg.openrouter.stage_reasoning.setdefault(stage, level)
        if cfg.openrouter.provider == "claude" and pricing.model == CLAUDE_SONNET_TRANSLATE[0]:
            cfg.openrouter.stage_reasoning.setdefault("translate", CLAUDE_SONNET_TRANSLATE[1])
    settings = {"(run)": cfg.openrouter.reasoning_effort, **cfg.openrouter.stage_reasoning}
    for where, eff in settings.items():
        if not eff or eff == "default":
            continue
        if eff.startswith("max:"):
            if not eff[4:].isdigit():
                raise NCTEError(f"reasoning setting for {where} must be max:<tokens>, got '{eff}'")
            continue
        if pricing.reasoning_efforts and eff not in pricing.reasoning_efforts:
            raise NCTEError(f"reasoning_effort '{eff}' ({where}) not supported by {pricing.model} "
                            f"(supported: {', '.join(pricing.reasoning_efforts)})")
    if eff and "reasoning" not in pricing.supported_parameters:
        raise NCTEError(f"{pricing.model} does not accept the reasoning parameter")


# --------------------------------------------------------------------------- run records


def new_run_id() -> str:
    return "r_" + datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(3)


def create_run(conn: sqlite3.Connection, cfg: Config, book: Book, opts: RunOptions) -> str:
    rid = new_run_id()
    if opts.client != "fake":
        opts.client = cfg.openrouter.provider
    cfg_json = cfg.model_dump(mode="json")
    cfg_json["_run_options"] = asdict(opts)
    with Transaction(conn):
        conn.execute(
            "INSERT INTO runs(id, book_id, mode, model, config_json, config_hash, started_at, status, budget_usd, "
            "unit_limit) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rid, book.id, opts.mode, opts.model, json.dumps(cfg_json, ensure_ascii=False), cfg.config_hash(),
             utcnow(), "created", opts.budget_usd, opts.limit))
    return rid


def load_run(conn: sqlite3.Connection, run_id: str) -> tuple[sqlite3.Row, Config, RunOptions]:
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise NCTEError(f"unknown run id {run_id}")
    data = json.loads(row["config_json"])
    opts = RunOptions(**data.pop("_run_options"))
    return row, Config.model_validate(data), opts


def record_event(conn: sqlite3.Connection, run_id: str, event: str, source: str, **detail: Any) -> None:
    """A line in the run's history: who started or continued it, who changed its budget (``run_events``)."""
    import os

    with Transaction(conn):
        conn.execute("INSERT INTO run_events(run_id, at, event, source, pid, detail_json) VALUES (?,?,?,?,?,?)",
                     (run_id, utcnow(), event, source, os.getpid(), json.dumps(detail, ensure_ascii=False)))


def run_spend(conn: sqlite3.Connection, run_id: str) -> tuple[float, int, int]:
    """(spent, unknown, in_flight): a request whose bill is unknown (``unknown``) and one still waiting for its answer
    (``pending``) both count at their estimate; they are told apart for the progress line."""
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN cost_usd IS NOT NULL THEN cost_usd "
        "WHEN cost_status IN ('unknown','pending') THEN est_cost_usd ELSE 0 END),0) AS spent, "
        "SUM(CASE WHEN cost_status='unknown' THEN 1 ELSE 0 END) AS unk, "
        "SUM(CASE WHEN cost_status='pending' THEN 1 ELSE 0 END) AS pend "
        "FROM llm_requests WHERE run_id=?", (run_id,)).fetchone()
    return float(row["spent"] or 0), int(row["unk"] or 0), int(row["pend"] or 0)


def run_spent(conn: sqlite3.Connection, run_id: str) -> tuple[float, int]:
    """What the budget counts: (spent, requests counted at their estimate — unknown bill or still in flight)."""
    spent, unk, pend = run_spend(conn, run_id)
    return spent, unk + pend


def spent_note(conn: sqlite3.Connection, run_id: str) -> str:
    """The progress line's ``spent ≈ $X``: requests still in flight on another worker are not "unknown cost"."""
    spent, unk, pend = run_spend(conn, run_id)
    parts = ([f"{unk} unknown-cost request{'s' if unk != 1 else ''}"] if unk else []) + \
        ([f"{pend} in flight"] if pend else [])
    return f"spent ≈ ${spent:.4f}" + (f" ({', '.join(parts)})" if parts else "")


def run_plan_usd(conn: sqlite3.Connection, run_ids: list[str]) -> float:
    """What requests on a plan (the Claude provider: nothing billed) would have cost at list price, as the CLI
    reported it (``usage.list_cost_usd``)."""
    if not run_ids:
        return 0.0
    row = conn.execute("SELECT COALESCE(SUM(json_extract(usage_json, '$.list_cost_usd')), 0) FROM llm_requests WHERE "
                       f"run_id IN ({','.join('?' * len(run_ids))}) AND usage_json LIKE '%list_cost_usd%'",
                       run_ids).fetchone()
    return float(row[0] or 0)


class DbRecorder:
    """Persists every LLM request (thread-safe; shares the orchestrator lock)."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock, run_id: str, book_id: str,
                 pricing: Pricing, *, reuse: bool = True):
        self.conn, self.lock, self.run_id, self.book_id, self.pricing = conn, lock, run_id, book_id, pricing
        self.reuse = reuse  # earlier runs' answers to identical post-read requests (ADR-094)
        self.reused: dict[str, int] = {}  # answers taken instead of asking: this_run / earlier_run
        self.models: dict[str, Pricing] = {}  # step models other than the run's (``stage_models``)
        self.sizes: dict[int, int] = {}  # request → its payload's characters, until it is answered

    def start(self, req: LLMRequest, unit_ids: list[str], est_cost: float) -> int:
        # how big the request is (the payload, not the shared rules before it): kept in ``usage`` to measure by
        size = sum(len(m["content"]) for m in req.messages if m.get("role") != "system")
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO llm_requests(run_id, book_id, stage, request_hash, model, prompt_version, "
                "structured_mode, unit_ids_json, est_cost_usd, status, cost_status, created_at, reasoning_effort, "
                "usage_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.run_id, self.book_id, req.stage, req.request_hash(), req.model, req.prompt_version,
                 req.structured_mode, json.dumps(unit_ids), est_cost, "sent", "pending", utcnow(),
                 reasoning_label(req.extra.get("reasoning")), json.dumps({"payload_chars": size})))
            rid = int(cur.lastrowid or 0)
            self.sizes[rid] = size
            return rid

    def answered(self, req: LLMRequest) -> tuple[int, str, bool] | None:
        """(request id, answer) of the same request already answered, if any: a post-read step that was stopped part
        way asks only what it has no answer for when resumed (the same input gives the same request). With ``reuse``
        an earlier run's answer to the identical request counts too (ADR-094): the request hash covers the prompt
        text, model, effort, schema and data, so only a request that changed is asked again."""
        with self.lock:
            row = self.conn.execute(
                "SELECT id, response_text, run_id FROM llm_requests WHERE book_id=? AND stage=? AND request_hash=? "
                "AND status='succeeded' AND response_text IS NOT NULL AND (run_id=? OR ?) "
                "ORDER BY (run_id=?) DESC, id DESC LIMIT 1",
                (self.book_id, req.stage, req.request_hash(), self.run_id, int(self.reuse), self.run_id)).fetchone()
        return (int(row[0]), str(row[1]), row[2] != self.run_id) if row is not None else None

    def took(self, earlier: bool) -> None:
        """An answer from ``answered`` was used (it parsed): counted, for the log and the job details."""
        key = "earlier_run" if earlier else "this_run"
        with self.lock:
            self.reused[key] = self.reused.get(key, 0) + 1

    def success(self, rid: int, resp: LLMResponse, parse_status: str) -> None:
        cost, status = resp.cost_usd, "reported"
        if cost is None:
            u = resp.usage or {}
            pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
            if pt or ct:
                pricing = self.models.get(resp.model, self.pricing)
                cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                cost, status = pricing.cost(pt, ct, cached_tokens=cached), "estimated_from_tokens"
            else:
                status = "unknown"
        req_status = "succeeded" if parse_status in ("strict", "repaired_fence") else f"invalid_output:{parse_status}"
        with self.lock:
            size = self.sizes.pop(rid, None)
            usage = {**(resp.usage or {}), **({"payload_chars": size} if size is not None else {})}
            self.conn.execute(
                "UPDATE llm_requests SET provider=?, usage_json=?, generation_id=?, cost_usd=?, cost_status=?, "
                "status=?, attempts=?, latency_ms=?, response_text=?, structured_mode=?, finished_at=? WHERE id=?",
                (resp.provider, json.dumps(usage), resp.generation_id, cost, status, req_status,
                 resp.attempts, resp.latency_ms, resp.content, resp.structured_mode, utcnow(), rid))
            for _ in range(resp.maybe_billed_attempts):
                # an earlier attempt of this request timed out and may have been billed: its own row, cost unknown
                # (counted at the estimate, like any request whose bill is unknown)
                self.conn.execute(
                    "INSERT INTO llm_requests(run_id, book_id, stage, request_hash, model, prompt_version, structured_mode, "
                    "unit_ids_json, est_cost_usd, status, cost_status, error, created_at, finished_at, reasoning_effort) "
                    "SELECT run_id, book_id, stage, request_hash, model, prompt_version, structured_mode, '[]', "
                    "est_cost_usd, 'failed', 'unknown', 'earlier attempt timed out; may have been billed', created_at, ?, "
                    "reasoning_effort FROM llm_requests WHERE id=?", (utcnow(), rid))

    def failure(self, rid: int, exc: Exception) -> None:
        billed = bool(getattr(exc, "possibly_billed", False))
        # a turn that ran and came back empty (a CLI source): its tokens, plan windows and time are kept as a success's
        # are, so the plan and cost reports count it (ADR-102)
        used, took = getattr(exc, "usage", None), getattr(exc, "latency_ms", None)
        with self.lock:
            size = self.sizes.pop(rid, None)
            self.conn.execute(
                "UPDATE llm_requests SET status='failed', error=?, cost_status=?, cost_usd=?, finished_at=?, "
                "latency_ms=COALESCE(?, latency_ms), usage_json=COALESCE(?, usage_json) WHERE id=?",
                (f"{type(exc).__name__}: {str(exc)[:500]}", "unknown" if billed else "none",
                 None if billed else 0.0, utcnow(), took,
                 json.dumps({**used, **({"payload_chars": size} if size is not None else {})})
                 if isinstance(used, dict) else None, rid))


# --------------------------------------------------------------------------- orchestrator


@dataclass
class Batch:
    rows: list[sqlite3.Row]
    meta: bool = False
    scene_id: str | None = None
    whole_scene: bool = False  # a part of a long scene: it sees the rest of the scene as context
    fix: bool = False  # a fix ("window" scope): the paragraphs around it as context, and only their memory

    @property
    def unit_ids(self) -> list[str]:
        return [r["id"] for r in self.rows]

    @property
    def chars(self) -> int:
        return sum(len(r["plain_text"]) for r in self.rows)


MAX_UNITS_PER_SCENE_BATCH = 200


def scene_batches(rows: list[sqlite3.Row], max_chars: int, *, position: dict[int, int] | None = None) -> list[Batch]:
    """One batch per scene, the way a translator works: the whole exchange in view. A scene longer than ``max_chars``
    (the model found no pause to cut it at) is split into as few, as even parts as possible, each cut between two
    narration paragraphs when there is one nearby, and each part sees the rest of the scene."""
    from ..analysis.scene import _calm

    out: list[Batch] = []
    for b in make_batches(rows, 10 ** 9, by_scene=True, position=position, max_units=MAX_UNITS_PER_SCENE_BATCH):
        if b.meta or b.chars <= max_chars:
            out.append(b)
            continue
        parts = -(-b.chars // max_chars)
        sizes = [len(r["plain_text"]) for r in b.rows]
        cuts: list[int] = []
        run, want = 0, b.chars / parts
        for i, n in enumerate(sizes):
            run += n
            if len(cuts) < parts - 1 and run >= want * (len(cuts) + 1) and 0 < i + 1 < len(b.rows):
                near = sorted(range(max(1, i - 7), min(i + 9, len(b.rows))), key=lambda k: abs(k - (i + 1)))
                j = next((k for k in near if _calm(b.rows[k], b.rows[k - 1])), i + 1)  # the nearest quiet point
                if not cuts or j > cuts[-1]:
                    cuts.append(j)
        edges = [0, *cuts, len(b.rows)]
        out += [Batch(b.rows[a:z], False, b.scene_id, whole_scene=True) for a, z in zip(edges, edges[1:], strict=False)
                if z > a]
    return out


def make_batches(rows: list[sqlite3.Row], target: int, *, by_scene: bool = False,
                 position: dict[int, int] | None = None, max_units: int = MAX_UNITS_PER_BATCH) -> list[Batch]:
    """Group rows into batches. ``position`` maps ordinal → index among *all* translatable body units of the
    book; when given, a batch never spans a gap (units in between that are not being translated now), because
    the model would then see two distant paragraphs side by side without the text that connects them."""
    batches: list[Batch] = []
    cur: list[sqlite3.Row] = []
    cur_len = 0
    cur_key: tuple[Any, ...] | None = None
    for r in rows:
        meta = r["kind"] in META_KINDS
        key = (r["resource_href"] if not meta else "META", r["scene_id"] if by_scene else None, meta)
        n = len(r["plain_text"])
        gap = bool(position and cur and not meta and cur[-1]["ordinal"] in position and r["ordinal"] in position
                   and position[r["ordinal"]] - position[cur[-1]["ordinal"]] > 1)
        if cur and (gap or key != cur_key or cur_len + n > target or len(cur) >= max_units):
            batches.append(Batch(cur, cur_key[2] if cur_key else False, cur_key[1] if cur_key else None))
            cur, cur_len = [], 0
        cur.append(r)
        cur_len += n
        cur_key = key
    if cur:
        batches.append(Batch(cur, cur_key[2] if cur_key else False, cur_key[1] if cur_key else None))
    return batches


class Orchestrator:
    SCENE_START_CONTEXT = False  # context mode: a scene's request sees the end of the scene before (ADR-085)

    def __init__(self, conn: sqlite3.Connection, cfg: Config, client: LLMClient, book: Book, run_id: str,
                 opts: RunOptions, *, log: Callable[[str], None] = print):
        self.conn, self.cfg, self.client, self.book, self.run_id, self.opts = conn, cfg, client, book, run_id, opts
        self.log = log
        self.lock = threading.RLock()
        self.notes: list[str] = []
        self.prompt = load_prompt("translate")
        self.pricing: Pricing | None = None
        self.stage_pricing: dict[str, Pricing] = {}  # other models some steps use (``openrouter.stage_models``)
        self.translator: Translator | None = None
        self.model = opts.model
        self._stop_reason: str | None = None
        self.glossary: list[dict[str, Any]] = []
        self.link_revealed: dict[str, tuple[float, int]] = {}
        self.volume = 1.0
        self.fix_checks_asked: set[int] = set()  # fixes (translation ids) whose check was asked in this process

    # ------------------------------------------------------------ setup
    def _note(self, msg: str) -> None:
        with self.lock:
            if msg not in self.notes:
                self.notes.append(msg)
        self.log(f"⚠ {msg}")

    def prepare(self) -> None:
        try:
            self.pricing, src = resolve_pricing(self.conn, self.client, self.model, log=self.log)
        except FatalAPIError:
            fb = self.cfg.openrouter.fallback_models
            if not fb:
                raise
            self.pricing = None
            for alt in fb:
                try:
                    self.pricing, src = resolve_pricing(self.conn, self.client, alt, log=self.log)
                    self._note(f"model {self.model} unavailable; using user-configured fallback {alt}")
                    self.model = alt
                    break
                except FatalAPIError:
                    continue
            if self.pricing is None:
                raise
        assert self.pricing is not None
        pin, pout = self.pricing.per_million()
        self.log(f"model {self.model}: ${pin:.4f}/M input, ${pout:.4f}/M output ({src})")
        check_reasoning_effort(self.cfg, self.pricing)
        params = self.pricing.supported_parameters
        if self.cfg.openrouter.temperature is not None and params and "temperature" not in params:
            # e.g. Claude Sonnet 5.5 on OpenRouter: with require_parameters a temperature finds no endpoint at all
            self._note(f"{self.model} does not accept temperature; sending none (model default)")
            self.cfg.openrouter.temperature = None
        mode: StructuredMode = self.pricing.structured_mode  # type: ignore[assignment]
        if mode != "json_schema":
            self._note(f"{self.model} does not advertise structured_outputs; using {mode} + strict local "
                       f"Pydantic validation (no native schema guarantee)")
        recorder = DbRecorder(self.conn, self.lock, self.run_id, self.book.id, self.pricing,
                              reuse=self.cfg.context.reuse_answers and not self.opts.force)  # --force redoes all
        self.recorder = recorder
        self.stage_pricing = self._step_models()
        recorder.models = self.stage_pricing
        self.translator = Translator(self.client, self.cfg, recorder, model=self.model, structured_mode=mode,
                                     estimate=self._estimate, prompt=self.prompt, on_downgrade=self._note)
        self.reload_glossary()

    def reload_glossary(self) -> None:
        from ..memory.entities import link_revealed
        from ..series import volume_of

        with self.lock:
            self.glossary = glossary_entries(self.conn, self.book.id)
            self.link_revealed = link_revealed(self.conn, self.book.id)
            self.volume = volume_of(self.conn, self.book.id)

    def _estimate(self, payload: dict[str, Any], out_chars: int, stage: str | None = None) -> float:
        assert self.pricing is not None
        text = self.prompt.text + json.dumps(payload, ensure_ascii=False)
        pricing = self.stage_pricing.get(self.model_for(stage), self.pricing) if stage else self.pricing
        return estimate_request_cost(pricing, text, out_chars)[2]

    def mode_for(self, model: str) -> Any:
        """Structured-output mode to start a request with: the run's (it may have been downgraded) or the step model's."""
        assert self.translator is not None
        p = self.stage_pricing.get(model)
        return self.translator.mode if p is None or model == self.model else p.structured_mode

    def model_for(self, stage: str) -> str:
        """The model this step uses (``openrouter.stage_models``; review and its fix check also ``review_model``)."""
        return stage_model(self.cfg, stage, self.model)

    def _step_models(self) -> dict[str, Pricing]:
        """Every other model a step is set to use: it must be listed by /models (never a silent substitute); its own
        prices estimate and record its requests, and a parameter it does not take is not sent to it."""
        from ..llm.base import MODEL_PARAMS

        if "translate" in self.cfg.openrouter.stage_models:
            raise NCTEError("stage_models cannot change the translating model (use --model); it keeps one voice")
        assert self.pricing is not None
        MODEL_PARAMS[self.model] = set(self.pricing.supported_parameters)
        out: dict[str, Pricing] = {}
        wanted = {*self.cfg.openrouter.stage_models.values(), self.cfg.openrouter.review_model,
                  self.cfg.review.cross_proofread_model} - {None, self.model}
        for m in sorted(x for x in wanted if x):
            pricing, _src = resolve_pricing(self.conn, self.client, m, log=self.log)
            pin, pout = pricing.per_million()
            self.log(f"step model {m}: ${pin:.4f}/M input, ${pout:.4f}/M output")
            MODEL_PARAMS[m] = set(pricing.supported_parameters)
            out[m] = pricing
        return out

    # ------------------------------------------------------------ jobs
    def target_units(self) -> list[sqlite3.Row]:
        kinds = list(BODY_KINDS) + (list(META_KINDS) if self.opts.include_meta and
                                    self.cfg.export.translate_nav_titles else [])
        q = (f"SELECT * FROM units WHERE book_id=? AND translatable=1 AND kind IN ({','.join('?' * len(kinds))}) "
             "ORDER BY ordinal")
        rows = self.conn.execute(q, (self.book.id, *kinds)).fetchall()
        if self.opts.unit_ids:
            wanted = set(self.opts.unit_ids)
            rows = [r for r in rows if r["id"] in wanted]
            missing = wanted - {r["id"] for r in rows}
            if missing:
                raise NCTEError(f"unknown or untranslatable unit ids: {sorted(missing)[:5]}")
        if self.opts.limit is not None:
            body = [r for r in rows if r["kind"] in BODY_KINDS][: self.opts.limit]
            rows = body
        return rows

    def ensure_jobs(self) -> None:
        with self.lock:
            existing = self.conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?", (self.run_id,)).fetchone()[0]
            if existing:
                self.mark_inflight_unknown()
                n = self.conn.execute(
                    "UPDATE jobs SET status='pending', updated_at=? WHERE run_id=? AND (status='running' OR "
                    "(status='failed' AND attempts<?))", (utcnow(), self.run_id, MAX_JOB_ATTEMPTS)).rowcount
                if n:
                    self.log(f"resume: {n} interrupted/failed jobs re-queued")
                return
            rows = self.target_units()
            with Transaction(self.conn):
                for seq, r in enumerate(rows):
                    status, cp = "pending", {}
                    if not self.opts.force and self.cfg.translation.skip_already_translated:
                        tr = latest_translation(self.conn, r["id"], r["source_hash"])
                        gh = glossary_hash(relevant_glossary(self.glossary, r["plain_text"]))
                        if is_cached(tr, prompt_version=self.prompt.version_id, model=self.model,
                                     mode=self.opts.mode, gl_hash=gh):
                            status, cp = "skipped", {"reused_translation_id": tr["id"]}  # type: ignore[index]
                    self.conn.execute(
                        "INSERT INTO jobs(book_id, run_id, stage, target_id, seq, status, checkpoint_json, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (self.book.id, self.run_id, "translate", r["id"], seq, status, json.dumps(cp), utcnow()))
                self.create_extra_jobs()

    def mark_inflight_unknown(self) -> None:
        """Requests sent but never answered may still have been billed: keep their estimate as cost."""
        with self.lock:
            n = self.conn.execute(
                "UPDATE llm_requests SET status='interrupted', cost_status='unknown', finished_at=? "
                "WHERE run_id=? AND status='sent'", (utcnow(), self.run_id)).rowcount
        if n:
            self._note(f"{n} in-flight request(s) interrupted — billing status unknown (estimate counted)")

    def create_extra_jobs(self) -> None:
        """Hook for context mode (scene analysis / memory update jobs)."""

    def pending_rows(self, *, meta: bool | None = None) -> list[sqlite3.Row]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT u.* FROM jobs j JOIN units u ON u.id=j.target_id WHERE j.run_id=? AND j.stage='translate' "
                "AND j.status='pending' ORDER BY u.ordinal", (self.run_id,)).fetchall()
        if meta is None:
            return rows
        return [r for r in rows if (r["kind"] in META_KINDS) == meta]

    def _set_jobs(self, unit_ids: list[str], status: str, error: str | None = None, *, bump: bool = False) -> None:
        with self.lock:
            self.conn.executemany(
                "UPDATE jobs SET status=?, error=?, attempts=attempts+?, updated_at=? WHERE run_id=? AND "
                "stage='translate' AND target_id=?",
                [(status, error, 1 if bump else 0, utcnow(), self.run_id, u) for u in unit_ids])

    # ------------------------------------------------------------ batch processing
    def batch_context(self, batch: Batch) -> dict[str, Any]:
        with self.lock:
            text = " ".join(r["plain_text"] for r in batch.rows)
            if batch.meta:
                ctx: dict[str, Any] = {"known_headings": heading_hints(self.conn, self.book.id),
                                       "note": "These are chapter titles / table-of-contents labels / book title."}
            else:
                ctx = neighbor_context(self.conn, self.cfg, self.book.id, batch.rows, scene_id=batch.scene_id,
                                       whole_scene=bool(self.opts.unit_ids) or batch.whole_scene, fix=batch.fix,
                                       scene_start=self.SCENE_START_CONTEXT)
                text += " " + " ".join(c["source"] for c in shown_context(ctx))
            ctx["glossary"] = glossary_payload(self.glossary, text, upto=(self.volume, batch.rows[-1]["ordinal"]),
                                               revealed=self.link_revealed, readings=True)
            if not batch.meta:
                style = style_payload(load_style(self.conn, self.book.id), text)
                if style:
                    ctx["style"] = style
                ids = [r["id"] for r in batch.rows]
                found = self.conn.execute(
                    f"SELECT unit_id, term, explanation, note_zh FROM reader_notes WHERE status!='dropped' AND unit_id IN "
                    f"({','.join('?' * len(ids))}) ORDER BY id", ids).fetchall()
                if found:  # ADR-026: wordplay / references found while reading this very unit
                    ctx["wordplay"] = [{"unit_id": f["unit_id"], "term": f["term"], "explanation": f["explanation"],
                                        **({"note": f["note_zh"]} if f["note_zh"] else {})} for f in found]
                if repeats := repeated_earlier(self.conn, self.book.id, batch.rows):
                    ctx["repeats"] = repeats
            return ctx

    def qa_glossary(self) -> list[dict[str, Any]]:
        return self.glossary

    def process_batch(self, batch: Batch) -> dict[str, int]:
        assert self.translator is not None
        units = [UnitInput.from_row(r) for r in batch.rows]
        ctx = self.batch_context(batch)
        self._set_jobs(batch.unit_ids, "running")
        try:
            res = self.translator.translate(units, ctx, qa_glossary=self.qa_glossary())
        except FatalAPIError:
            self._set_jobs(batch.unit_ids, "pending")
            raise
        except LLMError as exc:
            self._set_jobs(batch.unit_ids, "failed", f"{type(exc).__name__}: {exc}", bump=True)
            self._finding(None, "api_error", "high", f"batch of {len(units)} units: {exc}")
            return {"ok": 0, "failed": len(units)}
        ctx_hash = "c:" + hashlib.sha256(json.dumps(ctx, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        rows_by_id = {r["id"]: r for r in batch.rows}
        counts = {"ok": 0, "failed": 0, "needs_review": 0}
        with self.lock, Transaction(self.conn):
            for uid, ur in res.ok.items():
                r = rows_by_id[uid]
                sev = worst(ur.findings)
                status = "needs_review" if sev == "high" else "succeeded"
                gh = glossary_hash(relevant_glossary(self.glossary, r["plain_text"]))
                tid = self.conn.execute(
                    "INSERT INTO translations(unit_id, target_lang, text, source_hash, context_hash, prompt_version, "
                    "model_id, glossary_version, mode, status, run_id, request_id, flags_json, notes_json, created_at, "
                    "translator_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, self.cfg.app.language, ur.text, r["source_hash"], ctx_hash, self.prompt.version_id,
                     self.model, gh, self.opts.mode, status, self.run_id, ur.request_id,
                     json.dumps(ur.flags, ensure_ascii=False), json.dumps(ur.stored_notes(), ensure_ascii=False), utcnow(),
                     ur.translator_note or None)).lastrowid
                # each finding is about this version (ADR-055): a later version is not held back by it
                for f in ur.findings:
                    if f.severity != "info":
                        self._finding(uid, f.type, f.severity, f.evidence, translation_id=tid)
                for flag in ur.flags:
                    if flag in MODEL_FLAGS_TO_CONFIRM:  # high: the rule check picks it up, and review if it is real
                        self._finding(uid, f"model_flag:{flag}", "high",
                                      f"譯者標記 {flag}" + (f"：{'; '.join(ur.notes)}" if ur.notes else ""),
                                      translation_id=tid)
                    elif flag == "possible_source_error":  # reaches review through the translation's flags
                        self._finding(uid, f"model_flag:{flag}", "low", "; ".join(ur.notes)[:300], translation_id=tid)
                self.conn.execute(
                    "UPDATE jobs SET status=?, error=NULL, updated_at=? WHERE run_id=? AND stage='translate' AND target_id=?",
                    (status, utcnow(), self.run_id, uid))
                counts["ok"] += 1
                if status == "needs_review":
                    counts["needs_review"] += 1
            for uid, reason in res.failed.items():
                self.conn.execute(
                    "UPDATE jobs SET status='failed', error=?, attempts=attempts+1, updated_at=? WHERE run_id=? "
                    "AND stage='translate' AND target_id=?", (reason[:500], utcnow(), self.run_id, uid))
                typ = "content_blocked" if reason.startswith("content_blocked") else "translation_failed"
                self._finding(uid, typ, "high", f"kept original Japanese: {reason[:400]}")
                counts["failed"] += 1
        return counts

    def _finding(self, unit_id: str | None, typ: str, sev: str, evidence: str,
                 translation_id: int | None = None) -> int | None:
        """Record a finding; ``translation_id`` is the version of the unit's text it is about (when known)."""
        with self.lock:
            return self.conn.execute(
                "INSERT INTO qa_findings(run_id, book_id, unit_id, type, severity, evidence, created_at, translation_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.run_id, self.book.id, unit_id, typ, sev, evidence, utcnow(), translation_id)).lastrowid

    # ------------------------------------------------------------ budget
    def check_budget(self, est: float) -> None:
        with self.lock:
            spent, _ = run_spent(self.conn, self.run_id)
        if spent + est > self.opts.budget_usd:
            raise BudgetExceededError(
                f"budget ${self.opts.budget_usd:.4f} would be exceeded (spent≈${spent:.4f} + next≈${est:.4f})")

    def batch_estimate(self, batch: Batch) -> float:
        units = [{"unit_id": r["id"], "source": r["source_text"]} for r in batch.rows]
        # context is approximated by 2× the batch text (neighbours + glossary)
        return self._estimate({"units": units, "ctx": units}, batch.chars)

    # ------------------------------------------------------------ main loop
    lock_source = "cli"  # who holds the series lock while this runs (the web page sets "gui")

    def rebreak(self) -> None:
        raise NCTEError("choosing scene breaks again needs --mode context")

    def impressions_pass(self, *, record_job: bool = True) -> int:
        raise NCTEError("character impressions need context mode")

    def run(self) -> RunSummary:
        from .run_lock import SeriesLock

        # before anything changes: another program working on this series keeps its run, its jobs and its money
        series_lock = SeriesLock(self.conn)
        series_lock.acquire(self.run_id, self.lock_source)
        with self.lock:
            resumed = self.conn.execute("SELECT 1 FROM jobs WHERE run_id=? LIMIT 1", (self.run_id,)).fetchone() is not None
        record_event(self.conn, self.run_id, "resume" if resumed else "start", self.lock_source,
                     budget_usd=self.opts.budget_usd)
        try:
            return self._run_locked()
        finally:
            series_lock.release()

    def _run_locked(self) -> RunSummary:
        self._set_run_status("running")
        try:
            self.prepare()
            o = self.opts  # a run that sends texts back to the translator settles their checks before it ends
            fixing = not (o.naming_only or o.gender_only or o.house_style_only or o.relations_only or o.verify_only
                          or o.rebreak_only or o.impressions_only or o.terms_only)
            if self.opts.review_only:
                self.review_existing()
            elif self.opts.naming_only:
                self.propose_names()
            elif self.opts.gender_only:
                self.scan_genders()
            elif self.opts.reader_only:  # over the whole book: recorded like a run's own pass (ADR-055)
                whole = self._own_job("reader")
                self.reader_pass(record_job=whole)
            elif self.opts.proofread_only:
                whole = self._own_job("proofread")
                self.proofread_pass(record_job=whole)
            elif self.opts.house_style_only:
                self.house_style_pass(record_job=False)
            elif self.opts.relations_only:
                self.relations_pass(record_job=False)
            elif self.opts.verify_only:
                self.verify_pass(record_job=False)
            elif self.opts.rebreak_only:
                self.rebreak()
            elif self.opts.impressions_only:
                self.impressions_pass(record_job=False)
            elif self.opts.terms_only:
                self.terms_pass(record_job=False)
            else:
                self.ensure_jobs()
                self.execute()
            if fixing and not self._stop_reason:
                self.check_waiting_fixes()
            status = self._stop_reason or "completed"
        except KeyboardInterrupt:
            status = "interrupted"
            self.mark_inflight_unknown()
            self.log("\n⏸ interrupted — progress saved. Continue with: ncte resume --run-id " + self.run_id)
        except FatalAPIError as exc:
            status = "stopped_fatal"
            self._note(f"fatal API error: {exc}")
        except BudgetExceededError as exc:
            status = "budget_exhausted"
            self._note(str(exc))
        if status == "completed":
            self.reconcile_costs()
        summary = self.summary(status)
        self._set_run_status(summary.status, summary)
        return summary

    def _own_job(self, stage: str) -> bool:
        """A run that only proofreads or only reads like a reader over the whole book records that pass as a job on
        the book, kept until done (``ncte resume`` goes on from its checkpoint). Over chosen paragraphs it records
        none (False): it is not a pass over the book, and recording it would make a complete book incomplete."""
        if self.opts.unit_ids:
            return False
        with self.lock, Transaction(self.conn):
            if self.conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND stage=? AND target_id=?",
                                 (self.run_id, stage, self.book.id)).fetchone() is None:
                self.conn.execute("INSERT INTO jobs(book_id, run_id, stage, target_id, seq, status, updated_at) "
                                  "VALUES (?,?,?,?,?,?,?)", (self.book.id, self.run_id, stage, self.book.id, 0,
                                                             "pending", utcnow()))
        return True

    def body_positions(self) -> dict[int, int]:
        with self.lock:
            ords = [r[0] for r in self.conn.execute(
                "SELECT ordinal FROM units WHERE book_id=? AND translatable=1 AND kind IN ('text','heading') AND "
                "spine_index IS NOT NULL ORDER BY ordinal", (self.book.id,))]
        return {o: i for i, o in enumerate(ords)}

    def execute(self) -> None:
        self.run_batches(make_batches(self.pending_rows(meta=False), self.cfg.translation.unit_target_japanese_chars,
                                      position=self.body_positions()))
        self.review(self.translated_unit_ids())
        self.run_meta()

    def translated_unit_ids(self, scene_id: str | None = None) -> list[str]:
        q = ("SELECT j.target_id FROM jobs j JOIN units u ON u.id=j.target_id WHERE j.run_id=? AND j.stage='translate' "
             "AND j.status IN ('succeeded','needs_review') AND u.kind IN ('text','heading')")
        params: list[Any] = [self.run_id]
        if scene_id:
            q += " AND u.scene_id=?"
            params.append(scene_id)
        with self.lock:
            return [r[0] for r in self.conn.execute(q + " ORDER BY u.ordinal", params)]

    def review_existing(self) -> None:
        """``ncte review``: one review pass over the book's current machine translations."""
        from .cache import latest_texts
        from .review import review_scope

        best = latest_texts(self.conn, self.book.id)
        body = [r["id"] for r in self.conn.execute(
            "SELECT id FROM units WHERE book_id=? AND kind IN ('text','heading') ORDER BY ordinal", (self.book.id,))
                if r["id"] in best and best[r["id"]]["status"] != "human"]
        if self.opts.unit_ids:
            body = [u for u in body if u in set(self.opts.unit_ids)]
        stats = review_scope(self, body, any_run=True)
        self.log(f"review: {stats}")

    def propose_names(self) -> None:
        raise NCTEError("naming proposals need context mode")

    def reader_pass(self, *, record_job: bool = True) -> dict[str, int]:
        raise NCTEError("the reader's look needs context mode")

    def proofread_pass(self, *, record_job: bool = True) -> dict[str, int]:
        raise NCTEError("proofreading needs context mode")

    def house_style_pass(self, *, record_job: bool = True) -> dict[str, int]:
        raise NCTEError("the house style needs context mode")

    def relations_pass(self, *, record_job: bool = True) -> int:
        raise NCTEError("relations need context mode")

    def verify_pass(self, *, record_job: bool = True) -> None:
        raise NCTEError("the second look needs context mode")

    def terms_pass(self, *, record_job: bool = True) -> dict[str, int]:
        raise NCTEError("term definitions need context mode")

    def scan_genders(self) -> None:
        raise NCTEError("gender scan needs context mode")

    def check_waiting_fixes(self) -> dict[str, int]:
        """ADR-100: a fix still waiting for its check (a check that failed, or was cut off by a pause) is asked once
        more before the run completes; resumed, the run asks those it left."""
        from .review import check_waiting_fixes

        stats = check_waiting_fixes(self, set(self.opts.unit_ids) if self.opts.unit_ids else None)
        if stats.get("asked"):
            self.log(f"  fixes waiting for their check, asked again: {stats}")
        return stats

    def review(self, unit_ids: list[str]) -> None:
        from .review import review_scope

        stats = review_scope(self, unit_ids)
        if stats["actually_reviewed"] or stats["blocked"] or stats["failed"]:
            self.log(f"  review: {stats}")

    def run_meta(self) -> None:
        """The titles last: a TOC entry whose source is a story heading's takes the heading's final text (ADR-106,
        also those translated in an earlier run, so a heading fixed since is followed); the rest are translated."""
        from .headings import follow_headings

        with self.lock, Transaction(self.conn):
            got = follow_headings(self.conn, self.book.id, run_id=self.run_id,
                                  meta=self.opts.include_meta and self.cfg.export.translate_nav_titles)
            # by state, not by what happened: a title of this run that follows no heading is translated for itself
            # when it has no text that counts now (its copy taken off in this run, an earlier one or an export), and
            # is not sent when it has one (a person's text written while it waited), however its job stood before
            jobs = self.conn.execute(
                "SELECT u.*, j.status AS job_status FROM jobs j JOIN units u ON u.id=j.target_id WHERE j.run_id=? AND "
                f"j.stage='translate' AND j.status IN ('pending','skipped','succeeded') AND u.kind IN "
                f"({','.join('?' * len(META_KINDS))})", (self.run_id, *META_KINDS)).fetchall()
            again, done = [], []
            reuse = not self.opts.force and self.cfg.translation.skip_already_translated  # as ensure_jobs
            for r in jobs:
                if r["id"] in got["followed"]:
                    continue
                tr = latest_translation(self.conn, r["id"], r["source_hash"])
                has = is_cached(tr, prompt_version=self.prompt.version_id, model=self.model, mode=self.opts.mode,
                                gl_hash=glossary_hash(relevant_glossary(self.glossary, r["plain_text"])))
                if has and r["job_status"] == "pending" and reuse:
                    done.append((json.dumps({"reused_translation_id": tr["id"]}), r["id"]))  # type: ignore[index]
                elif not has and r["job_status"] != "pending":
                    again.append(r["id"])
            self.conn.executemany("UPDATE jobs SET status='pending', updated_at=? WHERE run_id=? AND stage='translate' "
                                  "AND target_id=?", [(utcnow(), self.run_id, u) for u in again])
            self.conn.executemany("UPDATE jobs SET status='skipped', checkpoint_json=?, updated_at=? WHERE run_id=? "
                                  "AND stage='translate' AND target_id=?",
                                  [(cp, utcnow(), self.run_id, u) for cp, u in done])
            meta_rows = self.pending_rows(meta=True)
            # a book-matter entry is a body unit (translated with its scene); a title waiting is taken from its heading
            self.conn.executemany("UPDATE jobs SET status='succeeded', checkpoint_json=?, updated_at=? WHERE "
                                  "run_id=? AND stage='translate' AND target_id=? AND status='pending'",
                                  [(json.dumps({"follows_heading": True}), utcnow(), self.run_id, r["id"])
                                   for r in meta_rows if r["id"] in got["followed"]])
        if got["written"]:
            self.log(f"TOC/title: {got['written']} entries follow their chapter heading")
        if again:
            self.log(f"TOC/title: {len(again)} entries no longer follow a heading and are translated for themselves")
        rest = [r for r in meta_rows if r["id"] not in got["followed"]]
        if rest:
            self.run_batches(make_batches(rest, self.cfg.translation.unit_target_japanese_chars))

    def run_batches(self, batches: list[Batch], concurrency: int | None = None) -> None:
        if not batches:
            return
        total = len(batches)
        conc = max(1, concurrency or self.cfg.openrouter.max_concurrency)
        done_q: queue.Queue[tuple[int, Batch, dict[str, int] | None, BaseException | None]] = queue.Queue()
        active = 0
        idx = 0
        fatal: BaseException | None = None

        def worker(i: int, b: Batch) -> None:
            try:
                done_q.put((i, b, self.process_batch(b), None))
            except BaseException as exc:  # noqa: BLE001 – forwarded to the main thread
                done_q.put((i, b, None, exc))

        try:
            while idx < total or active:
                while active < conc and idx < total and fatal is None and self._stop_reason is None:
                    b = batches[idx]
                    self.check_budget(self.batch_estimate(b))
                    t = threading.Thread(target=worker, args=(idx, b), daemon=True)
                    t.start()
                    active += 1
                    idx += 1
                if active == 0:
                    break
                try:
                    i, b, counts, exc = done_q.get(timeout=0.25)
                except queue.Empty:
                    continue
                active -= 1
                if exc is not None:
                    if isinstance(exc, FatalAPIError):
                        fatal = exc
                        continue
                    raise exc
                with self.lock:
                    note = spent_note(self.conn, self.run_id)
                assert counts is not None
                self.log(f"[{i + 1}/{total}] {len(b.rows)} units: ok {counts['ok']}, review "
                         f"{counts.get('needs_review', 0)}, failed {counts['failed']} | {note}")
        except BudgetExceededError:
            # let in-flight batches finish so their results are committed
            self._drain(done_q, active)
            raise
        if fatal is not None:
            self._drain(done_q, active - 0)
            raise fatal

    def _drain(self, done_q: queue.Queue, active: int) -> None:  # type: ignore[type-arg]
        deadline = time.monotonic() + self.cfg.openrouter.timeout_seconds + 5
        while active > 0 and time.monotonic() < deadline:
            try:
                done_q.get(timeout=0.25)
                active -= 1
            except queue.Empty:
                continue

    # ------------------------------------------------------------ finish
    def reconcile_costs(self) -> int:
        return reconcile_costs(self.conn, self.client, run_id=self.run_id)

    def summary(self, status: str) -> RunSummary:
        with self.lock:
            counts = {r["status"]: r["n"] for r in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs WHERE run_id=? AND stage='translate' GROUP BY status",
                (self.run_id,))}
            extra = {f"{r['stage']}:{r['status']}": r["n"] for r in self.conn.execute(
                "SELECT stage, status, COUNT(*) AS n FROM jobs WHERE run_id=? AND stage!='translate' "
                "GROUP BY stage, status", (self.run_id,))}
            spent, unk = run_spent(self.conn, self.run_id)
            nreq = self.conn.execute("SELECT COUNT(*) FROM llm_requests WHERE run_id=?", (self.run_id,)).fetchone()[0]
        counts.update(extra)
        if status == "completed" and (counts.get("failed") or counts.get("pending")):
            status = "completed_with_failures"
        return RunSummary(self.run_id, status, counts, spent, unk, nreq, list(self.notes))

    def _set_run_status(self, status: str, summary: RunSummary | None = None) -> None:
        with self.lock:
            if summary is None:
                self.conn.execute("UPDATE runs SET status=? WHERE id=?", (status, self.run_id))
            else:
                self.conn.execute("UPDATE runs SET status=?, finished_at=?, summary_json=?, model=? WHERE id=?",
                                  (status, utcnow(), json.dumps(asdict(summary), ensure_ascii=False), self.model,
                                   self.run_id))


def reconcile_costs(conn: sqlite3.Connection, client: LLMClient, *, run_id: str | None = None,
                    book_id: str | None = None) -> int:
    """Replace estimated costs by OpenRouter's /generation total_cost when available."""
    if client.name != "openrouter":
        return 0
    q = ("SELECT id, generation_id FROM llm_requests WHERE generation_id IS NOT NULL AND "
         "cost_status IN ('estimated_from_tokens','unknown') AND "
         "run_id IN (SELECT id FROM runs WHERE "
         "COALESCE(json_extract(config_json, '$.openrouter.provider'), 'openrouter') = 'openrouter')")
    params: list[Any] = []
    if run_id:
        q += " AND run_id=?"
        params.append(run_id)
    if book_id:
        q += " AND book_id=?"
        params.append(book_id)
    n = 0
    for r in conn.execute(q, params).fetchall():
        data = client.get_generation(r["generation_id"])
        if data and isinstance(data.get("total_cost"), (int, float)):
            conn.execute("UPDATE llm_requests SET cost_usd=?, cost_status='reported_generation' WHERE id=?",
                         (float(data["total_cost"]), r["id"]))
            n += 1
    return n


def findings_to_rows(findings: list[Finding]) -> list[dict[str, Any]]:
    return [asdict(f) for f in findings]
