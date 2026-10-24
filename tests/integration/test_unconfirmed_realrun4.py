"""ADR-103 實測四: a name link with the card it is about never joins the entity another card's words name; the lines a
reading left unanswered are asked again, and those still unanswered go to the second look."""

import json

from ncte.analysis import verify
from ncte.llm.fake import FakeLLM
from ncte.memory import repository as repo
from ncte.memory.entities import character_for_form

from ..helpers import orchestrator_for, start_run
from ..scripted import RESPONDERS
from .test_unconfirmed_cards import _card
from .test_unconfirmed_realrun import _link, _memory
from .test_unconfirmed_realrun2 import _mothers


def test_a_name_link_with_its_card_never_joins_the_entity_another_card_s_words_name(conn, cfg, fixtures):
    """Mio's mother was revealed as イレナ (her card, also called 「母親」 here); the memory step links イレナ to 「母親」
    with her card's id: the link stays with her card — 「母親」 is the present mother's entity, not hers."""
    s = _mothers(conn, cfg, fixtures, intro_text="　路地の奥に、母親が立っていた。イレナという名だった。")
    book, intro = s["book"], s["intro"]
    irena, _ = repo.upsert_character(conn, book.id, "イレナ", name_zh="伊蕾娜", aliases=["母親"], unit_id=intro["id"],
                                     ordinal=intro["ordinal"], origin="test")
    repo.ensure_glossary(conn, book.id, "母親", "母親", category="character", origin="test", first_unit_id=None)
    keys = _card(conn, irena)["reading_keys_json"]
    stats = _memory(conn, book, s["scenes"][1], name_links=[_link("イレナ", "母親", intro, irena)])
    ent = {r[0]: r[1] for r in conn.execute("SELECT source_term, entity_id FROM glossary WHERE source_term IN "
                                            "('母親','イレナ')")}
    assert ent.get("イレナ") != ent["母親"]
    assert _card(conn, irena) is not None and _card(conn, irena)["reading_keys_json"] == keys
    assert "イレナ" not in json.loads(_card(conn, s["a"])["aliases_json"])
    assert character_for_form(conn, book.id, "イレナ") == irena
    assert stats["rejected"] == 0


def _quiet(payload, req):
    """A reading that names no line at all (the real run's empty utterances), and then answers the lines asked again
    only for the first of them."""
    out = RESPONDERS["scene_analyze"](payload, req)
    if payload.get("only_quotes"):
        q = payload["quotes"][0]
        out["utterances"] = [{"unit_id": q["unit_id"], "quote_index": q["quote_index"], "speaker_candidates": [],
                              "resolution_status": "unknown", "speaker_ref": None, "contradictions": []}]
    else:
        out["utterances"] = []
    return out


def test_lines_a_reading_left_unanswered_are_asked_again_and_the_rest_go_to_the_second_look(conn, cfg, fixtures):
    fake = FakeLLM(responders={**RESPONDERS, "scene_analyze": _quiet}, complete_lines=False)
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], fake, mode="context")
    orch = orchestrator_for(conn, cfg, book, rid, opts, fake)
    orch.run()
    asked = [p for p, c in zip(fake.payloads, fake.calls, strict=True) if c.stage == "scene_analyze"]
    again = [p for p in asked if p.get("only_quotes")]
    first = [p for p in asked if not p.get("only_quotes") and p.get("quotes")]
    assert again and len(again) == len(first)  # one small request per scene whose lines were left out
    for p in again:
        full = next(f for f in first if f["scene_id"] == p["scene_id"])
        assert [q["unit_id"] for q in p["quotes"]] == [q["unit_id"] for q in full["quotes"]  # only those, all of them
                                                       if not q["continues_previous_quote"]]
    lines = conn.execute("SELECT * FROM utterances WHERE book_id=?", (book.id,)).fetchall()
    left = [u for u in lines if "(to look at)" in u["notes_json"]]
    assert left  # the ones still without an answer are marked for the second look …
    refs = {i["ref"] for i in verify.collect(conn, book.id) if i["kind"] == "speaker"}
    assert all(f"q:{u['id']}" in refs for u in left if u["resolution_source"] != "verify")
