"""CLI behaviour; commands that do not need an API key must work without one."""

import json

from typer.testing import CliRunner

from ncte import __version__
from ncte.cli import app

runner = CliRunner()


def invoke(ws, *args):
    return runner.invoke(app, ["-w", str(ws), *args])


def test_version_and_help(tmp_path):
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and __version__ in r.output
    for cmd in ["import", "inspect", "models", "translate", "status", "resume", "export", "validate", "report",
                "retranslate"]:
        r = runner.invoke(app, [cmd, "--help"])
        assert r.exit_code == 0, (cmd, r.output)


def test_offline_commands_without_key(tmp_path, fixtures):
    ws = tmp_path / "ws"
    epub = str(fixtures["media"])
    r = invoke(ws, "import", epub)
    assert r.exit_code == 0 and "imported" in r.output
    r = invoke(ws, "inspect", epub, "--json")
    info = json.loads(r.output)
    assert info["images"] == 3 and info["imported_as"].startswith("b_")
    assert invoke(ws, "status", epub).exit_code == 0
    r = invoke(ws, "validate", epub, "--no-epubcheck")
    assert r.exit_code == 0, r.output
    r = invoke(ws, "translate", epub, "--dry-run", "--fake")
    assert r.exit_code == 0 and "DRY RUN" in r.output


def test_translate_requires_key(tmp_path, fixtures):
    r = invoke(tmp_path / "ws", "translate", str(fixtures["basic"]))
    assert r.exit_code != 0
    assert "OPENROUTER_API_KEY" in str(r.exception)


def test_full_fake_flow_and_reports(tmp_path, fixtures):
    ws = tmp_path / "ws"
    epub = str(fixtures["ruby"])
    r = invoke(ws, "translate", epub, "--fake", "--json")
    assert r.exit_code == 0, r.output
    run_id = json.loads(r.output[r.output.index("{"):])["run_id"]
    r = invoke(ws, "export", epub, "--no-epubcheck")
    assert r.exit_code == 0, r.output
    out = ws / "exports" / "ruby_notes.zh-TW.epub"
    assert out.exists()
    r = invoke(ws, "validate", str(out), "--original", epub, "--no-epubcheck")
    assert r.exit_code == 0, r.output
    for fmt in ("html", "json"):
        r = invoke(ws, "report", "--run-id", run_id, "--format", fmt)
        assert r.exit_code == 0, r.output
    html = (ws / "reports").rglob("*.html")
    assert any("NCTE report" in p.read_text() for p in html)


def test_export_never_overwrites_source(tmp_path, fixtures):
    ws = tmp_path / "ws"
    epub = str(fixtures["basic"])
    invoke(ws, "import", epub)
    r = invoke(ws, "export", epub, "-o", epub)
    assert r.exit_code != 0
