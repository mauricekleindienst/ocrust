"""Pages read from their own text, as the sixteenth review found round
fifteen's bold headings reading them wrong: bold phrases inside paragraphs,
lists with bold titles, a letter's signature and an invoice's total, a
figure's suspended hyphen, notes under the text, a sheet's balance row and
Word's character styles.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

import pytest

from conftest import _winansi_literal
from ocrust.markdown import convert
from test_markdown import body as note_body
from test_markdown import convert as convert_file
from test_markdown import docx, xlsx
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


def paragraphs(md: str) -> list[str]:
    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", md)]


def test_a_bold_phrase_filling_a_line_stays_in_its_paragraph():
    lines = [
        ("F1", "The office will move to the new building on Market Street in May,", 72, 700),
        ("F2", "and the registration desk in the town hall remains closed during", 72, 686),
        ("F1", "the summer holidays, as the board decided at its last meeting.", 72, 672),
        ("F1", "All other services stay where they are until the end of the year.", 72, 650),
    ]
    md = page(*lines)
    assert "#" not in md.replace("<!-- page 1 -->", ""), md
    assert " ".join(line for _, line, _, _ in lines[:3]) in paragraphs(md), md


def test_a_list_items_bold_title_keeps_its_description():
    md = page(
        ("F1", "Our services:", 72, 720),
        ("F2", "• Project management", 72, 700),
        ("F1", "Planning and steering of software projects for public clients.", 84, 686),
        ("F2", "1. Quality assurance", 72, 666),
        ("F1", "Introduction of automated tests in all of our teams.", 84, 652),
    )
    assert "#" not in md.replace("<!-- page 1 -->", ""), md
    assert re.search(r"Project management\s+Planning and steering", md), md
    assert re.search(r"1\. Quality assurance\s+Introduction of automated tests", md), md


def test_a_signature_and_a_total_set_bold_are_no_headings():
    md = page(
        ("F1", "Thank you for your order. Please find the invoice for the work below.", 72, 720),
        ("F2", "Total amount due 1,234.50 EUR", 72, 690),
        ("F1", "Please pay the amount within thirty days of the date of this invoice.", 72, 660),
        ("F1", "Kind regards", 72, 630),
        ("F2", "Dr. Maria Schneider", 72, 600),
    )
    assert "#" not in md.replace("<!-- page 1 -->", ""), md


def test_a_figures_suspended_hyphen_keeps_its_space():
    md = page(
        ("F1", "The new houses in the eastern part of the town have 1-", 72, 700),
        ("F1", "or 2-bedroom flats, and each of them has a small garden.", 72, 686),
    )
    assert "1- or 2-bedroom" in md, md


def test_text_over_its_footnotes_is_no_heading():
    body = [
        "The court has repeatedly held that the duty of care extends to all",
        "persons who could reasonably be expected to be affected by the",
        "conduct in question, and the literature has followed this view.",
    ]
    notes = [
        f"{n} See the judgment of the Federal Court of Justice and the commentary {n}."
        for n in range(1, 9)
    ]
    parts = ["BT", "/F1 12 Tf"]
    y = 740.0
    for _ in range(2):
        for line in body:
            parts.append(text(72, y, line))
            y -= 15
        y -= 10
    parts.append("/F1 9 Tf")
    y -= 20
    for note in notes:
        for half in (note[:60], note[60:] or "."):
            parts.append(text(72, y, half))
            y -= 11
    parts.append("ET")
    md = convert(with_fonts("\n".join(parts), {"F1": HELVETICA}), name="d.pdf", ocr=False).body
    assert "#" not in md.replace("<!-- page 1 -->", ""), md


def row(r: int, *values: str | float) -> str:
    out = []
    for column, value in zip("ABCD", values):
        if value == "":
            continue
        if isinstance(value, float):
            out.append(f'<c r="{column}{r}"><v>{value}</v></c>')
        else:
            out.append(f'<c r="{column}{r}" t="inlineStr"><is><t>{value}</t></is></c>')
    return f'<row r="{r}">{"".join(out)}</row>'


def test_a_balance_row_set_off_by_an_empty_row_stays_in_its_table():
    sheet = (
        row(1, "Date", "Description", "Amount")
        + row(2, "01.03.", "Salary", 2500.0)
        + row(3, "05.03.", "Rent", -1200.0)
        + row(5, "Balance", "", 1300.0)
    )
    md = note_body(convert_file(xlsx({"Konto": sheet}, []), "k.xlsx").markdown)
    assert "| 05.03. | Rent | -1200 |\n| Balance |  | 1300 |" in md, md


def test_bold_and_italic_from_words_character_styles_are_kept():
    styles = (
        '<w:style w:type="character" w:styleId="Strong"><w:rPr><w:b/></w:rPr></w:style>'
        '<w:style w:type="character" w:styleId="Emphasis"><w:rPr><w:i/></w:rPr></w:style>'
    )

    def styled(value: str, style: str = "") -> str:
        props = f'<w:rPr><w:rStyle w:val="{style}"/></w:rPr>' if style else ""
        return f'<w:r>{props}<w:t xml:space="preserve">{escape(value)}</w:t></w:r>'

    document = (
        "<w:p>"
        + styled("Please note: ")
        + styled("all offices", "Strong")
        + styled(" are closed on ")
        + styled("Monday", "Emphasis")
        + styled(".")
        + "</w:p>"
    )
    md = note_body(convert_file(docx(document, styles=styles), "n.docx").markdown)
    assert md.strip() == "Please note: **all offices** are closed on *Monday*.", md
