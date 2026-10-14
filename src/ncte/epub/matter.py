"""Front matter / story / back matter of a book (ADR-024).

Cautions, tables of contents, title pages, afterwords, colophons and advertisements are translated like any other
text, but they are not part of the story: their names (authors, illustrators, publishers) are not characters and
their "scenes" have no plot. The classification uses the book's table of contents (NCX / NAV labels) and the file
names, in reading order: front matter until the first story file, back matter from the first afterword or colophon.
"""

from __future__ import annotations

import posixpath
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urldefrag

from .parser import NS, load_epub, resolve_href
from .security import parse_xml

FRONT = re.compile(r"表紙|cover|目次|もくじ|contents|caution|注意|toc|title-?page|扉|fmatter|front-?matter|口絵|kuchie|"
                   r"halftitle|credit", re.I)
BACK = re.compile(r"あとがき|後書き|後記|afterword|奥付|版權|版权|colophon|okuduke|copyright|付録|広告|advert|"
                  r"back-?matter|著者紹介|author", re.I)


def toc_labels(path: str | Path) -> dict[str, str]:
    """zip path of a content file → first TOC label pointing at it (NAV preferred, NCX otherwise)."""
    pkg = load_epub(path)
    out: dict[str, str] = {}
    if pkg.nav_href and pkg.archive.has(pkg.nav_href):
        root = parse_xml(pkg.archive.get(pkg.nav_href), name=pkg.nav_href)
        for a in root.iter(f"{{{NS['xhtml']}}}a"):
            href = a.get("href")
            if href:
                target = resolve_href(pkg.nav_href, urldefrag(unquote(href))[0])
                out.setdefault(target, "".join(str(x) for x in a.itertext()).strip())
    if not out and pkg.ncx_href and pkg.archive.has(pkg.ncx_href):
        root = parse_xml(pkg.archive.get(pkg.ncx_href), name=pkg.ncx_href)
        for np in root.iter(f"{{{NS['ncx']}}}navPoint"):
            label = np.find("ncx:navLabel/ncx:text", NS)
            content = np.find("ncx:content", NS)
            if content is not None and content.get("src"):
                target = posixpath.normpath(posixpath.join(posixpath.dirname(pkg.ncx_href),
                                                           urldefrag(unquote(content.get("src") or ""))[0]))
                out.setdefault(target, (label.text or "").strip() if label is not None else "")
    return out


def classify(hrefs: list[str], labels: dict[str, str], first_lines: dict[str, str] | None = None) -> dict[str, str]:
    """``hrefs`` in reading order → "front" | "story" | "back".

    The story starts at the first file whose TOC label is neither front nor back matter (everything before it —
    covers, illustrations, cautions, the table of contents — is front matter); without usable labels, at the first
    file whose name/first line does not look like front matter. Back matter starts at the first afterword /
    colophon after that (label, file name or first line) and runs to the end."""
    first_lines = first_lines or {}

    def text(h: str) -> str:
        return f"{labels.get(h, '')} {Path(h).stem} {first_lines.get(h, '')}"

    start = next((i for i, h in enumerate(hrefs) if labels.get(h) and not FRONT.search(labels[h])
                  and not BACK.search(labels[h])), None)
    if start is None:
        start = next((i for i, h in enumerate(hrefs) if not FRONT.search(text(h))), len(hrefs))
    out, kind = {}, "front"
    for i, h in enumerate(hrefs):
        if i == start:
            kind = "story"
        if kind == "story" and BACK.search(text(h)):
            kind = "back"
        out[h] = kind
    return out


@lru_cache(maxsize=32)
def _labels_cached(stored_path: str) -> dict[str, str]:
    try:
        return toc_labels(stored_path)
    except Exception:  # a book whose TOC cannot be read is classified by file names only
        return {}


def book_matter(conn: sqlite3.Connection, book_id: str) -> dict[str, str]:
    """resource href → front/story/back for one book (from its stored copy and its units)."""
    row = conn.execute("SELECT stored_path FROM books WHERE id=?", (book_id,)).fetchone()
    hrefs = [r[0] for r in conn.execute(
        "SELECT resource_href, MIN(ordinal) o FROM units WHERE book_id=? AND spine_index IS NOT NULL "
        "GROUP BY resource_href ORDER BY o", (book_id,))]
    labels = _labels_cached(row[0]) if row and row[0] and Path(row[0]).exists() else {}
    first: dict[str, str] = {}
    for h, t in conn.execute("SELECT resource_href, plain_text FROM units WHERE book_id=? AND spine_index IS NOT NULL "
                             "AND translatable=1 AND kind IN ('text','heading') ORDER BY ordinal", (book_id,)):
        if h not in first:
            first[h] = t[:20]
    return classify(hrefs, labels, first)


def story_hrefs(conn: sqlite3.Connection, book_id: str) -> set[str]:
    return {h for h, k in book_matter(conn, book_id).items() if k == "story"}
