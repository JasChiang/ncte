"""Versioned prompt files (spec §6): ``prompts/<task>/v<N>.md`` with a small front matter.

The effective prompt version string is ``<task>/v<N>#<sha8>`` so that editing a prompt file
invalidates cached translations even if the author forgets to bump the version number.

A rule several steps share is written once, in ``prompts/_shared/<name>.md`` (no front matter), and put in with
``<!-- include: <name> -->`` (a line of its own, or inside a sentence): the translator and the steps that check it read
the same words. Includes are expanded before the sha8 is taken, so editing a partial changes the version of every
prompt that includes it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..errors import ConfigError

TASKS = ("translate", "scene_analyze", "memory_update", "scene_read", "naming", "review", "verify", "reader",
         "relations",
         "confirm", "impressions", "proofread", "house_style", "fix_check", "scene_windows", "terms",
         "terms_candidates", "gender_gaps", "false_friends")


@dataclass(frozen=True)
class Prompt:
    task: str
    version: int
    text: str
    sha8: str

    @property
    def version_id(self) -> str:
        return f"{self.task}/v{self.version}#{self.sha8}"

    def render(self, payload: dict[str, Any] | None = None) -> str:
        """The prompt for one request: ``<!-- if: a|b|text:⟦ -->…<!-- endif -->`` blocks are kept only when the
        payload has key ``a`` or ``b`` (anywhere) or its text contains ``⟦`` — rules about data the request does not
        carry are left out, so the model is not asked to keep them in mind. ``<!-- if: !a -->`` is the other case:
        kept only when the payload does not have key ``a`` (an "otherwise" version of a block). Without a payload
        every block is kept."""
        if payload is None:
            return _BLOCK.sub(lambda m: m.group(2), self.text)
        blob = json.dumps(payload, ensure_ascii=False)
        keys = _filled_keys(payload)

        def keep(m: re.Match[str]) -> str:
            return m.group(2) if _applies(m.group(1), blob, keys) else ""

        out = _BLOCK.sub(keep, self.text)
        return re.sub(r"\n{3,}", "\n\n", out)

    def render_parts(self, payload: dict[str, Any]) -> tuple[str, str]:
        """(the rules every request shares, the rules this request's data needs). The shared part comes first so it
        can be cached across requests (ADR-043); the conditional blocks follow in their original order."""
        static = re.sub(r"\n{3,}", "\n\n", _BLOCK.sub("", self.text)).rstrip() + "\n"
        whole = self.render(payload)
        if whole.strip() == static.strip():
            return static, ""
        blob = json.dumps(payload, ensure_ascii=False)
        keys = _filled_keys(payload)
        kept = []
        for m in _BLOCK.finditer(self.text):
            if _applies(m.group(1), blob, keys):
                body = m.group(2).strip("\n")
                if not body.startswith("## "):  # a section heading of its own is folded into the one below
                    kept.append(body)
        return static, "## 本批資料的附加說明（輸入欄位與規則）\n" + "\n".join(kept) + "\n"


def prompts_dirs() -> list[Path]:
    dirs = []
    env = os.environ.get("NCTE_PROMPTS_DIR")
    if env:
        dirs.append(Path(env))
    here = Path(__file__).resolve()
    dirs.append(here.parent.parent / "prompts")  # installed wheel: ncte/prompts
    dirs.append(here.parents[3] / "prompts")  # source checkout: <repo>/prompts
    return dirs


def _filled_keys(payload: Any) -> set[str]:
    """The keys anywhere in the payload whose value carries something: an empty or missing value (null, "", [],
    {}, false) does not switch a block on — a request that sends an empty field has none of that data."""
    out: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if x is not None and x is not False and x != "" and x != [] and x != {}:
                    out.add(str(k))
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(payload)
    return out


def _applies(conditions: str, blob: str, keys: set[str]) -> bool:
    """Whether a block's ``a|b|text:⟦`` (any of them holds) or ``!a`` (key ``a`` absent or empty) applies to the
    payload. ``a`` holds when some key ``a`` in the payload has a value (``keys``, see ``_filled_keys``);
    ``text:⟦`` when the payload's text contains it."""
    for cond in conditions.split("|"):
        cond = cond.strip()
        if cond.startswith("!"):
            if cond[1:] not in keys:
                return True
        elif cond.startswith("text:"):
            if cond[5:] in blob:
                return True
        elif cond in keys:
            return True
    return False


_FM = re.compile(r"^---\n(.*?)\n---\n", re.S)
_INCLUDE = re.compile(r"<!-- include: *([A-Za-z0-9_-]+) *-->")
SHARED = "_shared"  # the partials' folder: not a task
_BLOCK = re.compile(r"<!-- if: *([^>]*?) *-->\n?(.*?)<!-- endif -->\n?", re.S)


def expand_includes(text: str, shared: Path) -> str:
    """``text`` with each ``<!-- include: name -->`` replaced by ``shared/name.md`` (its last newline dropped, so the
    directive's own line or sentence goes on as written). Partials do not include other partials."""

    def put(m: re.Match[str]) -> str:
        p = shared / f"{m.group(1)}.md"
        if not p.is_file():
            raise ConfigError(f"prompt include '{m.group(1)}' not found in {shared}")
        return p.read_text(encoding="utf-8").rstrip("\n")

    return _INCLUDE.sub(put, text)


def includes(task: str) -> set[str]:
    """The partials a task's prompt file includes (by name)."""
    for d in prompts_dirs():
        files = sorted((d / task).glob("v*.md"), key=lambda p: int(p.stem[1:]) if p.stem[1:].isdigit() else -1)
        if files:
            return set(_INCLUDE.findall(files[-1].read_text(encoding="utf-8")))
    raise ConfigError(f"prompt file for task '{task}' not found in {[str(d) for d in prompts_dirs()]}")


@lru_cache(maxsize=32)
def load_prompt(task: str, version: int | None = None) -> Prompt:
    if task not in TASKS:
        raise ConfigError(f"unknown prompt task {task}")
    for d in prompts_dirs():
        tdir = d / task
        if not tdir.is_dir():
            continue
        files = sorted(tdir.glob("v*.md"), key=lambda p: int(p.stem[1:]) if p.stem[1:].isdigit() else -1)
        if version is not None:
            files = [f for f in files if f.stem == f"v{version}"]
        if not files:
            continue
        f = files[-1]
        raw = expand_includes(f.read_text(encoding="utf-8"), d / SHARED)
        m = _FM.match(raw)
        body = raw[m.end():] if m else raw
        ver = int(f.stem[1:])
        return Prompt(task, ver, body.strip() + "\n", hashlib.sha256(raw.encode()).hexdigest()[:8])
    raise ConfigError(f"prompt file for task '{task}' not found in {[str(d) for d in prompts_dirs()]}")
