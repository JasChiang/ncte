"""Scenes inside a chapter, cut by the model reading the chapter in windows.

A chapter goes to the model a window at a time (each paragraph's opening, and the ending of a long one). The model
marks where a new scene starts and what kind of scene it is. The breaks it sees are kept; the last scene of a window
may not have ended, so the next window starts again at that scene's first paragraph: every window overlaps the one
before by its unfinished scene. The author's marks (a separator, blank lines, a new file, a heading) go with the
paragraphs as candidates, not as cuts — blank lines in the middle of one exchange were cut before, and never joined
again. A chapter the model has cut is marked decided; until then the program's provisional cut stands.

A book without a table of contents is one chapter: the model also says which breaks start a new chapter.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from ..llm.schemas import SceneWindowsOutput, WindowBreak
from ..memory.db import Transaction, utcnow
from .dialogue import UnitText, clean, find_quotes

TEXT_CHARS = 120  # a paragraph's opening
END_CHARS = 20  # and the ending of a longer one: whether it runs on into the next (「…眺めていた時――」)
WINDOW_CHARS = 9000  # what one window shows, about
WINDOW_PARAGRAPHS = 220
MIN_SCENE = 3  # paragraphs: a break closer than this to the one before is dropped (not after a separator)


def marks(r: sqlite3.Row, prev: sqlite3.Row | None) -> list[str]:
    out = []
    if r["is_separator"]:
        out.append("separator")
    if r["blank_before"] >= 2:
        out.append("blank")
    if prev is not None and r["resource_href"] != prev["resource_href"]:
        out.append("new_file")
    if r["kind"] == "heading":
        out.append("heading")
    return out


def window_end(rows: list[sqlite3.Row], start: int) -> int:
    used = 0
    for i in range(start, len(rows)):
        used += min(len(rows[i]["plain_text"]), TEXT_CHARS + END_CHARS)
        if i > start and (used > WINDOW_CHARS or i - start >= WINDOW_PARAGRAPHS):
            return i
    return len(rows)


def payload(rows: list[sqlite3.Row], start: int, end: int, *, title: str, known: bool, starts_scene: bool,
            max_units: int) -> dict[str, Any]:
    paras = []
    for i in range(start, end):
        r = rows[i]
        t = clean(r["plain_text"]).strip(" 　")
        p: dict[str, Any] = {"unit_id": r["id"], "text": t[:TEXT_CHARS], "chars": len(t),
                             "line": t.startswith(("「", "『"))}
        if len(t) > TEXT_CHARS:
            p["end"] = t[-END_CHARS:]
        if m := marks(r, rows[i - 1] if i else None):
            p["marks"] = m
        paras.append(p)
    return {"task": "scene_windows", "chapter": {"title": title, "known": known}, "starts_scene": starts_scene,
            "reaches_chapter_end": end >= len(rows), "scene_size": [MIN_SCENE, max_units], "paragraphs": paras}


def accepted(rows: list[sqlite3.Row], start: int, end: int, breaks: list[WindowBreak]) -> list[tuple[int, WindowBreak]]:
    """The breaks the program keeps, by position: inside the window and not its first paragraph, not inside a quote
    that runs over paragraphs, and not leaving a scene of one or two paragraphs (unless the author put a separator
    there). A break on the paragraph right after a separator moves onto the separator (the separator opens the scene).
    A sentence that runs on (「…時――」) is the model's to weigh: it sees each paragraph's ending, and 「――」 that ends
    one beat and opens the next is a real change of scene."""
    pos = {rows[i]["id"]: i for i in range(start + 1, end)}
    quotes = find_quotes([UnitText(r["id"], clean(r["plain_text"]), r["ordinal"]) for r in rows[start:end]])
    inside = {q.unit_id for q in quotes if q.continuation_of is not None}
    out: list[tuple[int, WindowBreak]] = []
    for b in sorted((b for b in breaks if b.unit_id in pos), key=lambda b: pos[b.unit_id]):
        i = pos[b.unit_id]
        if i - 1 > start and rows[i - 1]["is_separator"]:
            i -= 1
        sep = bool(rows[i]["is_separator"])
        if b.unit_id in inside or (out and out[-1][0] == i):
            continue
        if not sep and i - (out[-1][0] if out else start) < MIN_SCENE:
            continue
        out.append((i, b))
    return out


def cut_chapter(rows: list[sqlite3.Row], ask: Callable[[dict[str, Any]], SceneWindowsOutput | None], *,
                title: str, known: bool, max_units: int) -> tuple[list[tuple[int, WindowBreak]], str]:
    """(breaks by position, the kind of the chapter's first scene). A window the model could not answer (refused,
    failed) keeps the author's separators and blank lines as breaks, marked unsure, and the chapter goes on."""
    out: list[tuple[int, WindowBreak]] = []
    first_kind = "present"
    start, starts_scene = 0, True
    while start < len(rows):
        end = window_end(rows, start)
        res = ask(payload(rows, start, end, title=title, known=known, starts_scene=starts_scene, max_units=max_units))
        if res is None:
            got = [(i, WindowBreak(unit_id=rows[i]["id"], sure=False, reason="作者的標記（模型未回答）"))
                   for i in range(start + 1, end) if {"separator", "blank"} & set(marks(rows[i], rows[i - 1]))]
        else:
            got = accepted(rows, start, end, res.breaks)
            if start == 0:
                first_kind = res.first_kind
        out.extend(got)
        if end >= len(rows):
            break
        if got and got[-1][0] > start:
            start, starts_scene = got[-1][0], True  # the last scene's end is not known yet: shown again from its start
        else:
            start, starts_scene = max(start + 1, end - max(1, (end - start) // 4)), False  # no break: go on, overlapping
    return out, first_kind


def save(conn: sqlite3.Connection, book_id: str, rows: list[sqlite3.Row], breaks: list[tuple[int, WindowBreak]],
         first_kind: str) -> list[str]:
    """The chapter's first paragraph marks it decided; each break starts a scene. Returns the first paragraphs of the
    chapters the model found (a book without a TOC)."""
    with Transaction(conn):
        conn.executemany("DELETE FROM scene_breaks WHERE book_id=? AND unit_id=?", [(book_id, r["id"]) for r in rows])
        conn.execute("INSERT INTO scene_breaks(book_id, unit_id, reason, origin, created_at, kind, sure) "
                     "VALUES (?,?,?,?,?,?,?)", (book_id, rows[0]["id"], "chapter cut by the model", "chapter", utcnow(),
                                                first_kind, 1))
        conn.executemany("INSERT INTO scene_breaks(book_id, unit_id, reason, origin, created_at, kind, sure) "
                         "VALUES (?,?,?,?,?,?,?)",
                         [(book_id, b.unit_id, b.reason[:200], "llm", utcnow(), b.kind, int(b.sure)) for _, b in breaks])
    return [b.unit_id for _, b in breaks if b.new_chapter]


def chapter_rows(conn: sqlite3.Connection, book_id: str, ch: sqlite3.Row) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM units WHERE book_id=? AND kind IN ('text','heading') AND spine_index IS NOT NULL "
                        "AND ordinal BETWEEN ? AND ? ORDER BY ordinal", (book_id, ch["start_ordinal"], ch["end_ordinal"])
                        ).fetchall()
