from ncte.llm.schemas import UtteranceAnalysis


def test_a_line_answered_with_a_null_tag_has_no_tag():
    u = UtteranceAnalysis.model_validate({"unit_id": "u1", "qi": 0, "candidates": [], "status": "inferred",
                                          "speaker": None, "conflicts": [], "tag": None})
    assert u.speech_tag == ""
