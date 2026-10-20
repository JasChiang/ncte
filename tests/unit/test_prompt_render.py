"""Conditional prompt blocks (only rules about data the request carries)."""

from ncte.llm.prompt_loader import load_prompt


def test_rules_follow_the_payload():
    p = load_prompt("translate")
    plain = p.render({"units": [{"unit_id": "u", "source": "こんにちは"}], "glossary": []})
    assert "address_terms" not in plain and "may_refer_to" not in plain and "《" not in plain.split("## 翻譯規則")[1]
    assert "<!--" not in plain and "只輸出指定 JSON" in plain
    full = p.render({"units": [{"unit_id": "u", "source": "⟦1⟧強敵《とも》⟦/1⟧"}], "address_terms": [{}],
                     "glossary": [{"ja": "a", "may_refer_to": []}]})
    assert "`address_terms`：" in full and "may_refer_to" in full and "受保護的排版標記" in full and "ruby" in full
    assert "<!--" not in p.render()  # without a payload every block is kept


def _rules(text: str) -> dict[str, str]:
    """The numbered translation rules: number → the rule with its sub-items."""
    body = text.split("## 翻譯規則")[1].split("\n## ")[0]
    out: dict[str, str] = {}
    for part in body.split("\n"):
        head = part.split(".", 1)[0]
        if head.isdigit():
            out[head] = part
        elif out:
            out[list(out)[-1]] += "\n" + part
    return out


def test_later_paragraphs_are_for_understanding_not_for_adding():
    """3.1 (ADR-064): rule 4 says later paragraphs may and should be used to understand a line; what is forbidden is
    writing in what is revealed later, and taking the guesses of scene / memory as fact."""
    static, _ = load_prompt("translate").render_parts({"units": []})
    rule = _rules(static)["4"]
    assert "也應該用來理解" in rule and "增譯" in rule and "推測" in rule and "懸念" in rule
    assert "推測或後文資訊寫進譯文" not in rule  # the old wording that read as "do not look ahead"



def test_the_reactions_around_a_line_are_a_rule_of_their_own():
    """3.2 (ADR-064): making the reactions before and after a line work is a general rule of its own, between the
    order of judgement and the translation rules, about what is said as well as how; the sentence about a way of
    speaking being mocked moved out of rule 7 into it."""
    static, _ = load_prompt("translate").render_parts({"units": []})
    order, rules = static.index("## 判斷順序"), static.index("## 翻譯規則")
    at = static.index("## 讓前後的反應成立")
    assert order < at < rules
    section = static[at:rules]
    for word in ("反駁", "追問", "糾正", "評論", "吐槽", "模仿", "比較", "指的是誰", "說法", "不是補資訊"):
        assert word in section, word
    assert "讓後面的反應說得通" not in _rules(static)["7"]


def test_every_task_has_a_prompt_and_every_prompt_a_task():
    """A1 (ADR-067): the old long-paragraph scene prompt (ADR-036, removed by ADR-059) is gone with its task."""
    from pathlib import Path

    from ncte.llm.prompt_loader import TASKS
    from ncte.llm.schemas import SCHEMAS

    root = Path(__file__).resolve().parents[2] / "prompts"
    assert "scenes" not in TASKS and "scenes" not in SCHEMAS
    assert {d.name for d in root.iterdir() if d.is_dir()} == set(TASKS)
    for t in TASKS:
        assert "<!--" not in load_prompt(t).render()


def _bullet(text: str, key: str) -> str:
    """The bullet that defines `key` (``- `key`：…``) up to the next bullet or heading."""
    at = text.index(f"- `{key}`")
    rest = text[at + 3:]
    ends = [i for i in (rest.find("\n- "), rest.find("\n\n")) if i >= 0]
    return text[at:at + 3 + min(ends)] if ends else text[at:]


def test_an_omitted_subject_is_one_rule_on_three_sides():
    """A2 (ADR-067): supplying an omitted subject the context has settled is written for the translator (do it),
    the proofreader (missing or wrong subject is a meaning error; a settled subject is not an addition) and the fix
    check (a settled subject is not new information)."""
    static, _ = load_prompt("translate").render_parts({"units": []})
    rule = _rules(static)["3"]
    assert "接到別人身上" in rule and "已經確定" in rule and "subject_omitted_kept" in rule
    pr = load_prompt("proofread").render({"paragraphs": []})
    assert "主詞" in _bullet(pr, "meaning") and "另一個人" in _bullet(pr, "meaning")
    assert "已經確定的主詞不算" in _bullet(pr, "addition") and "刻意不說" in _bullet(pr, "addition")
    fc = load_prompt("fix_check").render({"items": []})
    assert "已經確定是誰的主詞" in _bullet(fc, "new_error")


def test_a_fix_with_the_wrong_tone_is_a_new_error():
    """A3 (ADR-067): a fix whose connotation or tone does not fit the original or the character's mood is a new
    error, as order of judgement 3 says."""
    fc = load_prompt("fix_check").render({"items": []})
    assert "褒貶" in _bullet(fc, "new_error") and "心情" in _bullet(fc, "new_error")


def _order(text: str) -> str:
    """The six-item order of judgement and the paragraph after it, without the one-line lead-in."""
    body = text.split("## 判斷順序\n", 1)[1].split("\n## ")[0]
    return body.split("\n", 1)[1].strip()


def test_the_order_of_judgement_is_shared_by_the_steps_that_judge():
    """A4 (ADR-067): review and the fix check judge with the same order as translation, proofreading, naming and
    house style."""
    same = {t: _order(load_prompt(t).render()) for t in
            ("translate", "proofread", "naming", "house_style", "review", "fix_check")}
    assert len(set(same.values())) == 1
    assert "1. **意思正確**" in same["review"] and "6. **最少介入**" in same["fix_check"]


def test_review_knows_tone_voice_other_and_two_readings():
    """A5 (ADR-067): review has the proofreader's tone and voice kinds, mentions other, and does not report a reading
    the source allows as a mistranslation."""
    from ncte.llm.schemas import ReviewIssue

    rv = load_prompt("review").render({"items": []})
    pr = load_prompt("proofread").render({"paragraphs": []})
    for kind in ("tone", "voice"):
        assert _bullet(rv, kind).split("：", 1)[1] == _bullet(pr, kind).split("：", 1)[1]
    assert "`other`" in rv
    assert "說不出原文為什麼不能是目前的讀法" in rv and "不要報成 `mistranslation`" in rv
    for kind in ("tone", "voice", "other"):
        ReviewIssue(unit_id="u", issue_type=kind, severity="medium", evidence="e", suggestion="s",
                    needs_retranslation=True)


def test_a_guessed_listener_or_gender_does_not_make_a_neutral_you_wrong():
    """A6 (ADR-067): the translator uses the neutral 你 when the listener or gender is only inferred; proofreading
    and the reader's look do not report that as an error."""
    pr = load_prompt("proofread").render({"paragraphs": []})
    assert "（推測）" in _bullet(pr, "speaker") and "不分性別的「你」不算錯" in _bullet(pr, "speaker")
    rd = load_prompt("reader").render({"paragraphs": []})
    assert "(inferred)" in _bullet(rd, "voice") and "不據此回報" in _bullet(rd, "voice")


def test_memory_enums_are_explained_with_the_schemas_values():
    """A7 (ADR-067): glossary_candidates.category and name_links.form_type list exactly the schema's values."""
    from typing import get_args

    from ncte.llm.schemas import GlossaryCandidate, NameLink

    cats = get_args(GlossaryCandidate.model_fields["category"].annotation)
    forms = get_args(NameLink.model_fields["form_type"].annotation)
    for task in ("memory_update", "scene_read"):
        text = load_prompt(task).render()
        line = next(x for x in text.split("\n") if "`category`" in x and "glossary_candidates" in x)
        assert all(f"`{c}`" in line for c in cats), task
        line = next(x for x in text.split("\n") if "`form_type`" in x)
        assert all(f"`{f}`" in line for f in forms), task


def test_voice_and_speech_habits_yield_to_the_line():
    """A8 (ADR-067): a character's voice is the overall manner, the house style's habits fixed renderings; where they
    pull apart, the line's own source decides."""
    static, _ = load_prompt("translate").render_parts({"units": []})
    voice = next(x for x in _rules(static)["3"].split("\n") if "`characters[].voice`" in x)
    assert "整體" in voice and "固定" in voice and "牴觸時以本句原文為準" in voice


def test_the_judging_steps_are_told_what_the_read_pass_said_about_omitted_subjects():
    """B1 (ADR-067): proofreading and the fix check describe `actions`: judged while reading, unknown / inferred not
    sure, for checking who an action is given to."""
    for task, payload in (("proofread", {"paragraphs": []}), ("fix_check", {"items": []})):
        text = load_prompt(task).render(payload)
        line = next(x for x in text.split("\n") if "`actions`" in x)
        assert "讀的時候" in line and "`unknown`" in line and "`inferred`" in line and "核對" in line, task


def test_the_rule_check_explains_the_translators_own_flags():
    """B3 (ADR-067): the confirm prompt says what each translator flag is and asks whether the translation holds."""
    from ncte.translation.orchestrator import CONFIRM_MODEL_FLAGS

    text = load_prompt("confirm").render({"items": []})
    assert "譯者自己" in text and "前後文" in text
    for rule in CONFIRM_MODEL_FLAGS:
        assert f"- `{rule}`" in text, rule


def test_proofreading_and_review_are_told_what_translator_notes_are():
    """B4 (ADR-067): the translator's notes are words for the checker, not translation."""
    for task, payload in (("proofread", {"paragraphs": []}), ("review", {"items": []})):
        text = load_prompt(task).render(payload)
        line = next(x for x in text.split("\n") if "`translator_notes`" in x)
        assert "譯者留給審稿者的話" in line and "不是譯文" in line, task


def test_the_read_pass_s_questions_are_explained_where_they_are_asked_and_used():
    """B5 (ADR-067): the second look knows the question kind and its answer (a sentence, optionally who); the
    translator and the reviewer are told what the answer is."""
    vf = load_prompt("verify").render({"items": []})
    assert "- `question`" in vf and "`character_id`" in vf
    for task, payload in (("translate", {"units": []}), ("review", {"items": []})):
        text = load_prompt(task).render(payload)
        line = next(x for x in text.split("\n") if "`scene.uncertainties`" in x)
        assert "`answer`" in line and "不是確定" in line, task


EVIDENCE_SENTENCE = ("`evidence_quote` 要選原文中直接寫出這筆事實的那一句，包含事實的主體與關鍵詞，"
                     "不要選只是在同一段開頭、沒有寫出這件事的句子。")


def test_a_fact_s_evidence_is_the_sentence_that_states_it():
    """ADR-069: a fact's evidence was the paragraph's first sentence, without the word the fact is about, so nothing
    searching by that word found it. Rule 1 of the memory update says so, in both prompts that carry it, word for word."""
    for task in ("memory_update", "scene_read"):
        text = load_prompt(task).render()
        rule = next(line for line in text.splitlines() if line.startswith("1. 每一筆 `facts` 必須附"))
        assert EVIDENCE_SENTENCE in rule, task


BOOK_WORDS = ("魔法", "魔術", "じゃなくて", "戦力外")


def test_a_subordinate_part_stays_subordinate():
    """ADR-070: a part that hangs on the main clause (contrast, modifier, condition, concession) is not turned into a
    statement of its own: split off, it says something the source does not (who did what, what the state was)."""
    static, _ = load_prompt("translate").render_parts({"units": []})
    rule = _rules(static)["5"]
    for word in ("附屬", "對比", "修飾", "條件", "讓步", "獨立的斷言", "誰做了什麼", "當時是什麼狀態",
                 "只說原文那個成分本身說的"):
        assert word in rule, word
    assert not any(w in static for w in BOOK_WORDS)


def test_proofreading_lists_the_readings_before_calling_a_meaning_error():
    """ADR-070: before judging the meaning of a line, list the readings grammar and context allow; a translation in
    one of them is not a meaning error; when every reading asserts more than the source, the fix is the wording that
    asserts none of it; `problem` writes the readings out."""
    pr = load_prompt("proofread").render({"paragraphs": []})
    meaning = _bullet(pr, "meaning")
    for word in ("先列出", "容許的讀法", "其中一種讀法", "`ambiguity`", "多斷定", "`fix`", "不帶這些斷定",
                 "`problem` 要寫出列出的讀法"):
        assert word in meaning, word
    assert "列出的讀法" in _bullet(pr, "ambiguity")
    assert not any(w in meaning + _bullet(pr, "ambiguity") for w in BOOK_WORDS)  # the ruby example elsewhere is older


def test_the_fix_check_weighs_before_and_after_against_the_readings_the_source_allows():
    """ADR-070: with two or more readings, `reason` lists them; before and after in different readings that both hold
    is issue_invalid (keep before), not a new error; both asserting more than the source is undecided with the
    wording that asserts none of it; `new_error` is only what no allowed reading has."""
    fc = load_prompt("fix_check").render({"items": []})
    gist = fc.split("## 每一項回答\n", 1)[1].split("\n- `gist`", 1)[0]
    method = gist.split("\n", 1)[1]  # the paragraph after the one on writing the gist
    for word in ("兩種以上讀法", "`reason` 列出", "`issue_invalid`", "保留 `before`", "不要判成新錯", "多斷定",
                 "`undecided`", "不帶這些斷定的寫法"):
        assert word in method, word
    assert "任何一種讀法都沒有" in _bullet(fc, "new_error")
    assert not any(w in fc for w in BOOK_WORDS)


def test_review_lists_the_readings_before_calling_a_mistranslation():
    """ADR-070: review's two-readings test starts from the readings grammar and context allow, as proofreading does."""
    rv = load_prompt("review").render({"items": []})
    rule = _bullet(rv, "mistranslation")
    for word in ("先列出", "容許的讀法", "其中一種讀法", "多斷定", "不帶這些斷定", "`evidence` 寫出列出的讀法"):
        assert word in rule, word
    assert not any(w in rv for w in BOOK_WORDS)


def test_notes_are_read_as_printed_and_wellknown_names_may_be_used():
    """A translator's note is part of what the reader reads (the reader's look and proofreading are told so); a brand
    or work with a well-known Chinese name in Taiwan may be named by it when sure (ADR-047's principle), never invented."""
    from ncte.llm.prompt_loader import load_prompt

    assert "已經由譯註說明清楚的地方不算卡住" in load_prompt("reader").text
    assert "譯註也要檢查" in load_prompt("proofread").text
    for task in ("memory_update", "scene_read"):
        t = load_prompt(task).text
        assert "在臺灣有通行的中文名稱、而且你有把握時" in t and "不要自己編中文名" in t
        assert "不要自己補上外國品牌的中文名稱" not in t
