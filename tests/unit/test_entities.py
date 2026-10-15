

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
