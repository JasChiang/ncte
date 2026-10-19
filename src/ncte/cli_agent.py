"""``ncte agent …``: an agent translates a book with itself as the model (it answers the engine's requests as
tasks), and ``ncte mcp``: the same as MCP tools. For a run on the Claude plan with no agent in the loop, use
``NCTE_LLM_PROVIDER=claude ncte translate …`` or the web UI instead."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from .cli import app

agent_app = typer.Typer(no_args_is_help=True,
                        help="Translate with an agent (Claude Code, Codex …) answering the engine's model requests "
                             "itself, one task at a time.")
app.add_typer(agent_app, name="agent")

WS = Annotated[str, typer.Option("--workspace", "-w", help="Workspace directory for this book")]


def _echo(data: object) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


@agent_app.command("start")
def agent_start(epub: Annotated[str, typer.Argument(help="Source EPUB (DRM-free)")], workspace: WS,
                limit_units: Annotated[int | None, typer.Option("--limit-units",
                                                                help="Only the first N paragraphs")] = None) -> None:
    """Import the book and start the engine in the background. Then answer tasks with `next` / `submit`."""
    from .agent import runner

    pid = runner.start_detached(workspace, Path(epub).expanduser().resolve(), limit=limit_units)
    _echo({"started": True, "pid": pid, "next": f"ncte agent next -w {workspace}"})


@agent_app.command("resume")
def agent_resume(workspace: WS) -> None:
    """Continue the workspace's run after a stop (answered tasks are not asked again)."""
    from .agent import runner
    from .agent.tasks import TaskQueue

    rid = TaskQueue(workspace).state().get("run_id")
    if not rid:
        raise typer.BadParameter("no run in this workspace")
    _echo({"resumed": rid, "pid": runner.start_detached(workspace, run_id=rid)})


@agent_app.command("serve", hidden=True)
def agent_serve(workspace: WS, epub: Annotated[str | None, typer.Option("--epub")] = None,
                book: Annotated[str | None, typer.Option("--book")] = None,
                limit_units: Annotated[int | None, typer.Option("--limit-units")] = None,
                run_id: Annotated[str | None, typer.Option("--run-id")] = None) -> None:
    """The engine process (started by `start`)."""
    from .agent import runner

    out = runner.serve(workspace, epub, book_ref=book, limit=limit_units, run_id=run_id, log=typer.echo)
    typer.echo(f"run finished: {out.get('status')}")


@agent_app.command("next")
def agent_next(workspace: WS) -> None:
    """Take the next task. Writes it to a file and prints the file's path, the stage and how to submit."""
    from .agent import runner
    from .agent.tasks import TaskQueue

    q = TaskQueue(workspace)
    t = q.claim_next()
    if t is None:
        st = runner.status(workspace)
        _echo({"task": None, "status": st.get("status"), "run_status": st.get("run_status"),
               "hint": "the engine is working (wait a few seconds and ask again)" if st.get("engine_alive")
               else "the engine is not running: see `ncte agent status`"})
        return
    path = q.root / "work" / f"{t.id}.md"
    schema = json.dumps(t.schema, ensure_ascii=False) if t.schema else "(see the output format in the instructions)"
    path.write_text(
        f"# Task {t.id} — stage `{t.stage}`" + (f" (think at {t.effort} effort)" if t.effort else "") + "\n\n"
        "You are the model the NCTE translation engine is asking. Follow the instructions exactly and answer with "
        "ONE JSON object only (no prose, no code fence), in the output format the instructions give.\n\n"
        f"## Instructions (system prompt)\n\n{t.instructions}\n\n"
        f"## Input (the request's JSON)\n\n```json\n{t.input}\n```\n\n"
        f"## Output JSON schema\n\n```json\n{schema}\n```\n\n"
        f"## Submit\n\nWrite the JSON to a file, then run:\n\n"
        f"    ncte agent submit -w {workspace} {t.id} <answer.json>\n", encoding="utf-8")
    _echo({"task_id": t.id, "stage": t.stage, "effort": t.effort, "file": str(path), "input_chars": len(t.input)})


@agent_app.command("submit")
def agent_submit(task_id: Annotated[str, typer.Argument()],
                 answer: Annotated[Path, typer.Argument(help="JSON file")], workspace: WS) -> None:
    """Submit the answer to a task. Prints the problems to fix if the JSON does not fit the step's schema."""
    from .agent.tasks import TaskQueue

    problems = TaskQueue(workspace).submit(task_id, answer.read_text(encoding="utf-8"))
    _echo({"accepted": not problems, "problems": problems})
    if problems:
        raise typer.Exit(1)


@agent_app.command("status")
def agent_status(workspace: WS) -> None:
    """The run's state, how many tasks are open, and the engine's last log lines."""
    from .agent import runner

    _echo(runner.status(workspace))


@agent_app.command("export")
def agent_export(output: Annotated[Path, typer.Argument(help="Output EPUB path")], workspace: WS) -> None:
    """Rebuild the EPUB with the translations (untranslated paragraphs keep the original)."""
    from .agent import runner

    _echo(runner.export(workspace, output))


@agent_app.command("stop")
def agent_stop(workspace: WS) -> None:
    """Stop the engine at its next checkpoint (continue later with `resume`)."""
    from .agent import runner

    _echo({"stopped": runner.stop(workspace)})


@app.command("mcp")
def mcp() -> None:
    """Serve the `ncte agent` commands as MCP tools over stdio (needs the `agent` extra: pip install 'ncte[agent]').

    claude mcp add ncte -- ncte mcp      (Codex: codex mcp add ncte -- ncte mcp)"""
    from .agent.mcp_server import main

    main()
