"""B6 (ADR-067): a scene break the model marked as needing a second look is shown as such (web page and
`ncte scenes`), with its reason; nothing is cut again by itself."""

from ncte.llm.fake import FakeLLM, default_scene_windows

from ..helpers import run_book
from ..scripted import RESPONDERS


def unsure_all(payload, req):
    out = default_scene_windows(payload, req)
    for b in out["breaks"]:
        b["sure"], b["reason"] = False, "空行前後像同一件事"
    return out


def test_an_unsure_break_is_marked_on_the_story_page(conn, cfg, fixtures):
    from ncte.gui import data

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "scene_windows": unsure_all}),
                          mode="context")
    shown = data.story(conn, book)["scenes"]
    marked = [s for s in shown if s["unsure"]]
    assert marked and all(s["unsure"] == "空行前後像同一件事" for s in marked)
    assert any(not s["unsure"] for s in shown)  # a chapter's first scene starts at the chapter, not at a break



def test_the_scene_list_marks_an_unsure_break(tmp_path, fixtures, monkeypatch):
    import json

    from typer.testing import CliRunner

    import ncte.llm.fake as fake
    from ncte.cli import app

    monkeypatch.setitem(fake.DEFAULTS, "scene_windows", unsure_all)
    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    listed = json.loads(r.invoke(app, ["-w", str(ws), "scenes", epub, "--json"]).output)
    marked = [d for d in listed if "unsure" in d]
    assert marked and all(d["unsure"] == "空行前後像同一件事" for d in marked)
    out = r.invoke(app, ["-w", str(ws), "scenes", epub]).output
    assert out.count("[模型標需複核：空行前後像同一件事]") == len(marked)
