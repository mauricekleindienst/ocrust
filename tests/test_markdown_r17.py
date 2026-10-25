"""Pages read from their own text, as the seventeenth review found round
sixteen's rules reading them wrong: numbered headings in bold, headings
with versions or exclamation marks, a salutation over a heading, bold
questions over their answers, second tables in a sheet; and lists lettered
`a)`, an OpenDocument's nested lists and plain text in columns.
"""

from __future__ import annotations

import re

import pytest

from conftest import _winansi_literal
from ocrust.markdown import convert
from test_markdown import ODF, package, xlsx
from test_markdown import body as note_body
from test_markdown import convert as convert_file
from test_markdown_layouts import HELVETICA, with_fonts

HELVETICA_BOLD = (
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
)
TEXT = [
    "The council has approved the budget for the coming year after a long",
    "debate about the costs of the new school and the repairs to the old",
    "bridge over the river, which many members of the public asked for.",
]


@pytest.fixture(autouse=True)
def _no_models(monkeypatch, tmp_path):
    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))


def text(x: float, y: float, s: str) -> str:
    return f"1 0 0 1 {x} {y:.1f} Tm ({_winansi_literal(s)}) Tj"


def sections(*headings: str, size: int = 11, gap: float = 22) -> str:
    """Bold headings at `size`, each over three lines of 11-point text."""
    parts, y = ["BT"], 760.0
    for heading in headings:
        parts += [f"/F2 {size} Tf", text(72, y, heading), "/F1 11 Tf"]
        y -= gap
        for line in TEXT:
            parts.append(text(72, y, line))
            y -= 14
        y -= 12
    parts.append("ET")
    pdf = with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": HELVETICA_BOLD})
    return convert(pdf, name="d.pdf", ocr=False).body


def test_numbered_headings_set_bold_are_headings():
    md = sections("1. Introduction", "2. Methods", "3. Results")
    for heading in ("1. Introduction", "2. Methods", "3. Results"):
        assert re.search(rf"^#+ {re.escape(heading)}$", md, re.M), md


def test_bold_numbered_items_one_after_another_are_a_list():
    parts = ["BT", "/F1 11 Tf", text(72, 760, "The project runs in three steps:"), "/F2 11 Tf"]
    parts += [text(72, 740 - 16 * k, f"{k + 1}. Step number {k + 1}") for k in range(3)]
    parts += ["/F1 11 Tf", text(72, 680, "Each step ends with a short review of the results.")]
    parts.append("ET")
    pdf = with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": HELVETICA_BOLD})
    md = convert(pdf, name="d.pdf", ocr=False).body
    assert "#" not in md.replace("<!-- page 1 -->", ""), md
    assert "1. Step number 1\n2. Step number 2\n3. Step number 3" in md, md


def test_headings_naming_a_version_or_ending_in_an_exclamation_mark_are_headings():
    md = sections("What's new in version 2.10", "Save the date!", size=13)
    assert re.search(r"^#+ What's new in version 2\.10$", md, re.M), md
    assert re.search(r"^#+ Save the date!$", md, re.M), md


def test_a_heading_under_a_salutation_is_a_heading():
    parts = ["BT", "/F1 11 Tf", text(72, 760, "Dear parents,"), "/F2 11 Tf"]
    parts += [text(72, 734, "School trip to Berlin"), "/F1 11 Tf"]
    parts += [text(72, 712 - 14 * k, line) for k, line in enumerate(TEXT)]
    parts.append("ET")
    pdf = with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": HELVETICA_BOLD})
    md = convert(pdf, name="d.pdf", ocr=False).body
    assert re.search(r"^#+ School trip to Berlin$", md, re.M), md


def test_a_bold_question_running_to_the_edge_stays_apart_from_its_answer():
    question = "How can I change the address that my monthly invoices are sent to?"
    parts = ["BT", "/F2 11 Tf", text(72, 760, question), "/F1 11 Tf"]
    parts += [text(72, 746 - 14 * k, line) for k, line in enumerate(TEXT)]
    parts.append("ET")
    pdf = with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": HELVETICA_BOLD})
    md = convert(pdf, name="d.pdf", ocr=False).body
    blocks = [" ".join(b.split()) for b in re.split(r"\n\s*\n", md)]
    assert any(b.endswith(question) for b in blocks), md
    assert " ".join(TEXT) in blocks, md


def test_lettered_items_are_items_of_their_own():
    items = [
        "a) The tenant pays the rent on the first working day of each month.",
        "b) The landlord keeps the roof and the heating in good order.",
        "c) The rooms are returned at the end of the lease in good order.",
    ]
    parts = ["BT", "/F1 10 Tf", text(72, 760, "The parties agree on the following duties:")]
    parts += [text(80, 744 - 13 * k, item) for k, item in enumerate(items)]
    parts.append("ET")
    md = convert(with_fonts("\n".join(parts), {"F1": HELVETICA}), name="d.pdf", ocr=False).body
    for item in items:
        assert f"- {item}" in md, md


def row(r: int, *values: str | float) -> str:
    out = []
    for column, value in zip("ABCD", values):
        if isinstance(value, float):
            out.append(f'<c r="{column}{r}"><v>{value}</v></c>')
        else:
            out.append(f'<c r="{column}{r}" t="inlineStr"><is><t>{value}</t></is></c>')
    return f'<row r="{r}">{"".join(out)}</row>'


def test_a_second_table_headed_by_day_numbers_stays_a_table_of_its_own():
    sheet = (
        row(1, "Team A", 1.0, 2.0, 3.0)
        + row(2, "Anna", 8.0, 7.0, 8.0)
        + row(3, "Bob", 3.0, 4.0, 2.0)
        + row(5, "Team B", 1.0, 2.0, 3.0)
        + row(6, "Carol", 6.0, 6.0, 5.0)
        + row(7, "Dan", 2.0, 8.0, 4.0)
    )
    md = note_body(convert_file(xlsx({"Hours": sheet}, []), "h.xlsx").markdown)
    assert "| Team B | 1 | 2 | 3 |\n| --- |" in md, md


def test_an_opendocuments_nested_list_has_its_own_levels_kind():
    content = (
        f"<office:document-content {ODF}><office:automatic-styles>"
        '<text:list-style style:name="L1">'
        '<text:list-level-style-number text:level="1" style:num-format="1"/>'
        '<text:list-level-style-bullet text:level="2" text:bullet-char="•"/>'
        "</text:list-style></office:automatic-styles><office:body><office:text>"
        '<text:list text:style-name="L1"><text:list-item><text:p>Move the shop</text:p>'
        "<text:list><text:list-item><text:p>redirect product pages</text:p></text:list-item>"
        "<text:list-item><text:p>redirect category pages</text:p></text:list-item></text:list>"
        "</text:list-item></text:list>"
        "</office:text></office:body></office:document-content>"
    )
    data = package({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": content})
    md = note_body(convert_file(data, "p.odt").markdown)
    assert re.search(r"1\. Move the shop\n\s+- redirect product pages", md), md


def test_plain_text_in_columns_keeps_its_lines():
    notes = (
        "Attendees:\n  Anna Weber     Chair\n  Bob Smith      Minutes\n  Carol Jones    Finance\n"
    )
    md = note_body(convert_file(notes.encode(), "notes.txt").markdown)
    assert "Attendees:\n\n```\nAnna Weber     Chair\nBob Smith      Minutes\n" in md, md
