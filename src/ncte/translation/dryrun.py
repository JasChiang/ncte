"""``translate --dry-run``: plan + price estimate, never calls a paid completion endpoint."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..config import Config, reasoning_param, reasoning_setting
from ..epub.units import match_text
from ..library import Book
from ..llm.base import LLMClient
from ..llm.cost import estimate_tokens
from ..llm.prompt_loader import load_prompt
from .cache import glossary_entries, glossary_hash, is_cached, latest_translation, relevant_glossary
from .context_builder import BODY_KINDS, META_KINDS
from .orchestrator import RunOptions, make_batches, resolve_pricing


@dataclass
class DryRunPlan:
    model: str
    pricing_source: str
    price_per_million: tuple[float, float]
    structured_mode: str
    units_total: int
    units_cached: int
    units_to_translate: int
    japanese_chars: int
    batches: int
    scenes: int
    requests_estimate: int
    input_tokens_estimate: int
    output_tokens_low: int
    output_tokens_high: int
    cost_low_usd: float
    cost_high_usd: float
    budget_usd: float
    sample_batches: list[list[str]] = field(default_factory=list)
    calibrated_cost_usd: float | None = None
    calibration: dict[str, Any] = field(default_factory=dict)
    reasoning_effort: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def dry_run(conn: sqlite3.Connection, cfg: Config, book: Book, opts: RunOptions, client: LLMClient,
            log: Any = print, *, history: list[sqlite3.Connection] | None = None) -> DryRunPlan:
    """``history``: databases whose real requests calibrate the estimate (default: ``conn`` only)."""
    pricing, source = resolve_pricing(conn, client, opts.model, allow_snapshot=True, log=log)
    from .orchestrator import check_reasoning_effort

    check_reasoning_effort(cfg, pricing)
    prompt = load_prompt("translate")
    kinds = list(BODY_KINDS) + list(META_KINDS)
    rows = conn.execute(
        f"SELECT * FROM units WHERE book_id=? AND translatable=1 AND kind IN ({','.join('?' * len(kinds))}) "
        "ORDER BY ordinal", (book.id, *kinds)).fetchall()
    if opts.unit_ids:
        rows = [r for r in rows if r["id"] in set(opts.unit_ids)]
    if opts.limit is not None:
        rows = [r for r in rows if r["kind"] in BODY_KINDS][: opts.limit]
    gl = glossary_entries(conn, book.id)
    todo, cached, old_revision = [], 0, 0
    for r in rows:
        tr = latest_translation(conn, r["id"], r["source_hash"])
        gh = glossary_hash(relevant_glossary(gl, r["plain_text"]))
        if not opts.force and is_cached(tr, prompt_version=prompt.version_id, model=opts.model, mode=opts.mode,
                                        gl_hash=gh):
            cached += 1
            if tr is not None and tr["status"] != "human" and tr["prompt_version"] != prompt.version_id:
                old_revision += 1
        else:
            todo.append(r)
    target = cfg.translation.unit_target_japanese_chars
    body = [r for r in todo if r["kind"] not in META_KINDS]
    meta = [r for r in todo if r["kind"] in META_KINDS]
    batches = make_batches(body, target, by_scene=opts.mode == "context") + make_batches(meta, target)
    sys_tokens = estimate_tokens(prompt.text)
    in_tok = out_low = out_high = 0
    n_req = len(batches)
    for b in batches:
        payload = json.dumps([{"unit_id": r["id"], "source": r["source_text"]} for r in b.rows], ensure_ascii=False)
        # input: system prompt + units + ~2x context (neighbours, glossary, memory)
        in_tok += sys_tokens + estimate_tokens(payload) * (3 if opts.mode == "baseline" else 4)
        out_low += int(b.chars * 0.9) + 25 * len(b.rows)
        out_high += int(b.chars * 2.2) + 60 * len(b.rows)
    scenes = 0
    if opts.mode == "context":
        scene_ids = {r["scene_id"] for r in todo if r["scene_id"]}
        scenes = len(scene_ids)
        chars = sum(len(r["plain_text"]) for r in todo)
        n_req += 2 * scenes
        in_tok += 2 * (chars + scenes * 1500)
        out_low += int(chars * 0.5)
        out_high += int(chars * 1.5)
    breaks = 0
    if opts.mode == "context" and cfg.context.scene_breaks == "llm" and not opts.unit_ids and todo:
        # the model cuts scenes first, each chapter in windows (ADR-057); a sample only what it reaches
        from ..analysis import scene_windows as sw
        from ..analysis.scene import ensure_chapters
        from ..memory.db import Transaction

        end = max(r["ordinal"] for r in rows) if opts.limit is not None else None
        with Transaction(conn):
            ensure_chapters(conn, book.id)  # local: the TOC read once and kept
        decided = {r[0] for r in conn.execute("SELECT unit_id FROM scene_breaks WHERE book_id=? AND origin='chapter'",
                                              (book.id,))}
        paras = 0
        for ch in conn.execute("SELECT * FROM chapters WHERE book_id=? ORDER BY seq", (book.id,)).fetchall():
            if end is not None and ch["start_ordinal"] > end:
                break
            crows = sw.chapter_rows(conn, book.id, ch)
            if crows and (opts.force or crows[0]["id"] not in decided):
                paras += len(crows)
                breaks += -(-len(crows) // sw.WINDOW_PARAGRAPHS) + 1  # windows overlap by the unfinished scene
        prompt_name, per_para = "scene_windows", sw.TEXT_CHARS // 2 + 20
        n_req += breaks
        in_tok += breaks * estimate_tokens(load_prompt(prompt_name).text) + int(paras * per_para * 1.3)
        out_low += paras * 5
        out_high += paras * 30  # reasoning
    cost_low = in_tok * pricing.prompt + out_low * pricing.completion
    cost_high = in_tok * pricing.prompt * 1.3 + out_high * pricing.completion
    stage_chars = {"translate": sum(len(match_text(r["plain_text"])) for r in todo)}
    if opts.mode == "context":
        sids = {r["scene_id"] for r in todo if r["scene_id"]}
        scene_chars = sum(len(match_text(r["plain_text"])) for r in conn.execute(
            "SELECT plain_text, scene_id FROM units WHERE book_id=?", (book.id,)) if r["scene_id"] in sids)
        if cfg.context.scene_analysis:
            stage_chars["scene_analyze"] = scene_chars
        if cfg.context.memory_update:
            stage_chars["memory_update"] = scene_chars
    from .orchestrator import reasoning_label

    calib = calibrate(history or [conn], opts.model, cfg.openrouter.reasoning_effort, provider=cfg.openrouter.provider,
                      per_stage={st: reasoning_label(reasoning_param(reasoning_setting(cfg, st))) for st in stage_chars})
    calibrated = None
    if all(s in calib for s in stage_chars):
        calibrated = round(sum(calib[s]["usd_per_char"] * n for s, n in stage_chars.items()), 4)
    notes = ["Estimates use a pessimistic 1 token ≈ 1 CJK character model; real usage is read from the API.",
             "Retries, splits and selective review can add requests (bounded by --budget-usd)."]
    if client.name == "fal":
        notes.append("fal: OpenRouter catalog prices are reference estimates; final fal billing may differ. "
                     "Fal balance and generation reconciliation are not available in this adapter.")
    if breaks:
        notes.append(f"includes {breaks} scene-cutting requests before reading")
    if old_revision:
        notes.append(f"{old_revision} cached units were translated with an earlier revision of {prompt.version_id.split('#')[0]}"
                     " (kept; use --force or retranslate to refresh them)")
    if pricing.structured_mode != "json_schema":
        notes.append(f"model lacks structured_outputs → {pricing.structured_mode} + local validation")
    return DryRunPlan(
        model=opts.model, pricing_source=source, price_per_million=pricing.per_million(),
        structured_mode=pricing.structured_mode, units_total=len(rows), units_cached=cached,
        units_to_translate=len(todo), japanese_chars=sum(len(r["plain_text"]) for r in todo),
        batches=len(batches), scenes=scenes, requests_estimate=n_req, input_tokens_estimate=in_tok,
        output_tokens_low=out_low, output_tokens_high=out_high, cost_low_usd=round(cost_low, 6),
        cost_high_usd=round(cost_high, 6), budget_usd=opts.budget_usd,
        sample_batches=[b.unit_ids[:5] for b in batches[:3]], notes=notes, calibrated_cost_usd=calibrated,
        calibration={s: {k: v for k, v in c.items() if k != "usd_per_char"} for s, c in calib.items()},
        reasoning_effort=cfg.openrouter.reasoning_effort)


def calibrate(conns: list[sqlite3.Connection], model: str, effort: str | None, *, min_requests: int = 3,
              min_chars: int = 500, provider: str = "openrouter",
              per_stage: dict[str, str | None] | None = None) -> dict[str, dict[str, Any]]:
    """Actual cost per source character, per stage, from earlier requests with the same model and
    reasoning setting (any book, any series; ``per_stage`` when steps use their own). Only stages with enough
    samples are returned."""
    agg: dict[str, list[float]] = {}  # stage → [requests, cost, chars]
    for conn in conns:
        _calibrate_one(conn, model, effort, agg, provider, per_stage or {})
    return _calibration_result(agg, min_requests, min_chars)


def _calibrate_one(conn: sqlite3.Connection, model: str, effort: str | None, agg: dict[str, list[float]], provider: str,
                   per_stage: dict[str, str | None]) -> None:
    rows = conn.execute(
        "SELECT stage, cost_usd, unit_ids_json, reasoning_effort FROM llm_requests WHERE model=? AND "
        "status='succeeded' AND cost_status IN ('reported','reported_generation') AND cost_usd IS NOT NULL "
        "AND run_id IN (SELECT id FROM runs WHERE "
        "COALESCE(json_extract(config_json, '$.openrouter.provider'), 'openrouter') = ?)",
        (model, provider)).fetchall()
    for r in rows:
        if r["reasoning_effort"] != per_stage.get(r["stage"].split(":")[0], effort):
            continue
        ids = json.loads(r["unit_ids_json"])
        if not ids:
            continue
        ph = ",".join("?" * len(ids))
        chars = sum(len(match_text(x[0])) for x in conn.execute(f"SELECT plain_text FROM units WHERE id IN ({ph})", ids))
        a = agg.setdefault(r["stage"], [0.0, 0.0, 0.0])
        a[0] += 1
        a[1] += float(r["cost_usd"])
        a[2] += chars


def _calibration_result(agg: dict[str, list[float]], min_requests: int, min_chars: int) -> dict[str, dict[str, Any]]:
    out = {}
    for stage, (n, cost, nchars) in agg.items():
        if n >= min_requests and nchars >= min_chars:
            out[stage] = {"requests": int(n), "chars": int(nchars), "usd_per_char": cost / nchars,
                          "usd_per_10k_chars": round(cost / nchars * 10000, 4)}
    return out
