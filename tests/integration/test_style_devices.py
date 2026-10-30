"""A device the book uses in a few places (a classic's opening borrowed for a modern line) reaches the house style, and a
paragraph the author repeats word for word comes with the earlier paragraph's translation."""


def _body(conn, book_id):
    return conn.execute("SELECT * FROM units WHERE book_id=? AND kind='text' ORDER BY ordinal", (book_id,)).fetchall()


def test_reader_notes_reach_the_house_style(conn, cfg, fixtures):
    from ncte.analysis import house_style
    from ncte.library import import_book

    book, _ = import_book(conn, cfg, fixtures["basic"])
    u = _body(conn, book.id)[1]
    conn.execute("INSERT INTO reader_notes(book_id, unit_id, term, explanation, origin, created_at) VALUES "
                 "(?, ?, '今は昔', '仿《竹取物語》開頭的古文口吻', 'llm', '2026-10-30')", (book.id, u["id"]))
    conn.execute("INSERT INTO reader_notes(book_id, unit_id, term, explanation, origin, status, created_at) VALUES "
                 "(?, ?, 'x', '不要', 'llm', 'dropped', '2026-10-30')", (book.id, u["id"]))
    req, _ = house_style.request(conn, book.id)
    assert req["reader_notes"] == [{"at": u["ordinal"], "term": "今は昔", "explanation": "仿《竹取物語》開頭的古文口吻"}]


def test_a_repeated_paragraph_comes_with_the_earlier_translation(conn, cfg, fixtures):
    from ncte.library import import_book
    from ncte.translation.context_builder import repeated_earlier

    book, _ = import_book(conn, cfg, fixtures["basic"])
    rows = _body(conn, book.id)
    first, short, again = rows[0], rows[1], rows[2]
    line = "　──ゲームしている普通の女子高生ありけり。"
    for r, text in ((first, line), (short, "　……はい。"), (again, line)):
        conn.execute("UPDATE units SET plain_text=? WHERE id=?", (text, r["id"]))
    conn.execute("INSERT INTO translations(unit_id, target_lang, text, source_hash, status, created_at) VALUES "
                 "(?, 'zh-TW', '⟦1⟧　——從前，有個打電動的女高中生。⟦/1⟧', ?, 'succeeded', '2026-10-30')",
                 (first["id"], first["source_hash"]))
    rows = {r["id"]: r for r in _body(conn, book.id)}
    assert repeated_earlier(conn, book.id, [rows[first["id"]], rows[short["id"]], rows[again["id"]]]) == [
        {"unit_id": again["id"], "earlier_translation": "——從前，有個打電動的女高中生。"}]
