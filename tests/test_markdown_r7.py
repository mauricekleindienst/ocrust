"""Pages read from their own text, as the seventh review found round six's
changes reading them wrong: text typed in a monospaced font, a caption over a
table, numbered steps beside what they mean, a table header set smaller than
its rows, and totals set a little apart from the items.
"""

from __future__ import annotations

import re

import pytest

from conftest import _pdf_from_stream, _winansi_literal
from ocrust.markdown import convert
from test_markdown_layouts import HELVETICA, with_fonts

COURIER = "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"


@pytest.fixture(autouse=True)
def _no_models(monkeypatch, tmp_path):
    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))


def text(x: float, y: float, s: str, size: float | None = None) -> str:
    font = f"/F1 {size} Tf " if size else ""
    return f"{font}1 0 0 1 {x} {y:.1f} Tm ({_winansi_literal(s)}) Tj"


def body(pdf: bytes) -> str:
    return convert(pdf, name="d.pdf", ocr=False).body


def grid(md: str) -> list[list[str]]:
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in md.splitlines()
        if line.startswith("|") and not re.match(r"^\| *:?-", line)
    ]


def test_a_letter_typed_in_courier_is_text_not_code():
    lines = [
        "Sehr geehrte Damen und Herren, wir bestaetigen Ihnen",
        "hiermit den Eingang Ihrer Bestellung vom 12.03.2026 und",
        "werden die Ware in der kommenden Woche an Sie liefern.",
    ]
    parts = ["BT /F2 10 Tf"] + [text(72, 700 - 12 * i, line) for i, line in enumerate(lines)]
    parts.append("ET")
    md = body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": COURIER}))
    assert "```" not in md
    assert " ".join(lines) in md


def test_a_caption_over_a_table_is_no_group_of_its_header():
    parts = ["BT /F1 10 Tf", text(180, 720, "Tabelle 3: Umsatz nach Region")]
    rows = [("Region", "2023", "2024", "2025")] + [
        (name, "1.200", "1.350", "1.410") for name in ("Nord", "Sued", "West")
    ]
    for r, row in enumerate(rows):
        parts += [text(x, 700 - 14 * r, cell) for x, cell in zip((80, 200, 280, 360), row)]
    parts.append("ET")
    md = body(_pdf_from_stream("\n".join(parts)))
    assert grid(md)[0] == ["Region", "2023", "2024", "2025"]
    assert md.count("Tabelle 3") == 1


def test_numbered_steps_beside_what_they_mean_stay_rows():
    steps = [
        ("1. Antrag", "bis 31. Maerz beim Amt einreichen"),
        ("2. Pruefung", "dauert etwa sechs Wochen"),
        ("3. Bescheid", "kommt per Post"),
        ("4. Auszahlung", "innerhalb von 14 Tagen"),
    ]
    # Drawn column by column, as two text boxes of a slide are.
    parts = ["BT /F1 11 Tf"]
    parts += [text(72, 700 - 15 * i, label) for i, (label, _) in enumerate(steps)]
    parts += [text(200, 700 - 15 * i, value) for i, (_, value) in enumerate(steps)]
    parts.append("ET")
    md = body(_pdf_from_stream("\n".join(parts)))
    lines = [line.strip() for line in md.replace("  \n", "\n").splitlines()]
    for label, value in steps:
        assert any(label in line and value in line for line in lines), (label, lines)


def test_a_table_header_set_smaller_than_its_rows_is_its_header():
    parts = ["BT"]
    parts += [text(x, 700, t, 8) for x, t in zip((80, 220, 320), ("PRODUKT", "MENGE", "PREIS"))]
    rows = [("Beratung", "8 h", "960,00"), ("Umsetzung", "24 h", "2.880,00")]
    rows += [("Schulung", "4 h", "480,00")]
    for r, row in enumerate(rows):
        parts += [text(x, 684 - 16 * r, cell, 10) for x, cell in zip((80, 220, 320), row)]
    parts.append("ET")
    got = grid(body(_pdf_from_stream("\n".join(parts))))
    assert got[0] == ["PRODUKT", "MENGE", "PREIS"]
    assert got[1] == ["Beratung", "8 h", "960,00"]


def test_totals_set_a_little_apart_stay_in_the_items_table():
    parts = ["BT /F1 10 Tf"]
    rows = [("Pos.", "Leistung", "Betrag"), ("1", "Wartung", "120,00"), ("2", "Pruefung", "74,10")]
    for r, row in enumerate(rows):
        parts += [text(x, 700 - 14 * r, cell) for x, cell in zip((80, 120, 320), row)]
    parts += [text(120, 650, "Summe netto"), text(320, 650, "194,10")]
    parts += [text(120, 636, "MwSt. 19 %"), text(320, 636, "36,88")]
    parts.append("ET")
    tables = [
        block for block in body(_pdf_from_stream("\n".join(parts))).split("\n\n") if "|" in block
    ]
    assert len(tables) == 1 and "Summe netto" in tables[0]
