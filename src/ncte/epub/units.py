"""Translation-unit extraction and protected inline placeholders (spec §4.2).

A *unit* is a leaf block element (``p``, ``hN``, ``li`` …) whose inline content contains
translatable text. Inline markup is replaced by placeholders so the model never sees or
produces HTML:

* paired element ``<em>…</em>``   → ``⟦3⟧…⟦/3⟧``
* empty/void element ``<br/>``    → ``⟦4/⟧``
* simple ``<ruby>`` is flattened to ``base`` + reading in interlinear annotation characters (shown to the model as
  ``base《reading》``; the reading is only a hint for the model;
  the Chinese output is plain text – Japanese readings are never forced onto Chinese words).

Reassembly rebuilds the element subtree from the *original* element map; the model's text is
inserted as text nodes only, so no model-generated HTML can enter the book.
"""

from __future__ import annotations

import copy
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

XHTML_NS = "http://www.w3.org/1999/xhtml"

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "caption", "center", "dd", "details", "div",
    "dl", "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hgroup", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "summary", "table", "tbody",
    "td", "tfoot", "th", "thead", "tr", "ul",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {"script", "style", "svg", "math", "template", "noscript", "object", "iframe", "video", "audio",
             "canvas", "head"}
INLINE_TAGS = {
    "a", "abbr", "b", "bdi", "bdo", "big", "br", "cite", "code", "data", "del", "dfn", "em", "font", "i",
    "img", "ins", "kbd", "label", "mark", "q", "s", "samp", "small", "span", "strike", "strong", "sub",
    "sup", "time", "tt", "u", "var", "wbr", "image",
}
VOID_TAGS = {"br", "img", "wbr", "image"}

PH_RE = re.compile(r"⟦(/?)(\d+)(/?)⟧")
_WS = re.compile(r"[ \t\r\n\f]+")
SEPARATOR_RE = re.compile(r"^[\s　＊*◇◆□■☆★○●◎―—\-─━…‥・＿_~〜～#＃§※†◯▽△▼▲♢♦♠♣♥♡❖✽✻✧]+$")


class UnsupportedInline(Exception):
    pass


@dataclass
class InlineSpec:
    n: int
    kind: str  # "element" | "comment"
    tag: str
    attrib: dict[str, str]
    void: bool
    parent: int  # 0 = block root
    text: str = ""  # comment text

    def to_json(self) -> dict[str, Any]:
        return {"n": self.n, "kind": self.kind, "tag": self.tag, "attrib": self.attrib, "void": self.void,
                "parent": self.parent, "text": self.text}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> InlineSpec:
        return cls(**d)


@dataclass
class ExtractedUnit:
    resource_href: str
    xpath: str
    kind: str  # text | heading | head_title | nav | ncx | opf_title
    source_text: str  # with placeholders
    plain_text: str  # placeholders removed, ruby as U+FFF9 base U+FFFA reading U+FFFB
    inline_map: dict[int, InlineSpec] = field(default_factory=dict)
    translatable: bool = True
    skip_reason: str | None = None
    blank_before: int = 0
    is_separator: bool = False
    ruby_count: int = 0
    element: Any = None  # lxml element (runtime only)
    run_index: int | None = None  # set for inline runs inside mixed blocks

    @property
    def source_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()

    def inline_map_json(self) -> dict[str, Any]:
        return {str(k): v.to_json() for k, v in self.inline_map.items()}


def local(el: Any) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return etree.QName(tag).localname.lower()


def element_path(el: Any) -> str:
    """Stable path like ``/html[1]/body[1]/section[2]/p[14]`` (local names, same-name sibling index)."""
    parts: list[str] = []
    cur = el
    while cur is not None and isinstance(cur.tag, str):
        parent = cur.getparent()
        name = local(cur)
        if parent is None:
            parts.append(f"{name}[1]")
        else:
            same = [c for c in parent if isinstance(c.tag, str) and local(c) == name]
            parts.append(f"{name}[{same.index(cur) + 1}]")
        cur = parent
    return "/" + "/".join(reversed(parts))


def has_letters(text: str) -> bool:
    return any(unicodedata.category(ch).startswith("L") for ch in text)


def normalize_ws(text: str) -> str:
    return _WS.sub(" ", text).strip(" ")


def strip_placeholders(text: str) -> str:
    return PH_RE.sub("", text)


# A reading is stored with Unicode's own interlinear annotation characters — anchor, base, separator, reading,
# terminator (U+FFF9 蒼 U+FFFA あお U+FFFB) — which never occur in a book's text, so the author's own 《》 (a title:
# 「…、《亡雄の旅団》」) stay text everywhere. The model is shown the familiar 蒼《あお》 (for_model); what it quotes
# back may carry that form, so matching also drops a kana-only 《…》 right after a character.
RUBY_OPEN, RUBY_SEP, RUBY_CLOSE = "\ufff9", "\ufffa", "\ufffb"
RUBY_MARKED = re.compile("\ufff9([^\ufffa\ufffb]*)\ufffa([^\ufffb]*)\ufffb")
_SHOWN_READING = r"(?<=[^\s、。，．！？!?「」『』（）()《》〈〉【】・…―─])《[ぁ-ゖァ-ヺー・ゝゞヽヾ\s]+》"
RUBY_NOTATION = re.compile("\ufff9|\ufffa[^\ufffb]*\ufffb|" + _SHOWN_READING)  # .sub("", text) → the base text


def for_model(text: str) -> str:
    """Readings as the model reads them: 蒼《あお》."""
    return RUBY_MARKED.sub(r"\1《\2》", text)


def match_text(text: str) -> str:
    """Text for term/name matching: ruby readings removed (蒼《あお》依《い》 → 蒼依), placeholders removed."""
    return RUBY_NOTATION.sub("", PH_RE.sub("", text))


def _has_block_descendant(el: Any) -> bool:
    for d in el.iterdescendants():
        if isinstance(d.tag, str) and local(d) in BLOCK_TAGS:
            return True
    return False


def _is_block_child(c: Any) -> bool:
    if not isinstance(c.tag, str):
        return False
    name = local(c)
    return name in BLOCK_TAGS or name in SKIP_TAGS or _has_block_descendant(c)


def _runs(el: Any) -> list[tuple[Any, str, list[Any]]]:
    """Split a mixed block into inline runs separated by block children.

    Each run is (lead_owner, "text"|"tail", inline_children)."""
    runs: list[tuple[Any, str, list[Any]]] = []
    owner: Any = el
    attr = "text"
    cur: list[Any] = []
    for c in el:
        if _is_block_child(c):
            runs.append((owner, attr, cur))
            owner, attr, cur = c, "tail", []
        else:
            cur.append(c)
    runs.append((owner, attr, cur))
    return runs


def _block_child_after_run(el: Any, k: int) -> list[Any]:
    blocks = [c for c in el if _is_block_child(c)]
    return [blocks[k]] if k < len(blocks) else []


def _fake_run(lead: str, inline: list[Any]) -> Any:
    fake = etree.Element("ncte-run")
    fake.text = lead
    for c in inline:
        fake.append(copy.deepcopy(c))
    return fake


def rebuild_run(el: Any, k: int, text: str, inline_map: dict[int, InlineSpec]) -> None:
    """Rebuild inline run ``k`` of a mixed block element (see :func:`_runs`)."""
    runs = _runs(el)
    owner, attr, inline = runs[k]
    fake = etree.Element("ncte-run")
    rebuild_block(fake, text, inline_map)
    if inline:
        pos = list(el).index(inline[0])
        for c in inline:
            el.remove(c)
    else:
        pos = 0 if owner is el else list(el).index(owner) + 1
    setattr(owner, attr, fake.text)
    for i, c in enumerate(list(fake)):
        el.insert(pos + i, c)


class _Serializer:
    def __init__(self) -> None:
        self.n = 0
        self.map: dict[int, InlineSpec] = {}
        self.out: list[str] = []
        self.plain: list[str] = []
        self.ruby = 0
        self.dropped_wrappers = 0

    def emit_text(self, t: str | None) -> None:
        if t:
            self.out.append(t)
            self.plain.append(t)

    def children(self, el: Any, parent_n: int) -> None:
        self.emit_text(el.text)
        for c in el:
            if isinstance(c, etree._Comment):
                self.n += 1
                self.map[self.n] = InlineSpec(self.n, "comment", "", {}, True, parent_n, c.text or "")
                self.out.append(f"⟦{self.n}/⟧")
            elif isinstance(c, etree._ProcessingInstruction) or not isinstance(c.tag, str):
                raise UnsupportedInline("processing_instruction")
            else:
                name = local(c)
                if name == "ruby":
                    self.ruby_el(c)
                elif name in INLINE_TAGS:
                    self.n += 1
                    n = self.n
                    empty = len(c) == 0 and not c.text
                    void = name in VOID_TAGS or empty
                    if void and (len(c) or (c.text and c.text.strip())):
                        raise UnsupportedInline(f"void_with_content:{name}")
                    self.map[n] = InlineSpec(n, "element", c.tag, dict(c.attrib), void, parent_n)
                    if void:
                        self.out.append(f"⟦{n}/⟧")
                    else:
                        self.out.append(f"⟦{n}⟧")
                        self.children(c, n)
                        self.out.append(f"⟦/{n}⟧")
                else:
                    raise UnsupportedInline(f"unsupported_inline:{name}")
            self.emit_text(c.tail)

    def ruby_el(self, ruby: Any) -> None:
        self.ruby += 1
        base = ruby.text or ""
        pieces: list[str] = []
        for c in ruby:
            if not isinstance(c.tag, str):
                raise UnsupportedInline("complex_ruby")
            name = local(c)
            if len(c):
                raise UnsupportedInline("complex_ruby")
            if name == "rb":
                base += c.text or ""
            elif name in INLINE_TAGS and name not in VOID_TAGS:
                # text-only wrapper around the base (e.g. Kobo «<span class="kobospan" id="kobo.4.1">彩</span>»):
                # its text is base text; the wrapper itself is dropped with the flattened ruby (ADR-016)
                base += c.text or ""
                self.dropped_wrappers += 1
            elif name == "rt":
                reading = (c.text or "").strip()
                if base:
                    pieces.append(f"{RUBY_OPEN}{base}{RUBY_SEP}{reading}{RUBY_CLOSE}" if reading else base)
                base = ""
            elif name == "rp":
                pass
            else:
                raise UnsupportedInline("complex_ruby")
            base += c.tail or ""
        if base:
            pieces.append(base)
        self.emit_text("".join(pieces))


def serialize_block(el: Any) -> tuple[str, str, dict[int, InlineSpec], int]:
    s = _Serializer()
    s.children(el, 0)
    src = normalize_ws("".join(s.out))
    plain = normalize_ws(strip_placeholders("".join(s.plain)))
    return src, plain, s.map, s.ruby


def extract_xhtml_units(root: Any, href: str, *, nav: bool = False) -> list[ExtractedUnit]:
    """Extract units from an XHTML document root in document order."""
    units: list[ExtractedUnit] = []
    blank = [0]
    kind_default = "nav" if nav else "text"

    head = next((c for c in root if local(c) == "head"), None)
    if head is not None:
        title = next((c for c in head if local(c) == "title"), None)
        if title is not None and len(title) == 0 and title.text and has_letters(title.text):
            t = normalize_ws(title.text)
            units.append(ExtractedUnit(href, element_path(title), "head_title", t, t, element=title))

    def add_unit(el: Any) -> None:
        name = local(el)
        kind = "heading" if name in HEADING_TAGS and not nav else kind_default
        path = element_path(el)
        if name == "pre":
            txt = "".join(el.itertext())
            if txt.strip():
                units.append(ExtractedUnit(href, path, kind, normalize_ws(txt), normalize_ws(txt),
                                           translatable=False, skip_reason="preformatted", element=el))
            return
        try:
            src, plain, imap, ruby = serialize_block(el)
        except UnsupportedInline as exc:
            txt = normalize_ws("".join(el.itertext()))
            if txt:
                units.append(ExtractedUnit(href, path, kind, txt, txt, translatable=False,
                                           skip_reason=str(exc), blank_before=blank[0], element=el))
                blank[0] = 0
            return
        if not plain.strip(" 　"):
            blank[0] += 1
            return
        if not has_letters(plain):
            units.append(ExtractedUnit(href, path, kind, src, plain, imap, translatable=False,
                                       skip_reason="no_letters", blank_before=blank[0],
                                       is_separator=bool(SEPARATOR_RE.match(plain)), element=el))
            blank[0] = 0
            return
        units.append(ExtractedUnit(href, path, kind, src, plain, imap, blank_before=blank[0],
                                   ruby_count=ruby, element=el))
        blank[0] = 0

    def visit(el: Any) -> None:
        name = local(el)
        if not name or name in SKIP_TAGS:
            return
        if name in BLOCK_TAGS:
            if _has_block_descendant(el):
                for k, (owner, attr, inline) in enumerate(_runs(el)):
                    lead = getattr(owner, attr) or ""
                    fake = _fake_run(lead, inline)
                    path = f"{element_path(el)}#run{k}"
                    try:
                        src, plain, imap, ruby = serialize_block(fake)
                    except UnsupportedInline as exc:
                        txt = normalize_ws("".join(fake.itertext()))
                        if txt and has_letters(txt):
                            units.append(ExtractedUnit(href, path, kind_default, txt, txt, translatable=False,
                                                       skip_reason=str(exc), element=el, run_index=k))
                        continue
                    if plain.strip(" 　") and has_letters(plain):
                        units.append(ExtractedUnit(href, path, kind_default, src, plain, imap,
                                                   blank_before=blank[0], ruby_count=ruby, element=el,
                                                   run_index=k))
                        blank[0] = 0
                    for c in _block_child_after_run(el, k):
                        visit(c)
            else:
                if name == "hr":
                    return
                add_unit(el)
        else:
            # inline (or unknown) element outside a leaf block: only descend if it hides blocks
            if _has_block_descendant(el):
                for c in el:
                    if isinstance(c.tag, str):
                        visit(c)

    body = next((c for c in root if local(c) == "body"), None)
    if body is not None:
        visit(body)
    return units


def extract_ncx_units(root: Any, href: str) -> list[ExtractedUnit]:
    out = []
    for el in root.iter("{*}text"):
        parent = el.getparent()
        if parent is None or local(parent) not in ("navlabel", "doctitle"):
            continue
        txt = normalize_ws(el.text or "")
        if txt and has_letters(txt) and len(el) == 0:
            out.append(ExtractedUnit(href, element_path(el), "ncx", txt, txt, element=el))
    return out


def extract_opf_units(root: Any, href: str) -> list[ExtractedUnit]:
    out = []
    for el in root.iter("{http://purl.org/dc/elements/1.1/}title"):
        txt = normalize_ws(el.text or "")
        if txt and has_letters(txt) and len(el) == 0:
            out.append(ExtractedUnit(href, element_path(el), "opf_title", txt, txt, element=el))
    return out


# --------------------------------------------------------------------------- placeholders


def validate_placeholders(text: str, inline_map: dict[int, InlineSpec]) -> list[str]:
    """Return a list of problems; empty list = placeholders complete and correctly nested."""
    errors: list[str] = []
    stack: list[int] = []
    seen_open: set[int] = set()
    seen_close: set[int] = set()
    seen_void: set[int] = set()
    stripped = PH_RE.sub("", text)
    if "⟦" in stripped or "⟧" in stripped:
        errors.append("malformed placeholder bracket")
    for m in PH_RE.finditer(text):
        closing, n, selfclose = m.group(1) == "/", int(m.group(2)), m.group(3) == "/"
        spec = inline_map.get(n)
        if spec is None:
            errors.append(f"unknown placeholder {m.group(0)}")
            continue
        parent = stack[-1] if stack else 0
        if selfclose:
            if not spec.void:
                errors.append(f"{m.group(0)} should be paired")
            elif n in seen_void:
                errors.append(f"duplicate {m.group(0)}")
            elif spec.parent != parent:
                errors.append(f"{m.group(0)} moved out of its parent element")
            seen_void.add(n)
        elif closing:
            if not stack or stack[-1] != n:
                errors.append(f"unexpected closing {m.group(0)}")
            else:
                stack.pop()
                seen_close.add(n)
        else:
            if spec.void:
                errors.append(f"⟦{n}⟧ should be void ⟦{n}/⟧")
            elif n in seen_open:
                errors.append(f"duplicate ⟦{n}⟧")
            elif spec.parent != parent:
                errors.append(f"⟦{n}⟧ moved out of its parent element")
            seen_open.add(n)
            stack.append(n)
    if stack:
        errors.append(f"unclosed placeholders {stack}")
    for n, spec in inline_map.items():
        if spec.void and n not in seen_void:
            errors.append(f"missing ⟦{n}/⟧")
        if not spec.void and (n not in seen_open or n not in seen_close):
            errors.append(f"missing ⟦{n}⟧…⟦/{n}⟧")
    return errors


def _append_text(parent: Any, text: str) -> None:
    if not text:
        return
    if len(parent):
        last = parent[-1]
        last.tail = (last.tail or "") + text
    else:
        parent.text = (parent.text or "") + text


def rebuild_block(el: Any, text: str, inline_map: dict[int, InlineSpec]) -> None:
    """Replace the content of ``el`` with ``text``, recreating inline elements from ``inline_map``.

    Caller must have validated placeholders first.
    """
    errors = validate_placeholders(text, inline_map)
    if errors:
        raise ValueError("; ".join(errors))
    tail = el.tail
    for c in list(el):
        el.remove(c)
    el.text = None
    stack = [el]
    pos = 0
    for m in PH_RE.finditer(text):
        _append_text(stack[-1], text[pos:m.start()])
        pos = m.end()
        closing, n, selfclose = m.group(1) == "/", int(m.group(2)), m.group(3) == "/"
        spec = inline_map[n]
        if closing:
            stack.pop()
            continue
        if spec.kind == "comment":
            stack[-1].append(etree.Comment(spec.text))
            continue
        node = etree.SubElement(stack[-1], spec.tag, attrib=copy.copy(spec.attrib))
        if not selfclose:
            stack.append(node)
    _append_text(stack[-1], text[pos:])
    el.tail = tail
