"""ADR-059: a stated speaker needs words of the text that state it, next to the line — checked by position, not read.
Regression cases from the review of the branch (first-person narrators, speeches over several paragraphs)."""

from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from .test_fix_check import small_book


def _read(conn, cfg, tmp_path, lines, answer):
    def analyze(payload, req):
        units = {u["source"]: u["unit_id"] for u in payload["units"]}
        q = {(x["unit_id"], x["quote_index"]): x for x in payload["quotes"]}
        utts, new, narration = answer(units, q)
        return {"pov": None, "location": None, "time": None, "participants": [], "actions": [], "props": [],
                "uncertainties": [], "utterances": utts, "new_characters": new,
                **({"narration": narration} if narration else {})}

    book, _, _ = run_book(conn, cfg, small_book(tmp_path, lines), FakeLLM(responders={"scene_analyze": analyze}),
                          mode="context")
    return {r["quote_text"]: r["status"] for r in conn.execute(
        "SELECT quote_text, status FROM utterances WHERE book_id=?", (book.id,))}


def _said(k, speaker, tag):
    return {"unit_id": k[0], "qi": k[1], "candidates": [], "status": "explicit", "speaker": speaker, "conflicts": [],
            "tag": tag}


def _new(t, name, unit):
    return {"temp_id": t, "name_ja": name, "aliases_ja": [], "proposed_name_zh": name, "evidence_unit_ids": [unit]}


LINES = ["　俺の名前はレドだ。今日も学院に向かう。", "「行くぞ」と俺は言った。", "「待ってよ」とミナが言った。"]


def _first_person(person):
    def answer(units, q):
        k1 = next(k for k in q if k[0] == units[LINES[1]])
        k2 = next(k for k in q if k[0] == units[LINES[2]])
        narration = {"person": person, "narrator_ref": "new_1" if person == "first" else None,
                     "focal_ref": "new_1" if person == "third" else None, "evidence_unit_ids": [units[LINES[0]]],
                     "self_forms": [{"ja": "俺", "zh": "我"}] if person == "first" else []}
        return ([_said(k1, "new_1", "と俺は言った"), _said(k2, "new_2", "とミナが言った")],
                [_new("new_1", "レド", units[LINES[0]]), _new("new_2", "ミナ", units[LINES[2]])], narration)
    return answer


def test_a_first_person_narrator_states_their_own_line(conn, cfg, tmp_path):
    """「行くぞ」と俺は言った in a scene the model reads as first person, narrated by レド: 「俺」 is the narrator's own
    word for himself (the model recorded it), so the line is stated."""
    got = _read(conn, cfg, tmp_path, LINES, _first_person("first"))
    assert got["「行くぞ」"] == "explicit" and got["「待ってよ」"] == "explicit"


def test_the_narrators_pronoun_names_no_one_in_a_third_person_scene(conn, cfg, tmp_path):
    """The same words in a scene read as third person: 「俺」 is not anyone's name there; the answer is an inference."""
    got = _read(conn, cfg, tmp_path, LINES, _first_person("third"))
    assert got["「行くぞ」"] == "inferred" and got["「待ってよ」"] == "explicit"


def test_a_speech_over_paragraphs_is_stated_where_it_ends(conn, cfg, tmp_path):
    """「わたしは…／…決めたわ」とミナは静かに言った: the words after the speech's last paragraph state its first line."""
    lines = ["　ミナが前に出た。", "「わたしは、ずっと考えていたの。", "　だから、もう決めたわ」とミナは静かに言った。"]

    def answer(units, q):
        k1 = next(k for k in q if k[0] == units[lines[1]])
        return [_said(k1, "new_1", "とミナは静かに言った")], [_new("new_1", "ミナ", units[lines[0]])], None

    got = _read(conn, cfg, tmp_path, lines, answer)
    assert got["「わたしは、ずっと考えていたの。"] == "explicit"
    assert all(st == "explicit" for st in got.values())  # the rest of the speech follows its first line


def test_a_line_cannot_borrow_the_tag_of_another_line_in_its_paragraph(conn, cfg, tmp_path):
    """「おはよう」とミナが言い、「おう」とレドが答えた: 「とミナが言い」 belongs to the first line, not the second."""
    lines = ["　朝の教室。", "「おはよう」とミナが言い、「おう」とレドが答えた。"]

    def answer(units, q):
        ks = sorted(k for k in q if k[0] == units[lines[1]])
        return ([_said(ks[0], "new_1", "とミナが言い"), _said(ks[1], "new_1", "とミナが言い")],
                [_new("new_1", "ミナ", units[lines[1]])], None)

    got = _read(conn, cfg, tmp_path, lines, answer)
    assert got["「おはよう」"] == "explicit" and got["「おう」"] == "inferred"


def test_a_longer_name_does_not_name_someone_shorter(conn, cfg, tmp_path):
    """「…」とミナミが言った does not state that ミナ said it (ミナミ is someone else)."""
    lines = ["　ミナとミナミがいた。", "「行こう」とミナミが言った。"]

    def answer(units, q):
        k = next(k for k in q if k[0] == units[lines[1]])
        return ([_said(k, "new_1", "とミナミが言った")],
                [_new("new_1", "ミナ", units[lines[0]]), _new("new_2", "ミナミ", units[lines[0]])], None)

    assert _read(conn, cfg, tmp_path, lines, answer)["「行こう」"] == "inferred"
