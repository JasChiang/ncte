

def test_temperature_omitted_when_model_does_not_accept_it(conn, cfg, fixtures):
    from ncte.llm.fake import FakeLLM

    from ..helpers import run_book

    fake = FakeLLM(temperature=False)
    _, _, s = run_book(conn, cfg, fixtures["basic"], fake)
    assert s.status == "completed"
    assert fake.calls and all(c.temperature is None for c in fake.calls)
    assert any("does not accept temperature" in n for n in s.notes)


def test_retranslating_one_unit_sees_its_scene(conn, cfg, fixtures):
    import json

    from ncte.llm.fake import FakeLLM

    from ..helpers import run_book

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(), mode="context")
    rows = conn.execute("SELECT id, scene_id, ordinal FROM units WHERE book_id=? AND kind IN ('text','heading') AND translatable=1 AND scene_id IS NOT NULL "
                        "ORDER BY ordinal", (book.id,)).fetchall()
    scene = max({r["scene_id"] for r in rows}, key=lambda s: sum(1 for r in rows if r["scene_id"] == s))
    members = [r for r in rows if r["scene_id"] == scene]
    target = members[len(members) // 2]
    fake = FakeLLM()
    run_book(conn, cfg, fixtures["basic"], fake, mode="context", unit_ids=[target["id"]], force=True)
    p = next(json.loads(c.messages[1]["content"]) for c in fake.calls if c.stage.startswith("translate"))
    around = {u["unit_id"] for u in p["context_before"] + p["context_after"]}
    assert [u["unit_id"] for u in p["units"]] == [target["id"]]
    assert around == {r["id"] for r in members} - {target["id"]}  # the whole (small) scene, nothing outside it


def test_content_block_retried_without_memory(conn, cfg, fixtures):
    """A filter that reacts to the memory sent along: the batch is retried with text, neighbours and glossary."""
    from ncte.errors import ContentBlockedError
    from ncte.llm.fake import FakeLLM, default_translate

    from ..helpers import run_book
    from ..scripted import RESPONDERS

    def picky(payload, req):
        if "memory" in payload or "characters" in payload:
            raise ContentBlockedError("HTTP 403 content filter: fake PROHIBITED_CONTENT")
        return default_translate(payload, req)

    _, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "translate": picky}),
                         mode="context")
    failed = conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=? AND stage='translate' AND status='failed'",
                          (rid,)).fetchone()[0]
    assert failed == 0 and s.status == "completed"
