from ncte.analysis.split_read import merge_outputs, subset_payload
from ncte.llm.schemas import MemoryUpdateOutput, SceneAnalysisOutput


def _scene(chars, utts, narration=None, pov=None):
    return SceneAnalysisOutput.model_validate({
        "pov": pov, "location": None, "time": None, "participants": [], "actions": [], "props": [],
        "uncertainties": [], "narration": narration,
        "new_characters": [{"temp_id": t, "name_ja": n, "aliases_ja": [], "proposed_name_zh": n,
                            "evidence_unit_ids": [e]} for t, n, e in chars],
        "utterances": utts})


def test_parts_merge_and_new_characters_stay_distinct():
    utt = lambda u, ref: {"unit_id": u, "quote_index": 0, "speaker_ref": ref,  # noqa: E731
                          "resolution_status": "explicit", "speaker_candidates": [], "contradictions": []}
    a = _scene([("n1", "ミツ", "u1")], [utt("u1", "n1")], pov="高木")
    b = _scene([("n1", "姉", "u5"), ("n2", "ミツ", "u6")], [utt("u5", "n1"), utt("u6", "n2")],
               narration={"person": "first", "narrator_ref": None, "focal_ref": None})
    m = merge_outputs([a, b])
    assert [c.name_ja for c in m.new_characters] == ["ミツ", "姉"]
    assert m.new_characters[0].evidence_unit_ids == ["u1", "u6"]
    assert [u.speaker_ref for u in m.utterances] == ["n1", "n1~1", "n1"]  # 姉 renamed, second ミツ → first
    assert m.pov == "高木" and m.narration and m.narration.person == "first"


def test_memory_parts_join_summaries_and_facts():
    def mem(summary):
        return MemoryUpdateOutput.model_validate({"scene_summary": summary, "facts": [], "glossary_candidates": [],
                                                  "prop_states": []})
    assert merge_outputs([mem("前半"), mem("後半")]).scene_summary == "前半\n後半"


def test_subset_keeps_only_those_units_quotes_and_utterances():
    p = {"units": [{"unit_id": "a"}, {"unit_id": "b"}], "quotes": [{"unit_id": "b", "quote_index": 0}],
         "scene": {"utterances": [{"unit_id": "a"}], "pov": "x"}, "glossary": [1]}
    s = subset_payload(p, ["b"])
    assert s["units"] == [{"unit_id": "b"}] and s["quotes"] == p["quotes"]
    assert s["scene"] == {"utterances": [], "pov": "x"} and s["glossary"] == [1]


def test_several_lists_are_split_without_asking_anything_twice():
    from ncte.analysis.split_read import call_in_parts
    from ncte.errors import ContentBlockedError
    from ncte.llm.schemas import NamingOutput

    asked: list[list[str]] = []

    def call(pl):
        refs = [x["ref"] for k in ("names", "address") for x in pl[k]]
        if "bad" in refs and len(refs) > 1 or refs == ["bad"]:
            raise ContentBlockedError("refused")
        asked.append(refs)
        return NamingOutput(names=[{"ref": r, "zh": r, "reason": ""} for r in refs])

    blocked: list[str] = []
    out = call_in_parts(call, {"names": [{"ref": "n1"}, {"ref": "bad"}, {"ref": "n3"}, {"ref": "n4"}],
                               "address": [{"ref": "a1"}]}, ("names", "address"), blocked, min_part=1)
    flat = [r for a in asked for r in a]
    assert sorted(flat) == ["a1", "n1", "n3", "n4"] and len(flat) == len(set(flat))  # each asked once
    assert blocked == ["bad"] and len(out.names) == 4


def test_same_word_another_person_or_time_is_not_merged_across_parts():
    """「母親」 of the present in one part, 「母親」 of a past life (another person) in the next, 「母親」 of that past
    life again in a third: the first two stay apart (other_person and period kept); the last two are one card."""
    def scene(chars):
        s = _scene([(t, n, e) for t, n, e, *_ in chars], [])
        for c, (_, _, _, other, period) in zip(s.new_characters, chars, strict=True):
            c.other_person, c.period = other, period
        return s

    now = scene([("n1", "母親", "u1", False, "")])
    past = scene([("n1", "母親", "u5", True, "前世")])
    past_again = scene([("n1", "母親", "u9", True, "前世")])
    m = merge_outputs([now, past, past_again])
    kept = [(c.name_ja, c.other_person, c.period) for c in m.new_characters]
    assert kept == [("母親", False, ""), ("母親", True, "前世")]
    assert m.new_characters[1].evidence_unit_ids == ["u5", "u9"]  # the past-life mother, met twice
