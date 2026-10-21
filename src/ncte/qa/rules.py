"""Non-LLM translation QA (spec §5.7). Every translation passes through these checks.

Severities:
* ``error``  – the translation is rejected (retry / fail; never exported)
* ``high``   – stored, unit marked ``needs_review`` (triggers selective LLM review)
* ``medium`` / ``low`` / ``info`` – recorded as flags (a flag is not proof of an error)
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..epub.units import PH_RE, InlineSpec, match_text, validate_placeholders
from ..lexicon import GENERIC_REFERENTS
from .typeset import halfwidth

# Hiragana + katakana letters only. Excludes ゠ (U+30A0), ・ (U+30FB), ー (U+30FC) and halfwidth ｰ (U+FF70):
# they are punctuation that is also normal in Chinese text (間隔號、長音).
KANA_RE = re.compile("[\u3041-\u309f\u30a1-\u30fa\u30fd-\u30ff\uff66-\uff6f\uff71-\uff9f]")
KANA_RUN_RE = re.compile("[\u3041-\u309f\u30a1-\u30fa\u30fd-\u30ff\uff66-\uff6f\uff71-\uff9f]+")
SMALL_KANA_RE = re.compile("[ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ]")
RUBY_LEAK_RE = re.compile(r"《[぀-ヿ]+》")
NUM_RE = re.compile(r"\d+")
# Glyphs that should not appear in Taiwanese Traditional Chinese output: Simplified-only forms
# and Japanese shinjitai forms that differ from the Taiwanese standard.
NON_TW_GLYPHS = set(
    "这们说话时对见没还让发样爱书东车门问间开关头过应该实现动学习经买卖钱气电万与为从乐个写吗页颜听觉记认识谁请饭鸡鱼马鸟龙岛剑灵战杀伤恋梦惊么"
    "気歩変実単戦剣様図広読売続関鉄声楽薬処拠払帰隠険験駅桜壊懐黒覚権歳円竜")
# Japanese shinjitai → the Taiwanese form (never right in the translation, even when the source has them)
JA_TO_TW = dict(zip("竜気剣戦様図読続関鉄声楽薬処帰険験駅壊黒覚歳歩変実単広売拠払隠懐権",
                    "龍氣劍戰樣圖讀續關鐵聲樂藥處歸險驗驛壞黑覺歲步變實單廣賣據拂隱懷權", strict=True))
ALWAYS_CHINESE = set(JA_TO_TW)
PLAIN_KINDS = {"head_title", "ncx", "opf_title"}
# Chinese slurs / strongly derogatory words that a literal rendering can produce by accident
# (黒鬼 "black ogre" → 黑鬼, a racial slur in Chinese). Flagged for human review, never auto-fixed.
OFFENSIVE_ZH = ("黑鬼", "支那", "阿三", "黑奴", "小日本", "鬼佬", "棒子", "死玻璃", "娘炮", "土人")
# Cantonese-only wording. Japanese dialects (e.g. Kansai-ben) must not be rendered as Cantonese for Taiwanese
# readers (real-book regression). The interjection 唔 alone is NOT included (唔～ = "hmm" in Mandarin too).
CANTONESE_CHARS = set("嘅㗎咗佢啲喺冇嘢嚟嗰噉冚攞睇諗嬲")
# Only sequences that do not occur inside ordinary Mandarin phrases (點解 ⊂ 重點解釋, 聽日 ⊂ 聽日本, 邊有 ⊂ 那邊有,
# 邊度 ⊂ 那邊度過 were real-book false positives and are deliberately excluded).
CANTONESE_WORDS = ("係咪", "唔係", "唔好", "唔使", "唔該", "而家", "乜嘢", "咁樣")
# Cyrillic, Greek, Armenian, Hebrew, Arabic, Devanagari, Thai, Hangul: never part of a Chinese sentence unless the
# source has them
FOREIGN_SCRIPT_RE = re.compile("[\u0370-\u03FF\u0400-\u052F\u0530-\u058F\u0590-\u06FF\u0900-\u097F"
                               "\u0E00-\u0E7F\u1100-\u11FF\u3130-\u318F\uAC00-\uD7AF]")
SEVERITY_ORDER = {"error": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
# rules that read words, not meaning: what they find is checked by the model with context before it counts
CONFIRM_RULES = ("offensive_term", "narration_first_person", "narrator_self_form", "latin_word", "possible_added_name",
                 "second_person_gender", "narrator_name_leak", "quote_mismatch")
CONFIRMED = " | confirmed: "  # appended to a word-rule finding the model checked with context and found real


@dataclass
class Finding:
    type: str
    severity: str
    evidence: str
    unit_id: str | None = None


def _visible(text: str) -> str:
    return "".join(ch for ch in PH_RE.sub("", text) if not ch.isspace() and ch != "　")


def check_translation(*, unit_id: str, kind: str, source_text: str, plain_text: str,
                      inline_map: dict[int, InlineSpec], ruby_count: int, text: str,
                      glossary: Iterable[dict[str, Any]] = (), allowed_kana: Iterable[str] = ()) -> list[Finding]:
    f: list[Finding] = []

    def add(t: str, sev: str, ev: str) -> None:
        f.append(Finding(t, sev, ev, unit_id))

    src_match = match_text(plain_text)
    vis_src = _visible(plain_text)
    vis = _visible(text)
    if not vis:
        add("empty_translation", "error", "translation is empty")
        return f
    if kind in PLAIN_KINDS and PH_RE.search(text):
        add("placeholder_error", "error", "placeholder in plain-text unit")
    elif kind not in PLAIN_KINDS:
        errs = validate_placeholders(text, inline_map)
        if errs:
            add("placeholder_error", "error", "; ".join(errs[:5]))
    if "⟦" in PH_RE.sub("", text) or "⟧" in PH_RE.sub("", text):
        add("placeholder_error", "error", "stray placeholder bracket")

    kana_src = KANA_RE.findall(vis_src)
    if text.strip() == source_text.strip() and kana_src:
        if len(vis_src) <= 10 and re.search(r"[\u4e00-\u9fff]", vis_src) and not re.search(r"[。、！？!?「」]", vis_src):
            # a name kept as is (the author's signature 藤木わしろ)
            add("untranslated", "low", "kept as in the source (a name?)")
        else:
            add("untranslated", "error", "translation identical to Japanese source")
    else:
        residue = vis
        for w in allowed_kana:
            residue = residue.replace(w, "")
        kana = KANA_RE.findall(residue)
        runs = KANA_RUN_RE.findall(residue)
        # a word copied whole from the source may be a name kept on purpose (藤木わしろ); a stray sound mark
        # (「…王命了ッ!!」) or a word the source does not have is a leftover: retried with this correction
        kept = [r for r in runs if 2 <= len(r) <= 8 and r in vis_src and not SMALL_KANA_RE.search(r)]
        stray = [r for r in runs if r not in kept]
        if kana and len(kana) / max(len(vis), 1) > 0.3 and (stray or len(kana) > 8):
            add("japanese_residue", "error", f"{len(kana)} kana in output (mostly untranslated)")
        elif kana:
            if stray:
                add("japanese_residue", "error", f"kana left in output: {''.join(stray)[:20]} — write it in Chinese "
                                                 f"(a small ッ/っ before !! is just dropped)")
            else:
                add("japanese_residue", "low", f"kana kept from the source (a name?): {'、'.join(kept)[:30]}")

    if ruby_count and RUBY_LEAK_RE.search(text):
        add("ruby_notation_leak", "medium", RUBY_LEAK_RE.search(text).group(0))  # type: ignore[union-attr]

    body_src = _visible(match_text(plain_text))  # ruby readings (魔《ま》導《どう》車《しや》) are not text to translate
    if len(body_src) >= 12:
        ratio = len(vis) / len(body_src)
        if ratio < 0.25:
            add("too_short", "high", f"length ratio {ratio:.2f} (possible omission)")
        elif ratio < 0.4:
            add("too_short", "medium", f"length ratio {ratio:.2f}")
        elif ratio > 2.5:
            add("too_long", "medium", f"length ratio {ratio:.2f} (possible addition)")

    # quotes follow the source by default (prompts/_shared/punctuation.md). A kind changed (『』 → 「」, a title in 《》)
    # is a slip shown for proofreading to judge with the source (low); unpaired quotes may drop a line (medium). A pair
    # added or missing may be right or wrong by what it marks — 「阿爾汀」 for the source's アルテインという is fine,
    # quotes put around narration or an unquoted thought are not: the model decides with the text around (confirm)
    pairs = (("「", "」"), ("『", "』"), ("《", "》"), ("〈", "〉"))
    src_q = sum(source_text.count(o) for o, _ in pairs[:2])
    out_q = sum(text.count(o) for o, _ in pairs)
    balanced = all(text.count(o) == text.count(c) for o, c in pairs)
    for o, c in pairs[:2]:
        so, sc, to, tc = source_text.count(o), source_text.count(c), text.count(o), text.count(c)
        if (so, sc) != (to, tc):
            sev = "medium" if not balanced else "low" if out_q == src_q else "high"
            what = "" if sev != "high" else " (a pair added)" if out_q > src_q else " (a pair missing)"
            add("quote_mismatch", sev, f"{o}{c}: source {so}/{sc} → {to}/{tc}{what}")
            break

    nums_src = set(NUM_RE.findall(unicodedata.normalize("NFKC", PH_RE.sub("", plain_text))))
    nums_out = set(NUM_RE.findall(unicodedata.normalize("NFKC", PH_RE.sub("", text))))
    if nums_src - nums_out:
        add("number_mismatch", "low", f"numbers missing in output: {sorted(nums_src - nums_out)}")

    # an English word the source does not have (「唯一對等 peer 的存在」): a leftover of the model's own thinking
    latin = sorted({w for w in re.findall(r"[A-Za-z]{3,}", PH_RE.sub("", text))
                    if w.lower() not in unicodedata.normalize("NFKC", source_text).lower()})
    if latin:
        add("latin_word", "high", f"English word(s) not in the source: {', '.join(latin)[:60]}")

    # letters of another script the source does not have (「最強人類」 with Cyrillic in it): leaked from the model
    foreign = sorted({ch for ch in vis if FOREIGN_SCRIPT_RE.match(ch) and ch not in source_text})
    if foreign:
        add("foreign_script", "error", f"letters of another script not in the source: {''.join(foreign)[:20]} — "
                                       f"write the sentence in Chinese only")

    canto = sorted({ch for ch in vis if ch in CANTONESE_CHARS} | {w for w in CANTONESE_WORDS if w in vis})
    if canto:
        add("cantonese", "error", f"Cantonese wording {'、'.join(canto)}: use natural Taiwan Mandarin (render dialects "
                                  f"such as Kansai-ben through tone and word choice, not Cantonese)")
    bad = sorted({ch for ch in vis if ch in NON_TW_GLYPHS})
    # not copied from the source: the output slid into Simplified; a Japanese form with a Chinese one (竜→龍, 剣→劍)
    # is wrong even when the source has it — only 円/桜-like forms that a name or currency may keep are excused
    drift = [ch for ch in bad if ch not in vis_src or ch in ALWAYS_CHINESE]
    if drift:
        add("non_taiwan_glyph", "error", f"simplified/Japanese glyphs {''.join(drift)}: write the whole unit in "
                                         f"Taiwan Traditional Chinese")
    elif bad:
        add("non_taiwan_glyph", "medium", f"simplified/Japanese glyphs copied from the source: {''.join(bad)}")

    gl = list(glossary)
    flat = PH_RE.sub("", text)  # 「株式會社⟦2⟧KADOKAWA⟦/2⟧」 still contains 株式會社KADOKAWA
    for w in OFFENSIVE_ZH:
        if w in vis and w not in vis_src:
            add("offensive_term", "high", f"「{w}」 is a slur/derogatory word in Chinese – choose a different rendering")
    entities: dict[str, list[dict[str, Any]]] = {}
    for g in gl:
        if g.get("entity_id"):
            entities.setdefault(str(g["entity_id"]), []).append(g)

    def in_source(g: dict[str, Any]) -> bool:
        terms = [g.get("source_term") or g.get("ja"), *(g.get("aliases") or [])]
        return any(t and t in src_match for t in terms)

    def rendered(g: dict[str, Any], ja: str, zh: str) -> bool:
        if zh in flat:
            return True
        if ja in src_match:
            return False
        # only another form is in the source. A title or description (「英雄」「学院長」) is rendered as itself, not as
        # the name: nothing to require. A part of the name (「カイル」, 「ヴァイス閣下」): that part of the rendering.
        parts = [p for p in re.split(r"[・･·]", ja) if len(p) >= 2]
        named = [a for a in g.get("aliases") or [] if a in src_match and (a in ja or any(p in a for p in parts))]
        if not named:
            return True
        return any(len(p) >= 2 and p in flat for p in re.split(r"[・･·]", zh))

    for g in gl:
        ja, zh = g.get("source_term") or g.get("ja"), halfwidth(g.get("target_term") or g.get("zh") or "")
        if not ja or not zh:
            continue
        in_src = in_source(g)
        if in_src and not rendered(g, ja, zh):
            sev = "high" if g.get("verified") else "low"
            add("glossary_mismatch", sev, f"{ja} → expected 「{zh}」")
            # another name form of the same entity used instead (the official name rendered with the nickname's rendering)
            for other in entities.get(str(g.get("entity_id") or ""), []):
                ozh = halfwidth(other.get("target_term") or "")
                if other is not g and ozh and ozh != zh and ozh not in zh and zh not in ozh and ozh in flat \
                        and not in_source(other):
                    add("name_form_substitution", "high",
                        f"source uses {ja} (→「{zh}」) but translation uses 「{ozh}」 ({other.get('form_type')} {other['source_term']})")
        elif (not in_src and g.get("category") == "character" and len(zh) >= 2 and zh in flat
              and ja not in GENERIC_REFERENTS):
            # justified if any other entry whose rendering contains this name is present in the source
            # (ソラ / ソラっち / 天野ソラ → 索拉 …)
            if any(in_source(o) for o in gl if zh in halfwidth(str(o.get("target_term") or o.get("zh") or ""))):
                continue
            add("possible_added_name", "high", f"「{zh}」 appears in translation but {ja} not in source")
    return f


def check_batch(results: dict[str, str], sources: dict[str, str]) -> list[Finding]:
    """Cross-unit checks inside one batch (duplicates / swapped units)."""
    f: list[Finding] = []
    seen: dict[str, str] = {}
    for uid, text in results.items():
        key = _visible(text)
        if len(key) < 6:
            continue
        if key in seen and _visible(sources[seen[key]]) != _visible(sources[uid]):
            f.append(Finding("duplicate_translation", "high", f"same translation as {seen[key]}", uid))
        seen.setdefault(key, uid)
    return f


def worst(findings: Iterable[Finding]) -> str | None:
    best = None
    for x in findings:
        if best is None or SEVERITY_ORDER[x.severity] > SEVERITY_ORDER[best]:
            best = x.severity
    return best
