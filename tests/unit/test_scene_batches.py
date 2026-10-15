from ncte.translation.orchestrator import scene_batches


def _rows(spec):
    out = []
    for i, (scene, text) in enumerate(spec):
        out.append({"id": f"u{i}", "ordinal": i, "kind": "text", "resource_href": "c1.xhtml", "scene_id": scene,
                    "plain_text": text})
    return out


def test_one_batch_per_scene():
    rows = _rows([("s1", "　地の文。" * 10)] * 70 + [("s2", "「台詞」")] * 5)
    bs = scene_batches(rows, 6000)
    assert [len(b.rows) for b in bs] == [70, 5] and not any(b.whole_scene for b in bs)


def test_long_scene_split_evenly_at_a_quiet_point_and_sees_the_whole_scene():
    talk, narr = "「" + "あ" * 270 + "」", "　" + "地" * 270
    spec = [("s1", narr if i % 6 in (0, 1) else talk) for i in range(40)]  # 40 × ~270 ≈ 10,900 chars → two parts
    bs = scene_batches(_rows(spec), 6000)
    assert len(bs) == 2 and all(b.whole_scene for b in bs)
    cut = bs[1].rows[0]
    prev = bs[0].rows[-1]
    assert not cut["plain_text"].startswith("「") and not prev["plain_text"].startswith("「")  # between narration
    assert abs(len(bs[0].rows) - len(bs[1].rows)) <= 8
