"""Safe ZIP / XML handling (spec §4.1, §12).

* Zip Slip: reject absolute paths, ``..`` segments, backslashes, drive letters, NUL bytes.
* Zip Bomb: limits on entry count, per-entry size, total expanded size and compression ratio.
  Sizes are verified while actually decompressing, not only trusted from the central directory.
* DRM: encrypted ZIP entries or ``META-INF/encryption.xml`` entries other than font obfuscation
  are rejected.
* XML: lxml parser with entity resolution, DTD loading and network access disabled (XXE).
"""

from __future__ import annotations

import html.entities
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from lxml import etree

from ..errors import EpubSecurityError, EpubStructureError, UnsupportedEpubError


@dataclass(frozen=True)
class ZipLimits:
    max_entries: int = 20_000
    max_entry_bytes: int = 300 * 1024 * 1024
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_ratio: float = 200.0
    ratio_min_bytes: int = 1 * 1024 * 1024  # ratio only checked for entries that expand beyond this


FONT_OBFUSCATION_ALGORITHMS = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}


@dataclass
class ZipEntry:
    name: str
    data: bytes
    compress_type: int
    date_time: tuple[int, int, int, int, int, int]
    external_attr: int = 0


@dataclass
class SafeArchive:
    entries: list[ZipEntry] = field(default_factory=list)

    def names(self) -> list[str]:
        return [e.name for e in self.entries]

    def get(self, name: str) -> bytes:
        for e in self.entries:
            if e.name == name:
                return e.data
        raise KeyError(name)

    def has(self, name: str) -> bool:
        return any(e.name == name for e in self.entries)

    def as_dict(self) -> dict[str, bytes]:
        return {e.name: e.data for e in self.entries}


_DRIVE = re.compile(r"^[A-Za-z]:")


def check_member_name(name: str) -> None:
    if not name or "\x00" in name:
        raise EpubSecurityError(f"Illegal empty/NUL entry name: {name!r}")
    if "\\" in name:
        raise EpubSecurityError(f"Backslash in entry name (possible path traversal): {name!r}")
    if name.startswith("/") or _DRIVE.match(name):
        raise EpubSecurityError(f"Absolute entry path rejected: {name!r}")
    parts = PurePosixPath(name).parts
    if any(p == ".." for p in parts):
        raise EpubSecurityError(f"Path traversal entry rejected: {name!r}")


def read_epub_archive(path: str | Path, limits: ZipLimits | None = None) -> SafeArchive:
    limits = limits or ZipLimits()
    p = Path(path)
    if not p.is_file():
        raise EpubSecurityError(f"Not a file: {p}")
    try:
        zf = zipfile.ZipFile(p)
    except zipfile.BadZipFile as exc:
        raise EpubSecurityError(f"Not a valid ZIP/EPUB archive: {exc}") from exc
    archive = SafeArchive()
    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_entries:
            raise EpubSecurityError(f"Too many entries ({len(infos)} > {limits.max_entries})")
        seen: set[str] = set()
        total = 0
        for info in infos:
            name = info.filename
            check_member_name(name)
            if name in seen:
                raise EpubSecurityError(f"Duplicate entry name: {name!r}")
            seen.add(name)
            if info.flag_bits & 0x1:
                raise UnsupportedEpubError(f"Encrypted ZIP entry (DRM?) not supported: {name}")
            if info.is_dir():
                continue
            if info.file_size > limits.max_entry_bytes:
                raise EpubSecurityError(f"Entry too large: {name} ({info.file_size} bytes)")
            data = _read_limited(zf, info, limits)
            total += len(data)
            if total > limits.max_total_bytes:
                raise EpubSecurityError("Total expanded size exceeds limit (zip bomb?)")
            if len(data) >= limits.ratio_min_bytes and info.compress_size > 0:
                ratio = len(data) / info.compress_size
                if ratio > limits.max_ratio:
                    raise EpubSecurityError(f"Suspicious compression ratio {ratio:.0f}:1 for {name} (zip bomb?)")
            archive.entries.append(
                ZipEntry(
                    name=name,
                    data=data,
                    compress_type=info.compress_type,
                    date_time=info.date_time,
                    external_attr=info.external_attr,
                )
            )
    _check_encryption(archive)
    return archive


def _read_limited(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limits: ZipLimits) -> bytes:
    chunks: list[bytes] = []
    size = 0
    with zf.open(info) as fh:
        while True:
            chunk = fh.read(1 << 16)
            if not chunk:
                break
            size += len(chunk)
            if size > limits.max_entry_bytes:
                raise EpubSecurityError(f"Entry {info.filename} expands beyond limit (zip bomb?)")
            chunks.append(chunk)
    return b"".join(chunks)


def _check_encryption(archive: SafeArchive) -> None:
    if not archive.has("META-INF/encryption.xml"):
        return
    root = parse_xml(archive.get("META-INF/encryption.xml"), name="META-INF/encryption.xml")
    for method in root.iter("{*}EncryptionMethod"):
        algo = method.get("Algorithm", "")
        if algo not in FONT_OBFUSCATION_ALGORITHMS:
            raise UnsupportedEpubError(
                f"EPUB contains encrypted resources (Algorithm={algo}); DRM-protected books are not supported."
            )


def _parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_blank_text=False,
        strip_cdata=False,
    )


_NAMED_ENTITY = re.compile(rb"&([A-Za-z][A-Za-z0-9]*);")
_XML_ENTITIES = {b"amp", b"lt", b"gt", b"quot", b"apos"}
_ENTITY_DECL = re.compile(rb"<!ENTITY", re.IGNORECASE)


def _numeric_entities(data: bytes) -> bytes:
    """Replace HTML named entities (``&nbsp;`` …) with numeric references so the XHTML parses
    without loading a DTD. Unknown names are left alone (and will fail parsing loudly)."""

    def repl(m: re.Match[bytes]) -> bytes:
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        cp = html.entities.name2codepoint.get(name.decode("ascii"))
        if cp is None:
            return m.group(0)
        return b"&#%d;" % cp

    return _NAMED_ENTITY.sub(repl, data)


def parse_xml(data: bytes, *, name: str = "<xml>", html_entities: bool = False) -> etree._Element:
    return parse_xml_tree(data, name=name, html_entities=html_entities).getroot()


def parse_xml_tree(data: bytes, *, name: str = "<xml>", html_entities: bool = False) -> etree._ElementTree:
    if _ENTITY_DECL.search(data):
        # Internal/external entity declarations are never needed in EPUB content and are the
        # vector for XXE / billion-laughs. Refuse instead of guessing.
        raise EpubSecurityError(f"{name}: DTD entity declarations are not allowed")
    if html_entities:
        data = _numeric_entities(data)
    try:
        return etree.ElementTree(etree.fromstring(data, _parser())).getroot().getroottree()
    except etree.XMLSyntaxError as exc:
        raise EpubStructureError(f"{name}: XML parse error: {exc}") from exc
