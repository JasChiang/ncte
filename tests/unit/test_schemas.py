from ncte.llm.schemas import UtteranceAnalysis


def test_a_line_answered_with_a_null_tag_has_no_tag():
    u = UtteranceAnalysis.model_validate({"unit_id": "u1", "qi": 0, "candidates": [], "status": "inferred",
                                          "speaker": None, "conflicts": [], "tag": None})
    assert u.speech_tag == ""


def test_a_scene_reading_not_asked_to_picture_the_scene_is_not_offered_situation():
    """ADR-106: with ``scene_state`` off the request has no ``write_situation`` and the schema the model is shown has
    no ``situation`` (Codex, shown the whole schema, filled it unasked); a picture written anyway is not kept."""
    from ncte.llm.schemas import SceneAnalysisOutput, SceneReadOutput, strict_json_schema

    plain = strict_json_schema(SceneReadOutput, {"task": "scene_read"})["properties"]["analysis"]
    assert "situation" not in plain["properties"] and "situation" not in plain["required"]
    assert "utterances" in plain["required"]
    asked = strict_json_schema(SceneReadOutput, {"write_situation": True})["properties"]["analysis"]
    assert "situation" in asked["properties"]
    assert "situation" in strict_json_schema(SceneAnalysisOutput)["properties"]  # no request given: the whole schema
    answer = {"pov": None, "location": None, "time": None, "new_characters": [], "participants": [], "utterances": [],
              "situation": [{"at": "u1", "people": []}], "actions": [], "props": [], "uncertainties": []}
    assert SceneAnalysisOutput.model_validate(answer, context={"payload": {}}).situation == []
    assert len(SceneAnalysisOutput.model_validate(answer, context={"payload": {"write_situation": True}}).situation) == 1
