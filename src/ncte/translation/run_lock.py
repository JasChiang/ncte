"""One run at a time per series database, across processes (the CLI and the web page).

Two programs working on the same run sent the same paid requests twice (both re-queued each other's running jobs),
and two runs of one series write the same memory. The lock is an OS file lock next to the series database
(``run.lock``): only the process holding it may start or resume a run there, and the system releases it when that
process ends, however it ends, so a crashed run is never mistaken for one still working. The file also says who holds
it, for the message shown to the other program.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..errors import NCTEError
from ..memory.db import utcnow

SOURCES = {"cli": "指令列", "gui": "網頁"}


class RunLocked(NCTEError):
    def __init__(self, owner: dict[str, Any]):
        self.owner = owner
        who = SOURCES.get(str(owner.get("source")), owner.get("source") or "另一個程序")
        super().__init__(f"另一個程序（{who}，pid {owner.get('pid', '?')}）正在處理這個系列的翻譯"
                         f"（run {owner.get('run_id', '?')}，{owner.get('since', '?')} 開始）；請等它結束或先在那邊暫停")


def lock_path(conn: sqlite3.Connection) -> Path | None:
    """``run.lock`` beside the connection's database file; None for an in-memory database."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2]).with_name("run.lock")
    return None


def _read(fd: int) -> dict[str, Any]:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 4096)
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class SeriesLock:
    def __init__(self, conn: sqlite3.Connection):
        self.path = lock_path(conn)
        self.fd: int | None = None

    def acquire(self, run_id: str, source: str) -> None:
        if self.path is None or self.fd is not None:
            return
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        for attempt in range(5):  # the page's holder() check takes the lock for an instant
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == 4 or _read(fd):  # a real holder writes who it is
                    owner = _read(fd)
                    os.close(fd)
                    raise RunLocked(owner) from None
                time.sleep(0.1)
        owner = {"pid": os.getpid(), "host": socket.gethostname(), "run_id": run_id, "source": source,
                 "since": utcnow()}
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(owner, ensure_ascii=False).encode("utf-8"))
        self.fd = fd

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            os.ftruncate(self.fd, 0)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


def holder(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Who is working on this series right now (any process, this one included), or None."""
    path = lock_path(conn)
    if path is None or not path.exists():
        return None
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read(fd) or {"source": None}
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)
