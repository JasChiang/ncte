"""M4 acceptance on the story fixture with a scripted (sometimes wrong) analysis model."""

from ncte.analysis.scene import apply_scene_analysis, get_scene
from ncte.analysis.speaker import set_speaker_human
from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS


def utt(conn, book, text):
    return conn.execute("SELECT u.*, c.canonical_name AS speaker FROM utterances u LEFT JOIN characters c ON "
                        "c.id=u.speaker_id_nullable WHERE u.book_id=? AND u.quote_text LIKE ?",
                        (book.id, f"%{text}%")).fetchone()


def setup(conn, cfg, fixtures):
    book, rid, s = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders=RESPONDERS), mode="context")
    assert s.status == "completed"
    return book


def test_speaker_states(conn, cfg, fixtures):
    book = setup(conn, cfg, fixtures)
    cases = {
        "カイト、遅いですよ": ("ミナ", "explicit"),       # explicit tag
        "悪い悪い": ("カイト", "inferred"),              # adjacent action agrees with the model
        "本当だろうな": ("カイト", "inferred"),          # LLM claimed explicit with weak evidence → downgraded
        "俺様に任せろ": ("カイト", "inferred"),          # the model's answer stands; the action (ミナ) goes to the second look
        "待って": (None, "unknown"),                    # unknown kept; invalid id rejected
        "似てますよ": (None, "unknown"),
        "それに、もう一つ": (None, "unknown"),           # continuation inherits
        "王女様だったのかよ": ("カイト", "explicit"),     # 「…」 + カイトは呆然と呟いた
    }
    for text, (speaker, status) in cases.items():
        u = utt(conn, book, text)
        assert (u["speaker"], u["status"]) == (speaker, status), text
    src = conn.execute("SELECT resolution_source FROM utterances WHERE quote_text LIKE '%悪い悪い%'").fetchone()[0]
    assert src != "program"  # the model decided (reading or the second look), not the narration match
    cont = utt(conn, book, "それに、もう一つ")
    assert cont["continuation_of"] == utt(conn, book, "似てますよ")["id"]


def test_only_explicit_examples_enter_speech_library(conn, cfg, fixtures):
    setup(conn, cfg, fixtures)
    rows = conn.execute("SELECT e.*, u.status FROM speech_examples e JOIN utterances u ON u.id=e.utterance_id").fetchall()
    assert rows and all(r["verification_status"] == "explicit" and r["status"] == "explicit" for r in rows)
    texts = {r["raw_text"] for r in rows}
    assert not any("俺様" in t or "本当だろうな" in t for t in texts)
    rejected = conn.execute("SELECT COUNT(*) FROM memory_rejections WHERE reason LIKE 'unknown %ref%'").fetchone()[0]
    assert rejected >= 1


def test_human_correction_flow(conn, cfg, fixtures):
    book = setup(conn, cfg, fixtures)
    kaito = conn.execute("SELECT id FROM characters WHERE canonical_name='カイト'").fetchone()[0]
    u = utt(conn, book, "似てますよ")
    out = set_speaker_human(conn, u["id"], kaito, reason="editor check")
    assert len(out["utterances"]) == 2  # the continuation paragraph follows
    assert utt(conn, book, "それに、もう一つ")["status"] == "human_verified"
    ex = conn.execute("SELECT verification_status FROM speech_examples WHERE utterance_id=?", (u["id"],)).fetchone()
    assert ex[0] == "human_verified"
    assert conn.execute("SELECT COUNT(*) FROM qa_findings WHERE type='speaker_corrected'").fetchone()[0] == 2
    # re-running the automatic analysis never overwrites a human decision
    scene = get_scene(conn, book.id, u["scene_id"])
    stats = apply_scene_analysis(conn, book.id, scene, None)
    assert stats["human_kept"] == 2
    assert utt(conn, book, "似てますよ")["speaker"] == "カイト"
    # mark unknown by hand
    set_speaker_human(conn, u["id"], None)
    assert utt(conn, book, "似てますよ")["speaker"] is None
    assert conn.execute("SELECT COUNT(*) FROM speech_examples WHERE utterance_id=?", (u["id"],)).fetchone()[0] == 0


def test_translation_request_carries_speaker_status(conn, cfg, fixtures):
    fake = FakeLLM(responders=RESPONDERS)
    run_book(conn, cfg, fixtures["basic"], fake, mode="context")
    p = next(p for p, c in zip(fake.payloads, fake.calls, strict=True)
             if c.stage == "translate" and any("待って" in u["source"] for u in p["units"]))
    st = {x["quote"]: (x["speaker"], x["status"]) for x in p["scene"]["utterances"]}
    assert st["「待って」"] == (None, "unknown")
    assert st["「ふふん、俺様に任せろってんだ！」"] == ("海斗", "inferred")  # the model's (the scripted one's) answer
