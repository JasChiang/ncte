"""XHTML DOM reassembly and EPUB packaging (spec §4.3).

Only XHTML documents with applied translations, the NAV/NCX and the OPF are rewritten; all
other entries are copied byte-for-byte in their original order. ``mimetype`` is always the first
entry and stored uncompressed.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from .parser import NS, XHTML_TYPES, EpubPackage
from .security import parse_xml_tree
from .units import (
    ExtractedUnit,
    extract_ncx_units,
    extract_opf_units,
    extract_xhtml_units,
    rebuild_block,
    rebuild_run,
)

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


@dataclass
class TranslationPatch:
    text: str
    source_hash: str
    unit_id: str


@dataclass
class ExportResult:
    output_path: Path
    applied: int = 0
    untranslated: list[tuple[str, str]] = field(default_factory=list)  # (href, xpath)
    hash_mismatch: list[str] = field(default_factory=list)  # unit ids
    rebuild_errors: list[tuple[str, str]] = field(default_factory=list)  # (unit_id, error)
    modified_files: list[str] = field(default_factory=list)


def content_documents(pkg: EpubPackage) -> list[tuple[str, bool]]:
    """(href, is_nav) for all XHTML documents that may contain units, spine order first."""
    docs: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for s in pkg.spine:
        item = pkg.item_by_href(s.href)
        if item and item.media_type in XHTML_TYPES and s.href not in seen:
            docs.append((s.href, s.href == pkg.nav_href))
            seen.add(s.href)
    if pkg.nav_href and pkg.nav_href not in seen and pkg.archive.has(pkg.nav_href):
        docs.append((pkg.nav_href, True))
    return docs


def extract_all_units(pkg: EpubPackage) -> list[tuple[int | None, ExtractedUnit]]:
    """All units of the book in reading order: (spine_index or None, unit)."""
    out: list[tuple[int | None, ExtractedUnit]] = []
    spine_index = {s.href: i for i, s in enumerate(pkg.spine)}
    for href, is_nav in content_documents(pkg):
        tree = parse_xml_tree(pkg.archive.get(href), name=href, html_entities=True)
        for u in extract_xhtml_units(tree.getroot(), href, nav=is_nav):
            out.append((spine_index.get(href), u))
    if pkg.ncx_href and pkg.archive.has(pkg.ncx_href):
        tree = parse_xml_tree(pkg.archive.get(pkg.ncx_href), name=pkg.ncx_href)
        out.extend((None, u) for u in extract_ncx_units(tree.getroot(), pkg.ncx_href))
    tree = parse_xml_tree(pkg.archive.get(pkg.opf_path), name=pkg.opf_path)
    out.extend((None, u) for u in extract_opf_units(tree.getroot(), pkg.opf_path))
    return out


def _apply(units: list[ExtractedUnit], patches: dict[tuple[str, str], TranslationPatch],
           result: ExportResult) -> int:
    applied = 0
    for u in units:
        if not u.translatable:
            continue
        patch = patches.get((u.resource_href, u.xpath))
        if patch is None:
            result.untranslated.append((u.resource_href, u.xpath))
            continue
        if patch.source_hash != u.source_hash:
            result.hash_mismatch.append(patch.unit_id)
            continue
        try:
            if u.kind in ("head_title", "ncx", "opf_title"):
                if "⟦" in patch.text:
                    raise ValueError("placeholder in plain-text unit")
                u.element.text = patch.text
            elif u.run_index is not None:
                rebuild_run(u.element, u.run_index, patch.text, u.inline_map)
            else:
                rebuild_block(u.element, patch.text, u.inline_map)
            applied += 1
        except ValueError as exc:
            result.rebuild_errors.append((patch.unit_id, str(exc)))
    return applied


def _serialize(tree: Any) -> bytes:
    return etree.tostring(tree, xml_declaration=True, encoding="utf-8")


def _set_lang(root: Any, lang: str) -> None:
    if root.get(XML_LANG) is not None:
        root.set(XML_LANG, lang)
    if root.get("lang") is not None:
        root.set("lang", lang)


def build_epub(pkg: EpubPackage, patches: dict[tuple[str, str], TranslationPatch], output: Path,
               *, lang: str = "zh-TW", set_language: bool = True) -> ExportResult:
    output = Path(output)
    if output.resolve() == pkg.path.resolve():
        raise ValueError("Refusing to overwrite the source EPUB")
    result = ExportResult(output)
    replaced: dict[str, bytes] = {}

    for href, is_nav in content_documents(pkg):
        tree = parse_xml_tree(pkg.archive.get(href), name=href, html_entities=True)
        units = extract_xhtml_units(tree.getroot(), href, nav=is_nav)
        n = _apply(units, patches, result)
        if n:
            if set_language:
                _set_lang(tree.getroot(), lang)
            replaced[href] = _serialize(tree)
            result.applied += n

    if pkg.ncx_href and pkg.archive.has(pkg.ncx_href):
        tree = parse_xml_tree(pkg.archive.get(pkg.ncx_href), name=pkg.ncx_href)
        n = _apply(extract_ncx_units(tree.getroot(), pkg.ncx_href), patches, result)
        if n:
            if set_language:
                _set_lang(tree.getroot(), lang)
            replaced[pkg.ncx_href] = _serialize(tree)
            result.applied += n

    tree = parse_xml_tree(pkg.archive.get(pkg.opf_path), name=pkg.opf_path)
    n = _apply(extract_opf_units(tree.getroot(), pkg.opf_path), patches, result)
    result.applied += n
    if set_language and result.applied:
        md = tree.getroot().find("opf:metadata", NS)
        langs = md.findall("dc:language", NS) if md is not None else []
        if langs:
            langs[0].text = lang
        if n or langs:
            replaced[pkg.opf_path] = _serialize(tree)
    elif n:
        replaced[pkg.opf_path] = _serialize(tree)

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".part")
    with zipfile.ZipFile(tmp, "w") as zf:
        zi = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, b"application/epub+zip")
        for e in pkg.archive.entries:
            if e.name == "mimetype":
                continue
            zi = zipfile.ZipInfo(e.name, date_time=e.date_time)
            zi.external_attr = e.external_attr
            if e.name in replaced:
                zi.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(zi, replaced[e.name])
            else:
                zi.compress_type = e.compress_type
                zf.writestr(zi, e.data)
    tmp.replace(output)
    result.modified_files = sorted(replaced)
    return result
