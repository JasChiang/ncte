"""Self-authored sample/fixture EPUB generator (no third-party book content).

All Japanese text below was written for NCTE's tests. It deliberately contains: explicit and
unattributed dialogue, one speaker talking several times in a row, a character imitating another
character's speech style, an object changing hands, an identity revealed only in a later chapter,
ruby, emphasis, links, footnotes, images and gaiji-like inline images.
"""

from __future__ import annotations

import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape


def tiny_png(width: int = 4, height: int = 4, rgb: tuple[int, int, int] = (200, 80, 80)) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


CSS = b"""body { font-family: serif; line-height: 1.8; }
p { margin: 0; text-indent: 1em; }
.tcy { text-combine-upright: all; }
img.gaiji { height: 1em; }
"""


def xhtml(title: str, body: str, lang: str = "ja") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{lang}" lang="{lang}">\n<head>\n<meta charset="UTF-8"/>\n<title>{escape(title)}</title>\n'
        '<link rel="stylesheet" type="text/css" href="../Styles/style.css"/>\n</head>\n'
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def paras(lines: list[str]) -> str:
    out = []
    for line in lines:
        if line == "":
            out.append("<p><br/></p>")
        elif line.startswith("<"):
            out.append(line)
        else:
            out.append(f"<p>{line}</p>")
    return "\n".join(out)


@dataclass
class Doc:
    id: str
    href: str  # relative to OPF dir
    title: str
    body: str
    in_toc: bool = True
    properties: str = ""


@dataclass
class BookSpec:
    title: str
    identifier: str
    docs: list[Doc]
    images: dict[str, bytes] = field(default_factory=dict)  # href -> bytes (relative to OPF dir)
    cover_href: str | None = None
    with_ncx: bool = False
    nav_in_spine: bool = False
    opf_dir: str = "OEBPS"
    opf_name: str = "content.opf"
    extra_files: dict[str, bytes] = field(default_factory=dict)  # zip path -> bytes
    layout_fixed: bool = False


def _media_type(href: str) -> str:
    ext = href.rsplit(".", 1)[-1].lower()
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
            "svg": "image/svg+xml", "css": "text/css"}.get(ext, "application/octet-stream")


def write_book(spec: BookSpec, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    d = spec.opf_dir
    pre = f"{d}/" if d else ""
    nav_items = "\n".join(
        f'<li><a href="{doc.href.split("/", 1)[-1] if doc.href.startswith("Text/") else doc.href}">{escape(doc.title)}</a></li>'
        for doc in spec.docs if doc.in_toc)
    nav_body = (f'<nav epub:type="toc" id="toc"><h1>目次</h1>\n<ol>\n{nav_items}\n</ol>\n</nav>')
    nav_doc = xhtml("目次", nav_body)
    manifest = ['<item id="nav" href="Text/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="css" href="Styles/style.css" media-type="text/css"/>']
    for doc in spec.docs:
        props = f' properties="{doc.properties}"' if doc.properties else ""
        manifest.append(f'<item id="{doc.id}" href="{doc.href}" media-type="application/xhtml+xml"{props}/>')
    for i, href in enumerate(spec.images):
        props = ' properties="cover-image"' if href == spec.cover_href else ""
        manifest.append(f'<item id="img{i}" href="{href}" media-type="{_media_type(href)}"{props}/>')
    if spec.with_ncx:
        manifest.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
    spine = []
    if spec.nav_in_spine:
        spine.append('<itemref idref="nav"/>')
    spine += [f'<itemref idref="{doc.id}"/>' for doc in spec.docs]
    layout = '<meta property="rendition:layout">pre-paginated</meta>' if spec.layout_fixed else ""
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="ja">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">{escape(spec.identifier)}</dc:identifier>
<dc:title>{escape(spec.title)}</dc:title>
<dc:language>ja</dc:language>
<dc:creator>NCTE Test Author</dc:creator>
<meta property="dcterms:modified">2026-10-01T00:00:00Z</meta>
{layout}
</metadata>
<manifest>
{chr(10).join(manifest)}
</manifest>
<spine{' toc="ncx"' if spec.with_ncx else ''} page-progression-direction="rtl">
{chr(10).join(spine)}
</spine>
</package>
"""
    files: dict[str, bytes] = {
        "META-INF/container.xml": f"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="{pre}{spec.opf_name}" media-type="application/oebps-package+xml"/></rootfiles>
</container>
""".encode(),
        f"{pre}{spec.opf_name}": opf.encode(),
        f"{pre}Text/nav.xhtml": nav_doc.encode(),
        f"{pre}Styles/style.css": CSS,
    }
    for doc in spec.docs:
        files[f"{pre}{doc.href}"] = xhtml(doc.title, doc.body).encode()
    for href, data in spec.images.items():
        files[f"{pre}{href}"] = data
    if spec.with_ncx:
        points = "\n".join(
            f'<navPoint id="np{i}" playOrder="{i + 1}"><navLabel><text>{escape(doc.title)}</text></navLabel>'
            f'<content src="{doc.href}"/></navPoint>'
            for i, doc in enumerate(x for x in spec.docs if x.in_toc))
        ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="ja">
<head><meta name="dtb:uid" content="{escape(spec.identifier)}"/></head>
<docTitle><text>{escape(spec.title)}</text></docTitle>
<navMap>
{points}
</navMap>
</ncx>
"""
        files[f"{pre}toc.ncx"] = ncx.encode()
    files.update(spec.extra_files)
    with zipfile.ZipFile(path, "w") as zf:
        zi = zipfile.ZipInfo("mimetype", date_time=(2026, 10, 1, 0, 0, 0))
        zf.writestr(zi, b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, data in files.items():
            zi = zipfile.ZipInfo(name, date_time=(2026, 10, 1, 0, 0, 0))
            zf.writestr(zi, data, compress_type=zipfile.ZIP_DEFLATED)
    return path


# ----------------------------------------------------------------------------- story text

CH1_SCENE1 = [
    "　朝の市場は、もう人であふれていた。",
    "「カイト、遅いですよ」",
    "　ミナは腕を組んで言った。",
    "「悪い悪い。寝坊しちまったんだよ」",
    "　カイトは頭をかいた。",
    "「まったく……。それで、例の鍵は持ってきましたか？」",
    "「ああ、ここにある」",
    "　カイトは銀の鍵をミナに手渡した。",
    "「ありがとうございます。大切にしますね」",
    "「おう。なくすなよ」",
    "「なくしませんよ」",
    "「本当だろうな」",
]
CH1_SCENE2 = [
    "　路地の奥に、フードをかぶった少女が立っていた。",
    "「……あなたたち、その鍵をどこで手に入れたの？」",
    "「誰だ、お前」",
    "「名乗るほどの者じゃないわ」",
    "「答えになってないぞ」",
    "「答える必要はないもの」",
    "　少女はそれだけ言うと、人混みの中へ消えた。",
    "「怪しいですね」",
]
CH2 = [
    "　翌日、二人は町外れの丘に来ていた。",
    "「ふふん、俺様に任せろってんだ！」",
    "　ミナはカイトの口調を真似て、胸を張ってみせた。",
    "「似てねえよ」",
    "「似てますよ。",
    "<p>　それに、もう一つ言いたいことがあります。明日は<em>絶対に</em>早く来てください」</p>",
    "「……わかったよ」",
    "　カイトは目をそらした。銀の鍵は、今はミナの鞄の中にある。",
    "「そろそろ、帰りましょうか」",
    "「ああ」",
    "「待って」",
]
CH3 = [
    "　夜、宿の扉を叩く音がした。",
    "　そこに立っていたのは、あのフードの少女だった。",
    "　フードの少女の正体は、この国の王女リリアだった。",
    "「隠していてごめんなさい。私はリリア。その鍵は、王家の宝物庫の鍵なの」",
    "　リリアは静かに頭を下げた。",
    "「お、王女様だったのかよ……」",
    "　カイトは呆然と呟いた。",
]


def _section(title: str, lines: list[str], sep_after: list[str] | None = None, cid: str = "") -> str:
    body = f'<section epub:type="chapter" id="{cid}">\n<h1>{escape(title)}</h1>\n' + paras(lines)
    if sep_after is not None:
        body += '\n<p class="sep">＊　＊　＊</p>\n' + paras(sep_after)
    return body + "\n</section>"


def story_spec(identifier: str = "urn:uuid:6f1d2a1e-0b7c-4c55-9a43-000000000001") -> BookSpec:
    docs = [
        Doc("ch1", "Text/ch1.xhtml", "第一章　銀の鍵", _section("第一章　銀の鍵", CH1_SCENE1, CH1_SCENE2, "c1")),
        Doc("ch2", "Text/ch2.xhtml", "第二章　丘の上で", _section("第二章　丘の上で", CH2, None, "c2")),
        Doc("ch3", "Text/ch3.xhtml", "第三章　夜の来訪者", _section("第三章　夜の来訪者", CH3, None, "c3")),
    ]
    return BookSpec("銀の鍵と王女", identifier, docs)


def media_spec() -> BookSpec:
    docs = [
        Doc("cover", "Text/cover.xhtml", "表紙",
            '<div class="cover"><img src="../Images/cover.png" alt="表紙"/></div>', in_toc=False),
        Doc("illust", "Text/illust.xhtml", "口絵",
            '<figure id="fig1"><img src="../Images/illust01.png" alt="口絵"/>'
            "<figcaption>市場で出会う二人</figcaption></figure>"),
        Doc("ch1", "Text/ch1.xhtml", "第一話　地図",
            _section("第一話　地図", [
                "　ミナは古い地図を広げた。",
                '「<a href="ch2.xhtml#place">北の塔</a>に行けば、何かわかるはずです」',
                '　詳しくは<a href="https://example.com/">公式サイト</a>を参照。',
                '<p>　挿絵は<a href="illust.xhtml#fig1">こちら</a>。<img class="gaiji" src="../Images/gaiji01.png" alt="※"/>印の場所が目的地だ。</p>',
            ], None, "c1")),
        Doc("ch2", "Text/ch2.xhtml", "第二話　北の塔",
            _section("第二話　北の塔", [
                '<p id="place">　北の塔は、霧の中にそびえていた。</p>',
                "「やっと着いたな」",
                '<p>　<a href="ch1.xhtml#c1">第一話</a>に戻る。</p>',
            ], None, "c2")),
    ]
    imgs = {"Images/cover.png": tiny_png(8, 12, (30, 60, 120)),
            "Images/illust01.png": tiny_png(16, 16, (90, 160, 90)),
            "Images/gaiji01.png": tiny_png(4, 4, (0, 0, 0))}
    return BookSpec("地図と塔", "urn:uuid:6f1d2a1e-0b7c-4c55-9a43-000000000002", docs, imgs, cover_href="Images/cover.png",
                    nav_in_spine=True, opf_dir="item", opf_name="standard.opf")


def ruby_spec() -> BookSpec:
    docs = [
        Doc("ch1", "Text/ch1.xhtml", "第一章　強敵",
            _section("第一章　強敵", [
                "　<ruby>強敵<rt>とも</rt></ruby>と呼べる相手が、俺にはいた。",
                "　彼の名は<ruby><rb>蒼</rb><rp>（</rp><rt>あお</rt><rp>）</rp></ruby>。<span class=\"tcy\">12</span>歳の春に出会った。",
                '「<strong>約束</strong>だ」<a id="ref1" href="notes.xhtml#note1" epub:type="noteref">※1</a>',
                "　<ruby>魔<rt>ま</rt>法<rt>ほう</rt></ruby>の練習は、毎朝<i>欠かさず</i>続けた。",
                "　空は、<span class=\"em\"><b>どこまでも</b>青かった</span>。",
            ], None, "c1")),
        Doc("notes", "Text/notes.xhtml", "注釈",
            '<aside epub:type="footnote" id="note1"><p><a href="ch1.xhtml#ref1">※1</a>　幼い頃に交わした約束のこと。</p></aside>',
            in_toc=False),
    ]
    return BookSpec("強敵の約束", "urn:uuid:6f1d2a1e-0b7c-4c55-9a43-000000000003", docs, with_ncx=True)


def write_fixture_set(directory: str | Path) -> dict[str, Path]:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    out = {
        "basic": write_book(story_spec(), d / "basic.epub"),
        "media": write_book(media_spec(), d / "media_links.epub"),
        "ruby": write_book(ruby_spec(), d / "ruby_notes.epub"),
    }
    out.update(write_malicious_set(d / "malicious"))
    return out


def write_malicious_set(directory: str | Path) -> dict[str, Path]:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    base = story_spec("urn:uuid:6f1d2a1e-0b7c-4c55-9a43-0000000000ff")
    out: dict[str, Path] = {}

    slip = BookSpec(**{**base.__dict__, "extra_files": {"../../evil.txt": b"pwned"}})
    out["zip_slip"] = write_book(slip, d / "zip_slip.epub")

    bomb = BookSpec(**{**base.__dict__, "extra_files": {"OEBPS/bomb.bin": b"\0" * (8 * 1024 * 1024)}})
    out["zip_bomb"] = write_book(bomb, d / "zip_bomb.epub")

    xxe_container = b"""<?xml version="1.0"?>
<!DOCTYPE container [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml">&xxe;</rootfile></rootfiles>
</container>"""
    xxe = BookSpec(**{**base.__dict__, "extra_files": {"META-INF/container.xml": xxe_container}})
    out["xxe"] = write_book(xxe, d / "xxe.epub")

    enc = b"""<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
<enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
<enc:CipherData><enc:CipherReference URI="OEBPS/Text/ch1.xhtml"/></enc:CipherData></enc:EncryptedData>
</encryption>"""
    drm = BookSpec(**{**base.__dict__, "extra_files": {"META-INF/encryption.xml": enc}})
    out["drm"] = write_book(drm, d / "drm.epub")

    fixed = BookSpec(**{**base.__dict__, "layout_fixed": True})
    out["fixed_layout"] = write_book(fixed, d / "fixed_layout.epub")
    return out
