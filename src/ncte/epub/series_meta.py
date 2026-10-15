"""Guess "which series / which volume" from EPUB metadata and the title (suggestion only, never applied
silently — putting a book into the wrong series mixes characters and renderings).

Sources, most reliable first:
1. EPUB 3 ``belongs-to-collection`` (+ ``collection-type`` series, ``group-position``)
2. Calibre ``calibre:series`` / ``calibre:series_index``
3. Title patterns: 「〇〇 2」「〇〇（3）」「〇〇 第4巻」「〇〇 Ⅱ」「〇〇 上」「〇〇 ③」「〇〇 2 ―副題―」
4. Calibre ``title_sort`` ending in digits (「チョウカグヤヒメ001」) – weak hint only
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from lxml import etree

from .parser import NS, EpubPackage
from .security import parse_xml

ROMAN = {"Ⅰ": 1, "Ⅱ": 2, "Ⅲ": 3, "Ⅳ": 4, "Ⅴ": 5, "Ⅵ": 6, "Ⅶ": 7, "Ⅷ": 8, "Ⅸ": 9, "Ⅹ": 10, "Ⅺ": 11, "Ⅻ": 12,
         "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}
CIRCLED = {chr(0x2460 + i): i + 1 for i in range(20)}  # ①…⑳
UPPER_LOWER = {"上": 1, "中": 2, "下": 2, "前編": 1, "後編": 2, "前篇": 1, "後篇": 2}
_SEP = r"[\s　]*"
_SUB = r"(?:[\s　]*[-‐—―〜~～:：].*)?"  # optional trailing subtitle


@dataclass
class SeriesGuess:
    title: str | None
    volume: float | None
    source: str  # epub3_collection | calibre | title | title_sort | none
    confidence: str  # high | medium | low | none
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _num(s: str) -> float | None:
    s = unicodedata.normalize("NFKC", s).strip()
    try:
        return float(s)
    except ValueError:
        return None


_LABEL = re.compile(r"[\s　]*[（(\[【][^()（）\[\]【】]*(?:文庫|ノベル|ブックス|books|novels|新書|レーベル|コミックス|"
                    r"版|特典|edition)[^()（）\[\]【】]*[)）\]】][\s　]*$", re.IGNORECASE)
# the same labels in front: 「【電子版限定特典付き】英雄と賢者の転生婚 1」 (store editions; volume 2 may lack it)
_LEAD = re.compile(r"^[\s　]*[（(\[【][^()（）\[\]【】]*(?:版|特典|限定|edition)[^()（）\[\]【】]*[)）\]】][\s　]*",
                   re.IGNORECASE)


def clean_title(title: str) -> str:
    """Remove imprint / edition labels: 「安達としまむら2 (電撃文庫)」 → 「安達としまむら2」,
    「【電子版限定特典付き】英雄と賢者の転生婚 1」 → 「英雄と賢者の転生婚 1」."""
    t = title.strip()
    while True:
        new = _LEAD.sub("", _LABEL.sub("", t))
        if new == t or not new:
            return t
        t = new


def guess_from_title(title: str) -> tuple[str, float, str] | None:
    """(base title, volume, matched text) from a book title, or None."""
    title = clean_title(title)
    t = unicodedata.normalize("NFKC", title).strip()
    raw = title.strip()
    patterns = [
        rf"^(?P<base>.+?){_SEP}第{_SEP}(?P<n>\d+(?:\.\d+)?){_SEP}[巻卷集部]{_SUB}$",
        rf"^(?P<base>.+?){_SEP}[（(]{_SEP}(?P<n>\d+(?:\.\d+)?){_SEP}[)）]{_SUB}$",
        rf"^(?P<base>.+?)[\s　]+(?:Vol\.?|vol\.?|VOL\.?){_SEP}(?P<n>\d+(?:\.\d+)?){_SUB}$",
        rf"^(?P<base>.+?)[\s　]+(?P<n>\d{{1,3}}(?:\.\d+)?){_SUB}$",
        r"^(?P<base>.{2,}?)[\s　]+(?P<n>\d{1,3})[\s　]+(?P<sub>\D.*)$",  # 「〇〇 3 副題」
        # number glued to Japanese/Chinese text: 「安達としまむら2」 (not 「Re:ゼロ2」 / 「1Q84」)
        rf"^(?P<base>.*[぀-ヿ㐀-鿿々〆！？」』】])(?P<n>\d{{1,3}}(?:\.\d+)?){_SUB}$",
    ]
    for p in patterns:
        m = re.match(p, t)
        if m and m.group("base").strip():
            return m.group("base").strip(), float(m.group("n")), m.group(0)
    m = re.match(rf"^(?P<base>.+?){_SEP}(?P<c>[{''.join(CIRCLED)}]){_SUB}$", raw)
    if m:
        return m.group("base").strip(), float(CIRCLED[m.group("c")]), m.group(0)
    m = re.match(rf"^(?P<base>.+?){_SEP}(?P<r>[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]){_SUB}$", raw) or \
        re.match(rf"^(?P<base>.+?)[\s　]+(?P<r>I{{1,3}}|IV|V|VI{{0,3}}|IX|X){_SUB}$", t)
    if m:
        return m.group("base").strip(), float(ROMAN[m.group("r")]), m.group(0)
    m = re.match(rf"^(?P<base>.+?){_SEP}[（(]?(?P<u>上|中|下|前編|後編|前篇|後篇)[)）]?{_SUB}$", raw)
    if m and len(m.group("base").strip()) >= 2:
        return m.group("base").strip(), float(UPPER_LOWER[m.group("u")]), m.group(0)
    return None


def detect_series(pkg: EpubPackage) -> SeriesGuess:
    opf = parse_xml(pkg.archive.get(pkg.opf_path), name=pkg.opf_path)
    md = opf.find("opf:metadata", NS)
    metas = [] if md is None else [m for m in md if isinstance(m.tag, str) and etree.QName(m).localname == "meta"]
    refines: dict[str, dict[str, str]] = {}
    for m in metas:
        ref = (m.get("refines") or "").lstrip("#")
        if ref and m.get("property"):
            refines.setdefault(ref, {})[m.get("property", "")] = (m.text or "").strip()
    # 1. EPUB 3 collection
    for m in metas:
        if m.get("property") == "belongs-to-collection" and (m.text or "").strip():
            r = refines.get(m.get("id") or "", {})
            if r.get("collection-type", "series") in ("series", ""):
                vol = _num(r.get("group-position", ""))
                return SeriesGuess((m.text or "").strip(), vol, "epub3_collection", "high" if vol else "medium",
                                   f"belongs-to-collection «{(m.text or '').strip()}» group-position "
                                   f"{r.get('group-position', '?')}")
    named = {m.get("name"): m.get("content") for m in metas if m.get("name")}
    # 2. Calibre
    if named.get("calibre:series"):
        vol = _num(named.get("calibre:series_index") or "")
        return SeriesGuess(named["calibre:series"], vol, "calibre", "high" if vol else "medium",
                           f"calibre:series «{named['calibre:series']}» index {named.get('calibre:series_index', '?')}")
    # 3. title
    g = guess_from_title(pkg.title)
    if g:
        return SeriesGuess(g[0], g[1], "title", "medium", f"title «{pkg.title}»")
    # 4. calibre title_sort ending in digits
    ts = named.get("calibre:title_sort") or ""
    tm = re.search(r"(\d{1,4})$", unicodedata.normalize("NFKC", ts))
    if tm and int(tm.group(1)) > 0:
        return SeriesGuess(pkg.title, float(int(tm.group(1))), "title_sort", "low", f"calibre:title_sort «{ts}»")
    return SeriesGuess(None, None, "none", "none", "")


def normalize_title(t: str) -> str:
    t = unicodedata.normalize("NFKC", clean_title(t)).lower()
    return re.sub(r"[\s　!！?？・:：\-‐—―~〜～「」『』【】()（）\[\]]+", "", t)
