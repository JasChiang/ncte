"""A/B experiment runner (spec §10 M6, §11).

Experiments (same model, same source, fresh workspace each → no shared cache/memory):
  A  paragraph   – one unit per request, no neighbouring context
  B  neighbors   – baseline batches + previous/next units
  C  memory      – context mode: scene memory, characters, summaries (no LLM speaker analysis, no review)
  D  full        – context mode + LLM scene/speaker analysis + selective review
"""

from __future__ import annotations

import csv
import json
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from ..config import Config
from ..epub.validate import validate_epub
from ..exporter import export_book
from ..library import import_book
from ..llm.base import LLMClient
from ..memory.db import connect
from ..reports.cost_report import cost_report
from ..translation.orchestrator import Orchestrator, RunOptions, create_run
from .dataset import Dataset, build_dataset_epub
from .metrics import compute_metrics


@dataclass(frozen=True)
class Experiment:
    key: str
    name: str
    mode: str
    description: str

    def configure(self, cfg: Config) -> None:
        if self.key == "A":
            cfg.translation.include_previous_units = 0
            cfg.translation.include_next_units = 0
            cfg.translation.unit_target_japanese_chars = 1  # one unit per request
            cfg.review.automatic_review = "off"
        elif self.key == "B":
            cfg.review.automatic_review = "off"
        elif self.key == "C":
            cfg.context.scene_analysis = False
            cfg.context.memory_update = True
            cfg.review.automatic_review = "off"
        elif self.key == "D":
            cfg.context.scene_analysis = True
            cfg.context.memory_update = True
            cfg.review.automatic_review = "flagged_only"


EXPERIMENTS = {
    "A": Experiment("A", "paragraph", "baseline", "純段落翻譯（無上下文）"),
    "B": Experiment("B", "neighbors", "baseline", "＋鄰近上下文"),
    "C": Experiment("C", "memory", "context", "＋場景與人物／劇情記憶"),
    "D": Experiment("D", "full", "context", "＋對話與省略指涉分析、選擇性複查"),
}

METRIC_KEYS = ["speaker_accuracy_explicit", "speaker_explicit_precision", "unknown_asserted_rate",
               "weak_evidence_asserted", "unknown_false_assertion_rate", "omitted_argument_error_rate",
               "omission_rate", "omission_addition_rate", "entity_terminology_consistency", "spoiler_violations",
               "epub_structure_integrity", "total_cost_usd", "usd_per_10k_ja_chars", "units_per_minute",
               "retries_per_100_units", "prompt_tokens", "completion_tokens", "requests"]


def run_experiment(exp: Experiment, ds: Dataset, epub: Path, base_cfg: Config, client: LLMClient, *, model: str,
                   budget_usd: float, workdir: Path, run_epubcheck: bool,
                   log: Callable[[str], None] = print) -> dict[str, Any]:
    cfg = Config.model_validate(base_cfg.model_dump())
    cfg.app.workspace = str(workdir)
    exp.configure(cfg)
    conn = connect(cfg.workspace / "ncte.db")
    try:
        book, _ = import_book(conn, cfg, epub)
        opts = RunOptions(mode=exp.mode, model=model, budget_usd=budget_usd, client=client.name)
        rid = create_run(conn, cfg, book, opts)
        if exp.mode == "context":
            from ..translation.context_pipeline import ContextOrchestrator

            orch: Orchestrator = ContextOrchestrator(conn, cfg, client, book, rid, opts, log=lambda m: None)
        else:
            orch = Orchestrator(conn, cfg, client, book, rid, opts, log=lambda m: None)
        t0 = time.monotonic()
        summary = orch.run()
        elapsed = time.monotonic() - t0
        m = compute_metrics(conn, book.id, ds, has_speaker_state=exp.mode == "context")
        out = export_book(conn, cfg, book, run_epubcheck=run_epubcheck)
        v = validate_epub(out.result.output_path, original=epub, run_epubcheck=False)
        checks = 4
        passed = sum([not v.errors, v.stats.get("broken_references", 1) == 0,
                      out.stats["rebuild_errors"] == 0, out.validation.epubcheck.get("status") != "failed"])
        cost = cost_report(conn, run_id=rid)
        t = cost["totals"]
        units = m["translated_units"]
        ja_chars = sum(r["n"] for r in conn.execute(
            "SELECT LENGTH(plain_text) n FROM units WHERE book_id=? AND translatable=1", (book.id,)))
        m.update({
            "epub_structure_integrity": round(passed / checks, 4),
            "total_cost_usd": t["total_cost_upper_usd"],
            "usd_per_10k_ja_chars": round(t["total_cost_upper_usd"] / ja_chars * 10000, 6) if ja_chars else None,
            "units_per_minute": round(units / (elapsed / 60), 1) if elapsed > 0 else None,
            "retries_per_100_units": round(t["requests_with_retries"] / units * 100, 2) if units else None,
            "prompt_tokens": t["prompt_tokens"], "completion_tokens": t["completion_tokens"],
            "requests": t["requests"], "run_status": summary.status, "run_id": rid,
            "epubcheck": out.validation.epubcheck.get("status"),
        })
        log(f"  {exp.key} ({exp.name}): status={summary.status} requests={t['requests']} "
            f"cost=${t['total_cost_upper_usd']:.6f}")
        return m
    finally:
        conn.close()


def run_eval(ds: Dataset, base_cfg: Config, client: LLMClient, *, model: str, experiments: list[str], repeat: int,
             budget_usd: float, out_dir: Path, run_epubcheck: bool = False,
             log: Callable[[str], None] = print) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    epub = build_dataset_epub(ds, out_dir / "eval_source.epub")
    results: dict[str, list[dict[str, Any]]] = {}
    for key in experiments:
        exp = EXPERIMENTS[key]
        for rep in range(repeat):
            log(f"experiment {key} repeat {rep + 1}/{repeat}")
            results.setdefault(key, []).append(
                run_experiment(exp, ds, epub, base_cfg, client, model=model, budget_usd=budget_usd,
                               workdir=out_dir / f"ws_{key}_{rep + 1}", run_epubcheck=run_epubcheck, log=log))
    agg = {k: aggregate(v) for k, v in results.items()}
    report = {
        "dataset": {"name": ds.name, "version": ds.version, "items": len(ds.items),
                    "annotated_utterances": sum(len(i.gold.utterances) for i in ds.items)},
        "client": client.name, "model": model, "repeat": repeat,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "warning": ("FakeLLM run: translation-quality metrics are NOT meaningful (fake output); only the harness, "
                    "EPUB integrity and the deterministic speaker pipeline are exercised.") if client.name == "fake" else None,
        "experiments": {k: {"description": EXPERIMENTS[k].description, "mode": EXPERIMENTS[k].mode,
                            "aggregate": agg[k], "runs": results[k]} for k in results},
    }
    (out_dir / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(report, out_dir / "results.csv")
    (out_dir / "report.html").write_text(render_eval_html(report), encoding="utf-8")
    return report


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in METRIC_KEYS:
        vals = [r[k] for r in runs if isinstance(r.get(k), (int, float))]
        if not vals:
            out[k] = None
            continue
        out[k] = {"mean": round(statistics.fmean(vals), 6), "min": min(vals), "max": max(vals),
                  "stdev": round(statistics.pstdev(vals), 6) if len(vals) > 1 else 0.0}
    return out


def write_csv(report: dict[str, Any], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["experiment", "repeat", "mode", *METRIC_KEYS, "run_status"])
        for key, exp in report["experiments"].items():
            for i, r in enumerate(exp["runs"], 1):
                w.writerow([key, i, exp["mode"], *[r.get(k) for k in METRIC_KEYS], r.get("run_status")])
            w.writerow([key, "mean", exp["mode"], *[(exp["aggregate"].get(k) or {}).get("mean")
                                                    if exp["aggregate"].get(k) else None for k in METRIC_KEYS], ""])


def render_eval_html(report: dict[str, Any]) -> str:
    exps = report["experiments"]
    head = "".join(f"<th>{escape(k)}<br><span class='muted'>{escape(v['description'])}</span></th>" for k, v in exps.items())
    rows = []
    for m in METRIC_KEYS:
        cells = []
        for v in exps.values():
            a = v["aggregate"].get(m)
            cells.append("<td>n/a</td>" if a is None else
                         f"<td>{a['mean']}{'' if a['min'] == a['max'] else f' <span class=muted>({a['min']}–{a['max']})</span>'}</td>")
        rows.append(f"<tr><th>{escape(m)}</th>{''.join(cells)}</tr>")
    warn = f"<p class='warn'>{escape(report['warning'])}</p>" if report.get("warning") else ""
    details = "".join(
        f"<h3>{escape(k)}</h3><pre>{escape(json.dumps(v['runs'][0]['details'], ensure_ascii=False, indent=1))}</pre>"
        for k, v in exps.items())
    return f"""<!doctype html><html lang="zh-Hant-TW"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>NCTE A/B 評測</title>
<style>:root{{--bg:#fbfaf7;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e1db;--warn:#b26b00}}
@media (prefers-color-scheme: dark){{:root{{--bg:#18181a;--fg:#ececee;--muted:#a0a0a8;--line:#333338;--warn:#f0b35a}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"PingFang TC",sans-serif;max-width:1100px;margin:auto;padding:24px 16px}}
table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}
.muted{{color:var(--muted);font-weight:400;font-size:12px}}.warn{{color:var(--warn);font-weight:600}}pre{{white-space:pre-wrap;font-size:12px}}
.wrap{{overflow-x:auto}}</style></head><body>
<h1>NCTE A/B 評測</h1>
<p class="muted">dataset {escape(report['dataset']['name'])} v{report['dataset']['version']} ·
{report['dataset']['items']} items · {report['dataset']['annotated_utterances']} annotated utterances ·
client {escape(report['client'])} · model {escape(report['model'])} · repeat {report['repeat']} · {escape(report['generated_at'])}</p>
{warn}
<div class="wrap"><table><tr><th>metric</th>{head}</tr>{''.join(rows)}</table></div>
<p class="muted">指標以人工標註計算（見 docs/evaluation.md）；不使用 LLM 主觀評分。n/a = 該實驗沒有說話者狀態。</p>
<h2>錯誤明細（第一次重複）</h2>{details}
</body></html>"""
