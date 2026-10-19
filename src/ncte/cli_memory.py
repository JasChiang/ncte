"""CLI commands for M3–M6: scenes, characters, memory, glossary, speakers, human edits, review, evaluation.

Registered on the main Typer ``app`` at import time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from .cli import _book, app, echo_json, make_client, state
from .errors import NCTEError
from .memory.db import Transaction, utcnow
from .series import SERIES_SCOPE

glossary_app = typer.Typer(help="Glossary (譯名／術語表) management.", no_args_is_help=True)
app.add_typer(glossary_app, name="glossary")


# --------------------------------------------------------------------------- scenes / characters / memory


@app.command()
def scenes(epub: Annotated[str, typer.Argument(help="EPUB path or book id")],
           split: Annotated[str | None, typer.Option("--split", help="Force a scene break before this unit")] = None,
           merge: Annotated[str | None, typer.Option("--merge", help="Suppress the automatic break at this unit")] = None,
           clear: Annotated[str | None, typer.Option("--clear", help="Remove a manual override")] = None,
           rebreak: Annotated[bool, typer.Option("--rebreak", help="Let the model choose the scene breaks again "
                                                 "(books cut before ADR-036); changed scenes are read again")] = False,
           budget_usd: Annotated[float, typer.Option("--budget-usd")] = 0.5,
           fake: Annotated[bool, typer.Option("--fake")] = False,
           as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List scenes (deterministic segmentation) and manage manual overrides."""
    from .analysis.scene import segment_book

    book = _book(epub)
    conn = state.conn
    if rebreak:
        from .cli import _run
        from .translation.orchestrator import RunOptions

        cfg = state.cfg
        _run(book, RunOptions(mode="context", model=cfg.openrouter.model, budget_usd=budget_usd,
                              client="fake" if fake else cfg.openrouter.provider, rebreak_only=True), fake=fake, cfg=cfg)
    if split or merge or clear:
        with Transaction(conn):
            if clear:
                conn.execute("DELETE FROM scene_overrides WHERE book_id=? AND unit_id=?", (book.id, clear))
            for uid, action in ((split, "split"), (merge, "merge")):
                if uid:
                    if not conn.execute("SELECT 1 FROM units WHERE id=? AND book_id=?", (uid, book.id)).fetchone():
                        raise NCTEError(f"unknown unit {uid}")
                    conn.execute("INSERT OR REPLACE INTO scene_overrides VALUES (?,?,?,?)", (book.id, uid, action, utcnow()))
        segment_book(conn, state.cfg, book.id)
    rows = conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM units u WHERE u.book_id=s.book_id AND u.scene_id=s.id) AS n, "
        "(SELECT plain_text FROM units u WHERE u.id=s.start_unit_id) AS first, b.sure, b.reason AS break_note "
        "FROM scenes s LEFT JOIN scene_breaks b ON b.book_id=s.book_id AND b.unit_id=s.start_unit_id WHERE s.book_id=? "
        "ORDER BY s.seq", (book.id,)).fetchall()
    data = [{"scene_id": r["id"], "units": r["n"], "break": r["break_reason"], "analyzed_version": r["version"],
             "start_unit_id": r["start_unit_id"], "first_text": r["first"][:40],
             # the model marked this break as a judgement call (ADR-057): listed, never cut again by itself
             **({"unsure": r["break_note"] or ""} if r["sure"] == 0 else {})} for r in rows]
    if as_json:
        echo_json(data)
        return
    for d in data:
        typer.echo(f"{d['scene_id']}  {d['units']:4d} units  break={d['break']:<11} v{d['analyzed_version']}  "
                   f"{d['start_unit_id']}  {d['first_text']}"
                   + (f"  [模型標需複核：{d['unsure']}]" if "unsure" in d else ""))


@app.command()
def characters(epub: Annotated[str, typer.Argument()],
               as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List characters, names, aliases and speech profiles (from verified examples only)."""
    book = _book(epub)
    rows = state.conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM speech_examples e WHERE e.character_id=c.id) AS examples FROM characters c "
        "WHERE book_id=? ORDER BY known_from_ordinal", (book.id,)).fetchall()
    data = [{"character_id": r["id"], "name_ja": r["canonical_name"], "name_zh": r["display_name_zh"],
             "aliases_ja": json.loads(r["aliases_json"]), "known_from_unit_id": r["known_from_unit_id"],
             "status": r["status"], "origin": r["origin"], "verified_examples": r["examples"],
             "speech_profile": json.loads(r["speech_profile_json"] or "{}")} for r in rows]
    if as_json:
        echo_json(data)
        return
    for d in data:
        typer.echo(f"{d['character_id']}  {d['name_ja']} → {d['name_zh']}  aliases={d['aliases_ja']}  "
                   f"examples={d['verified_examples']}  profile={d['speech_profile']}")


@app.command()
def memory(epub: Annotated[str, typer.Argument()],
           search: Annotated[str | None, typer.Option("--search", help="FTS5 keyword search")] = None,
           at: Annotated[str | None, typer.Option("--at", help="Only show what is visible before this unit")] = None,
           facts: Annotated[bool, typer.Option("--facts", help="List facts (timeline)")] = False,
           as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Inspect plot memory: facts timeline, summaries and keyword search (spoiler-safe with --at)."""
    from .memory import visibility as vis
    from .memory.retrieval import search as fts

    book = _book(epub)
    conn = state.conn
    horizon = None
    if at:
        row = conn.execute("SELECT ordinal FROM units WHERE id=? AND book_id=?", (at, book.id)).fetchone()
        if not row:
            raise NCTEError(f"unknown unit {at}")
        horizon = row[0]
    out: dict[str, Any] = {"horizon_ordinal": horizon}
    if search:
        out["search"] = fts(conn, book.id, search, horizon=horizon, limit=20)
    if facts or not search:
        hz = horizon if horizon is not None else 1 << 62
        out["facts"] = [{"fact_id": f["id"], "subject": f["subject"], "predicate": f["predicate"], "object": f["object"],
                         "type": f["fact_type"], "certainty": f["certainty"], "evidence": f["evidence_quote"],
                         "first_revealed_unit_id": f["first_revealed_unit_id"], "retracted_by": f["retracted_by"]}
                        for f in vis.visible_facts(conn, book.id, hz)]
        out["summaries"] = [{"scene_id": s["scene_id"], "summary": s["summary"]} for s in vis.visible_summaries(conn, book.id, hz)]
        out["rejected_claims"] = conn.execute("SELECT COUNT(*) FROM memory_rejections WHERE book_id=?", (book.id,)).fetchone()[0]
    if as_json:
        echo_json(out)
        return
    v: Any
    for k, v in out.items():
        if isinstance(v, list):
            typer.echo(f"{k}: {len(v)}")
            for x in v[:50]:
                typer.echo("  " + json.dumps(x, ensure_ascii=False))
        else:
            typer.echo(f"{k}: {v}")


# --------------------------------------------------------------------------- glossary


def glossary_impact(conn: Any, book_id: str, term: str) -> list[dict[str, Any]]:
    """Units whose source contains ``term`` and whether their current translation uses the current target."""
    from .translation.cache import latest_texts

    g = conn.execute(f"SELECT * FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book_id, term)).fetchone()
    target = g["target_term"] if g else None
    best = latest_texts(conn, book_id)
    out = []
    from .epub.units import match_text

    for u in conn.execute("SELECT * FROM units WHERE book_id=? AND translatable=1 ORDER BY ordinal", (book_id,)):
        if term not in match_text(u["plain_text"]):
            continue
        t = best.get(u["id"])
        out.append({"unit_id": u["id"], "source": u["plain_text"][:80], "translation": t["text"][:80] if t else None,
                    "uses_current_target": bool(t and target and target in t["text"]),
                    "translation_status": t["status"] if t else None})
    return out


@glossary_app.command("list")
def glossary_list(epub: Annotated[str, typer.Argument()],
                  as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List glossary entries (verified = confirmed by a human)."""
    book = _book(epub)
    rows = [dict(r) for r in state.conn.execute("SELECT source_term, target_term, category, verified, version, origin "
                                                f"FROM glossary WHERE {SERIES_SCOPE} ORDER BY id", (book.id,))]
    if as_json:
        echo_json(rows)
        return
    for r in rows:
        typer.echo(f"{'✔' if r['verified'] else '·'} {r['source_term']} → {r['target_term']}  [{r['category']}, "
                   f"v{r['version']}, {r['origin']}]")


@glossary_app.command("set")
def glossary_set(epub: Annotated[str, typer.Argument()], source: Annotated[str, typer.Argument()],
                 target: Annotated[str, typer.Argument()],
                 category: Annotated[str, typer.Option(help="character|place|item|skill|organization|term")] = "term",
                 unverified: Annotated[bool, typer.Option("--unverified")] = False) -> None:
    """Add/change a term. Affected units are listed; their cached translations become stale automatically."""
    from .memory.repository import set_glossary

    book = _book(epub)
    with Transaction(state.conn):
        res = set_glossary(state.conn, book.id, source, target, category=category, verified=not unverified)
    impact = glossary_impact(state.conn, book.id, source)
    stale = [x for x in impact if x["translation"] is not None and not x["uses_current_target"]]
    typer.echo(f"{source} → {target} (previous: {res['previous']}, version {res['version']})")
    typer.echo(f"affected units: {len(impact)}, translations not using the new term: {len(stale)}")
    for x in stale[:30]:
        typer.echo(f"  {x['unit_id']}  {x['translation']}")
    if stale:
        typer.echo("re-run `ncte translate` (only these units are invalidated) or "
                   "`ncte retranslate --unit-id …` to update them")


@glossary_app.command("impact")
def glossary_impact_cmd(epub: Annotated[str, typer.Argument()], source: Annotated[str, typer.Argument()],
                        as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Locate translation units affected by a term."""
    book = _book(epub)
    data = glossary_impact(state.conn, book.id, source)
    if as_json:
        echo_json(data)
        return
    for x in data:
        typer.echo(f"{'✓' if x['uses_current_target'] else '✗'} {x['unit_id']}  {x['source']}  →  {x['translation']}")


# --------------------------------------------------------------------------- speakers


@app.command()
def speakers(epub: Annotated[str, typer.Argument()],
             scene: Annotated[str | None, typer.Option("--scene")] = None,
             status: Annotated[str | None, typer.Option("--status", help="explicit|inferred|unknown|human_verified")] = None,
             as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List utterances with speaker status, candidates and evidence."""
    book = _book(epub)
    q = ("SELECT u.*, c.canonical_name AS speaker FROM utterances u LEFT JOIN characters c ON c.id=u.speaker_id_nullable "
         "JOIN units x ON x.id=u.unit_id WHERE u.book_id=?")
    params: list[Any] = [book.id]
    if scene:
        q += " AND u.scene_id=?"
        params.append(scene)
    if status:
        q += " AND u.status=?"
        params.append(status)
    rows = state.conn.execute(q + " ORDER BY x.ordinal, u.quote_index", params).fetchall()
    data = []
    for r in rows:
        hyps = [json.loads(h["evidence_json"]) for h in state.conn.execute(
            "SELECT evidence_json FROM speaker_hypotheses WHERE utterance_id=?", (r["id"],))]
        data.append({"utterance_id": r["id"], "scene_id": r["scene_id"], "quote": r["quote_text"], "speaker": r["speaker"],
                     "status": r["status"], "source": r["resolution_source"], "notes": json.loads(r["notes_json"]),
                     "candidates": hyps})
    if as_json:
        echo_json(data)
        return
    for d in data:
        typer.echo(f"{d['utterance_id']}  [{d['status']:<14}] {d['speaker'] or '—':<8} {d['quote'][:40]}"
                   + (f"   ({'; '.join(d['notes'])[:80]})" if d["notes"] else ""))


@app.command("speaker-set")
def speaker_set(epub: Annotated[str, typer.Argument()],
                utterance_id: Annotated[str, typer.Option("--utterance-id")],
                character: Annotated[str | None, typer.Option("--character", help="character id (see `ncte characters`)")] = None,
                unknown: Annotated[bool, typer.Option("--unknown", help="Confirm the speaker is unknown")] = False,
                reason: Annotated[str, typer.Option("--reason")] = "") -> None:
    """Human confirmation/correction of a speaker (becomes human_verified; never auto-overwritten)."""
    from .analysis.speaker import set_speaker_human

    _book(epub)
    if bool(character) == unknown:
        raise NCTEError("give exactly one of --character or --unknown")
    with Transaction(state.conn):
        try:
            out = set_speaker_human(state.conn, utterance_id, None if unknown else character, reason=reason)
        except KeyError as exc:
            raise NCTEError(str(exc)) from exc
    typer.echo(f"updated {len(out['utterances'])} utterance(s); units whose speaker changed: {out['changed_units']}")
    if out["changed_units"]:
        typer.echo("retranslate them with: ncte retranslate BOOK --mode context " +
                   " ".join(f"--unit-id {u}" for u in out["changed_units"]))



# --------------------------------------------------------------------------- units / human edits (M5)


@app.command()
def unit(epub: Annotated[str, typer.Argument()], unit_id: Annotated[str, typer.Argument()],
         as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show a unit: source with placeholders, translation history, findings, speaker status."""
    book = _book(epub)
    conn = state.conn
    r = conn.execute("SELECT * FROM units WHERE id=? AND book_id=?", (unit_id, book.id)).fetchone()
    if r is None:
        raise NCTEError(f"unknown unit {unit_id}")
    data = {
        "unit_id": r["id"], "kind": r["kind"], "href": r["resource_href"], "xpath": r["xpath"], "scene_id": r["scene_id"],
        "translatable": bool(r["translatable"]), "skip_reason": r["skip_reason"], "source": r["source_text"],
        "placeholders": json.loads(r["inline_map_json"]),
        "translations": [{"id": t["id"], "status": t["status"], "model": t["model_id"], "run_id": t["run_id"],
                          "text": t["text"], "flags": json.loads(t["flags_json"]), "created_at": t["created_at"]}
                         for t in conn.execute("SELECT * FROM translations WHERE unit_id=? ORDER BY id", (unit_id,))],
        "findings": [dict(f) for f in conn.execute("SELECT type, severity, status, evidence FROM qa_findings WHERE unit_id=?",
                                                   (unit_id,))],
        "utterances": [dict(u) for u in conn.execute("SELECT id, quote_text, speaker_id_nullable, status FROM utterances "
                                                     "WHERE unit_id=?", (unit_id,))],
        "human_edits": [dict(h) for h in conn.execute("SELECT * FROM human_edits WHERE unit_id=?", (unit_id,))],
    }
    if as_json:
        echo_json(data)
        return
    typer.echo(f"{data['unit_id']} [{data['kind']}] {data['href']} {data['xpath']} scene={data['scene_id']}")
    typer.echo(f"source: {data['source']}")
    for t in data["translations"]:
        typer.echo(f"  #{t['id']} [{t['status']}] {t['model']}: {t['text']}")
    for f in data["findings"]:
        typer.echo(f"  ! {f['severity']} {f['type']} ({f['status']}): {f['evidence']}")
    for u in data["utterances"]:
        typer.echo(f"  utterance {u['id']}: speaker={u['speaker_id_nullable']} [{u['status']}]")


@app.command()
def edit(epub: Annotated[str, typer.Argument()],
         unit_id: Annotated[str, typer.Option("--unit-id")],
         text: Annotated[str, typer.Option("--text", help="Corrected translation (keep every ⟦n⟧ placeholder)")],
         reason: Annotated[str, typer.Option("--reason")] = "",
         force: Annotated[bool, typer.Option("--force", help="Accept despite non-placeholder QA errors")] = False) -> None:
    """Record a human correction. It takes priority at export and is never overwritten by machine runs."""
    from .translation.edits import apply_human_edit

    book = _book(epub)
    findings = apply_human_edit(state.conn, book.id, unit_id, text, reason=reason, force=force)
    typer.echo(f"saved human translation for {unit_id}")
    for f in findings:
        typer.echo(f"  ! {f.severity} {f.type}: {f.evidence}")


@app.command()
def review(epub: Annotated[str, typer.Argument()],
           scope: Annotated[str, typer.Option("--scope", help="flagged | all")] = "flagged",
           unit_id: Annotated[list[str] | None, typer.Option("--unit-id")] = None,
           budget_usd: Annotated[float, typer.Option("--budget-usd")] = 1.0,
           fake: Annotated[bool, typer.Option("--fake")] = False) -> None:
    """One selective review pass over existing translations (fixes are retranslated once and re-checked)."""
    from .cli import _run
    from .translation.orchestrator import RunOptions

    if scope not in ("flagged", "all"):
        raise NCTEError("--scope must be flagged or all")
    cfg = state.cfg
    cfg.review.automatic_review = "all" if scope == "all" else "flagged_only"
    cfg.review.maximum_review_passes = 1
    book = _book(epub)
    # fixes are retranslated with the same kind of context the book was translated with (memory in context mode)
    last = state.conn.execute("SELECT mode FROM runs WHERE book_id=? AND mode='context' LIMIT 1", (book.id,)).fetchone()
    opts = RunOptions(mode="context" if last else "baseline", model=cfg.openrouter.model, budget_usd=budget_usd,
                      unit_ids=unit_id or None, client="fake" if fake else cfg.openrouter.provider, review_only=True)
    _run(book, opts, fake=fake, cfg=cfg)


# --------------------------------------------------------------------------- evaluation (M6)

eval_app = typer.Typer(help="A/B evaluation on the human-annotated dataset.", no_args_is_help=True)
app.add_typer(eval_app, name="eval")


@eval_app.command("check")
def eval_check(dataset: Annotated[str | None, typer.Argument(help="dataset JSON (default: bundled v1)")] = None) -> None:
    """Validate an evaluation dataset and show its coverage."""
    from .eval.dataset import load_dataset

    ds = load_dataset(dataset)
    typer.echo(f"{ds.name} v{ds.version}: {len(ds.items)} items, "
               f"{sum(len(i.gold.utterances) for i in ds.items)} annotated utterances, "
               f"{sum(len(i.gold.units) for i in ds.items)} annotated units")
    for i in ds.items:
        typer.echo(f"  {i.id:<26} {i.category}")


@eval_app.command("run")
def eval_run(dataset: Annotated[str | None, typer.Option("--dataset")] = None,
             experiments: Annotated[str, typer.Option("--experiments", help="comma list of A,B,C,D")] = "A,B,C,D",
             repeat: Annotated[int, typer.Option("--repeat", min=1, max=10)] = 1,
             model: Annotated[str | None, typer.Option("--model")] = None,
             budget_usd: Annotated[float, typer.Option("--budget-usd", help="budget per experiment run")] = 0.5,
             out: Annotated[str | None, typer.Option("--out")] = None,
             epubcheck: Annotated[bool, typer.Option("--epubcheck/--no-epubcheck")] = False,
             fake: Annotated[bool, typer.Option("--fake", help="FakeLLM: harness check only, metrics not meaningful")] = False,
             dry_run: Annotated[bool, typer.Option("--dry-run", help="Only estimate the cost of the evaluation")] = False) -> None:
    """Run experiments A–D on the same annotated source and write results.json / results.csv / report.html."""
    from datetime import UTC, datetime
    from pathlib import Path

    from .eval.dataset import build_dataset_epub, load_dataset
    from .eval.runner import EXPERIMENTS, run_eval

    ds = load_dataset(dataset)
    keys = [k.strip().upper() for k in experiments.split(",") if k.strip()]
    if not keys or any(k not in EXPERIMENTS for k in keys):
        raise NCTEError("--experiments must be a comma list of A,B,C,D")
    cfg = state.cfg
    mdl = model or cfg.openrouter.model
    out_dir = Path(out) if out else cfg.workspace / "eval" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    if dry_run:
        from .library import import_book
        from .memory.db import connect
        from .translation.dryrun import dry_run as do_dry
        from .translation.orchestrator import RunOptions

        out_dir.mkdir(parents=True, exist_ok=True)
        epub = build_dataset_epub(ds, out_dir / "eval_source.epub")
        total_hi = 0.0
        for k in keys:
            c2 = cfg.model_copy(deep=True)
            c2.app.workspace = str(out_dir / f"dry_{k}")
            EXPERIMENTS[k].configure(c2)
            conn = connect(c2.workspace / "ncte.db")
            book, _ = import_book(conn, c2, epub)
            plan = do_dry(conn, c2, book, RunOptions(mode=EXPERIMENTS[k].mode, model=mdl, budget_usd=budget_usd),
                          make_client(cfg, fake=fake, need_key=False), log=lambda m: None)
            total_hi += plan.cost_high_usd * repeat
            typer.echo(f"{k}: requests≈{plan.requests_estimate} cost ${plan.cost_low_usd:.4f}–${plan.cost_high_usd:.4f}"
                       f" × {repeat}")
            conn.close()
        typer.echo(f"total upper estimate ≈ ${total_hi:.4f} (no paid calls made)")
        return
    client = make_client(cfg, fake=fake, log=typer.echo)
    report = run_eval(ds, cfg, client, model=mdl, experiments=keys, repeat=repeat, budget_usd=budget_usd,
                      out_dir=out_dir, run_epubcheck=epubcheck, log=typer.echo)
    if report.get("warning"):
        typer.echo(f"⚠ {report['warning']}")
    for k, e in report["experiments"].items():
        a = e["aggregate"]

        def mean(key: str, a: dict[str, Any] = a) -> Any:
            return a[key]["mean"] if a.get(key) else "n/a"

        typer.echo(f"{k} {e['description']}: speaker_acc={mean('speaker_accuracy_explicit')} "
                   f"unknown_false={mean('unknown_false_assertion_rate')} omit/add={mean('omission_addition_rate')} "
                   f"terms={mean('entity_terminology_consistency')} spoilers={mean('spoiler_violations')} "
                   f"cost=${mean('total_cost_usd')}")
    typer.echo(f"results: {out_dir}/results.json, results.csv, report.html")


@eval_app.command("fix-check")
def eval_fix_check(models: Annotated[list[str], typer.Option("--model", help="model to compare (repeatable)")],
                   cases: Annotated[str | None, typer.Option("--cases", help="bundled set name (fix_check_v1) or JSON path; default the latest")] = None,
                   repeat: Annotated[int, typer.Option("--repeat", min=1, max=5)] = 1,
                   budget_usd: Annotated[float, typer.Option("--budget-usd", help="for the whole comparison")] = 0.3,
                   out: Annotated[str | None, typer.Option("--out")] = None,
                   fake: Annotated[bool, typer.Option("--fake", help="FakeLLM: harness check only")] = False,
                   dry_run: Annotated[bool, typer.Option("--dry-run", help="Only estimate (reads /models, no paid calls)")] = False) -> None:
    """How often each model settles a fix that is still wrong (known cases, the run's fix_check prompt)."""
    from collections import Counter
    from datetime import UTC, datetime

    from .eval.fix_check import estimate, load_fix_set, pricing_for, reasoning_for, run_comparison

    fs = load_fix_set(cases)
    cfg = state.cfg
    client = make_client(cfg, fake=fake, need_key=not dry_run, log=typer.echo)
    gold = Counter(c.gold for c in fs.cases)
    typer.echo(f"{fs.name} v{fs.version}: {len(fs.cases)} cases " + ", ".join(f"{k} {v}" for k, v in sorted(gold.items())))
    if not fake and (bal := getattr(client, "balance", None)):
        typer.echo(f"balance: {bal() or 'not reported'}")
    if dry_run:
        total = 0.0
        for m, p in pricing_for(client, models).items():
            effort = reasoning_for(cfg, p)
            est = estimate(fs, p, reasoning=effort) * repeat
            total += est
            pin, pout = p.per_million()
            typer.echo(f"  {m}: ${pin:.3f}/M in, ${pout:.3f}/M out, reasoning {effort or 'default'}, "
                       f"{p.structured_mode}; ≤ ${est:.4f} for {repeat} pass(es)")
        typer.echo(f"upper estimate ≈ ${total:.4f} (no paid calls made)")
        return
    out_dir = Path(out) if out else cfg.workspace / "eval" / ("fix_check_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S"))
    report = run_comparison(fs, cfg, client, models, repeat=repeat, budget_usd=budget_usd, out_dir=out_dir, log=typer.echo)
    for r in report["runs"]:
        typer.echo(f"{r['model']} #{r['pass']}: false settle {r['false_settle_rate']} {r['false_settled']} · "
                   f"false fixed {r['false_fixed_rate']} · settle recall {r['settle_recall']} · exact {r['exact']} · "
                   f"undecided {r['undecided_rate']} · ${r['cost_usd']:.4f}")
    typer.echo(f"spent ${report['spent_usd']:.4f}; results: {out_dir}/fix_check_results.json")


@eval_app.command("retranslate")
def eval_retranslate(judge: Annotated[str, typer.Option("--judge", help="model that checks the fixes (not the translator's family)")],
                     model: Annotated[str | None, typer.Option("--model", help="translator (default: the run's model)")] = None,
                     cases: Annotated[str | None, typer.Option("--cases", help="bundled set name or JSON path")] = None,
                     budget_usd: Annotated[float, typer.Option("--budget-usd", help="for the translations")] = 0.3,
                     out: Annotated[str | None, typer.Option("--out")] = None,
                     fake: Annotated[bool, typer.Option("--fake", help="FakeLLM: harness check only")] = False,
                     dry_run: Annotated[bool, typer.Option("--dry-run", help="Count the requests (reads /models)")] = False) -> None:
    """Does a fix change only what is wrong? Known wrong translations go back with what was found (audit §5.2)."""
    from datetime import UTC, datetime

    from .eval.fix_check import estimate, load_fix_set, pricing_for, reasoning_for
    from .eval.retranslate import distinct_cases, run_retranslate_eval

    fs = load_fix_set(cases)
    cfg = state.cfg
    mdl = model or cfg.openrouter.model
    client = make_client(cfg, fake=fake, need_key=not dry_run, log=typer.echo)
    todo = distinct_cases(fs)
    typer.echo(f"{fs.name}: {len(todo)} fixes ({sum(s for _, s in todo)} on a real error, "
               f"{sum(not s for _, s in todo)} on a wrong or unsettled suggestion); translator {mdl}, judge {judge}")
    if not fake and (bal := getattr(client, "balance", None)):
        typer.echo(f"balance: {bal() or 'not reported'}")
    if dry_run:
        p = pricing_for(client, [judge])[judge]
        typer.echo(f"  {len(todo)} translate requests (one per fix), stopped at --budget-usd ${budget_usd:.2f} spent; "
                   f"judging ≤ ${estimate(fs, p, reasoning=reasoning_for(cfg, p)):.4f} (no paid calls made)")
        return
    out_dir = Path(out) if out else cfg.workspace / "eval" / ("retranslate_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S"))
    r = run_retranslate_eval(fs, cfg, client, model=mdl, judge=judge, budget_usd=budget_usd, out_dir=out_dir, log=typer.echo)
    s, u = r["supported"], r["unsupported"]
    typer.echo(f"real errors ({s['n']}): fixed {s['fixed_rate']} · {s['outcomes']} · left as it was {s['kept_original']} · "
               f"median edit {s['median_edit']}")
    typer.echo(f"wrong/unsettled suggestions ({u['n']}): {u['outcomes']} · left as it was {u['kept_original']} · "
               f"followed into an error {u['followed_rate']}")
    typer.echo(f"cost ${r['translate_cost_usd'] + r['judge_cost_usd']:.4f}; {out_dir}/retranslate_results.json, "
               "retranslate_review.md")


@app.command()
def recheck(epub: Annotated[str, typer.Argument()]) -> None:
    """Re-run program QA on existing translations (after rule fixes); no API calls."""
    from .qa.recheck import recheck_book

    book = _book(epub)
    typer.echo(f"recheck: {recheck_book(state.conn, book.id)}")


@glossary_app.command("prune")
def glossary_prune(epub: Annotated[str, typer.Argument()],
                   dry_run: Annotated[bool, typer.Option("--dry-run")] = False) -> None:
    """Remove automatic (unverified) entries that are generic nouns or single characters."""
    from .lexicon import acceptable_glossary_term

    book = _book(epub)
    rows = state.conn.execute(f"SELECT id, source_term, target_term FROM glossary WHERE {SERIES_SCOPE} AND verified=0 AND "
                              "origin != 'user'", (book.id,)).fetchall()
    bad = [r for r in rows if not acceptable_glossary_term(r["source_term"])]
    for r in bad:
        typer.echo(f"  {'would remove' if dry_run else 'removed'}: {r['source_term']} → {r['target_term']}")
    if not dry_run and bad:
        with Transaction(state.conn):
            state.conn.executemany("DELETE FROM glossary WHERE id=?", [(r["id"],) for r in bad])
    typer.echo(f"{len(bad)} generic entries {'found' if dry_run else 'removed'}")



# --------------------------------------------------------------------------- entities / name forms (ADR-019)

names_app = typer.Typer(help="Entities and their name forms (official name, nickname, alias …).", no_args_is_help=True)
app.add_typer(names_app, name="names")


def _name_warnings(forms: list[dict[str, Any]]) -> list[str]:
    from .qa.rules import OFFENSIVE_ZH

    out = []
    official = [f for f in forms if f["form"] in ("official", "full")]
    for f in forms:
        if any(w in (f["zh"] or "") for w in OFFENSIVE_ZH):
            out.append(f"「{f['zh']}」({f['ja']}) is a derogatory word in Chinese")
        if f["form"] in ("nickname", "short") and any(o["zh"] == f["zh"] for o in official):
            out.append(f"{f['form']} {f['ja']} has the same rendering as the official name (nickname feel lost)")
    return out


@names_app.command("list")
def names_list(epub: Annotated[str, typer.Argument()],
               all_: Annotated[bool, typer.Option("--all", help="Include single-form entities")] = False,
               as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show entities with their name forms, renderings, warnings and pending link suggestions."""
    from .memory.entities import entity_forms, senses

    book = _book(epub)
    conn = state.conn
    shared = senses(conn, book.id)
    ents = conn.execute(f"SELECT * FROM entities WHERE {SERIES_SCOPE} ORDER BY label_ja", (book.id,)).fetchall()
    forms = entity_forms(conn, book.id)
    data = []
    for e in ents:
        fs = sorted(forms.get(e["id"], []), key=lambda f: (f["form"] != "official", f["first_ordinal"] or 0))
        if not fs or (len(fs) == 1 and not all_ and not _name_warnings(fs)):
            continue
        data.append({"entity_id": e["id"], "kind": e["kind"], "forms": fs, "warnings": _name_warnings(fs),
                     "shared": [{"term": t, "also_refers_to": x["entity_id"], "note": x["note"]}
                                for t, xs in shared.items() for x in xs
                                if any(f["ja"] == t for f in fs)]})
    cands = [dict(r) for r in conn.execute("SELECT term, target_term, form_type, origin, evidence_quote FROM entity_links "
                                           f"WHERE {SERIES_SCOPE} AND status='candidate'", (book.id,))]
    if as_json:
        echo_json({"entities": data, "candidates": cands})
        return
    for d in data:
        head = next((f for f in d["forms"] if f["form"] == "official"), d["forms"][0])
        typer.echo(f"■ {head['zh']}  [{d['kind']}]  {d['entity_id']}")
        for f in d["forms"]:
            typer.echo(f"    {f['ja']:<16} {f['form']:<9} → {f['zh']}{'  ✔' if f['verified'] else ''}"
                       + (f"   ({f['note']})" if f["note"] else ""))
        for x in d["shared"]:
            other = conn.execute("SELECT label_ja FROM entities WHERE id=?", (x["also_refers_to"],)).fetchone()
            typer.echo(f"    ↔ {x['term']} may also refer to {other[0] if other else x['also_refers_to']}"
                       + (f"   ({x['note']})" if x["note"] else ""))
        for w in d["warnings"]:
            typer.echo(f"    ⚠ {w}")
    if cands:
        typer.echo("\nsuggested links (need a rendering or confirmation):")
        for c in cands:
            typer.echo(f"  {c['term']} → same as {c['target_term']} [{c['form_type']}, {c['origin']}]  {c['evidence_quote'][:60]}")


@names_app.command("link")
def names_link(epub: Annotated[str, typer.Argument()], term: Annotated[str, typer.Argument()],
               to: Annotated[str, typer.Option("--to", help="another form of the same entity")],
               form: Annotated[str, typer.Option("--type", help="nickname|short|full|alias|title|official")] = "nickname",
               zh: Annotated[str | None, typer.Option("--zh", help="(new) rendering for TERM; marks it verified")] = None,
               note: Annotated[str | None, typer.Option("--note")] = None,
               shared: Annotated[bool, typer.Option("--shared", help="TERM keeps its entity but may ALSO refer to "
                                                    "--to's (a name two people share)")] = False) -> None:
    """Declare that TERM names the same entity as --to (human decision)."""
    from .memory.entities import FORM_TYPES, add_sense, link
    from .memory.repository import set_glossary

    book = _book(epub)
    if form not in FORM_TYPES:
        raise NCTEError(f"--type must be one of {', '.join(FORM_TYPES)}")
    conn = state.conn
    if shared:
        with Transaction(conn):
            ok = add_sense(conn, book.id, term, to, origin="human", note=note)
            conn.execute(f"UPDATE entity_links SET status='superseded' WHERE {SERIES_SCOPE} AND term=? AND "
                         "target_term=? AND status='candidate'", (book.id, term, to))
        if not ok:
            raise NCTEError(f"{term} and {to} need renderings and must belong to different entities")
        typer.echo(f"{term} may now refer to {to}'s entity as well (translations are told to decide from context)")
        return
    with Transaction(conn):
        if zh:
            cat = conn.execute(f"SELECT category FROM glossary WHERE {SERIES_SCOPE} AND source_term=?", (book.id, to)).fetchone()
            set_glossary(conn, book.id, term, zh, category=cat[0] if cat else "term", verified=True)
        status = link(conn, book.id, term, to, form, origin="human", note=note)
        conn.execute(f"UPDATE entity_links SET status='superseded' WHERE {SERIES_SCOPE} AND term=? AND status='candidate'",
                     (book.id, term))
    if status != "accepted":
        raise NCTEError(f"both terms need a rendering first (use --zh for {term}, or `ncte glossary set` for {to})")
    impact = glossary_impact(conn, book.id, term)
    typer.echo(f"{term} is now a {form} of {to}; {len(impact)} units contain {term}")
    stale = [x for x in impact if not x["uses_current_target"]]
    if stale:
        typer.echo(f"{len(stale)} of them do not use the current rendering – retranslate with:\n  ncte translate BOOK "
                   "--mode context  (verified renderings invalidate exactly these units)")


@names_app.command("unlink")
def names_unlink(epub: Annotated[str, typer.Argument()], term: Annotated[str, typer.Argument()]) -> None:
    """Detach TERM from its entity (it becomes its own entity)."""
    from .memory.entities import unlink

    book = _book(epub)
    with Transaction(state.conn):
        try:
            unlink(state.conn, book.id, term)
        except KeyError as exc:
            raise NCTEError(f"unknown term {exc}") from exc
    typer.echo(f"{term} detached")


def _character(book_id: str, name: str) -> str:
    """Character id from an id, a Japanese name / alias or the Chinese name."""
    for r in state.conn.execute(f"SELECT id, canonical_name, display_name_zh, aliases_json FROM characters "
                                f"WHERE {SERIES_SCOPE}", (book_id,)):
        if name in (r["id"], r["canonical_name"], r["display_name_zh"]) or name in json.loads(r["aliases_json"]):
            return str(r["id"])
    raise NCTEError(f"unknown character {name!r} (see `ncte characters`)")


@names_app.command("address")
def names_address(epub: Annotated[str, typer.Argument()],
                  character: Annotated[str | None, typer.Option("--character", help="only forms said by or to this character")] = None,
                  as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Who calls whom what (address terms), in order of first use, with uses and the rendering kept."""
    from .series import volumes

    book = _book(epub)
    conn = state.conn
    names = {r[0]: r[1] or r[2] for r in conn.execute(
        f"SELECT id, display_name_zh, canonical_name FROM characters WHERE {SERIES_SCOPE}", (book.id,))}
    cid = _character(book.id, character) if character else None
    vols = volumes(conn)
    data = []
    for r in conn.execute(f"SELECT * FROM address_terms WHERE {SERIES_SCOPE}", (book.id,)):
        if cid and cid not in (r["speaker_id"], r["addressee_id"]):
            continue
        data.append({"id": r["id"], "speaker": names.get(r["speaker_id"], r["speaker_id"]),
                     "to": "（自稱）" if r["speaker_id"] == r["addressee_id"] else names.get(r["addressee_id"], r["addressee_id"]),
                     "ja": r["form_ja"], "zh": r["form_zh"], "zh_origin": r["zh_origin"], "uses": r["uses"],
                     "certainty": r["certainty"], "first": (vols.get(r["book_id"], 1.0), r["first_ordinal"]),
                     "note": r["note"]})
    data.sort(key=lambda d: (d["speaker"], d["to"], d["first"]))
    if as_json:
        echo_json(data)
        return
    pair = None
    for d in data:
        if (d["speaker"], d["to"]) != pair:
            pair = (d["speaker"], d["to"])
            typer.echo(f"■ {d['speaker']} → {d['to']}")
        mark = {"human": " ✔", "llm": "", "naming": " (定名)", "glossary": " (譯名表)", None: ""}.get(d["zh_origin"], "")
        typer.echo(f"    {d['ja']:<14} → {d['zh'] or '?'}{mark}   ×{d['uses']}  第{d['first'][0]:g}集 §{d['first'][1]}"
                   + ("  (推測說話者)" if d["certainty"] == "inferred" else "")
                   + (f"   {d['note']}" if d["note"] else ""))


@names_app.command("address-set")
def names_address_set(epub: Annotated[str, typer.Argument()],
                      speaker: Annotated[str, typer.Option("--speaker", help="character who says it")],
                      to: Annotated[str, typer.Option("--to", help="character called (same as --speaker = refers to self)")],
                      ja: Annotated[str, typer.Option("--ja", help="the form as written, with honorific")],
                      zh: Annotated[str, typer.Option("--zh", help="rendering to keep")],
                      note: Annotated[str | None, typer.Option("--note")] = None) -> None:
    """Fix how one character's form of address for another is rendered (human decision; never overwritten)."""
    from .memory.address import upsert

    book = _book(epub)
    conn = state.conn
    sp, ad = _character(book.id, speaker), _character(book.id, to)
    row = conn.execute("SELECT * FROM address_terms WHERE speaker_id=? AND addressee_id=? AND form_ja=?",
                       (sp, ad, ja)).fetchone()
    with Transaction(conn):
        if row is None:
            first = conn.execute("SELECT id, ordinal FROM units WHERE book_id=? AND instr(plain_text, ?) > 0 "
                                 "ORDER BY ordinal LIMIT 1", (book.id, ja)).fetchone()
            upsert(conn, book.id, sp, ad, ja, ordinal=first[1] if first else 0, unit_id=first[0] if first else None,
                   origin="human", certainty="human", form_zh=zh, note=note)
        else:
            conn.execute("UPDATE address_terms SET form_zh=?, zh_origin='human', note=COALESCE(?, note), updated_at=? "
                         "WHERE id=?", (zh, note, utcnow(), row["id"]))
    typer.echo(f"{speaker} → {to}: {ja} → {zh} (kept for later translations; existing ones are not changed — "
               "use `ncte retranslate` for specific units)")


@names_app.command("gender-scan")
def names_gender_scan(epub: Annotated[str, typer.Argument()],
                      budget_usd: Annotated[float, typer.Option("--budget-usd")] = 1.5,
                      fake: Annotated[bool, typer.Option("--fake")] = False) -> None:
    """Find characters' gender (with evidence) for a volume read before genders were recorded."""
    from .cli import _run
    from .translation.orchestrator import RunOptions

    cfg = state.cfg
    book = _book(epub)
    opts = RunOptions(mode="context", model=cfg.openrouter.model, budget_usd=budget_usd,
                      client="fake" if fake else cfg.openrouter.provider, gender_only=True)
    _run(book, opts, fake=fake, cfg=cfg)
    typer.echo(f"see: ncte names gender {epub}")


@app.command("reader-check")
def reader_check(epub: Annotated[str, typer.Argument()],
                 budget_usd: Annotated[float, typer.Option("--budget-usd")] = 1.0,
                 fake: Annotated[bool, typer.Option("--fake")] = False) -> None:
    """Read the translation like a reader who knows no Japanese; what a reader would stumble on is retranslated with
    the source (or given a translator's note) — ADR-038. For a book already translated. Recorded as the book's
    reader's-look pass (what "acceptable" reads)."""
    from .cli import _run
    from .translation.orchestrator import RunOptions

    cfg = state.cfg
    book = _book(epub)
    opts = RunOptions(mode="context", model=cfg.openrouter.model, budget_usd=budget_usd,
                      client="fake" if fake else cfg.openrouter.provider, reader_only=True)
    _run(book, opts, fake=fake, cfg=cfg)


@app.command("proofread")
def proofread_cmd(epub: Annotated[str, typer.Argument()],
                  budget_usd: Annotated[float, typer.Option("--budget-usd")] = 2.0,
                  unit_id: Annotated[list[str] | None, typer.Option("--unit-id", help="Only these units")] = None,
                  passes: Annotated[int | None, typer.Option("--passes", min=1, max=3,
                                                             help="Passes over the book (default: context.proofread_passes)")] = None,
                  fake: Annotated[bool, typer.Option("--fake")] = False) -> None:
    """Check every paragraph against its source like a proofreader; real errors (meaning, omission, addition, speaker,
    tone, voice, house style) are retranslated with the source — ADR-051. For a book already translated. Recorded as
    the book's proofreading pass (what "acceptable" reads); with --unit-id it is partial, never complete. --passes
    checks the whole current translation again after each pass's fixes (ADR-066)."""
    from .cli import _run
    from .translation.orchestrator import RunOptions

    cfg = state.cfg
    if passes is not None:
        cfg = cfg.model_copy(deep=True)
        cfg.context.proofread_passes = passes
    book = _book(epub)
    opts = RunOptions(mode="context", model=cfg.openrouter.model, budget_usd=budget_usd,
                      client="fake" if fake else cfg.openrouter.provider, proofread_only=True, unit_ids=unit_id or None)
    _run(book, opts, fake=fake, cfg=cfg)


@names_app.command("verify")
def names_verify(epub: Annotated[str, typer.Argument()],
                 budget_usd: Annotated[float, typer.Option("--budget-usd")] = 0.3,
                 fake: Annotated[bool, typer.Option("--fake")] = False) -> None:
    """The second look after reading (ADR-033, ADR-046), for a book already read: doubtful speakers and genders,
    unnamed narrators, cards that may be a character known by name, groups and their members."""
    from .cli import _run
    from .translation.orchestrator import RunOptions

    cfg = state.cfg
    book = _book(epub)
    opts = RunOptions(mode="context", model=cfg.openrouter.model, budget_usd=budget_usd,
                      client="fake" if fake else cfg.openrouter.provider, verify_only=True)
    _run(book, opts, fake=fake, cfg=cfg)


@names_app.command("relations")
def names_relations(epub: Annotated[str, typer.Argument()],
                    budget_usd: Annotated[float, typer.Option("--budget-usd")] = 0.3,
                    fake: Annotated[bool, typer.Option("--fake")] = False) -> None:
    """Summarize relations between characters from the facts (ADR-039), for a book already read."""
    from .cli import _run
    from .translation.orchestrator import RunOptions

    cfg = state.cfg
    book = _book(epub)
    opts = RunOptions(mode="context", model=cfg.openrouter.model, budget_usd=budget_usd,
                      client="fake" if fake else cfg.openrouter.provider, relations_only=True)
    _run(book, opts, fake=fake, cfg=cfg)


@names_app.command("impressions")
def names_impressions(epub: Annotated[str, typer.Argument()],
                      budget_usd: Annotated[float, typer.Option("--budget-usd")] = 0.3,
                      fake: Annotated[bool, typer.Option("--fake")] = False) -> None:
    """Personality, way of speaking and place in the story of the main characters (ADR-045), for a book already read."""
    from .cli import _run
    from .translation.orchestrator import RunOptions

    cfg = state.cfg
    book = _book(epub)
    opts = RunOptions(mode="context", model=cfg.openrouter.model, budget_usd=budget_usd,
                      client="fake" if fake else cfg.openrouter.provider, impressions_only=True)
    _run(book, opts, fake=fake, cfg=cfg)


@names_app.command("gender")
def names_gender(epub: Annotated[str, typer.Argument()],
                 who: Annotated[str | None, typer.Argument(help="character name or id; omit to list")] = None,
                 gender: Annotated[str | None, typer.Argument(help="female | male | unknown")] = None) -> None:
    """List characters' gender (with the evidence), or set one (human decision; never overwritten)."""
    from .memory.repository import set_gender

    book = _book(epub)
    conn = state.conn
    if who is None:
        for r in conn.execute("SELECT c.canonical_name, c.display_name_zh, g.gender, g.origin, g.evidence_quote, "
                              "g.certainty FROM character_gender g JOIN characters c ON c.id=g.character_id ORDER BY g.ordinal"):
            typer.echo(f"{r[1] or r[0]}  {r[2]}  ({r[3]}, {r[5]}) {r[4] or ''}")
        return
    if gender not in ("female", "male", "unknown"):
        raise typer.BadParameter("gender must be female, male or unknown")
    cid = _character(book.id, who)
    with Transaction(conn):
        if gender == "unknown":
            conn.execute("DELETE FROM character_gender WHERE character_id=?", (cid,))
        else:
            c = conn.execute("SELECT book_id, known_from_ordinal FROM characters WHERE id=?", (cid,)).fetchone()
            set_gender(conn, c[0], cid, gender, ordinal=c[1], quote=None, origin="human")
    typer.echo(f"{who}: {gender} (used by later translations; `ncte retranslate` for existing ones)")


# --------------------------------------------------------------------------- naming before translating (ADR-023)
@names_app.command("proposals")
def names_proposals(epub: Annotated[str, typer.Argument()],
                    show_all: Annotated[bool, typer.Option("--all", help="also proposals already applied or decided")] = False,
                    show_context: Annotated[bool, typer.Option("--context", help="example sentences for every item")] = False,
                    as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Renderings proposed after the read pass; pending ones wait for your choice before translating."""
    from .analysis.naming import pending

    book = _book(epub)
    rows = pending(state.conn, book.id, include_done=show_all)
    if as_json:
        echo_json([{k: v for k, v in r.items() if k != "options_json"} for r in rows])
        return
    if not rows:
        typer.echo("no proposals waiting" if not show_all else "no proposals")
        return
    for r in rows:
        state_mark = {"pending": "待確認", "applied": "已套用", "decided": "已決定"}.get(r["status"], r["status"])
        current = r["decided_zh"] or r["chosen_zh"]
        typer.echo(f"#{r['id']} [{state_mark}] {r['label']}  ×{r['importance']}  → {current}")
        for i, o in enumerate(r["options"], 1):
            typer.echo(f"    {i}. {o['zh']}　{o['reason']}")
        if r["status"] == "pending" or show_context:
            for c in r["context"]:
                typer.echo(f"       例 §{c['at']}：{c['text']}")
    if any(r["status"] == "pending" for r in rows):
        typer.echo("\nchoose: ncte names decide <book> --id N --pick K   (or --zh TEXT, or --accept-all)")
    from .analysis.naming import suspicious_characters

    odd = [c for c in suspicious_characters(state.conn, book.id) if c["also"]]
    if odd:
        typer.echo("\n⚠ characters named by a form of address that also fits another character "
                   "(their lines may be attributed wrongly):")
        for c in odd:
            typer.echo(f"    {c['name']}（{c['zh']}） ↔ {c['also']}: rename the character, and mark the form as shared: "
                       f"ncte names link <book> {c['name']} {c['also']} --shared")


@names_app.command("decide")
def names_decide(epub: Annotated[str, typer.Argument()],
                 pid: Annotated[int | None, typer.Option("--id", help="proposal id (see `names proposals`)")] = None,
                 pick: Annotated[int | None, typer.Option("--pick", help="option number")] = None,
                 zh: Annotated[str | None, typer.Option("--zh", help="your own rendering")] = None,
                 accept_all: Annotated[bool, typer.Option("--accept-all", help="take the suggestion for every pending item")] = False) -> None:
    """Choose a rendering (human decision: kept for this and later volumes, never overwritten)."""
    from .analysis.naming import decide, pending

    book = _book(epub)
    conn = state.conn
    if accept_all:
        rows = pending(conn, book.id)
        with Transaction(conn):
            for r in rows:
                decide(conn, book.id, r["id"], r["chosen_zh"])
        typer.echo(f"accepted {len(rows)} suggestion(s)")
        return
    if pid is None or (pick is None) == (zh is None):
        raise NCTEError("give --id with exactly one of --pick or --zh (or use --accept-all)")
    row = next((r for r in pending(conn, book.id, include_done=True) if r["id"] == pid), None)
    if row is None:
        raise NCTEError(f"unknown proposal id {pid}")
    if pick is not None:
        if not 1 <= pick <= len(row["options"]):
            raise NCTEError(f"--pick must be 1–{len(row['options'])}")
        zh = row["options"][pick - 1]["zh"]
    assert zh is not None
    with Transaction(conn):
        out = decide(conn, book.id, pid, zh)
    left = len(pending(conn, book.id))
    typer.echo(f"{out['label']} → {out['zh']}" + (f"   ({left} still pending)" if left else "   (all decided)"))


@names_app.command("propose")
def names_propose(epub: Annotated[str, typer.Argument()],
                  budget_usd: Annotated[float, typer.Option("--budget-usd")] = 0.5,
                  fake: Annotated[bool, typer.Option("--fake")] = False) -> None:
    """Propose renderings for a volume that was already read (analysed) — e.g. before retranslating it."""
    from .cli import _run
    from .translation.orchestrator import RunOptions

    cfg = state.cfg
    book = _book(epub)
    opts = RunOptions(mode="context", model=cfg.openrouter.model, budget_usd=budget_usd,
                      client="fake" if fake else cfg.openrouter.provider, naming_only=True)
    _run(book, opts, fake=fake, cfg=cfg)
    typer.echo(f"see: ncte names proposals {epub}")


# --------------------------------------------------------------------------- translator's notes (ADR-025)
notes_app = typer.Typer(help="Translator's notes (exported in parentheses after the paragraph).", no_args_is_help=True)
app.add_typer(notes_app, name="notes")


@notes_app.command("list")
def notes_list(epub: Annotated[str, typer.Argument()], as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Notes in the current translations, in reading order."""
    from .translation.cache import latest_texts

    book = _book(epub)
    best = latest_texts(state.conn, book.id)
    units = {r["id"]: r for r in state.conn.execute("SELECT id, ordinal, plain_text FROM units WHERE book_id=?", (book.id,))}
    rows = sorted(({"unit_id": uid, "ordinal": units[uid]["ordinal"], "ja": units[uid]["plain_text"][:60],
                    "zh": tr["text"][:60], "note": tr["translator_note"]}
                   for uid, tr in best.items() if tr["translator_note"] and tr["status"] != "human"),
                  key=lambda r: r["ordinal"])
    if as_json:
        echo_json(rows)
        return
    for r in rows:
        typer.echo(f"§{r['ordinal']} {r['unit_id']}\n    {r['ja']}\n    {r['zh']}\n    （註：{r['note']}）")
    if not rows:
        typer.echo("no notes")


@notes_app.command("found")
def notes_found(epub: Annotated[str, typer.Argument()]) -> None:
    """Wordplay and real-world references found while reading, and whether the translation carries a note."""
    from .translation.cache import latest_texts

    book = _book(epub)
    best = latest_texts(state.conn, book.id)
    rows = state.conn.execute("SELECT r.*, u.ordinal FROM reader_notes r JOIN units u ON u.id=r.unit_id WHERE "
                              "r.book_id=? ORDER BY u.ordinal", (book.id,)).fetchall()
    for r in rows:
        tr = best.get(r["unit_id"])
        has = "已加註" if tr is not None and tr["translator_note"] else "未加註"
        typer.echo(f"§{r['ordinal']} {r['unit_id']} 「{r['term']}」 [{has}]\n    {r['explanation']}"
                   + (f"\n    建議譯註：{r['note_zh']}" if r["note_zh"] else ""))
    if not rows:
        typer.echo("nothing found")


@notes_app.command("drop")
def notes_drop(epub: Annotated[str, typer.Argument()], unit_id: Annotated[str, typer.Argument()]) -> None:
    """Remove the note of one unit's current translation (the translation itself stays)."""
    from .translation.cache import latest_texts

    book = _book(epub)
    tr = latest_texts(state.conn, book.id).get(unit_id)
    if tr is None or not tr["translator_note"]:
        raise NCTEError(f"{unit_id} has no note")
    with Transaction(state.conn):
        state.conn.execute("UPDATE translations SET translator_note=NULL WHERE id=?", (tr["id"],))
    typer.echo(f"removed the note of {unit_id}")


# --------------------------------------------------------------------------- series style guide (ADR-022)

style_app = typer.Typer(help="Series style guide: honorifics, house rules, character voices (all volumes).",
                        no_args_is_help=True)
app.add_typer(style_app, name="style")


def _print_style(style: Any) -> None:
    from .memory.style import HONORIFICS_ZH

    typer.echo(f"honorifics: {style.honorifics} — {HONORIFICS_ZH[style.honorifics]}")
    for i, r in enumerate(style.rules, 1):
        typer.echo(f"rule {i}: {r}")
    for k, v in style.voices.items():
        typer.echo(f"voice {k}: {v}")
    typer.echo(f"notes: {style.notes}" + ("（譯註以括號附在段落後）" if style.notes == "inline" else "（不加譯註）"))


@style_app.command("auto")
def style_auto(epub: Annotated[str, typer.Argument()],
               budget_usd: Annotated[float, typer.Option("--budget-usd")] = 0.3,
               fake: Annotated[bool, typer.Option("--fake")] = False) -> None:
    """Let the model choose the house style for a book already read (ADR-053): honorific policy, the main characters'
    speech habits, a few rules. A style a person set is kept."""
    from .cli import _run
    from .translation.orchestrator import RunOptions

    cfg = state.cfg
    book = _book(epub)
    opts = RunOptions(mode="context", model=cfg.openrouter.model, budget_usd=budget_usd,
                      client="fake" if fake else cfg.openrouter.provider, house_style_only=True)
    _run(book, opts, fake=fake, cfg=cfg)


@style_app.command("show")
def style_show(epub: Annotated[str, typer.Argument()],
               as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show the series style guide."""
    from .memory.style import load_style

    book = _book(epub)  # selects the series database: read state.conn only afterwards
    style = load_style(state.conn, book.id)
    if as_json:
        echo_json(style.model_dump())
    else:
        _print_style(style)


@style_app.command("set")
def style_set(epub: Annotated[str, typer.Argument()],
              honorifics: Annotated[str | None, typer.Option("--honorifics", help="natural | explicit | omit")] = None,
              rule: Annotated[list[str] | None, typer.Option("--rule", help="add a house rule (repeatable)")] = None,
              remove_rule: Annotated[list[int] | None, typer.Option("--remove-rule", help="rule number to remove")] = None,
              voice: Annotated[list[str] | None, typer.Option("--voice", help="NAME=how they sound (repeatable)")] = None,
              remove_voice: Annotated[list[str] | None, typer.Option("--remove-voice")] = None,
              notes: Annotated[str | None, typer.Option("--notes", help="off | inline: translator's notes in parentheses")] = None) -> None:
    """Change the series style guide. New translations follow it; existing ones are kept until retranslated."""
    from pydantic import ValidationError

    from .memory.style import load_style, save_style

    book = _book(epub)
    conn = state.conn
    style = load_style(conn, book.id)
    data = style.model_dump()
    data["auto"] = False  # a person changed it: kept from now on (ADR-053)
    if honorifics:
        data["honorifics"] = honorifics
    if notes:
        data["notes"] = notes
    drop = {n - 1 for n in remove_rule or []}
    if any(n < 0 or n >= len(data["rules"]) for n in drop):
        raise NCTEError(f"--remove-rule must be between 1 and {len(data['rules'])}")
    data["rules"] = [r for i, r in enumerate(data["rules"]) if i not in drop] + [r.strip() for r in rule or [] if r.strip()]
    for v in voice or []:
        name, sep, note = v.partition("=")
        if not sep or not name.strip() or not note.strip():
            raise NCTEError("--voice takes NAME=description")
        data["voices"][name.strip()] = note.strip()
    for name in remove_voice or []:
        data["voices"].pop(name, None)
    try:
        new = type(style).model_validate(data)
    except ValidationError as exc:
        raise NCTEError(f"invalid style: {exc.errors()[0]['msg']}") from exc
    with Transaction(conn):
        save_style(conn, book.id, new)
    _print_style(new)
    typer.echo("applies to every volume of the series for new translations; existing translations are kept "
               "(retranslate units or run translate --force to apply it).")


# --------------------------------------------------------------------------- series (ADR-020)

series_app = typer.Typer(help="Series: several volumes sharing names, characters and plot memory.",
                         no_args_is_help=True)
app.add_typer(series_app, name="series")


@series_app.command("list")
def series_list(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List series and their volumes."""
    data = state.ws.list_series()
    if as_json:
        echo_json(data)
        return
    for s in data:
        typer.echo(f"«{s['title']}»  {s['series_id']}  {s['folder']}")
        for v in s["volumes"]:  # type: ignore[attr-defined]
            typer.echo(f"    vol {v['volume_index']:g}{(' (' + v['volume_label'] + ')') if v['volume_label'] else ''}"
                       f"  {v['book_id']}  「{v['title']}」")


@series_app.command("set-volume")
def series_set_volume(epub: Annotated[str, typer.Argument(help="EPUB path or book id")],
                      volume: Annotated[float, typer.Argument(help="new volume number (reading order)")],
                      label: Annotated[str | None, typer.Option("--label")] = None) -> None:
    """Change a book's position in its series (affects what later/earlier volumes may see)."""
    state.ws.set_volume(epub, volume, label)
    typer.echo(f"volume set to {volume:g}")


# --------------------------------------------------------------------------- name index / relationship map


def _horizon(book_id: str, upto: str | None, volume: float | None) -> tuple[Any, str]:
    from .series import INF_POS, unit_pos

    if upto and volume is not None:
        raise NCTEError("give either --upto or --volume")
    if upto:
        p = unit_pos(state.conn, upto)
        if p is None:
            raise NCTEError(f"unknown unit {upto}")
        return p, f"不劇透：只含 第{p[0]:g}集 第{p[1]}段 之前揭露的內容"
    if volume is not None:
        return (volume, 1 << 62), f"不劇透：只含到第{volume:g}集為止的內容"
    return INF_POS, "全系列（含所有已翻譯集數的內容，可能有劇透）"


def _write(book: Any, name: str, fmt: str, content: str, output: Any) -> None:
    rdir = state.cfg.workspace / "reports" / book.id
    rdir.mkdir(parents=True, exist_ok=True)
    path = output or rdir / f"{name}.{fmt}"
    path.write_text(content, encoding="utf-8")
    typer.echo(f"written: {path}")


@app.command("index")
def index_cmd(epub: Annotated[str, typer.Argument(help="any volume of the series")],
              upto: Annotated[str | None, typer.Option("--upto", help="spoiler-free: unit id")] = None,
              volume: Annotated[float | None, typer.Option("--volume", help="spoiler-free: up to the end of volume N")] = None,
              fmt: Annotated[str, typer.Option("--format", help="html | md | json")] = "html",
              output: Annotated[Path | None, typer.Option("--output", "-o")] = None) -> None:
    """Name index of the series: every character / group / place / item with all name forms and renderings."""
    from .reports.codex import _book_labels, build_index, index_html, index_markdown

    book = _book(epub)
    hz, scope = _horizon(book.id, upto, volume)
    idx = build_index(state.conn, book.id, hz)
    labels = _book_labels(state.conn, book.id)
    title = state.conn.execute("SELECT s.title FROM books b JOIN series s ON s.id=b.series_id WHERE b.id=?",
                               (book.id,)).fetchone()[0]
    out = Path(output) if output else None
    if fmt == "json":
        _write(book, "index", "json", json.dumps({"scope": scope, "entries": idx}, ensure_ascii=False, indent=1,
                                                 default=list), out)
    elif fmt == "md":
        _write(book, "index", "md", index_markdown(idx, labels, title), out)
    elif fmt == "html":
        _write(book, "index", "html", index_html(idx, labels, title, scope), out)
    else:
        raise NCTEError("--format must be html, md or json")
    typer.echo(f"{len(idx)} entries ({scope})")


@app.command("relations")
def relations_cmd(epub: Annotated[str, typer.Argument(help="any volume of the series")],
                  upto: Annotated[str | None, typer.Option("--upto", help="spoiler-free: unit id")] = None,
                  volume: Annotated[float | None, typer.Option("--volume", help="spoiler-free: up to volume N")] = None,
                  min_scenes: Annotated[int, typer.Option("--min-scenes", help="min shared scenes for an edge")] = 2,
                  fmt: Annotated[str, typer.Option("--format", help="html | md (Mermaid) | json")] = "html",
                  output: Annotated[Path | None, typer.Option("--output", "-o")] = None) -> None:
    """Character relationship map from evidence-backed facts and scene co-appearance."""
    from .reports.codex import _book_labels, build_relations, relations_html, relations_mermaid

    book = _book(epub)
    hz, scope = _horizon(book.id, upto, volume)
    rel = build_relations(state.conn, book.id, hz, min_shared_scenes=min_scenes)
    title = state.conn.execute("SELECT s.title FROM books b JOIN series s ON s.id=b.series_id WHERE b.id=?",
                               (book.id,)).fetchone()[0]
    out = Path(output) if output else None
    if fmt == "json":
        _write(book, "relations", "json", json.dumps(rel, ensure_ascii=False, indent=1, default=list), out)
    elif fmt == "md":
        _write(book, "relations", "md", f"# {title}：人物關係圖\n\n{scope}\n\n```mermaid\n{relations_mermaid(rel)}```\n", out)
    elif fmt == "html":
        _write(book, "relations", "html", relations_html(rel, _book_labels(state.conn, book.id), title, scope), out)
    else:
        raise NCTEError("--format must be html, md or json")
    typer.echo(f"{len(rel['nodes'])} characters, {len(rel['relations'])} evidence-backed relations, "
               f"{len(rel['co_appearance'])} co-appearance links ({scope})")


@series_app.command("move")
def series_move(epub: Annotated[str, typer.Argument(help="EPUB path or book id")],
                to: Annotated[str, typer.Option("--to", help="target series (title or id; created if new)")],
                volume: Annotated[float | None, typer.Option("--volume")] = None,
                label: Annotated[str | None, typer.Option("--label")] = None) -> None:
    """Move an imported book (with its translations and memory) into another series."""
    from .workspace import move_book

    stats = move_book(state.ws, epub, to, volume=volume, label=label)
    typer.echo(f"moved: units {stats.get('units', 0)}, translations {stats.get('translations', 0)}, "
               f"characters merged {stats.get('characters_merged', 0)}, terms merged {stats.get('glossary_merged', 0)}")



@names_app.command("sync")
def names_sync(epub: Annotated[str, typer.Argument()]) -> None:
    """Merge characters whose names are forms of the same entity (one character per person)."""
    from .memory.entities import reconcile_characters

    book = _book(epub)
    with Transaction(state.conn):
        n = reconcile_characters(state.conn, book.id)
    typer.echo(f"{n} duplicate characters merged")


# --------------------------------------------------------------------------- local web UI
@app.command()
def gui(port: Annotated[int, typer.Option("--port")] = 8780,
        no_open: Annotated[bool, typer.Option("--no-open", help="do not open the browser")] = False) -> None:
    """Open the local web UI: add books, translate, export, review and edit, choose renderings (this computer only)."""
    from .gui.server import serve

    serve(state.ws, port=port, open_browser=not no_open)
