"""Tables read from a PDF's own text, as the sixth review found them read
wrong: rows that leave a cell empty, a table drawn with its borders, a
letter over an invoice's items, a browser's header over a table running on
from the page before, and a header of two rows.
"""

from __future__ import annotations

import re

import pytest

from conftest import _pdf_from_stream, _winansi_literal
from ocrust import Box, Document, Line, Page, Segment
from ocrust.markdown import convert
from ocrust.markdown._ir import Note
from ocrust.markdown._render import render
from ocrust.markdown._scan import blocks_of


@pytest.fixture(autouse=True)
def _no_models(monkeypatch, tmp_path):
    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))


def grid(body: str) -> list[list[str]]:
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in body.splitlines()
        if line.startswith("|") and not re.match(r"^\| *:?-", line)
    ]


def text(x: float, y: float, s: str) -> str:
    return f"1 0 0 1 {x} {y:.1f} Tm ({_winansi_literal(s)}) Tj"


def bordered(rows: list[list[str]], xs: list[float], right: float, top: float = 700.0) -> bytes:
    """A table set tight, 12 points a row at 10 points, with a border round
    every cell, as a browser or Word draws it: thin filled bars."""
    parts = ["BT /F1 10 Tf"]
    for r, row in enumerate(rows):
        for x, cell in zip(xs, row, strict=True):
            if cell:
                parts.append(text(x + 3, top - 12 * r, cell))
    parts.append("ET")
    # Each row's box runs from 3 points under its baseline to 9 over it.
    edges = [top + 9.5] + [top - 12 * r - 2.5 for r in range(len(rows))]
    for y in edges:
        parts.append(f"{xs[0]:.1f} {y:.1f} {right - xs[0]:.1f} 0.5 re f")
    for x in [*xs, right]:
        parts.append(f"{x:.1f} {edges[-1]:.1f} 0.5 {edges[0] - edges[-1]:.1f} re f")
    return _pdf_from_stream("\n".join(parts))


def test_rows_of_a_bordered_table_that_leave_a_cell_empty_stay_rows():
    contacts = [
        ["Name", "Telefon", "E-Mail"],
        ["Eva Roth", "030 1234", "eva@example.de"],
        ["Maximilian Berger", "", "max@example.de"],
        ["Jan Ott", "030 9876", ""],
        ["Christina Hoffmann", "", "chris@example.de"],
        ["Tom Ulm", "040 5555", "tom@example.de"],
    ]
    body = convert(bordered(contacts, [72, 180, 250], 360), name="k.pdf", ocr=False).body
    assert grid(body) == contacts


def test_a_row_with_only_its_middle_cell_filled_is_a_row_of_the_bordered_table():
    rows = [
        ["Gruppe", "Posten", "Betrag"],
        ["Hardware", "Server", "2.400,00"],
        ["", "Switch", ""],
        ["", "", "120,00"],
        ["Software", "Lizenz", "300,00"],
    ]
    body = convert(bordered(rows, [72, 160, 240], 320), name="g.pdf", ocr=False).body
    assert grid(body) == rows


def test_the_subject_and_salutation_of_a_letter_stay_out_of_its_item_table():
    parts = ["BT /F1 10 Tf"]
    address = ["Herrn", "Firma Beispiel AG", "Hauptstrasse 12", "12345 Berlin"]
    info = ["Datum: 14.09.2026", "Kundennummer: K-4711", "Ihr Zeichen: AS/12", "Telefon: 030 1234"]
    for i, (a, b) in enumerate(zip(address, info, strict=True)):
        parts += [text(72, 740 - 12 * i, a), text(380, 740 - 12 * i, b)]
    parts += [
        text(72, 680, "Rechnung Nr. 815"),
        text(72, 662, "Sehr geehrte Frau Schmidt,"),
        text(72, 644, "fuer unsere Leistungen berechnen wir Ihnen:"),
    ]
    items = [("Pos.", "Leistung", "Betrag")] + [
        (str(i), f"Wartung Anlage {i}", f"{i * 120},00") for i in range(1, 5)
    ]
    for r, row in enumerate(items):
        for x, cell in zip((80, 120, 300), row, strict=True):
            parts.append(text(x, 620 - 14 * r, cell))
    parts.append("ET")
    body = convert(_pdf_from_stream("\n".join(parts)), name="r.pdf", ocr=False).body
    lines = [line.strip() for line in body.replace("  \n", "\n").splitlines()]
    assert "Sehr geehrte Frau Schmidt," in lines
    assert "Rechnung Nr. 815" in lines
    assert "| Pos. | Leistung | Betrag |" in lines
    assert not any("Sehr geehrte" in line and "|" in line for line in lines)


def test_a_header_of_two_rows_titles_each_column_with_its_group():
    parts = ["BT /F1 10 Tf"]
    parts += [text(196, 700, "2024"), text(316, 700, "2025")]
    parts.append(text(80, 694, "Region"))
    parts += [text(x, 688, t) for x, t in zip((150, 210, 270, 330), ("Umsatz", "Gewinn") * 2)]
    for r, (name, *figures) in enumerate(
        [("Nord", "1.200", "150", "1.350", "170"), ("Sued", "980", "90", "1.020", "110")]
        + [("West", "1.540", "210", "1.600", "230")]
    ):
        y = 674 - 14 * r
        parts.append(text(80, y, name))
        parts += [text(x, y, f) for x, f in zip((155, 215, 275, 335), figures)]
    parts.append("ET")
    body = convert(_pdf_from_stream("\n".join(parts)), name="k.pdf", ocr=False).body
    assert grid(body)[0] == ["Region", "2024 Umsatz", "2024 Gewinn", "2025 Umsatz", "2025 Gewinn"]
    assert "2024 2025" not in body


def _line(text: str, x0: float, y0: float, x1: float, parts=()) -> Line:
    box = Box(x0, y0, x1, y0 + 20)
    segments = tuple(Segment(t, Box(a, y0, b, y0 + 20), 1.0) for t, a, b in parts)
    return Line(text=text, box=box, confidence=1.0, angle=0.0, segments=segments)


def test_a_browsers_date_and_title_are_running_heads_where_one_page_sets_them_as_one_line():
    from ocrust import Block

    def page(index: int, merged: bool) -> Page:
        if merged:
            top = [
                _line(
                    "9/25/26, 1:07 AM Inventar",
                    40,
                    10,
                    900,
                    parts=(("9/25/26, 1:07 AM", 40, 200), ("Inventar", 800, 900)),
                )
            ]
        else:
            top = [_line("9/25/26, 1:07 AM", 40, 10, 200), _line("Inventar", 800, 10, 900)]
        body = [_line(f"Absatz {index} mit Text.", 100, 300 + 30 * i, 700) for i in range(3)]
        blocks = [Block(kind="paragraph", box=line.box, lines=[line]) for line in top]
        blocks.append(Block(kind="paragraph", box=Box(100, 300, 700, 400), lines=body))
        return Page(index, 1000, 1400, 0.0, "pdf_text", blocks, 0.0)

    doc = Document("x.pdf", [page(0, False), page(1, True), page(2, False)], 0.0)
    out = render(Note(blocks=blocks_of(doc, paged=False)), {})
    assert "1:07 AM" not in out and "Inventar" not in out
    assert "Absatz 1 mit Text." in out
