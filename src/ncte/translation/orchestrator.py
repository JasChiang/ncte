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

from ..config import Config
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
from .context_builder import BODY_KINDS, META_KINDS, glossary_payload, heading_hints, neighbor_context
from .translator import Translator, UnitInput

MAX_UNITS_PER_BATCH = 60
MAX_JOB_ATTEMPTS = 3


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
    address_only: bool = False  # `ncte names address-check` (ADR-037, read pass already done)
    reader_only: bool = False  # `ncte reader-check` (ADR-038, a book already translated)


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
            save_snapshot(conn, models, client.name)
        source = f"{client.name}:/models (live)"
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


def check_reasoning_effort(cfg: Config, pricing: Pricing) -> None:
    eff = cfg.openrouter.reasoning_effort
    if eff and pricing.reasoning_efforts and eff not in pricing.reasoning_efforts:
        raise NCTEError(f"reasoning_effort '{eff}' not supported by {pricing.model} "
                        f"(supported: {', '.join(pricing.reasoning_efforts)})")
    if eff and "reasoning" not in pricing.supported_parameters:
        raise NCTEError(f"{pricing.model} does not accept the reasoning parameter")


# --------------------------------------------------------------------------- run records


def new_run_id() -> str:
    return "r_" + datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(3)


def create_run(conn: sqlite3.Connection, cfg: Config, book: Book, opts: RunOptions) -> str:
    rid = new_run_id()
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


def run_spent(conn: sqlite3.Connection, run_id: str) -> tuple[float, int]:
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN cost_usd IS NOT NULL THEN cost_usd "
        "WHEN cost_status IN ('unknown','pending') THEN est_cost_usd ELSE 0 END),0) AS spent, "
        "SUM(CASE WHEN cost_status IN ('unknown','pending') THEN 1 ELSE 0 END) AS unk "
        "FROM llm_requests WHERE run_id=?", (run_id,)).fetchone()
    return float(row["spent"] or 0), int(row["unk"] or 0)


class DbRecorder:
    """Persists every LLM request (thread-safe; shares the orchestrator lock)."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock, run_id: str, book_id: str,
                 pricing: Pricing, *, reuse: bool = True):
        self.conn, self.lock, self.run_id, self.book_id, self.pricing = conn, lock, run_id, book_id, pricing
        self.reuse = reuse

    def start(self, req: LLMRequest, unit_ids: list[str], est_cost: float) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO llm_requests(run_id, book_id, stage, request_hash, model, prompt_version, "
                "structured_mode, unit_ids_json, est_cost_usd, status, cost_status, created_at, reasoning_effort) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.run_id, self.book_id, req.stage, req.request_hash(), req.model, req.prompt_version,
                 req.structured_mode, json.dumps(unit_ids), est_cost, "sent", "pending", utcnow(),
                 (req.extra.get("reasoning") or {}).get("effort")))
            return int(cur.lastrowid or 0)

    def success(self, rid: int, resp: LLMResponse, parse_status: str) -> None:
        cost, status = resp.cost_usd, "reported"
        if cost is None:
            u = resp.usage or {}
            pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
            if pt or ct:
                cost, status = self.pricing.cost(pt, ct), "estimated_from_tokens"
            else:
                status = "unknown"
        req_status = "succeeded" if parse_status in ("strict", "repaired_fence") else f"invalid_output:{parse_status}"
        with self.lock:
            self.conn.execute(
                "UPDATE llm_requests SET provider=?, usage_json=?, generation_id=?, cost_usd=?, cost_status=?, "
                "status=?, attempts=?, latency_ms=?, response_text=?, structured_mode=?, finished_at=? WHERE id=?",
                (resp.provider, json.dumps(resp.usage), resp.generation_id, cost, status, req_status,
                 resp.attempts, resp.latency_ms, resp.content, resp.structured_mode, utcnow(), rid))

    def failure(self, rid: int, exc: Exception) -> None:
        billed = bool(getattr(exc, "possibly_billed", False))
        with self.lock:
            self.conn.execute(
                "UPDATE llm_requests SET status='failed', error=?, cost_status=?, cost_usd=?, finished_at=? WHERE id=?",
                (f"{type(exc).__name__}: {str(exc)[:500]}", "unknown" if billed else "none",
                 None if billed else 0.0, utcnow(), rid))


# --------------------------------------------------------------------------- orchestrator


@dataclass
class Batch:
    rows: list[sqlite3.Row]
    meta: bool = False
    scene_id: str | None = None

    @property
    def unit_ids(self) -> list[str]:
        return [r["id"] for r in self.rows]

    @property
    def chars(self) -> int:
        return sum(len(r["plain_text"]) for r in self.rows)


def make_batches(rows: list[sqlite3.Row], target: int, *, by_scene: bool = False,
                 position: dict[int, int] | None = None) -> list[Batch]:
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
        if cur and (gap or key != cur_key or cur_len + n > target or len(cur) >= MAX_UNITS_PER_BATCH):
            batches.append(Batch(cur, cur_key[2] if cur_key else False, cur_key[1] if cur_key else None))
            cur, cur_len = [], 0
        cur.append(r)
        cur_len += n
        cur_key = key
    if cur:
        batches.append(Batch(cur, cur_key[2] if cur_key else False, cur_key[1] if cur_key else None))
    return batches


class Orchestrator:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, client: LLMClient, book: Book, run_id: str,
                 opts: RunOptions, *, log: Callable[[str], None] = print):
        self.conn, self.cfg, self.client, self.book, self.run_id, self.opts = conn, cfg, client, book, run_id, opts
        self.log = log
        self.lock = threading.RLock()
        self.notes: list[str] = []
        self.prompt = load_prompt("translate")
        self.pricing: Pricing | None = None
        self.translator: Translator | None = None
        self.model = opts.model
        self._stop_reason: str | None = None
        self.glossary: list[dict[str, Any]] = []
        self.link_revealed: dict[str, tuple[float, int]] = {}
        self.volume = 1.0

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
        recorder = DbRecorder(self.conn, self.lock, self.run_id, self.book.id, self.pricing)
        self.recorder = recorder
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

    def _estimate(self, payload: dict[str, Any], out_chars: int) -> float:
        assert self.pricing is not None
        text = self.prompt.text + json.dumps(payload, ensure_ascii=False)
        return estimate_request_cost(self.pricing, text, out_chars)[2]

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
                                       whole_scene=bool(self.opts.unit_ids))
                text += " " + " ".join(c["source"] for c in ctx["context_before"] + ctx["context_after"])
            ctx["glossary"] = glossary_payload(self.glossary, text, upto=(self.volume, batch.rows[-1]["ordinal"]),
                                               revealed=self.link_revealed)
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
                self.conn.execute(
                    "INSERT INTO translations(unit_id, target_lang, text, source_hash, context_hash, prompt_version, "
                    "model_id, glossary_version, mode, status, run_id, request_id, flags_json, notes_json, created_at, "
                    "translator_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, self.cfg.app.language, ur.text, r["source_hash"], ctx_hash, self.prompt.version_id,
                     self.model, gh, self.opts.mode, status, self.run_id, ur.request_id,
                     json.dumps(ur.flags, ensure_ascii=False), json.dumps(ur.notes, ensure_ascii=False), utcnow(),
                     ur.translator_note or None))
                for f in ur.findings:
                    if f.severity != "info":
                        self._finding(uid, f.type, f.severity, f.evidence)
                for flag in ur.flags:
                    if flag in ("speaker_uncertain", "term_uncertain", "possible_source_error"):
                        self._finding(uid, f"model_flag:{flag}", "low", "; ".join(ur.notes)[:300])
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

    def _finding(self, unit_id: str | None, typ: str, sev: str, evidence: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO qa_findings(run_id, book_id, unit_id, type, severity, evidence, created_at) "
                "VALUES (?,?,?,?,?,?,?)", (self.run_id, self.book.id, unit_id, typ, sev, evidence, utcnow()))

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
    def run(self) -> RunSummary:
        self._set_run_status("running")
        try:
            self.prepare()
            if self.opts.review_only:
                self.review_existing()
            elif self.opts.naming_only:
                self.propose_names()
            elif self.opts.gender_only:
                self.scan_genders()
            elif self.opts.address_only:
                self.classify_address()
            elif self.opts.reader_only:
                self.reader_pass(record_job=False)
            else:
                self.ensure_jobs()
                self.execute()
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

    def classify_address(self) -> None:
        raise NCTEError("address check needs context mode")

    def reader_pass(self, *, record_job: bool = True) -> dict[str, int]:
        raise NCTEError("the reader's look needs context mode")

    def scan_genders(self) -> None:
        raise NCTEError("gender scan needs context mode")

    def review(self, unit_ids: list[str]) -> None:
        from .review import review_scope

        stats = review_scope(self, unit_ids)
        if stats["reviewed"]:
            self.log(f"  review: {stats}")

    def run_meta(self) -> None:
        meta_rows = self.pending_rows(meta=True)
        if not meta_rows:
            return
        reused = self.reuse_heading_translations(meta_rows)
        rest = [r for r in meta_rows if r["id"] not in reused]
        self.run_batches(make_batches(rest, self.cfg.translation.unit_target_japanese_chars))

    def reuse_heading_translations(self, rows: list[sqlite3.Row]) -> set[str]:
        hints = {h["ja"]: h["zh"] for h in heading_hints(self.conn, self.book.id)}
        done: set[str] = set()
        with self.lock, Transaction(self.conn):
            for r in rows:
                zh = hints.get(r["plain_text"])
                if not zh:
                    continue
                src = r["source_text"]
                if src == r["plain_text"]:
                    text = zh
                elif src == f"⟦1⟧{r['plain_text']}⟦/1⟧":
                    text = f"⟦1⟧{zh}⟦/1⟧"
                else:
                    continue
                self.conn.execute(
                    "INSERT INTO translations(unit_id, target_lang, text, source_hash, prompt_version, model_id, "
                    "glossary_version, mode, status, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (r["id"], self.cfg.app.language, text, r["source_hash"], self.prompt.version_id, self.model,
                     glossary_hash(relevant_glossary(self.glossary, r["plain_text"])), self.opts.mode, "succeeded",
                     self.run_id, utcnow()))
                self.conn.execute("UPDATE jobs SET status='succeeded', checkpoint_json=?, updated_at=? WHERE "
                                  "run_id=? AND stage='translate' AND target_id=?",
                                  (json.dumps({"reused_heading": True}), utcnow(), self.run_id, r["id"]))
                done.add(r["id"])
        if done:
            self.log(f"TOC/title: reused {len(done)} chapter-heading translations")
        return done

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
                    spent, unk = run_spent(self.conn, self.run_id)
                assert counts is not None
                self.log(f"[{i + 1}/{total}] {len(b.rows)} units: ok {counts['ok']}, review "
                         f"{counts.get('needs_review', 0)}, failed {counts['failed']} | spent ≈ ${spent:.4f}"
                         + (f" ({unk} unknown-cost requests)" if unk else ""))
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
         "cost_status IN ('estimated_from_tokens','unknown')")
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
