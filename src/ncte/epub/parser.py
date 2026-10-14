"""OCF / OPF / NAV / NCX parsing (spec §4.1).

The package path is always discovered through ``META-INF/container.xml``; nothing assumes
``OEBPS/content.opf``.
"""

from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urldefrag

from lxml import etree

from ..errors import EpubStructureError, UnsupportedEpubError
from .security import SafeArchive, ZipLimits, parse_xml, read_epub_archive

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
}

XHTML_TYPES = {"application/xhtml+xml", "text/html"}
IMAGE_PREFIX = "image/"


@dataclass
class ManifestItem:
    id: str
    href: str  # zip path (resolved relative to the OPF, percent-decoded)
    raw_href: str
    media_type: str
    properties: set[str] = field(default_factory=set)


@dataclass
class SpineItem:
    idref: str
    href: str
    linear: bool = True


@dataclass
class EpubPackage:
    path: Path
    archive: SafeArchive
    sha256: str
    opf_path: str
    version: str
    metadata: dict[str, list[str]]
    manifest: dict[str, ManifestItem]
    spine: list[SpineItem]
    nav_href: str | None
    ncx_href: str | None
    page_progression: str | None = None
    warnings: list[str] = field(default_factory=list)
    fixed_layout: set[str] = field(default_factory=set)  # pre-paginated pages inside a reflowable book (kept as is)

    @property
    def opf_dir(self) -> str:
        return posixpath.dirname(self.opf_path)

    @property
    def title(self) -> str:
        titles = self.metadata.get("title") or [self.path.stem]
        return titles[0]

    @property
    def language(self) -> str:
        langs = self.metadata.get("language") or [""]
        return langs[0]

    def item_by_href(self, href: str) -> ManifestItem | None:
        for item in self.manifest.values():
            if item.href == href:
                return item
        return None

    def images(self) -> list[ManifestItem]:
        return [m for m in self.manifest.values() if m.media_type.startswith(IMAGE_PREFIX)]


def resolve_href(base_file: str, href: str) -> str:
    """Resolve a (percent-encoded, possibly fragment-bearing) href relative to a zip path."""
    path, _frag = urldefrag(href)
    path = unquote(path)
    if not path:
        return base_file
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base_file), path))
    return joined.lstrip("/")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_epub(path: str | Path, limits: ZipLimits | None = None) -> EpubPackage:
    p = Path(path)
    archive = read_epub_archive(p, limits)
    return parse_package(p, archive, sha256_file(p))


def parse_package(path: Path, archive: SafeArchive, sha256: str) -> EpubPackage:
    warnings: list[str] = []
    if not archive.has("mimetype"):
        warnings.append("mimetype file missing")
    elif archive.get("mimetype").strip() != b"application/epub+zip":
        warnings.append("mimetype content is not application/epub+zip")
    if not archive.has("META-INF/container.xml"):
        raise EpubStructureError("META-INF/container.xml not found")
    container = parse_xml(archive.get("META-INF/container.xml"), name="container.xml")
    rootfiles = container.findall(".//c:rootfile", NS)
    opf_paths: list[str] = [
        str(rf.get("full-path")) for rf in rootfiles
        if rf.get("full-path") and rf.get("media-type") in (None, "application/oebps-package+xml")
    ]
    if not opf_paths:
        raise EpubStructureError("container.xml has no OPF rootfile")
    opf_path = unquote(opf_paths[0])
    if len(opf_paths) > 1:
        warnings.append(f"multiple renditions; using the first ({opf_path})")
    if not archive.has(opf_path):
        raise EpubStructureError(f"OPF not found in archive: {opf_path}")
    opf = parse_xml(archive.get(opf_path), name=opf_path)
    version = opf.get("version", "")

    metadata: dict[str, list[str]] = {}
    md = opf.find("opf:metadata", NS)
    if md is None:
        raise EpubStructureError("OPF has no <metadata>")
    layout = None
    page_progression = None
    for el in md:
        if not isinstance(el.tag, str):
            continue
        qn = etree.QName(el)
        if qn.namespace == NS["dc"]:
            metadata.setdefault(qn.localname, []).append((el.text or "").strip())
        elif qn.localname == "meta" and el.get("property") == "rendition:layout":
            layout = (el.text or "").strip()
    if layout == "pre-paginated":
        raise UnsupportedEpubError("Fixed-layout (pre-paginated) EPUBs are not supported by NCTE")

    manifest: dict[str, ManifestItem] = {}
    man = opf.find("opf:manifest", NS)
    if man is None:
        raise EpubStructureError("OPF has no <manifest>")
    for it in man.findall("opf:item", NS):
        iid, href, mt = it.get("id"), it.get("href"), it.get("media-type")
        if not iid or not href or not mt:
            warnings.append(f"manifest item missing id/href/media-type: {etree.tostring(it)[:120]!r}")
            continue
        props = set((it.get("properties") or "").split())
        manifest[iid] = ManifestItem(iid, resolve_href(opf_path, href), href, mt, props)

    spine_el = opf.find("opf:spine", NS)
    if spine_el is None:
        raise EpubStructureError("OPF has no <spine>")
    page_progression = spine_el.get("page-progression-direction")
    spine: list[SpineItem] = []
    fixed: set[str] = set()
    for ref in spine_el.findall("opf:itemref", NS):
        idref = ref.get("idref", "")
        item = manifest.get(idref)
        if item is None:
            raise EpubStructureError(f"spine itemref '{idref}' not in manifest")
        if not archive.has(item.href):
            raise EpubStructureError(f"spine item file missing from archive: {item.href}")
        props = set((ref.get("properties") or "").split())
        if "rendition:layout-pre-paginated" in props or "rendition:layout-pre-paginated" in item.properties:
            # light novels often have fixed-layout colour plates in a reflowable book: keep those pages untouched
            fixed.add(item.href)
        spine.append(SpineItem(idref, item.href, ref.get("linear", "yes") != "no"))
    if not spine:
        raise EpubStructureError("spine is empty")
    if fixed and len(fixed) == len(spine):
        raise UnsupportedEpubError("Fixed-layout (pre-paginated) EPUBs are not supported by NCTE")
    if fixed:
        warnings.append(f"{len(fixed)} fixed-layout page(s) (e.g. colour plates) are kept untranslated")

    nav_href = next((m.href for m in manifest.values() if "nav" in m.properties), None)
    ncx_href = None
    toc_id = spine_el.get("toc")
    if toc_id and toc_id in manifest:
        ncx_href = manifest[toc_id].href
    else:
        ncx_href = next((m.href for m in manifest.values() if m.media_type == "application/x-dtbncx+xml"), None)
    for m in manifest.values():
        if not archive.has(m.href) and not m.href.startswith(("http:", "https:")):
            warnings.append(f"manifest item file missing: {m.href}")
    return EpubPackage(
        path=path,
        archive=archive,
        sha256=sha256,
        opf_path=opf_path,
        version=version,
        metadata=metadata,
        manifest=manifest,
        spine=spine,
        nav_href=nav_href,
        ncx_href=ncx_href,
        page_progression=page_progression,
        warnings=warnings,
        fixed_layout=fixed,
    )
