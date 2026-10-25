"""Pages read from their own text, as the thirteenth review found round
twelve's changes reading them wrong: a word of code on a line of its own in
its sentence, fonts named like monospaced ones, a letter typed in Courier
under a letterhead, prose ending its lines on commas, listings in columns,
and trees drawn in box characters.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

import pytest

from conftest import _pdf_from_stream, _winansi_literal
from ocrust.markdown import convert
from ocrust.markdown._ir import monospaced_font
from test_markdown import body as note_body
from test_markdown import convert as convert_file
from test_markdown import docx
from test_markdown_layouts import HELVETICA, with_fonts

COURIER = "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"


@pytest.fixture(autouse=True)
def _no_models(monkeypatch, tmp_path):
    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))


def text(x: float, y: float, s: str) -> str:
    return f"1 0 0 1 {x} {y:.1f} Tm ({_winansi_literal(s)}) Tj"


def body(pdf: bytes) -> str:
    return convert(pdf, name="d.pdf", ocr=False).body


def paragraphs(md: str) -> list[str]:
    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", md)]


def test_only_monospaced_faces_are_taken_for_code_fonts():
    for name in ["Consolas", "Courier New", "Liberation Mono", "Source Code Pro", "PTMono"]:
        assert monospaced_font(name), name
    for name in ["Arial Unicode MS", "Lucida Sans Unicode", "Encode Sans", "American Typewriter"]:
        assert not monospaced_font(name), name


def test_a_command_on_a_line_of_its_own_stays_in_its_sentence():
    parts = [
        "BT",
        "/F1 11 Tf",
        text(72, 700, "To update every machine of the laboratory, simply run"),
    ]
    parts += ["/F2 10 Tf", text(72, 686, "docker compose --file deploy/prod.yml up --detach")]
    parts += ["/F1 11 Tf", text(72, 672, "after which the service picks up the new version.")]
    parts.append("ET")
    md = body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": COURIER}))
    assert "```" not in md, md
    assert "run `docker compose --file deploy/prod.yml up --detach` after which" in md, md


def test_listing_columns_are_kept():
    rows = [
        "NAME          READY   STATUS    RESTARTS",
        "web-1         1/1     Running   0",
        "db-1          0/1     Pending   3",
    ]
    parts = ["BT", "/F1 11 Tf", text(72, 740, "The pods:"), "/F2 9 Tf"]
    parts += [text(72, 720 - 11 * i, row) for i, row in enumerate(rows)]
    parts.append("ET")
    md = body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": COURIER}))
    assert "```\n" + "\n".join(rows) + "\n```" in md, md


def test_prose_ending_its_lines_on_commas_is_no_verse():
    lines = [
        "This is why the board, after a long discussion,",
        "decided to postpone the vote. The new library,",
        "the swimming pool and the sports hall will be",
        "built first.",
    ]
    parts = ["BT /F1 11 Tf", *(text(72, 700 - 14 * i, line) for i, line in enumerate(lines))]
    parts.append("ET")
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert " ".join(lines) in got, got


def word_run(value: str, font: str = "") -> str:
    props = f'<w:rPr><w:rFonts w:ascii="{font}" w:hAnsi="{font}"/></w:rPr>' if font else ""
    return f'<w:r>{props}<w:t xml:space="preserve">{escape(value)}</w:t></w:r>'


def word_para(*runs: str) -> str:
    return "<w:p>" + "".join(runs) + "</w:p>"


def test_a_letter_typed_in_courier_under_a_letterhead_is_text():
    letter = [
        "Herrn Thomas Schmidt",
        "Gartenweg 3, 79098 Freiburg",
        "Sehr geehrter Herr Schmidt,",
        "vielen Dank für die Unterlagen, die Sie uns geschickt haben.",
        "Mit freundlichen Grüßen",
    ]
    document = word_para(word_run("Müller & Partner Steuerberatung", "Arial"))
    document += "".join(word_para(word_run(line, "Courier New")) for line in letter)
    md = note_body(convert_file(docx(document), "brief.docx").markdown)
    assert "```" not in md and "`" not in md, md


def test_text_in_a_font_named_like_unicode_is_no_code():
    document = word_para(word_run("The committee met on Tuesday.", "Arial Unicode MS"))
    md = note_body(convert_file(docx(document), "report.docx").markdown)
    assert md.strip() == "The committee met on Tuesday.", md
