"""``translate --dry-run``: plan + price estimate, never calls a paid completion endpoint."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
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
            log: Any = print) -> DryRunPlan:
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
    calib = calibrate(conn, opts.model, cfg.openrouter.reasoning_effort)
    calibrated = None
    if all(s in calib for s in stage_chars):
        calibrated = round(sum(calib[s]["usd_per_char"] * n for s, n in stage_chars.items()), 4)
    notes = ["Estimates use a pessimistic 1 token ≈ 1 CJK character model; real usage is read from the API.",
             "Retries, splits and selective review can add requests (bounded by --budget-usd)."]
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


def calibrate(conn: sqlite3.Connection, model: str, effort: str | None, *, min_requests: int = 3,
              min_chars: int = 500) -> dict[str, dict[str, Any]]:
    """Actual cost per source character, per stage, from earlier requests with the same model and
    reasoning effort (any book). Only stages with enough samples are returned."""
    rows = conn.execute(
        "SELECT stage, cost_usd, unit_ids_json FROM llm_requests WHERE model=? AND reasoning_effort IS ? AND "
        "status='succeeded' AND cost_status IN ('reported','reported_generation') AND cost_usd IS NOT NULL",
        (model, effort)).fetchall()
    agg: dict[str, list[float]] = {}  # stage → [requests, cost, chars]
    for r in rows:
        ids = json.loads(r["unit_ids_json"])
        if not ids:
            continue
        ph = ",".join("?" * len(ids))
        chars = sum(len(match_text(x[0])) for x in conn.execute(f"SELECT plain_text FROM units WHERE id IN ({ph})", ids))
        a = agg.setdefault(r["stage"], [0.0, 0.0, 0.0])
        a[0] += 1
        a[1] += float(r["cost_usd"])
        a[2] += chars
    out = {}
    for stage, (n, cost, nchars) in agg.items():
        if n >= min_requests and nchars >= min_chars:
            out[stage] = {"requests": int(n), "chars": int(nchars), "usd_per_char": cost / nchars,
                          "usd_per_10k_chars": round(cost / nchars * 10000, 4)}
    return out
