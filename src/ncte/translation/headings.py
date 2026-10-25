"""A table-of-contents entry follows its chapter heading (ADR-106).

The book's own contents page (book matter, ADR-024) is translated without the story around it and never proofread,
and the NAV / NCX labels are translated as a list of titles; the chapter's heading in the story is translated in its
scene and proofread with it. An entry whose source is the same text as a story heading therefore takes the heading's
final text (human edit, proofread fix or translation, whichever is current): the program judges nothing here, it copies
a text the model's translation and proofreading already settled. "The same text": equal after readings, placeholders
and whitespace are taken off (``key``), and only when every story heading with that text reads the same in Chinese —
two headings 「幕間」 translated differently say nothing about which one an entry means, so it keeps its own.

Followed again whenever a run ends its translation (``run_meta``) and before every export, so a heading fixed, edited
or re-translated later is followed too. A person's edit of the entry itself is kept.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..epub.units import match_text
from ..memory.db import utcnow
from .cache import FOLLOWS_HEADING, VALID_MACHINE, latest_translation
from .context_builder import META_KINDS

_WHOLE = re.compile(r"⟦(\d+)⟧(.*)⟦/\1⟧", re.S)  # one placeholder around a whole unit
TITLE_CHARS = 40  # a story paragraph this short that is exactly a TOC label is a title set as a styled paragraph


def key(plain_text: str) -> str:
    """What two titles are compared by: the base text without readings or placeholders, all whitespace removed
    (「第一章　出会い」 and 「第一章 出会い」 are one title)."""
    return "".join(match_text(plain_text).split())


def _inner(text: str) -> tuple[str | None, str]:
    """(the number of a placeholder around the whole text, or None; what is inside it)."""
    m = _WHOLE.fullmatch(text)
    return (m.group(1), m.group(2)) if m else (None, text)


def heading_texts(conn: sqlite3.Connection, book_id: str, story: set[str]) -> dict[str, tuple[str, sqlite3.Row]]:
    """key → (the Chinese of the story headings with that source, the newest of their translations), only for a key
    every such heading has a translation for and all read the same, without inline markup left inside. A heading
    that is not translated at all (an image in it) counts as one without a translation."""
    rows = conn.execute(
        "SELECT * FROM units u WHERE book_id=? AND spine_index IS NOT NULL AND (kind='heading' OR "
        f"(kind='text' AND length(plain_text)<={TITLE_CHARS} AND plain_text IN (SELECT plain_text FROM units v WHERE "
        "v.book_id=u.book_id AND v.kind IN ('nav','ncx')))) ORDER BY ordinal", (book_id,)).fetchall()
    seen: dict[str, set[str | None]] = {}
    row_of: dict[str, sqlite3.Row] = {}
    for r in rows:
        if r["resource_href"] not in story or not (k := key(r["plain_text"])):
            continue
        tr = latest_translation(conn, r["id"], r["source_hash"]) if r["translatable"] else None
        zh: str | None = None
        if tr is not None and tr["text"]:
            (n_src, src), (n_zh, text) = _inner(r["source_text"]), _inner(tr["text"])
            if n_src == n_zh and "⟦" not in src and "⟦" not in text and text.strip():
                zh = text
                if k not in row_of or tr["id"] > row_of[k]["id"]:
                    row_of[k] = tr  # the newest of them: what a copy is made after
        seen.setdefault(k, set()).add(zh)  # None: a heading not translated yet, or with markup inside
    return {k: (zh, row_of[k]) for k, v in seen.items() if len(v) == 1 and (zh := next(iter(v))) is not None}


def follow_headings(conn: sqlite3.Connection, book_id: str, *, meta: bool = True,
                    run_id: str | None = None) -> dict[str, Any]:
    """Every TOC entry (book matter's text, and with ``meta`` the NAV / NCX labels and titles) whose source is a
    story heading's takes that heading's current Chinese; the copy keeps the heading translation's prompt and model,
    ``run_id`` is the run that copies it (none from an export). An entry whose heading is no longer a clear match (two
    headings with its source now read differently) loses its copies (``unfollowed``): it falls back to its own
    translation, and a title is translated for itself again. Returns ``followed`` (unit ids now holding it, written
    or already so), ``written`` (how many were changed) and ``dropped`` (unit ids whose copies were taken off). The
    caller holds the transaction."""
    from ..epub.matter import story_hrefs

    story = story_hrefs(conn, book_id)
    leaders = heading_texts(conn, book_id, story)
    kinds = ("text", "heading", *(META_KINDS if meta else ()))
    rows = conn.execute(f"SELECT * FROM units WHERE book_id=? AND translatable=1 AND kind IN ({','.join('?' * len(kinds))}) "
                        "ORDER BY ordinal", (book_id, *kinds)).fetchall()
    followed: set[str] = set()
    written = 0
    dropped: set[str] = set()
    for r in rows:
        if r["kind"] not in META_KINDS and (r["spine_index"] is None or r["resource_href"] in story):
            continue  # the story's own paragraphs and headings are translated in their scene
        got = leaders.get(key(r["plain_text"]))
        n, src = _inner(r["source_text"])
        if got is None or "⟦" in src:  # inline markup inside the entry: there is no place to put the heading's text
            # a copy of a heading it no longer clearly follows is not its text any more (nor counted as translated)
            if conn.execute(
                    f"UPDATE translations SET status='unfollowed' WHERE unit_id=? AND context_hash LIKE ? AND status IN "
                    f"({','.join('?' * len(VALID_MACHINE))})", (r["id"], f"{FOLLOWS_HEADING}%", *VALID_MACHINE)
            ).rowcount:
                dropped.add(r["id"])
            continue
        zh, lead = got
        text = f"⟦{n}⟧{zh}⟦/{n}⟧" if n is not None else zh
        cur = latest_translation(conn, r["id"], r["source_hash"])
        if cur is not None and cur["status"] == "human":
            continue  # a person's own wording of the entry
        followed.add(r["id"])
        mark = f"{FOLLOWS_HEADING}{lead['unit_id']}"
        if cur is not None and cur["text"] == text and cur["context_hash"] == mark and cur["id"] > lead["id"]:
            continue  # already a copy of the heading as it is now (a heading translated again is copied again)
        conn.execute(
            "INSERT INTO translations(unit_id, target_lang, text, source_hash, context_hash, prompt_version, model_id, "
            "glossary_version, mode, status, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], lead["target_lang"], text, r["source_hash"], mark,
             lead["prompt_version"], lead["model_id"], lead["glossary_version"], lead["mode"], "succeeded",
             run_id, utcnow()))
        written += 1
    return {"followed": followed, "written": written, "dropped": dropped}
