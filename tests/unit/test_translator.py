import pytest

from ncte.config import Config
from ncte.epub.units import InlineSpec
from ncte.errors import FatalAPIError, ModelOutputError
from ncte.llm.fake import FakeLLM
from ncte.llm.schemas import TranslationBatchOutput
from ncte.translation.translator import NullRecorder, Translator, UnitInput, parse_json_output

EM = {1: InlineSpec(1, "element", "{http://www.w3.org/1999/xhtml}em", {}, False, 0)}


def units(n=4):
    out = [UnitInput(f"u_{i}", "text", f"「彼は⟦1⟧走った⟦/1⟧」その{i}", f"「彼は走った」その{i}", EM) for i in range(n)]
    return out


def tr(fake, cfg=None):
    return Translator(fake, cfg or Config(), NullRecorder(), model="google/gemini-3.8-flash",
                      structured_mode="json_schema", estimate=lambda p, c: 0.0)


def test_happy_path():
    r = tr(FakeLLM()).translate(units(), {})
    assert set(r.ok) == {f"u_{i}" for i in range(4)} and not r.failed and r.requests == 1
    assert "⟦1⟧" in r.ok["u_0"].text


@pytest.mark.parametrize("fault", ["bad_json", "missing_id", "extra_id", "dup_id", "bad_placeholder", "empty",
                                   "untranslated"])
def test_single_fault_recovers_with_one_retry(fault):
    fake = FakeLLM(faults={"translate": [fault]})
    r = tr(fake).translate(units(), {})
    assert set(r.ok) == {f"u_{i}" for i in range(4)} and not r.failed
    assert len(fake.calls) == 2


def test_partial_retry_only_resends_bad_units():
    fake = FakeLLM(faults={"translate": ["bad_placeholder"]})
    tr(fake).translate(units(), {})
    assert len(fake.payloads[1]["units"]) == 1
    assert "上一次的輸出未通過程式驗證" in fake.calls[1].messages[0]["content"]


def test_fenced_json_repaired_locally():
    fake = FakeLLM(faults={"translate": ["fenced"]})
    r = tr(fake).translate(units(), {})
    assert len(r.ok) == 4 and len(fake.calls) == 1


def test_persistent_failure_splits_then_fails_single_unit():
    # every call drops the last id → batches split down to single units, which then succeed?
    # missing_id on a 1-unit batch removes the only unit → that unit fails.
    fake = FakeLLM(faults={"translate": ["missing_id"] * 50})
    r = tr(fake).translate(units(2), {})
    assert set(r.failed) == {"u_0", "u_1"} and not r.ok
    assert "unit_id mismatch" in r.failed["u_0"]


def test_never_maps_translation_to_wrong_unit():
    fake = FakeLLM(faults={"translate": ["extra_id", "extra_id", "ok", "ok"]})
    r = tr(fake).translate(units(2), {})
    assert "u_bogus" not in r.ok and set(r.ok) == {"u_0", "u_1"}


def test_fatal_propagates():
    with pytest.raises(FatalAPIError):
        tr(FakeLLM(faults={"translate": ["fatal"]})).translate(units(), {})


def test_strict_parse():
    with pytest.raises(ModelOutputError):
        parse_json_output("Sure! here: {}", TranslationBatchOutput)
    with pytest.raises(ModelOutputError):
        parse_json_output('{"translations": [{"unit_id": "a"}]}', TranslationBatchOutput)
    obj, st = parse_json_output('```json\n{"translations": []}\n```', TranslationBatchOutput)
    assert st == "repaired_fence"
