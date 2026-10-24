"""Export translated EPUB + validation (spec §4.3)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .epub.reassemble import ExportResult, TranslationPatch, build_epub
from .epub.validate import ValidationReport, validate_epub
from .errors import NCTEError
from .library import Book, load_book_package
from .memory.db import utcnow
from .translation.cache import latest_texts


@dataclass
class ExportOutcome:
    result: ExportResult
    validation: ValidationReport
    report_path: Path
    stats: dict[str, Any]
    acceptance: dict[str, Any] | None = None  # whether the book is done, apart from whether it exports


def default_output_path(cfg: Config, book: Book, lang: str) -> Path:
    stem = Path(book.source_path).stem
    return cfg.workspace / "exports" / f"{stem}.{lang}.epub"


def exported_text(text: str, note: str | None, status: str) -> str:
    """A unit as it goes into the book: the translation, and a machine translator's note in parentheses after it
    (ADR-025). The fix check reads this too, so a fix made (or an error brought in) in the note is seen."""
    return f"{text}（註：{note}）" if status != "human" and note else text


def export_book(conn: sqlite3.Connection, cfg: Config, book: Book, *, output: Path | None = None,
                lang: str = "zh-TW", strict: bool = False, run_epubcheck: bool = True) -> ExportOutcome:
    pkg = load_book_package(book)
    out = Path(output) if output else default_output_path(cfg, book, lang)
    for protected in (Path(book.source_path), Path(book.stored_path)):
        if protected.exists() and out.resolve() == protected.resolve():
            raise NCTEError("Refusing to overwrite the original EPUB")
    from .memory.db import Transaction
    from .translation.headings import follow_headings

    with Transaction(conn):  # a heading fixed or edited since the run: its TOC entries follow it (ADR-106)
        follow_headings(conn, book.id, meta=cfg.export.translate_nav_titles)
    units = {r["id"]: r for r in conn.execute("SELECT * FROM units WHERE book_id=?", (book.id,))}
    best = latest_texts(conn, book.id)
    patches: dict[tuple[str, str], TranslationPatch] = {}
    for uid, tr in best.items():
        u = units[uid]
        text = exported_text(tr["text"], tr["translator_note"], tr["status"])
        patches[(u["resource_href"], u["xpath"])] = TranslationPatch(text, u["source_hash"], uid)
    translatable = [u for u in units.values() if u["translatable"]]
    missing = [u["id"] for u in translatable if u["id"] not in best]
    if strict and missing:
        raise NCTEError(f"{len(missing)} translatable units have no valid translation (use without --strict "
                        f"to export with the original Japanese kept for them)")
    result = build_epub(pkg, patches, out, lang=lang)
    validation = validate_epub(out, original=pkg.path, epubcheck_path=cfg.export.epubcheck_path,
                               run_epubcheck=run_epubcheck, workspace=cfg.workspace)
    for uid, err in result.rebuild_errors:
        conn.execute("INSERT INTO qa_findings(book_id, unit_id, type, severity, evidence, created_at) "
                     "VALUES (?,?,?,?,?,?)", (book.id, uid, "rebuild_error", "high", err, utcnow()))
    unsafe = [{"unit_id": u["id"], "href": u["resource_href"], "reason": u["skip_reason"], "text": u["plain_text"][:80]}
              for u in units.values() if not u["translatable"] and u["skip_reason"] not in ("no_letters",)]
    stats = {
        "units_total": len(units), "translatable": len(translatable), "translated": len(best),
        "untranslated_kept_original": len(missing), "applied": result.applied,
        "hash_mismatch": len(result.hash_mismatch), "rebuild_errors": len(result.rebuild_errors),
        "unsafe_units_kept_original": len(unsafe), "human_edits": sum(1 for t in best.values() if t["status"] == "human"),
        "needs_review": sum(1 for t in best.values() if t["status"] == "needs_review"),
    }
    from .reports.acceptance import acceptance

    context = conn.execute("SELECT 1 FROM runs WHERE book_id=? AND mode='context' LIMIT 1", (book.id,)).fetchone()
    accept = acceptance(conn, book.id, proofread=bool(context) and cfg.context.proofread,
                        reader=bool(context) and cfg.context.reader_pass, validation_ok=validation.ok)
    report = {
        "acceptance": accept,
        "book_id": book.id, "title": book.title, "output": str(out), "exported_at": utcnow(), "language": lang,
        "stats": stats, "modified_files": result.modified_files, "validation": validation.to_dict(),
        "untranslated_unit_ids": missing, "unsafe_units": unsafe,
        "rebuild_errors": [{"unit_id": u, "error": e} for u, e in result.rebuild_errors],
        "hash_mismatch_unit_ids": result.hash_mismatch,
    }
    rdir = cfg.workspace / "reports" / book.id
    rdir.mkdir(parents=True, exist_ok=True)
    rpath = rdir / f"export-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.json"
    rpath.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return ExportOutcome(result, validation, rpath, stats, accept)
