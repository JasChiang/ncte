"""Quote structure, speech profiles, and who says a line: the model's reading with its evidence checked (ADR-059)."""

from ncte.analysis.dialogue import UnitText, find_quotes, speech_profile
from ncte.analysis.speaker import TO_LOOK_AT, Candidate, LlmClaim, Resolution, resolve


def mk(*lines):
    return [UnitText(f"u{i}", t, i) for i, t in enumerate(lines)]


def claim(ref, status, tag="", types=("dialogue_tag",)):
    return LlmClaim(ref, status, [Candidate(ref, set(types), ["u0"], "llm")] if ref else [], tag=tag)


def names_ok(names):
    """The program's check: the quoted words name the speaker (and, in scene analysis, are next to the line)."""
    return lambda tag, cid: any(n in tag for n in names.get(cid, []))


NAMES = {"c_mina": ["ミナ"], "c_kaito": ["カイト"]}


def test_quotes_nested_and_multiple():
    us = mk("「『銀の鍵』のこと？」とミナは言った。「知らないわ」")
    qs = find_quotes(us)
    assert [q.text for q in qs] == ["「『銀の鍵』のこと？」", "「知らないわ」"]


def test_multi_paragraph_quotes_both_conventions():
    qs = find_quotes(mk("「一つ目。", "　二つ目」"))
    assert qs[1].continuation_of == ("u0", 0) and not qs[1].opens
    qs = find_quotes(mk("「一つ目。", "「二つ目」"))
    assert qs[1].continuation_of == ("u0", 0) and qs[1].opens
    # an unmarked paragraph after an unclosed quote is the middle of a multi-paragraph speech
    qs = find_quotes(mk("「一つ目。", "二つ目。", "　三つ目」", "「別の発言」"))
    assert [q.continuation_of for q in qs] == [None, ("u0", 0), ("u1", 0), None]


def test_a_stated_speaker_needs_words_that_name_them():
    """「とミナは言った」 given as the evidence: explicit. No words, or words naming someone else: the model's answer is
    kept, as an inference — the program never makes or unmakes the speaker, it checks the evidence."""
    ok = names_ok(NAMES)
    assert (resolve(claim("c_mina", "explicit", "とミナは言った"), None, ok).status) == "explicit"
    r = resolve(claim("c_mina", "explicit", ""), None, ok)
    assert (r.speaker_id, r.status) == ("c_mina", "inferred")
    r = resolve(claim("c_mina", "explicit", "とカイトは言った"), None, ok)
    assert (r.speaker_id, r.status) == ("c_mina", "inferred")


def test_the_model_answer_stands_and_no_answer_is_unknown():
    assert (resolve(claim("c_kaito", "inferred"), None).speaker_id, resolve(claim("c_kaito", "inferred"), None).status) == (
        "c_kaito", "inferred")
    r = resolve(claim(None, "unknown"), None)
    assert (r.speaker_id, r.status) == (None, "unknown")
    assert resolve(None, None).status == "unknown"


def test_a_continuation_keeps_its_speech_unless_the_model_reads_another():
    head = Resolution("c_mina", "explicit", "llm")
    r = resolve(claim(None, "unknown"), head)
    assert (r.speaker_id, r.status, r.source) == ("c_mina", "explicit", "continuity")
    r = resolve(claim("c_kaito", "inferred"), head)
    assert (r.speaker_id, r.status) == ("c_kaito", "inferred") and any(TO_LOOK_AT in n for n in r.notes)


def test_speech_profile():
    prof = speech_profile(["「私は行きます」", "「あなたも来てください」", "「私、知りませんでした」"])
    assert prof["first_person"] == ["私"] and prof["formality"] == "usually_polite"
    assert speech_profile(["「俺は知らねえよ」"])["formality"] == "casual"


def test_a_paragraph_that_is_one_double_bracket_is_a_voice():
    from ncte.analysis.dialogue import UnitText, find_quotes

    us = [UnitText("u1", "『お前は何故、俺を恐れない？』", 1), UnitText("u2", "『帰ったか』と、ルカの脳裏に声が届いた。", 2),
          UnitText("u3", "『私』の世界は、ゆっくりと黒に塗りつぶされた。", 3), UnitText("u4", "「『光在れ』って言ったの」", 4),
          UnitText("u5", "『この苦しみを早く終わらせて』──と。", 5)]
    got = {q.unit_id: q.text for q in find_quotes(us)}
    assert got["u1"] == "『お前は何故、俺を恐れない？』" and got["u2"] == "『帰ったか』" and got["u5"].startswith("『この")
    assert "u3" not in got  # emphasis inside narration
    assert got["u4"].startswith("「") and len([q for q in find_quotes(us) if q.unit_id == "u4"]) == 1


def test_an_action_says_how_sure_who_does_it_is():
    """「ミナを押した」: ミナ is the one pushed; with no one named as doing it, the action's status is unknown even if
    the model was sure of the object — the translator must not read a known subject into it."""
    from ncte.analysis.reference_resolution import validate_actions
    from ncte.llm.schemas import ActionAnalysis

    a = ActionAnalysis(unit_id="u0", summary="推了米娜", subject_candidates=[], object_candidates=["c_mina"],
                       resolution_status="explicit")
    kept, _ = validate_actions([a], {"u0": "　ミナを押した。"}, {"c_mina": "c_mina"}, {"c_mina": ["ミナ"]})
    assert kept[0]["status"] == "unknown" and kept[0]["object_candidates"] == ["c_mina"]


def test_the_rest_of_a_speech_whose_start_has_no_speaker_keeps_the_models_answer():
    head = Resolution(None, "unknown", "llm")
    r = resolve(claim("c_mina", "inferred"), head)
    assert (r.speaker_id, r.status) == ("c_mina", "inferred") and any(TO_LOOK_AT in n for n in r.notes)
