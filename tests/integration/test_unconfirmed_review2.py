"""ADR-103 review of the real-run rounds: a link's other word naming several cards, display-only words in lookups,
an answer naming a card merged earlier in the same look, several words for one card in one memory answer."""

import json

from ncte.analysis import verify
from ncte.analysis.scene import get_scene
from ncte.llm.schemas import VerifyOutput
from ncte.memory import repository as repo
from ncte.memory import visibility as vis
from ncte.memory.address import name_forms

from .test_unconfirmed_cards import _card, _setup, _speaker
from .test_unconfirmed_realrun import _gloss, _link, _memory
from .test_unconfirmed_realrun2 import _mothers
from .test_unconfirmed_realrun3 import _scene


def test_a_link_whose_other_word_names_several_cards_needs_the_card(conn, cfg, fixtures):
    s = _mothers(conn, cfg, fixtures, intro_text="　路地の奥に、母親が立っていた。エレナという名だった。")
    book, intro = s["book"], s["intro"]
    stats = _memory(conn, book, s["scenes"][1], glossary_candidates=[_gloss("エレナ", intro)],
                    name_links=[_link("エレナ", "母親", intro)])
    assert "エレナ" not in json.loads(_card(conn, s["a"])["aliases_json"])
    assert "エレナ" not in json.loads(_card(conn, s["b"])["aliases_json"])
    ent = {r[0]: r[1] for r in conn.execute("SELECT source_term, entity_id FROM glossary WHERE source_term IN "
                                            "('母親','エレナ')")}
    assert ent.get("エレナ") != ent.get("母親")
    reasons = [r[0] for r in conn.execute("SELECT reason FROM memory_rejections WHERE stage='memory_update'")]
    assert stats["rejected"] >= 1 and any("several cards" in r for r in reasons)


def test_words_a_card_is_only_shown_by_find_no_one(conn, cfg, fixtures):
    """イレナ is shown as 「白衣の女」 before her name (display only): that phrase names no one in a lookup."""
    s = _mothers(conn, cfg, fixtures, intro_text="　路地の奥に、白衣の女が立っていた。")
    book, intro, first = s["book"], s["intro"], s["first"]
    irena, _ = repo.upsert_character(conn, book.id, "イレナ", name_zh=None, aliases=[], unit_id=first["id"],
                                     ordinal=first["ordinal"], origin="test")
    repo.note_forms(conn, irena, ["白衣の女"], book.id, first["ordinal"])  # recorded to show her by, not a name
    view = vis.character_view(conn, book.id, _card(conn, irena), intro["ordinal"])
    assert view is not None and "白衣の女" in [view["name_ja"], *view["aliases_ja"]]  # shown …
    assert "白衣の女" not in name_forms(conn, book.id, intro["ordinal"])[0]  # … never found by it
    made = _scene(conn, s, [{"temp_id": "new_1", "name_ja": "白衣の女"}])["new_1"]
    assert made != irena


def test_an_answer_naming_a_card_merged_earlier_in_the_same_look_follows_the_merge(conn, cfg, fixtures):
    s = _setup(conn, cfg, fixtures)
    book, s1 = s["book"], s["scenes"][1]
    state = json.loads(get_scene(conn, book.id, s1["id"])["state_json"])
    state["uncertainties"] = [{"unit_ids": [s["line"]["id"]], "question": "誰が言ったのか"}]
    conn.execute("UPDATE scenes SET state_json=? WHERE id=?", (json.dumps(state, ensure_ascii=False), s1["id"]))
    items = verify.collect(conn, book.id)
    same = next(i for i in items if i["ref"] == f"s:{s['p']}")
    question = next(i for i in items if i["kind"] == "question")
    assert s["p"] in question["candidates"]
    parsed = VerifyOutput.model_validate({"items": [
        {"ref": same["ref"], "answer": s["a"], "reason": "x", "evidence": [next(iter(same["_codes"]))]},
        {"ref": question["ref"], "answer": "その母親", "reason": "x", "character_id": s["p"], "line_role": "speaker"}]})
    verify.apply(conn, book.id, items, parsed, lambda *a: None)
    assert _card(conn, s["p"]) is None and _speaker(conn, s["line"]["id"]) == s["a"]


def test_several_words_for_one_card_in_one_memory_answer_are_all_kept(conn, cfg, fixtures):
    s = _mothers(conn, cfg, fixtures, intro_text="　路地の奥に、母親が立っていた。澄枝とも、エレナとも呼ばれた。")
    book, intro = s["book"], s["intro"]
    _memory(conn, book, s["scenes"][1], glossary_candidates=[_gloss("澄枝", intro, s["b"]), _gloss("エレナ", intro, s["b"])])
    assert {"澄枝", "エレナ"} <= set(json.loads(_card(conn, s["b"])["aliases_json"]))
