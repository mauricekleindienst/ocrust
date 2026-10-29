"""Pages read from their own text, as the nineteenth review found round
eighteen's lists reading them wrong: an outer item after its sub-list, a
sub-list at Word's indent, a count inside an item, Word's `o` bullets and
tabbed numbers, bold items over their markers, and a wrapped sentence of
plain text with colons in it.
"""

from __future__ import annotations

import re

import pytest

from conftest import _winansi_literal
from ocrust.markdown import convert
from test_markdown import body as note_body
from test_markdown import convert as convert_file
from test_markdown_layouts import HELVETICA, with_fonts

HELVETICA_BOLD = (
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
)


@pytest.fixture(autouse=True)
def _no_models(monkeypatch, tmp_path):
    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))


def text(x: float, y: float, s: str) -> str:
    return f"1 0 0 1 {x} {y:.1f} Tm ({_winansi_literal(s)}) Tj"


def page(*runs: tuple[str, str, float, float]) -> str:
    """Runs as (font, text, x, y), F1 regular and F2 bold, at 11 points."""
    parts = ["BT"]
    for font, run, x, y in runs:
        parts += [f"/{font} 11 Tf", text(x, y, run)]
    parts.append("ET")
    pdf = with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": HELVETICA_BOLD})
    return convert(pdf, name="d.pdf", ocr=False).body


def items(md: str) -> list[str]:
    return [line.strip() for line in md.splitlines() if re.match(r"\s*(?:[-*]|\d+\.)\s", line)]


def test_an_outer_item_after_its_sub_list_is_an_outer_item():
    md = page(
        ("F1", "The contract covers:", 72, 760),
        ("F1", "1. Payment terms", 72, 740),
        ("F1", "a. The rent is paid on the first working day of each month.", 90, 726),
        ("F1", "b. Late payments are charged at the base rate.", 90, 712),
        ("F1", "2. Obligations of the tenant", 72, 698),
    )
    got = items(md)
    assert "2. Obligations of the tenant" in got, md
    assert any(i.endswith("b. Late payments are charged at the base rate.") for i in got), md


def test_a_count_inside_an_item_at_its_text_indent_goes_on_the_item():
    md = page(
        ("F1", "The plan:", 72, 760),
        ("F1", "1. The new office opens in the spring, and the move", 72, 740),
        ("F1", "follows in the second quarter of the year.", 84, 726),
        ("F1", "2. The old building is sold by the end of the year, at the latest im", 72, 712),
        ("F1", "3. Quartal, as the board decided at its last meeting.", 84, 698),
    )
    assert "im 3. Quartal, as the board decided" in md, md


def test_words_o_bullets_and_tabbed_numbers_are_list_items():
    md = page(
        ("F1", "Rooms to clean:", 72, 760),
        ("F1", "•", 72, 740),
        ("F1", "Ground floor", 90, 740),
        ("F1", "o", 90, 726),
        ("F1", "Kitchen", 108, 726),
        ("F1", "o", 90, 712),
        ("F1", "Cellar", 108, 712),
        ("F1", "Agenda:", 72, 680),
        ("F1", "1.", 72, 660),
        ("F1", "Budget cut", 90, 660),
        ("F1", "2.", 72, 646),
        ("F1", "New members", 90, 646),
    )
    assert "|" not in md, md
    got = items(md)
    assert any(i.endswith("Kitchen") for i in got) and any(i.endswith("Cellar") for i in got), md
    assert "1. Budget cut" in got and "2. New members" in got, md


def test_bold_items_over_their_markers_stay_a_list():
    runs = []
    for k, title in enumerate(["Planning", "Building", "Testing and roll-out"]):
        y = 740 - 18 * k
        runs += [("F1", f"{k + 1}.", 72, y), ("F2", title, 86, y)]
    md = page(("F1", "The project runs in three phases:", 72, 760), *runs)
    assert "#" not in md.replace("<!-- page 1 -->", ""), md
    assert items(md) == ["1. Planning", "2. Building", "3. Testing and roll-out"], md


def test_a_wrapped_plain_text_sentence_with_colons_is_one_paragraph():
    data = (
        b"Background: The team finished the review of the new contract last week, and\n"
        b"Next steps: we tell the landlord before the end of the month and wait.\n"
    )
    md = note_body(convert_file(data, "n.txt").markdown)
    assert "  \n" not in md and "\\\n" not in md, md
