"""ADR-022: series style guide."""

import json

from typer.testing import CliRunner

from ncte.cli import app
from ncte.llm.fake import FakeLLM
from ncte.memory.style import SeriesStyle, load_style, save_style, style_payload

from ..helpers import run_book
from ..scripted import RESPONDERS


def test_default_style_adds_nothing():
    assert style_payload(SeriesStyle(), "ミナ") is None


def test_payload_filters_voices():
    st = SeriesStyle(honorifics="omit", rules=["先輩一律譯「學姊」"], voices={"ミナ": "有禮貌的敬語", "リリア": "王族口吻"})
    p = style_payload(st, "「カイト、遅いですよ」とミナ")
    assert p["rules"] == ["先輩一律譯「學姊」"] and p["voices"] == {"ミナ": "有禮貌的敬語"} and "省略" in p["honorifics"]
    assert style_payload(st, "無名", ["リリア"])["voices"] == {"リリア": "王族口吻"}


def test_style_reaches_every_volume_request(conn, cfg, fixtures):
    from ncte.library import import_book

    book, _ = import_book(conn, cfg, fixtures["basic"])
    save_style(conn, book.id, SeriesStyle(rules=["保留語尾「ですよ」的禮貌感"], voices={"カイト": "粗魯的男生口吻"}))
    assert load_style(conn, book.id).rules == ["保留語尾「ですよ」的禮貌感"]
    fake = FakeLLM(responders=RESPONDERS)
    run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    payloads = [json.loads(c.messages[1]["content"]) for c in fake.calls if c.stage.startswith("translate")]
    body = [p for p in payloads if "known_headings" not in p]
    assert body and all(p["style"]["rules"] == ["保留語尾「ですよ」的禮貌感"] for p in body)
    # カイト's voice note reaches batches where カイト is named or present in the scene
    assert any(p["style"].get("voices") == {"カイト": "粗魯的男生口吻"} for p in body)


def test_style_cli(tmp_path, fixtures):
    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "import", epub]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "style", "set", epub, "--honorifics", "explicit", "--rule", "A", "--rule", "B",
                         "--voice", "ミナ=禮貌"])
    assert out.exit_code == 0, out.output
    assert r.invoke(app, ["-w", str(ws), "style", "set", epub, "--remove-rule", "1"]).exit_code == 0
    data = json.loads(r.invoke(app, ["-w", str(ws), "style", "show", epub, "--json"]).output)
    assert data == {"honorifics": "explicit", "rules": ["B"], "voices": {"ミナ": "禮貌"}}
    bad = r.invoke(app, ["-w", str(ws), "style", "set", epub, "--honorifics", "polite"])
    assert bad.exit_code != 0 and "invalid style" in str(bad.exception)
