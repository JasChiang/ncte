from lxml import etree

from ncte.epub.security import parse_xml_tree
from ncte.epub.units import extract_xhtml_units, rebuild_block, rebuild_run, validate_placeholders

NS = 'xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"'


def doc(body: str) -> etree._ElementTree:
    return parse_xml_tree(f"<html {NS}><head><title>題</title></head><body>{body}</body></html>".encode(),
                          html_entities=True)


def units(body: str):
    t = doc(body)
    return t, [u for u in extract_xhtml_units(t.getroot(), "a.xhtml") if u.kind != "head_title"]


def test_paragraph_with_inline_placeholders():
    t, us = units('<p>「<em>なぜ</em>だ」<br/>彼は<a href="b.xhtml#x">読んだ</a>。</p>')
    u = us[0]
    assert u.source_text == "「⟦1⟧なぜ⟦/1⟧だ」⟦2/⟧彼は⟦3⟧読んだ⟦/3⟧。"
    assert u.plain_text == "「なぜだ」彼は読んだ。"
    assert u.inline_map[3].attrib["href"] == "b.xhtml#x"
    assert u.inline_map[2].void


def test_ruby_flattened_and_counted():
    _, us = units("<p><ruby>強敵<rt>とも</rt></ruby>と<ruby><rb>蒼</rb><rp>(</rp><rt>あお</rt><rp>)</rp></ruby></p>")
    assert us[0].source_text == "強敵《とも》と蒼《あお》"
    assert us[0].ruby_count == 2


def test_mono_ruby():
    _, us = units("<p><ruby>魔<rt>ま</rt>法<rt>ほう</rt></ruby></p>")
    assert us[0].source_text == "魔《ま》法《ほう》"


def test_complex_ruby_kept_original():
    _, us = units("<p><ruby><rb><span>漢</span></rb><rt>かん</rt></ruby>字</p>")
    assert not us[0].translatable and us[0].skip_reason == "complex_ruby"
    _, us = units("<p><ruby><span><b>漢</b></span><rt>かん</rt></ruby>字</p>")  # nested wrapper
    assert not us[0].translatable
    _, us = units("<p><ruby>漢<rt><span>かん</span></rt></ruby>字</p>")  # markup inside rt
    assert not us[0].translatable


def test_kobo_span_inside_ruby_is_flattened():
    t, us = units('<p><span class="kobospan" id="kobo.3.1">「</span><ruby><span class="kobospan" id="kobo.4.1">蒼</span>'
                  '<rt>あお</rt><span class="kobospan" id="kobo.5.1">依</span><rt>い</rt></ruby>'
                  '<span class="kobospan" id="kobo.6.1">！」</span></p>')
    u = us[0]
    assert u.translatable and u.ruby_count == 1
    assert u.source_text == "⟦1⟧「⟦/1⟧蒼《あお》依《い》⟦2⟧！」⟦/2⟧"
    rebuild_block(u.element, "⟦1⟧「⟦/1⟧蒼依⟦2⟧！」⟦/2⟧", u.inline_map)
    s = etree.tostring(u.element, encoding="unicode")
    assert 'id="kobo.3.1"' in s and 'id="kobo.6.1"' in s and "<ruby>" not in s


def test_unsupported_inline_kept_original():
    _, us = units('<p>図<svg xmlns="http://www.w3.org/2000/svg"><text>x</text></svg>です</p>')
    assert not us[0].translatable and us[0].skip_reason.startswith("unsupported_inline")


def test_separator_and_blank_lines():
    _, us = units("<p>あ</p><p><br/></p><p><br/></p><p>＊　＊　＊</p><p>い</p>")
    assert [u.plain_text for u in us] == ["あ", "＊　＊　＊", "い"]
    assert us[1].is_separator and not us[1].translatable
    assert us[1].blank_before == 2


def test_heading_kind_and_skip_script():
    _, us = units("<h1>第一章</h1><script>var a='日本語';</script><p>本文</p>")
    assert [u.kind for u in us] == ["heading", "text"]


def test_mixed_block_runs_nav_like():
    t, us = units('<ol><li><a href="c1.xhtml">第一章</a><ol><li><a href="c2.xhtml">節</a></li></ol></li></ol>')
    assert [u.source_text for u in us] == ["⟦1⟧第一章⟦/1⟧", "⟦1⟧節⟦/1⟧"]
    assert us[0].run_index == 0
    rebuild_run(us[0].element, 0, "⟦1⟧第1章⟦/1⟧", us[0].inline_map)
    s = etree.tostring(t, encoding="unicode")
    assert '<a href="c1.xhtml">第1章</a><ol>' in s


def test_mixed_block_text_runs():
    t, us = units("<div>前の文<p>段落</p>後の文</div>")
    assert [u.source_text for u in us] == ["前の文", "段落", "後の文"]
    rebuild_run(us[2].element, 1, "後文", us[2].inline_map)
    rebuild_run(us[0].element, 0, "前文", us[0].inline_map)
    assert "<div>前文<p>段落</p>後文</div>" in etree.tostring(t, encoding="unicode")


def test_rebuild_reordered_placeholders():
    t, us = units('<p>彼は<a href="x">本</a>を<em>静かに</em>読んだ。</p>')
    u = us[0]
    tr = "他⟦2⟧安靜地⟦/2⟧讀著⟦1⟧書⟦/1⟧。"
    assert validate_placeholders(tr, u.inline_map) == []
    rebuild_block(u.element, tr, u.inline_map)
    s = etree.tostring(u.element, encoding="unicode")
    assert '他<em>安靜地</em>讀著<a href="x">書</a>。' in s


def test_nested_placeholders_must_keep_parent():
    _, us = units("<p><span>外<b>中</b></span>後</p>")
    u = us[0]
    assert u.source_text == "⟦1⟧外⟦2⟧中⟦/2⟧⟦/1⟧後"
    assert validate_placeholders("⟦1⟧外⟦2⟧中⟦/2⟧⟦/1⟧後", u.inline_map) == []
    assert validate_placeholders("⟦1⟧外⟦/1⟧⟦2⟧中⟦/2⟧後", u.inline_map)  # moved out of parent


def test_placeholder_errors():
    _, us = units("<p>あ<em>い</em>う<br/>え</p>")
    m = us[0].inline_map
    assert validate_placeholders("あいう", m)  # missing
    assert validate_placeholders("⟦1⟧い⟦/1⟧⟦1⟧い⟦/1⟧⟦2/⟧", m)  # duplicate
    assert validate_placeholders("⟦1⟧い⟦2/⟧", m)  # unclosed
    assert validate_placeholders("⟦1⟧い⟦/1⟧⟦2⟧⟦/2⟧", m)  # void used as pair
    assert validate_placeholders("⟦1⟧い⟦/1⟧⟦2/⟧⟦9/⟧", m)  # unknown
    assert validate_placeholders("⟦1⟧い⟦/1⟧⟦2/⟧ ⟦", m)  # stray bracket
    assert validate_placeholders("甲⟦1⟧乙⟦/1⟧丙⟦2/⟧丁", m) == []


def test_comment_preserved():
    t, us = units("<p>あ<!-- note -->い</p>")
    u = us[0]
    rebuild_block(u.element, "甲⟦1/⟧乙", u.inline_map)
    assert "甲<!-- note -->乙" in etree.tostring(u.element, encoding="unicode")


def test_xpath_stable_and_unique():
    _, us = units("<section><p>一</p><p>二</p></section><section><p>三</p></section>")
    paths = [u.xpath for u in us]
    assert paths == ["/html[1]/body[1]/section[1]/p[1]", "/html[1]/body[1]/section[1]/p[2]",
                     "/html[1]/body[1]/section[2]/p[1]"]
