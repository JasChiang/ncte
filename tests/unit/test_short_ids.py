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
