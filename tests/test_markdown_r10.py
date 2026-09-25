"""Pages read from their own text, as the tenth review found round nine's
changes reading them wrong: a template's bracketed placeholders, a letter
typed in Courier, program output, the other language of a bilingual page, a
short two-column section, a one-line paragraph over an indented one, and text
set beside a picture.
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


def paragraphs(md: str) -> list[str]:
    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", md)]


def lines_at(x: float, top: float, lines: list[str], step: float = 13) -> list[str]:
    return [text(x, top - step * i, line) for i, line in enumerate(lines)]


def test_bracketed_placeholders_are_no_code():
    parts = ["BT /F1 10 Tf"]
    parts += lines_at(72, 700, ["[Your Name]", "[Street Address]", "[Email Address]"])
    parts += [text(72, 640, "[Signature page follows]"), "ET"]
    md = body(_pdf_from_stream("\n".join(parts)))
    assert "```" not in md and "[Signature page follows]" in md


def test_a_letter_typed_in_courier_is_no_code_block():
    parts = ["BT /F2 10 Tf"]
    parts += lines_at(72, 740, ["Hans Meier", "Lindenweg 4", "79098 Freiburg"])
    parts += [text(300, 690, "Freiburg, 3. Maerz 2025")]
    parts += lines_at(72, 660, ["Stadtwerke Freiburg", "Kundenservice", "79098 Freiburg"])
    parts += [text(72, 610, "Sehr geehrte Damen und Herren,")]
    parts += lines_at(
        72,
        590,
        [
            "anbei sende ich Ihnen den Zaehlerstand vom 1. Maerz. Bitte",
            "passen Sie die Abschlaege entsprechend an, wie besprochen.",
        ],
    )
    parts += [text(300, 550, "Mit freundlichen Gruessen"), "ET"]
    md = body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": COURIER}))
    assert "```" not in md, md


def test_program_output_in_a_monospaced_font_is_code_a_record_a_line():
    log = [
        "2025-03-14 09:12:01 INFO Server started on port 8080",
        "2025-03-14 09:12:05 WARN Cache directory not found, creating it",
        "2025-03-14 09:13:44 ERROR Connection to database refused",
        "2025-03-14 09:13:50 INFO Retrying in 5 seconds",
    ]
    parts = ["BT /F1 11 Tf", text(72, 740, "Das Protokoll zeigt den Fehler:"), "/F2 9 Tf"]
    parts += lines_at(72, 720, log, 11)
    parts.append("ET")
    md = body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": COURIER}))
    assert "```\n" + "\n".join(log) + "\n```" in md, md


GERMAN = [
    "Die Stadtwerke haben im Sommer das neue",
    "Leitungsnetz im Norden der Stadt in Betrieb",
    "genommen und damit viele Haushalte neu an",
    "die Fernwaerme angeschlossen, auch im Winter.",
    "Die Kundenzufriedenheit ist im Vergleich zum",
    "Vorjahr deutlich gestiegen, vor allem beim",
    "Service am Telefon und in den Kundenzentren",
]


def test_the_other_language_beside_a_column_is_no_continuation_whatever_it_is():
    polish = [
        "jesienia rozpocznie sie remont budynku",
        "administracji miejskiej przy rynku, a",
        "dostawy sa zapewnione nawet podczas mrozow.",
    ]
    parts = ["BT /F1 10 Tf", *lines_at(72, 700, GERMAN), *lines_at(320, 700, polish), "ET"]
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert " ".join(polish) in got, got


def test_a_short_column_ending_on_a_little_word_goes_on_in_the_next():
    left = ["Die Versorgung ist auch bei grosser Kaelte", "gesichert, wie die Tests zeigen. Die"]
    right = ["Kundenzufriedenheit ist deutlich gestiegen,", "vor allem beim Service am Telefon."]
    parts = ["BT /F1 10 Tf", *lines_at(72, 700, left), *lines_at(320, 700, right), "ET"]
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert " ".join(left + right) in got, got


def test_a_one_line_paragraph_over_an_indented_one_stays_its_own():
    parts = ["BT /F1 11 Tf", text(72, 700, "und ging dann nach Hause.")]
    parts += [text(90, 686, "Der Regen hatte am Abend aufgehoert, und die Strassen")]
    parts += lines_at(
        72,
        672,
        [
            "glaenzten im Licht der Laternen, als sie spaeter noch einmal",
            "durch die Altstadt zum Bahnhof ging und dort auf den Zug wartete.",
        ],
        14,
    )
    parts.append("ET")
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert "und ging dann nach Hause." in got, got


def test_text_set_beside_a_picture_wraps_where_its_lines_end():
    beside = [
        "Die Stadtwerke haben im Sommer das neue",
        "Leitungsnetz im Norden der Stadt in Betrieb",
        "genommen und damit die Versorgung gesichert.",
        "Ueber dreitausend Haushalte sind jetzt an",
        "die Fernwaerme angeschlossen, auch im Winter.",
    ]
    under = [
        "Im Herbst beginnt die Sanierung des Verwaltungsgebaeudes am Markt, und waehrend der",
        "Bauarbeiten ziehen die Abteilungen Einkauf und Personal in das Gebaeude am Hafen um.",
    ]
    parts = ["BT /F1 10 Tf", *lines_at(72, 700, beside), *lines_at(72, 620, under), "ET"]
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert " ".join(beside) in got, got
