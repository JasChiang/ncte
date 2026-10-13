"""Workspace layout (ADR-020): one folder + one SQLite database per series, plus a library index.

    workspace/
      library.db                       index: series and which book is which volume (+ model price snapshots)
      series/<slug>/ncte.db            everything about one series (all volumes)
      series/<slug>/books/<book_id>/   stored copy of each source EPUB
      exports/ reports/ tools/ logs/

A standalone book is a series with one volume. The previous single ``workspace/ncte.db`` is migrated
automatically (and kept as ``ncte.legacy-<timestamp>.db``).
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .epub.parser import load_epub, sha256_file
from .errors import NCTEError
from .library import Book, import_book, resolve_book
from .memory.db import Transaction, connect, utcnow
from .series import ensure_series

# tables whose rows belong to one book (used when splitting a legacy multi-book database)
_BOOK_TABLES = ("jobs", "llm_requests", "qa_findings", "utterances", "memory_rejections", "entity_links", "entities",
                "facts", "chapter_summaries", "prop_states", "scene_overrides", "scenes", "units", "resources",
                "glossary", "characters", "runs")  # dependents first (foreign keys)


def slugify(title: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title).strip().strip(".")
    s = re.sub(r"\s+", "_", s)[:60]
    return s or "series"


class Workspace:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.workspace
        self._lib: sqlite3.Connection | None = None
        self._conns: dict[str, sqlite3.Connection] = {}

    # ------------------------------------------------------------ connections
    @property
    def lib(self) -> sqlite3.Connection:
        if self._lib is None:
            self._lib = connect(self.root / "library.db")
            self._migrate_legacy()
        return self._lib

    def series_row(self, series_id: str) -> sqlite3.Row:
        row = self.lib.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        if row is None:
            raise NCTEError(f"unknown series {series_id}")
        return row

    def series_dir(self, slug: str) -> Path:
        return self.root / "series" / slug

    def series_conn(self, series_id: str) -> sqlite3.Connection:
        if series_id not in self._conns:
            self._conns[series_id] = connect(self.series_dir(self.series_row(series_id)["slug"]) / "ncte.db")
        return self._conns[series_id]

    def close(self) -> None:
        for c in self._conns.values():
            c.close()
        if self._lib is not None:
            self._lib.close()

    # ------------------------------------------------------------ series
    def find_series(self, ref: str) -> sqlite3.Row | None:
        return self.lib.execute("SELECT * FROM series WHERE id=? OR title=? OR slug=?", (ref, ref, ref)).fetchone()

    def create_series(self, title: str) -> str:
        base = slugify(title)
        slug, n = base, 2
        while self.lib.execute("SELECT 1 FROM series WHERE slug=?", (slug,)).fetchone():
            slug, n = f"{base}_{n}", n + 1
        sid = "sr_" + __import__("hashlib").sha256(f"{title}|{utcnow()}|{slug}".encode()).hexdigest()[:12]
        with Transaction(self.lib):
            self.lib.execute("INSERT INTO series(id, title, slug, created_at) VALUES (?,?,?,?)", (sid, title, slug, utcnow()))
        return sid

    def list_series(self) -> list[dict[str, object]]:
        out = []
        for s in self.lib.execute("SELECT * FROM series ORDER BY created_at"):
            vols = [dict(r) for r in self.lib.execute(
                "SELECT book_id, volume_index, volume_label, title FROM library_books WHERE series_id=? "
                "ORDER BY volume_index", (s["id"],))]
            out.append({"series_id": s["id"], "title": s["title"], "folder": str(self.series_dir(s["slug"])),
                        "volumes": vols})
        return out

    # ------------------------------------------------------------ books
    def find_book(self, ref: str | Path) -> tuple[str, str] | None:
        s = str(ref)
        if s.startswith("b_"):
            row = self.lib.execute("SELECT series_id, book_id FROM library_books WHERE book_id=?", (s,)).fetchone()
            if row:
                return row[0], row[1]
        p = Path(s).expanduser()
        if p.is_file():
            row = self.lib.execute("SELECT series_id, book_id FROM library_books WHERE sha256=?",
                                   (sha256_file(p),)).fetchone()
            if row:
                return row[0], row[1]
        return None

    def open_book(self, ref: str | Path) -> tuple[sqlite3.Connection, Book]:
        found = self.find_book(ref)
        if not found:
            raise NCTEError(f"Book not imported yet: {ref} (run `ncte import` first)")
        conn = self.series_conn(found[0])
        return conn, resolve_book(conn, found[1])

    def suggest_series(self, pkg: Any) -> dict[str, Any]:
        """Series/volume suggestion for a package: metadata/title guess + matching series already in the library."""
        from .epub.series_meta import detect_series, guess_from_title, normalize_title

        g = detect_series(pkg)
        out: dict[str, Any] = {"guess": g.to_dict(), "series_id": None, "series_title": None, "volume": g.volume}
        keys = {normalize_title(x) for x in (g.title, pkg.title) if x}
        for s in self.lib.execute("SELECT * FROM series").fetchall():
            names = {normalize_title(s["title"])}
            for b in self.lib.execute("SELECT title FROM library_books WHERE series_id=?", (s["id"],)):
                if b[0]:
                    names.add(normalize_title(b[0]))
                    tg = guess_from_title(b[0])
                    if tg:
                        names.add(normalize_title(tg[0]))
            if keys & names:
                out["series_id"], out["series_title"] = s["id"], s["title"]
                break
        return out

    def import_book(self, path: str | Path, *, series: str | None = None, volume: float | None = None,
                    label: str | None = None) -> tuple[sqlite3.Connection, Book, bool]:
        found = self.find_book(path)
        if found:
            conn, book = self.open_book(path)
            return conn, book, False
        pkg = load_epub(path)  # safety checks before anything is created
        if series == "auto":
            sug = self.suggest_series(pkg)
            if not sug["series_id"] and not sug["guess"]["title"]:
                raise NCTEError("no series information detected; give --series NAME (and --volume N)")
            series = sug["series_id"] or sug["guess"]["title"]
            if volume is None and sug["volume"] is not None:
                volume = float(sug["volume"])
        if series:
            row = self.find_series(series)
            sid = row["id"] if row else self.create_series(series)
        else:
            sid = self.create_series(pkg.title or Path(path).stem)
        srow = self.series_row(sid)
        if volume is None:
            last = self.lib.execute("SELECT MAX(volume_index) FROM library_books WHERE series_id=?", (sid,)).fetchone()[0]
            volume = float(last) + 1 if last is not None else 1.0
        clash = self.lib.execute("SELECT book_id FROM library_books WHERE series_id=? AND volume_index=?",
                                 (sid, volume)).fetchone()
        if clash:
            raise NCTEError(f"volume {volume:g} of «{srow['title']}» is already {clash[0]}; choose another --volume")
        conn = self.series_conn(sid)
        with Transaction(conn):
            ensure_series(conn, srow["title"], series_id=sid, slug=srow["slug"])
        book, created = import_book(conn, self.cfg, path, series_id=sid, volume=volume, volume_label=label,
                                    store_root=self.series_dir(srow["slug"]) / "books")
        self._register(book, sid, volume, label)
        return conn, book, created

    def _register(self, book: Book, sid: str, volume: float, label: str | None) -> None:
        with Transaction(self.lib):
            self.lib.execute(
                "INSERT OR REPLACE INTO library_books(book_id, sha256, series_id, volume_index, volume_label, title, "
                "source_path, added_at) VALUES (?,?,?,?,?,?,?,?)",
                (book.id, book.sha256, sid, volume, label, book.title, book.source_path, utcnow()))

    def set_volume(self, ref: str, volume: float, label: str | None = None) -> None:
        found = self.find_book(ref)
        if not found:
            raise NCTEError(f"unknown book {ref}")
        sid, bid = found
        clash = self.lib.execute("SELECT book_id FROM library_books WHERE series_id=? AND volume_index=? AND book_id!=?",
                                 (sid, volume, bid)).fetchone()
        if clash:
            raise NCTEError(f"volume {volume:g} is already {clash[0]}")
        conn = self.series_conn(sid)
        with Transaction(conn):
            conn.execute("UPDATE books SET volume_index=?, volume_label=COALESCE(?, volume_label) WHERE id=?",
                         (volume, label, bid))
        with Transaction(self.lib):
            self.lib.execute("UPDATE library_books SET volume_index=?, volume_label=COALESCE(?, volume_label) "
                             "WHERE book_id=?", (volume, label, bid))

    def find_run(self, run_id: str) -> sqlite3.Connection:
        for (sid,) in self.lib.execute("SELECT id FROM series").fetchall():
            conn = self.series_conn(sid)
            if conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                return conn
        raise NCTEError(f"unknown run id {run_id}")

    # ------------------------------------------------------------ legacy single-database workspace
    def _migrate_legacy(self) -> None:
        legacy = self.root / "ncte.db"
        assert self._lib is not None
        if not legacy.exists() or self._lib.execute("SELECT COUNT(*) FROM library_books").fetchone()[0]:
            return
        old = connect(legacy)  # applies all migrations (books get a series each)
        old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        groups = old.execute("SELECT s.id, s.title FROM series s WHERE EXISTS (SELECT 1 FROM books b WHERE "
                             "b.series_id=s.id)").fetchall()
        books = old.execute("SELECT * FROM books").fetchall()
        old.close()
        if not books:
            return
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        for sid, title in groups:
            base, slug, n = slugify(title), slugify(title), 2
            while self._lib.execute("SELECT 1 FROM series WHERE slug=?", (slug,)).fetchone():
                slug, n = f"{base}_{n}", n + 1
            target = self.series_dir(slug) / "ncte.db"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, target)
            conn = connect(target)
            keep = [r[0] for r in conn.execute("SELECT id FROM books WHERE series_id=?", (sid,))]
            if len(keep) != len(books):
                _prune_to(conn, keep)
            with Transaction(conn):
                conn.execute("UPDATE series SET slug=? WHERE id=?", (slug, sid))
                conn.execute("DELETE FROM series WHERE id!=?", (sid,))
            with Transaction(self._lib):
                self._lib.execute("INSERT INTO series(id, title, slug, created_at) VALUES (?,?,?,?)",
                                  (sid, title, slug, utcnow()))
            for b in conn.execute("SELECT * FROM books").fetchall():
                _move_stored_copy(conn, b, self.root, self.series_dir(slug))
                with Transaction(self._lib):
                    self._lib.execute(
                        "INSERT INTO library_books(book_id, sha256, series_id, volume_index, volume_label, title, "
                        "source_path, added_at) VALUES (?,?,?,?,?,?,?,?)",
                        (b["id"], b["sha256"], sid, b["volume_index"], b["volume_label"], b["title"], b["source_path"],
                         utcnow()))
            conn.close()
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(legacy) + suffix)
            if f.exists():
                f.rename(self.root / f"ncte.legacy-{stamp}.db{suffix}")


def _prune_to(conn: sqlite3.Connection, keep: list[str]) -> None:
    ph = ",".join("?" * len(keep))
    with Transaction(conn):
        units = f"SELECT id FROM units WHERE book_id NOT IN ({ph})"
        conn.execute(f"DELETE FROM translations WHERE unit_id IN ({units})", keep)
        conn.execute(f"DELETE FROM human_edits WHERE unit_id IN ({units})", keep)
        conn.execute(f"DELETE FROM speech_examples WHERE unit_id IN ({units})", keep)
        conn.execute(f"DELETE FROM speaker_hypotheses WHERE utterance_id IN (SELECT id FROM utterances WHERE "
                     f"book_id NOT IN ({ph}))", keep)
        conn.execute(f"DELETE FROM memory_fts WHERE book_id NOT IN ({ph})", keep)
        for t in _BOOK_TABLES:
            conn.execute(f"DELETE FROM {t} WHERE book_id NOT IN ({ph})", keep)
        conn.execute(f"DELETE FROM books WHERE id NOT IN ({ph})", keep)


def _move_stored_copy(conn: sqlite3.Connection, book: sqlite3.Row, root: Path, series_dir: Path) -> None:
    """Move workspace/books/<id>/source.epub into the series folder (legacy layout)."""
    old = Path(book["stored_path"])
    if not old.exists() or root / "books" not in old.parents:
        return
    new = series_dir / "books" / book["id"] / old.name
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), new)
    with Transaction(conn):
        conn.execute("UPDATE books SET stored_path=? WHERE id=?", (str(new), book["id"]))
    try:
        old.parent.rmdir()
    except OSError:
        pass


# --------------------------------------------------------------------------- moving a book between series

# copy order respects foreign keys; (table, where-clause on the source row, drop-integer-id)
_MOVE_PLAN: list[tuple[str, str, bool]] = [
    ("books", "id=:b", False), ("resources", "book_id=:b", False), ("units", "book_id=:b", False),
    ("scenes", "book_id=:b", False), ("scene_overrides", "book_id=:b", False), ("glossary", "book_id=:b", True),
    ("characters", "book_id=:b", False), ("entities", "book_id=:b", False), ("entity_links", "book_id=:b", True),
    ("runs", "book_id=:b", False), ("jobs", "book_id=:b", True), ("llm_requests", "book_id=:b", True),
    ("translations", "unit_id IN (SELECT id FROM units WHERE book_id=:b)", True),
    ("human_edits", "unit_id IN (SELECT id FROM units WHERE book_id=:b)", True),
    ("utterances", "book_id=:b", False),
    ("speaker_hypotheses", "utterance_id IN (SELECT id FROM utterances WHERE book_id=:b)", True),
    ("speech_examples", "unit_id IN (SELECT id FROM units WHERE book_id=:b)", True),
    ("facts", "book_id=:b", False), ("chapter_summaries", "book_id=:b", True), ("prop_states", "book_id=:b", True),
    ("memory_rejections", "book_id=:b", True), ("qa_findings", "book_id=:b", True),
]


def move_book(ws: Workspace, ref: str, series: str, *, volume: float | None = None,
              label: str | None = None) -> dict[str, int]:
    """Move an imported book (translations, runs, memory) into another series. Names and characters that both
    series know are merged (human-verified renderings win, then the target's)."""
    found = ws.find_book(ref)
    if not found:
        raise NCTEError(f"unknown book {ref}")
    src_sid, bid = found
    row = ws.find_series(series)
    dst_sid = row["id"] if row else ws.create_series(series)
    if dst_sid == src_sid:
        raise NCTEError("the book is already in that series")
    if volume is None:
        last = ws.lib.execute("SELECT MAX(volume_index) FROM library_books WHERE series_id=?", (dst_sid,)).fetchone()[0]
        volume = float(last) + 1 if last is not None else 1.0
    if ws.lib.execute("SELECT 1 FROM library_books WHERE series_id=? AND volume_index=?", (dst_sid, volume)).fetchone():
        raise NCTEError(f"volume {volume:g} already exists in the target series")
    src, dst = ws.series_conn(src_sid), ws.series_conn(dst_sid)
    drow = ws.series_row(dst_sid)
    stats: dict[str, int] = {}
    req_map: dict[int, int] = {}
    with Transaction(dst):
        ensure_series(dst, drow["title"], series_id=dst_sid, slug=drow["slug"])
        for table, where, drop_id in _MOVE_PLAN:
            rows = src.execute(f"SELECT * FROM {table} WHERE {where}", {"b": bid}).fetchall()
            for r in rows:
                d = dict(r)
                old_id = d.pop("id") if drop_id else None
                if table == "books":
                    d.update(series_id=dst_sid, volume_index=volume, volume_label=label or d.get("volume_label"))
                if table == "translations" and d.get("request_id") is not None:
                    d["request_id"] = req_map.get(d["request_id"])
                cols = ",".join(d)
                cur = dst.execute(f"INSERT INTO {table}({cols}) VALUES ({','.join('?' * len(d))})", list(d.values()))
                if table == "llm_requests" and old_id is not None:
                    req_map[int(old_id)] = int(cur.lastrowid or 0)
            stats[table] = len(rows)
        for r in src.execute("SELECT text, kind, ref_id, book_id, ordinal FROM memory_fts WHERE book_id=?", (bid,)):
            dst.execute("INSERT INTO memory_fts(text, kind, ref_id, book_id, ordinal) VALUES (?,?,?,?,?)", tuple(r))
        stats.update(_merge_duplicates(dst, bid))
    _prune_to(src, [r[0] for r in src.execute("SELECT id FROM books WHERE id!=?", (bid,))])
    # stored source copy follows the book
    b = dst.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone()
    old = Path(b["stored_path"])
    if old.exists():
        new = ws.series_dir(drow["slug"]) / "books" / bid / old.name
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), new)
        with Transaction(dst):
            dst.execute("UPDATE books SET stored_path=? WHERE id=?", (str(new), bid))
    with Transaction(ws.lib):
        ws.lib.execute("UPDATE library_books SET series_id=?, volume_index=?, volume_label=COALESCE(?, volume_label) "
                       "WHERE book_id=?", (dst_sid, volume, label, bid))
    if not src.execute("SELECT 1 FROM books").fetchone():  # source series is now empty: retire it (kept on disk)
        srow = ws.series_row(src_sid)
        src.close()
        ws._conns.pop(src_sid, None)
        folder = ws.series_dir(srow["slug"])
        folder.rename(folder.with_name(f"{folder.name}.moved-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"))
        with Transaction(ws.lib):
            ws.lib.execute("DELETE FROM series WHERE id=?", (src_sid,))
    return stats


def _merge_duplicates(conn: sqlite3.Connection, moved: str) -> dict[str, int]:
    """After a move: one glossary row per term and one character per name across the target series."""
    from .series import pos

    merged = {"glossary_merged": 0, "characters_merged": 0}
    for g in conn.execute("SELECT * FROM glossary WHERE book_id=?", (moved,)).fetchall():
        other = conn.execute("SELECT * FROM glossary WHERE book_id!=? AND source_term=? AND book_id IN "
                             "(SELECT id FROM books WHERE series_id=(SELECT series_id FROM books WHERE id=?))",
                             (moved, g["source_term"], moved)).fetchone()
        if other is None:
            continue
        loser = other if (g["verified"] and not other["verified"]) else g
        conn.execute("DELETE FROM glossary WHERE id=?", (loser["id"],))
        merged["glossary_merged"] += 1
    for c in conn.execute("SELECT * FROM characters WHERE book_id=?", (moved,)).fetchall():
        other = conn.execute("SELECT * FROM characters WHERE book_id!=? AND canonical_name=? AND book_id IN "
                             "(SELECT id FROM books WHERE series_id=(SELECT series_id FROM books WHERE id=?))",
                             (moved, c["canonical_name"], moved)).fetchone()
        if other is None:
            continue
        for sql in ("UPDATE utterances SET speaker_id_nullable=? WHERE speaker_id_nullable=?",
                    "UPDATE speaker_hypotheses SET candidate_character_id=? WHERE candidate_character_id=?",
                    "UPDATE speech_examples SET character_id=? WHERE character_id=?",
                    "UPDATE facts SET subject=? WHERE subject=?",
                    "UPDATE entities SET character_id=? WHERE character_id=?"):
            conn.execute(sql, (other["id"], c["id"]))
        if pos(conn, moved, c["known_from_ordinal"]) < pos(conn, other["book_id"], other["known_from_ordinal"]):
            conn.execute("UPDATE characters SET book_id=?, known_from_unit_id=?, known_from_ordinal=? WHERE id=?",
                         (moved, c["known_from_unit_id"], c["known_from_ordinal"], other["id"]))
        conn.execute("DELETE FROM characters WHERE id=?", (c["id"],))
        merged["characters_merged"] += 1
    return merged
