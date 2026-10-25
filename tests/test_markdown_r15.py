"""Pages read from their own text, as the fifteenth review found them read
wrong: compounds broken next to a figure, words broken at a soft hyphen,
slides' titles stating their point, headings set in a bold face but little
larger than the text, and spreadsheets' title rows and second tables.
"""

from __future__ import annotations

import re

import pytest

from conftest import _pdf_from_stream, _winansi_literal
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


def body(pdf: bytes) -> str:
    return convert(pdf, name="d.pdf", ocr=False).body


def prose(*lines: str) -> str:
    parts = ["BT /F1 11 Tf", *(text(72, 700 - 14 * i, line) for i, line in enumerate(lines))]
    parts.append("ET")
    return body(_pdf_from_stream("\n".join(parts)))


def test_a_compound_broken_next_to_its_figure_is_whole():
    md = prose(
        "The report counts all the hospital admissions that were COVID-19-",
        "related in the last year, and the number of COVID-",
        "19 patients who needed intensive care in the whole region.",
    )
    assert "COVID-19-related" in md and "COVID-19 patients" in md, md


def test_a_word_broken_at_a_soft_hyphen_is_whole():
    md = prose(
        "Die Stadt hat die Arbeiten zur Instand­-",
        "haltung der Brücken im Frühjahr ausgeschrieben.",
    )
    assert "Instandhaltung der Brücken" in md, md


def test_a_slides_title_stating_its_point_over_lines_is_a_heading():
    title = [
        "Revenue grew by twenty percent in 2024,",
        "driven mainly by strong demand in the",
        "two new markets in Asia.",
    ]
    parts = ["BT /F1 26 Tf", *(text(45, 360 - 30 * i, line) for i, line in enumerate(title))]
    parts.append("/F1 18 Tf")
    parts += [text(60, 250 - 26 * k, f"- Point {k + 1} of the year") for k in range(3)]
    parts.append("ET")
    md = body(_pdf_from_stream("\n".join(parts)))
    assert f"# {' '.join(title)}" in md, md


def test_headings_set_bold_and_but_a_little_larger_are_headings():
    paragraph = [
        "The council has approved the budget for the coming year after a long",
        "debate about the costs of the new school and the repairs to the old",
        "bridge over the river, which many members of the public asked for.",
    ]
    parts, y = ["BT"], 760.0
    for heading, size in (("1 Background", 13), ("1.1 Data sources", 11)):
        parts += [f"/F2 {size} Tf", text(72, y, heading), "/F1 11 Tf"]
        y -= 22
        for line in paragraph:
            parts.append(text(72, y, line))
            y -= 14
        y -= 12
    parts.append("ET")
    md = body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": HELVETICA_BOLD}))
    assert re.search(r"^#+ 1 Background$", md, re.M), md
    assert re.search(r"^#+ 1\.1 Data sources$", md, re.M), md


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


def test_a_sheets_title_row_over_its_header_is_no_header():
    sheet = (
        cells(1, "Umsatzübersicht 2024")
        + cells(2, "Region", "Q1", "Q2")
        + cells(3, "Nord", 120.0, 130.0)
        + cells(4, "Süd", 90.0, 95.0)
    )
    md = note_body(convert_file(xlsx({"Umsatz": sheet}, []), "u.xlsx").markdown)
    assert "Umsatzübersicht 2024\n\n| Region | Q1 | Q2 |" in md, md


def test_a_small_table_under_a_table_is_a_table_of_its_own():
    sheet = (
        cells(1, "Region", "Jan", "Feb")
        + cells(2, "West", 70.0, 75.0)
        + cells(3, "East", 60.0, 65.0)
        + cells(5, "Year", "2023", "2024")
        + cells(6, "Growth", "5%", "7%")
    )
    md = note_body(convert_file(xlsx({"Sales": sheet}, []), "s.xlsx").markdown)
    assert "| Year | 2023 | 2024 |\n| --- |" in md, md
