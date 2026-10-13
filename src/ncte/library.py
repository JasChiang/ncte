"""Book import / lookup service: EPUB → resources + translation units in SQLite."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .epub.parser import EpubPackage, load_epub, sha256_file
from .epub.reassemble import extract_all_units
from .epub.units import match_text
from .errors import NCTEError
from .memory.db import Transaction, utcnow
from .series import ensure_series, series_id_for


@dataclass
class Book:
    id: str
    source_path: str
    stored_path: str
    sha256: str
    title: str
    language: str

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Book:
        return cls(r["id"], r["source_path"], r["stored_path"], r["sha256"], r["title"] or "", r["language"] or "")


def book_id_for(sha: str) -> str:
    return "b_" + sha[:16]


def unit_id_for(book_id: str, href: str, xpath: str) -> str:
    return "u_" + hashlib.sha256(f"{book_id}|{href}|{xpath}".encode()).hexdigest()[:16]


def import_book(conn: sqlite3.Connection, cfg: Config, epub_path: str | Path, *, series_id: str | None = None,
                series_title: str | None = None, volume: float = 1.0, volume_label: str | None = None,
                store_root: Path | None = None) -> tuple[Book, bool]:
    """Import (idempotent). Returns (book, created).

    ``series_id`` adds the book as volume ``volume`` of an existing series of this database; otherwise a new
    one-volume series is created (titled ``series_title`` or the book title)."""
    src = Path(epub_path).expanduser().resolve()
    sha = sha256_file(src)
    row = conn.execute("SELECT * FROM books WHERE sha256=?", (sha,)).fetchone()
    if row:
        return Book.from_row(row), False
    pkg = load_epub(src)  # all safety/structure checks happen here
    bid = book_id_for(sha)
    store_dir = (store_root or cfg.workspace / "books") / bid
    store_dir.mkdir(parents=True, exist_ok=True)
    stored = store_dir / "source.epub"
    if not stored.exists():
        shutil.copy2(src, stored)
    units = extract_all_units(pkg)
    meta = {"metadata": pkg.metadata, "version": pkg.version, "opf_path": pkg.opf_path,
            "nav_href": pkg.nav_href, "ncx_href": pkg.ncx_href, "spine": [s.href for s in pkg.spine],
            "warnings": pkg.warnings, "page_progression": pkg.page_progression}
    with Transaction(conn):
        conn.execute(
            "INSERT INTO books(id, source_path, stored_path, sha256, title, language, metadata_json, import_time) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (bid, str(src), str(stored), sha, pkg.title, pkg.language, json.dumps(meta, ensure_ascii=False), utcnow()))
        sid = series_id or ensure_series(conn, series_title or pkg.title or bid, series_id=series_id_for(bid))
        conn.execute("UPDATE books SET series_id=?, volume_index=?, volume_label=? WHERE id=?",
                     (sid, float(volume), volume_label, bid))
        res_ids: dict[str, str] = {}
        manifest_by_href = {m.href: m for m in pkg.manifest.values()}
        for e in pkg.archive.entries:
            rid = "r_" + hashlib.sha256(f"{bid}|{e.name}".encode()).hexdigest()[:16]
            m = manifest_by_href.get(e.name)
            conn.execute("INSERT INTO resources(id, book_id, href, media_type, manifest_id, sha256, size) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (rid, bid, e.name, m.media_type if m else None, m.id if m else None,
                          hashlib.sha256(e.data).hexdigest(), len(e.data)))
            res_ids[e.name] = rid
        for ordinal, (spine_index, u) in enumerate(units):
            conn.execute(
                "INSERT INTO units(id, book_id, resource_id, resource_href, xpath, ordinal, spine_index, kind, "
                "source_text, plain_text, source_hash, inline_map_json, translatable, skip_reason, blank_before, "
                "is_separator, ruby_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (unit_id_for(bid, u.resource_href, u.xpath), bid, res_ids[u.resource_href], u.resource_href,
                 u.xpath, ordinal, spine_index, u.kind, u.source_text, u.plain_text, u.source_hash,
                 json.dumps(u.inline_map_json(), ensure_ascii=False), int(u.translatable), u.skip_reason,
                 u.blank_before, int(u.is_separator), u.ruby_count))
            if spine_index is not None:
                conn.execute("INSERT INTO memory_fts(text, kind, ref_id, book_id, ordinal) VALUES (?,?,?,?,?)",
                             (match_text(u.plain_text), "unit", unit_id_for(bid, u.resource_href, u.xpath), bid,
                              ordinal))
    book = Book(bid, str(src), str(stored), sha, pkg.title, pkg.language)
    from .analysis.scene import segment_book  # local import: analysis depends on library

    segment_book(conn, cfg, bid)
    return book, True


def resolve_book(conn: sqlite3.Connection, ref: str | Path) -> Book:
    """Find an imported book by path (content hash) or by book id."""
    s = str(ref)
    if s.startswith("b_"):
        row = conn.execute("SELECT * FROM books WHERE id=?", (s,)).fetchone()
        if row:
            return Book.from_row(row)
    p = Path(s).expanduser()
    if p.is_file():
        row = conn.execute("SELECT * FROM books WHERE sha256=?", (sha256_file(p),)).fetchone()
        if row:
            return Book.from_row(row)
        raise NCTEError(f"Book not imported yet: {p} (run `ncte import` first)")
    raise NCTEError(f"Unknown book reference: {s}")


def load_book_package(book: Book) -> EpubPackage:
    p = Path(book.stored_path)
    if not p.exists():
        p = Path(book.source_path)
    if not p.exists() or sha256_file(p) != book.sha256:
        raise NCTEError(f"Stored source EPUB for {book.id} is missing or changed; re-import the original file")
    return load_epub(p)
