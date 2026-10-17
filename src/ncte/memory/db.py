"""SQLite (WAL) storage with versioned, additive migrations (spec §7, §13).

Migrations are append-only: a new feature adds a new migration, existing tables/rows are never
dropped, so upgrading never loses translations.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "core: books, units, translations, runs, requests, QA, jobs, glossary", """
CREATE TABLE books (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    title TEXT,
    language TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    import_time TEXT NOT NULL
);
CREATE TABLE resources (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id),
    href TEXT NOT NULL,
    media_type TEXT,
    manifest_id TEXT,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    UNIQUE(book_id, href)
);
CREATE TABLE units (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id),
    resource_id TEXT NOT NULL REFERENCES resources(id),
    resource_href TEXT NOT NULL,
    xpath TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    spine_index INTEGER,
    kind TEXT NOT NULL,
    source_text TEXT NOT NULL,
    plain_text TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    inline_map_json TEXT NOT NULL DEFAULT '{}',
    translatable INTEGER NOT NULL,
    skip_reason TEXT,
    blank_before INTEGER NOT NULL DEFAULT 0,
    is_separator INTEGER NOT NULL DEFAULT 0,
    ruby_count INTEGER NOT NULL DEFAULT 0,
    scene_id TEXT,
    UNIQUE(book_id, resource_href, xpath)
);
CREATE INDEX idx_units_book_ordinal ON units(book_id, ordinal);
CREATE INDEX idx_units_book_scene ON units(book_id, scene_id);

CREATE TABLE scenes (
    book_id TEXT NOT NULL REFERENCES books(id),
    id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    spine_index INTEGER,
    start_unit_id TEXT NOT NULL,
    end_unit_id TEXT NOT NULL,
    start_ordinal INTEGER NOT NULL,
    end_ordinal INTEGER NOT NULL,
    break_reason TEXT NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (book_id, id)
);
CREATE TABLE scene_overrides (
    book_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('split', 'merge')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (book_id, unit_id)
);

CREATE TABLE glossary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL REFERENCES books(id),
    source_term TEXT NOT NULL,
    target_term TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'term',
    scope TEXT NOT NULL DEFAULT 'book',
    verified INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL DEFAULT 'user',
    first_unit_id TEXT,
    note TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(book_id, source_term)
);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id),
    mode TEXT NOT NULL,
    model TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    budget_usd REAL NOT NULL,
    unit_limit INTEGER,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE translations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id TEXT NOT NULL REFERENCES units(id),
    target_lang TEXT NOT NULL,
    text TEXT,
    source_hash TEXT NOT NULL,
    context_hash TEXT,
    prompt_version TEXT,
    model_id TEXT,
    glossary_version TEXT,
    mode TEXT,
    status TEXT NOT NULL,
    run_id TEXT,
    request_id INTEGER,
    flags_json TEXT NOT NULL DEFAULT '[]',
    notes_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_translations_unit_status ON translations(unit_id, status);
CREATE INDEX idx_translations_cache ON translations(source_hash, context_hash, prompt_version, model_id);

CREATE TABLE llm_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    book_id TEXT,
    stage TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    provider TEXT,
    prompt_version TEXT,
    structured_mode TEXT,
    unit_ids_json TEXT NOT NULL DEFAULT '[]',
    usage_json TEXT NOT NULL DEFAULT '{}',
    generation_id TEXT,
    cost_usd REAL,
    cost_status TEXT NOT NULL DEFAULT 'pending',
    est_cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    response_text TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX idx_requests_run ON llm_requests(run_id);
CREATE INDEX idx_requests_hash ON llm_requests(request_hash, status);

CREATE TABLE qa_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    book_id TEXT,
    unit_id TEXT,
    type TEXT NOT NULL,
    severity TEXT NOT NULL,
    evidence TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_qa_run ON qa_findings(run_id);
CREATE INDEX idx_qa_unit ON qa_findings(unit_id, status);

CREATE TABLE human_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id TEXT NOT NULL REFERENCES units(id),
    previous_text TEXT,
    corrected_text TEXT NOT NULL,
    reason TEXT,
    edited_at TEXT NOT NULL
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    target_id TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, stage, target_id)
);
CREATE INDEX idx_jobs_run_status ON jobs(run_id, stage, status);

CREATE TABLE model_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL,
    models_json TEXT NOT NULL
);
"""),
    (2, "memory: characters, utterances, speaker hypotheses, speech examples, facts, summaries, FTS5", """
CREATE TABLE characters (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id),
    canonical_name TEXT NOT NULL,
    display_name_zh TEXT,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    known_from_unit_id TEXT,
    known_from_ordinal INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'partial',
    origin TEXT NOT NULL,
    speech_profile_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(book_id, canonical_name)
);
CREATE TABLE utterances (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    scene_id TEXT,
    quote_index INTEGER NOT NULL,
    quote_start INTEGER NOT NULL,
    quote_end INTEGER NOT NULL,
    quote_text TEXT NOT NULL,
    continuation_of TEXT,
    speaker_id_nullable TEXT REFERENCES characters(id),
    status TEXT NOT NULL,
    resolution_source TEXT NOT NULL,
    notes_json TEXT NOT NULL DEFAULT '[]',
    analysis_version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(unit_id, quote_index)
);
CREATE INDEX idx_utterances_scene ON utterances(book_id, scene_id);
CREATE TABLE speaker_hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    utterance_id TEXT NOT NULL REFERENCES utterances(id),
    candidate_character_id TEXT NOT NULL REFERENCES characters(id),
    evidence_json TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_hyp_utt ON speaker_hypotheses(utterance_id);
CREATE TABLE speech_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL REFERENCES characters(id),
    unit_id TEXT NOT NULL REFERENCES units(id),
    utterance_id TEXT NOT NULL REFERENCES utterances(id),
    raw_text TEXT NOT NULL,
    verification_status TEXT NOT NULL CHECK (verification_status IN ('explicit', 'human_verified')),
    created_at TEXT NOT NULL,
    UNIQUE(character_id, utterance_id)
);
CREATE TABLE facts (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id),
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    certainty TEXT NOT NULL,
    fact_json TEXT NOT NULL,
    evidence_unit_ids_json TEXT NOT NULL,
    evidence_quote TEXT NOT NULL,
    first_revealed_unit_id TEXT NOT NULL,
    first_revealed_ordinal INTEGER NOT NULL,
    source_viewpoint TEXT,
    valid_from TEXT,
    valid_until TEXT,
    retracted_by TEXT,
    retracted_at_ordinal INTEGER,
    revision INTEGER NOT NULL DEFAULT 1,
    scene_id TEXT,
    run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_facts_book_reveal ON facts(book_id, first_revealed_ordinal);
CREATE TABLE chapter_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL REFERENCES books(id),
    chapter_ref TEXT NOT NULL,
    scene_id TEXT,
    summary TEXT NOT NULL,
    horizon_unit_id TEXT NOT NULL,
    horizon_ordinal INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_summaries_book ON chapter_summaries(book_id, horizon_ordinal);
CREATE TABLE prop_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    name_ja TEXT NOT NULL,
    holder_character_id TEXT,
    evidence_unit_ids_json TEXT NOT NULL,
    horizon_ordinal INTEGER NOT NULL,
    scene_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE memory_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    scene_id TEXT,
    stage TEXT NOT NULL,
    item_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE memory_fts USING fts5(
    text, kind UNINDEXED, ref_id UNINDEXED, book_id UNINDEXED, ordinal UNINDEXED, tokenize='trigram'
);
INSERT INTO memory_fts(text, kind, ref_id, book_id, ordinal)
    SELECT plain_text, 'unit', id, book_id, ordinal FROM units WHERE spine_index IS NOT NULL;
"""),
    (3, "requests.reasoning_effort; FTS unit text without ruby readings", """
ALTER TABLE llm_requests ADD COLUMN reasoning_effort TEXT;
"""),
    (4, "glossary_version keyed on verified entries only (rehash existing translations)", """
SELECT 1;
"""),
    (5, "entities: one entity, many name forms (official / nickname / alias …) with evidence links", """
ALTER TABLE glossary ADD COLUMN entity_id TEXT;
ALTER TABLE glossary ADD COLUMN form_type TEXT NOT NULL DEFAULT 'official';
ALTER TABLE glossary ADD COLUMN form_note TEXT;
CREATE INDEX idx_glossary_entity ON glossary(book_id, entity_id);
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    label_ja TEXT NOT NULL,
    character_id TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE entity_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    term TEXT NOT NULL,
    target_term TEXT NOT NULL,
    form_type TEXT NOT NULL,
    evidence_unit_id TEXT,
    evidence_quote TEXT,
    origin TEXT NOT NULL,
    status TEXT NOT NULL,
    revealed_ordinal INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_entity_links_book ON entity_links(book_id, status);
"""),
    (6, "series: books.series_id / volume_index; library index (workspace/library.db)", """
ALTER TABLE books ADD COLUMN series_id TEXT;
ALTER TABLE books ADD COLUMN volume_index REAL NOT NULL DEFAULT 1;
ALTER TABLE books ADD COLUMN volume_label TEXT;
CREATE TABLE series (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE library_books (
    book_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    series_id TEXT NOT NULL,
    volume_index REAL NOT NULL,
    volume_label TEXT,
    title TEXT,
    source_path TEXT,
    added_at TEXT NOT NULL
);
"""),
    (7, "address terms (who calls whom what), shared name forms, series style guide", """
CREATE TABLE address_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    speaker_id TEXT NOT NULL,
    addressee_id TEXT NOT NULL,
    form_ja TEXT NOT NULL,
    form_zh TEXT,
    zh_origin TEXT,
    note TEXT,
    origin TEXT NOT NULL,
    certainty TEXT NOT NULL,
    first_unit_id TEXT,
    first_ordinal INTEGER NOT NULL,
    last_book_id TEXT,
    last_ordinal INTEGER,
    uses INTEGER NOT NULL DEFAULT 1,
    evidence_quote TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(speaker_id, addressee_id, form_ja)
);
CREATE INDEX idx_address_pair ON address_terms(speaker_id, addressee_id);
CREATE TABLE address_observations (
    utterance_id TEXT NOT NULL,
    address_id INTEGER NOT NULL,
    PRIMARY KEY (utterance_id, address_id)
);
CREATE TABLE form_senses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    term TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    note TEXT,
    origin TEXT NOT NULL,
    evidence_unit_id TEXT,
    evidence_quote TEXT,
    revealed_ordinal INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(term, entity_id)
);
ALTER TABLE series ADD COLUMN style_json TEXT NOT NULL DEFAULT '{}';
"""),
    (8, "naming proposals: renderings for names and address terms chosen before translating (ADR-023)", """
CREATE TABLE naming_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    label TEXT NOT NULL,
    previous_zh TEXT,
    chosen_zh TEXT NOT NULL,
    options_json TEXT NOT NULL,
    reason TEXT,
    importance INTEGER NOT NULL DEFAULT 0,
    needs_review INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    decided_zh TEXT,
    run_id TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(kind, ref)
);
"""),
    (9, "naming proposals keep example sentences to show while choosing", """
ALTER TABLE naming_proposals ADD COLUMN context_json TEXT NOT NULL DEFAULT '[]';
"""),
    (10, "translator's notes (reader-facing, exported in parentheses)", """
ALTER TABLE translations ADD COLUMN translator_note TEXT;
"""),
    (11, "wordplay and real-world references found while reading (ADR-026)", """
CREATE TABLE reader_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    scene_id TEXT,
    term TEXT NOT NULL,
    explanation TEXT NOT NULL,
    note_zh TEXT,
    evidence_quote TEXT,
    origin TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'found',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_reader_notes_unit ON reader_notes(unit_id);
"""),
    (12, "character gender with verbatim evidence (2nd person 妳／你)", """
CREATE TABLE character_gender (
    character_id TEXT PRIMARY KEY REFERENCES characters(id),
    gender TEXT NOT NULL,
    book_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    evidence_quote TEXT,
    origin TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""),
    (13, "gender certainty: explicit (a word in the text says so) or inferred (speech style …)", """
ALTER TABLE character_gender ADD COLUMN certainty TEXT NOT NULL DEFAULT 'explicit';
"""),
    (14, "scene breaks chosen by the model inside long stretches without the author's markers (ADR-036)", """
CREATE TABLE scene_breaks (
    book_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    reason TEXT,
    origin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (book_id, unit_id)
);
"""),
    (15, "how a form found in someone's line is used: address / mention / self / quoted / unrelated (ADR-037)", """
CREATE TABLE address_usage (
    book_id TEXT NOT NULL,
    speaker_id TEXT NOT NULL,
    addressee_id TEXT NOT NULL,
    form_ja TEXT NOT NULL,
    usage TEXT NOT NULL,
    reason TEXT,
    origin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (speaker_id, addressee_id, form_ja)
);
"""),
    (16, "relations between characters, summarized from evidence-backed facts, visible from their evidence (ADR-039)", """
CREATE TABLE relations (
    book_id TEXT NOT NULL,
    a_id TEXT NOT NULL,
    b_id TEXT NOT NULL,
    relation_zh TEXT NOT NULL,
    a_is TEXT,
    b_is TEXT,
    since_ordinal INTEGER NOT NULL,
    fact_ids_json TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (book_id, a_id, b_id, relation_zh)
);
"""),
    (17, "who started or continued a run and who changed its budget (fal real run: a budget changed with no record)", """
CREATE TABLE run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    source TEXT,
    pid INTEGER,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_run_events_run ON run_events(run_id);
"""),
    (18, "relations noted while reading, by character id; kinds, ends and origin of relations; narrator placeholders; "
         "character impressions", """
ALTER TABLE facts ADD COLUMN object_ref TEXT;
CREATE TABLE relation_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    scene_id TEXT,
    a_id TEXT NOT NULL,
    b_id TEXT NOT NULL,
    relation_zh TEXT NOT NULL,
    a_is TEXT,
    b_is TEXT,
    kind TEXT NOT NULL,
    evidence_unit_ids_json TEXT NOT NULL,
    evidence_quote TEXT NOT NULL,
    first_ordinal INTEGER NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_relation_notes_book ON relation_notes(book_id, scene_id);
ALTER TABLE relations ADD COLUMN kind TEXT;
ALTER TABLE relations ADD COLUMN certainty TEXT;
ALTER TABLE relations ADD COLUMN origin TEXT NOT NULL DEFAULT 'llm';
ALTER TABLE relations ADD COLUMN until_ordinal INTEGER;
ALTER TABLE relations ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE characters ADD COLUMN is_narrator INTEGER NOT NULL DEFAULT 0;
CREATE TABLE character_impressions (
    character_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    personality TEXT NOT NULL,
    voice TEXT NOT NULL,
    role TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    run_id TEXT,
    updated_at TEXT NOT NULL
);
"""),
    (19, "who each line is said to, as the read pass judged it (你／妳 for the person spoken to)", """
ALTER TABLE utterances ADD COLUMN addressee_ids_json TEXT NOT NULL DEFAULT '[]';
"""),
    (20, "who a group character is made of (「跟班兩人」 = the two followers), judged after reading", """
CREATE TABLE group_members (
    group_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    book_id TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (group_id, member_id)
);
"""),
    (21, "group members noted while reading (the scene they were noted in, so reading it again replaces them)", """
ALTER TABLE group_members ADD COLUMN scene_id TEXT;
ALTER TABLE group_members ADD COLUMN origin TEXT NOT NULL DEFAULT 'verify';
"""),
    (22, "the translation a review / proofreading / reader finding was made on (a finding is settled for that version only)", """
ALTER TABLE qa_findings ADD COLUMN translation_id INTEGER;
"""),
    (23, "chapters from the table of contents; how a book is cut into scenes; what kind of scene a break opens", """
CREATE TABLE chapters (
    book_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    start_ordinal INTEGER NOT NULL,
    end_ordinal INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    PRIMARY KEY (book_id, seq)
);
ALTER TABLE books ADD COLUMN segmentation TEXT;
ALTER TABLE scene_breaks ADD COLUMN kind TEXT;
ALTER TABLE scene_breaks ADD COLUMN sure INTEGER;
ALTER TABLE scenes ADD COLUMN kind TEXT;
"""),
]


def _reindex_units_fts(conn: sqlite3.Connection) -> None:
    from ..epub.units import match_text

    conn.execute("DELETE FROM memory_fts WHERE kind='unit'")
    rows = conn.execute("SELECT id, book_id, ordinal, plain_text FROM units WHERE spine_index IS NOT NULL").fetchall()
    conn.executemany("INSERT INTO memory_fts(text, kind, ref_id, book_id, ordinal) VALUES (?,?,?,?,?)",
                     [(match_text(r[3]), "unit", r[0], r[1], r[2]) for r in rows])


def _rehash_glossary_versions(conn: sqlite3.Connection) -> None:
    import hashlib
    import json

    from ..epub.units import match_text

    gl = conn.execute("SELECT book_id, source_term, target_term, verified, updated_at FROM glossary").fetchall()
    rows = conn.execute("SELECT t.id, t.created_at, u.book_id, u.plain_text FROM translations t JOIN units u ON "
                        "u.id=t.unit_id WHERE t.status IN ('succeeded','needs_review')").fetchall()
    for tid, created, book, text in rows:
        t = match_text(text)
        rel = sorted((g[1], g[2]) for g in gl if g[0] == book and g[3] and g[4] <= created and g[1] in t)
        h = "gv:" + hashlib.sha256(json.dumps(rel, ensure_ascii=False).encode()).hexdigest()[:12]
        conn.execute("UPDATE translations SET glossary_version=? WHERE id=?", (h, tid))


def _group_entities(conn: sqlite3.Connection) -> None:
    from .entities import backfill_entities

    for (book_id,) in conn.execute("SELECT id FROM books").fetchall():
        backfill_entities(conn, book_id)


def _assign_series(conn: sqlite3.Connection) -> None:
    import hashlib

    for bid, title in conn.execute("SELECT id, title FROM books WHERE series_id IS NULL").fetchall():
        sid = "sr_" + hashlib.sha256(bid.encode()).hexdigest()[:12]
        conn.execute("INSERT OR IGNORE INTO series(id, title, slug, created_at) VALUES (?,?,?,?)",
                     (sid, title or bid, sid, utcnow()))
        conn.execute("UPDATE books SET series_id=?, volume_index=1 WHERE id=?", (sid, bid))


def _harvest_address_terms(conn: sqlite3.Connection) -> None:
    from .address import harvest_book

    for (book_id,) in conn.execute("SELECT id FROM books").fetchall():
        harvest_book(conn, book_id)


def _naming_context(conn: sqlite3.Connection) -> None:
    from ..analysis.naming import refresh_context

    for (bid,) in conn.execute("SELECT DISTINCT book_id FROM naming_proposals").fetchall():
        refresh_context(conn, bid)


POST_MIGRATION = {3: _reindex_units_fts, 4: _rehash_glossary_versions, 5: _group_entities, 6: _assign_series,
                  7: _harvest_address_terms, 9: _naming_context}


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    migrate(conn)
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, "
                 "description TEXT, applied_at TEXT NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def migrate(conn: sqlite3.Connection) -> int:
    ver = current_version(conn)
    for version, desc, sql in MIGRATIONS:
        if version <= ver:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            if version in POST_MIGRATION:
                POST_MIGRATION[version](conn)
            conn.execute("INSERT INTO schema_migrations(version, description, applied_at) VALUES (?,?,?)",
                         (version, desc, utcnow()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        ver = version
    return ver


def _split_sql(sql: str) -> list[str]:
    """Split a migration script into statements (handles CREATE TRIGGER … BEGIN … END;)."""
    out, buf, in_trigger = [], [], False
    for line in sql.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        buf.append(line)
        up = s.upper()
        if up.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if up.startswith("END;"):
                out.append("\n".join(buf))
                buf, in_trigger = [], False
        elif s.endswith(";"):
            out.append("\n".join(buf))
            buf = []
    if buf:
        out.append("\n".join(buf))
    return out


class Transaction:
    """``with Transaction(conn):`` — BEGIN IMMEDIATE / COMMIT / ROLLBACK."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
