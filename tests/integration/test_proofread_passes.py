"""ADR-066: proofreading in more than one pass (``context.proofread_passes``). Each later pass checks the whole
current translation again, after the pass before has retranslated and checked its fixes; a stopped run goes on from
the pass and the stretch it got to."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ncte.analysis import proofread as pr
from ncte.llm.fake import FakeLLM

from ..helpers import orchestrator_for, resume_run, run_book, start_run
from ..scripted import RESPONDERS


class TwoPasses:
    """A proofreader that flags the first paragraph of a stretch the first time it sees it, the second paragraph the
    next time (a later pass finds something else): what each pass was shown is kept."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.shown: list[list[dict[str, Any]]] = []  # per request: the paragraphs
        self.flagged: list[tuple[int, str]] = []  # (pass, unit id)

    def __call__(self, payload: dict[str, Any], req: Any) -> dict[str, Any]:
        paras = payload["paragraphs"]
        again = paras[0]["unit_id"] in self.seen
        self.seen.update(p["unit_id"] for p in paras)
        self.shown.append(paras)
        p = paras[1] if again and len(paras) > 1 else paras[0]
        self.flagged.append((2 if again else 1, p["unit_id"]))
        return {"issues": [{"unit_id": p["unit_id"], "kind": "meaning", "severity": "high", "quote": p["zh"][:2],
                            "problem": "原文是否定", "fix": "改成否定"}]}


class Counting(FakeLLM):
    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.asked: dict[str, int] = {}
        self.orch: Any = None

    def complete(self, req: Any) -> Any:
        st = req.stage.split(":")[0]
        self.asked[st] = self.asked.get(st, 0) + 1
        return super().complete(req)


class Stopping(Counting):
    """At the k-th proofreading request: paused after answering, or killed while asking."""

    def __init__(self, k: int, how: str, **kw: Any):
        super().__init__(**kw)
        self.k, self.how = k, how

    def complete(self, req: Any) -> Any:
        st = req.stage.split(":")[0]
        if st == "proofread" and self.asked.get(st, 0) + 1 == self.k and self.how == "kill":
            self.asked[st] = self.asked.get(st, 0) + 1
            raise KeyboardInterrupt
        resp = super().complete(req)
        if st == "proofread" and self.asked[st] == self.k and self.how == "pause":
            self.orch._stop_reason = "interrupted"
        return resp


def _job(conn, rid):
    row = conn.execute("SELECT status, checkpoint_json FROM jobs WHERE run_id=? AND stage='proofread'", (rid,)).fetchone()
    return row["status"], json.loads(row["checkpoint_json"] or "{}")


def _setup(cfg, passes: int) -> None:
    cfg.context.proofread_chunk_units = 3
    cfg.context.proofread_passes = passes
    cfg.openrouter.max_concurrency = 1


def test_the_default_is_one_pass(cfg):
    from ncte.config import ContextConfig, proofread_passes

    assert ContextConfig().proofread_passes is None and proofread_passes(cfg) == 1  # an API source: one pass
    with pytest.raises(ValueError):
        ContextConfig(proofread_passes=4)
    with pytest.raises(ValueError):
        ContextConfig(proofread_passes=0)


def test_a_second_pass_checks_the_whole_current_translation_and_its_findings_are_kept(conn, cfg, fixtures):
    _setup(cfg, 2)
    cfg.context.reader_pass = False  # nothing changes the translation after proofreading
    reader = TwoPasses()
    client = Counting(responders={**RESPONDERS, "proofread": reader})
    book, rid, summary = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    assert summary.status == "completed", summary
    total = len(pr.chunks(conn, book.id, size=3))
    assert client.asked["proofread"] == 2 * total
    first = [x["unit_id"] for part in reader.shown[:total] for x in part]
    second = [x["unit_id"] for part in reader.shown[total:] for x in part]
    assert first == second  # the whole book again, not only what the first pass changed
    from ncte.translation.cache import latest_texts

    now = latest_texts(conn, book.id)
    pass1 = {u for k, u in reader.flagged if k == 1}
    pass2 = {u for k, u in reader.flagged if k == 2}
    assert len([k for k, _ in reader.flagged if k == 1]) == len([k for k, _ in reader.flagged if k == 2]) == total
    assert pass2 - pass1  # the second pass found paragraphs the first did not
    # the second pass read the first pass's retranslations (each was flagged once, then fixed)
    shown2 = {x["unit_id"]: x["zh"] for part in reader.shown[total:] for x in part}
    from ncte.analysis.proofread import PH_RE

    assert all(shown2[u] == PH_RE.sub("", now[u]["text"]) for u in pass1)
    retranslated = {r[0] for r in conn.execute("SELECT unit_id FROM translations t JOIN units u ON u.id=t.unit_id "
                                               "WHERE u.book_id=? GROUP BY unit_id HAVING COUNT(*) > 1", (book.id,))}
    assert pass1 <= retranslated
    found = [r[0] for r in conn.execute("SELECT unit_id FROM qa_findings WHERE run_id=? AND "
                                        "type='review:proofread_meaning'", (rid,))]
    assert sorted(found) == sorted(u for _, u in reader.flagged)  # both passes' findings
    status, cp = _job(conn, rid)
    assert status == "succeeded"
    assert cp["retranslated"] == 2 * total and cp["issues"] == 2 * total  # the two passes added up
    assert [p["issues"] for p in cp["passes"]] == [total, total]
    assert [p["retranslated"] for p in cp["passes"]] == [total, total]
    last = conn.execute(f"SELECT MAX(id) FROM translations WHERE unit_id IN ({','.join('?' * len(pass2))})",
                        sorted(pass2)).fetchone()[0]
    assert cp["upto_translation_id"] >= last  # recorded when the last pass is done, after its fixes (2.11)
    assert "pass" not in cp and "done_until_ordinal" not in cp


def test_one_pass_asks_as_before(conn, cfg, fixtures):
    _setup(cfg, 1)
    client = Counting(responders={**RESPONDERS, "proofread": TwoPasses()})
    book, rid, summary = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    assert summary.status == "completed"
    total = len(pr.chunks(conn, book.id, size=3))
    assert client.asked["proofread"] == total
    status, cp = _job(conn, rid)
    assert status == "succeeded" and cp["issues"] == total and "passes" not in cp and "pass" not in cp


@pytest.mark.parametrize("how,at,done", [("pause", 2, 2), ("kill", 2, 1), ("kill", 1, 0)])
def test_a_run_stopped_in_the_second_pass_goes_on_from_there(conn, cfg, fixtures, how, at, done):
    """Stopped at the second pass's ``at``-th stretch (paused after it, or killed while asking): resumed, the first
    pass is not done again and the second goes on from its checkpoint."""
    _setup(cfg, 2)
    reader = TwoPasses()
    responders = {**RESPONDERS, "proofread": reader}
    client = Stopping(0, how, responders=responders)
    book, rid, opts = start_run(conn, cfg, fixtures["basic"], client, mode="context")
    orch = orchestrator_for(conn, cfg, book, rid, opts, client)
    client.orch = orch
    real = client.complete
    total = 0

    def complete(req: Any) -> Any:  # the stretches are known once the book is translated
        nonlocal total
        if req.stage.startswith("proofread") and not total:
            total = len(pr.chunks(conn, book.id, size=3))
            client.k = total + at
        return real(req)

    client.complete = complete  # type: ignore[method-assign]
    assert orch.run().status == "interrupted"
    assert total > 2
    status, cp = _job(conn, rid)
    assert status == "running" and cp["pass"] == 2 and len(cp["passes"]) == 1
    assert cp["passes"][0]["issues"] == total and cp["issues"] == done
    if done:
        assert cp["done_until_ordinal"] == pr.chunks(conn, book.id, size=3)[done - 1][-1]["_at"]
    else:
        assert "done_until_ordinal" not in cp

    again = Counting(responders=responders)
    summary = resume_run(conn, cfg, rid, again)
    assert summary.status == "completed", summary
    assert again.asked.get("proofread") == total - done  # only the second pass's stretches not checked yet
    status, cp = _job(conn, rid)
    assert status == "succeeded" and len(cp["passes"]) == 2
    assert [p["issues"] for p in cp["passes"]] == [total, total] and cp["retranslated"] == 2 * total
    found = [r[0] for r in conn.execute("SELECT unit_id FROM qa_findings WHERE run_id=? AND "
                                        "type='review:proofread_meaning'", (rid,))]
    assert len(found) == 2 * total and sorted(found) == sorted(u for _, u in reader.flagged)  # none found twice


def test_the_command_takes_a_number_of_passes(tmp_path, fixtures):
    from typer.testing import CliRunner

    from ncte.cli import app
    from ncte.config import Config
    from ncte.workspace import Workspace

    ws, epub, r = tmp_path / "ws", str(fixtures["basic"]), CliRunner()
    assert r.invoke(app, ["-w", str(ws), "translate", epub, "--fake", "--mode", "context"]).exit_code == 0
    out = r.invoke(app, ["-w", str(ws), "proofread", epub, "--fake", "--passes", "2"])
    assert out.exit_code == 0 and "proofreading:" in out.output, out.output
    cfg = Config()
    cfg.app.workspace = str(ws)
    w = Workspace(cfg)
    try:
        conn, book = w.open_book(epub)
        cp = json.loads(conn.execute("SELECT checkpoint_json FROM jobs WHERE book_id=? AND stage='proofread' "
                                     "ORDER BY id DESC LIMIT 1", (book.id,)).fetchone()[0])
    finally:
        w.close()
    assert len(cp["passes"]) == 2
    assert r.invoke(app, ["-w", str(ws), "proofread", epub, "--fake", "--passes", "4"]).exit_code != 0


def test_the_estimate_counts_each_pass(conn, cfg, fixtures):
    from ncte.translation.dryrun import dry_run
    from ncte.translation.orchestrator import RunOptions

    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(), mode="context")
    opts = RunOptions(mode="context", model=cfg.openrouter.model, force=True)
    one = dry_run(conn, cfg, book, opts, FakeLLM(), log=lambda m: None).stage_estimates["proofread"]
    cfg.context.proofread_passes = 2
    two = dry_run(conn, cfg, book, opts, FakeLLM(), log=lambda m: None).stage_estimates["proofread"]
    assert two["usd"] == pytest.approx(2 * one["usd"], abs=2e-4) and two["high_usd"] == pytest.approx(
        2 * one["high_usd"], abs=2e-4)


def test_on_the_claude_plan_proofreading_does_two_passes_unless_set(tmp_path, monkeypatch):
    """ADR-066 (2026-10-20): on the Claude plan a second pass costs quota, not money, and found 6–8 more each time;
    OpenRouter stays at one. What the configuration says wins, also 1."""
    from ncte.config import load_config, proofread_passes

    monkeypatch.setenv("NCTE_LLM_PROVIDER", "claude")
    assert proofread_passes(load_config()) == 2
    monkeypatch.setenv("NCTE_LLM_PROVIDER", "openrouter")
    assert proofread_passes(load_config()) == 1
    monkeypatch.setenv("NCTE_LLM_PROVIDER", "claude")
    one = tmp_path / "one.yaml"
    one.write_text("context:\n  proofread_passes: 1\n", encoding="utf-8")
    assert proofread_passes(load_config(one)) == 1


def test_the_estimate_follows_the_claude_default(conn, cfg, fixtures):
    from ncte.translation.dryrun import dry_run
    from ncte.translation.orchestrator import RunOptions

    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(), mode="context")
    opts = RunOptions(mode="context", model=cfg.openrouter.model, force=True)
    api = dry_run(conn, cfg, book, opts, FakeLLM(), log=lambda m: None).stage_estimates["proofread"]
    cfg.openrouter.provider = "claude"
    plan = dry_run(conn, cfg, book, opts, FakeLLM(), log=lambda m: None).stage_estimates["proofread"]
    assert plan["usd"] == pytest.approx(2 * api["usd"], abs=2e-4)


def test_a_run_on_the_claude_plan_proofreads_twice(conn, cfg, fixtures, monkeypatch):
    """The run itself takes the default from its source (the fake model stands in for the Claude plan here)."""
    from ncte.translation import orchestrator

    monkeypatch.setattr(orchestrator, "check_reasoning_effort", lambda cfg, pricing: None)
    cfg.openrouter.provider = "claude"
    cfg.context.proofread_chunk_units = 100
    client = FakeLLM()
    book, rid, _ = run_book(conn, cfg, fixtures["basic"], client, mode="context")
    _, cp = _job(conn, rid)
    assert len(cp["passes"]) == 2
