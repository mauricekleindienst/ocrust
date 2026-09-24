"""Documents of every kind as Markdown notes, and the folder that keeps them.

The inputs are built here, byte by byte — a Word file is a zip of a few XML
parts — so the suite needs neither Office nor any package that writes Office
files, and every case says exactly which piece of a format it is about.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest

from ocrust import markdown
from ocrust.cli import main
from ocrust.markdown import _ir, _render

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def body(note: str) -> str:
    """A note without its front matter."""
    if note.startswith("---\n"):
        return note.split("\n---\n", 1)[1].strip("\n")
    return note.strip("\n")


def convert(data: bytes, name: str, **settings: object) -> markdown.Converted:
    return markdown.convert(data, name=name, **settings)


def package(parts: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def rels(*entries: tuple[str, str, str], external: tuple[str, ...] = ()) -> str:
    items = "".join(
        f'<Relationship Id="{rid}" Type="{REL}/{kind}" Target="{target}"'
        + (' TargetMode="External"' if rid in external else "")
        + "/>"
        for rid, kind, target in entries
    )
    return (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{items}</Relationships>"
    )


# --------------------------------------------------------------------------
# The writer


def test_text_that_looks_like_markup_is_escaped_and_nothing_else():
    cases = {
        "5 * 3 = 15": r"5 \* 3 = 15",
        "snake_case and j_doe@example.org": "snake_case and j_doe@example.org",
        "_emphasis_": r"\_emphasis\_",
        "a `tick`": r"a \`tick\`",
        "Klammern <b> und a < b": r"Klammern \<b> und a < b",
        "[text](http://x)": r"[text\](http://x)",
        "[1] und [[Wiki]]": r"[1] und \[[Wiki]]",
        "C# und Nr. #12 und #tag": r"C# und Nr. #12 und \#tag",
        "&amp; und R&D": r"\&amp; und R&D",
        "==markiert==": r"\=\=markiert\=\=",
        "C:\\Users\\x": "C:\\Users\\x",
        "~ungefähr": r"\~ungefähr",
    }
    for text, expected in cases.items():
        assert _render.escape(text) == expected, text


def test_a_line_that_would_start_a_block_is_escaped():
    note = _ir.Note(
        blocks=[
            _ir.Paragraph(["# kein Titel"]),
            _ir.Paragraph(["1. Mai ist Feiertag"]),
            _ir.Paragraph(["- kein Punkt"]),
            _ir.Paragraph(["> kein Zitat"]),
            _ir.Paragraph(["---"]),
            _ir.Paragraph(["erste", _ir.Break(), "+ zweite"]),
        ]
    )
    assert body(_render.render(note, {})).split("\n\n") == [
        r"\# kein Titel",
        r"1\. Mai ist Feiertag",
        r"\- kein Punkt",
        r"\> kein Zitat",
        r"\---",
        "erste  \n\\+ zweite",
    ]


def test_emphasis_never_opens_next_to_white_space():
    content = _ir.assemble(
        [
            ("Der ", frozenset(), ""),
            ("Vertrag ", frozenset({"strong"}), ""),
            ("gilt", frozenset(), ""),
            (" ab ", frozenset({"strong"}), ""),
            ("heute", frozenset({"strong"}), ""),
        ]
    )
    assert _render.inline(content) == "Der **Vertrag** gilt **ab heute**"


def test_a_table_escapes_pipes_and_aligns_figures_right():
    table = _ir.Table(
        [
            [["Position"], ["Preis"]],
            [["A | B"], ["1.299,90 €"]],
            [["Montage"], ["85 %"]],
        ]
    )
    assert _render.blocks([table]) == (
        "| Position | Preis |\n| --- | ---: |\n| A \\| B | 1.299,90 € |\n| Montage | 85 % |"
    )


def test_a_code_fence_is_longer_than_any_fence_inside():
    rendered = _render.blocks([_ir.Code("```\nx\n```", "md")])
    assert rendered.startswith("````md\n") and rendered.endswith("\n````")


def test_front_matter_is_flat_yaml_a_title_cannot_break():
    head = _render.front_matter(
        {
            "title": 'Angebot: "Nord" – Teil 2\nneu',
            "pages": 3,
            "ocr_quality": 0.97,
            "truncated": True,
            "keywords": ["a", "b: c"],
            "created": dt.datetime(2026, 3, 1, 9, 30, tzinfo=dt.timezone.utc),
            "date": dt.date(2026, 3, 2),
            "empty": "",
            "none": None,
        }
    )
    assert head.split("\n") == [
        "---",
        'title: "Angebot: \\"Nord\\" – Teil 2 neu"',
        "pages: 3",
        "ocr_quality: 0.97",
        "truncated: true",
        'keywords: ["a", "b: c"]',
        "created: 2026-03-01T09:30:00Z",
        "date: 2026-03-02",
        "---",
    ]


def test_a_note_is_nfc_with_lf_line_ends():
    decomposed = "Gru" + chr(0x308) + "ße"
    note = convert(f"{decomposed}\r\nzweite Zeile\r\n".encode(), "a.txt")
    assert "Grüße" in note.markdown
    assert "\r" not in note.markdown
    assert note.markdown.endswith("\n") and not note.markdown.endswith("\n\n")


# --------------------------------------------------------------------------
# Word


def docx(
    document: str,
    *,
    styles: str = "",
    numbering: str = "",
    extra: dict | None = None,
    relations: tuple = (),
    external: tuple = (),
) -> bytes:
    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": "<Types/>",
        "_rels/.rels": rels(("rId1", "officeDocument", "word/document.xml")),
        "word/document.xml": f"<w:document {W} {R}><w:body>{document}</w:body></w:document>",
        "word/_rels/document.xml.rels": rels(*relations, external=external),
    }
    if styles:
        parts["word/styles.xml"] = f"<w:styles {W}>{styles}</w:styles>"
    if numbering:
        parts["word/numbering.xml"] = f"<w:numbering {W}>{numbering}</w:numbering>"
    parts.update(extra or {})
    return package(parts)


def para(text: str, style: str = "", props: str = "") -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/>{props}</w:pPr>' if style or props else ""
    if not style and props:
        ppr = f"<w:pPr>{props}</w:pPr>"
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


HEADINGS = "".join(
    f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/></w:style>'
    for sid, name in (
        ("Title", "Title"),
        ("H1", "heading 1"),
        ("H2", "heading 2"),
        ("Quote", "Quote"),
    )
)


def test_word_headings_lists_and_formatting():
    numbering = (
        '<w:abstractNum w:abstractNumId="1">'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
        '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/></w:lvl>'
        "</w:abstractNum>"
        '<w:num w:numId="7"><w:abstractNumId w:val="1"/></w:num>'
    )

    def item(text: str, level: int) -> str:
        props = f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="7"/></w:numPr>'
        return para(text, props=props)

    document = (
        para("Vertrag", "Title")
        + para("Umfang", "H1")
        + '<w:p><w:r><w:t xml:space="preserve">Die </w:t></w:r>'
        "<w:r><w:rPr><w:b/></w:rPr><w:t>Kessler GmbH</w:t></w:r>"
        '<w:r><w:t xml:space="preserve"> liefert </w:t></w:r>'
        "<w:r><w:rPr><w:i/></w:rPr><w:t>sofort</w:t></w:r>"
        '<w:r><w:t xml:space="preserve"> 120 m</w:t></w:r>'
        '<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>2</w:t></w:r>'
        "<w:r><w:rPr><w:vanish/></w:rPr><w:t> verborgen</w:t></w:r>"
        "<w:del><w:r><w:delText>gestrichen</w:delText></w:r></w:del>"
        '<w:ins><w:r><w:t xml:space="preserve">.</w:t></w:r></w:ins></w:p>'
        + item("Lieferung", 0)
        + item("Leser", 1)
        + item("Montage", 0)
        + para("Wer zahlt, bestimmt.", "Quote")
    )
    note = convert(docx(document, styles=HEADINGS, numbering=numbering), "v.docx")
    assert body(note.markdown).split("\n\n") == [
        "# Vertrag",
        "## Umfang",
        "Die **Kessler GmbH** liefert *sofort* 120 m².",
        "1. Lieferung\n   - Leser\n2. Montage",
        "> Wer zahlt, bestimmt.",
    ]
    assert note.meta["title"] == "Vertrag"


def test_a_word_list_interrupted_by_a_table_goes_on_counting():
    numbering = (
        '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
        '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>'
    )
    item = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
    table = (
        "<w:tbl><w:tr><w:tc>" + para("a") + "</w:tc><w:tc>" + para("b") + "</w:tc></w:tr>"
        "<w:tr><w:tc>" + para("c") + "</w:tc><w:tc>" + para("d") + "</w:tc></w:tr></w:tbl>"
    )
    document = (
        para("eins", props=item) + para("zwei", props=item) + table + para("drei", props=item)
    )
    text = body(convert(docx(document, numbering=numbering), "l.docx").markdown)
    assert text.endswith("3. drei")
    assert text.startswith("1. eins\n2. zwei")


def test_word_tables_spans_merges_and_layout_tables():
    grid = (
        "<w:tbl>"
        "<w:tr><w:tc>"
        + para("Gewerk")
        + '</w:tc><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
        + para("Kosten")
        + "</w:tc></w:tr>"
        '<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>' + para("Elektro") + "</w:tc>"
        "<w:tc>" + para("Plan") + "</w:tc><w:tc>" + para("Ist") + "</w:tc></w:tr>"
        "<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr>" + para("") + "</w:tc>"
        "<w:tc>" + para("120") + "</w:tc><w:tc>" + para("98") + "</w:tc></w:tr>"
        "</w:tbl>"
    )
    layout = "<w:tbl><w:tr><w:tc>" + para("Nur ein Kasten") + "</w:tc></w:tr></w:tbl>"
    text = body(convert(docx(grid + layout), "t.docx").markdown)
    assert text.split("\n\n") == [
        "| Gewerk | Kosten |  |\n| --- | --- | --- |\n| Elektro | Plan | Ist |\n|  | 120 | 98 |",
        "Nur ein Kasten",
    ]


def test_word_links_fields_footnotes_and_text_boxes():
    document = (
        '<w:p><w:hyperlink r:id="rId5"><w:r><w:t>Portal</w:t></w:r></w:hyperlink>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> HYPERLINK "https://example.org/b" </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:r><w:t xml:space="preserve"> Bedingungen</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        '<w:r><w:t xml:space="preserve"> siehe</w:t></w:r>'
        '<w:r><w:footnoteReference w:id="2"/></w:r>'
        '<w:r><mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
        "<mc:Choice><w:drawing><w:txbxContent>"
        + para("Im Kasten")
        + "</w:txbxContent></w:drawing></mc:Choice>"
        "<mc:Fallback><w:pict><w:txbxContent>"
        + para("Im Kasten")
        + "</w:txbxContent></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r></w:p>"
    )
    notes = (
        f"<w:footnotes {W}>"
        '<w:footnote w:type="separator" w:id="0"><w:p/></w:footnote>'
        '<w:footnote w:id="2">' + para("Gilt ab 2026.") + "</w:footnote></w:footnotes>"
    )
    data = docx(
        document,
        relations=(("rId5", "hyperlink", "https://example.org/a"),),
        external=("rId5",),
        extra={"word/footnotes.xml": notes},
    )
    text = body(convert(data, "f.docx").markdown)
    assert text.split("\n\n") == [
        "[Portal](https://example.org/a) [Bedingungen](https://example.org/b) siehe[^2]",
        "Im Kasten",
        "[^2]: Gilt ab 2026.",
    ]


def test_a_broken_package_is_a_conversion_error():
    with pytest.raises(markdown.ConversionError):
        convert(b"not a zip", "x.docx")
    evil = docx("", extra={"word/document.xml": '<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'})
    with pytest.raises(markdown.ConversionError, match="document type"):
        convert(evil, "x.docx")


# --------------------------------------------------------------------------
# PowerPoint


def pptx(slides: list[str], notes: dict[int, str] | None = None) -> bytes:
    notes = notes or {}
    parts: dict[str, str | bytes] = {
        "_rels/.rels": rels(("rId1", "officeDocument", "ppt/presentation.xml")),
        "ppt/presentation.xml": (
            f"<p:presentation {P} {R}><p:sldIdLst>"
            + "".join(f'<p:sldId id="{256 + i}" r:id="rId{10 + i}"/>' for i in range(len(slides)))
            + "</p:sldIdLst></p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": rels(
            *[(f"rId{10 + i}", "slide", f"slides/slide{i + 1}.xml") for i in range(len(slides))]
        ),
    }
    for index, tree in enumerate(slides, 1):
        parts[f"ppt/slides/slide{index}.xml"] = (
            f"<p:sld {P} {A} {R}><p:cSld><p:spTree>{tree}</p:spTree></p:cSld></p:sld>"
        )
        if index in notes:
            parts[f"ppt/slides/_rels/slide{index}.xml.rels"] = rels(
                ("rId1", "notesSlide", f"../notesSlides/notesSlide{index}.xml")
            )
            parts[f"ppt/notesSlides/notesSlide{index}.xml"] = (
                f"<p:notes {P} {A}><p:cSld><p:spTree>"
                + shape("body", [(0, notes[index])])
                + "</p:spTree></p:cSld></p:notes>"
            )
    return package(parts)


def shape(kind: str | None, paragraphs: list[tuple[int, str]], y: int = 0) -> str:
    ph = f'<p:nvPr><p:ph type="{kind}"/></p:nvPr>' if kind else "<p:nvPr/>"
    text = "".join(
        f'<a:p><a:pPr lvl="{level}"/><a:r><a:t>{t}</a:t></a:r></a:p>' for level, t in paragraphs
    )
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="1" name="s"/><p:cNvSpPr/>{ph}</p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="0" y="{y}"/></a:xfrm></p:spPr>'
        f"<p:txBody>{text}</p:txBody></p:sp>"
    )


def test_slides_in_order_with_titles_bullets_and_notes():
    first = shape("ctrTitle", [(0, "Quartal")], 100) + shape("subTitle", [(0, "Nord")], 900)
    second = (
        shape("body", [(0, "Umsatz +12 %"), (1, "Industrie"), (0, "Churn")], 900)
        + shape("title", [(0, "Highlights")], 100)
        + shape(None, [(0, "Stand: März")], 5000)
    )
    text = body(convert(pptx([first, second], {2: "Zahlen vorläufig"}), "q.pptx").markdown)
    assert text.split("\n\n") == [
        "<!-- slide 1 -->",
        "## Quartal",
        "Nord",
        "<!-- slide 2 -->",
        "## Highlights",
        "- Umsatz +12 %\n  - Industrie\n- Churn",
        "Stand: März",
        "> **Notes:**\n>\n> Zahlen vorläufig",
    ]


# --------------------------------------------------------------------------
# Excel


def xlsx(
    sheets: dict[str, str], shared: list[str], styles: str = "", date1904: bool = False
) -> bytes:
    names = list(sheets)
    parts: dict[str, str | bytes] = {
        "_rels/.rels": rels(("rId1", "officeDocument", "xl/workbook.xml")),
        "xl/workbook.xml": (
            f"<workbook {S} {R}>"
            + ('<workbookPr date1904="1"/>' if date1904 else "")
            + "<sheets>"
            + "".join(
                f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                for i, n in enumerate(names)
            )
            + "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": rels(
            *[
                (f"rId{i + 1}", "worksheet", f"worksheets/sheet{i + 1}.xml")
                for i in range(len(names))
            ],
            ("rIdS", "sharedStrings", "sharedStrings.xml"),
            ("rIdT", "styles", "styles.xml"),
        ),
        "xl/sharedStrings.xml": f"<sst {S}>"
        + "".join(f"<si><t>{s}</t></si>" for s in shared)
        + "</sst>",
        "xl/styles.xml": f"<styleSheet {S}>{styles}</styleSheet>",
    }
    for i, name in enumerate(names):
        parts[f"xl/worksheets/sheet{i + 1}.xml"] = (
            f"<worksheet {S}><sheetData>{sheets[name]}</sheetData></worksheet>"
        )
    return package(parts)


def test_a_spreadsheet_is_a_table_per_block_with_real_dates_and_percentages():
    styles = (
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="dd/mm/yyyy\\ hh:mm"/></numFmts>'
        '<cellXfs count="4"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="10"/>'
        '<xf numFmtId="164"/></cellXfs>'
    )
    sheet = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
        '<row r="3"><c r="A3" t="s"><v>1</v></c><c r="B3" t="s"><v>2</v></c>'
        '<c r="C3" t="s"><v>3</v></c><c r="D3" t="s"><v>4</v></c></row>'
        '<row r="4"><c r="A4" t="inlineStr"><is><t>Elektro</t></is></c><c r="B4"><v>120000</v></c>'
        '<c r="C4" s="2"><v>0.8208</v></c><c r="D4" s="1"><v>46082</v></c></row>'
        '<row r="5"><c r="A5" t="s"><v>5</v></c><c r="B5"><f>B4*2</f><v>240000</v></c>'
        '<c r="C5" t="b"><v>1</v></c><c r="D5" s="3"><v>46082.5</v></c></row>'
    )
    data = xlsx(
        {"Kosten": sheet}, ["Übersicht", "Gewerk", "Budget", "Quote", "Stand", "Summe"], styles
    )
    note = convert(data, "kosten.xlsx")
    assert body(note.markdown).split("\n\n") == [
        "<!-- sheet Kosten -->",
        "## Kosten",
        "Übersicht",
        "| Gewerk | Budget | Quote | Stand |\n| --- | ---: | --- | --- |\n"
        "| Elektro | 120000 | 82.08 % | 2026-03-01 |\n| Summe | 240000 | TRUE | 2026-03-01 12:00 |",
    ]
    assert note.meta["title"] == "kosten"
    assert note.meta["sheets"] == 1


def test_a_long_sheet_is_cut_and_says_so():
    rows = "".join(
        f'<row r="{r}"><c r="A{r}"><v>{r}</v></c><c r="B{r}"><v>{r * 2}</v></c></row>'
        for r in range(1, 21)
    )
    note = convert(xlsx({"S": rows}, []), "s.xlsx", max_rows=5)
    text = body(note.markdown)
    assert [line for line in text.split("\n") if line.startswith("| ")][2:] == [
        "| 2 | 4 |",
        "| 3 | 6 |",
        "| 4 | 8 |",
        "| 5 | 10 |",
    ]
    assert text.endswith("*15 more rows not shown*")
    assert note.meta["truncated"] is True


# --------------------------------------------------------------------------
# OpenDocument, e-books, web pages, mail, RTF


ODF = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink"'
)


def test_an_opendocument_text():
    content = (
        f"<office:document-content {ODF}><office:automatic-styles>"
        '<style:style style:name="T1" style:family="text"><style:text-properties fo:font-weight="bold"/></style:style>'
        '<text:list-style style:name="L1"><text:list-level-style-number text:level="1"/></text:list-style>'
        "</office:automatic-styles><office:body><office:text>"
        '<text:h text:outline-level="1">Protokoll</text:h>'
        '<text:p>Anwesend: <text:span text:style-name="T1">Albers</text:span>,<text:s/>Brandt'
        "<text:note><text:note-citation>1</text:note-citation><text:note-body><text:p>Später.</text:p>"
        "</text:note-body></text:note></text:p>"
        '<text:list text:style-name="L1"><text:list-item><text:p>Plan</text:p></text:list-item>'
        "<text:list-item><text:p>Bau</text:p></text:list-item></text:list>"
        '<text:p><text:a xlink:href="https://example.org">Link</text:a></text:p>'
        "<table:table><table:table-row><table:table-cell><text:p>A</text:p></table:table-cell>"
        "<table:table-cell><text:p>B</text:p></table:table-cell></table:table-row>"
        '<table:table-row table:number-rows-repeated="1"><table:table-cell office:value-type="float" office:value="3">'
        "<text:p>3</text:p></table:table-cell><table:table-cell/></table:table-row></table:table>"
        "</office:text></office:body></office:document-content>"
    )
    data = package({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": content})
    assert body(convert(data, "p.odt").markdown).split("\n\n") == [
        "# Protokoll",
        "Anwesend: **Albers**, Brandt[^1]",
        "1. Plan\n2. Bau",
        "[Link](https://example.org)",
        "| A | B |\n| --- | --- |\n| 3 |  |",
        "[^1]: Später.",
    ]


def test_html_keeps_the_document_and_drops_the_page_around_it():
    page = """<html lang="de"><head><title>Newsletter</title><style>p{}</style></head><body>
    <nav><a href="/">Start</a></nav>
    <table role="presentation"><tr><td><h1>Neu im März</h1><p>Die <a href="https://example.org/app">App</a>
    ist da.<p>Wartung am 12.&nbsp;Mai</td></tr></table>
    <table><tr><th rowspan="2">Raum</th><th colspan="2">Status</th></tr>
    <tr><td>alt</td><td>neu</td></tr><tr><td>1.01</td><td>ok</td><td>–</td></tr></table>
    <ul><li>eins<ul><li>zwei</li></ul></li><li>drei</ul>
    <pre><code class="language-python">x = 1
if x:
    print(x)</code></pre>
    <script>alert(1)</script><p style="display:none">versteckt</p>
    </body></html>"""
    note = convert(page.encode(), "n.html")
    assert note.meta["title"] == "Newsletter" and note.meta["language"] == "de"
    assert body(note.markdown).split("\n\n") == [
        "# Neu im März",
        "Die [App](https://example.org/app) ist da.",
        "Wartung am 12. Mai",
        "| Raum | Status |  |\n| --- | --- | --- |\n|  | alt | neu |\n| 1.01 | ok | – |",
        "- eins\n  - zwei\n- drei",
        "```python\nx = 1\nif x:\n    print(x)\n```",
    ]


def test_a_web_page_reads_pictures_from_its_own_folder_only(tmp_path):
    (tmp_path / "site").mkdir()
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"\x89PNG" + b"0" * 5000)
    (tmp_path / "site" / "logo.png").write_bytes(b"\x89PNG" + b"1" * 5000)
    page = tmp_path / "site" / "p.html"
    page.write_text(
        '<p>x</p><img src="logo.png" alt="a b"><img src="../secret.png" alt="c d">',
        encoding="utf-8",
    )
    note = markdown.convert(page, assets=True, ocr=False)
    assert list(note.assets) == ["logo.png"]


def test_a_mail_with_its_headers_body_and_attachments():
    inner = EmailMessage()
    inner["Subject"] = "Ursprung"
    inner["From"] = "a@example.org"
    inner.set_content("Erste Nachricht.")
    mail = EmailMessage()
    mail["Subject"] = "Rückfrage zum Angebot"
    mail["From"] = "Jürgen Weißmüller <j.w@example.org>"
    mail["To"] = "Maurice <m@example.org>, b@example.org"
    mail["Date"] = "Tue, 24 Mar 2026 10:15:00 +0100"
    mail.set_content("Hallo,\nbitte Position 2 prüfen.\n")
    mail.add_alternative("<p>Hallo,</p><p>bitte <b>Position 2</b> prüfen.</p>", subtype="html")
    mail.add_attachment(
        b"Liste:\n- eins\n- zwei\n", maintype="text", subtype="plain", filename="liste.txt"
    )
    mail.add_attachment(inner)
    note = convert(bytes(mail), "r.eml")
    assert note.meta["title"] == "Rückfrage zum Angebot"
    assert note.meta["from"] == "Jürgen Weißmüller <j.w@example.org>"
    assert note.meta["to"] == ["Maurice <m@example.org>", "b@example.org"]
    assert note.meta["date"] == dt.datetime(
        2026, 3, 24, 10, 15, tzinfo=dt.timezone(dt.timedelta(hours=1))
    )
    assert note.meta["attachments"] == ["liste.txt"]
    text = body(note.markdown)
    assert text.split("\n\n")[:2] == ["Hallo,", "bitte **Position 2** prüfen."]
    assert "## Attachment: liste.txt\n\nListe:\n\n- eins\n- zwei" in text
    assert "## Attachment: Ursprung" in text and "Erste Nachricht." in text


def test_rtf_in_its_code_page_with_tables_lists_links_and_notes():
    bs = "\\"
    rtf = (
        "{" + bs + "rtf1" + bs + "ansi" + bs + "ansicpg1252{" + bs + "fonttbl{" + bs + "f0 Arial;}}"
        "{" + bs + "stylesheet{" + bs + "s0 Normal;}{" + bs + "s1 heading 1;}}"
        "{"
        + bs
        + "info{"
        + bs
        + "title Notiz}}"
        + bs
        + "pard"
        + bs
        + "s1 Begehung am 3. M"
        + bs
        + "'e4rz"
        + bs
        + "par"
        + bs
        + "pard Zugang "
        + bs
        + "b Nord"
        + bs
        + "b0  ist "
        + bs
        + "i bereit"
        + bs
        + "i0 , Gr"
        + bs
        + "u"
        + "252?"
        + "n und "
        + bs
        + "u"
        + "8364?5{"
        + bs
        + "v versteckt}."
        + bs
        + "par"
        + bs
        + "pard{"
        + bs
        + "pntext"
        + bs
        + "'95"
        + bs
        + "tab}"
        + bs
        + "ls1 eins"
        + bs
        + "par"
        + bs
        + "pard{"
        + bs
        + "pntext"
        + bs
        + "'95"
        + bs
        + "tab}"
        + bs
        + "ls1 zwei"
        + bs
        + "par"
        + bs
        + "trowd"
        + bs
        + "pard"
        + bs
        + "intbl Raum"
        + bs
        + "cell Status"
        + bs
        + "cell"
        + bs
        + "row"
        + bs
        + "trowd"
        + bs
        + "pard"
        + bs
        + "intbl 1.01"
        + bs
        + "cell ok"
        + bs
        + "cell"
        + bs
        + "row"
        + bs
        + "pard Siehe {"
        + bs
        + "field{"
        + bs
        + "*"
        + bs
        + 'fldinst HYPERLINK "https://example.org"}'
        "{"
        + bs
        + "fldrslt Protokoll}}{"
        + bs
        + "footnote "
        + bs
        + "pard"
        + bs
        + "s1 Fu"
        + bs
        + "'df.}."
        + bs
        + "par}"
    ).encode("latin-1")
    note = convert(rtf, "n.rtf")
    assert note.meta["title"] == "Notiz"
    assert body(note.markdown).split("\n\n") == [
        "# Begehung am 3. März",
        "Zugang **Nord** ist *bereit*, Grün und €5.",
        "- eins\n- zwei",
        "| Raum | Status |\n| --- | --- |\n| 1.01 | ok |",
        "Siehe [Protokoll](https://example.org)[^1].",
        "[^1]: Fuß.",
    ]


def test_an_epub_in_reading_order():
    container = (
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/book.opf"/></rootfiles></container>'
    )
    opf = (
        '<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<metadata><dc:title>Handbuch</dc:title><dc:creator>Helvetor</dc:creator><dc:language>de-DE</dc:language></metadata>"
        '<manifest><item id="b" href="b.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="n" href="nav.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="n" linear="no"/><itemref idref="a"/><itemref idref="b"/></spine></package>'
    )
    data = package(
        {
            "mimetype": "application/epub+zip",
            "META-INF/container.xml": container,
            "OEBPS/book.opf": opf,
            "OEBPS/a.xhtml": "<html><body><h1>Eins</h1><p>Erstes Kapitel.</p></body></html>",
            "OEBPS/b.xhtml": "<html><body><h1>Zwei</h1><p>Zweites.</p></body></html>",
            "OEBPS/nav.xhtml": "<html><body><nav>Inhalt</nav><p>Inhaltsverzeichnis</p></body></html>",
        }
    )
    note = convert(data, "h.epub")
    assert (note.meta["title"], note.meta["author"], note.meta["language"]) == (
        "Handbuch",
        "Helvetor",
        "de",
    )
    assert body(note.markdown).split("\n\n") == [
        "<!-- chapter 1 -->",
        "# Eins",
        "Erstes Kapitel.",
        "<!-- chapter 2 -->",
        "# Zwei",
        "Zweites.",
    ]


# --------------------------------------------------------------------------
# Text files


def test_plain_text_joins_wrapped_lines_and_keeps_deliberate_ones():
    text = (
        "Hinweise\n========\n\n"
        "Die Anlage wird jeden Montag geprüft. Bei Störungen bitte die\n"
        "Hotline anrufen, die rund um die Uhr erreichbar ist, und warten.\n\n"
        "- Karte verloren: sofort\n  melden\n- Tür klemmt: Hausmeister\n\n"
        "Firma Helvetor\nHafenstraße 12\n28195 Bremen\n\n"
        "> Zitat aus der\n> Mail\n\n"
        "Raum\tStatus\n1.01\tok\n"
    ).encode("cp1252")
    assert body(convert(text, "h.txt").markdown).split("\n\n") == [
        "# Hinweise",
        "Die Anlage wird jeden Montag geprüft. Bei Störungen bitte die Hotline anrufen, die rund "
        "um die Uhr erreichbar ist, und warten.",
        "- Karte verloren: sofort melden\n- Tür klemmt: Hausmeister",
        "Firma Helvetor  \nHafenstraße 12  \n28195 Bremen",
        "> Zitat aus der Mail",
        "| Raum | Status |\n| --- | --- |\n| 1.01 | ok |",
    ]


def test_text_encodings_are_recognized():
    assert "Grüße" in convert("Grüße".encode("utf-16"), "a.txt").markdown
    assert "Grüße" in convert("Grüße".encode("cp1252"), "a.txt").markdown
    assert "Grüße" in convert((chr(0xFEFF) + "Grüße").encode(), "a.txt").markdown


def test_csv_tables_find_their_delimiter_and_markdown_keeps_its_own_front_matter():
    csv = convert(b"Name;Ort\nAlbers;Bremen\n", "a.csv").markdown
    assert body(csv) == "| Name | Ort |\n| --- | --- |\n| Albers | Bremen |"
    note = convert(b"---\ntags: [a, b]\ntitle: Eigen\n---\n# Kopf\n\nText [[Link]].\n", "n.md")
    assert note.markdown.startswith('---\ntags: [a, b]\ntitle: Eigen\nsource_type: "md"\n')
    assert body(note.markdown) == "# Kopf\n\nText [[Link]]."


def test_source_code_is_one_fenced_block():
    note = convert(b"def f():\n    return 1\n", "tool.py")
    assert body(note.markdown) == "```python\ndef f():\n    return 1\n```"
    assert markdown.supported("Dockerfile") and not markdown.supported("x.exe")


# --------------------------------------------------------------------------
# Scans and PDFs (these need the OCR models)


def multipage_pdf(pages: list[list[tuple[str, int, int, int]]]) -> bytes:
    """A PDF whose pages each hold `(text, size, x, y)` runs of Helvetica."""
    from conftest import _winansi_literal

    objects = ["<< /Type /Catalog /Pages 2 0 R >>", ""]
    kids = []
    font_id = 3 + 2 * len(pages)
    for index, runs in enumerate(pages):
        page_id = 3 + 2 * index
        kids.append(f"{page_id} 0 R")
        stream = (
            "BT\n"
            + "\n".join(
                f"/F1 {size} Tf 1 0 0 1 {x} {y} Tm ({_winansi_literal(text)}) Tj"
                for text, size, x, y in runs
            )
            + "\nET"
        )
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {page_id + 1} 0 R >>"
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    objects.append(
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    out = "%PDF-1.4\n"
    offsets = []
    for number, content in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{content}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{o:010} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


def test_a_pdf_note_has_page_markers_and_loses_its_running_heads(engine):
    pages = []
    bodies = [
        "Der Vertrag beginnt am ersten Mai und endet",
        "am dreissigsten Juni. Danach gilt die",
        "Verlaengerung um jeweils ein Jahr.",
    ]
    for number, text in enumerate(bodies, 1):
        pages.append(
            [
                ("Kessler Logistik GmbH", 12, 60, 760),
                (text, 14, 60, 600),
                (f"Seite {number} von 3", 10, 280, 30),
            ]
        )
    note = markdown.convert(multipage_pdf(pages), name="v.pdf", engine=engine_auto())
    text = body(note.markdown)
    assert "Kessler" not in text and "Seite" not in text
    assert text.count("<!-- page ") == 3
    assert note.meta["pages"] == 3 and note.meta["extraction"] == "text"
    # A sentence cut by a page break is one paragraph again.
    assert "Der Vertrag beginnt am ersten Mai und endet am dreissigsten Juni." in text


def engine_auto():
    import ocrust

    return ocrust.Ocr(models_dir=os.environ.get("OCRUST_MODELS_DIR"), pdf_text="auto")


def test_a_born_digital_table_becomes_a_pipe_table(engine, table_pdf):
    note = markdown.convert(table_pdf, engine=engine_auto())
    assert "| Position | Menge | Preis | Summe |" in note.markdown
    assert "| Kabel C | 7 | 12,50 | 87,50 |" in note.markdown
    assert note.meta["extraction"] == "text"


def test_a_picture_in_a_word_file_is_read_and_can_be_kept(engine, image_fixtures, tmp_path):
    png = image_fixtures["png"].read_bytes()
    document = (
        para("Siehe Plan:")
        + '<w:p><w:r><w:drawing><wp:docPr xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/'
        'wordprocessingDrawing" id="1" name="Bild" descr="Rechnung als Bild"/>'
        f'<a:blip {A} r:embed="rId9"/></w:drawing></w:r></w:p>'
    )
    data = docx(
        document,
        relations=(("rId9", "image", "media/image1.png"),),
        extra={"word/media/image1.png": png},
    )
    note = markdown.convert(data, name="p.docx", engine=engine, assets=True)
    text = body(note.markdown)
    assert "![Rechnung als Bild](image1.png)" in text
    assert "> INVOICE 2026-0042" in text
    assert note.assets == {"image1.png": png}


# --------------------------------------------------------------------------
# The folder of notes


def make_tree(root: Path) -> None:
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "brief.txt").write_text("Erster Brief.\n", encoding="utf-8")
    (root / "a" / "b" / "notiz.md").write_text("# Notiz\n\nText.\n", encoding="utf-8")
    (root / "a" / "b" / "brief.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (root / "a" / "tool.exe").write_bytes(b"MZ")
    (root / "a" / ".versteckt.txt").write_text("nein", encoding="utf-8")


def test_an_export_mirrors_the_folder_and_converts_only_what_changed(tmp_path):
    make_tree(tmp_path)
    out = tmp_path / "wissen"
    first = markdown.export([tmp_path / "a"], out)
    assert sorted(p.relative_to(out).as_posix() for _, p in first.written) == [
        "b/brief.csv.md",
        "b/notiz.md.md",
        "brief.txt.md",
    ]
    assert [p.name for p in first.skipped] == ["tool.exe"]
    index = json.loads((out / ".ocrust" / "index.json").read_text(encoding="utf-8"))
    assert len(index["documents"]) == 3
    assert not (out / ".ocrust" / "lock").exists()
    note = (out / "brief.txt.md").read_text(encoding="utf-8")
    assert 'source: "a/brief.txt"' in note and "Erster Brief." in note

    second = markdown.export([tmp_path / "a"], out)
    assert not second.written and len(second.unchanged) == 3

    # Touched but not changed: recognized by its content, and not rewritten.
    os.utime(tmp_path / "a" / "brief.txt", (1, 1))
    third = markdown.export([tmp_path / "a"], out)
    assert not third.written

    (tmp_path / "a" / "brief.txt").write_text("Zweiter Brief.\n", encoding="utf-8")
    fourth = markdown.export([tmp_path / "a"], out)
    assert [s.name for s, _ in fourth.written] == ["brief.txt"]
    assert "Zweiter Brief." in (out / "brief.txt.md").read_text(encoding="utf-8")


def test_a_note_edited_by_hand_or_not_written_by_an_export_is_never_overwritten(tmp_path):
    make_tree(tmp_path)
    out = tmp_path / "wissen"
    markdown.export([tmp_path / "a"], out)
    note = out / "brief.txt.md"
    note.write_text(note.read_text(encoding="utf-8") + "\nMeine Ergänzung\n", encoding="utf-8")
    (tmp_path / "a" / "brief.txt").write_text("Geändert.\n", encoding="utf-8")
    stranger = out / "b" / "fremd.txt.md"
    stranger.write_text("von Hand", encoding="utf-8")
    (tmp_path / "a" / "b" / "fremd.txt").write_text("Quelle", encoding="utf-8")

    result = markdown.export([tmp_path / "a"], out)
    kept = sorted(p.name for p, _ in result.kept)
    assert kept == ["brief.txt.md", "fremd.txt.md"]
    assert (
        "Meine Ergänzung" in note.read_text(encoding="utf-8")
        and stranger.read_text(encoding="utf-8") == "von Hand"
    )
    # And again on the next run: a kept note is not quietly taken over.
    assert sorted(p.name for p, _ in markdown.export([tmp_path / "a"], out).kept) == kept

    forced = markdown.export([tmp_path / "a"], out, force=True)
    assert not forced.kept
    assert "Geändert." in note.read_text(encoding="utf-8") and "Quelle" in stranger.read_text(
        encoding="utf-8"
    )


def test_prune_removes_what_is_gone_and_only_that(tmp_path):
    make_tree(tmp_path)
    other = tmp_path / "anderes"
    other.mkdir()
    (other / "x.txt").write_text("anders", encoding="utf-8")
    out = tmp_path / "wissen"
    markdown.export([tmp_path / "a"], out)
    markdown.export([other], out)
    (tmp_path / "a" / "b" / "brief.csv").unlink()
    edited = out / "b" / "notiz.md.md"
    (tmp_path / "a" / "b" / "notiz.md").unlink()
    edited.write_text(edited.read_text(encoding="utf-8") + "\nbehalten\n", encoding="utf-8")

    kept = markdown.export([tmp_path / "a"], out)
    assert (out / "b" / "brief.csv.md").exists()

    dry = markdown.export([tmp_path / "a"], out, prune=True, dry_run=True)
    assert (out / "b" / "brief.csv.md").exists() and dry.pruned

    pruned = markdown.export([tmp_path / "a"], out, prune=True)
    assert [p.name for p in pruned.pruned] == ["brief.csv.md"]
    assert not (out / "b" / "brief.csv.md").exists()
    # A note edited by hand survives its document; another folder's notes are
    # none of this run's business.
    assert edited.exists() and (out / "x.txt.md").exists()
    assert kept.ok


def test_two_documents_that_would_share_a_note_are_refused(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    (tmp_path / "x" / "a.txt").write_text("eins", encoding="utf-8")
    (tmp_path / "y" / "a.txt").write_text("zwei", encoding="utf-8")
    result = markdown.export([tmp_path / "x" / "a.txt", tmp_path / "y" / "a.txt"], tmp_path / "out")
    assert len(result.failed) == 1 and "rename one" in result.failed[0][1]


def test_several_folders_each_get_their_own_folder(tmp_path):
    for name in ("x", "y"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.txt").write_text(name, encoding="utf-8")
    result = markdown.export([tmp_path / "x", tmp_path / "y"], tmp_path / "out")
    assert sorted(p.relative_to(tmp_path / "out").as_posix() for _, p in result.written) == [
        "x/a.txt.md",
        "y/a.txt.md",
    ]


def test_an_archive_becomes_a_folder_of_notes_and_nothing_escapes_it(tmp_path):
    data = package(
        {"docs/a.txt": "eins", "../../evil.txt": "böse", "b.csv": "x,y\n1,2\n", "c.bin": b"\x00"}
    )
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "paket.zip").write_bytes(data)
    out = tmp_path / "out"
    result = markdown.export([tmp_path / "in"], out)
    assert sorted(p.relative_to(out).as_posix() for _, p in result.written) == [
        "paket.zip/b.csv.md",
        "paket.zip/docs/a.txt.md",
    ]
    assert not (tmp_path / "evil.txt.md").exists()
    assert 'source: "in/paket.zip/docs/a.txt"' in (
        out / "paket.zip" / "docs" / "a.txt.md"
    ).read_text(encoding="utf-8")


def test_kept_pictures_live_in_assets_and_leave_with_their_note(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 3000
    data = docx(
        para("x") + f'<w:p><w:r><w:drawing><a:blip {A} r:embed="rId9"/></w:drawing></w:r></w:p>',
        relations=(("rId9", "image", "media/image1.png"),),
        extra={"word/media/image1.png": png},
    )
    (tmp_path / "in" / "sub").mkdir(parents=True)
    source = tmp_path / "in" / "sub" / "d.docx"
    source.write_bytes(data)
    out = tmp_path / "out"
    markdown.export([tmp_path / "in"], out, assets=True, ocr=False)
    asset = out / "_assets" / "sub" / "d.docx" / "image1.png"
    assert asset.read_bytes() == png
    assert "![](../_assets/sub/d.docx/image1.png)" in (out / "sub" / "d.docx.md").read_text(
        encoding="utf-8"
    )
    source.unlink()
    markdown.export([tmp_path / "in"], out, assets=True, ocr=False, prune=True)
    assert not asset.exists() and not (out / "_assets").exists()


def test_a_second_export_into_the_same_folder_waits_its_turn(tmp_path):
    make_tree(tmp_path)
    out = tmp_path / "wissen"
    (out / ".ocrust").mkdir(parents=True)
    (out / ".ocrust" / "lock").write_text(str(os.getppid()), encoding="utf-8")
    with pytest.raises(markdown.ConversionError, match="another export"):
        markdown.export([tmp_path / "a"], out)
    # A lock whose process is gone is taken over.
    (out / ".ocrust" / "lock").write_text("999999999", encoding="utf-8")
    assert markdown.export([tmp_path / "a"], out).ok


# --------------------------------------------------------------------------
# The command line


def test_the_command_line_writes_one_note_or_a_folder(tmp_path, capsys, monkeypatch):
    make_tree(tmp_path)
    source = tmp_path / "a" / "brief.txt"
    assert main(["markdown", str(source)]) == 0
    assert "Erster Brief." in capsys.readouterr().out
    assert main(["md", str(source), "-o", str(tmp_path / "n.md"), "-q"]) == 0
    assert (tmp_path / "n.md").read_text(encoding="utf-8").startswith("---\ntitle:")
    assert main(["md", str(tmp_path / "a"), str(source)]) == 2
    assert main(["md", str(tmp_path / "a"), "-o", str(tmp_path / "out")]) == 0
    assert "3 notes written" in capsys.readouterr().err
    assert main(["md", str(tmp_path / "a"), "-o", str(tmp_path / "out"), "-q"]) == 0
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"x;y\n1;2\n")))
    assert main(["md", "-", "--name", "t.csv"]) == 0
    assert "| x | y |" in capsys.readouterr().out
