"""Versioned prompt files (spec §6): ``prompts/<task>/v<N>.md`` with a small front matter.

The effective prompt version string is ``<task>/v<N>#<sha8>`` so that editing a prompt file
invalidates cached translations even if the author forgets to bump the version number.
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

TASKS = ("translate", "scene_analyze", "memory_update", "naming", "review", "verify", "scenes", "reader", "relations",
         "confirm", "impressions", "proofread", "house_style")


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
        carry are left out, so the model is not asked to keep them in mind. Without a payload every block is kept."""
        if payload is None:
            return _BLOCK.sub(lambda m: m.group(2), self.text)
        blob = json.dumps(payload, ensure_ascii=False)

        def keep(m: re.Match[str]) -> str:
            for cond in m.group(1).split("|"):
                cond = cond.strip()
                if cond.startswith("text:") and cond[5:] in blob or f'"{cond}"' in blob:
                    return m.group(2)
            return ""

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
        kept = []
        for m in _BLOCK.finditer(self.text):
            conds = [c.strip() for c in m.group(1).split("|")]
            if any((c.startswith("text:") and c[5:] in blob) or f'"{c}"' in blob for c in conds):
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


_FM = re.compile(r"^---\n(.*?)\n---\n", re.S)
_BLOCK = re.compile(r"<!-- if: *([^>]*?) *-->\n?(.*?)<!-- endif -->\n?", re.S)


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
        raw = f.read_text(encoding="utf-8")
        m = _FM.match(raw)
        body = raw[m.end():] if m else raw
        ver = int(f.stem[1:])
        return Prompt(task, ver, body.strip() + "\n", hashlib.sha256(raw.encode()).hexdigest()[:8])
    raise ConfigError(f"prompt file for task '{task}' not found in {[str(d) for d in prompts_dirs()]}")
