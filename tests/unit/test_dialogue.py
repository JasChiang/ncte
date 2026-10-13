"""M4: quote detection, dialogue tags, sequence-level speaker resolution, speech profiles."""

from ncte.analysis.dialogue import UnitText, detect_tags, find_quotes, speech_profile, style_hints
from ncte.analysis.speaker import Candidate, LlmClaim, Resolution, resolve

AL = {"ミナ": "c_mina", "カイト": "c_kaito"}


def mk(*lines):
    return [UnitText(f"u{i}", t, i) for i, t in enumerate(lines)]


def ev_for(lines):
    us = mk(*lines)
    qs = find_quotes(us)
    return us, qs, detect_tags(us, qs, AL)


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


def test_explicit_tags():
    _, qs, ev = ev_for(["「行こう」とカイトは言った。"])
    assert [(e.character_id, e.kind) for e in ev[("u0", 0)]] == [("c_kaito", "dialogue_tag")]
    _, qs, ev = ev_for(["「行こう」", "　ミナは静かに答えた。"])
    assert ev[("u0", 0)][0].kind == "dialogue_tag"
    _, qs, ev = ev_for(["ミナが口を開いた。", "「行きましょう」"])
    assert ev[("u1", 0)][0].character_id == "c_mina"


def test_action_is_not_explicit():
    _, qs, ev = ev_for(["「悪い悪い」", "　カイトは頭をかいた。"])
    assert [(e.character_id, e.kind) for e in ev[("u0", 0)]] == [("c_kaito", "action")]


def test_tag_inside_previous_quote_paragraph_is_not_reused():
    # regression found by the M6 evaluation: «「…」とミナは言った。» must not make the next line ミナ's
    _, qs, ev = ev_for(["「カイト、遅いですよ」とミナは言った。", "「悪い、寝坊した」"])
    assert ("u1", 0) not in ev


def test_after_tag_is_not_reused_for_next_quote():
    _, qs, ev = ev_for(["「遅いですよ」", "　ミナは腕を組んで言った。", "「悪い悪い」"])
    assert ("u2", 0) not in ev


def res(program=(), llm=None, head=None):
    return resolve(list(program), llm, head)


def test_resolution_known_speaker():
    _, _, ev = ev_for(["「行こう」とカイトは言った。"])
    r = res(ev[("u0", 0)], LlmClaim("c_mina", "explicit", [Candidate("c_mina", {"first_person"}, ["u0"], "llm")]))
    assert (r.speaker_id, r.status) == ("c_kaito", "explicit")
    assert any("contradicted by explicit dialogue tag" in n for n in r.notes)


def test_resolution_llm_explicit_downgraded():
    r = res((), LlmClaim("c_mina", "explicit", [Candidate("c_mina", {"dialogue_tag"}, ["u0"], "llm")]))
    assert (r.speaker_id, r.status) == ("c_mina", "inferred")


def test_resolution_unknown_stays_unknown():
    r = res((), LlmClaim(None, "unknown", []))
    assert (r.speaker_id, r.status) == (None, "unknown")
    r = res(())
    assert (r.speaker_id, r.status) == (None, "unknown")


def test_resolution_weak_only_is_inferred_not_explicit():
    r = res((), LlmClaim("c_kaito", "inferred", [Candidate("c_kaito", {"sentence_ending"}, ["u0"], "llm")]))
    assert r.status == "inferred" and "only weak (style) evidence" in r.notes


def test_resolution_imitation_style_overridden_by_action():
    _, _, ev = ev_for(["「俺様に任せろ！」", "　ミナはカイトの口調を真似て、胸を張った。"])
    r = res(ev[("u0", 0)], LlmClaim("c_kaito", "inferred", [Candidate("c_kaito", {"first_person", "verbal_tic"}, ["u0"], "llm")]))
    assert (r.speaker_id, r.status) == ("c_mina", "inferred")
    assert any("overridden by action evidence" in n for n in r.notes)


def test_resolution_conflicting_strong_evidence_is_unknown():
    _, _, ev = ev_for(["「やめろ」", "　カイトは剣を抜いた。"])
    r = res(ev[("u0", 0)], LlmClaim("c_mina", "inferred", [Candidate("c_mina", {"address"}, ["u0"], "llm")]))
    assert (r.speaker_id, r.status) == (None, "unknown")


def test_resolution_continuation_inherits_head():
    head = Resolution("c_mina", "explicit", "program")
    r = res((), LlmClaim("c_kaito", "inferred", []), head)
    assert (r.speaker_id, r.status, r.source) == ("c_mina", "explicit", "continuity")


def test_consecutive_lines_not_forced_to_alternate():
    # two consecutive quotes by the same explicit speaker stay with that speaker
    us = mk("「まず一つ」とミナは言った。", "「それから二つ目」とミナは続けた。")
    qs = find_quotes(us)
    ev = detect_tags(us, qs, AL)
    assert [resolve(ev[(q.unit_id, q.index)], None).speaker_id for q in qs] == ["c_mina", "c_mina"]


def test_speech_profile_and_style_hints():
    prof = speech_profile(["「私は行きます」", "「あなたも来てください」", "「私、知りませんでした」"])
    assert prof["first_person"] == ["私"] and prof["formality"] == "usually_polite"
    assert speech_profile(["「俺は知らねえよ」"])["formality"] == "casual"
    hints = style_hints("「私が行く」", {"c_mina": prof, "c_kaito": {"first_person": ["俺"]}})
    assert [h.character_id for h in hints] == ["c_mina"]
