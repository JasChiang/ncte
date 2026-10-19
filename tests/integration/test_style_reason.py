"""B7 (ADR-067): why the house style chose its way with honorifics is kept with the style and shown on the page."""

from ncte.llm.fake import FakeLLM

from ..helpers import run_book
from ..scripted import RESPONDERS

WHY = "宮廷作品，身分敬稱代表主從"


def test_the_honorifics_reason_is_kept_and_shown(conn, cfg, fixtures):
    from ncte.gui import data
    from ncte.memory.style import load_style, style_payload

    def house(payload, req):
        return {"honorifics": "explicit", "honorifics_reason": WHY, "habits": [], "rules": ["様譯作「大人」"]}

    book, _, _ = run_book(conn, cfg, fixtures["basic"], FakeLLM(responders={**RESPONDERS, "house_style": house}),
                          mode="context")
    style = load_style(conn, book.id)
    assert style.honorifics == "explicit" and style.honorifics_reason == WHY
    assert WHY not in str(style_payload(style, "")) and "honorifics_reason" not in str(style_payload(style, ""))
    shown = data.house_style(conn, book)
    assert shown["honorifics"] == "explicit" and shown["reason"] == WHY and shown["rules"] == ["様譯作「大人」"]
