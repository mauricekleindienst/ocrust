"""Pages read from their own text, as the eleventh review found round ten's
changes reading them wrong: poems and lines broken on purpose, a slide
title set in bold, typed lists and notes, YAML and listings opening with a
comment, a reference list set with a hanging indent — and the text beside
a picture, the key figures a column ends on and a letter's copy list that
those fixes had to keep.
"""

from __future__ import annotations

import re

import pytest

from conftest import _pdf_from_stream, _winansi_literal
from ocrust.markdown import convert
from test_markdown import body as note_body
from test_markdown import convert as convert_file
from test_markdown import pptx
from test_markdown_layouts import HELVETICA, with_fonts

COURIER = "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"

RUNNING = [
    "Die Stadtwerke haben im Sommer das neue Leitungsnetz im Norden der Stadt in Betrieb",
    "genommen und damit viele Haushalte neu an die Fernwaerme angeschlossen, auch im Winter.",
]


@pytest.fixture(autouse=True)
def _no_models(monkeypatch, tmp_path):
    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))


def text(x: float, y: float, s: str) -> str:
    return f"1 0 0 1 {x} {y:.1f} Tm ({_winansi_literal(s)}) Tj"


def body(pdf: bytes) -> str:
    return convert(pdf, name="d.pdf", ocr=False).body


def paragraphs(md: str) -> list[str]:
    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", md)]


def lines_of(md: str) -> list[str]:
    return [line.strip() for line in md.splitlines()]


def lines_at(x: float, top: float, lines: list[str], step: float = 13) -> list[str]:
    return [text(x, top - step * i, line) for i, line in enumerate(lines)]


def under_running(block: list[str], x: float = 72) -> bytes:
    """`block` under a paragraph of running text that shows the column's
    width."""
    parts = ["BT /F1 10 Tf", *lines_at(72, 720, RUNNING), *lines_at(x, 680, block), "ET"]
    return _pdf_from_stream("\n".join(parts))


def test_lines_broken_on_purpose_in_lower_case_keep_a_line_each():
    for block in (
        ["so much depends", "upon the red wheel barrow", "glazed with rain water"],
        ["Things to bring on the hike:", "sunscreen and a good hat", "water for the whole day"],
    ):
        got = lines_of(body(under_running(block)))
        assert all(line in got or line + "  " in got for line in block), got


def test_verse_narrower_than_its_column_keeps_a_line_each():
    frost = [
        "Two roads diverged in a yellow wood,",
        "And sorry I could not travel both",
        "And be one traveler, long I stood",
        "And looked down one as far as I could",
    ]
    got = [line.rstrip() for line in lines_of(body(under_running(frost, x=90)))]
    assert all(line in got for line in frost), got


def courier(listing: list[str], before: str = "So sieht es aus:") -> str:
    parts = ["BT", "/F1 11 Tf", text(72, 740, before), "/F2 9 Tf"]
    y = 720.0
    for line in listing:
        if line:
            indent = len(line) - len(line.lstrip())
            parts.append(text(72 + 5.4 * indent, y, line.strip()))
        y -= 11
    parts.append("ET")
    return body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": COURIER}))


def test_typed_references_notes_and_copy_lists_are_no_code():
    for block in (
        ["Your ref: JC/ab", "Our ref: CL/4711/22", "Date: 05/03/2025"],
        ["cc: Dr. Miller, legal/contracts", "cc: accounts/billing", "encl: 2 copies"],
        ["the light of morning", "falls on the kitchen table", "and the cat is asleep"],
        ["meeting moved to thursday", "bring the budget sheets", "ask tom about the van"],
    ):
        md = courier(block)
        assert "```" not in md, md


def test_yaml_with_worded_values_is_code_with_its_indents():
    yaml = [
        "name: Build and test the project",
        "on: [push, pull_request]",
        "jobs:",
        "  test:",
        "    name: Run the unit tests on every push",
        "    runs-on: ubuntu-latest",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "      - name: Install the dependencies",
        "        run: npm ci",
    ]
    md = courier(yaml)
    assert "```\n" + "\n".join(yaml) + "\n```" in md, md


def test_a_listing_opening_with_a_comment_is_one_code_block():
    listing = [
        "#!/bin/sh",
        "# Run it from cron as the backup user.",
        "",
        "import os",
        "import sys",
        "",
        "def main():",
        "    print(os.getcwd())",
    ]
    md = courier(listing)
    assert "```\n" + "\n".join(listing) + "\n```" in md, md


def test_a_reference_list_set_with_a_hanging_indent_keeps_its_entries_apart():
    entries = [
        [
            "Brown, A. (2010). Table structure recognition in scanned documents and",
            "forms. arXiv preprint arXiv:2101.01234, pages 1 to 12 of the report.",
        ],
        [
            "Brown, A. (2013). A survey of optical character recognition in historical",
            "newspapers. Journal of Document Analysis 12, pages 101 to 131, Berlin.",
        ],
        [
            "Rossi, M. and Bianchi, F. (2001). Reading order in two columns of text on",
            "the page. In Proceedings of the Workshop on Documents, pages 5 to 9.",
        ],
    ]
    parts, y = ["BT /F1 10 Tf"], 700.0
    for first, *rest in entries:
        parts.append(text(72, y, first))
        for line in rest:
            y -= 12
            parts.append(text(90, y, line))
        y -= 12
    parts.append("ET")
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert all(" ".join(entry) in got for entry in entries), got


def test_a_bold_or_italic_slide_title_is_a_plain_heading():
    def title(runs: str) -> str:
        return (
            '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Titel"/><p:cNvSpPr/><p:nvPr>'
            '<p:ph type="title"/></p:nvPr></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/>'
            f"<a:p>{runs}</a:p></p:txBody></p:sp>"
        )

    bold = title('<a:r><a:rPr b="1"/><a:t>Quarterly results</a:t></a:r>')
    part = title('<a:r><a:t>Results for </a:t></a:r><a:r><a:rPr i="1"/><a:t>Q3 2025</a:t></a:r>')
    for shape, want in ((bold, "## Quarterly results"), (part, "## Results for Q3 2025")):
        md = note_body(convert_file(pptx([shape]), "folie.pptx").markdown)
        assert want in md.split("\n\n"), md


def test_list_items_beside_a_picture_run_on_where_their_lines_end():
    # German nouns open lines with capitals: no verse for that.
    items = [
        [
            "- Die Kundenzufriedenheit ist im Vergleich zum",
            "Vorjahr deutlich gestiegen, vor allem beim",
            "Service am Telefon und im Kundencenter.",
        ],
        [
            "- Die Stadtwerke haben im Sommer das neue",
            "Leitungsnetz im Norden der Stadt in Betrieb",
            "genommen und damit die Versorgung gesichert.",
        ],
    ]
    under = [
        "Im Herbst beginnt die Sanierung des Verwaltungsgebaeudes am Markt, und waehrend der",
        "Bauarbeiten ziehen die Abteilungen Einkauf und Personal in das Gebaeude am Hafen um.",
    ]
    parts, y = ["BT /F1 10 Tf"], 700.0
    for first, *rest in items:
        parts.append(text(72, y, first))
        for line in rest:
            y -= 13
            parts.append(text(82, y, line))
        y -= 17
    parts += [*lines_at(72, y - 20, under), "ET"]
    md = body(_pdf_from_stream("\n".join(parts)))
    assert "  \n" not in md, md


def test_a_paragraph_beside_a_picture_runs_on_under_it():
    beside = [
        "Die Stadtwerke haben im Sommer das neue",
        "Leitungsnetz im Norden der Stadt in Betrieb",
        "genommen und damit die Versorgung gesichert.",
        "Ueber dreitausend Haushalte sind jetzt an",
    ]
    under = ["die Fernwaerme angeschlossen, und im naechsten Jahr folgen noch einmal tausend."]
    parts = ["BT /F1 10 Tf", *lines_at(72, 700, beside), *lines_at(72, 648, under), "ET"]
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert " ".join(beside + under) in got, got


def test_a_paragraph_goes_on_in_the_next_column_into_its_key_figures():
    left = [
        "Die Stadtwerke haben im Sommer das neue",
        "Leitungsnetz im Norden der Stadt in Betrieb",
        "genommen, und die Versorgung ist gesichert.",
        "Die wichtigsten Kennzahlen im Ueberblick:",
        "Umsatz 4,2 Mio. EUR (Vorjahr 3,7 Mio. EUR);",
        "EBIT 0,48 Mio. EUR (0,31 Mio. EUR); Cashflow",
    ]
    right = [
        "0,9 Mio. EUR; Investitionen 1,2 Mio. EUR;",
        "Mitarbeitende 212 (198); Netz 412 km (398 km).",
    ]
    parts = ["BT /F1 10 Tf", *lines_at(72, 700, left), *lines_at(320, 700, right), "ET"]
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert " ".join(left + right) in got, got
