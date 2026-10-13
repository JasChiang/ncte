"""``ncte`` command-line interface (spec §8)."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from . import __version__
from .config import Config, get_api_key, load_config, redact
from .errors import NCTEError
from .library import Book, resolve_book

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="NCTE — context-aware Japanese EPUB → Traditional Chinese (Taiwan) translator.")


class State:
    config_path: str | None = None
    workspace: str | None = None
    _cfg: Config | None = None
    _conn: sqlite3.Connection | None = None
    _ws: Any = None

    @property
    def cfg(self) -> Config:
        if self._cfg is None:
            self._cfg = load_config(self.config_path, workspace=self.workspace)
        return self._cfg

    @property
    def ws(self) -> Any:
        if self._ws is None:
            from .workspace import Workspace

            self._ws = Workspace(self.cfg)
        return self._ws

    @property
    def conn(self) -> sqlite3.Connection:
        """Database of the book selected by the current command (library index if none)."""
        if self._conn is None:
            self._conn = self.ws.lib
        return self._conn


state = State()


def _version(value: bool) -> None:
    if value:
        typer.echo(f"ncte {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    config: Annotated[str | None, typer.Option("--config", "-c", help="YAML config file")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace", "-w", help="Workspace directory")] = None,
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True,
                                          help="Show version and exit")] = False,
) -> None:
    state.config_path = config
    state.workspace = workspace
    state._cfg = None
    state._conn = None
    state._ws = None


def echo_json(data: Any) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def make_client(cfg: Config, *, fake: bool = False, need_key: bool = True) -> Any:
    if fake:
        from .llm.fake import FakeLLM

        models = list({cfg.openrouter.model, *(cfg.openrouter.fallback_models or [])})
        return FakeLLM(models=models)
    from .llm.openrouter import OpenRouterClient

    key = get_api_key()
    if need_key and not key:
        raise NCTEError("OPENROUTER_API_KEY is not set. Export it (or put it in ./.env), or use --dry-run / --fake.")
    o = cfg.openrouter
    return OpenRouterClient(key, base_url=o.base_url, timeout=o.timeout_seconds, max_attempts=o.max_attempts,
                            app_title=o.app_title)


def _book(ref: str, *, auto_import: bool = False) -> Book:
    """Resolve a book (path or id) through the library and select its series database."""
    if state.ws.find_book(ref) is None and auto_import and Path(ref).expanduser().is_file():
        conn, book, _ = state.ws.import_book(ref)
        typer.echo(f"imported {book.id}")
    else:
        conn, book = state.ws.open_book(ref)
    state._conn = conn
    return book


# --------------------------------------------------------------------------- commands


@app.command("import")
def import_cmd(epub: Annotated[Path, typer.Argument(help="Source EPUB (DRM-free)")],
               series: Annotated[str | None, typer.Option("--series", help="Series title or id (created if new); "
                                                          "default: a new series named after the book")] = None,
               volume: Annotated[float | None, typer.Option("--volume", help="Volume number in the series "
                                                            "(e.g. 1, 2, 2.5); default: next")] = None,
               label: Annotated[str | None, typer.Option("--label", help="Display label, e.g. 第3集")] = None) -> None:
    """Validate an EPUB and build the local unit index (no LLM calls)."""
    conn, book, created = state.ws.import_book(epub, series=series, volume=volume, label=label)
    state._conn = conn
    n = conn.execute("SELECT COUNT(*), SUM(translatable) FROM units WHERE book_id=?", (book.id,)).fetchone()
    s = conn.execute("SELECT COUNT(*) FROM scenes WHERE book_id=?", (book.id,)).fetchone()[0]
    b = conn.execute("SELECT s.title, b.volume_index FROM books b JOIN series s ON s.id=b.series_id WHERE b.id=?",
                     (book.id,)).fetchone()
    typer.echo(f"{'imported' if created else 'already imported'}: {book.id}  「{book.title}」")
    typer.echo(f"series: «{b[0]}» volume {b[1]:g}")
    typer.echo(f"units: {n[0]} ({n[1]} translatable), scenes: {s}")
    typer.echo(f"workspace: {state.cfg.workspace}")


@app.command()
def inspect(epub: Annotated[Path, typer.Argument()],
            as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show package structure, unit statistics and units that will be kept in Japanese."""
    from .epub.parser import load_epub
    from .epub.reassemble import extract_all_units

    pkg = load_epub(epub)
    units = extract_all_units(pkg)
    kinds: dict[str, int] = {}
    for _, u in units:
        kinds[u.kind] = kinds.get(u.kind, 0) + 1
    unsafe = [{"href": u.resource_href, "xpath": u.xpath, "reason": u.skip_reason, "text": u.plain_text[:60]}
              for _, u in units if not u.translatable and u.skip_reason != "no_letters"]
    info: dict[str, Any] = {
        "file": str(epub), "sha256": pkg.sha256, "title": pkg.title, "language": pkg.language,
        "epub_version": pkg.version, "opf": pkg.opf_path, "nav": pkg.nav_href, "ncx": pkg.ncx_href,
        "page_progression": pkg.page_progression, "spine": [s.href for s in pkg.spine],
        "manifest_items": len(pkg.manifest), "images": len(pkg.images()),
        "units": len(units), "translatable_units": sum(1 for _, u in units if u.translatable),
        "japanese_chars": sum(len(u.plain_text) for _, u in units if u.translatable),
        "units_by_kind": kinds, "ruby_units": sum(1 for _, u in units if u.ruby_count),
        "unsafe_units_kept_original": unsafe, "warnings": pkg.warnings,
    }
    found = state.ws.find_book(epub)
    info["imported_as"] = found[1] if found else None
    if as_json:
        echo_json(info)
        return
    for k, v in info.items():
        if k in ("spine",):
            typer.echo(f"{k}: {len(v)} items")
        elif k == "unsafe_units_kept_original":
            typer.echo(f"{k}: {len(v)}")
            for x in v[:20]:
                typer.echo(f"  - {x['href']} {x['xpath']} [{x['reason']}] {x['text']}")
        else:
            typer.echo(f"{k}: {v}")


@app.command()
def models(refresh: Annotated[bool, typer.Option("--refresh", help="Fetch live list from OpenRouter")] = False,
           filter_: Annotated[str | None, typer.Option("--filter", help="substring filter on model id")] = None,
           as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show OpenRouter model availability and current per-token pricing (live API, never hard-coded)."""
    from .llm.cost import Pricing, find_model, latest_snapshot, save_snapshot
    from .memory.db import Transaction

    cfg = state.cfg
    snap = latest_snapshot(state.conn)
    if refresh or not snap:
        client = make_client(cfg, need_key=False)
        data = client.list_models()
        with Transaction(state.conn):
            save_snapshot(state.conn, data, "openrouter")
        snap = latest_snapshot(state.conn)
    assert snap is not None
    fetched, data = snap
    target = cfg.openrouter.model
    m = find_model(data, target)
    rows = []
    for x in data:
        if filter_ and filter_ not in x["id"]:
            continue
        if not filter_ and x["id"] != target:
            continue
        try:
            p = Pricing.from_model_json(x)
            rows.append({"id": x["id"], "input_usd_per_M": round(p.per_million()[0], 6),
                         "output_usd_per_M": round(p.per_million()[1], 6), "context": x.get("context_length"),
                         "structured_outputs": "structured_outputs" in (x.get("supported_parameters") or [])})
        except NCTEError:
            rows.append({"id": x["id"], "pricing": "variable"})
    if as_json:
        echo_json({"fetched_at": fetched, "configured_model": target, "available": m is not None, "models": rows})
        return
    typer.echo(f"model list fetched at {fetched} ({len(data)} models)")
    typer.echo(f"configured model {target}: {'AVAILABLE' if m else 'NOT FOUND'}")
    for r in rows:
        typer.echo("  " + "  ".join(f"{k}={v}" for k, v in r.items()))


@app.command()
def translate(
    epub: Annotated[str, typer.Argument(help="EPUB path or book id")],
    mode: Annotated[str, typer.Option(help="baseline | context")] = "",
    model: Annotated[str | None, typer.Option(help="OpenRouter model id")] = None,
    budget_usd: Annotated[float | None, typer.Option("--budget-usd", help="Hard cost ceiling for this run")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Plan + price estimate only; no paid calls")] = False,
    limit_units: Annotated[int | None, typer.Option("--limit-units", help="Only the first N body units (smoke test)")] = None,
    unit_id: Annotated[list[str] | None, typer.Option("--unit-id", help="Restrict to these unit ids")] = None,
    force: Annotated[bool, typer.Option("--force", help="Ignore translation cache")] = False,
    fake: Annotated[bool, typer.Option("--fake", help="Use offline FakeLLM (NOT a real translation)")] = False,
    reasoning_effort: Annotated[str | None, typer.Option("--reasoning-effort",
                                                         help="low | medium | high (model-dependent)")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Translate a book (imports it first if needed)."""
    from .translation.orchestrator import RunOptions

    cfg = state.cfg
    if reasoning_effort:
        cfg.openrouter.reasoning_effort = reasoning_effort
    book = _book(epub, auto_import=True)
    opts = RunOptions(mode=mode or cfg.app.mode, model=model or cfg.openrouter.model,
                      budget_usd=cfg.openrouter.per_book_budget_usd if budget_usd is None else budget_usd,
                      unit_ids=unit_id or None, force=force, limit=limit_units, client="fake" if fake else "openrouter")
    if opts.mode not in ("baseline", "context"):
        raise NCTEError("--mode must be baseline or context")
    if dry_run:
        from .translation.dryrun import dry_run as do_dry

        client = make_client(cfg, fake=fake, need_key=False)
        plan = do_dry(state.conn, cfg, book, opts, client, log=typer.echo)
        if as_json:
            echo_json(plan.to_dict())
            return
        typer.echo(f"DRY RUN — no paid requests were made\nbook {book.id} 「{book.title}」 mode={opts.mode}")
        for k, v in plan.to_dict().items():
            if k not in ("notes", "calibrated_cost_usd"):
                typer.echo(f"  {k}: {v}")
        if plan.calibrated_cost_usd is not None:
            typer.echo(f"  ➜ calibrated estimate from your previous real requests: ${plan.calibrated_cost_usd:.4f} "
                       f"(reasoning_effort={plan.reasoning_effort or 'model default'})")
        else:
            typer.echo("  (no calibration yet: run a small paid sample with the same model/reasoning effort first)")
        for n in plan.notes:
            typer.echo(f"  note: {n}")
        if plan.cost_high_usd > opts.budget_usd:
            typer.echo(f"  ⚠ estimated upper bound ${plan.cost_high_usd:.4f} exceeds budget ${opts.budget_usd:.2f}; "
                       "the run will stop at the budget and can be resumed")
        return
    _run(book, opts, fake=fake, as_json=as_json)


def _run(book: Book, opts: Any, *, fake: bool, as_json: bool = False, run_id: str | None = None,
         cfg: Config | None = None) -> None:
    from .translation.orchestrator import create_run

    cfg = cfg or state.cfg
    client = make_client(cfg, fake=fake)
    if fake:
        typer.echo("⚠ using FakeLLM: output is placeholder text, not a translation")
    rid = run_id or create_run(state.conn, cfg, book, opts)
    typer.echo(f"run {rid}  book {book.id}  mode={opts.mode}  model={opts.model}  budget=${opts.budget_usd}")
    orch = _orchestrator(cfg, client, book, rid, opts)
    summary = orch.run()
    data = summary.__dict__
    if as_json:
        echo_json(data)
    else:
        typer.echo(f"\nrun {summary.run_id}: {summary.status}")
        typer.echo(f"  jobs: {summary.counts}")
        typer.echo(f"  requests: {summary.requests}, spent ≈ ${summary.spent_usd:.6f}"
                   + (f" (incl. {summary.unknown_cost_requests} requests with unknown billing)"
                      if summary.unknown_cost_requests else ""))
        for n in summary.notes:
            typer.echo(f"  note: {n}")
        if summary.status not in ("completed",):
            typer.echo(f"  continue with: ncte resume --run-id {summary.run_id}")
    if summary.status == "interrupted":
        raise typer.Exit(130)
    if summary.status == "stopped_fatal":
        raise typer.Exit(2)


def _orchestrator(cfg: Config, client: Any, book: Book, rid: str, opts: Any) -> Any:
    if opts.mode == "context":
        from .translation.context_pipeline import ContextOrchestrator

        return ContextOrchestrator(state.conn, cfg, client, book, rid, opts, log=typer.echo)
    from .translation.orchestrator import Orchestrator

    return Orchestrator(state.conn, cfg, client, book, rid, opts, log=typer.echo)


@app.command()
def resume(run_id: Annotated[str, typer.Option("--run-id")],
           budget_usd: Annotated[float | None, typer.Option("--budget-usd", help="Raise/lower the budget")] = None,
           as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Continue an interrupted / budget-stopped run from its checkpoint."""
    from .memory.db import Transaction
    from .translation.orchestrator import load_run

    state._conn = state.ws.find_run(run_id)
    row, cfg, opts = load_run(state.conn, run_id)
    cfg.app.workspace = str(state.cfg.workspace)
    if budget_usd is not None:
        opts.budget_usd = budget_usd
        data = json.loads(row["config_json"])
        data["_run_options"]["budget_usd"] = budget_usd
        with Transaction(state.conn):
            state.conn.execute("UPDATE runs SET budget_usd=?, config_json=? WHERE id=?",
                               (budget_usd, json.dumps(data, ensure_ascii=False), run_id))
    book = resolve_book(state.conn, row["book_id"])
    _run(book, opts, fake=opts.client == "fake", as_json=as_json, run_id=run_id, cfg=cfg)


@app.command()
def status(epub: Annotated[str, typer.Argument(help="EPUB path or book id")],
           as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show translation progress, runs and costs for a book."""
    from .reports.qa_report import book_status

    book = _book(epub)
    st = book_status(state.conn, book.id)
    if as_json:
        echo_json({"book_id": book.id, "title": book.title, **st})
        return
    typer.echo(f"{book.id} 「{book.title}」")
    typer.echo(f"  translatable {st['translatable']}, translated {st['translated']}, remaining {st['remaining']}")
    typer.echo(f"  status: {st['translation_status']}  scenes: {st['scenes']}")
    for r in st["runs"]:
        typer.echo(f"  run {r['id']} {r['mode']} {r['model']} {r['status']} pending={r['pending']} "
                   f"failed={r['failed']} cost≈${r['cost_usd']:.6f}")


@app.command("export")
def export_cmd(epub: Annotated[str, typer.Argument(help="EPUB path or book id")],
               lang: Annotated[str, typer.Option("--lang")] = "zh-TW",
               output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
               strict: Annotated[bool, typer.Option("--strict", help="Fail if any unit is untranslated")] = False,
               no_epubcheck: Annotated[bool, typer.Option("--no-epubcheck")] = False) -> None:
    """Rebuild the EPUB with validated translations (untranslated units keep the original text)."""
    from .exporter import export_book

    book = _book(epub)
    out = export_book(state.conn, state.cfg, book, output=output, lang=lang, strict=strict,
                      run_epubcheck=not no_epubcheck)
    v = out.validation
    typer.echo(f"exported: {out.result.output_path}")
    typer.echo(f"  {out.stats}")
    typer.echo(f"  internal validation: {'OK' if not v.errors else 'ERRORS'}; EPUBCheck: {v.epubcheck.get('status')}"
               + (f" {v.epubcheck.get('counts')}" if v.epubcheck.get("counts") else ""))
    for e in v.errors[:20]:
        typer.echo(f"  ✗ {e}")
    for w in v.warnings[:20]:
        typer.echo(f"  ! {w}")
    typer.echo(f"  report: {out.report_path}")
    if not v.ok:
        raise typer.Exit(1)


@app.command()
def validate(epub: Annotated[Path, typer.Argument()],
             original: Annotated[Path | None, typer.Option("--original", help="Compare with source EPUB")] = None,
             no_epubcheck: Annotated[bool, typer.Option("--no-epubcheck")] = False,
             as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Validate an EPUB (internal checks + EPUBCheck if installed)."""
    from .epub.validate import validate_epub

    rep = validate_epub(epub, original=original, epubcheck_path=state.cfg.export.epubcheck_path,
                        run_epubcheck=not no_epubcheck, workspace=state.cfg.workspace)
    if as_json:
        echo_json(rep.to_dict())
    else:
        typer.echo(f"{epub}: {'OK' if rep.ok else 'FAILED'}")
        typer.echo(f"  stats: {rep.stats}")
        typer.echo(f"  EPUBCheck: {rep.epubcheck.get('status')} {rep.epubcheck.get('counts', '')}")
        for e in rep.errors:
            typer.echo(f"  ✗ {e}")
        for w in rep.warnings:
            typer.echo(f"  ! {w}")
        if rep.epubcheck.get("status") == "failed":
            typer.echo(rep.epubcheck.get("output", "")[-2000:])
    if not rep.ok:
        raise typer.Exit(1)


@app.command()
def report(run_id: Annotated[str | None, typer.Option("--run-id")] = None,
           book: Annotated[str | None, typer.Option("--book", help="EPUB path or book id (all runs)")] = None,
           fmt: Annotated[str, typer.Option("--format", help="html | json")] = "html",
           reconcile: Annotated[bool, typer.Option("--reconcile", help="Fetch final costs from /generation")] = False,
           output: Annotated[Path | None, typer.Option("--output", "-o")] = None) -> None:
    """Cost / QA report (JSON or self-contained local HTML)."""
    from .reports.cost_report import cost_report
    from .reports.html_report import render_html
    from .reports.qa_report import book_status, qa_report

    if not run_id and not book:
        raise NCTEError("give --run-id or --book")
    if run_id:
        state._conn = state.ws.find_run(run_id)
        row = state.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise NCTEError(f"unknown run {run_id}")
        bk = resolve_book(state.conn, row["book_id"])
    else:
        bk = _book(book)  # type: ignore[arg-type]
    if reconcile:
        from .translation.orchestrator import reconcile_costs

        n = reconcile_costs(state.conn, make_client(state.cfg), run_id=run_id, book_id=None if run_id else bk.id)
        typer.echo(f"reconciled {n} request costs via /generation")
    cost = cost_report(state.conn, run_id=run_id, book_id=None if run_id else bk.id)
    qa = qa_report(state.conn, book_id=bk.id, run_id=run_id)
    st = book_status(state.conn, bk.id)
    data = {"book_id": bk.id, "title": bk.title, "cost": cost, "qa": qa, "status": st}
    rdir = state.cfg.workspace / "reports" / bk.id
    rdir.mkdir(parents=True, exist_ok=True)
    name = f"run-{run_id}" if run_id else "book"
    if fmt == "json":
        path = output or rdir / f"{name}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    elif fmt == "html":
        path = output or rdir / f"{name}.html"
        path.write_text(render_html(f"NCTE report — {bk.title}", cost, qa, st), encoding="utf-8")
    else:
        raise NCTEError("--format must be html or json")
    t = cost["totals"]
    typer.echo(f"report: {path}")
    typer.echo(f"  requests {t['requests']}, cost upper bound ${t['total_cost_upper_usd']:.6f} "
               f"(reported ${t['cost_reported_usd']:.6f}, unknown-billing est ${t['cost_unknown_estimate_usd']:.6f})")
    typer.echo(f"  QA findings {qa['findings_total']}, needs review {len(qa['needs_review'])}, "
               f"untranslated {len(qa['untranslated'])}")


@app.command()
def retranslate(epub: Annotated[str, typer.Argument(help="EPUB path or book id")],
                unit_id: Annotated[list[str], typer.Option("--unit-id", help="Unit(s) to retranslate")],
                mode: Annotated[str, typer.Option()] = "baseline",
                model: Annotated[str | None, typer.Option()] = None,
                budget_usd: Annotated[float, typer.Option("--budget-usd")] = 0.5,
                fake: Annotated[bool, typer.Option("--fake")] = False) -> None:
    """Retranslate only the given units; other translations (and human edits) are untouched."""
    from .translation.orchestrator import RunOptions

    book = _book(epub)
    opts = RunOptions(mode=mode, model=model or state.cfg.openrouter.model, budget_usd=budget_usd,
                      unit_ids=unit_id, force=True, client="fake" if fake else "openrouter", include_meta=True)
    _run(book, opts, fake=fake)


def main() -> None:
    try:
        app()
    except NCTEError as exc:
        typer.echo(f"error: {redact(str(exc), get_api_key())}", err=True)
        sys.exit(1)


from . import cli_memory  # noqa: E402,F401  (registers memory / QA / eval commands on `app`)

if __name__ == "__main__":
    main()
