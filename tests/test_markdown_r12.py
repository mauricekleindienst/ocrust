"""Pages read from their own text, as the twelfth review found them read
wrong: short paragraphs indented the book's way, a poem's lines, typed
prose naming links, commands of any tool, one-line code blocks, and code
set in a monospaced font in Word and LibreOffice documents.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

import pytest

from conftest import _pdf_from_stream, _winansi_literal
from ocrust.markdown import convert
from test_markdown import ODF, docx, package
from test_markdown import body as note_body
from test_markdown import convert as convert_file
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


def lines_of(md: str) -> list[str]:
    return [line.strip() for line in md.splitlines()]


def test_short_paragraphs_indented_the_books_way_are_never_torn():
    paras = [
        ["Tom sat down at the table and took off his wet shoes, one after the", "other."],
        ["Martha shrugged and went into the kitchen to make some tea for both", "of them."],
        ["Outside the rain had stopped, and the first birds began to sing in the", "garden."],
    ]
    parts, y = ["BT /F1 11 Tf"], 700.0
    for n, (first, last) in enumerate(paras):
        parts.append(text(72 if n == 0 else 90, y, first))
        parts.append(text(72, y - 14, last))
        y -= 28
    parts.append("ET")
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    for first, last in paras:
        assert any(f"{first} {last}" in p for p in got), got


def test_a_poems_lines_keep_a_line_each_alone_on_the_page():
    poem = [
        "The woods are quiet in the snow",
        "and no one walks the path tonight,",
        "the river sleeps beneath the ice,",
        "the stars are cold and very bright.",
    ]
    parts = ["BT /F1 12 Tf", text(72, 740, "A poem by Anna Schmidt, written in 2019.")]
    parts += [text(72, 700 - 15 * i, line) for i, line in enumerate(poem)]
    parts.append("ET")
    got = [line.rstrip() for line in lines_of(body(_pdf_from_stream("\n".join(parts))))]
    assert all(line in got for line in poem), got


def test_names_opening_a_line_beside_commas_are_no_verse():
    lines = [
        "Our new colleague Anna Schmidt joined the Berlin office in",
        "May, after six years with Siemens in Munich, Vienna and",
        "Zurich. She will lead the team for Customer Support,",
        "together with Peter Braun, Laura Klein and Jonas Weber,",
        "and report to Martin Vogel, who leads the whole department.",
    ]
    parts = ["BT /F1 10 Tf", *(text(72, 700 - 13 * i, line) for i, line in enumerate(lines))]
    parts.append("ET")
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert " ".join(lines) in got, got


def courier(listing: list[str], before: str = "So geht es:", after: str = "") -> str:
    parts = ["BT", "/F1 11 Tf", text(72, 740, before), "/F2 9 Tf"]
    y = 720.0
    for line in listing:
        if line:
            indent = len(line) - len(line.lstrip())
            parts.append(text(72 + 5.4 * indent, y, line.strip()))
        y -= 11
    if after:
        parts += ["/F1 11 Tf", text(72, y - 4, after)]
    parts.append("ET")
    return body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": COURIER}))


def test_typed_prose_naming_links_and_paths_is_no_code():
    for prose in (
        [
            "The new version of the report is available at https://example.org/a.",
            "Please read it before the meeting and send your comments to the",
            "office. The old version at https://example.org/reports/old is gone.",
        ],
        [
            "I have put the photos in /home/shared/photos/summer and the videos in",
            "/home/shared/videos. If you cannot open them, please let me know and I",
            "will send them to you on a stick next week.",
        ],
    ):
        md = courier(prose)
        assert "```" not in md, md


def test_commands_of_any_tool_and_one_line_commands_are_code():
    for listing in (
        ["gcloud auth login", "gcloud config set project my-project"],
        ["gem install rails", "rails new blog", "cd blog", "rails server"],
        ["pip install requests"],
        ["$ ls", "README.md  src  tests", "$ git status", "On branch main"],
        ["SELECT name, email", "FROM customers", "WHERE country = 'DE'", "ORDER BY name;"],
    ):
        md = courier(listing, after="Danach ist alles bereit.")
        fenced = re.findall(r"```\n(.*?)\n```", md, re.S)
        assert [" ".join(f.split()) for f in fenced] == [" ".join(" ".join(listing).split())], md


def test_a_lone_note_typed_in_courier_is_no_configuration():
    md = courier(["note: please keep this letter"], before="Mit freundlichen Gruessen")
    assert "```" not in md, md


def word_run(value: str, font: str = "") -> str:
    props = f'<w:rPr><w:rFonts w:ascii="{font}" w:hAnsi="{font}"/></w:rPr>' if font else ""
    return f'<w:r>{props}<w:t xml:space="preserve">{escape(value)}</w:t></w:r>'


def word_para(*runs: str) -> str:
    return "<w:p>" + "".join(runs) + "</w:p>"


def test_code_set_in_consolas_in_a_word_document_is_code():
    document = "".join(
        [
            word_para(word_run("Then write the entry point:")),
            word_para(word_run("def main():", "Consolas")),
            word_para(word_run('    print("hello")', "Consolas")),
            word_para(
                word_run("Call the function "),
                word_run("main", "Consolas"),
                word_run(" from the command line."),
            ),
        ]
    )
    md = note_body(convert_file(docx(document), "howto.docx").markdown)
    assert md.split("\n\n") == [
        "Then write the entry point:",
        '```\ndef main():\n    print("hello")\n```',
        "Call the function `main` from the command line.",
    ]


def test_a_letter_typed_in_courier_new_in_word_is_text():
    lines = ["Hans Meier", "Sehr geehrte Damen und Herren,", "anbei der Zaehlerstand."]
    document = "".join(word_para(word_run(line, "Courier New")) for line in lines)
    md = note_body(convert_file(docx(document), "brief.docx").markdown)
    assert md.split("\n\n") == lines


def test_code_set_in_a_monospaced_font_in_an_opendocument_is_code():
    content = (
        f"<office:document-content {ODF}><office:automatic-styles>"
        '<style:style style:name="T1" style:family="text">'
        '<style:text-properties style:font-name="Liberation Mono"/></style:style>'
        "</office:automatic-styles><office:body><office:text>"
        "<text:p>Install it:</text:p>"
        '<text:p><text:span text:style-name="T1">pip install ocrust</text:span></text:p>'
        '<text:p>Then call <text:span text:style-name="T1">ocrust</text:span> once.</text:p>'
        "</office:text></office:body></office:document-content>"
    )
    data = package({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": content})
    assert note_body(convert_file(data, "p.odt").markdown).split("\n\n") == [
        "Install it:",
        "```\npip install ocrust\n```",
        "Then call `ocrust` once.",
    ]


def test_a_dash_opening_a_line_of_running_text_goes_on_its_sentence():
    lines = [
        "Unsere Partner in Spanien und Portugal haben uns sehr geholfen",
        "– und später auch in Italien – und das Projekt gut unterstützt.",
    ]
    parts = ["BT /F1 11 Tf", *(text(72, 700 - 14 * i, line) for i, line in enumerate(lines))]
    parts.append("ET")
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert " ".join(lines) in got, got


def test_a_spaced_dash_ending_a_line_is_kept():
    lines = [
        "Our partners in Spain and Portugal helped us a lot, and Italy -",
        "as the report shows - supported the project from the start.",
    ]
    parts = ["BT /F1 11 Tf", *(text(72, 700 - 14 * i, line) for i, line in enumerate(lines))]
    parts.append("ET")
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert " ".join(lines) in got, got


def test_reference_entries_of_one_line_beside_wrapped_ones_stay_apart():
    entries = [
        ["Nguyen, T. (2017). Fonts. Technical report, Example Labs."],
        [
            "Garcia, L., Chen, W., and Okafor, N. (2020). A survey of table detection in",
            "scanned documents. ACM Computing Surveys, 53(2):1–35.",
        ],
        ["Ivanova, P. (2018). Reading order. PhD thesis, University of Leeds."],
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


def test_a_sentence_set_larger_than_a_page_of_notes_is_no_heading():
    sentence = "The following table lists the results of the last quarter for all regions."
    parts = ["BT /F1 18 Tf", text(72, 770, "Results"), "/F1 12 Tf", text(72, 740, sentence)]
    parts.append("/F1 9 Tf")
    parts += [
        text(72, 715 - 12 * k, f"{k + 1}. Note number {k + 1} on the figures above.")
        for k in range(8)
    ]
    parts.append("ET")
    md = body(_pdf_from_stream("\n".join(parts)))
    assert f"\n{sentence}\n" in f"\n{md}\n" and f"# {sentence}" not in md, md


def test_a_slides_title_stating_its_point_stays_a_heading():
    # Set but a little larger than its bullets, it is still the page's largest.
    title = "Revenue grew by twelve per cent in the third quarter."
    parts = ["BT /F1 18 Tf", text(36, 740, title), "/F1 14 Tf"]
    parts += [text(54, 700 - 22 * k, f"- Point {k + 1} of the quarter") for k in range(3)]
    parts.append("ET")
    md = body(_pdf_from_stream("\n".join(parts)))
    assert f"# {title}" in md, md
