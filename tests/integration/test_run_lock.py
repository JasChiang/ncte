"""A run is worked on by one program at a time (fal real run: the CLI and the page both worked one run)."""

import subprocess
import sys
import textwrap

import pytest

from ncte.llm.fake import FakeLLM
from ncte.translation.run_lock import RunLocked, SeriesLock, holder

from ..helpers import orchestrator_for, start_run


def _hold_in_other_process(db_path, ready, release):
    code = textwrap.dedent(f"""
        import sqlite3, time, pathlib
        from ncte.translation.run_lock import SeriesLock
        lock = SeriesLock(sqlite3.connect({str(db_path)!r}))
        lock.acquire("r_other", "cli")
        pathlib.Path({str(ready)!r}).write_text("ok")
        while not pathlib.Path({str(release)!r}).exists():
            time.sleep(0.05)
    """)
    return subprocess.Popen([sys.executable, "-c", code])


def test_second_program_is_refused_before_any_change(conn, cfg, fixtures, tmp_path):
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], FakeLLM(), mode="context")
    db = cfg.workspace / "ncte.db"
    ready, release = tmp_path / "ready", tmp_path / "release"
    proc = _hold_in_other_process(db, ready, release)
    try:
        while not ready.exists():
            pass
        assert holder(conn)["run_id"] == "r_other"
        with pytest.raises(RunLocked, match="指令列"):
            orchestrator_for(conn, cfg, book, rid, opts, FakeLLM()).run()
        # nothing was touched: no jobs queued, the run not marked as running, no request sent
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?", (rid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0] == 0
    finally:
        release.write_text("go")
        proc.wait(timeout=10)
    # the other program ended: the lock is free again and the run goes through
    assert holder(conn) is None
    s = orchestrator_for(conn, cfg, book, rid, opts, FakeLLM()).run()
    assert s.status == "completed" and holder(conn) is None


def test_lock_is_freed_when_the_holder_dies(conn, cfg, tmp_path):
    ready, release = tmp_path / "ready", tmp_path / "release"
    proc = _hold_in_other_process(cfg.workspace / "ncte.db", ready, release)
    while not ready.exists():
        pass
    proc.kill()
    proc.wait(timeout=10)
    assert holder(conn) is None  # a crashed run is resumable, never mistaken for one still working
    lock = SeriesLock(conn)
    lock.acquire("r_mine", "gui")
    assert holder(conn)["source"] == "gui"
    lock.release()


def test_in_memory_database_needs_no_lock():
    import sqlite3

    mem = sqlite3.connect(":memory:")
    lock = SeriesLock(mem)
    lock.acquire("r", "cli")
    assert holder(mem) is None
    lock.release()


def test_run_history_records_who_started_and_changed_the_budget(tmp_path, fixtures, monkeypatch):
    from typer.testing import CliRunner

    from ncte.cli import app

    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    ws = ["-w", str(tmp_path / "ws")]
    out = r.invoke(app, [*ws, "translate", str(fixtures["basic"]), "--fake", "--budget-usd", "0.0001"])
    rid = next(w for w in out.output.split() if w.startswith("r_"))
    r.invoke(app, [*ws, "resume", "--run-id", rid, "--budget-usd", "5"])
    import sqlite3
    from pathlib import Path

    db = next(Path(tmp_path / "ws").rglob("ncte.db"))
    ev = [tuple(x) for x in sqlite3.connect(db).execute("SELECT event, source FROM run_events WHERE run_id=? ORDER BY id", (rid,))]
    assert ev[0] == ("start", "cli") and ("budget", "cli") in ev and ev[-1] == ("resume", "cli")
