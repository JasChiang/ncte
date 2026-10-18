"""Small shared word lists (no external data). They flag (QA) and help display; they decide nothing the model reads
(ADR-059)."""

from __future__ import annotations

# Role / kinship / crowd nouns: legitimately registered as "characters" for speaker attribution, but they are
# ordinary words: never glossary terms, and the "added name" QA heuristic does not apply to them.
GENERIC_REFERENTS = frozenset(
    "赤ちゃん 赤ん坊 ベビー 先生 お客さん 客 店長 店員 母 父 お母さん お父さん 母さん 父さん おじいちゃん おばあちゃん "
    "祖父 祖母 お兄ちゃん お姉ちゃん 兄 姉 弟 妹 兄さん 姉さん 生徒 生徒たち 観客 周りの声 チームメイト 少女 少年 女の子 "
    "男の子 警察 お巡りさん 店主 社長 先輩 後輩 担任 クラスメイト 友達 みんな 皆 スタッフ 男 女 姫 王 人 子 "
    "その子 あの子 この子 その人 あの人 この人 その女の子 あの女の子 あの男 その男 あの女 その女 "
    # role nouns that name anyone (an alias 「兵士」 made a character of every soldier mentioned)
    "兵士 兵 兵士たち 兵たち 青年 女性 男性 職員 教員 教師 騎士 魔法士 学生 受験者 合格者 従者 付き人 侍女 メイド 執事 "
    # colloquial / kana kinship forms (「おねえちゃん」→「姐姐」 is a word, not a name)
    "姉ちゃん 兄ちゃん ねえちゃん にいちゃん おねえちゃん おにいちゃん おねーちゃん おにーちゃん お姉さん お兄さん "
    "おねえさん おにいさん 姉貴 兄貴 母ちゃん 父ちゃん かあさん とうさん おかあさん おとうさん ママ パパ "
    "おじさん おばさん じいちゃん ばあちゃん 母親 父親 お袋 親父 両親".split())


def acceptable_glossary_term(term: str) -> bool:
    """Automatic glossary candidates must be specific terms: ≥2 characters and not a generic referent."""
    t = term.strip()
    return len(t) >= 2 and t not in GENERIC_REFERENTS
