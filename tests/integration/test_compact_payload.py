"""The memory blocks of a request in fewer characters (``translation.compact_payload``): the same information — who
says each line to whom, the facts, the forms of address — with short excerpts and defaults left out, each default
said in the prompt; off, the earlier form. Ids are short codes everywhere in a request, nested data included."""

import json
import re

from ncte.config import Config
from ncte.llm.fake import FakeLLM
from ncte.llm.prompt_loader import load_prompt
from ncte.llm.short_ids import ShortIds
from ncte.memory.db import connect
from ncte.memory.retrieval import FACT_SOURCE_CHARS, QUOTE_CHARS, clip, fact_excerpt

from ..helpers import run_book
from ..scripted import RESPONDERS

LONG_ID = re.compile(r"[ucfq]_[0-9a-f]{12,16}")


def _translate_requests(tmp_path, fixtures, compact):
    cfg = Config()
    cfg.openrouter.provider = "openrouter"
    cfg.app.workspace = str(tmp_path / ("compact" if compact else "full"))
    cfg.openrouter.max_concurrency = 1
    cfg.translation.compact_payload = compact
    conn = connect(cfg.workspace / "ncte.db")
    fake = FakeLLM(responders=RESPONDERS)
    try:
        run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    finally:
        conn.close()
    return [(c.messages[1]["content"], p) for c, p in zip(fake.calls, fake.payloads, strict=True)
            if c.stage == "translate"]


def _lines(payload):
    """(paragraph, line) → (speaker, said to, how sure of each), with the defaults the prompt states filled in."""
    return {(u["unit_id"], u.get("quote_index", 0)): (u["speaker"], u.get("status", "explicit"), u.get("to"),
                                                      u.get("to_certainty", "explicit"))
            for u in (payload.get("scene") or {}).get("utterances", [])}


def test_compact_requests_are_smaller_and_say_the_same_about_every_line(tmp_path, fixtures):
    full = _translate_requests(tmp_path, fixtures, compact=False)
    short = _translate_requests(tmp_path, fixtures, compact=True)
    assert len(full) == len(short)
    before, after = sum(len(s) for s, _ in full), sum(len(s) for s, _ in short)
    assert after < before, (before, after)
    for (_, old), (_, new) in zip(full, short, strict=True):
        assert [u["unit_id"] for u in old["units"]] == [u["unit_id"] for u in new["units"]]
        assert _lines(old) == _lines(new)
        old_facts = [{k: v for k, v in f.items() if k != "source"} for f in old.get("memory", {}).get("facts", [])]
        assert old_facts == [{k: v for k, v in f.items() if k != "source"}
                             for f in new.get("memory", {}).get("facts", [])]  # the same facts, shorter sources
        assert [{k: v for k, v in a.items() if k != "since"} for a in old.get("address_terms", [])] == \
            new.get("address_terms", [])
    lines = [u for _, p in short for u in (p.get("scene") or {}).get("utterances", [])]
    assert lines and all(len(u["quote"]) <= QUOTE_CHARS + 1 for u in lines)
    assert any("status" not in u for u in lines) and any(u.get("status") == "inferred" for u in lines)
    assert all("quote_index" not in u for u in lines)  # the sample book's paragraphs hold one line each


def test_compact_off_keeps_the_earlier_form(tmp_path, fixtures):
    full = _translate_requests(tmp_path, fixtures, compact=False)
    lines = [u for _, p in full for u in (p.get("scene") or {}).get("utterances", [])]
    assert lines and all({"quote_index", "quote", "speaker", "status"} <= set(u) for u in lines)
    assert any(u["status"] == "explicit" for u in lines)
    forms = [a for _, p in full for a in p.get("address_terms", [])]
    assert forms and all(a["since"] == "第1集" for a in forms)


def test_no_long_id_reaches_the_model(tmp_path, fixtures):
    """The units are sent with short codes, and so are the ids inside the scene's lines, actions and questions, and
    the context paragraphs (numbered in reading order)."""
    sent = _translate_requests(tmp_path, fixtures, compact=True)
    for wire, _ in sent:
        assert not LONG_ID.search(wire), LONG_ID.search(wire)
    wire, payload = next((w, p) for w, p in sent if p.get("context_before") and (p.get("scene") or {}).get("utterances"))
    data = json.loads(wire)
    codes = {u["unit_id"] for u in data["units"]} | {u["unit_id"] for u in data["context_before"]}
    assert all(re.fullmatch(r"P\d+", c) for c in codes)
    assert {u["unit_id"] for u in data["scene"]["utterances"]} <= codes


def test_an_answer_citing_a_context_paragraph_maps_to_the_real_one():
    ctx, unit, after = "u_000000000000000a", "u_000000000000000b", "u_000000000000000c"
    payload = {"context_before": [{"unit_id": ctx, "source": "前"}], "units": [{"unit_id": unit, "source": "本"}],
               "context_after": [{"unit_id": after, "source": "後"}],
               "scene": {"utterances": [{"unit_id": unit, "quote": "「本」"}],
                         "omitted_arguments": [{"unit_id": ctx, "summary": "…"}],
                         "uncertainties": [{"unit_ids": [ctx, unit], "question": "？"}]}}
    ids = ShortIds.for_payload(payload)
    sent = ids.encode(payload)
    assert [sent["context_before"][0]["unit_id"], sent["units"][0]["unit_id"], sent["context_after"][0]["unit_id"]] \
        == ["P1", "P2", "P3"]
    assert sent["scene"]["utterances"][0]["unit_id"] == "P2" and sent["scene"]["omitted_arguments"][0]["unit_id"] == "P1"
    assert sent["scene"]["uncertainties"][0]["unit_ids"] == ["P1", "P2"]
    back = ids.decode({"read_through": ["P1 的說法限制了 P2"], "translations": [{"unit_id": "P2", "translated_text": "P1"}],
                       "evidence_unit_ids": ["P1", "P3"]})
    assert back["evidence_unit_ids"] == [ctx, after] and back["translations"][0] == {"unit_id": unit,
                                                                                     "translated_text": "P1"}
    assert back["read_through"] == [f"{ctx} 的說法限制了 {unit}"]


def test_quotes_and_fact_sources_are_excerpts():
    assert clip("「待って」", QUOTE_CHARS) == "「待って」"
    long_line = "「" + "あ" * 30 + "」"
    assert clip(long_line, QUOTE_CHARS) == long_line[:QUOTE_CHARS] + "…"
    evidence = "朝の市場はにぎやかだった。" * 4 + "カイトは銀の鍵をミナに手渡した。" + "夕方の丘は静かだった。" * 4
    card = [{"character_id": "c_000000000001", "name_ja": "カイト", "aliases_ja": ["カイ"]}]
    fact = {"evidence_quote": evidence, "subject": "c_000000000001", "object": "把銀之鑰交給", "object_ref": None}
    got = fact_excerpt(fact, card, set())
    assert len(got) <= FACT_SOURCE_CHARS and "カイトは銀の鍵をミナに手渡した" in got
    assert got.startswith("…") and got.endswith("…")
    # about no one the evidence names: the stretch sharing the batch's words, else the beginning
    world = {"evidence_quote": evidence, "subject": "アルテイン", "object": "", "object_ref": None}
    from ncte.memory.retrieval import _grams

    assert "夕方の丘" in fact_excerpt(world, [], _grams("夕方の丘で"))
    assert fact_excerpt(world, [], set()) == evidence[:FACT_SOURCE_CHARS - 2] + "…"
    assert fact_excerpt({**world, "evidence_quote": "短い。"}, [], set()) == "短い。"


def test_every_default_left_out_is_said_in_the_prompts():
    translate = load_prompt("translate").render({"address_terms": [{"ja": "a"}], "units": []})
    for said in ("段落只有一句台詞時省略", "沒有 `status` 表示原文寫明了說話者", "沒有 `to_certainty` 的是原文寫明",
                 "沒有 `certainty` 的是原文寫明", "依首次使用的順序排列", "`quote` 是台詞開頭的節錄", "原文依據的節錄"):
        assert said in translate, said
    review = load_prompt("review").render({"memory": {"facts": []}, "items": []})
    for said in ("段落只有一句台詞時省略", "沒有 `status` 表示原文寫明了說話者", "依首次使用的順序排列", "原文依據的節錄"):
        assert said in review, said
    for task in ("proofread", "fix_check"):
        assert "依首次使用的順序排列" in load_prompt(task).render({"address_terms": [{"ja": "a"}], "glossary": [],
                                                                     "paragraphs": [], "items": []}), task


def test_a_scenes_lines_are_sent_in_story_order(conn, cfg, fixtures):
    """The lines of the scene analysis follow the paragraphs (they came in unit-id order, which is a hash)."""
    from ncte.llm.fake import FakeLLM

    from ..helpers import run_book
    from ..scripted import RESPONDERS

    client = FakeLLM(responders=RESPONDERS)
    run_book(conn, cfg, fixtures["basic"], client, mode="context")
    for p in client.payloads:
        if p.get("task") != "translate" or "review_feedback" in p:
            continue
        order = [u["unit_id"] for u in p["units"]]
        lines = [x["unit_id"] for x in (p.get("scene") or {}).get("utterances", []) if x["unit_id"] in order]
        assert lines == sorted(lines, key=order.index)
