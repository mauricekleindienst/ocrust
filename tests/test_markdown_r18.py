"""Pages read from their own text, as the eighteenth review found round
seventeen's lists and headings reading them wrong: abbreviations and
inline letters at a line's start, a letter's `z. Hd.`, bold lead
sentences, numbered headings beside numbered lists, short numbered items,
a sheet's block of totals, and plain text's lettered lists, header lines
and tables of contents.
"""

from __future__ import annotations

import re

import pytest

from conftest import _winansi_literal
from ocrust.markdown import convert
from test_markdown import body as note_body
from test_markdown import convert as convert_file
from test_markdown import xlsx
from test_markdown_layouts import HELVETICA, with_fonts

HELVETICA_BOLD = (
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
)


@pytest.fixture(autouse=True)
def _no_models(monkeypatch, tmp_path):
    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))


def text(x: float, y: float, s: str) -> str:
    return f"1 0 0 1 {x} {y:.1f} Tm ({_winansi_literal(s)}) Tj"


def page(*lines: tuple[str, str, float, float], size: int = 11) -> str:
    """Lines as (font, text, x, y), F1 regular and F2 bold."""
    parts = ["BT"]
    for font, line, x, y in lines:
        parts += [f"/{font} {size} Tf", text(x, y, line)]
    parts.append("ET")
    pdf = with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": HELVETICA_BOLD})
    return convert(pdf, name="d.pdf", ocr=False).body


def test_a_list_item_wrapped_before_an_abbreviation_stays_whole():
    md = page(
        ("F1", "Die Regeln:", 72, 760),
        ("F1", "• Die Kosten werden auf alle Mieter verteilt, und zwar", 72, 740),
        ("F1", "z. B. nach der Wohnfläche oder nach dem Verbrauch.", 84, 726),
        ("F1", "• Der Vorstand prüft jedes Jahr die Kasse des Vereins.", 72, 706),
    )
    assert re.search(r"und zwar z\. B\. nach der Wohnfläche", md), md
    assert len(re.findall(r"^- ", md, re.M)) == 2, md


def test_a_letters_abbreviations_open_no_list():
    md = page(
        ("F1", "Müller & Partner GmbH", 72, 760),
        ("F1", "z. Hd. Herrn Thomas Schmidt", 72, 746),
        ("F1", "Gartenweg 3, 79098 Freiburg", 72, 732),
        ("F1", "Mit freundlichen Grüßen", 72, 690),
        ("F1", "i. A. Tom Becker", 72, 660),
    )
    assert not re.search(r"^\s*- ", md, re.M), md


def test_a_paragraph_opening_on_a_bold_sentence_filling_a_line_is_whole():
    md = page(
        ("F2", "The office will be closed on Friday for the staff outing.", 72, 760),
        ("F1", "Urgent requests can be sent to the address below and will be", 72, 746),
        ("F1", "answered on Monday morning by the first colleague in the office.", 72, 732),
    )
    blocks = [" ".join(b.split()) for b in re.split(r"\n\s*\n", md)]
    assert any(
        b.startswith("The office will be closed") and b.endswith("office.") for b in blocks
    ), md


def test_a_numbered_heading_beside_a_numbered_list_is_a_heading():
    md = page(
        ("F2", "2. Steps", 72, 760),
        ("F1", "1. Open the browser and sign in.", 72, 738),
        ("F1", "2. Check the logs for errors.", 72, 724),
        ("F2", "3. Costs and funding", 72, 694),
        ("F1", "The council approved the budget for the coming year after a long", 72, 672),
        ("F1", "debate about the costs of the new school and the old bridge.", 72, 658),
    )
    assert re.search(r"^#+ 3\. Costs and funding$", md, re.M), md


def test_short_numbered_items_without_stops_are_items_of_their_own():
    md = page(
        ("F1", "Follow these steps:", 72, 760),
        ("F1", "1. Configure the server", 72, 740),
        ("F1", "2. Open the browser", 72, 726),
        ("F1", "3. Check the logs", 72, 712),
    )
    assert "1. Configure the server\n2. Open the browser\n3. Check the logs" in md, md


def cells(r: int, *values: str | float) -> str:
    out = []
    for column, value in zip("ABCD", values):
        if value == "":
            continue
        if isinstance(value, float):
            out.append(f'<c r="{column}{r}"><v>{value}</v></c>')
        else:
            out.append(f'<c r="{column}{r}" t="inlineStr"><is><t>{value}</t></is></c>')
    return f'<row r="{r}">{"".join(out)}</row>'


def test_a_block_of_totals_with_empty_cells_stays_in_its_table():
    sheet = (
        cells(1, "Item", "Qty", "Price", "Amount")
        + cells(2, "Desk", 1.0, 300.0, 300.0)
        + cells(3, "Chair", 3.0, 95.0, 285.0)
        + cells(5, "Subtotal", "", "", 585.0)
        + cells(6, "VAT 19%", "", "", 111.15)
        + cells(7, "Total", "", "", 696.15)
    )
    md = note_body(convert_file(xlsx({"Invoice": sheet}, []), "i.xlsx").markdown)
    assert "| Chair | 3 | 95 | 285 |\n| Subtotal |  |  | 585 |" in md, md


def test_a_plain_text_list_lettered_with_two_spaces_is_a_list():
    data = b"The tenant agrees:\n\na)  to pay the rent,\nb)  to keep the rooms clean,\nc)  to keep no animals.\n"
    md = note_body(convert_file(data, "t.txt").markdown)
    assert "```" not in md and "- a) to pay the rent," in md and "- c) to keep no animals." in md, (
        md
    )


def test_plain_text_header_lines_and_contents_keep_their_lines():
    data = (
        b"From: Anna Weber <anna@example.com>\n"
        b"To: Ben Fischer <ben@example.com>; Carla Schmidt <carla@example.com>; Dieter Braun\n"
        b"Cc: Erik Lang <erik@example.com>\n"
        b"Subject: Budget\n\n"
        b"Introduction ............................ 3\n"
        b"Results of the survey ................... 7\n"
        b"Appendix ................................ 12\n"
    )
    md = note_body(convert_file(data, "m.txt").markdown)
    assert re.search(r"Dieter Braun *\\?\n+ *Cc: Erik Lang", md) or "Dieter Braun  \nCc:" in md, md
    assert re.search(r"\. 3 *(?:\\|  )?\nResults of the survey", md), md
