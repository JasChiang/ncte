from pathlib import Path

import pytest

from ncte.config import Config, get_api_key, load_config, redact
from ncte.errors import ConfigError


def test_defaults():
    c = Config()
    assert c.openrouter.model == "google/gemini-3.8-flash"
    assert c.app.mode == "baseline"
    assert c.translation.unit_target_japanese_chars == 1400


def test_yaml_loading(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("app:\n  mode: context\nopenrouter:\n  max_concurrency: 3\n", encoding="utf-8")
    c = load_config(p)
    assert c.app.mode == "context" and c.openrouter.max_concurrency == 3


def test_unknown_key_rejected(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("openrouter:\n  api_key: sk-or-v1-abc\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_example_config_is_valid():
    repo = Path(__file__).resolve().parents[2]
    c = load_config(repo / "config.example.yaml")
    assert c.memory.vector_rag_enabled is False


def test_api_key_from_dotenv(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert get_api_key() is None
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-v1-testkey123456\n")
    assert get_api_key() == "sk-or-v1-testkey123456"


def test_redact():
    assert "sk-or-v1-secretsecret" not in redact("bad key sk-or-v1-secretsecret here")
    assert redact("x KEY y", "KEY") == "x *** y"


def test_relations_and_second_look_think_more_when_the_model_offers_it():
    from ncte.config import Config, reasoning_setting
    from ncte.llm.cost import Pricing
    from ncte.translation.orchestrator import check_reasoning_effort

    cfg = Config()
    p = Pricing(model="m", prompt=0.0, completion=0.0, reasoning=0.0, cache_read=None, context_length=None,
                supported_parameters=("reasoning",), reasoning_efforts=("low", "medium", "high"))
    check_reasoning_effort(cfg, p)
    assert reasoning_setting(cfg, "relations") == "high" and reasoning_setting(cfg, "verify") == "high"
    assert reasoning_setting(cfg, "proofread") == "high"
    cfg2 = Config()
    check_reasoning_effort(cfg2, Pricing(model="m", prompt=0.0, completion=0.0, reasoning=0.0, cache_read=None,
                                         context_length=None, supported_parameters=(), reasoning_efforts=()))
    assert reasoning_setting(cfg2, "relations") is None  # a model without reasoning gets nothing


def test_a_relation_can_say_two_cards_are_one_person():
    from ncte.llm.schemas import RelationOut

    r = RelationOut.model_validate({"a": "c1", "b": "c2", "relation": "同一人物", "a_is": "化身", "b_is": "本人",
                                    "facts": ["n1"], "reason": "t", "kind": "same_person", "certainty": "inferred"})
    assert r.kind == "same_person"
