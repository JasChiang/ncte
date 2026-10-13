"""Name index and relationship map (spoiler-aware)."""

from typer.testing import CliRunner

from ncte.cli import app
from ncte.llm.fake import FakeLLM
from ncte.reports.codex import build_index, build_relations, index_markdown, relations_mermaid

from ..helpers import run_book
from ..scripted import RESPONDERS


def test_index_and_relations_respect_horizon(conn, cfg, fixtures):
    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    ch3 = conn.execute("SELECT start_ordinal FROM scenes WHERE book_id=? AND id='s_002_01'", (book.id,)).fetchone()[0]
    full = {e["name_ja"] for e in build_index(conn, book.id)}
    early = {e["name_ja"] for e in build_index(conn, book.id, (1.0, ch3))}
    assert {"ミナ", "カイト", "リリア"} <= full
    assert "リリア" not in early and {"ミナ", "カイト"} <= early
    mina = next(e for e in build_index(conn, book.id) if e["name_ja"] == "ミナ")
    assert mina["mentions"] == 4 and mina["first"][0] == 1.0
    rel = build_relations(conn, book.id, min_shared_scenes=1)
    lab = rel["labels"]
    edges = {(lab[f["subject"]], f["predicate"], lab[t]) for f in rel["relations"] for t in f["targets"]}
    assert ("海斗", "把銀之鑰交給", "米娜") in edges
    assert any({lab[e["a"]], lab[e["b"]]} == {"米娜", "海斗"} for e in rel["co_appearance"])
    early_rel = build_relations(conn, book.id, (1.0, ch3), min_shared_scenes=1)
    assert "莉莉亞" not in early_rel["labels"].values()
    assert "米娜" in index_markdown(build_index(conn, book.id), {1.0: "第1集"}, "t")
    assert relations_mermaid(rel).startswith("graph LR")


def test_codex_cli(tmp_path, fixtures):
    ws = tmp_path / "ws"
    epub = str(fixtures["basic"])
    r = CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    for args in (["index", epub], ["index", epub, "--format", "md", "--volume", "1"],
                 ["relations", epub], ["relations", epub, "--format", "md"], ["relations", epub, "--format", "json"]):
        out = r.invoke(app, ["-w", str(ws), *args])
        assert out.exit_code == 0, out.output
    assert list((ws / "reports").rglob("relations.html")) and list((ws / "reports").rglob("index.md"))
