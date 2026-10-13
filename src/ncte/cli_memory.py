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
           as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List scenes (deterministic segmentation) and manage manual overrides."""
    from .analysis.scene import segment_book

    book = _book(epub)
    conn = state.conn
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
        "(SELECT plain_text FROM units u WHERE u.id=s.start_unit_id) AS first FROM scenes s WHERE book_id=? "
        "ORDER BY seq", (book.id,)).fetchall()
    data = [{"scene_id": r["id"], "units": r["n"], "break": r["break_reason"], "analyzed_version": r["version"],
             "start_unit_id": r["start_unit_id"], "first_text": r["first"][:40]} for r in rows]
    if as_json:
        echo_json(data)
        return
    for d in data:
        typer.echo(f"{d['scene_id']}  {d['units']:4d} units  break={d['break']:<11} v{d['analyzed_version']}  "
                   f"{d['start_unit_id']}  {d['first_text']}")


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
    opts = RunOptions(mode="baseline", model=cfg.openrouter.model, budget_usd=budget_usd, unit_ids=unit_id or None,
                      client="fake" if fake else "openrouter", review_only=True)
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
    client = make_client(cfg, fake=fake)
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
    from .memory.entities import entity_forms

    book = _book(epub)
    conn = state.conn
    ents = conn.execute(f"SELECT * FROM entities WHERE {SERIES_SCOPE} ORDER BY label_ja", (book.id,)).fetchall()
    forms = entity_forms(conn, book.id)
    data = []
    for e in ents:
        fs = sorted(forms.get(e["id"], []), key=lambda f: (f["form"] != "official", f["first_ordinal"] or 0))
        if not fs or (len(fs) == 1 and not all_ and not _name_warnings(fs)):
            continue
        data.append({"entity_id": e["id"], "kind": e["kind"], "forms": fs, "warnings": _name_warnings(fs)})
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
               note: Annotated[str | None, typer.Option("--note")] = None) -> None:
    """Declare that TERM names the same entity as --to (human decision)."""
    from .memory.entities import FORM_TYPES, link
    from .memory.repository import set_glossary

    book = _book(epub)
    if form not in FORM_TYPES:
        raise NCTEError(f"--type must be one of {', '.join(FORM_TYPES)}")
    conn = state.conn
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


@names_app.command("detect")
def names_detect(epub: Annotated[str, typer.Argument()]) -> None:
    """Scan the whole book for «A。通称B» style statements and link the forms (no API calls)."""
    from .memory.entities import backfill_entities, detect_links

    book = _book(epub)
    conn = state.conn
    with Transaction(conn):
        backfill_entities(conn, book.id)
        rows = conn.execute("SELECT * FROM units WHERE book_id=? AND spine_index IS NOT NULL ORDER BY ordinal",
                            (book.id,)).fetchall()
        found = detect_links(conn, book.id, rows)
    for term, target, st in found:
        typer.echo(f"  {term} → {target}: {st}")
    typer.echo(f"{len(found)} statements found")


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
