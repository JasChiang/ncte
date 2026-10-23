"""Every check that a string the model gives occurs in the source compares it as the source has it (``in_source``): a
name, term or quote written with the reading it was shown (強敵《とも》, 蒼《あお》) is the text's 強敵, 蒼, and is kept
without the reading; words not in the text are still rejected; the author's own 《》 are text (ADR-052)."""

import json
from typing import Any

import pytest

from ncte.analysis import terms
from ncte.analysis.memory_update import apply_memory_update
from ncte.analysis.scene import apply_scene_analysis
from ncte.analysis.split_read import merge_outputs
from ncte.epub.units import in_source, source_form
from ncte.library import import_book
from ncte.llm.schemas import MemoryUpdateOutput, SceneAnalysisOutput, TermCandidatesOutput
from ncte.memory import repository as repo

SHOWN = {"name": "蒼《あお》", "term": "強敵《とも》", "quote": "強敵《とも》と呼べる相手", "tag": "彼の名は蒼《あお》",
         "self": "俺《おれ》"}
ABSENT = {"name": "翠《みどり》", "term": "宿敵《とも》", "quote": "宿敵《とも》と呼べる相手", "tag": "彼の名は翠《みどり》",
          "self": "僕《ぼく》"}


@pytest.fixture
def book(conn, cfg, fixtures):
    b, _ = import_book(conn, cfg, fixtures["ruby"])
    unit = conn.execute("SELECT * FROM units WHERE book_id=? AND plain_text LIKE '%と呼べる相手%'", (b.id,)).fetchone()
    scene = conn.execute("SELECT * FROM scenes WHERE book_id=? AND id=?", (b.id, unit["scene_id"])).fetchone()
    ids = {k: conn.execute("SELECT id FROM units WHERE book_id=? AND plain_text LIKE ?", (b.id, f"%{k}%")).fetchone()[0]
           for k in ("と呼べる相手", "彼の名は", "約束")}
    return conn, b.id, scene, ids


def _scene(conn, book_id, scene, **parts: Any) -> None:
    out = {"pov": None, "location": None, "time": None, "new_characters": [], "participants": [], "utterances": [],
           "actions": [], "props": [], "uncertainties": [], **parts}
    apply_scene_analysis(conn, book_id, scene, SceneAnalysisOutput.model_validate(out))


def _memory(conn, book_id, scene, **parts: Any) -> None:
    out = {"scene_summary": "", "facts": [], "glossary_candidates": [], "prop_states": [], **parts}
    apply_memory_update(conn, book_id, scene, MemoryUpdateOutput.model_validate(out))


def _two(conn, book_id, ids) -> tuple[str, str]:
    a, _ = repo.upsert_character(conn, book_id, "甲", name_zh="甲", aliases=[], unit_id=ids["と呼べる相手"], ordinal=0,
                                 origin="test")
    b, _ = repo.upsert_character(conn, book_id, "乙", name_zh="乙", aliases=[], unit_id=ids["と呼べる相手"], ordinal=0,
                                 origin="test")
    return a, b


def _one(conn, sql: str, *args: Any) -> Any:
    row = conn.execute(sql, args).fetchone()
    return row[0] if row else None


def new_character(conn, book_id, scene, ids, v):
    _scene(conn, book_id, scene, new_characters=[{"temp_id": "new_1", "name_ja": v["term"], "aliases_ja": [],
                                                   "proposed_name_zh": "勁敵", "evidence_unit_ids": [ids["と呼べる相手"]]}])
    return _one(conn, "SELECT canonical_name FROM characters WHERE book_id=?", book_id)


def alias(conn, book_id, scene, ids, v):
    _scene(conn, book_id, scene, new_characters=[{"temp_id": "new_1", "name_ja": SHOWN["name"], "aliases_ja": [v["term"]],
                                                   "proposed_name_zh": "蒼", "evidence_unit_ids": [ids["彼の名は"]]}])
    return json.loads(_one(conn, "SELECT aliases_json FROM characters WHERE book_id=?", book_id) or "[]")


def other_form(conn, book_id, scene, ids, v):
    _scene(conn, book_id, scene,
           new_characters=[{"temp_id": "new_1", "name_ja": SHOWN["name"], "aliases_ja": [], "proposed_name_zh": "蒼",
                            "evidence_unit_ids": [ids["彼の名は"]]}],
           other_forms=[{"ref": "new_1", "ja": v["term"], "kind": "nickname", "evidence_unit_ids": [ids["と呼べる相手"]]}])
    return json.loads(_one(conn, "SELECT aliases_json FROM characters WHERE book_id=?", book_id) or "[]")


def scene_prop(conn, book_id, scene, ids, v):
    _scene(conn, book_id, scene, props=[{"name_ja": v["term"], "holder_ref": None, "evidence_unit_ids": []}])
    return [p["name_ja"] for p in json.loads(_one(conn, "SELECT state_json FROM scenes WHERE id=?", scene["id"]))["props"]]


def speech_tag(conn, book_id, scene, ids, v):
    _scene(conn, book_id, scene,
           new_characters=[{"temp_id": "new_1", "name_ja": SHOWN["name"], "aliases_ja": [], "proposed_name_zh": "蒼",
                            "evidence_unit_ids": [ids["彼の名は"]]}],
           utterances=[{"unit_id": ids["約束"], "qi": 0, "candidates": [], "status": "explicit", "speaker": "new_1",
                        "conflicts": [], "tag": v["tag"]}])
    return _one(conn, "SELECT status FROM utterances WHERE unit_id=?", ids["約束"]) == "explicit"


def self_form(conn, book_id, scene, ids, v):
    _scene(conn, book_id, scene, narration={"person": "first", "narrator_ref": "new_narrator",
                                            "self_forms": [{"ja": v["self"], "zh": "我"}]})
    return [f["ja"] for f in json.loads(_one(conn, "SELECT state_json FROM scenes WHERE id=?", scene["id"]))
            ["narration"].get("self_forms", [])]


def fact(conn, book_id, scene, ids, v):
    _memory(conn, book_id, scene, facts=[{
        "subject_ref": "world", "predicate": "有", "object": "勁敵", "fact_type": "objective", "certainty": "explicit",
        "evidence_unit_ids": [ids["と呼べる相手"]], "evidence_quote": v["quote"], "source_viewpoint": None,
        "retracts_fact_ids": []}])
    return _one(conn, "SELECT evidence_quote FROM facts WHERE book_id=?", book_id)


def glossary(conn, book_id, scene, ids, v):
    _memory(conn, book_id, scene, glossary_candidates=[{"source_term": v["term"], "target_term": "勁敵", "category": "term",
                                                        "evidence_unit_ids": [ids["と呼べる相手"]]}])
    return _one(conn, "SELECT source_term FROM glossary WHERE book_id=? AND target_term='勁敵'", book_id)


def name_link(conn, book_id, scene, ids, v):
    _memory(conn, book_id, scene, name_links=[{
        "term": v["term"], "term_zh": "勁敵", "same_as": v["name"], "form_type": "nickname", "note": "",
        "evidence_unit_ids": [ids["と呼べる相手"]], "evidence_quote": v["quote"]}])
    return _one(conn, "SELECT term || '→' || target_term FROM entity_links WHERE book_id=?", book_id)


def address_term(conn, book_id, scene, ids, v):
    a, b = _two(conn, book_id, ids)
    _memory(conn, book_id, scene, address_terms=[{
        "speaker_ref": a, "addressee_ref": b, "term": v["term"], "term_zh": "勁敵", "note": "",
        "evidence_unit_ids": [ids["と呼べる相手"]], "evidence_quote": v["quote"]}])
    return _one(conn, "SELECT form_ja FROM address_terms WHERE book_id=?", book_id)


def wordplay(conn, book_id, scene, ids, v):
    _memory(conn, book_id, scene, wordplay=[{"unit_id": ids["と呼べる相手"], "term": v["term"], "explanation": "讀作とも",
                                             "note_zh": "", "evidence_quote": v["quote"]}])
    return _one(conn, "SELECT term FROM reader_notes WHERE book_id=?", book_id)


def gender(conn, book_id, scene, ids, v):
    a, _ = _two(conn, book_id, ids)
    _memory(conn, book_id, scene, genders=[{"character_ref": a, "gender": "male", "evidence_unit_ids": [ids["と呼べる相手"]],
                                            "evidence_quote": v["quote"]}])
    return _one(conn, "SELECT evidence_quote FROM character_gender WHERE character_id=?", a)


def relation(conn, book_id, scene, ids, v):
    a, b = _two(conn, book_id, ids)
    _memory(conn, book_id, scene, relations=[{"a_ref": a, "b_ref": b, "relation": "勁敵", "a_is": "勁敵", "b_is": "勁敵",
                                              "kind": "feeling", "evidence_unit_ids": [ids["と呼べる相手"]],
                                              "evidence_quote": v["quote"]}])
    return _one(conn, "SELECT evidence_quote FROM relation_notes WHERE book_id=?", book_id)


def group(conn, book_id, scene, ids, v):
    a, b = _two(conn, book_id, ids)
    _memory(conn, book_id, scene, groups=[{"group_ref": a, "member_refs": [b], "evidence_unit_ids": [ids["と呼べる相手"]],
                                           "evidence_quote": v["quote"]}])
    return _one(conn, "SELECT reason FROM group_members WHERE group_id=?", a)


def memory_prop(conn, book_id, scene, ids, v):
    _memory(conn, book_id, scene, prop_states=[{"name_ja": v["term"], "holder_ref": None, "evidence_unit_ids": []}])
    return _one(conn, "SELECT name_ja FROM prop_states WHERE book_id=?", book_id)


def term_candidate(conn, book_id, scene, ids, v, monkeypatch):
    monkeypatch.setattr(terms, "MIN_COUNT", 1)
    parsed = TermCandidatesOutput.model_validate({"candidates": [{"ja": v["term"], "evidence": ["f1"]}]})
    return terms.take_candidates(conn, book_id, parsed, {"_ev": {"f1": 0}})


CASES = [  # (the check, what it keeps of the shown value)
    (new_character, "強敵"), (alias, ["強敵"]), (other_form, ["強敵"]), (scene_prop, ["強敵"]), (speech_tag, True),
    (self_form, ["俺"]), (fact, "強敵と呼べる相手"), (glossary, "強敵"), (name_link, "強敵→蒼"), (address_term, "強敵"),
    (wordplay, "強敵"), (gender, "強敵と呼べる相手"), (relation, "強敵と呼べる相手"), (group, "強敵と呼べる相手"),
    (memory_prop, "強敵"), (term_candidate, ["強敵"]),
]
_REJECTED: dict[Any, Any] = {new_character: None, alias: [], other_form: [], scene_prop: [], speech_tag: False,
                             self_form: [], fact: None, glossary: None, name_link: None, address_term: None,
                             wordplay: None, gender: None, relation: None, group: None, memory_prop: None,
                             term_candidate: []}


@pytest.mark.parametrize(("check", "kept"), CASES, ids=[c.__name__ for c, _ in CASES])
@pytest.mark.parametrize("shown", [True, False], ids=["shown_reading", "not_in_source"])
def test_model_words_are_compared_as_the_source_has_them(book, check, kept, shown, monkeypatch):
    conn, book_id, scene, ids = book
    args = (conn, book_id, scene, ids, SHOWN if shown else ABSENT)
    got = check(*args, monkeypatch) if check is term_candidate else check(*args)
    if shown:
        assert got == kept
    else:
        assert got == _REJECTED[check]


READING = {"name": "あお", "term": "とも", "quote": "ともと呼べる相手"}  # the book's readings, given as they are read
_AS_READ: dict[Any, Any] = {  # what each check keeps of words the book gives as a reading: the words as given
    new_character: "とも", alias: ["とも"], other_form: ["とも"], scene_prop: ["とも"], fact: "ともと呼べる相手",
    glossary: "とも", name_link: "とも→あお", address_term: "とも", wordplay: "とも", gender: "ともと呼べる相手",
    relation: "ともと呼べる相手", group: "ともと呼べる相手", memory_prop: "とも"}


@pytest.mark.parametrize("check", list(_AS_READ), ids=[c.__name__ for c in _AS_READ])
def test_a_reading_is_the_books_words_too(book, check):
    """Words the book gives only as a reading (〝下級精霊《ローエレメンタル》〟) are found in the text read with its readings
    and kept as given (ADR-101)."""
    conn, book_id, scene, ids = book
    assert check(conn, book_id, scene, ids, READING) == _AS_READ[check]


def test_the_same_name_quoted_with_and_without_its_reading_is_one_person_across_parts():
    part = {"pov": None, "location": None, "time": None, "participants": [], "utterances": [], "actions": [],
            "props": [], "uncertainties": []}
    one = SceneAnalysisOutput.model_validate({**part, "new_characters": [
        {"temp_id": "new_1", "name_ja": "強敵《とも》", "aliases_ja": [], "proposed_name_zh": "", "evidence_unit_ids": []}]})
    two = SceneAnalysisOutput.model_validate({**part, "new_characters": [
        {"temp_id": "new_1", "name_ja": "強敵", "aliases_ja": [], "proposed_name_zh": "", "evidence_unit_ids": []}]})
    assert len(merge_outputs([one, two]).new_characters) == 1


def test_the_authors_own_brackets_are_text(book):
    assert source_form("《亡雄の旅団》") == "《亡雄の旅団》"
    assert source_form("《はじまり》の歌") == "《はじまり》の歌"  # a title the author wrote in kana is not a reading
    assert in_source("、《はじまり》の歌", "空に、《はじまり》の歌が")
    assert not in_source("《はじまり》の歌", "空に、の歌が")
    conn, book_id, scene, ids = book
    conn.execute("UPDATE units SET plain_text='　空に、《はじまり》の歌が流れた。' WHERE id=?", (ids["彼の名は"],))
    _memory(conn, book_id, scene, prop_states=[{"name_ja": "《はじまり》の歌", "holder_ref": None, "evidence_unit_ids": []}])
    assert _one(conn, "SELECT name_ja FROM prop_states WHERE book_id=?", book_id) == "《はじまり》の歌"


def test_words_across_the_base_text_and_its_reading_are_not_the_books(book):
    """The base text and the text read with its readings are looked in each alone, a paragraph at a time: nothing is
    found where one ends and the other starts (the scene's base text ends 「青かった。」, read from the start it is
    「第一章　強敵」), nor across two paragraphs (the heading's 「強敵」 and the next paragraph's reading 「とも」)."""
    conn, book_id, scene, ids = book
    assert scene_prop(conn, book_id, scene, ids, {"term": "青かった。第一章"}) == []
    assert scene_prop(conn, book_id, scene, ids, {"term": "強敵とも"}) == []
    assert scene_prop(conn, book_id, scene, ids, {"term": "青かった"}) == ["青かった"]


def test_a_single_character_alias_never_names_an_unnamed_person(book):
    """ADR-101 keeps a person the book does not name under the book's words among the aliases — two characters at
    least, as other forms of a person are: 「空」 alone (no kind given) names anyone."""
    conn, book_id, scene, ids = book
    unit = conn.execute("SELECT id FROM units WHERE book_id=? AND plain_text LIKE '%空は%'", (book_id,)).fetchone()[0]
    _scene(conn, book_id, scene, new_characters=[{"temp_id": "new_1", "name_ja": "空を見ていた誰か", "aliases_ja": ["空"],
                                                   "proposed_name_zh": "某人", "evidence_unit_ids": [unit]}])
    assert _one(conn, "SELECT canonical_name FROM characters WHERE book_id=?", book_id) is None
    assert _one(conn, "SELECT reason FROM memory_rejections WHERE book_id=?", book_id).startswith("name_ja not found")


def test_the_model_label_of_an_unnamed_person_is_kept_as_its_description_only(book):
    """The label the model gave a person the book does not name is its description: kept on the scene's record, never a
    name, an alias or a glossary term, and not sent back to the model."""
    from ncte.analysis.memory_update import build_memory_payload

    conn, book_id, scene, ids = book
    _scene(conn, book_id, scene, new_characters=[{"temp_id": "new_1", "name_ja": "主人公の好敵手", "aliases_ja": ["強敵"],
                                                   "alias_kinds": [{"ja": "強敵", "kind": "description"}],
                                                   "proposed_name_zh": "好對手", "evidence_unit_ids": [ids["と呼べる相手"]]}])
    card = conn.execute("SELECT * FROM characters WHERE book_id=?", (book_id,)).fetchone()
    assert card["canonical_name"] == "強敵" and "主人公の好敵手" not in card["aliases_json"]
    state = json.loads(_one(conn, "SELECT state_json FROM scenes WHERE id=?", scene["id"]))
    assert state["model_labels"] == [{"character_id": card["id"], "label": "主人公の好敵手"}]
    assert _one(conn, "SELECT 1 FROM glossary WHERE book_id=? AND source_term='主人公の好敵手'", book_id) is None
    scene = conn.execute("SELECT * FROM scenes WHERE id=?", (scene["id"],)).fetchone()
    payload = build_memory_payload(conn, book_id, scene, [])
    assert "主人公の好敵手" not in json.dumps(payload, ensure_ascii=False)
