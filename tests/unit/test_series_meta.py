import pytest

from ncte.epub.parser import load_epub
from ncte.epub.series_meta import detect_series, guess_from_title, normalize_title
from ncte.samples import BookSpec, story_spec, write_book


@pytest.mark.parametrize("title,base,vol", [
    ("銀の鍵と王女 2", "銀の鍵と王女", 2), ("銀の鍵と王女　２", "銀の鍵と王女", 2),
    ("銀の鍵と王女（3）", "銀の鍵と王女", 3), ("銀の鍵と王女 第4巻", "銀の鍵と王女", 4),
    ("銀の鍵と王女 Vol.5", "銀の鍵と王女", 5), ("銀の鍵と王女 Ⅱ", "銀の鍵と王女", 2),
    ("銀の鍵と王女 III", "銀の鍵と王女", 3), ("銀の鍵と王女③", "銀の鍵と王女", 3),
    ("銀の鍵と王女 上", "銀の鍵と王女", 1), ("銀の鍵と王女（下）", "銀の鍵と王女", 2),
    ("銀の鍵と王女 2 ―星降る夜―", "銀の鍵と王女", 2), ("銀の鍵と王女 6 星降る夜", "銀の鍵と王女", 6),
    ("銀の鍵と王女 2.5", "銀の鍵と王女", 2.5),
    ("銀の鍵と王女2 (電撃文庫)", "銀の鍵と王女", 2), ("銀の鍵と王女2", "銀の鍵と王女", 2),
    ("銀の鍵と王女（3）（MF文庫J）", "銀の鍵と王女", 3), ("銀の鍵と王女 4【電子特典付き】", "銀の鍵と王女", 4),
])
def test_title_patterns(title, base, vol):
    g = guess_from_title(title)
    assert g is not None and g[0] == base and g[1] == vol


@pytest.mark.parametrize("title", ["超かぐや姫！", "86―エイティシックス―", "1Q84", "銀の鍵と王女", "Re:ゼロ",
                                   "銀の鍵と王女 (電撃文庫)"])
def test_titles_without_volume(title):
    assert guess_from_title(title) is None


def _book(tmp_path, name, **kw):
    spec = story_spec(f"urn:uuid:6f1d2a1e-0b7c-4c55-9a43-0000000000{name[-2:]}")
    for k, v in kw.items():
        setattr(spec, k, v)
    return load_epub(write_book(spec, tmp_path / f"{name}.epub"))


def test_epub3_collection_metadata(tmp_path):
    meta = ('<meta property="belongs-to-collection" id="c01">銀の鍵シリーズ</meta>'
            '<meta refines="#c01" property="collection-type">series</meta>'
            '<meta refines="#c01" property="group-position">2</meta>')
    g = detect_series(_book(tmp_path, "c_01", extra_meta=meta))
    assert (g.title, g.volume, g.source, g.confidence) == ("銀の鍵シリーズ", 2.0, "epub3_collection", "high")


def test_calibre_metadata(tmp_path):
    meta = '<meta name="calibre:series" content="銀の鍵シリーズ"/><meta name="calibre:series_index" content="3.0"/>'
    g = detect_series(_book(tmp_path, "c_02", extra_meta=meta))
    assert (g.title, g.volume, g.source) == ("銀の鍵シリーズ", 3.0, "calibre")


def test_title_and_title_sort(tmp_path):
    g = detect_series(_book(tmp_path, "c_03", title="銀の鍵と王女 2"))
    assert (g.title, g.volume, g.confidence) == ("銀の鍵と王女", 2.0, "medium")
    g = detect_series(_book(tmp_path, "c_04", extra_meta='<meta name="calibre:title_sort" content="ギンノカギ001"/>'))
    assert (g.volume, g.confidence) == (1.0, "low")
    assert detect_series(_book(tmp_path, "c_05")).source == "none"


def test_clean_title():
    from ncte.epub.series_meta import clean_title

    assert clean_title("安達としまむら (電撃文庫)") == "安達としまむら"
    assert clean_title("銀の鍵と王女（上）") == "銀の鍵と王女（上）"  # a volume marker is not a label
    assert clean_title("【電子版限定特典付き】英雄と賢者の転生婚 1 ～かつての好敵手と婚約して最強夫婦になりました～") == \
        "英雄と賢者の転生婚 1 ～かつての好敵手と婚約して最強夫婦になりました～"
    assert guess_from_title("【電子版限定特典付き】英雄と賢者の転生婚 2 ～副題～")[:2] == ("英雄と賢者の転生婚", 2.0)
    assert clean_title("【悲報】ダンジョン") == "【悲報】ダンジョン"  # a bracket that is part of the title stays


def test_normalize_title():
    assert normalize_title("超かぐや姫！") == normalize_title("超かぐや姫")
    assert normalize_title("銀の鍵と王女　") == normalize_title("銀の鍵と王女")


def test_cli_suggests_and_auto_joins(tmp_path, fixtures):
    import json

    from typer.testing import CliRunner

    from ncte.cli import app

    ws = tmp_path / "ws"
    r = CliRunner()
    assert r.invoke(app, ["-w", str(ws), "import", str(fixtures["basic"])]).exit_code == 0  # 「銀の鍵と王女」
    v2 = write_book(BookSpec(**{**story_spec("urn:uuid:6f1d2a1e-0b7c-4c55-9a43-000000000a02").__dict__,
                               "title": "銀の鍵と王女 2"}), tmp_path / "v2.epub")
    v3 = write_book(BookSpec(**{**story_spec("urn:uuid:6f1d2a1e-0b7c-4c55-9a43-000000000a03").__dict__,
                               "title": "銀の鍵と王女 3"}), tmp_path / "v3.epub")
    out = r.invoke(app, ["-w", str(ws), "inspect", str(v2), "--json"])
    sug = json.loads(out.output)["series_suggestion"]
    assert sug["series_title"] == "銀の鍵と王女" and sug["volume"] == 2.0
    out = r.invoke(app, ["-w", str(ws), "import", str(v2)])  # suggestion only
    assert "偵測到可能的系列資訊" in out.output and "書庫中已有此系列" in out.output
    out = r.invoke(app, ["-w", str(ws), "import", str(v3), "--series", "auto"])
    assert out.exit_code == 0 and "volume 3" in out.output, out.output
    data = {s["title"]: s for s in json.loads(r.invoke(app, ["-w", str(ws), "series", "list", "--json"]).output)}
    assert [v["volume_index"] for v in data["銀の鍵と王女"]["volumes"]] == [1.0, 3.0]
    assert len(data) == 2  # volume 2 was imported standalone (suggestion not applied silently)
