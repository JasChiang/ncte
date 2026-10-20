"""A name keeps the readings the source annotates on it (強敵《とも》): the program records them on the glossary entry from
the stored annotations (no judgement), naming sees its example sentences as the translator reads them, and the
translator, the proofreader's cards and the GUI carry the readings next to the name."""

from ncte.analysis.naming import build_naming_payload, model_view
from ncte.analysis.proofread import cast
from ncte.epub.units import term_readings
from ncte.gui import data
from ncte.library import import_book
from ncte.llm.prompt_loader import load_prompt
from ncte.memory import db
from ncte.memory import repository as repo
from ncte.memory.db import Transaction
from ncte.translation.cache import glossary_entries
from ncte.translation.context_builder import glossary_payload


def _book(conn, cfg, fixtures):
    book, _ = import_book(conn, cfg, fixtures["ruby"])
    unit = conn.execute("SELECT id, ordinal, plain_text FROM units WHERE book_id=? AND plain_text LIKE '%と呼べる相手%'",
                        (book.id,)).fetchone()
    with Transaction(conn):
        repo.ensure_glossary(conn, book.id, "強敵", "勁敵", category="character", origin="memory_update",
                             first_unit_id=unit["id"])
        repo.ensure_glossary(conn, book.id, "魔法", "魔法", category="skill", origin="memory_update", first_unit_id=None)
        repo.ensure_glossary(conn, book.id, "約束", "約定", category="item", origin="memory_update", first_unit_id=None)
        repo.upsert_character(conn, book.id, "強敵", name_zh="勁敵", aliases=[], unit_id=unit["id"],
                              ordinal=unit["ordinal"], origin="test")
    return book, unit


def _readings(conn, book_id, term):
    return conn.execute("SELECT readings_json FROM glossary WHERE book_id=? AND source_term=?", (book_id, term)).fetchone()[0]


def test_readings_are_collected_from_the_source(conn, cfg, fixtures):
    book, _ = _book(conn, cfg, fixtures)
    assert _readings(conn, book.id, "強敵") == '["とも"]'  # the heading's bare 強敵 adds nothing
    assert _readings(conn, book.id, "魔法") == '["まほう"]'  # one reading per character, the term spans them all
    assert _readings(conn, book.id, "約束") == "[]"
    assert repo.readings_of(conn, book.id) == {"強敵": ["とも"], "魔法": ["まほう"]}
    # only an occurrence that is exactly the annotated bases counts
    texts = [r[0] for r in conn.execute("SELECT plain_text FROM units WHERE book_id=?", (book.id,))]
    assert term_readings(texts, "強") == [] and term_readings(texts, "魔") == ["ま"]


def test_naming_sees_the_reading(conn, cfg, fixtures):
    book, _ = _book(conn, cfg, fixtures)
    item = next(n for n in build_naming_payload(conn, book.id)["names"] if n["ja"] == "強敵")
    assert item["readings"] == ["とも"]
    assert any("強敵《とも》" in s for s in item["samples"])
    assert "readings" in model_view({"names": [item]})["names"][0]
    prompt = load_prompt("naming")
    assert "選譯名時參考讀音" in prompt.render({"names": [item]})
    assert "選譯名時參考讀音" not in prompt.render({"names": [{"ja": "x"}]})


def test_translator_and_proofreader_get_the_readings(conn, cfg, fixtures):
    book, unit = _book(conn, cfg, fixtures)
    gl = glossary_payload(glossary_entries(conn, book.id), unit["plain_text"], readings=True)
    assert next(g for g in gl if g["ja"] == "強敵")["readings"] == ["とも"]
    assert all("readings" not in g for g in glossary_payload(glossary_entries(conn, book.id), unit["plain_text"]))
    card = next(c for c in cast(conn, book.id, [{"ja": unit["plain_text"], "_at": unit["ordinal"]}]) if c["ja"] == "強敵")
    assert card["readings"] == ["とも"]
    assert "不要寫進譯文" in load_prompt("translate").render({"glossary": gl})


def test_gui_shows_the_readings(conn, cfg, fixtures):
    book, _ = _book(conn, cfg, fixtures)
    assert next(g for g in data.glossary(conn, book) if g["ja"] == "強敵")["readings"] == ["とも"]
    people = data.characters(conn, book, series=True)["characters"]
    assert next(p for p in people.values() if p["ja"] == "強敵")["readings"] == ["とも"]


def test_migration_33_adds_the_readings_column(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] < 33])
    c = db.connect(tmp_path / "old.db")
    assert "readings_json" not in {r[1] for r in c.execute("PRAGMA table_info(glossary)")}
    c.close()
    monkeypatch.undo()
    c = db.connect(tmp_path / "old.db")
    col = next(r for r in c.execute("PRAGMA table_info(glossary)") if r[1] == "readings_json")
    assert col[4] == "'[]'"  # an entry made before has no readings until the book is read again
    c.close()
