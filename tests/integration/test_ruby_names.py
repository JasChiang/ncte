"""A name the model quotes with its reading, as it was shown the text (強敵《とも》), is the text's 強敵: the check that
the name occurs in the source compares it without the reading (a new character quoted that way was rejected, and
every line and fact about it with it)."""

from ncte.analysis.scene import apply_scene_analysis
from ncte.library import import_book
from ncte.llm.schemas import SceneAnalysisOutput


def test_a_new_character_quoted_with_its_reading_is_kept(conn, cfg, fixtures):
    book, _ = import_book(conn, cfg, fixtures["ruby"])
    unit = conn.execute("SELECT id, scene_id FROM units WHERE book_id=? AND plain_text LIKE '%と呼べる相手%'",
                        (book.id,)).fetchone()
    scene = conn.execute("SELECT * FROM scenes WHERE book_id=? AND id=?", (book.id, unit["scene_id"])).fetchone()
    parsed = SceneAnalysisOutput.model_validate({
        "new_characters": [{"temp_id": "new_1", "name_ja": "強敵《とも》", "aliases_ja": [], "proposed_name_zh": "勁敵",
                            "evidence_unit_ids": [unit["id"]]}],
        "pov": None, "location": None, "time": None, "participants": [], "utterances": [], "actions": [], "props": [],
        "uncertainties": []})
    apply_scene_analysis(conn, book.id, scene, parsed)
    assert conn.execute("SELECT canonical_name FROM characters WHERE book_id=?", (book.id,)).fetchone()[0] == "強敵"
    assert not conn.execute("SELECT 1 FROM memory_rejections WHERE book_id=? AND reason LIKE 'name_ja not found%'",
                            (book.id,)).fetchone()
