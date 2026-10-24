from ncte.llm.short_ids import ShortIds

U, C, F, Q = "u_a0ee6359b742dd15", "c_5cda59a1ea27", "f_0123456789ab", "q_a0ee6359b742dd15_0"


def test_ids_become_short_and_come_back_only_where_ids_belong():
    payload = {"units": [{"unit_id": U, "source": "P1グランプリ"}], "known_characters": [{"character_id": C}],
               "facts": [{"id": F}], "quotes": [{"utterance": Q}], "scene_id": "s_008_02"}
    ids = ShortIds.for_payload(payload)
    sent = ids.encode(payload)
    assert sent["units"][0] == {"unit_id": "P1", "source": "P1グランプリ"}  # text is never encoded
    assert sent["known_characters"][0]["character_id"] == "C1" and sent["facts"][0]["id"] == "F1"
    assert sent["quotes"][0]["utterance"] == "Q1" and sent["scene_id"] == "s_008_02"
    answer = {"translations": [{"unit_id": "P1", "translated_text": "P1大獎賽"}],
              "utterances": [{"unit_id": "P1", "speaker_ref": "C1", "evidence_unit_ids": ["P1"],
                              "speaker_candidates": [{"character_ref": "C1", "note": "C1 在說話"}]}],
              "facts": [{"subject_ref": "C1", "object": "C1", "retracts_fact_ids": ["F1"]}]}
    back = ids.decode(answer)
    assert back["translations"][0] == {"unit_id": U, "translated_text": "P1大獎賽"}  # a translation is never touched
    u = back["utterances"][0]
    assert u["unit_id"] == U and u["speaker_ref"] == C and u["evidence_unit_ids"] == [U]
    assert u["speaker_candidates"][0] == {"character_ref": C, "note": "C1 在說話"}
    assert back["facts"][0] == {"subject_ref": C, "object": C, "retracts_fact_ids": [F]}


def test_unknown_codes_and_real_ids_pass_through():
    ids = ShortIds.for_payload({"unit_id": U})
    assert ids.decode({"unit_id": "P9"}) == {"unit_id": "P9"}  # left for the program's checks to reject
    assert ids.decode({"unit_id": U}) == {"unit_id": U}
    assert ShortIds.for_payload({"x": "plain"}).encode({"x": "plain"}) == {"x": "plain"}


def test_line_analysis_with_short_keys_and_short_ids():
    from ncte.llm.schemas import UtteranceAnalysis

    ids = ShortIds.for_payload({"units": [{"unit_id": U}], "known_characters": [{"character_id": C}]})
    answer = {"unit_id": "P1", "qi": 0, "candidates": [{"ref": "C1", "types": ["action"], "evidence": ["P1"], "note": "動作"}],
              "status": "inferred", "speaker": "C1", "conflicts": []}
    u = UtteranceAnalysis.model_validate(ids.decode(answer))
    assert u.unit_id == U and u.speaker_ref == C and u.speaker_candidates[0].character_ref == C
    assert u.speaker_candidates[0].evidence_unit_ids == [U] and u.quote_index == 0


def test_paragraphs_are_numbered_in_reading_order():
    """A scene's utterances (sent before ``units``) mention a paragraph first; the paragraphs still read P1, P2, P3…"""
    u = [f"u_{i:016x}" for i in range(1, 7)]
    payload = {"scene": {"utterances": [{"unit_id": u[4]}]},  # mentions the 5th paragraph first
               "context_before": [{"unit_id": u[0]}, {"unit_id": u[1]}],
               "units": [{"unit_id": u[2]}, {"unit_id": u[3]}, {"unit_id": u[4]}],
               "context_after": [{"unit_id": u[5]}]}
    ids = ShortIds.for_payload(payload)
    assert [ids.to_short[x] for x in u] == ["P1", "P2", "P3", "P4", "P5", "P6"]


def test_codes_the_model_writes_in_its_own_words_become_names():
    """「C1 前世的兵服是黑色」 kept as written would point at whoever is C1 in the next request; whose view a fact is
    (source_viewpoint) is an id like the others."""
    C2 = "c_00000000000b"
    ids = ShortIds.for_payload({"known_characters": [{"character_id": C, "name_ja": "レイド", "name_zh": "雷德"},
                                                     {"character_id": C2, "name_ja": "エルリア"}]})
    back = ids.decode({"facts": [{"subject_ref": "C1", "predicate": "C2 的兵服", "object": "C1 黑色，C2 白色",
                                  "source_viewpoint": "C1"}],
                       "translations": [{"unit_id": "P1", "translated_text": "C1 型戰車"}]})
    f = back["facts"][0]
    assert f["subject_ref"] == C and f["source_viewpoint"] == C
    assert f["predicate"] == "エルリア 的兵服" and f["object"] == "雷德 黑色，エルリア 白色"
    assert back["translations"][0]["translated_text"] == "C1 型戰車"  # a translation is never touched
    assert ids.decode({"object": "C12 與 AC1"}) == {"object": "C12 與 AC1"}  # unknown code, part of a word


def test_short_keys_are_read_as_the_fields_they_stand_for():
    """ADR-106: a situation point's 「"at": "P1"」 is ``unit_id`` and comes back as the paragraph's id (it stayed
    「P1」 and every point was dropped as outside the scene); a line's 「"conflicts"」 is prose about people."""
    from ncte.llm.schemas import SceneAnalysisOutput

    payload = {"units": [{"unit_id": U}], "known_characters": [{"character_id": C, "name": "ミナ"}],
               "write_situation": True}
    ids = ShortIds.for_payload(payload)
    assert ids.encode(payload)["units"][0]["unit_id"] == "P1"
    answer = {"pov": None, "location": None, "time": None, "new_characters": [], "participants": [],
              "situation": [{"at": "P1", "people": [{"ref": "C1", "doing": "坐著"}]}],
              "utterances": [{"unit_id": "P1", "qi": 0, "candidates": [{"ref": "C1", "types": [], "evidence": ["P1"],
                                                                         "note": ""}],
                              "status": "inferred", "speaker": "C1", "conflicts": ["C1 不在場"], "to": ["C1"]}],
              "actions": [], "props": [], "uncertainties": []}
    back = SceneAnalysisOutput.model_validate(ids.decode(answer), context={"payload": payload})
    assert back.situation[0].unit_id == U and back.situation[0].people[0].ref == C
    ua = back.utterances[0]
    assert ua.unit_id == U and ua.speaker_ref == C and ua.listener_refs == [C]
    assert ua.speaker_candidates[0].character_ref == C and ua.speaker_candidates[0].evidence_unit_ids == [U]
    assert ua.contradictions == ["ミナ 不在場"]
