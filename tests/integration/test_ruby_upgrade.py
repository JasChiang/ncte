"""Books imported when readings were written base《reading》 move to the annotation characters once, translations kept."""

import hashlib
import json

from ncte.epub.units import for_model
from ncte.library import RUBY_FORMAT, upgrade_ruby
from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS


def test_an_old_book_gets_the_new_reading_marks_and_keeps_its_translations(conn, cfg, fixtures):
    book, _, _ = run_book(conn, cfg, fixtures["ruby"], FakeLLM(responders=RESPONDERS))
    u = conn.execute("SELECT * FROM units WHERE book_id=? AND ruby_count>0 LIMIT 1", (book.id,)).fetchone()
    assert u is not None and "￹" in u["source_text"]
    # as an older import stored it: readings shown as 蒼《あお》 inside the text itself, translations on that hash
    old_text = for_model(u["source_text"])
    old_hash = "sha256:" + hashlib.sha256(old_text.encode()).hexdigest()
    conn.execute("UPDATE units SET source_text=?, plain_text=?, source_hash=? WHERE id=?",
                 (old_text, for_model(u["plain_text"]), old_hash, u["id"]))
    conn.execute("UPDATE translations SET source_hash=? WHERE unit_id=?", (old_hash, u["id"]))
    meta = json.loads(conn.execute("SELECT metadata_json FROM books WHERE id=?", (book.id,)).fetchone()[0])
    meta.pop("ruby_format", None)
    conn.execute("UPDATE books SET metadata_json=? WHERE id=?", (json.dumps(meta), book.id))

    assert upgrade_ruby(conn, book) >= 1
    after = conn.execute("SELECT * FROM units WHERE id=?", (u["id"],)).fetchone()
    assert after["source_text"] == u["source_text"] and after["source_hash"] == u["source_hash"]
    assert conn.execute("SELECT COUNT(*) FROM translations WHERE unit_id=? AND source_hash=?",
                        (u["id"], u["source_hash"])).fetchone()[0] >= 1  # the translation follows: nothing to redo
    meta = json.loads(conn.execute("SELECT metadata_json FROM books WHERE id=?", (book.id,)).fetchone()[0])
    assert meta["ruby_format"] == RUBY_FORMAT and upgrade_ruby(conn, book) == 0  # once
