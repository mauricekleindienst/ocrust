"""Reading order and line joins on pages read from their own text, as the
sixth review found them wrong: a paragraph beside a list or a fact box, a
letter's reference block, a code listing, two columns under an abstract,
Chinese and Japanese lines, and a hyphen after an abbreviation.
"""

from __future__ import annotations

import pytest

from conftest import _pdf_from_stream, _winansi_literal
from ocrust import Block, Box, Document, Line, Page, Word
from ocrust.markdown import convert
from ocrust.markdown._ir import Note
from ocrust.markdown._render import render
from ocrust.markdown._scan import _joined, blocks_of
from test_markdown_layouts import HELVETICA, with_fonts

COURIER = "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"


@pytest.fixture(autouse=True)
def _no_models(monkeypatch, tmp_path):
    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))


def text(x: float, y: float, s: str) -> str:
    return f"1 0 0 1 {x} {y:.1f} Tm ({_winansi_literal(s)}) Tj"


def body(pdf: bytes) -> str:
    return convert(pdf, name="d.pdf", ocr=False).body


def lines_of(md: str) -> list[str]:
    return [line.strip() for line in md.replace("  \n", "\n").splitlines()]


def test_a_paragraph_beside_a_numbered_list_is_a_paragraph_then_the_list():
    left = [
        "Die Umsetzung erfolgt in drei Phasen",
        "ueber insgesamt neun Monate. Jede",
        "Phase endet mit einer Abnahme.",
    ]
    right = ["1. Konzeption (Q1)", "2. Pilotbetrieb (Q2)", "3. Rollout (Q3)"]
    # Each text box drawn whole, as a slide or a page does; a table draws its
    # rows in turn.
    parts = ["BT /F1 12 Tf"]
    parts += [text(72, 700 - 15 * i, line) for i, line in enumerate(left)]
    parts += [text(360, 700 - 15 * i, line) for i, line in enumerate(right)]
    parts.append("ET")
    md = body(_pdf_from_stream("\n".join(parts)))
    assert "|" not in md
    assert " ".join(left) in md
    assert "1. Konzeption (Q1)\n2. Pilotbetrieb (Q2)\n3. Rollout (Q3)" in md


def test_a_letters_address_and_reference_block_keep_a_line_each():
    address = ["Firma", "Beispiel AG", "Frau Anna Schmidt", "Hauptstrasse 12", "10115 Berlin"]
    info = [
        "Rechnungsnummer: RE-2026-0815",
        "Rechnungsdatum: 12.09.2026",
        "Kundennummer: K-4711",
        "Leistungszeitraum: August 2026",
    ]
    parts = ["BT /F1 10 Tf"]
    parts += [text(72, 650 - 13 * i, line) for i, line in enumerate(address)]
    parts += [text(360, 650 - 13 * i, line) for i, line in enumerate(info)]
    parts += [text(72, 560, "Sehr geehrte Frau Schmidt, wir berechnen Ihnen wie folgt.")]
    parts.append("ET")
    lines = lines_of(body(_pdf_from_stream("\n".join(parts))))
    assert all(line in lines for line in address + info), lines
    assert not any("|" in line for line in lines)


def test_a_code_listing_is_code_and_the_text_around_it_is_no_heading():
    code = ["def process(values):"] + [
        f"    result_{i} = compute(value_{i}, factor={i})" for i in range(12)
    ]
    code.append("    return results")
    parts = ["BT", "/F1 12 Tf", text(72, 720, "Run the processing function over every value.")]
    parts.append("/F2 10 Tf")
    parts += [text(72, 700 - 12 * i, line) for i, line in enumerate(code)]
    parts += ["/F1 12 Tf", text(72, 520, "Next, save the results to disk."), "ET"]
    md = body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": COURIER}))
    assert "#" not in md
    assert "```\n" + "\n".join(code) + "\n```" in md


def _line(text: str, x0: float, y0: float, x1: float, words: bool = False) -> Line:
    parts = text.split()
    step = (x1 - x0) / max(len(text), 1)
    word_boxes: list[Word] = []
    at = 0
    for part in parts if words else []:
        start = text.index(part, at)
        word_boxes.append(
            Word(part, Box(x0 + start * step, y0, x0 + (start + len(part)) * step, y0 + 20), 1.0)
        )
        at = start + len(part)
    return Line(
        text=text,
        box=Box(x0, y0, x1, y0 + 20),
        confidence=1.0,
        angle=0.0,
        words=tuple(word_boxes),
    )


def test_lines_of_a_paragraph_beside_a_box_end_where_the_box_begins():
    left = [
        _line(t, 100, 100 + 24 * i, x1)
        for i, (t, x1) in enumerate(
            [
                ("Im neunzehnten Jahrhundert wuchs die Stadt", 560),
                ("durch die Industrie stark an, und der Hafen", 575),
                ("wurde zum wichtigsten der ganzen Region.", 530),
            ]
        )
    ]
    box = [_line("Einwohner 58.214", 620, 100, 900), _line("Hoehe 112 m", 620, 124, 900)]
    below = [
        _line("Heute ist die Stadt vor allem fuer ihre Universitaet und Altstadt", 100, 200, 900),
        _line("bekannt, und der Tourismus waechst jedes Jahr.", 100, 224, 700),
    ]
    blocks = [
        Block(kind="paragraph", box=Box(100, 100, 575, 168), lines=left),
        Block(kind="paragraph", box=Box(620, 100, 900, 144), lines=box),
        Block(kind="paragraph", box=Box(100, 200, 900, 244), lines=below),
    ]
    doc = Document("x.pdf", [Page(0, 1000, 1400, 0.0, "pdf_text", blocks, 0.0)], 0.0)
    out = render(Note(blocks=blocks_of(doc, paged=False)), {})
    assert (
        "Im neunzehnten Jahrhundert wuchs die Stadt durch die Industrie stark an, und der "
        "Hafen wurde zum wichtigsten der ganzen Region." in out
    )


def test_chinese_and_japanese_lines_run_on_without_a_space():
    lines = [_line("项目的下一阶段", 100, 100, 400), _line("将于四月开始。", 100, 124, 300)]
    assert _joined(lines, 400) == ["项目的下一阶段", "将于四月开始。"]
    mixed = [_line("Der Plan", 100, 100, 400), _line("geht weiter.", 100, 124, 300)]
    assert _joined(mixed, 400) == ["Der Plan", " ", "geht weiter."]


def test_a_hyphen_after_an_abbreviation_at_a_line_end_stays():
    parts = ["BT /F1 10 Tf"]
    lines = ["Wir liefern seit Jahren IT-", "basierte Loesungen fuer den Mittelstand", "und fuer"]
    parts += [text(72, 700 - 12 * i, line) for i, line in enumerate(lines)]
    parts.append("ET")
    md = body(_pdf_from_stream("\n".join(parts)))
    assert "IT-basierte" in md and "ITbasierte" not in md
    parts = ["BT /F1 10 Tf"]
    lines = ["Beratung fuer die gesamte IT-", "und Telekommunikationsbranche im Land", "und mehr"]
    parts += [text(72, 700 - 12 * i, line) for i, line in enumerate(lines)]
    parts.append("ET")
    assert "IT- und Telekommunikationsbranche" in body(_pdf_from_stream("\n".join(parts)))


def test_a_drop_cap_starts_its_paragraph_again():
    lines = [
        "ie Stadt liegt am Ufer des Flusses und ist",
        "seit dem Mittelalter ein wichtiger Ort fuer",
        "den Handel mit Salz, Holz und Tuch gewesen.",
    ]
    parts = ["BT", "/F1 40 Tf", text(72, 672, "D"), "/F1 12 Tf"]
    parts += [text(104, 700 - 14 * i, line) for i, line in enumerate(lines)]
    parts += [text(72, 658, "Im neunzehnten Jahrhundert wuchs die Stadt stark an."), "ET"]
    md = body(_pdf_from_stream("\n".join(parts)))
    assert md.index("Die Stadt liegt am Ufer") < md.index("seit dem Mittelalter")
    assert "DIm" not in md and "Dden" not in md


def test_zapfdingbats_ticks_and_crosses_are_read_without_ocr():
    dingbats = "<< /Type /Font /Subtype /Type1 /BaseFont /ZapfDingbats >>"
    parts = [
        "BT",
        "/F2 10 Tf",
        text(72, 700, "4"),
        "/F1 10 Tf",
        text(90, 700, "Lieferung geprueft"),
    ]
    parts += ["/F2 10 Tf", text(72, 686, "8"), "/F1 10 Tf", text(90, 686, "Rechnung offen"), "ET"]
    md = body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": dingbats}))
    assert "✔ Lieferung geprueft" in md and "✘ Rechnung offen" in md
