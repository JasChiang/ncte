from ncte.epub.units import InlineSpec
from ncte.qa.rules import check_batch, check_translation

EM = {1: InlineSpec(1, "element", "{http://www.w3.org/1999/xhtml}em", {}, False, 0)}


def run(src, text, *, imap=None, glossary=(), kind="text", ruby=0):
    plain = src.replace("⟦1⟧", "").replace("⟦/1⟧", "")
    return {(f.type, f.severity) for f in check_translation(unit_id="u", kind=kind, source_text=src, plain_text=plain,
                                                            inline_map=imap or {}, ruby_count=ruby, text=text,
                                                            glossary=glossary)}


def test_clean_translation():
    assert run("「⟦1⟧なぜ⟦/1⟧だ」と彼は言った。", "「⟦1⟧為什麼⟦/1⟧？」他說道。", imap=EM) == set()


def test_errors():
    assert ("empty_translation", "error") in run("あいう", "  ")
    assert ("placeholder_error", "error") in run("⟦1⟧あ⟦/1⟧", "啊", imap=EM)
    assert ("untranslated", "error") in run("こんにちは", "こんにちは")
    assert ("japanese_residue", "error") in run("こんにちはこんにちは", "你好こんにちはこんにちは")
    assert ("placeholder_error", "error") in run("タイトル", "⟦1⟧標題⟦/1⟧", kind="ncx")


def test_warnings():
    assert ("too_short", "high") in run("あ" * 40, "啊")
    assert run("　魔《ま》導《どう》車《しや》。", "　魔導車。", ruby=3) == set()  # readings are not counted
    assert ("japanese_residue", "error") in run("「王命が下されていますッ!!」", "「已經下達王命了ッ!!」")  # one is enough
    assert ("japanese_residue", "low") in run("藤木わしろでございます。", "我是藤木わしろ。")  # a name kept whole: a note
    assert ("untranslated", "low") in run("藤木わしろ", "藤木わしろ")  # an author's signature
    assert ("latin_word", "high") in run("唯一対等な存在だ", "是唯一對等 peer 的存在")
    assert ("foreign_script", "error") in run("最強の人間に対して。", "對著曾被稱為最強的пель人類。")  # leaked: retried
    assert ("foreign_script", "error") in run("ソウルの街", "首爾的서울街道")
    assert "foreign_script" not in {t for t, _ in run("「Спасибо」と彼は言った", "他說：「Спасибо」")}  # in the source
    assert run("ＭＭＯのカイルボス", "MMO的團隊副本首領") == set()  # in the source (full width) too
    assert ("non_taiwan_glyph", "error") in run("武装竜", "武裝竜")
    # a pair missing or added: right or wrong by what it marks, for the model to judge with the text around (confirm)
    assert ("quote_mismatch", "high") in run("「あいうえお、かきくけこ」", "啊咿嗚欸喔，咖嘰咕")
    assert ("quote_mismatch", "high") in run("アルテインという国", "「阿爾汀」這個國家")
    assert ("quote_mismatch", "low") in run("『ＫＡＳＳＥＮ』という遊戲", "「KASSEN」這款遊戲")  # 『』 written 「」: not the source's kind, a slip
    assert ("quote_mismatch", "medium") in run("「行くぞ」と『彼』は言った", "他說：「走吧")  # a quote lost or left open
    assert ("non_taiwan_glyph", "error") in run("これは国です", "这是国家")  # slid into Simplified: retried
    assert ("number_mismatch", "low") in run("１２歳の春だった", "那是十二歲的春天")
    assert ("ruby_notation_leak", "medium") in run("強敵《とも》だ", "強敵《とも》啊", ruby=1)


def test_glossary_checks():
    g = [{"source_term": "ミナ", "target_term": "米娜", "verified": True, "category": "character"}]
    assert ("glossary_mismatch", "high") in run("ミナは笑った。", "米奈笑了。", glossary=g)
    w = [{"source_term": "ＦＵＳＨＩ", "target_term": "ＦＵＳＨＩ", "verified": True, "category": "character"}]
    assert not [x for x in run("ＦＵＳＨＩ、よろしく", "FUSHI，拜託了", glossary=w) if x[0] == "glossary_mismatch"]
    full = [{"source_term": "カイル・ヴァイス", "target_term": "凱爾·弗里登", "verified": 1,
             "aliases": ["カイル", "英雄", "ヴァイス閣下"]}]
    assert run("剣士と魔女の転生記", "英雄與賢者的轉生婚", glossary=full) == set()  # a title is not the name
    assert run("ヴァイス閣下に王命が", "向弗里登閣下下達王命", glossary=full) == set()
    assert run("カイルは頷いた。", "凱爾點了點頭。", glossary=full) == set()  # the given name alone is rendered
    assert ("glossary_mismatch", "high") in run("カイルは頷いた。", "萊德點了點頭。", glossary=full)
    assert ("glossary_mismatch", "high") in run("カイル・ヴァイスです", "我是凱爾。", glossary=full)
    assert run("ミナは笑った。", "米娜笑了。", glossary=g) == set()
    assert ("possible_added_name", "high") in run("彼女は笑った。", "米娜笑了。", glossary=g)


def test_batch_duplicates():
    f = check_batch({"a": "完全相同的翻譯文字", "b": "完全相同的翻譯文字"}, {"a": "一つ目の文です", "b": "二つ目の文です"})
    assert [x.type for x in f] == ["duplicate_translation"]


def test_middle_dot_and_long_vowel_are_not_kana():
    # real-book regression: a song spelled out syllable by syllable (・) was rejected as untranslated
    src = "「やったー───！　る・ん・る・ん・た・の・し・い・なー♪」"
    assert run(src, "「太好了───！　好・開・心・好・快・樂・喔～♪」") == set()
    assert ("japanese_residue", "error") in run(src, "「太好了　るんるんたのしいな♪」")


def test_added_name_heuristic_regressions():
    g = [{"source_term": "ソラっち", "target_term": "索拉", "category": "character", "verified": False},
         {"source_term": "ソラ", "target_term": "索拉", "category": "character", "verified": False},
         {"source_term": "黒崎レン", "target_term": "黑崎蓮", "category": "character", "verified": False, "aliases": ["レン様"]},
         {"source_term": "赤ちゃん", "target_term": "嬰兒", "category": "character", "verified": False}]
    assert run("ソラが笑った。", "索拉笑了。", glossary=g) == set()  # another entry with the same rendering
    assert run("レン様が来た。", "黑崎蓮來了。", glossary=g) == set()  # alias in source
    assert run("ベビーが泣いた。", "嬰兒哭了。", glossary=g) == set()  # generic referent
    assert ("possible_added_name", "high") in run("彼女が笑った。", "索拉笑了。", glossary=g)


def test_cantonese_is_rejected_but_interjection_is_fine():
    src = "　──百円やったら、明日でええやん。気にせんとき。"  # Kansai-ben
    assert ("cantonese", "error") in run(src, "　──一百円嘅話，聽日先啦。唔使理佢㗎。")
    assert ("cantonese", "error") not in run(src, "　──才一百日圓的話，明天再說就好啦。別放在心上。")
    assert run("「うーん……」", "「唔～嗯……」") == set()
    assert ("non_taiwan_glyph", "medium") in run(src, "　──才一百円的話，明天再說就好啦。別放在心上。")


def test_cantonese_rule_ignores_mandarin_substrings():
    for zh in ("那邊有什麼嗎？", "聽日本人說過。", "這是重點解釋。", "在那邊度過了夏天。"):
        assert ("cantonese", "error") not in run("そちらには何が？", zh)


def test_glossary_match_ignores_placeholders():
    g = [{"source_term": "株式会社KADOKAWA", "target_term": "株式會社KADOKAWA", "verified": False}]
    assert not any(t == "glossary_mismatch" for t, _ in
                   run("発行所　株式会社⟦1⟧KADOKAWA⟦/1⟧", "發行所　株式會社⟦1⟧KADOKAWA⟦/1⟧", imap=EM, glossary=g))



def test_house_punctuation_is_the_same_everywhere():
    from ncte.qa.typeset import typeset

    assert typeset("「――開戰吧」") == "「——開戰吧」" and typeset("──那個──") == "——那個——"
    assert typeset("真的嗎!?") == "真的嗎！？" and typeset("好啊,走吧") == "好啊，走吧"
    assert typeset("等等...我") == "等等……我" and typeset("嗯…") == "嗯……"
    assert typeset("⟦1⟧「喂!?」⟦/1⟧") == "⟦1⟧「喂！？」⟦/1⟧"  # placeholders kept exactly
    assert typeset("OK, let's go!") == "OK, let's go!"  # Latin text left alone
    assert typeset("好事啊⟦1⟧!?⟦/1⟧」") == "好事啊⟦1⟧！？⟦/1⟧」"  # marks alone inside a styled run read with their neighbours
    assert typeset("啊……⟦1⟧!?⟦/1⟧」") == "啊……⟦1⟧！？⟦/1⟧」" and typeset("「⟦/1⟧⟦2⟧!?⟦/2⟧　是") == "「⟦/1⟧⟦2⟧！？⟦/2⟧　是"
    assert typeset("喂～⟦1⟧!!⟦/1⟧　這") == "喂～⟦1⟧！！⟦/1⟧　這"
    assert typeset("卡拉ＯＫ和２個人") == "卡拉OK和2個人"  # letters and digits half-width
    assert typeset("完美了♡ 對吧♡") == "完美了♡　對吧♡" and typeset("耶！？ 搞什麼") == "耶！？　搞什麼"
    assert typeset("☆ FUSHI，交給妳") == "☆ FUSHI，交給妳" and typeset("我有 3 個") == "我有 3 個"  # beside Latin: kept
    assert typeset("⟦1⟧!?⟦/1⟧") == "⟦1⟧!?⟦/1⟧" and typeset("他說⟦1⟧OK, go!⟦/1⟧") == "他說⟦1⟧OK, go!⟦/1⟧"


def test_the_indent_is_the_source_paragraphs():
    """The model drops or adds the leading 「　」; the translation takes the source's, nothing else changes."""
    from ncte.qa.typeset import typeset

    assert typeset("話雖如此，我是女孩子。", "　とはいえ私は女の子。") == "　話雖如此，我是女孩子。"
    assert typeset("　『喂。』", "『おい』") == "『喂。』"
    assert typeset("　⟦1⟧啊⟦/1⟧!?", "　⟦1⟧あ⟦/1⟧!?") == "　⟦1⟧啊⟦/1⟧！？"
    assert typeset("啊...", None) == "啊……"
