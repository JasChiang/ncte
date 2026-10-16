"""Output EPUB validation (spec §4.3): internal minimum checks + optional EPUBCheck."""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..errors import NCTEError
from .parser import XHTML_TYPES, EpubPackage, load_epub, resolve_href
from .reassemble import extract_all_units
from .security import parse_xml_tree

REF_ATTRS = [("a", "href"), ("img", "src"), ("link", "href"), ("source", "src"), ("video", "src"),
             ("audio", "src"), ("image", "{http://www.w3.org/1999/xlink}href"), ("image", "href"),
             ("use", "{http://www.w3.org/1999/xlink}href")]


@dataclass
class ValidationReport:
    path: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    epubcheck: dict[str, Any] = field(default_factory=lambda: {"status": "not_run"})

    @property
    def ok(self) -> bool:
        return not self.errors and self.epubcheck.get("status") != "failed"

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "ok": self.ok, "errors": self.errors, "warnings": self.warnings,
                "stats": self.stats, "epubcheck": self.epubcheck}


def _ids(tree: Any) -> set[str]:
    return {el.get("id") for el in tree.iter() if isinstance(el.tag, str) and el.get("id")} | {
        el.get("name") for el in tree.iter() if isinstance(el.tag, str) and el.get("name")}


def validate_epub(path: str | Path, *, original: str | Path | None = None,
                  epubcheck_path: str | None = None, run_epubcheck: bool = True,
                  workspace: Path | None = None) -> ValidationReport:
    path = Path(path)
    rep = ValidationReport(str(path))
    # --- container level
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if not infos or infos[0].filename != "mimetype":
                rep.errors.append("first ZIP entry is not 'mimetype'")
            else:
                if infos[0].compress_type != zipfile.ZIP_STORED:
                    rep.errors.append("'mimetype' entry is compressed")
                if zf.read("mimetype") != b"application/epub+zip":
                    rep.errors.append("'mimetype' content is not exactly application/epub+zip")
                if infos[0].extra:
                    rep.warnings.append("'mimetype' entry has extra field")
    except zipfile.BadZipFile as exc:
        rep.errors.append(f"not a ZIP file: {exc}")
        return rep
    try:
        pkg = load_epub(path)
    except NCTEError as exc:
        rep.errors.append(f"package error: {exc}")
        return rep
    for w in pkg.warnings:
        (rep.errors if "missing" in w else rep.warnings).append(w)

    # --- documents and references
    xhtml_items = [m for m in pkg.manifest.values() if m.media_type in XHTML_TYPES]
    trees: dict[str, Any] = {}
    for m in xhtml_items:
        if not pkg.archive.has(m.href):
            continue
        try:
            trees[m.href] = parse_xml_tree(pkg.archive.get(m.href), name=m.href, html_entities=True)
        except NCTEError as exc:
            rep.errors.append(str(exc))
    id_cache = {h: _ids(t) for h, t in trees.items()}
    refs = broken = 0
    for href, tree in trees.items():
        for el in tree.iter():
            if not isinstance(el.tag, str):
                continue
            lname = el.tag.rsplit("}", 1)[-1]
            for tag, attr in REF_ATTRS:
                if lname != tag:
                    continue
                val = el.get(attr)
                if not val or urlparse(val).scheme or val.startswith("//"):
                    continue
                refs += 1
                target = resolve_href(href, val)
                frag = val.split("#", 1)[1] if "#" in val else None
                if not pkg.archive.has(target):
                    broken += 1
                    rep.errors.append(f"{href}: broken reference {val!r}")
                elif frag and target in id_cache and frag not in id_cache[target]:
                    broken += 1
                    rep.errors.append(f"{href}: missing anchor {val!r}")
    if pkg.ncx_href and pkg.archive.has(pkg.ncx_href):
        ncx = parse_xml_tree(pkg.archive.get(pkg.ncx_href), name=pkg.ncx_href)
        for c in ncx.iter("{*}content"):
            src = c.get("src", "")
            refs += 1
            target = resolve_href(pkg.ncx_href, src)
            if not pkg.archive.has(target):
                broken += 1
                rep.errors.append(f"NCX: broken reference {src!r}")
    units = extract_all_units(pkg)
    rep.stats.update({
        "spine_items": len(pkg.spine), "manifest_items": len(pkg.manifest), "images": len(pkg.images()),
        "internal_references": refs, "broken_references": broken,
        "units": len(units), "translatable_units": sum(1 for _, u in units if u.translatable),
    })

    if original:
        _compare(pkg, load_epub(original), rep)

    if run_epubcheck:
        rep.epubcheck = run_epubcheck_cli(path, epubcheck_path, workspace)
        if rep.epubcheck["status"] == "not_available":
            rep.warnings.append("尚未經 EPUBCheck 驗證 (EPUBCheck not installed)")
    return rep


def _compare(out: EpubPackage, src: EpubPackage, rep: ValidationReport) -> None:
    a, b = set(src.archive.names()), set(out.archive.names())
    missing = sorted(a - b - {"mimetype"})
    extra = sorted(b - a - {"mimetype"})
    if missing:
        rep.errors.append(f"resources lost vs original: {missing[:20]}")
    if extra:
        rep.warnings.append(f"resources added vs original: {extra[:20]}")
    if [s.href for s in src.spine] != [s.href for s in out.spine]:
        rep.errors.append("spine reading order differs from original")
    rewritable = {m.href for m in src.manifest.values() if m.media_type in XHTML_TYPES}
    rewritable |= {src.opf_path}
    if src.ncx_href:
        rewritable.add(src.ncx_href)
    changed_binary = [n for n in a & b if n not in rewritable and n != "mimetype"
                      and src.archive.get(n) != out.archive.get(n)]
    if changed_binary:
        rep.errors.append(f"non-text resources changed: {sorted(changed_binary)[:20]}")
    if len(src.images()) != len(out.images()):
        rep.errors.append(f"image count changed {len(src.images())} → {len(out.images())}")
    # every paragraph is still there: counted whether or not it has letters (「──っ」 translated 「──！」 is punctuation
    # only in the output, so a count of translatable paragraphs would differ by one with nothing lost)
    su = len(extract_all_units(src))
    ou = rep.stats.get("units", 0)
    rep.stats["original_units"] = su
    if su != ou:
        rep.warnings.append(f"paragraph count changed {su} → {ou}")


def find_epubcheck(configured: str | None = None, workspace: Path | None = None) -> list[str] | None:
    if configured:
        p = Path(configured).expanduser()
        if p.suffix == ".jar":
            java = _find_java()
            return [java, "-jar", str(p)] if java else None
        return [str(p)]
    exe = shutil.which("epubcheck")
    if exe:
        return [exe]
    env_jar = os.environ.get("EPUBCHECK_JAR")
    jars = [env_jar] if env_jar else []
    roots = [workspace / "tools"] if workspace else []
    roots.append(Path("workspace/tools"))
    for root in roots:
        jars += sorted(glob.glob(str(root / "**" / "epubcheck.jar"), recursive=True))
    for jar in jars:
        if jar and Path(jar).exists():
            java = _find_java()
            if java:
                return [java, "-jar", jar]
    return None


def _find_java() -> str | None:
    for cand in [os.environ.get("JAVA_HOME", "") + "/bin/java", "/opt/homebrew/opt/openjdk/bin/java",
                 "/opt/homebrew/opt/openjdk@17/bin/java", "/usr/local/opt/openjdk/bin/java"]:
        if cand and Path(cand).exists():
            return cand
    java = shutil.which("java")
    if java:
        probe = subprocess.run([java, "-version"], capture_output=True, text=True)
        if probe.returncode == 0:
            return java
    return None


def run_epubcheck_cli(path: Path, configured: str | None = None, workspace: Path | None = None) -> dict[str, Any]:
    cmd = find_epubcheck(configured, workspace)
    if not cmd:
        return {"status": "not_available"}
    try:
        proc = subprocess.run([*cmd, str(path)], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "output": str(exc)}
    out = (proc.stdout + "\n" + proc.stderr).strip()
    m = re.search(r"Messages:\s*(\d+)\s*fatals?\s*/\s*(\d+)\s*errors?\s*/\s*(\d+)\s*warnings?", out)
    counts = {"fatal": int(m.group(1)), "error": int(m.group(2)), "warning": int(m.group(3))} if m else {}
    status = "passed" if proc.returncode == 0 else "failed"
    return {"status": status, "returncode": proc.returncode, "counts": counts,
            "command": " ".join(cmd[:1] + ["…"]), "output": out[-4000:]}
