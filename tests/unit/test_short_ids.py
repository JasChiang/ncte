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
