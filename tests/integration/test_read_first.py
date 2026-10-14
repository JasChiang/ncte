"""ADR-023: read the whole volume first, name, then translate."""

from ncte.library import import_book
from ncte.memory import visibility as vis
from ncte.memory.db import utcnow
from ncte.memory.repository import ensure_glossary, upsert_character


def _units(conn, book_id):
    return conn.execute("SELECT id, ordinal FROM units WHERE book_id=? AND kind='text' ORDER BY ordinal",
                        (book_id,)).fetchall()


def test_character_names_follow_the_reveal(conn, cfg, fixtures):
    """A character merged after an identity reveal keeps its earlier name before the reveal."""
    book, _ = import_book(conn, cfg, fixtures["basic"])
    us = _units(conn, book.id)
    early, late = us[1], us[5]
    cid, _ = upsert_character(conn, book.id, "かぐや", name_zh="輝夜", aliases=["謎の少女"], unit_id=early["id"],
                              ordinal=early["ordinal"], origin="t")
    ensure_glossary(conn, book.id, "謎の少女", "謎之少女", category="character", origin="t", first_unit_id=early["id"])
    ensure_glossary(conn, book.id, "かぐや", "輝夜", category="character", origin="t", first_unit_id=late["id"])
    conn.execute("INSERT INTO entity_links(book_id, term, target_term, form_type, origin, status, revealed_ordinal, "
                 "created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (book.id, "謎の少女", "かぐや", "alias", "llm", "accepted", late["ordinal"], utcnow()))
    c = conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()

    before = vis.character_view(conn, book.id, c, late["ordinal"] - 1)
    assert before == {"name_ja": "謎の少女", "name_zh": "謎之少女", "aliases_ja": []}
    after = vis.character_view(conn, book.id, c, late["ordinal"])
    assert after == {"name_ja": "かぐや", "name_zh": "輝夜", "aliases_ja": ["謎の少女"]}
    assert vis.character_view(conn, book.id, c, early["ordinal"] - 1) is None
