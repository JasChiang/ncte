"""M6: dataset validity, metric computation and the A/B harness (offline)."""

import csv
import json

import pytest

from ncte.eval.dataset import Dataset, load_dataset
from ncte.eval.runner import run_eval
from ncte.llm.fake import FakeLLM, default_scene_analyze, default_translate


def name_proposer(ds: Dataset):
    """Scene analysis that only proposes characters whose names literally occur (no speaker guesses)."""
    names = [c.name_ja for c in ds.characters] + ["先生"]

    def analyze(payload, req):
        out = default_scene_analyze(payload, req)
        known = {c["name_ja"] for c in payload["known_characters"]}
        new = []
        for i, n in enumerate(names):
            ev = [u["unit_id"] for u in payload["units"] if n in u["source"]]
            if ev and n not in known:
                new.append({"temp_id": f"new_{i}", "name_ja": n, "aliases_ja": [], "proposed_name_zh": n,
                            "evidence_unit_ids": ev[:1]})
        out["new_characters"] = new
        return out

    return analyze


def oracle_translate(payload, req):
    """Pretend-perfect translation for metric plumbing: inserts the gender pronoun 她 to create an addition."""
    out = default_translate(payload, req)
    for t in out["translations"]:
        src = next(u["source"] for u in payload["units"] if u["unit_id"] == t["unit_id"])
        if "差し出した" in src:
            t["translated_text"] = "她默默遞出了一封信。"
    return out


def test_bundled_dataset_is_valid():
    ds = load_dataset()
    cats = {i.category for i in ds.items}
    assert len(ds.items) >= 10 and len(cats) == len(ds.items)
    assert any(u.speaker is None for i in ds.items for u in i.gold.utterances)
    assert any(i.gold.spoiler for i in ds.items)


def test_dataset_rejects_bad_line():
    with pytest.raises(ValueError):
        Dataset.model_validate({"name": "x", "version": 1, "characters": [],
                                "items": [{"id": "a", "category": "c", "lines": ["あ"],
                                           "gold": {"utterances": [{"line": 5, "speaker": None}]}}]})


def test_ab_harness_outputs_and_metrics(tmp_path, cfg):
    ds = load_dataset()
    fake = FakeLLM(responders={"scene_analyze": name_proposer(ds), "translate": oracle_translate})
    rep = run_eval(ds, cfg, fake, model=cfg.openrouter.model, experiments=["A", "B", "D"], repeat=2,
                   budget_usd=1.0, out_dir=tmp_path / "ev", log=lambda m: None)
    assert (tmp_path / "ev" / "results.csv").exists() and (tmp_path / "ev" / "report.html").exists()
    rows = list(csv.DictReader((tmp_path / "ev" / "results.csv").open(encoding="utf-8")))
    assert {r["experiment"] for r in rows} == {"A", "B", "D"} and len(rows) == 3 * 3
    assert "NOT meaningful" in rep["warning"]
    a, d = rep["experiments"]["A"], rep["experiments"]["D"]
    # baseline experiments have no speaker state; the full pipeline does
    assert a["aggregate"]["speaker_accuracy_explicit"] is None
    acc = d["aggregate"]["speaker_accuracy_explicit"]["mean"]
    assert 0.3 <= acc < 1.0  # explicit tags resolved, unmarked lines honestly left unknown
    assert d["aggregate"]["unknown_asserted_rate"]["mean"] == 0.0  # never asserts gold-unknown speakers
    assert d["aggregate"]["weak_evidence_asserted"]["mean"] == 0  # tic-only line not made explicit
    assert d["aggregate"]["speaker_explicit_precision"]["mean"] == 1.0
    # the injected gender pronoun is measured as an omitted-argument error in every experiment
    for e in rep["experiments"].values():
        assert e["aggregate"]["omitted_argument_error_rate"]["mean"] > 0
        assert e["aggregate"]["epub_structure_integrity"]["mean"] == 1.0
        assert e["aggregate"]["spoiler_violations"]["mean"] == 0
        assert e["aggregate"]["total_cost_usd"]["mean"] > 0 and e["aggregate"]["requests"]["mean"] > 0
    # A sends one request per unit, B batches → more requests for A
    assert a["aggregate"]["requests"]["mean"] > rep["experiments"]["B"]["aggregate"]["requests"]["mean"]
    saved = json.loads((tmp_path / "ev" / "results.json").read_text())
    assert saved["experiments"]["D"]["runs"][0]["run_status"] == "completed"


def test_eval_cli_dry_run(tmp_path):
    from typer.testing import CliRunner

    from ncte.cli import app

    r = CliRunner().invoke(app, ["-w", str(tmp_path / "ws"), "eval", "run", "--dry-run", "--fake",
                                 "--out", str(tmp_path / "o")])
    assert r.exit_code == 0, r.output
    assert "no paid calls made" in r.output
