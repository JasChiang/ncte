"""3.3 (ADR-064): ``translation.read_through`` — the model lists, before translating, where later paragraphs of the
batch constrain how an earlier line is translated. A switch, off by default; the program only keeps what is listed."""

import json

from ncte.config import Config
from ncte.llm.fake import FakeLLM, default_translate
from ncte.llm.prompt_loader import load_prompt
from ncte.llm.schemas import TranslationBatchOutput, strict_json_schema
from ncte.translation.translator import NullRecorder, Translator, UnitInput


def units(n=3):
    return [UnitInput(f"u_{i:016x}", "text", f"「走った」その{i}", f"「走った」その{i}", {}) for i in range(n)]


def tr(fake, on):
    cfg = Config()
    cfg.translation.read_through = on
    return Translator(fake, cfg, NullRecorder(), model="google/gemini-3.8-flash", structured_mode="json_schema",
                      estimate=lambda p, c: 0.0)


def test_off_by_default_and_the_request_is_as_before():
    assert Config().translation.read_through is False
    fake = FakeLLM()
    r = tr(fake, False).translate(units(), {})
    assert len(r.ok) == 3
    req = fake.calls[0]
    assert req.json_schema == strict_json_schema(TranslationBatchOutput)
    assert list(req.json_schema["properties"]) == ["translations"]
    assert "read_through" not in fake.payloads[0]
    assert "read_through" not in req.messages[0]["content"]
    assert all(not u.read_through for u in r.ok.values())


def test_on_the_list_comes_before_the_translations_in_the_schema():
    fake = FakeLLM()
    r = tr(fake, True).translate(units(), {})
    assert len(r.ok) == 3  # the fake answers an empty list
    req = fake.calls[0]
    assert fake.payloads[0]["read_through"] is True
    props = list(req.json_schema["properties"])
    assert props == ["read_through", "translations"]
    assert "read_through" in req.json_schema["required"]
    assert req.json_schema["properties"]["read_through"]["type"] == "array"
    assert "讀整批時找到的地方寫出來" in req.messages[0]["content"]


def test_the_prompt_block_is_kept_only_when_asked():
    p = load_prompt("translate")
    base = {"units": [{"unit_id": "u", "source": "こんにちは"}], "glossary": []}
    off, on = p.render(base), p.render({**base, "read_through": True})
    assert "讀整批時找到的地方寫出來" not in off and "<!--" not in off
    assert "讀整批時找到的地方寫出來" in on and "<!--" not in on
    assert on.index("讀整批時找到的地方寫出來") > on.index("## 輸出格式")
    static, own = p.render_parts({**base, "read_through": True})
    assert "讀整批時找到的地方寫出來" not in static and "讀整批時找到的地方寫出來" in own  # not in the cached part of the prompt
    assert p.render_parts(base)[0] == static  # the shared rules do not change with the switch


def test_what_is_listed_is_kept_on_the_first_paragraph_unjudged():
    def answer(payload, req):
        code = {v: k for k, v in req.id_codes.items()}["u_0000000000000001"]  # the model writes the short code it was given
        return {"read_through": [f"{code}：後面的反駁限定了比較的是年紀", "二", "三", "四", "五", "六"],
                "translations": default_translate(payload, req)["translations"]}

    r = tr(FakeLLM(responders={"translate": answer}), True).translate(units(), {})
    first = r.ok["u_0000000000000000"].read_through
    assert first[0] == "u_0000000000000001：後面的反駁限定了比較的是年紀"  # the code is mapped back to the paragraph it named
    assert len(first) == 5  # at most five
    assert not r.ok["u_0000000000000001"].read_through and not r.ok["u_0000000000000002"].read_through
    assert r.ok["u_0000000000000000"].notes == []  # the model's own notes are not mixed with it


def test_an_agent_answer_with_the_list_is_accepted_when_the_task_asks_for_it(tmp_path):
    from ncte.agent.tasks import TaskQueue

    q = TaskQueue(tmp_path)
    ans = json.dumps({"read_through": [], "translations": [{"unit_id": "P1", "translated_text": "好"}]})
    asked = q.put(stage="translate", model="agent/model", schema=None, id_codes={},
                  messages=[{"role": "user", "content": json.dumps({"read_through": True, "units": []})}])
    assert q.submit(asked, ans) == []
    plain = q.put(stage="translate", model="agent/model", schema=None, id_codes={},
                  messages=[{"role": "user", "content": json.dumps({"units": []})}])
    assert q.submit(plain, ans)  # not asked for: the answer has a field the step does not take


def test_the_output_example_shows_the_object_the_switch_asks_for():
    """(e) The output example is conditional: with read_through on it shows the object with read_through first,
    otherwise the plain one; neither leaves a block marker, and the shared (cached) rules are the same either way."""
    p = load_prompt("translate")
    base = {"units": [{"unit_id": "u", "source": "こんにちは"}], "glossary": []}
    on_obj = '{"read_through": ["…"], "translations": [{"unit_id": "…", "translated_text": "…"}]}'
    off_obj = '{"translations": [{"unit_id": "…", "translated_text": "…"}]}'
    off, on = p.render(base), p.render({**base, "read_through": True})
    assert "<!--" not in off and "<!--" not in on
    assert on_obj in on and off_obj not in on.replace(on_obj, "")
    assert off_obj in off and on_obj not in off and '"read_through"' not in off
    s_off, own_off = p.render_parts(base)
    s_on, own_on = p.render_parts({**base, "read_through": True})
    assert s_off == s_on and "<!--" not in s_off + own_off + own_on
    assert '"read_through"' not in s_off and off_obj not in s_off  # the example is not in the cached part
    assert off_obj in own_off and on_obj in own_on and off_obj not in own_on.replace(on_obj, "")
