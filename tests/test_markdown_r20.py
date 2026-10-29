"""Pages read from their own text, as the twentieth review found round
nineteen's lists reading them wrong: lists set without space under their
lead-in or over the paragraph after them, sub-lists counting on, lists set
in under a lead-in, tables counted down their first column, numbered
headings over lists of bold titles, roman figures set right, and a
heading run on over a narrow column's gutter.
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


def page(*runs: tuple, size: float = 11) -> str:
    """Runs as (font, text, x, y) or (font, text, x, y, size); F1 regular,
    F2 bold."""
    parts = ["BT"]
    for font, run, x, y, *rest in runs:
        parts += [
            f"/{font} {rest[0] if rest else size} Tf",
            f"1 0 0 1 {x} {y:.1f} Tm ({_winansi_literal(run)}) Tj",
        ]
    parts.append("ET")
    pdf = with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": HELVETICA_BOLD})
    return convert(pdf, name="d.pdf", ocr=False).body


def blocks(md: str) -> list[str]:
    md = md.replace("<!-- page 1 -->", "")
    return [" ".join(b.split()) for b in re.split(r"\n\s*\n", md) if b.strip()]


OPENING = [
    ("F1", "The board of the club met on Monday evening in the small hall of the", 72, 760),
    ("F1", "town library to agree on the budget and the plans for the next year.", 72, 746),
]


def test_a_list_set_without_space_under_a_one_line_lead_in_is_a_list():
    md = page(
        *OPENING,
        ("F1", "The board agreed on these points.", 72, 718),
        ("F1", "1. Budget cut", 90, 704),
        ("F1", "2. New posts in the support team", 90, 690),
        ("F1", "3. Review of the figures", 90, 676),
    )
    assert "The board agreed on these points." in blocks(md), md
    assert re.search(r"^1\. Budget cut\n2\. New posts in the support team\n3\. Review", md, re.M), (
        md
    )


def test_the_paragraph_set_without_space_under_a_list_is_its_own():
    tail = "The meeting closed at five and the next one is set for June."
    for x in (72, 90):
        md = page(
            *OPENING,
            ("F1", "The following points were agreed:", 72, 718),
            ("F1", "1. The budget is cut by ten percent.", x, 704),
            ("F1", "2. New posts", x, 690),
            ("F1", "3. Next meeting", x, 676),
            ("F1", tail, 72, 662),
        )
        assert tail in blocks(md), md


def test_a_sub_list_counting_on_from_the_item_before_is_a_sub_list():
    md = page(
        ("F1", "The contract covers:", 72, 760),
        ("F1", "1. Payment terms", 72, 740),
        ("F1", "(a) The rent is paid on the first working day of the month.", 90, 726),
        ("F1", "(b) Late payments are charged at the base rate.", 90, 712),
        ("F1", "2. Obligations of the tenant", 72, 698),
        ("F1", "(c) The rooms are cleaned every week by the tenant.", 90, 684),
        ("F1", "(d) Repairs are reported to the landlord at once.", 90, 670),
    )
    items = [line.strip() for line in md.splitlines() if re.match(r"\s*(?:-|\d+\.)\s", line)]
    assert any(i.endswith("(c) The rooms are cleaned every week by the tenant.") for i in items), md
    assert "2. Obligations of the tenant" in items, md


def test_a_list_set_in_under_a_lead_in_without_a_stop_is_a_list():
    md = page(
        *OPENING,
        ("F1", "The next steps for the team are as follows", 72, 718),
        ("F1", "1. Order the new chairs", 105, 704),
        ("F1", "2. Ask the landlord for a new contract", 105, 690),
    )
    assert "The next steps for the team are as follows" in blocks(md), md
    assert re.search(r"^1\. Order the new chairs\n2\. Ask the landlord", md, re.M), md


def test_a_table_counted_down_its_first_column_stays_a_table():
    md = page(
        ("F1", "The following actions were agreed at the meeting of the board.", 72, 760),
        ("F2", "No.", 72, 736),
        ("F2", "Task", 130, 736),
        ("F1", "1.", 72, 722),
        ("F1", "Order the new chairs", 130, 722),
        ("F1", "2.", 72, 708),
        ("F1", "Call the landlord", 130, 708),
        ("F1", "3.", 72, 694),
        ("F1", "Book the hall for June", 130, 694),
    )
    assert "| No. | Task |" in md and "| 2. | Call the landlord |" in md, md


def test_numbered_headings_over_lists_of_bold_titles_stay_headings():
    runs = [("F1", "This plan sets out the work of the coming year for the team.", 72, 770)]
    y = 740.0
    for head in ("1. Next steps", "2. Scope"):
        runs.append(("F2", head, 72, y))
        y -= 20
        for k, title in enumerate(("Timeline", "Budget")):
            runs += [("F1", f"{k + 1}.", 90, y), ("F2", title, 104, y)]
            runs.append(("F1", f"The board agreed that the {title.lower()} is cut.", 104, y - 14))
            y -= 32
    md = page(*runs)
    assert re.search(r"^#+ 1\. Next steps$", md, re.M), md
    assert re.search(r"^#+ 2\. Scope$", md, re.M), md


def test_roman_figures_set_right_are_items_of_their_own():
    marks = ["i.", "ii.", "iii.", "iv.", "v.", "vi.", "vii.", "viii.", "ix.", "x."]
    runs = [("F1", "The house rules:", 72, 770)]
    for k, mark in enumerate(marks):
        y = 750 - 14 * k
        # Right-aligned at 100, as a browser sets `type=i`.
        width = 3.1 * len(mark)
        runs += [("F1", mark, 100 - width, y), ("F1", f"Rule number {k + 1} of the house.", 106, y)]
    md = page(*runs)
    items = [line.strip() for line in md.splitlines() if re.match(r"\s*-\s", line)]
    assert len(items) == len(marks), md


def test_bold_titled_items_without_a_hanging_indent_are_items():
    runs = [("F1", "The plan for the coming year has three parts.", 72, 770)]
    y = 748.0
    for k, title in enumerate(("Training", "Hiring", "Review")):
        runs += [("F1", f"{k + 1}.", 72, y), ("F2", title, 86, y)]
        runs.append(("F1", f"The board agreed that the {title.lower()} goes ahead.", 72, y - 14))
        y -= 32
    md = page(*runs)
    assert "#" not in md.replace("<!-- page 1 -->", ""), md


def test_a_heading_run_on_over_its_columns_gutter_reads_in_columns():
    left = [
        "Die diesjährige Jahresversammlung des Vereins",
        "findet am Freitag im großen Saal des Heims",
        "statt. Auf der Tagesordnung stehen die Berichte",
        "des Vorstands und die Planung des Haushalts",
        "für das kommende Jahr und die Wahl des neuen",
        "Kassenwarts, der die Kasse ab Juli führt.",
    ]
    right = [
        "Unsere Jugendabteilung sucht weiterhin neue",
        "Übungsleiterinnen und Übungsleiter für die",
        "Gruppen am Montag und am Mittwoch nachmittag.",
        "Interessierte melden sich bitte im Büro des",
        "Vereins oder direkt bei der Vorsitzenden, die",
        "gern alle Fragen zur Arbeit mit den Kindern",
        "beantwortet und die Termine nennt.",
    ]
    # 28 points of Helvetica-Bold run from 72 to 331.9, where the second
    # column starts.
    runs = [("F2", "Vereinsnachrichten", 72, 752, 28)]
    for k, line in enumerate(left):
        runs.append(("F1", line, 72, 724 - 14 * k, 10))
    for k, line in enumerate(right):
        runs.append(("F1", line, 332, 768 - 14 * k, 10))
    md = page(*runs)
    assert "|" not in md, md
    text = " ".join(md.split())
    assert "# Vereinsnachrichten" in md, md
    assert "Die diesjährige Jahresversammlung des Vereins findet am Freitag" in text, md
    assert "Unsere Jugendabteilung sucht weiterhin neue Übungsleiterinnen" in text, md


def test_two_plain_text_header_lines_keep_a_line_each():
    data = (
        b"Subject: Budget review for the second quarter and the new posts in the support team\n"
        b"Date: Monday, 3 May 2024 10:15\n\n"
        b"The board agreed that the budget for the coming year is cut.\n"
    )
    md = note_body(convert_file(data, "n.txt").markdown)
    lines = [" ".join(line.replace("\\", "").split()) for line in md.splitlines()]
    assert "Date: Monday, 3 May 2024 10:15" in lines, md
