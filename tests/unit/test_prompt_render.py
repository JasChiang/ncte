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
