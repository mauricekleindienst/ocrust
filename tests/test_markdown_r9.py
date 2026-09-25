"""Pages read from their own text, as the ninth review found round eight's
changes reading them wrong: a column that is no continuation of the one
beside it, a caption at a column's head, a letter's address beside its
reference block, phone numbers and commands, short listings, and a drop cap.
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


LEFT = [
    "Die Stadtwerke haben im Sommer das neue",
    "Leitungsnetz im Norden der Stadt in Betrieb",
    "genommen und damit viele Haushalte neu an",
    "die Fernwärme angeschlossen, auch im Winter.",
    "Die Kundenzufriedenheit ist im Vergleich zum",
    "Vorjahr deutlich gestiegen, vor allem beim",
    "Service am Telefon und in den Kundenzentren",
]


def beside(right: list[str], x: float = 320, size: float = 10) -> bytes:
    parts = ["BT /F1 10 Tf"]
    parts += [text(72, 700 - 13 * i, line) for i, line in enumerate(LEFT)]
    parts += [text(x, 700 - 13 * i, line, size) for i, line in enumerate(right)]
    parts.append("ET")
    return _pdf_from_stream("\n".join(parts))


def test_the_other_language_beside_a_column_is_no_continuation():
    english = [
        "The municipal utility commissioned the",
        "new network in the north of the city in",
        "the summer, and many homes are now on it.",
    ]
    got = paragraphs(body(beside(english)))
    assert " ".join(english) in got, got
    assert not any("Kundenzentren The" in p for p in got), got


def test_a_sidebar_beside_a_column_is_no_continuation():
    sidebar = ["Weihnachtsfeier am 18. Dezember", "im Ratskeller, ab 18 Uhr."]
    got = paragraphs(body(beside(sidebar, x=380, size=9)))
    assert not any("Kundenzentren Weihnachtsfeier" in p for p in got), got


def test_a_caption_at_the_head_of_a_column_is_no_continuation():
    right = [
        "Abbildung 3: Der neue Hafen im Sommer",
        "",
        "der Stadt. Die Wartezeiten wurden halbiert,",
        "weil zwei neue Kolleginnen das Team der",
        "Beratung seit dem Frühjahr verstärken.",
    ]
    parts = ["BT /F1 10 Tf"]
    parts += [text(72, 700 - 13 * i, line) for i, line in enumerate(LEFT)]
    parts += [text(320, 700 - 13 * i, line) for i, line in enumerate(right) if line]
    parts.append("ET")
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert "Abbildung 3: Der neue Hafen im Sommer" in got, got
    assert not any("Kundenzentren Abbildung" in p for p in got), got


def test_a_letters_address_beside_its_reference_block_keeps_a_line_each():
    address = ["Herrn Jonas Weber", "Postfach 10 20 30", "20095 Hamburg"]
    info = ["Kundennummer 4711-0815", "Rechnung R-2026-117", "12.03.2026"]
    parts = ["BT /F1 10 Tf"]
    parts += [text(72, 650 - 13 * i, line) for i, line in enumerate(address)]
    parts += [text(380, 650 - 13 * i, line) for i, line in enumerate(info)]
    parts += [text(72, 580, "Ihre Jahresabrechnung 2025")]
    parts += [text(72, 555, "Sehr geehrte Damen und Herren,")]
    parts.append("ET")
    lines = [line.strip() for line in body(_pdf_from_stream("\n".join(parts))).splitlines()]
    for line in address:
        assert any(re.sub(r"\s*\|?\s*$", "", got).endswith(line) for got in lines), lines


def test_phone_numbers_in_a_text_font_are_no_code():
    phones = [
        "Zentrale 030 1234 5670",
        "Vertrieb 030 1234 5671",
        "Service 030 1234 5672",
        "Buchhaltung 030 1234 5673",
    ]
    parts = ["BT /F1 10 Tf"]
    parts += [text(72, 700 - 13 * i, line) for i, line in enumerate(phones)]
    parts.append("ET")
    assert "```" not in body(_pdf_from_stream("\n".join(parts)))


def courier(listing: list[str], before: str = "So wird der Dienst eingerichtet:") -> str:
    parts = ["BT", "/F1 11 Tf", text(72, 740, before), "/F2 9 Tf"]
    y = 720.0
    for line in listing:
        if line:
            indent = len(line) - len(line.lstrip())
            parts.append(text(72 + 5.4 * indent, y, line.strip()))
        y -= 11
    parts.append("ET")
    return body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": COURIER}))


def test_install_commands_naming_packages_are_code():
    commands = [
        "sudo apt update",
        "sudo apt install nginx postgresql redis certbot git",
        "sudo systemctl enable nginx postgresql redis",
        "sudo systemctl restart nginx postgresql redis",
    ]
    md = courier(commands)
    assert "```\n" + "\n".join(commands) + "\n```" in md, md


def test_a_short_listing_is_code_with_its_indents_and_its_blank_lines():
    listing = [
        "[server]",
        "host = 0.0.0.0",
        "port = 8080",
        "",
        "[database]",
        "url = postgres://db/app",
        "pool = 10",
    ]
    md = courier(listing)
    assert "```\n" + "\n".join(listing) + "\n```" in md, md
    yaml = [
        "server:",
        "  host: 0.0.0.0",
        "  port: 8080",
        "  workers: 4",
        "logging:",
        "  level: info",
        "  file: /var/log/app.log",
        "database:",
        "  url: postgres://db/app",
    ]
    md = courier(yaml, "Die Server werden über eine zentrale Datei konfiguriert.")
    assert "```\n" + "\n".join(yaml) + "\n```" in md, md
    assert "#" not in md.replace("<!--", "")


def test_json_in_a_text_font_is_code():
    listing = ["{", '"name": "aurora",', '"version": "1.4.2",', '"private": true', "}"]
    parts = ["BT /F1 10 Tf"]
    parts += [text(72, 700 - 12 * i, line) for i, line in enumerate(listing)]
    parts.append("ET")
    md = body(_pdf_from_stream("\n".join(parts)))
    assert '```\n{\n  "name": "aurora",\n  "version": "1.4.2",\n  "private": true\n}\n```' in md


def test_a_drop_cap_beside_its_paragraph_starts_no_new_one():
    lines = [
        "er Winter kam früh in diesem Jahr.",
        "Schon Anfang November lag Schnee auf",
        "den Höhen, und die Bauern mussten das Vieh vorzeitig",
        "von den Weiden holen, weil es kalt wurde.",
    ]
    parts = ["BT", "/F1 36 Tf", text(72, 676, "D"), "/F1 12 Tf"]
    parts += [text(100, 700, lines[0]), text(100, 686, lines[1])]
    parts += [text(72, 672, lines[2]), text(72, 658, lines[3]), "ET"]
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert "Der Winter kam früh in diesem Jahr. " + " ".join(lines[1:]) in got, got
