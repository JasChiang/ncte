from __future__ import annotations

import os
from pathlib import Path

import pytest

from ncte.config import Config
from ncte.memory.db import connect
from ncte.samples import write_fixture_set


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Offline tests must never see a real key nor a developer .env file.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("NCTE_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return write_fixture_set(tmp_path_factory.mktemp("fixtures"))


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config()
    c.app.workspace = str(tmp_path / "ws")
    c.openrouter.max_concurrency = 2
    return c


@pytest.fixture
def conn(cfg: Config):
    c = connect(cfg.workspace / "ncte.db")
    yield c
    c.close()


def epubcheck_available() -> bool:
    from ncte.epub.validate import find_epubcheck

    repo_ws = Path(__file__).resolve().parents[1] / "workspace"
    return find_epubcheck(None, repo_ws) is not None or bool(os.environ.get("EPUBCHECK_JAR"))


REPO_WORKSPACE = Path(__file__).resolve().parents[1] / "workspace"
