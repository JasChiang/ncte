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
    # every paid step of the run, not only translating and reading: what it usually costs and an upper estimate
    # (each step at the costliest rate seen before, plus a margin); steps with no history are counted by the share
    # they took in sample runs
    estimate_usd: float = 0.0
    estimate_high_usd: float = 0.0
    stage_estimates: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# What each step's cost follows: the paragraphs it reads, or the whole run (once a book: relations, the second look …;
# the fix check and the review follow how many fixes the run makes, which grows with the book).
PER_CHAR_STAGES = ("translate", "scene_analyze", "memory_update", "scene_windows", "proofread", "reader")
PER_RUN_STAGES = ("verify", "relations", "impressions", "house_style", "naming", "confirm", "fix_check", "review")
HIGH_MARGIN = 1.2
# A step with no history of its own: its cost as a share of translating + reading (scene analysis and memory) in a
# whole-book run (3,689 paragraphs, 2026-10-18; proofreading at medium from two 300-paragraph samples). Proofreading
# depends on its reasoning setting.
SAMPLE_SHARE = {"scene_windows": 0.06, "proofread": 0.17, "proofread:high": 0.55, "reader": 0.04, "verify": 0.04,
                "relations": 0.05, "impressions": 0.01, "house_style": 0.01, "naming": 0.02, "confirm": 0.01,
                "fix_check": 0.05, "review": 0.01}
UNCALIBRATED_HIGH = 2.0  # a step counted by its sample share may cost this many times as much


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
    full = _full_estimate(cfg, opts, history or [conn], stage_chars, todo, breaks, cost_low, cost_high)
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
        reasoning_effort=cfg.openrouter.reasoning_effort, estimate_usd=full["typical"],
        estimate_high_usd=full["high"], stage_estimates=full["stages"])


def _full_estimate(cfg: Config, opts: RunOptions, conns: list[sqlite3.Connection], stage_chars: dict[str, int],
                   todo: list[sqlite3.Row], breaks: int, cost_low: float, cost_high: float) -> dict[str, Any]:
    """Every step the run will pay for: from earlier runs with the same model and reasoning where there are any
    (usual = all their cost over all their characters, high = the costliest run's rate), else by its sample share."""
    from .orchestrator import reasoning_label

    chars = sum(len(match_text(r["plain_text"])) for r in todo)
    want: dict[str, int] = dict(stage_chars)
    if opts.mode == "context":
        if breaks:
            want["scene_windows"] = chars
        if cfg.context.proofread:
            want["proofread"] = chars
        if cfg.context.reader_pass:
            want["reader"] = chars
        for st in PER_RUN_STAGES:
            want[st] = chars
    effort = {st: reasoning_label(reasoning_param(reasoning_setting(cfg, st))) for st in want}
    rates = run_rates(conns, opts.model, cfg.openrouter.reasoning_effort, provider=cfg.openrouter.provider,
                      per_stage=effort)
    stages: dict[str, dict[str, Any]] = {}
    core = ("translate", "scene_analyze", "memory_update")
    if all(st in rates for st in core if st in want):
        base_lo = sum(rates[st]["typical"] * want[st] for st in core if st in want)
        base_hi = sum(rates[st]["high"] * want[st] for st in core if st in want) * HIGH_MARGIN
    else:  # no history for this model: the token model's range for translating, reading and cutting scenes
        base_lo, base_hi = cost_low, cost_high
    for st, n in want.items():
        if st in rates:
            stages[st] = {"usd": round(rates[st]["typical"] * n, 4), "high_usd": round(rates[st]["high"] * n * HIGH_MARGIN, 4),
                          "from": f"{rates[st]['runs']} earlier run(s)"}
        elif st in core or (st == "scene_windows" and base_lo == cost_low):
            continue  # in the token model's range
        else:
            share = SAMPLE_SHARE.get(f"{st}:{effort.get(st)}", SAMPLE_SHARE.get(st, 0.0))
            stages[st] = {"usd": round(base_lo * share, 4), "high_usd": round(base_hi * share * UNCALIBRATED_HIGH, 4),
                          "from": "sample share"}
    if base_lo == cost_low and not all(st in rates for st in core if st in want):
        stages["translate+read"] = {"usd": round(cost_low, 4), "high_usd": round(cost_high, 4), "from": "token model"}
    return {"typical": round(sum(v["usd"] for v in stages.values()), 4),
            "high": round(sum(v["high_usd"] for v in stages.values()), 4), "stages": stages}


def _sizer(conn: sqlite3.Connection) -> Any:
    """Characters of these paragraphs (cached per database)."""
    text: dict[str, int] = {}

    def size(ids: list[str]) -> int:
        missing = [i for i in ids if i not in text]
        for i in range(0, len(missing), 500):
            part = missing[i:i + 500]
            for uid, t in conn.execute(f"SELECT id, plain_text FROM units WHERE id IN ({','.join('?' * len(part))})", part):
                text[uid] = len(match_text(t))
        return sum(text.get(i, 0) for i in ids)
    return size


def run_rates(conns: list[sqlite3.Connection], model: str, effort: str | None, *, provider: str = "openrouter",
              per_stage: dict[str, str | None] | None = None, min_chars: int = 5000,
              recent: int = 10) -> dict[str, dict[str, Any]]:
    """Cost per character of each step, run by run, from earlier requests with the same model and reasoning setting.
    A step that reads paragraphs is divided by the characters it read; a once-a-run step by the characters the run
    translated (only runs that read the book, so a proofreading-only rerun does not count as a book). Small runs are
    left out (their fixed overhead per request makes a character look dear), and only the ``recent`` latest runs of
    a step count (prompts change)."""
    per: dict[str, list[tuple[str, float, float]]] = {}  # stage → [(run, cost, chars)]
    seen: set[str] = set()  # a run is counted once (a copied workspace carries the same runs)
    for conn in conns:
        rows = conn.execute(
            "SELECT run_id, stage, cost_usd, unit_ids_json, reasoning_effort FROM llm_requests WHERE model=? AND "
            "status='succeeded' AND cost_status IN ('reported','reported_generation') AND cost_usd IS NOT NULL "
            "AND run_id IN (SELECT id FROM runs WHERE "
            "COALESCE(json_extract(config_json, '$.openrouter.provider'), 'openrouter') = ?)", (model, provider)).fetchall()
        size = _sizer(conn)
        runs: dict[str, dict[str, list[float]]] = {}
        translated: dict[str, set[str]] = {}
        for r in rows:
            st = r["stage"].split(":")[0]
            ids = json.loads(r["unit_ids_json"] or "[]")
            if st == "translate":
                translated.setdefault(r["run_id"], set()).update(ids)
            if r["reasoning_effort"] != (per_stage or {}).get(st, effort):
                continue
            a = runs.setdefault(r["run_id"], {}).setdefault(st, [0.0, 0.0])
            a[0] += float(r["cost_usd"])
            a[1] += size(ids) if st in PER_CHAR_STAGES else 0
        for run, by_stage in runs.items():
            if run in seen:
                continue
            seen.add(run)
            book = size(sorted(translated.get(run, set())))
            read = "scene_analyze" in by_stage or "memory_update" in by_stage
            for st, (cost, n) in by_stage.items():
                if st in PER_CHAR_STAGES and n >= min_chars:
                    per.setdefault(st, []).append((run, cost, n))
                elif st not in PER_CHAR_STAGES and read and book >= min_chars:
                    per.setdefault(st, []).append((run, cost, book))
    out: dict[str, dict[str, Any]] = {}
    for st, xs in per.items():
        xs = sorted(xs)[-recent:]  # run ids begin with their start time
        cost, n = sum(c for _, c, _ in xs), sum(k for _, _, k in xs)
        out[st] = {"typical": cost / n, "high": max(c / k for _, c, k in xs), "runs": len(xs)}
    return out


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
