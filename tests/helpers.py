from __future__ import annotations

from typing import Any

from ncte.library import import_book
from ncte.translation.orchestrator import Orchestrator, RunOptions, create_run, load_run


def start_run(conn, cfg, epub, client, **kw: Any):
    book, _ = import_book(conn, cfg, epub)
    opts = RunOptions(model=kw.pop("model", cfg.openrouter.model), client="fake", **kw)
    rid = create_run(conn, cfg, book, opts)
    return book, rid, opts


def orchestrator_for(conn, cfg, book, rid, opts, client, logs=None):
    if opts.mode == "context":
        from ncte.translation.context_pipeline import ContextOrchestrator as cls
    else:
        cls = Orchestrator
    return cls(conn, cfg, client, book, rid, opts, log=(logs.append if logs is not None else (lambda m: None)))


def run_book(conn, cfg, epub, client, **kw: Any):
    book, rid, opts = start_run(conn, cfg, epub, client, **kw)
    summary = orchestrator_for(conn, cfg, book, rid, opts, client).run()
    return book, rid, summary


def resume_run(conn, cfg, rid, client, budget: float | None = None):
    from ncte.library import resolve_book

    row, rcfg, opts = load_run(conn, rid)
    rcfg.app.workspace = cfg.app.workspace
    if budget is not None:
        opts.budget_usd = budget
    book = resolve_book(conn, row["book_id"])
    return orchestrator_for(conn, rcfg, book, rid, opts, client).run()
