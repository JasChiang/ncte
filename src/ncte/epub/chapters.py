"""Chapters from the table of contents: each entry's target (a file, or an anchor inside one) is the paragraph a chapter
starts at, whatever the label says (「一章」, 「第一話」 or a title with no number). A file is not a chapter: a publisher may
put two chapters in one file or one chapter in several, and some books give a chapter title only as an image (the TOC
still names it). A book with no TOC has no chapters here; the model finds them while cutting scenes.
"""

from __future__ import annotations

import posixpath
import sqlite3
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urldefrag

from .parser import NS, EpubPackage, resolve_href
from .security import parse_xml
from .units import element_path


@dataclass(frozen=True)
class TocEntry:
    label: str
    href: str  # zip path of the file
    fragment: str  # anchor inside it ("" = the file's start)


def toc_entries(pkg: EpubPackage) -> list[TocEntry]:
    """The TOC in reading order: the NAV's toc list (EPUB 3), else the NCX's navPoints (EPUB 2)."""
    out: list[TocEntry] = []
    if pkg.nav_href and pkg.archive.has(pkg.nav_href):
        root = parse_xml(pkg.archive.get(pkg.nav_href), name=pkg.nav_href)
        navs = list(root.iter(f"{{{NS['xhtml']}}}nav"))
        toc = [n for n in navs if "toc" in (n.get(f"{{{NS['epub']}}}type") or "").split()] or navs[:1]
        for nav in toc:
            for a in nav.iter(f"{{{NS['xhtml']}}}a"):
                if href := a.get("href"):
                    path, frag = urldefrag(unquote(href))
                    out.append(TocEntry("".join(str(x) for x in a.itertext()).strip(), resolve_href(pkg.nav_href, path), frag))
    if not out and pkg.ncx_href and pkg.archive.has(pkg.ncx_href):
        root = parse_xml(pkg.archive.get(pkg.ncx_href), name=pkg.ncx_href)
        for np in root.iter(f"{{{NS['ncx']}}}navPoint"):
            label = np.find("ncx:navLabel/ncx:text", NS)
            content = np.find("ncx:content", NS)
            if content is not None and content.get("src"):
                path, frag = urldefrag(unquote(content.get("src") or ""))
                out.append(TocEntry((label.text or "").strip() if label is not None else "",
                                    posixpath.normpath(posixpath.join(posixpath.dirname(pkg.ncx_href), path)), frag))
    return out


def chapter_starts(conn: sqlite3.Connection, book_id: str, pkg: EpubPackage) -> list[tuple[int, str]]:
    """(first paragraph's ordinal, TOC label) per chapter, in reading order. An anchor is the element itself, an
    element around it, or the first paragraph after it; an entry whose file has no paragraphs is dropped."""
    at: dict[tuple[str, str], int] = {}
    first: dict[str, int] = {}
    for r in conn.execute("SELECT resource_href, xpath, ordinal FROM units WHERE book_id=? AND spine_index IS NOT NULL "
                          "AND kind IN ('text','heading')", (book_id,)):
        at[(r[0], r[1])] = r[2]
        first[r[0]] = min(first.get(r[0], r[2]), r[2])
    starts: dict[int, str] = {}
    for e in toc_entries(pkg):
        if e.href not in first:
            continue
        o = _anchor(pkg, e, at) if e.fragment else None
        o = first[e.href] if o is None else o
        starts.setdefault(o, e.label)
    return sorted(starts.items())


def _anchor(pkg: EpubPackage, e: TocEntry, at: dict[tuple[str, str], int]) -> int | None:
    if not pkg.archive.has(e.href):
        return None
    root = parse_xml(pkg.archive.get(e.href), name=e.href)
    hits = root.xpath("//*[@id=$f or @name=$f]", f=e.fragment)
    if not isinstance(hits, list) or not hits:
        return None
    el: Any = hits[0]
    for x in (el, *el.iterancestors()):
        if (o := at.get((e.href, element_path(x)))) is not None:
            return o
    seen = False
    for x in root.iter():
        seen = seen or x is el
        if seen and (o := at.get((e.href, element_path(x)))) is not None:
            return o
    return None


def save_chapters(conn: sqlite3.Connection, book_id: str, starts: list[tuple[int, str]], source: str) -> None:
    """Chapters from their starts (body paragraphs only: a file's <title> is not text); what comes before the first
    (a title page, a caution) is a chapter of its own."""
    ords = [r[0] for r in conn.execute("SELECT ordinal FROM units WHERE book_id=? AND spine_index IS NOT NULL "
                                       "AND kind IN ('text','heading') ORDER BY ordinal", (book_id,))]
    conn.execute("DELETE FROM chapters WHERE book_id=?", (book_id,))
    if not ords:
        return
    starts = [(o, label) for o, label in starts if o >= ords[0]]
    if not starts or starts[0][0] > ords[0]:
        starts = [(ords[0], ""), *starts]
    last = ords[-1]
    for seq, ((o, label), nxt) in enumerate(zip(starts, [*(s[0] for s in starts[1:]), last + 1], strict=True)):
        conn.execute("INSERT INTO chapters(book_id, seq, start_ordinal, end_ordinal, label, source) VALUES (?,?,?,?,?,?)",
                     (book_id, seq, o, nxt - 1, label, source))


def chapter_of(conn: sqlite3.Connection, book_id: str, ordinal: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM chapters WHERE book_id=? AND start_ordinal<=? AND end_ordinal>=?",  # type: ignore[no-any-return]
                        (book_id, ordinal, ordinal)).fetchone()
