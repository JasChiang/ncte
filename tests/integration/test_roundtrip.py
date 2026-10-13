"""M1 acceptance: safe parse + DOM round-trip for three self-made fixtures (+ EPUBCheck if available)."""

import zipfile

import pytest

from ncte.epub.parser import load_epub
from ncte.epub.reassemble import TranslationPatch, build_epub, extract_all_units
from ncte.epub.validate import validate_epub

from ..conftest import REPO_WORKSPACE, epubcheck_available

KEYS = ["basic", "media", "ruby"]


@pytest.mark.parametrize("key", KEYS)
def test_roundtrip_without_translation_is_byte_identical(fixtures, tmp_path, key):
    pkg = load_epub(fixtures[key])
    out = tmp_path / f"{key}.epub"
    res = build_epub(pkg, {}, out)
    assert res.applied == 0 and res.modified_files == []
    src, dst = zipfile.ZipFile(fixtures[key]), zipfile.ZipFile(out)
    assert dst.infolist()[0].filename == "mimetype" and dst.infolist()[0].compress_type == zipfile.ZIP_STORED
    for name in src.namelist():
        assert src.read(name) == dst.read(name), name
    rep = validate_epub(out, original=fixtures[key], run_epubcheck=False)
    assert rep.ok, rep.errors


@pytest.mark.parametrize("key", KEYS)
def test_roundtrip_through_dom_identity(fixtures, tmp_path, key):
    pkg = load_epub(fixtures[key])
    units = extract_all_units(pkg)
    patches = {(u.resource_href, u.xpath): TranslationPatch(u.plain_text if u.ruby_count else u.source_text,
                                                            u.source_hash, "x")
               for _, u in units if u.translatable and not u.ruby_count}
    out = tmp_path / f"{key}.epub"
    res = build_epub(pkg, patches, out)
    assert not res.rebuild_errors and not res.hash_mismatch
    rep = validate_epub(out, original=fixtures[key], run_epubcheck=False)
    assert rep.ok, rep.errors
    # same units and same text after re-extraction (ruby units were not patched)
    again = {(u.resource_href, u.xpath): u.source_text for _, u in extract_all_units(load_epub(out))}
    for _, u in units:
        assert again[(u.resource_href, u.xpath)] == u.source_text
    # reading order, images, links intact
    out_pkg = load_epub(out)
    assert [s.href for s in out_pkg.spine] == [s.href for s in pkg.spine]
    assert rep.stats["broken_references"] == 0
    for img in pkg.images():
        assert out_pkg.archive.get(img.href) == pkg.archive.get(img.href)


def test_translated_output_sets_language_and_keeps_links(fixtures, tmp_path):
    pkg = load_epub(fixtures["media"])
    patches = {}
    for _, u in extract_all_units(pkg):
        if u.translatable:
            patches[(u.resource_href, u.xpath)] = TranslationPatch("譯" + u.source_text.replace("の", "的"),
                                                                   u.source_hash, "x")
    out = tmp_path / "m.epub"
    build_epub(pkg, patches, out)
    z = zipfile.ZipFile(out)
    ch1 = z.read("item/Text/ch1.xhtml").decode()
    assert 'xml:lang="zh-TW"' in ch1 and 'href="ch2.xhtml#place"' in ch1 and "gaiji01.png" in ch1
    opf = z.read("item/standard.opf").decode()
    assert "<dc:language>zh-TW</dc:language>" in opf
    rep = validate_epub(out, original=fixtures["media"], run_epubcheck=False)
    assert rep.ok, rep.errors


def test_hash_mismatch_is_not_applied(fixtures, tmp_path):
    pkg = load_epub(fixtures["basic"])
    _, u = next((s, u) for s, u in extract_all_units(pkg) if u.kind == "text")
    res = build_epub(pkg, {(u.resource_href, u.xpath): TranslationPatch("錯", "sha256:stale", "uX")}, tmp_path / "o.epub")
    assert res.hash_mismatch == ["uX"] and res.applied == 0


def test_refuses_to_overwrite_source(fixtures):
    pkg = load_epub(fixtures["basic"])
    with pytest.raises(ValueError):
        build_epub(pkg, {}, fixtures["basic"])


def test_validator_detects_broken_output(fixtures, tmp_path):
    src = zipfile.ZipFile(fixtures["media"])
    bad = tmp_path / "bad.epub"
    with zipfile.ZipFile(bad, "w") as z:
        for i in src.infolist():
            if i.filename.endswith("illust01.png"):
                continue
            z.writestr(i, src.read(i.filename), compress_type=zipfile.ZIP_DEFLATED)
    rep = validate_epub(bad, original=fixtures["media"], run_epubcheck=False)
    assert not rep.ok
    joined = " ".join(rep.errors)
    assert "compressed" in joined and "illust01.png" in joined


@pytest.mark.skipif(not epubcheck_available(), reason="EPUBCheck not installed")
@pytest.mark.parametrize("key", KEYS)
def test_epubcheck_source_and_output(fixtures, tmp_path, key):
    pkg = load_epub(fixtures[key])
    patches = {(u.resource_href, u.xpath): TranslationPatch(u.source_text, u.source_hash, "x")
               for _, u in extract_all_units(pkg) if u.translatable}
    out = tmp_path / f"{key}.epub"
    build_epub(pkg, patches, out)
    for path in (fixtures[key], out):
        rep = validate_epub(path, workspace=REPO_WORKSPACE)
        assert rep.epubcheck["status"] == "passed", rep.epubcheck.get("output")
