"""PDF pages read from their own text: the layouts real documents come in.

Each test is a case an independent review of `ocrust markdown` found read
wrong, set the way Word, a browser or LaTeX sets it, and failed before the fix
it names.
"""

from __future__ import annotations

import math

import pytest

from conftest import _pdf_from_stream, _pdf_from_streams, _winansi_literal
from ocrust import markdown

HELVETICA = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"


@pytest.fixture(autouse=True)
def _no_models(monkeypatch, tmp_path):
    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))


def read(pdf: bytes) -> str:
    return markdown.convert(pdf, name="d.pdf", ocr=False).body


def shown(x: float, y: float, text: str, size: float | None = None) -> str:
    font = f"/F1 {size} Tf " if size else ""
    return f"{font}1 0 0 1 {x} {y:.1f} Tm ({_winansi_literal(text)}) Tj"


def table(rows, xs, *, top=690.0, pitch=11.5, space=5.0, before="", after="") -> bytes:
    """A table drawn cell by cell, as a browser draws it: each cell a list of
    its lines, each row `space` below the last line of the one before."""
    parts = ["BT /F1 10 Tf"]
    if before:
        parts.append(shown(72, 720, before))
    y = top
    for row in rows:
        for x, cell in zip(xs, row, strict=True):
            parts += [shown(x, y - pitch * i, text) for i, text in enumerate(cell)]
        y -= pitch * max(len(cell) for cell in row) + space
    if after:
        parts.append(shown(72, y - 12, after))
    parts.append("ET")
    return _pdf_from_stream("\n".join(parts))


def with_fonts(stream: str, fonts: dict[str, str], extra: tuple[str, ...] = ()) -> bytes:
    """One page with fonts of its own; `{obj:N}` in a font refers to `extra[N]`."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"]
    names = list(fonts)
    font_ids = {name: 5 + i for i, name in enumerate(names)}
    extra_base = 5 + len(names)
    resources = " ".join(f"/{n} {i} 0 R" for n, i in font_ids.items())
    objects.append(
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        f"/Resources << /Font << {resources} >> >> /Contents 4 0 R >>"
    )
    objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    for name in names:
        font = fonts[name]
        for index in range(len(extra)):
            font = font.replace(f"{{obj:{index}}}", f"{extra_base + index} 0 R")
        objects.append(font)
    objects += list(extra)
    pdf, offsets = "%PDF-1.4\n", []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += f"{number} 0 obj\n{obj}\nendobj\n"
    xref = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    pdf += "".join(f"{offset:010} 00000 n \n" for offset in offsets)
    pdf += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return pdf.encode("latin-1")


def to_unicode(mapping: dict[int, int]) -> str:
    body = "\n".join(f"<{k:02X}> <{v:04X}>" for k, v in mapping.items())
    cmap = (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << "
        "/Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def /CMapName /Adobe-Identity-UCS "
        "def /CMapType 2 def 1 begincodespacerange <00> <FF> endcodespacerange "
        f"{len(mapping)} beginbfchar\n{body}\nendbfchar endcmap CMapName currentdict /CMap "
        "defineresource pop end end"
    )
    return f"<< /Length {len(cmap)} >>\nstream\n{cmap}\nendstream"


# -- tables


def test_a_row_with_only_its_first_cell_filled_stays_a_row():
    rows = [
        ("Leistung", "Menge", "Preis"),
        ("Beratung", "8 h", "960,00"),
        ("Hardware", "", ""),
        ("Server", "1", "2.400,00"),
        ("Software", "", ""),
        ("Lizenzen", "10", "1.200,00"),
    ]
    text = read(table([[[c] if c else [] for c in r] for r in rows], (75, 200, 300), space=5))
    assert "| Hardware |  |  |" in text and "| Beratung | 8 h | 960,00 |" in text


def test_a_category_row_in_a_padded_table_keeps_the_table():
    rows = [[["Leistung"], ["Menge"], ["Preis"]], [["Beratung"], ["8 h"], ["960,00"]]]
    rows += [
        [["Hardware"], [], []],
        [["Server"], ["1"], ["2.400,00"]],
        [["Switch"], ["2"], ["380,00"]],
    ]
    text = read(table(rows, (75, 200, 300), space=9.5, before="Unser Angebot:"))
    assert "| Server | 1 | 2.400,00 |" in text


def test_a_cell_whose_second_line_is_a_date_or_a_number_keeps_it():
    rows = [
        [["Pos."], ["Leistung"], ["Betrag"]],
        [["1"], ["Ersatzteile laut Angebot vom", "12.03.2026"], ["480,00 EUR"]],
        [["2"], ["Montage der Anlage vor Ort"], ["960,00 EUR"]],
        [["3"], ["Wartung laut Vertrag Nummer", "4711-2026"], ["1.200,00 EUR"]],
        [["4"], ["Anfahrt"], ["80,00 EUR"]],
    ]
    text = read(table(rows, (78, 108, 260)))
    assert "| 1 | Ersatzteile laut Angebot vom 12.03.2026 | 480,00 EUR |" in text
    assert "| 3 | Wartung laut Vertrag Nummer 4711-2026 | 1.200,00 EUR |" in text


def test_a_row_under_the_widest_cell_is_not_folded_into_it():
    rows = [
        [["Aufgabe"], ["Verantwortlich"], ["Status"]],
        [["Firewall-Regeln pruefen"], ["Schmidt"], ["erledigt"]],
        [["Serverraum klimatisieren lassen"], ["Mueller"], ["offen"]],
        [["Backup testen"], [], []],
        [["Drucker tauschen"], ["Weber"], ["offen"]],
    ]
    text = read(table(rows, (75, 250, 360), pitch=16, space=4.5))
    assert "| Backup testen |  |  |" in text


def test_a_hyphenated_compound_across_a_cell_is_one_word():
    rows = [
        [["Feld"], ["Wert"]],
        [["Ihre E-Mail-", "Adresse"], ["info@example.org"]],
        [["Telefon"], ["030 1234"]],
        [["Fax"], ["030 1235"]],
    ]
    assert "Ihre E-Mail-Adresse" in read(table(rows, (72, 160)))


# -- the page around the tables


def test_two_lines_side_by_side_stay_apart():
    parts = ["BT /F1 10 Tf"]
    y = 720
    right = {1: "Berlin, 3. Juni 2026", 2: "Tel. 030 1234"}
    for i, line in enumerate(
        ["Firma Beispiel GmbH", "Herrn Max Mustermann", "Musterstrasse 12", "12345 Berlin"]
    ):
        parts.append(shown(72, y, line))
        if i in right:
            parts.append(shown(420, y, right[i]))
        y -= 12
    parts += [shown(72, 650, "Sehr geehrter Herr Mustermann,"), "ET"]
    lines = read(_pdf_from_stream("\n".join(parts))).splitlines()
    assert not [line for line in lines if "Mustermann" in line and "Juni" in line]
    assert not [line for line in lines if "Musterstrasse" in line and "Tel." in line]


def test_a_stamp_up_the_margin_or_a_watermark_leaves_the_page_alone():
    body = ["BT /F1 16 Tf 1 0 0 1 72 720 Tm (Keeping Structure) Tj ET", "BT /F1 10 Tf"]
    y = 680
    for k in range(3):
        body.append(shown(72, y, f"Section {k + 1}"))
        y -= 16
        for _ in range(3):
            body.append(shown(72, y, "Text of the section, a whole line of it, set in ten points."))
            y -= 12
        y -= 10
    body.append("ET")
    turn = math.radians(45)
    for extra in (
        "BT /F1 20 Tf 0 1 -1 0 40 250 Tm (arXiv:2401.12345v1 [cs.CL] 22 Jan 2024) Tj ET",
        f"BT /F1 72 Tf {math.cos(turn):.4f} {math.sin(turn):.4f} {-math.sin(turn):.4f} "
        f"{math.cos(turn):.4f} 150 250 Tm (ENTWURF) Tj ET",
    ):
        lines = read(_pdf_from_stream("\n".join([*body, extra]))).splitlines()
        assert all(any(line.startswith(f"Section {k}") for line in lines) for k in (1, 2, 3))


@pytest.mark.parametrize(
    ("matrix", "step"),
    [("0 1 -1 0", (14, 0)), ("0 -1 1 0", (-14, 0)), ("-1 0 0 -1", (0, 14))],
)
def test_a_page_set_sideways_or_upside_down_is_read_the_right_way_up(matrix, step):
    rows = [("Artikel", "Menge", "Preis"), ("Schrauben M4", "100", "4,90"), ("Winkel", "4", "7,96")]
    rows += [("Muttern M4", "50", "2,10")]
    parts = ["BT /F1 10 Tf"]
    along = {"0 1 -1 0": (100, 280, 380), "0 -1 1 0": (700, 520, 420), "-1 0 0 -1": (500, 320, 220)}
    start = {"0 1 -1 0": (130, 0), "0 -1 1 0": (470, 0), "-1 0 0 -1": (0, 130)}[matrix]
    for n, row in enumerate(rows):
        for offset, text in zip(along[matrix], row, strict=True):
            if matrix == "-1 0 0 -1":
                x, y = offset, start[1] + step[1] * n
            else:
                x, y = start[0] + step[0] * n, offset
            parts.append(f"{matrix} {x} {y} Tm ({text}) Tj")
    parts.append("ET")
    assert "| Schrauben M4 | 100 | 4,90 |" in read(_pdf_from_stream("\n".join(parts)))


# -- headings and paragraphs


def test_chapters_numbered_alike_are_all_headings():
    text = ["Der Text dieses Abschnitts beschreibt die Ausgangslage und die Ziele"]
    text += ["des Projekts im Detail, nennt die Beteiligten und den Zeitplan dazu."]

    def page(title: str) -> str:
        parts = ["BT", shown(72, 730, title, 20), "/F1 11 Tf"]
        y = 690.0
        for _ in range(4):
            for line in text:
                parts.append(shown(72, y, line))
                y -= 13.2
            y -= 10
        return "\n".join([*parts, "ET"])

    body = read(_pdf_from_streams([page(f"Kapitel {n}") for n in (1, 2, 3)]))
    assert all(f"# Kapitel {n}" in body for n in (1, 2, 3))


def test_a_page_crowded_with_footnotes_keeps_its_body_text():
    parts, y = ["BT", "/F1 11 Tf"], 760.0
    for paragraph in (
        ["im Jahr 2019 erstmals beschrieben und seitdem mehrfach bestaetigt."],
        [
            "Die Untersuchung stuetzt sich auf drei Quellen, die im Folgenden kurz vorgestellt",
            "werden. Die erste ist das Archiv der Stadt, das die Akten der Jahre 1950 bis 1990",
            "vollstaendig enthaelt; die zweite sind Gespraeche mit Zeitzeugen, die zwischen",
            "2020 und 2023 gefuehrt wurden; die dritte sind die Berichte der Presse, soweit",
            "sie sich erhalten haben, die in den folgenden Abschnitten gewuerdigt werden.",
        ],
        ["Die Frage ist also nicht, ob, sondern wie."],
    ):
        for line in paragraph:
            parts.append(shown(72, y, line))
            y -= 12.65
        y -= 6
    parts.append("/F1 9 Tf")
    y -= 20
    for i in range(17):
        parts.append(
            shown(72, y, f"{i + 1} Vgl. Mustermann, Die Stadt und ihre Akten, 2019, S. {10 + i}.")
        )
        y -= 10.35
    text = read(_pdf_from_stream("\n".join([*parts, "ET"])))
    assert not [line for line in text.splitlines() if line.startswith("#")]


def test_a_title_over_a_table_alone_is_a_heading():
    rows = [[["Posten"], ["Betrag"]], [["Miete"], ["1.200,00"]], [["Strom"], ["80,00"]]]
    rows += [[["Wasser"], ["40,00"]]]
    parts = ["BT", shown(72, 740, "Kostenuebersicht", 18), "/F1 10 Tf"]
    y = 700.0
    for row in rows:
        parts += [shown(x, y, cell[0]) for x, cell in zip((72, 250), row, strict=True)]
        y -= 16
    assert "# Kostenuebersicht" in read(_pdf_from_stream("\n".join([*parts, "ET"])))


def test_a_line_of_inline_code_does_not_split_its_paragraph():
    def line(y: float, runs: list[tuple[str, float]]) -> list[str]:
        out, x = [], 72.0
        for text, size in runs:
            out.append(shown(round(x, 1), y, text, size))
            x += len(text) * size * 0.52 + size * 0.28
        return out

    body, code = 11, 9.35
    parts = ["BT"]
    parts += line(
        720, [("Das Werkzeug liest seine Einstellungen beim Start aus einer Datei im", body)]
    )
    parts += line(
        707.35,
        [("Benutzerverzeichnis. Die Befehle heissen", body), ("add", code), ("commit", code)],
    )
    parts += line(
        694.7, [("rebase", code), ("fetch", code), ("status", code), ("und stehen bereit.", body)]
    )
    parts += line(
        670, [("Ein zweiter Absatz folgt hier, damit die Seite genug gewoehnlichen Text", body)]
    )
    parts += line(657.35, [("hat und die Zeilenabstaende gemessen werden koennen.", body)])
    text = read(_pdf_from_stream("\n".join([*parts, "ET"])))
    assert len([p for p in text.split("\n\n") if p.strip() and not p.startswith("<!--")]) == 2


# -- symbol fonts


def test_wingdings_bullets_are_a_list_and_symbol_letters_stay_letters():
    wingdings = "<< /Type /Font /Subtype /Type1 /BaseFont /Wingdings-Regular /ToUnicode {obj:0} >>"
    items = ["Umsatz +12%", "Kosten -3%", "Neue Kunden"]
    stream = ["BT /F1 24 Tf 1 0 0 1 72 700 Tm (Quartalsbericht Q3) Tj ET"]
    for n, item in enumerate(items):
        y = 640 - 30 * n
        stream.append(f"BT /F2 14 Tf 1 0 0 1 90 {y} Tm (\\166) Tj ET")
        stream.append(f"BT /F1 14 Tf 1 0 0 1 110 {y} Tm ({item}) Tj ET")
    pdf = with_fonts(
        "\n".join(stream), {"F1": HELVETICA, "F2": wingdings}, (to_unicode({0x76: 0xF076}),)
    )
    text = read(pdf)
    assert all(f"- {item}" in text for item in items)

    symbol = "<< /Type /Font /Subtype /Type1 /BaseFont /Symbol /ToUnicode {obj:0} >>"
    stream = (
        "BT /F1 12 Tf 1 0 0 1 72 700 Tm (Der Winkel ) Tj /F2 12 Tf (q) Tj "
        "/F1 12 Tf ( betraegt 30 Grad.) Tj ET"
    )
    pdf = with_fonts(stream, {"F1": HELVETICA, "F2": symbol}, (to_unicode({0x71: 0xF071}),))
    assert "•" not in read(pdf)
