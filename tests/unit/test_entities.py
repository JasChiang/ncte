

def test_group_forms_are_not_one_person():
    from ncte.lexicon import names_a_group

    for t in ("取り巻きの二人", "生徒たち", "騎士達", "三人組", "兵士ども", "両名"):
        assert names_a_group(t), t
    for t in ("取り巻き", "ヴァルク", "二人目", "サクラ", "一人娘"):
        assert not names_a_group(t), t


def test_a_group_is_never_merged_into_a_revealed_name():
    from ncte.analysis.scene import _revealed

    henchmen = {"id": "c_1", "book_id": "b", "canonical_name": "取り巻き", "aliases_json": '["取り巻きの二人"]',
                "known_from_ordinal": 10}
    hooded = {"id": "c_2", "book_id": "b", "canonical_name": "フードの少女", "aliases_json": "[]", "known_from_ordinal": 10}
    assert _revealed(None, "b", [henchmen], "c_1", ["取り巻き"], 50) is None  # even when the model says so
    assert _revealed(None, "b", [henchmen], None, ["取り巻き"], 50) is None
    assert _revealed(None, "b", [hooded], None, ["フードの少女"], 50) == "c_2"


def test_a_size_cut_waits_for_a_quiet_point():
    from ncte.analysis.scene import segment_units

    def row(i, text):
        return {"id": f"u{i}", "ordinal": i, "resource_href": "a.xhtml", "is_separator": 0, "kind": "text",
                "blank_before": 0, "spine_index": 1, "plain_text": text}

    rows = [row(i, "　地の文。") for i in range(8)] + [row(8 + i, "「台詞」") for i in range(4)] + \
        [row(12, "　地の文。"), row(13, "　地の文。"), row(14, "　地の文。")]
    scenes = segment_units(rows, {}, 9)  # the limit falls inside the exchange of lines (8–11)
    assert [len(s.unit_ids) for s in scenes] == [13, 2]  # cut after it, between two narration paragraphs


def test_a_description_is_not_a_name():
    from ncte.analysis.scene import _is_description

    for d in ("黒髪の女性", "艶やかな茶髪の美しい女性", "フードをかぶった少女", "年配の給仕"):
        assert _is_description(d), d
    for n in ("オリビア", "シルヴァード", "ユールシア", "安達", "しまむら", "あだっち", "ファレグ", "大司教モルト"):
        assert not _is_description(n), n


def test_narration_person_slip():
    from ncte.analysis.narration import first_person_slip

    assert first_person_slip("　村長の後を追って邸宅内に入る。", "和平常一樣互相鬥嘴幾句後，我跟在村長身後走進了宅邸內。")
    assert not first_person_slip("　その場に敵味方はない。", "在那種情況下無關敵我。")
    # a character's own thoughts in the narration (free indirect thought) keep 我
    assert not first_person_slip("　けれども涙がいっぱいになりました。そうだ僕は知っていたのだ。", "他眼眶蓄滿淚水。沒錯，我明明是知道的。")
    assert not first_person_slip("「俺がレイドだ」", "「我就是雷德」")
    assert not first_person_slip("　彼は言った。", "他說：「我會去的。」")


def test_narrator_self_form_kept_and_checked():
    from ncte.analysis.narration import self_form_slip, settle_self_forms

    rows = [{"kind": "text", "plain_text": "　吾輩は猫である。"}, {"kind": "text", "plain_text": "「俺は車屋の黒だ」"}]
    # only words the narration really has; an earlier decision for the same narrator wins
    got = settle_self_forms([{"ja": "吾輩", "zh": "我"}, {"ja": "俺", "zh": "老子"}, {"ja": "猫", "zh": "自己"}], None,
                            [{"narrator_ref": None, "ja": "吾輩", "zh": "吾輩"}], rows)
    assert got == [{"ja": "吾輩", "zh": "吾輩"}]
    assert self_form_slip("　吾輩は池の前に坐った。", "我坐在池塘前。", {"吾輩": "吾輩"})
    assert not self_form_slip("　吾輩は池の前に坐った。", "吾輩坐在池塘前。", {"吾輩": "吾輩"})
    assert self_form_slip("　吾輩は池の前に坐った。", "吾輩坐在池塘前。", {"吾輩": "我"})
    assert not self_form_slip("「吾輩は猫だ」", "「我是貓」", {"吾輩": "吾輩"})  # a quote is not the narration
