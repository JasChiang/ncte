"""Versioned prompt files (spec §6): ``prompts/<task>/v<N>.md`` with a small front matter.

The effective prompt version string is ``<task>/v<N>#<sha8>`` so that editing a prompt file
invalidates cached translations even if the author forgets to bump the version number.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..errors import ConfigError

TASKS = ("translate", "scene_analyze", "memory_update", "review")


@dataclass(frozen=True)
class Prompt:
    task: str
    version: int
    text: str
    sha8: str

    @property
    def version_id(self) -> str:
        return f"{self.task}/v{self.version}#{self.sha8}"


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
