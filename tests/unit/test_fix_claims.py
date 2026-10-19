"""What the fix check is told about an issue (ADR-055, ADR-067)."""

from ncte.analysis import proofread, reader
from ncte.translation.review import _claim


def test_the_fix_check_gets_the_finders_suggestion_without_the_note_to_the_translator():
    """A9: the instructions written for the translator (proofread.FIX_NOTE, reader.FIX_NOTE) are not a suggestion."""
    tone = _claim({"issue_type": "proofread_tone", "evidence": "原文是玩笑", "suggestion": f"改成「才怪」\n{proofread.FIX_NOTE}"})
    assert proofread.FIX_NOTE not in tone and "改成「才怪」" in tone and "原文是玩笑" in tone
    look = _claim({"issue_type": "reader_unclear", "evidence": "讀者看不懂", "suggestion": reader.FIX_NOTE})
    assert reader.FIX_NOTE not in look and look.endswith("讀者看不懂")
    assert "只給類別" in _claim({"issue_type": "proofread_meaning", "evidence": "e", "suggestion": "s"})
