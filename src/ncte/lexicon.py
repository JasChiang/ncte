"""Small shared word lists (no external data)."""

from __future__ import annotations

# Role / kinship / crowd nouns: legitimately registered as "characters" for speaker attribution, but they are
# ordinary words: never glossary terms, and the "added name" QA heuristic does not apply to them.
GENERIC_REFERENTS = frozenset(
    "赤ちゃん 赤ん坊 ベビー 先生 お客さん 客 店長 店員 母 父 お母さん お父さん 母さん 父さん おじいちゃん おばあちゃん "
    "祖父 祖母 お兄ちゃん お姉ちゃん 兄 姉 弟 妹 兄さん 姉さん 生徒 生徒たち 観客 周りの声 チームメイト 少女 少年 女の子 "
    "男の子 警察 お巡りさん 店主 社長 先輩 後輩 担任 クラスメイト 友達 みんな 皆 スタッフ 男 女 姫 王 人 子 "
    "その子 あの子 この子 その人 あの人 この人 その女の子 あの女の子 あの男 その男 あの女 その女 "
    # colloquial / kana kinship forms (「おねえちゃん」→「姐姐」 is a word, not a name)
    "姉ちゃん 兄ちゃん ねえちゃん にいちゃん おねえちゃん おにいちゃん おねーちゃん おにーちゃん お姉さん お兄さん "
    "おねえさん おにいさん 姉貴 兄貴 母ちゃん 父ちゃん かあさん とうさん おかあさん おとうさん ママ パパ "
    "おじさん おばさん じいちゃん ばあちゃん 母親 父親 お袋 親父 両親".split())


def acceptable_glossary_term(term: str) -> bool:
    """Automatic glossary candidates must be specific terms: ≥2 characters and not a generic referent."""
    t = term.strip()
    return len(t) >= 2 and t not in GENERIC_REFERENTS


# words that state a person's gender (an explicit gender needs one of them in its evidence quote)
GENDER_WORDS = {
    "female": ("彼女", "少女", "女性", "女の子", "女子", "娘", "母", "姉", "妹", "王女", "姫", "嬢", "夫人", "婦人", "令嬢",
               "幼女", "老婆", "おばさん", "おばあ", "奥さん", "妻", "女王", "女神", "女", "メイド", "シスター", "巫女"),
    "male": ("彼", "少年", "男性", "男の子", "男子", "息子", "父", "兄", "弟", "王子", "青年", "老人", "おじさん", "おじい",
             "旦那", "夫", "王", "男", "坊", "爺"),
}


def states_gender(quote: str, gender: str) -> bool:
    """True if ``quote`` contains a word stating ``gender`` (「彼女」 does not count as 「彼」)."""
    if gender == "male":
        quote = quote.replace("彼女", "")
    return any(w in quote for w in GENDER_WORDS.get(gender, ()))
