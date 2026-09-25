"""Pages read from their own text, as the eighth review found them read
wrong: a paragraph over a column break, the text beside a fact box that goes
on under it, a closing line under two columns, commands typed in a monospaced
font, a manuscript typed in Courier, and a header of two rows with one group.
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


LEFT_A = [
    "Die Stadtwerke haben im Sommer das neue",
    "Leitungsnetz im Norden der Stadt in Betrieb",
    "genommen und damit viele Haushalte neu an",
    "die Fernwärme angeschlossen, auch im Winter.",
]
LEFT_B = [
    "Die Kundenzufriedenheit ist im Vergleich zum",
    "Vorjahr deutlich gestiegen, vor allem beim",
    "Service am Telefon und in den Kundenzentren",
]
RIGHT_T = [
    "der Stadt. Die Wartezeiten wurden halbiert,",
    "weil zwei neue Kolleginnen das Team der",
    "Beratung seit dem Frühjahr verstärken.",
]
RIGHT_C = [
    "Im Herbst beginnt die Sanierung des großen",
    "Verwaltungsgebäudes am Markt. Während der",
    "Bauarbeiten ziehen zwei Abteilungen um.",
]


def two_columns(tail: str | None = None) -> bytes:
    parts = ["BT /F1 10 Tf"]
    y = 700.0
    for line in LEFT_A:
        parts.append(text(72, y, line))
        y -= 13
    y -= 8
    for line in LEFT_B:
        parts.append(text(72, y, line))
        y -= 13
    bottom = y
    y = 700.0
    for line in RIGHT_T:
        parts.append(text(320, y, line))
        y -= 13
    y -= 14
    for line in RIGHT_C:
        parts.append(text(320, y, line))
        y -= 13
    if tail:
        parts.append(text(72, min(bottom, y) - 12, tail))
    parts.append("ET")
    return _pdf_from_stream("\n".join(parts))


def test_a_paragraph_over_a_column_break_is_one_paragraph():
    got = paragraphs(body(two_columns()))
    assert " ".join(LEFT_B + RIGHT_T) in got, got
    assert " ".join(LEFT_A) in got and " ".join(RIGHT_C) in got


def test_the_closing_line_under_two_columns_is_a_paragraph_of_its_own():
    tail = "Weitere Informationen finden Sie im Intranet unter dem Stichwort Mitarbeiterbrief."
    md = body(two_columns(tail))
    assert "|" not in md
    got = paragraphs(md)
    assert tail in got, got
    assert " ".join(LEFT_B + RIGHT_T) in got, got


def test_text_beside_a_fact_box_goes_on_under_it():
    beside = [
        "Die Stadt liegt am Ufer des Flusses und",
        "ist seit dem Mittelalter ein wichtiger",
        "Handelsplatz. Im neunzehnten Jahrhundert",
        "wuchs sie durch die Industrie stark an,",
        "und der Hafen wurde bald zum größten der",
        "ganzen Region. Heute ist die Stadt vor",
    ]
    under = [
        "allem für ihre Universität und ihre historische Altstadt bekannt, und",
        "der Tourismus wächst von Jahr zu Jahr.",
    ]
    facts = [
        ("Bundesland", "Hessen"),
        ("Einwohner", "58.214"),
        ("Fläche", "92,3 km²"),
        ("Höhe", "112 m"),
        ("Vorwahl", "069"),
    ]
    # The box is drawn first, as a browser draws a floated box, and set a
    # little smaller, at a pitch of its own.
    parts = ["BT"]
    for i, (label, value) in enumerate(facts):
        parts += [text(340, 700 - 16 * i, label, 9), text(450, 700 - 16 * i, value, 9)]
    parts.append("/F1 10 Tf")
    for i, line in enumerate(beside + under):
        parts.append(text(72, 700 - 13 * i, line))
    parts.append("ET")
    md = body(_pdf_from_stream("\n".join(parts)))
    assert " ".join(beside + under) in paragraphs(md), md
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in md.splitlines()
        if line.startswith("|")
    ]
    assert ["Einwohner", "58.214"] in rows, md
    assert md.index("Tourismus") < md.index("Einwohner")


def test_commands_typed_in_a_monospaced_font_are_code():
    commands = [
        "sudo apt install nginx certbot python3-certbot-nginx",
        "sudo systemctl enable --now nginx.service",
        "sudo certbot --nginx -d example.org -d www.example.org",
        "sudo systemctl reload nginx",
    ]
    parts = ["BT", "/F1 11 Tf", text(72, 720, "Installieren Sie zuerst den Webserver:")]
    parts.append("/F2 9 Tf")
    parts += [text(72, 700 - 11 * i, line) for i, line in enumerate(commands)]
    parts += ["/F1 11 Tf", text(72, 640, "Danach ist die Seite erreichbar."), "ET"]
    md = body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": COURIER}))
    assert "```\n" + "\n".join(commands) + "\n```" in md, md


def test_a_manuscript_in_courier_with_indented_paragraphs_is_text():
    first = [
        "anbei erhalten Sie Ihre Jahresabrechnung fuer den",
        "Zeitraum vom 01.01.2025 bis 31.12.2025. Der Verbrauch lag",
        "bei 3.412 kWh und damit etwa acht Prozent unter dem Jahr",
        "davor, was vor allem am milden Winter gelegen haben mag.",
    ]
    second = [
        "Aus den geleisteten Abschlaegen ergibt sich fuer Sie",
        "ein Guthaben von 84,20 Euro, das wir Ihnen in den naechsten",
        "Tagen auf das bekannte Konto ueberweisen werden, wie immer.",
    ]
    # Each paragraph's first line indented by five characters, with no
    # space between the paragraphs: a manuscript's or a pleading's layout.
    parts = ["BT /F2 10 Tf"]
    for i, line in enumerate(first + second):
        parts.append(text(102 if i in (0, len(first)) else 72, 700 - 14 * i, line))
    parts.append("ET")
    md = body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": COURIER}))
    assert "```" not in md, md
    got = paragraphs(md)
    assert " ".join(first) in got, got
    assert " ".join(second) in got, got


def test_a_header_of_two_rows_with_one_group_keeps_its_label():
    parts = ["BT /F1 10 Tf", text(72, 740, "Die Angaben gelten ab dem 1. Oktober.")]
    parts += [text(80, 713, "Tag"), text(222, 720, "Uhrzeit")]
    parts += [text(180, 706, "von"), text(280, 706, "bis")]
    days = [("Montag", "10:00", "18:00"), ("Dienstag", "10:00", "19:00")]
    days.append(("Samstag", "10:00", "14:00"))
    for r, row in enumerate(days):
        parts += [text(x, 690 - 14 * r, cell) for x, cell in zip((80, 180, 280), row)]
    parts.append("ET")
    md = body(_pdf_from_stream("\n".join(parts)))
    header = next(line for line in md.splitlines() if line.startswith("|"))
    assert [c.strip() for c in header.strip().strip("|").split("|")] == [
        "Tag",
        "Uhrzeit von",
        "Uhrzeit bis",
    ], md
