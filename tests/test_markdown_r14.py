"""Pages read from their own text, as the fourteenth review found round
thirteen's changes reading them wrong: compounds with figures broken at a
line's end, words with a slash ending a line, links broken after a slash,
lead paragraphs and prose around a listing taken for headings, code
displayed in a sentence, a frame's top line, and a sheet's totals row.
"""

from __future__ import annotations

import re

import pytest

from conftest import _pdf_from_stream, _winansi_literal
from ocrust._types import Box, Line
from ocrust.markdown import convert
from ocrust.markdown._scan import _by_font
from test_markdown import body as note_body
from test_markdown import convert as convert_file
from test_markdown import xlsx
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


def prose(*lines: str, size: int = 11) -> str:
    parts = [f"BT /F1 {size} Tf"]
    parts += [text(72, 700 - (size + 3) * i, line) for i, line in enumerate(lines)]
    parts.append("ET")
    return body(_pdf_from_stream("\n".join(parts)))


def test_a_compound_with_figures_broken_at_the_line_end_is_one_word():
    md = prose(
        "Der Bericht beschreibt die Folgen der COVID-19-Pan-",
        "demie für die ganze Region. Ziel ist es, die CO2-Emis-",
        "sionen bis zum Jahr 2030 deutlich zu senken.",
    )
    assert "COVID-19-Pandemie" in md and "CO2-Emissionen" in md, md


def test_words_with_a_slash_ending_a_line_keep_their_space():
    md = prose(
        "The gateway of the new plant speaks either the TCP/IP",
        "and/or UDP, depending on the network of the site in 2023/24",
        "and/or later, as the operator decides.",
    )
    assert "TCP/IP and/or UDP" in md and "2023/24 and/or later" in md, md


def test_a_link_broken_after_a_slash_is_whole():
    md = prose(
        "Smith, J. and Lee, K. (2020). Recycling of batteries. https://doi.org/10.1016/",
        "j.jclepro.2020.124356. See also https://www.example.com/news/2024/05/",
        "new-library-opens-its-doors for the report.",
    )
    md = md.replace("\\", "")
    assert "https://doi.org/10.1016/j.jclepro.2020.124356" in md, md
    assert "https://www.example.com/news/2024/05/new-library-opens-its-doors" in md, md


def test_a_lead_paragraph_set_large_under_the_title_is_no_heading():
    lead = [
        "After three years of building work the new public",
        "library opened its doors on Saturday, and hundreds",
        "of visitors came to see it.",
    ]
    story = [
        "The new building replaces the old library on Market Street, which had",
        "become too small for the growing collection. It offers reading rooms,",
        "a children's corner and a café on the ground floor, and it stays open",
        "until eight in the evening on every working day of the week.",
    ]
    parts = ["BT /F1 24 Tf", text(72, 760, "Town opens new library"), "/F1 15.5 Tf"]
    parts += [text(72, 720 - 19 * i, line) for i, line in enumerate(lead)]
    parts.append("/F1 10.5 Tf")
    parts += [text(72, 650 - 13 * i, line) for i, line in enumerate(story)]
    parts.append("ET")
    md = body(_pdf_from_stream("\n".join(parts)))
    assert "# Town opens new library" in md, md
    assert " ".join(lead) in paragraphs(md), md


def mixed(before: list[str], code: list[str], after: list[str], *, size: int, x: float) -> str:
    parts, y = ["BT", "/F1 11 Tf"], 740.0
    for line in before:
        parts.append(text(72, y, line))
        y -= 14
    parts.append(f"/F2 {size} Tf")
    y -= 4
    for line in code:
        parts.append(text(x, y, line))
        y -= size + 2
    parts.append("/F1 11 Tf")
    y -= 4
    for line in after:
        parts.append(text(72, y, line))
        y -= 14
    parts.append("ET")
    return body(with_fonts("\n".join(parts), {"F1": HELVETICA, "F2": COURIER}))


def test_code_displayed_indented_in_its_sentence_is_a_code_block():
    code = [
        'frame = pandas.read_csv("measurements.csv")',
        'summary = frame.groupby("station").mean()',
    ]
    md = mixed(
        ["The data are read and summarised with the two calls"],
        code,
        ["where the second call averages all the values of each station."],
        size=10,
        x=100,
    )
    assert "```\n" + "\n".join(code) + "\n```" in md, md


def test_prose_around_a_smaller_listing_is_no_heading():
    listing = [
        "$ ls -l",
        "total 12",
        "-rw-r--r-- 1 anna staff  1204 May  3 10:12 notes.txt",
        "-rw-r--r-- 1 anna staff 10240 May  3 10:15 report.pdf",
        "drwxr-xr-x 4 anna staff   128 May  2 09:01 src",
    ]
    md = mixed(
        ["The following listing shows the current state of the system on the test machine."],
        listing,
        ["The next section explains each part in turn."],
        size=9,
        x=72,
    )
    assert "#" not in md.replace("```", ""), md
    assert "```\n$ ls -l\ntotal 12\n" in md, md


def test_a_frames_top_line_opens_its_code_block():
    rows = ["┌──────────┬───────┐", "│ Region   │ Sales │", "└──────────┴───────┘"]
    # The frame's glyphs come from a fallback font: only the row is told
    # monospaced.
    lines = [
        Line(row, Box(72, 700 + 12 * i, 200, 710 + 12 * i), 1.0, 0.0, monospaced=i == 1)
        for i, row in enumerate(rows)
    ]
    assert [[line.text for line in run] for run in _by_font(lines)] == [rows]


def test_a_totals_row_set_off_by_an_empty_row_keeps_its_columns():
    def row(r: int, *values: str | float) -> str:
        cells = []
        for column, value in zip("ABCD", values):
            if value == "":
                continue
            if isinstance(value, float):
                cells.append(f'<c r="{column}{r}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{column}{r}" t="inlineStr"><is><t>{value}</t></is></c>')
        return f'<row r="{r}">{"".join(cells)}</row>'

    sheet = (
        row(1, "Item", "Date", "Amount", "Share")
        + row(2, "Rent", "2025-01-15", 1200.5, "55%")
        + row(3, "Travel", "2025-02-03", 980.75, "45%")
        + row(5, "Total", "", 2181.25, "100%")
    )
    md = note_body(convert_file(xlsx({"Budget": sheet}, []), "budget.xlsx").markdown)
    assert "| Travel | 2025-02-03 | 980.75 | 45% |\n| Total |  | 2181.25 | 100% |" in md, md


def test_a_link_broken_after_a_path_of_two_slashes_is_whole():
    md = prose(
        "The measurements are published at https://zenodo.org/records/3456789/files/",
        "measurements_2019_final.csv together with the scripts of the project.",
    )
    assert "https://zenodo.org/records/3456789/files/measurements_2019_final.csv" in md.replace(
        "\\", ""
    ), md


def test_a_links_own_hyphen_at_a_line_end_is_kept_where_the_path_goes_on():
    md = prose(
        "The code is at https://github.com/example-org/document-layout-",
        "toolkit/blob/main/README.md for everyone to read and to use.",
    )
    assert "example-org/document-layout-toolkit/blob/main/README.md" in md.replace("\\", ""), md


def test_a_dialogues_lines_are_a_paragraph_each():
    said = [
        ["“Where have you been all night?” asked Martha, without looking up from"],
        ["“At the station,” said Tom."],
        ["“The last train was late again, and nobody knew why.”"],
        ["She closed the book and looked at him for a long time before she", "answered."],
        ["“Do you want milk?” she called through the open door."],
    ]
    said[0].append("her book.")
    parts, y = ["BT /F1 11 Tf"], 700.0
    for n, lines in enumerate(said):
        for k, line in enumerate(lines):
            parts.append(text(72 if n == 0 or k else 94, y, line))
            y -= 14
    parts.append("ET")
    got = paragraphs(body(_pdf_from_stream("\n".join(parts))))
    assert got[1:] == [" ".join(lines) for lines in said], got


def test_lines_each_ending_a_sentence_are_a_paragraph_each():
    lines = [
        "She closed the book and looked at him for a long time before she answered.",
        "“You could have called. I waited for you until midnight and then I went to bed.”",
        "“Where have you been all night?” asked Martha, without looking up from her book.",
        "“Yes, please, and two spoons of sugar if there is any left in the jar.”",
    ]
    assert paragraphs(prose(*lines, size=10))[1:] == lines


def test_items_opening_on_a_hyphen_are_a_list():
    items = ["the quick start guide", "a power supply", "two new export formats"]
    md = prose("The package contains", *(f"- {item}" for item in items))
    assert "\n".join(f"- {item}" for item in items) in md, md
