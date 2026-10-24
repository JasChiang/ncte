"""ADR-103, from a real run on a synthetic book: words a card of another time (or a numbered card) shares, the narrator
placeholder, chapter headings alone, a one-character form of address."""

import json

from ncte.analysis.memory_update import apply_memory_update
from ncte.analysis.scene import get_scene, new_narrator, segment_units
from ncte.llm.schemas import MemoryUpdateOutput
from ncte.memory import repository as repo
from ncte.memory.entities import character_for_form

from .test_unconfirmed_cards import _card, _past_mother, _read


def _memory(conn, book, scene, **parts):
    out = {"scene_summary": "", "facts": [], "glossary_candidates": [], "prop_states": [], **parts}
    return apply_memory_update(conn, book.id, get_scene(conn, book.id, scene["id"]),
                               MemoryUpdateOutput.model_validate(out))


def _sumie(conn, cfg, fixtures):
    """The present 「母親」 and the past life's 「母親」, whose name in that life, 澄枝, the reading put on her card."""
    s = _past_mother(conn, cfg, fixtures)
    book = s["book"]
    conn.execute("UPDATE units SET plain_text='　前世の記憶の中で、母親が立っていた。澄枝という名だった。' WHERE id=?",
                 (s["intro"]["id"],))
    past = _read(conn, book, s["scenes"][1], s["intro"], s["line"], other_person=True, period="前世")["new_1"]
    conn.execute("UPDATE characters SET aliases_json=? WHERE id=?", (json.dumps(["澄枝"]), past))
    repo.note_forms(conn, past, ["澄枝"], book.id, s["intro"]["ordinal"])
    return s, past


def _gloss(term, unit, ref=""):
    return {"source_term": term, "target_term": term, "category": "character", "evidence_unit_ids": [unit["id"]],
            **({"character_ref": ref} if ref else {})}


def _link(term, same_as, unit, ref=""):
    return {"term": term, "term_zh": term, "same_as": same_as, "form_type": "alias", "note": "",
            "evidence_unit_ids": [unit["id"]], "evidence_quote": term, **({"character_ref": ref} if ref else {})}


def test_words_a_card_of_another_time_shares_are_not_given_to_the_present_card(conn, cfg, fixtures):
    s, past = _sumie(conn, cfg, fixtures)
    book, s1, intro = s["book"], s["scenes"][1], s["intro"]
    n = conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
    _memory(conn, book, s1, glossary_candidates=[_gloss("澄枝", intro)], name_links=[_link("澄枝", "母親", intro)])
    assert conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == n  # no 澄枝 card
    assert "澄枝" not in json.loads(_card(conn, s["mother"])["aliases_json"])
    ent = {r[0]: r[1] for r in conn.execute("SELECT source_term, entity_id FROM glossary WHERE source_term IN "
                                            "('母親','澄枝')")}
    assert ent.get("澄枝") != ent["母親"] and character_for_form(conn, book.id, "澄枝") is None
    reasons = [r[0] for r in conn.execute("SELECT reason FROM memory_rejections WHERE stage='memory_update'")]
    assert any("another time" in r for r in reasons)


def test_a_name_link_with_the_card_it_is_about_goes_to_that_card(conn, cfg, fixtures):
    s, past = _sumie(conn, cfg, fixtures)
    book, s1, intro = s["book"], s["scenes"][1], s["intro"]
    stats = _memory(conn, book, s1, glossary_candidates=[_gloss("澄枝", intro, past)],
                    name_links=[_link("澄枝", "母親", intro, past)])
    assert "澄枝" in json.loads(_card(conn, past)["aliases_json"])
    assert "澄枝" not in json.loads(_card(conn, s["mother"])["aliases_json"])
    ent = {r[0]: r[1] for r in conn.execute("SELECT source_term, entity_id FROM glossary WHERE source_term IN "
                                            "('母親','澄枝')")}
    assert ent["澄枝"] != ent["母親"]
    assert stats["rejected"] == 0


def test_a_glossary_name_already_a_form_of_a_card_is_that_card_and_the_placeholder_is_never_a_name(conn, cfg,
                                                                                                    fixtures):
    s = _past_mother(conn, cfg, fixtures)
    book, s1, intro = s["book"], s["scenes"][1], s["intro"]
    conn.execute("UPDATE units SET plain_text='　汐乃は窓の外を見た。' WHERE id=?", (intro["id"],))
    narrator = new_narrator(conn, book.id, intro)
    conn.execute("UPDATE characters SET aliases_json=? WHERE id=?", (json.dumps(["汐乃"]), narrator))
    n = conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
    _memory(conn, book, s1, glossary_candidates=[_gloss("汐乃", intro)])
    assert conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == n  # no stray 汐乃 card
    named, _ = repo.upsert_character(conn, book.id, "リセナ", name_zh=None, aliases=[], unit_id=intro["id"],
                                     ordinal=intro["ordinal"], origin="test")
    repo.merge_characters(conn, named, narrator)
    aliases = json.loads(_card(conn, named)["aliases_json"])
    assert "汐乃" in aliases and not any(a.startswith("（敘述者") for a in aliases)


def test_a_chapter_heading_stays_with_the_scene_after_it_when_the_model_cuts_right_after_it():
    def row(i, text, kind="text"):
        return {"id": f"u{i}", "ordinal": i, "resource_href": "a.xhtml", "is_separator": 0, "kind": kind,
                "blank_before": 0, "spine_index": 1, "plain_text": text}

    rows = [row(0, "第二章", "heading"), row(1, "　地の文。"), row(2, "　地の文。"), row(3, "　次の場面。")]
    scenes = segment_units(rows, {}, 80, {"u1": "llm", "u3": "llm"}, chapters={"u0": True})
    assert [s.unit_ids for s in scenes] == [["u0", "u1", "u2"], ["u3"]]


def test_a_scene_of_headings_only_is_not_sent_to_be_read(conn, cfg, fixtures):
    from ncte.llm.fake import FakeLLM

    from ..helpers import orchestrator_for, start_run
    from ..scripted import RESPONDERS

    fake = FakeLLM(responders=RESPONDERS)
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], fake, mode="context")
    heading = conn.execute("SELECT * FROM units WHERE book_id=? AND kind='heading' ORDER BY ordinal LIMIT 1 OFFSET 1",
                           (book.id,)).fetchone()
    after = conn.execute("SELECT id FROM units WHERE book_id=? AND ordinal>? ORDER BY ordinal LIMIT 1",
                         (book.id, heading["ordinal"])).fetchone()[0]
    conn.execute("INSERT INTO scene_overrides(book_id, unit_id, action, created_at) VALUES (?,?,?,?)",
                 (book.id, after, "split", "2026-10-23T00:00:00Z"))
    orchestrator_for(conn, cfg, book, rid, opts, fake).run()
    alone = {r[0] for r in conn.execute("SELECT s.id FROM scenes s WHERE s.book_id=? AND NOT EXISTS (SELECT 1 FROM "
                                        "units u WHERE u.scene_id=s.id AND u.kind='text')", (book.id,))}
    assert alone
    read = {p.get("scene_id") for p, c in zip(fake.payloads, fake.calls, strict=True)
            if c.stage in ("scene_analyze", "memory_update", "scene_read")}
    assert not alone & read


def test_a_one_character_form_of_address_has_its_own_reason(conn, cfg, fixtures):
    s = _past_mother(conn, cfg, fixtures)
    book, s1 = s["book"], s["scenes"][1]
    _memory(conn, book, s1, address_terms=[{"speaker_ref": s["mother"], "addressee_ref": s["mother"], "term": "母",
                                            "term_zh": "媽", "note": "", "evidence_unit_ids": [s["intro"]["id"]],
                                            "evidence_quote": "母親が立って"}])
    reasons = [r[0] for r in conn.execute("SELECT reason FROM memory_rejections WHERE stage='memory_update'")]
    assert any("single character" in r for r in reasons)
    assert not any("not found verbatim" in r for r in reasons)


def _late_link(conn, book, term, target, at):
    from ncte.memory.entities import record_link

    record_link(conn, book.id, term, target, "alias", origin="test", status="accepted", unit_id=None, quote=None,
                ordinal=at)


def test_a_name_revealed_later_does_not_hide_the_card_before_it(conn, cfg, fixtures):
    """The narrator is 汐乃, revealed only at the end: before that the card is still there, as 「（敘述者）」, without
    the name; the same for any card whose other name is revealed later."""
    from ncte.analysis.scene import build_analysis_payload
    from ncte.memory import visibility as vis

    s = _past_mother(conn, cfg, fixtures)
    book, intro, last = s["book"], s["intro"], s["scenes"][-1]
    narrator = new_narrator(conn, book.id, intro)
    conn.execute("UPDATE characters SET aliases_json=? WHERE id=?", (json.dumps(["汐乃"]), narrator))
    repo.note_forms(conn, narrator, ["汐乃"], book.id, intro["ordinal"])
    _late_link(conn, book, "汐乃", "（敘述者）", last["start_ordinal"])
    view = vis.character_view(conn, book.id, _card(conn, narrator), s["scenes"][1]["end_ordinal"])
    assert view is not None and view["name_ja"] == "（敘述者）" and "汐乃" not in view["aliases_ja"]
    payload = build_analysis_payload(conn, book.id, get_scene(conn, book.id, s["scenes"][2]["id"]), [])
    assert narrator in {c["character_id"] for c in payload["known_characters"]}
    assert vis.character_view(conn, book.id, _card(conn, narrator), last["start_ordinal"])["aliases_ja"] == ["汐乃"]
    woman, _ = repo.upsert_character(conn, book.id, "白衣の女", name_zh=None, aliases=["ユナ"], unit_id=intro["id"],
                                     ordinal=intro["ordinal"], origin="test")
    _late_link(conn, book, "ユナ", "白衣の女", last["start_ordinal"])
    early = vis.character_view(conn, book.id, _card(conn, woman), intro["ordinal"])
    assert early == {"name_ja": "白衣の女", "name_zh": None, "aliases_ja": []}


def _question(conn, s, who_else):
    """The read pass asked who says the scene's line; the scene has the speaker it gave and someone else present."""
    from ncte.analysis import verify

    book, s1 = s["book"], s["scenes"][1]
    state = json.loads(get_scene(conn, book.id, s1["id"])["state_json"])
    state["uncertainties"] = [{"unit_ids": [s["line"]["id"]], "question": "誰がこの台詞を言ったのか"}]
    state["participants"] = [*state.get("participants", []),
                             {"character_id": who_else, "presence": "explicit", "evidence_unit_ids": []}]
    conn.execute("UPDATE scenes SET state_json=? WHERE id=?", (json.dumps(state, ensure_ascii=False), s1["id"]))
    items = verify.collect(conn, book.id)
    return items, next(i for i in items if i["kind"] == "question")


def test_an_answer_to_the_reading_s_own_question_about_a_line_sets_its_speaker(conn, cfg, fixtures):
    from ncte.analysis import verify
    from ncte.llm.schemas import VerifyOutput

    from .test_unconfirmed_cards import _setup, _speaker

    s = _setup(conn, cfg, fixtures)
    items, item = _question(conn, s, s["a"])
    utt = conn.execute("SELECT id FROM utterances WHERE unit_id=?", (s["line"]["id"],)).fetchone()[0]
    assert [x["line_ref"] for x in item["lines"]] == [utt] and s["a"] in item["candidates"]
    parsed = VerifyOutput.model_validate({"items": [{"ref": item["ref"], "answer": "unknown", "reason": "x"}]})
    verify.apply(conn, s["book"].id, items, parsed, lambda *a: None)
    assert _speaker(conn, s["line"]["id"]) == s["p"]  # unknown: the line keeps its speaker
    conn.execute("DELETE FROM verify_answers")  # (asked again, as after a re-read)
    items, item = _question(conn, s, s["a"])
    parsed = VerifyOutput.model_validate({"items": [{"ref": item["ref"], "answer": "母親が言った", "reason": "x",
                                                     "character_id": s["a"], "line_ref": utt, "line_role": "speaker"}]})
    stats = verify.apply(conn, s["book"].id, items, parsed, lambda *a: None)
    row = conn.execute("SELECT * FROM utterances WHERE id=?", (utt,)).fetchone()
    assert (row["speaker_id_nullable"], row["status"], row["resolution_source"]) == (s["a"], "inferred", "verify")
    assert stats["speakers_changed"] == 1


def test_a_speaker_re_check_may_choose_anyone_in_the_scene(conn, cfg, fixtures):
    from ncte.analysis import verify

    from .test_unconfirmed_cards import _setup

    s = _setup(conn, cfg, fixtures)
    conn.execute("UPDATE utterances SET status='inferred', notes_json=? WHERE unit_id=?",
                 (json.dumps(["(to look at)"]), s["line"]["id"]))
    _, _ = _question(conn, s, s["a"])
    item = next(i for i in verify.collect(conn, s["book"].id) if i["kind"] == "speaker")
    assert s["a"] in item["candidates"] and s["p"] in item["candidates"]
