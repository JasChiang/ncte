"""MCP server: the `ncte agent` commands as tools any MCP-capable agent can call (needs the `agent` extra).

    claude mcp add ncte -- ncte mcp
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer
except ImportError as exc:  # pragma: no cover - depends on the optional extra
    raise SystemExit("the MCP server needs the `mcp` package: pip install 'ncte[agent]'") from exc

from . import runner
from .tasks import TaskQueue

server = MCPServer(
    name="ncte",
    instructions=(
        "Translate a Japanese EPUB into Taiwan Traditional Chinese with the NCTE engine, with you as its model. "
        "ncte_start once; then repeat: ncte_next_task → answer it exactly as its instructions say (one JSON object) "
        "→ ncte_submit; until ncte_next_task says the run is finished; then ncte_export. Each task is independent: "
        "judge only from its instructions and input (the engine keeps the book's memory for you); think at the "
        "task's `effort` when it gives one."),
)


@server.tool()
def ncte_start(epub_path: str, workspace: str, limit_units: int | None = None) -> dict[str, Any]:
    """Import the EPUB into the workspace and start the engine in the background."""
    pid = runner.start_detached(workspace, Path(epub_path).expanduser().resolve(), limit=limit_units)
    return {"started": True, "pid": pid}


@server.tool()
def ncte_next_task(workspace: str) -> dict[str, Any]:
    """Take the next task: stage, instructions (system prompt), input (JSON) and output schema. When there is none
    yet, says whether the engine is still working or finished."""
    t = TaskQueue(workspace).claim_next()
    if t is not None:
        return t.for_agent()
    st = runner.status(workspace)
    return {"task_id": None, "status": st.get("status"), "run_status": st.get("run_status"),
            "engine_alive": st.get("engine_alive", False)}


@server.tool()
def ncte_submit(workspace: str, task_id: str, output: str) -> dict[str, Any]:
    """Submit the answer (one JSON object as text). Returns the problems to fix when it does not fit the schema."""
    problems = TaskQueue(workspace).submit(task_id, output)
    return {"accepted": not problems, "problems": problems}


@server.tool()
def ncte_status(workspace: str) -> dict[str, Any]:
    """The run's state, open tasks and the engine's last log lines."""
    return runner.status(workspace)


@server.tool()
def ncte_export(workspace: str, output_path: str) -> dict[str, Any]:
    """Rebuild the translated EPUB."""
    return runner.export(workspace, output_path)


@server.tool()
def ncte_stop(workspace: str) -> dict[str, Any]:
    """Stop the engine at its next checkpoint (ncte_resume continues)."""
    return {"stopped": runner.stop(workspace)}


@server.tool()
def ncte_resume(workspace: str) -> dict[str, Any]:
    """Continue the workspace's run after a stop."""
    rid = TaskQueue(workspace).state().get("run_id")
    if not rid:
        return {"resumed": None, "error": "no run in this workspace"}
    return {"resumed": rid, "pid": runner.start_detached(workspace, run_id=rid)}


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
