"""The engine for an agent: import a book, create a run, and let the engine work in its own process while every
model request becomes a task (``tasks.py``) for the agent to answer."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from .client import MODEL_ID, AgentClient
from .tasks import TaskQueue


def engine_config(workspace: str | Path) -> Any:
    """NCTE's configuration for an agent run: the workspace and the agent as the only model. The engine's defaults,
    not an ncte.yaml or .env from wherever the agent happens to be; steps think at medium, the judging ones at high."""
    from ..config import Config

    cfg = Config()
    cfg.app.workspace = str(Path(workspace).expanduser().resolve())
    o = cfg.openrouter
    o.provider = "agent"
    o.model = MODEL_ID
    o.review_model = None
    o.fallback_models = []
    o.stage_models = {}
    o.temperature = None
    o.reasoning_effort = "medium"
    o.per_book_budget_usd = 1e9  # nothing is billed through the engine
    return cfg


def serve(workspace: str | Path, epub: str | Path | None = None, *, book_ref: str | None = None,
          limit: int | None = None, run_id: str | None = None, stop: threading.Event | None = None,
          log: Any = None) -> dict[str, Any]:
    """Blocking: run (or resume) a translation with the agent as the model. Returns the run's summary."""
    from ..library import resolve_book
    from ..translation.context_pipeline import ContextOrchestrator
    from ..translation.orchestrator import RunOptions, create_run, load_run
    from ..workspace import Workspace

    queue = TaskQueue(workspace)
    cfg = engine_config(workspace)
    ws = Workspace(cfg)
    say = log or (lambda m: None)
    try:
        if run_id:
            conn = ws.find_run(run_id)
            row, cfg, opts = load_run(conn, run_id)
            cfg.app.workspace = str(Path(workspace).expanduser().resolve())
            book = resolve_book(conn, row["book_id"])
        else:
            if book_ref:
                conn, book = ws.open_book(book_ref)
            elif ws.find_book(str(epub)) is not None:
                conn, book = ws.open_book(str(epub))
            else:
                conn, book, _ = ws.import_book(str(epub))
            opts = RunOptions(mode="context", model=cfg.openrouter.model,
                              budget_usd=cfg.openrouter.per_book_budget_usd, limit=limit)
            run_id = create_run(conn, cfg, book, opts)
        queue.set_state(run_id=run_id, book_id=book.id, title=book.title, status="running", pid=os.getpid(),
                        workspace=str(Path(workspace).expanduser().resolve()), limit=limit, error=None)
        client = AgentClient(queue, model_id=cfg.openrouter.model, stop=(stop.is_set if stop else None))
        orch = ContextOrchestrator(conn, cfg, client, book, run_id, opts, log=say)
        summary = orch.run()
        queue.set_state(status=summary.status, pid=None)
        return dict(summary.__dict__)
    except BaseException as exc:
        queue.set_state(status="error", error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-4000:],
                        pid=None)
        raise
    finally:
        ws.close()


def start_detached(workspace: str | Path, epub: str | Path | None = None, *, book_ref: str | None = None,
                   limit: int | None = None, run_id: str | None = None) -> int:
    """Start ``serve`` in its own process (it keeps running between the agent's tool calls). Returns its pid."""
    if epub is not None and not Path(epub).expanduser().is_file():
        raise FileNotFoundError(f"no such EPUB: {epub}")
    root = TaskQueue(workspace).root
    # not `-m ncte.cli`: run as __main__ the CLI module would be a second copy, without the commands registered on it
    args = [sys.executable, "-c", "from ncte.cli import main; main()", "agent", "serve",
            "--workspace", str(Path(workspace).resolve())]
    if epub:
        args += ["--epub", str(Path(epub).expanduser().resolve())]
    if book_ref:
        args += ["--book", book_ref]
    if limit:
        args += ["--limit-units", str(limit)]
    if run_id:
        args += ["--run-id", run_id]
    out = open(root / "engine.log", "a", encoding="utf-8")  # noqa: SIM115 — the child keeps it
    proc = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True, cwd=str(root))
    TaskQueue(workspace).set_state(status="starting", pid=proc.pid, error=None)
    return proc.pid


def status(workspace: str | Path) -> dict[str, Any]:
    """Where the run is: its state, task counts, and the engine's own progress."""
    queue = TaskQueue(workspace)
    st = queue.state()
    out: dict[str, Any] = {**{k: v for k, v in st.items() if k != "trace"}, **queue.counts()}
    pid = st.get("pid")
    if pid:
        try:
            os.kill(int(pid), 0)
            out["engine_alive"] = True
        except OSError:
            out["engine_alive"] = False
            if st.get("status") in ("starting", "running"):  # it died without saying so (e.g. at start-up)
                out["status"] = "error"
                out["error"] = st.get("error") or "the engine process exited; see log_tail"
    if st.get("run_id") and st.get("book_id"):
        from ..workspace import Workspace

        ws = Workspace(engine_config(workspace))
        try:
            conn = ws.find_run(st["run_id"])
            out["run_status"] = conn.execute("SELECT status FROM runs WHERE id=?", (st["run_id"],)).fetchone()[0]
            out["stages_done"] = {r[0]: r[1] for r in conn.execute(
                "SELECT stage, count(*) FROM llm_requests WHERE run_id=? AND status='succeeded' GROUP BY stage",
                (st["run_id"],))}
            out["translated"] = conn.execute(
                "SELECT count(DISTINCT unit_id) FROM translations WHERE run_id=? AND "
                "status IN ('succeeded','needs_review')", (st["run_id"],)).fetchone()[0]
        finally:
            ws.close()
    log = queue.root / "engine.log"
    if log.exists():
        out["log_tail"] = log.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
    return out


def export(workspace: str | Path, output: str | Path, *, book_ref: str | None = None) -> dict[str, Any]:
    from ..exporter import export_book
    from ..workspace import Workspace

    queue = TaskQueue(workspace)
    ref = book_ref or queue.state().get("book_id")
    if not ref:
        raise ValueError("no book in this workspace yet")
    cfg = engine_config(workspace)
    ws = Workspace(cfg)
    try:
        conn, book = ws.open_book(ref)
        out = export_book(conn, cfg, book, output=Path(output).expanduser(), lang="zh-TW", strict=False,
                          run_epubcheck=True)
        return {"output": str(out.result.output_path), "stats": out.stats, "ok": out.validation.ok,
                "errors": out.validation.errors[:20], "warnings": out.validation.warnings[:20]}
    finally:
        ws.close()


def stop(workspace: str | Path) -> bool:
    import signal

    pid = TaskQueue(workspace).state().get("pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), signal.SIGINT)  # the engine stops at a checkpoint; the run can be resumed
        return True
    except OSError:
        return False
