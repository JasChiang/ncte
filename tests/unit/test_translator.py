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


class _CapClient:
    """Answers through FakeLLM but reports truncation until max_tokens reaches ``need``."""

    name = "fake"

    def __init__(self, need):
        self.inner, self.need, self.seen = FakeLLM(), need, []

    def complete(self, req):
        self.seen.append(req.max_tokens)
        resp = self.inner.complete(req)
        if req.max_tokens < self.need:
            resp.finish_reason = "length"
        return resp


def test_auto_max_tokens_bounds():
    from ncte.translation.translator import AUTO_MAX_TOKENS_CEIL, AUTO_MAX_TOKENS_FLOOR, auto_max_tokens

    assert auto_max_tokens("短い") == AUTO_MAX_TOKENS_FLOOR
    assert auto_max_tokens("あ" * 10_000) == 40_004
    assert auto_max_tokens("あ" * 100_000) == AUTO_MAX_TOKENS_CEIL


def test_high_reasoning_starts_with_room_to_think():
    """A fix check of 20 short items thought for up to 24,600 tokens: under the plain floor it was cut off and paid
    twice. High reasoning starts higher; a ``max:N`` setting adds its reasoning tokens to what the answer needs."""
    from ncte.translation.translator import (
        AUTO_MAX_TOKENS_CEIL,
        AUTO_MAX_TOKENS_FLOOR,
        HIGH_REASONING_FLOOR,
        auto_max_tokens,
    )

    assert auto_max_tokens("短い", "medium") == AUTO_MAX_TOKENS_FLOOR
    assert auto_max_tokens("短い", "high") == HIGH_REASONING_FLOOR
    assert auto_max_tokens("あ" * 10_000, "high") == 40_004
    assert auto_max_tokens("あ" * 1_000, "max:20000") == 24_004
    assert auto_max_tokens("あ" * 100_000, "high") == AUTO_MAX_TOKENS_CEIL


def test_a_fix_check_at_high_reasoning_is_sent_the_higher_cap(cfg):
    from ncte.llm.fake import FakeLLM
    from ncte.llm.prompt_loader import load_prompt
    from ncte.translation.translator import HIGH_REASONING_FLOOR, NullRecorder, call_structured

    cfg.openrouter.stage_reasoning["fix_check"] = "high"
    client = FakeLLM()
    payload = {"task": "fix_check", "items": [{"unit_id": "u1", "source": "あ", "before": "a", "after": "b",
                                               "issues": ["x"], "around": []}]}
    call_structured(client, NullRecorder(), stage="fix_check", model="google/gemini-3.8-flash",
                    prompt=load_prompt("fix_check"), payload=payload, mode="json_schema", cfg=cfg, unit_ids=["u1"],
                    est_cost=0.0)
    assert client.calls[-1].max_tokens == HIGH_REASONING_FLOOR


def test_auto_cap_is_sent_and_doubled_on_truncation():
    client = _CapClient(need=30_000)
    r = tr(client).translate(units(), {})
    assert not r.failed and client.seen == [16384, 32768]


def test_configured_cap_is_not_raised():
    cfg = Config()
    cfg.openrouter.max_output_tokens = 1000
    client = _CapClient(need=30_000)
    tr(client, cfg).translate(units(), {})
    assert set(client.seen) == {1000}
