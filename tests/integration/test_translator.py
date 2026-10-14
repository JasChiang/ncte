

def test_temperature_omitted_when_model_does_not_accept_it(conn, cfg, fixtures):
    from ncte.llm.fake import FakeLLM

    from ..helpers import run_book

    fake = FakeLLM(temperature=False)
    _, _, s = run_book(conn, cfg, fixtures["basic"], fake)
    assert s.status == "completed"
    assert fake.calls and all(c.temperature is None for c in fake.calls)
    assert any("does not accept temperature" in n for n in s.notes)
