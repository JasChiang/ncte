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

