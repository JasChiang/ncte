import zipfile
from pathlib import Path

import pytest

from ncte.epub.parser import load_epub
from ncte.epub.security import ZipLimits, check_member_name, parse_xml, read_epub_archive
from ncte.errors import EpubSecurityError, UnsupportedEpubError


@pytest.mark.parametrize("name", ["../x", "a/../../x", "/etc/passwd", "C:/x", "a\\b", "a\x00b", ""])
def test_bad_member_names(name):
    with pytest.raises(EpubSecurityError):
        check_member_name(name)


def test_good_member_name():
    check_member_name("OEBPS/Text/ch1.xhtml")


@pytest.mark.parametrize("key,exc", [("zip_slip", EpubSecurityError), ("zip_bomb", EpubSecurityError),
                                     ("xxe", EpubSecurityError), ("drm", UnsupportedEpubError),
                                     ("fixed_layout", UnsupportedEpubError)])
def test_malicious_fixtures_rejected(fixtures, key, exc):
    with pytest.raises(exc):
        load_epub(fixtures[key])


def test_entry_count_limit(fixtures):
    with pytest.raises(EpubSecurityError):
        read_epub_archive(fixtures["basic"], ZipLimits(max_entries=3))


def test_lying_header_size_is_caught(tmp_path: Path):
    # entry larger than limit is detected while decompressing
    p = tmp_path / "big.epub"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", b"application/epub+zip")
        zf.writestr("big.bin", b"a" * 2_000_000)
    with pytest.raises(EpubSecurityError):
        read_epub_archive(p, ZipLimits(max_entry_bytes=1_000_000))


def test_xxe_never_resolved():
    data = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>'
    with pytest.raises(EpubSecurityError):
        parse_xml(data)


def test_html_named_entities_converted():
    root = parse_xml(b"<p>a&nbsp;b&hellip;&amp;</p>", html_entities=True)
    assert root.text == "a\xa0b\u2026&"


def test_font_obfuscation_allowed(tmp_path: Path, fixtures):
    src = zipfile.ZipFile(fixtures["basic"])
    p = tmp_path / "font.epub"
    with zipfile.ZipFile(p, "w") as zf:
        for i in src.infolist():
            zf.writestr(i, src.read(i.filename))
        zf.writestr("META-INF/encryption.xml", b"""<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
<enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>
<enc:CipherData><enc:CipherReference URI="OEBPS/font.otf"/></enc:CipherData></enc:EncryptedData></encryption>""")
    load_epub(p)


def test_fixed_layout_pages_inside_a_reflowable_book_are_kept(tmp_path, fixtures):
    """Colour plates are often pre-paginated pages in an otherwise reflowable light novel: the book is accepted
    and only those pages are left untranslated."""
    import re
    import zipfile

    from ncte.epub.parser import load_epub
    from ncte.epub.reassemble import extract_all_units

    src, out = fixtures["basic"], tmp_path / "mixed.epub"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename.endswith(".opf"):
                data = re.sub(rb'<itemref idref="ch1"\s*/>', b'<itemref idref="ch1" properties="rendition:layout-pre-paginated"/>',
                              data)
            zout.writestr(info, data, compress_type=zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED)
    pkg = load_epub(out)
    assert len(pkg.fixed_layout) == 1 and any("fixed-layout" in w for w in pkg.warnings)
    units = [u for _, u in extract_all_units(pkg)]
    fixed = [u for u in units if u.resource_href in pkg.fixed_layout]
    assert fixed and all(not u.translatable for u in fixed) and any(u.skip_reason == "fixed_layout" for u in fixed)
    assert any(u.translatable for u in units if u.resource_href not in pkg.fixed_layout)
