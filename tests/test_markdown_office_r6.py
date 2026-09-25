"""Regression tests for the Office readers, sixth review round: ruby in Word,
legacy form fields, imported chunks (`w:altChunk`), and hidden rows and
columns of spreadsheets.

Each test names the case it is about. Most of them failed before their fix;
the others hold its edges: what must stay as it was, and input that must not
break it.
"""

from __future__ import annotations

import io
import zipfile

from test_markdown import W, body, convert, docx, package, para, xlsx

ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
)


def run(text: str, props: str = "") -> str:
    rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'


def with_part(data: bytes, name: str, change: object) -> bytes:
    """A package with one of its parts changed."""
    source = zipfile.ZipFile(io.BytesIO(data))
    parts: dict[str, str | bytes] = {n: source.read(n) for n in source.namelist()}
    parts[name] = change(parts[name].decode())  # type: ignore[operator, union-attr]
    return package(parts)


# --------------------------------------------------------------------------
# Ruby


def ruby(base: str, reading: str, props: str = "") -> str:
    return (
        '<w:r><w:ruby><w:rubyPr><w:hps w:val="10"/><w:lid w:val="ja-JP"/></w:rubyPr>'
        f"<w:rt>{run(reading, props)}</w:rt><w:rubyBase>{run(base, props)}</w:rubyBase>"
        "</w:ruby></w:r>"
    )


def test_word_ruby_keeps_its_base_text_with_the_reading_after_it():
    document = (
        "<w:p>"
        + run("本社は")
        + ruby("東京", "とうきょう")
        + run("にあります。担当者は")
        + ruby("山田", "やまだ")
        + run("さんです。")
        + "</w:p>"
    )
    text = body(convert(docx(document), "k.docx").markdown)
    assert text == "本社は東京(とうきょう)にあります。担当者は山田(やまだ)さんです。"


def test_word_ruby_without_a_reading_worth_writing_is_its_base_text():
    document = (
        "<w:p>"
        + ruby("大事", "・・")  # emphasis dots, not a reading
        + ruby("ABC", "ABC")
        + ruby("文字", "")
        + "<w:r><w:ruby><w:rubyBase><w:r><w:t>基</w:t></w:r></w:rubyBase></w:ruby></w:r>"
        + "<w:r><w:ruby><w:rt><w:r><w:t>よみ</w:t></w:r></w:rt></w:ruby></w:r>"
        + "</w:p>"
    )
    assert body(convert(docx(document), "k.docx").markdown) == "大事ABC文字基(よみ)"


def test_a_bold_word_ruby_stays_one_bold_phrase():
    document = "<w:p>" + ruby("東京", "とうきょう", "<w:b/>") + "</w:p>"
    assert body(convert(docx(document), "k.docx").markdown) == "**東京(とうきょう)**"


# --------------------------------------------------------------------------
# Legacy form fields


def form_field(data: str, instruction: str, result: str = "", separate: bool = True) -> str:
    return (
        '<w:r><w:fldChar w:fldCharType="begin"><w:ffData><w:name w:val="F"/><w:enabled/>'
        f"{data}</w:ffData></w:fldChar></w:r>"
        f'<w:r><w:instrText xml:space="preserve"> {instruction} </w:instrText></w:r>'
        + ('<w:r><w:fldChar w:fldCharType="separate"/></w:r>' if separate else "")
        + result
        + '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )


def check_box(state: str, separate: bool = True) -> str:
    return form_field(
        f"<w:checkBox><w:sizeAuto/>{state}</w:checkBox>", "FORMCHECKBOX", "", separate
    )


def drop_down(choice: str, entries: tuple[str, ...] = ("I", "II", "III"), result: str = "") -> str:
    listed = "".join(f'<w:listEntry w:val="{entry}"/>' for entry in entries)
    return form_field(f"<w:ddList>{choice}{listed}</w:ddList>", "FORMDROPDOWN", result)


def test_word_form_check_boxes_show_their_state():
    document = (
        "<w:p>"
        + run("Vollzeit: ")
        + check_box('<w:default w:val="0"/><w:checked/>')
        + run(" ja ")
        + check_box('<w:default w:val="0"/>')
        + run(" nein")
        + "</w:p><w:p>"
        # As the form was made, unless the user changed it; no result part.
        + check_box('<w:default w:val="1"/>', separate=False)
        + run(" a ")
        + check_box('<w:default w:val="1"/><w:checked w:val="0"/>', separate=False)
        + run(" b ")
        + check_box("", separate=False)
        + run(" c")
        + "</w:p>"
    )
    text = body(convert(docx(document), "f.docx").markdown)
    assert text.split("\n\n") == ["Vollzeit: ☒ ja ☐ nein", "☒ a ☐ b ☐ c"]


def test_word_form_drop_downs_show_the_entry_chosen():
    document = "".join(
        "<w:p>" + run(f"{label}: ") + field + "</w:p>"
        for label, field in (
            ("chosen", drop_down('<w:result w:val="2"/>')),
            ("default", drop_down('<w:default w:val="1"/>')),
            ("none", drop_down("")),
            ("out of range", drop_down('<w:result w:val="7"/>')),
            ("broken", drop_down('<w:result w:val="zwei"/>')),
            ("empty", drop_down('<w:result w:val="0"/>', entries=())),
            # A result written out is what is shown, once.
            ("written", drop_down('<w:result w:val="0"/>', result=run("II"))),
        )
    )
    text = body(convert(docx(document), "f.docx").markdown)
    assert text.split("\n\n") == [
        "chosen: III",
        "default: II",
        "none: I",
        "out of range:",
        "broken: I",
        "empty:",
        "written: II",
    ]


def test_word_form_text_fields_and_other_fields_keep_their_results():
    document = (
        "<w:p>"
        + run("Name: ")
        + form_field("<w:textInput/>", "FORMTEXT", run("Anna Schmidt"))
        + run(" / Seite ")
        + form_field("", "PAGE", run("3"))
        + "</w:p>"
    )
    assert body(convert(docx(document), "f.docx").markdown) == "Name: Anna Schmidt / Seite 3"


# --------------------------------------------------------------------------
# Imported chunks


CHUNK_HTML = (
    "<html><body><h2>Leistungsbeschreibung</h2><p>Der Auftragnehmer wartet die Anlage.</p>"
    "<table border=1><tr><td>Wartung</td><td>480,00 EUR</td></tr></table></body></html>"
)


def with_chunks(
    chunks: dict[str, tuple[str, str | bytes]],
    types: str = "",
    extra: dict[str, str | bytes] | None = None,
) -> bytes:
    """A Word document with a chunk between two paragraphs for each of
    `chunks`: relationship id → (part, content)."""
    document = (
        para("Vertrag")
        + "".join(f'<w:altChunk r:id="{rid}"/>' for rid in chunks)
        + para("Unterschrift")
    )
    parts: dict[str, str | bytes] = {f"word/{part}": data for part, data in chunks.values()}
    if types:
        parts["[Content_Types].xml"] = (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f"{types}</Types>"
        )
    relations = tuple((rid, "aFChunk", part) for rid, (part, _) in chunks.items())
    return docx(document, extra={**parts, **(extra or {})}, relations=relations)


def test_a_word_chunk_of_html_is_read_in_its_place():
    text = body(convert(with_chunks({"rIdA": ("chunk1.html", CHUNK_HTML)}), "v.docx").markdown)
    assert text.split("\n\n") == [
        "Vertrag",
        "## Leistungsbeschreibung",
        "Der Auftragnehmer wartet die Anlage.",
        "| Wartung | 480,00 EUR |\n| --- | --- |",
        "Unterschrift",
    ]


def test_a_word_chunk_is_read_by_its_content_type_or_what_it_is():
    types = (
        '<Override PartName="/word/afchunk1.dat" ContentType="text/html"/>'
        '<Default Extension="bin" ContentType="text/plain"/>'
    )
    rtf = b"{\\rtf1\\ansi Aus RTF\\par}"
    chunks = {
        "rId1": ("afchunk1.dat", CHUNK_HTML),
        "rId2": ("afchunk2.bin", "Einfacher Text"),
        "rId3": ("afchunk3.dat", rtf),
    }
    text = body(convert(with_chunks(chunks, types), "v.docx").markdown)
    assert text.split("\n\n")[1:-1] == [
        "## Leistungsbeschreibung",
        "Der Auftragnehmer wartet die Anlage.",
        "| Wartung | 480,00 EUR |\n| --- | --- |",
        "Einfacher Text",
        "Aus RTF",
    ]


def test_a_word_chunk_of_another_word_document_keeps_its_footnotes_apart():
    def notes(text: str) -> str:
        return f'<w:footnotes {W}><w:footnote w:id="1">{para(text)}</w:footnote></w:footnotes>'

    ref = '<w:r><w:footnoteReference w:id="1"/></w:r>'
    inner = docx(f"<w:p>{run('Anlage')}{ref}</w:p>", extra={"word/footnotes.xml": notes("Innen.")})
    data = with_chunks(
        {"rIdD": ("anlage.docx", inner)}, extra={"word/footnotes.xml": notes("Außen.")}
    )
    data = with_part(
        data,
        "word/document.xml",
        lambda xml: xml.replace("Vertrag</w:t></w:r>", f"Vertrag</w:t></w:r>{ref}"),
    )
    text = body(convert(data, "v.docx").markdown)
    assert text.split("\n\n") == [
        "Vertrag[^1]",
        "Anlage[^chunk1-1]",
        "[^chunk1-1]: Innen.",
        "Unterschrift",
        "[^1]: Außen.",
    ]


def test_a_word_chunk_that_cannot_be_read_is_left_out_and_nothing_else():
    chunks = {
        "rId1": ("kaputt.docx", b"PK\x03\x04 not really a zip"),
        "rId2": ("fehlt.html", ""),
        "rId3": ("bild.png", b"\x89PNG\r\n\x1a\n"),
        "rId4": ("leer.html", ""),
    }
    data = with_chunks(chunks)
    # One of them is not in the package at all, one points outside it.
    data = with_part(
        data,
        "word/_rels/document.xml.rels",
        lambda xml: xml.replace('Target="fehlt.html"', 'Target="nirgends.html"').replace(
            'Target="leer.html"', 'Target="https://example.org/x.html" TargetMode="External"'
        ),
    )
    data = with_part(
        data,
        "word/document.xml",
        lambda xml: xml.replace("</w:body>", '<w:altChunk r:id="rIdNone"/><w:altChunk/></w:body>'),
    )
    assert body(convert(data, "v.docx").markdown) == "Vertrag\n\nUnterschrift"


def test_word_chunks_nested_in_themselves_stop_at_the_nesting_limit():
    data = with_chunks({"rIdA": ("chunk1.html", CHUNK_HTML)})
    for _ in range(8):
        data = with_chunks({"rIdD": ("inner.docx", data)})
    text = body(convert(data, "v.docx").markdown)
    assert text.startswith("Vertrag\n\nVertrag") and text.endswith("Unterschrift")
    assert "Leistungsbeschreibung" not in text


# --------------------------------------------------------------------------
# Hidden rows and columns


def quote_sheet(cols: str) -> bytes:
    def cell(ref: str, value: str) -> str:
        if value.lstrip("-").isdigit():
            return f'<c r="{ref}"><v>{value}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'

    lines = [
        ("", ("Artikel", "Verkauf", "Einkauf", "Marge", "Notiz")),
        ("", ("Server", "2400", "1650", "750", "ok")),
        (' hidden="1"', ("Rabatt intern", "-300", "0", "-300", "geheim")),
        (' hidden="true" outlineLevel="1"', ("Gruppe zu", "1", "1", "1", "zu")),
        ("", ("Switch", "380", "210", "170", "ok")),
    ]
    rows = "".join(
        f'<row r="{r}"{flag}>'
        + "".join(cell(f"{'ABCDE'[c]}{r}", value) for c, value in enumerate(values))
        + "</row>"
        for r, (flag, values) in enumerate(lines, 1)
    )
    return with_part(
        xlsx({"Angebot": rows}, []),
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(
            "<sheetData>", f"<sheetFormatPr defaultRowHeight='15'/>{cols}<sheetData>"
        ),
    )


def test_hidden_rows_and_columns_of_a_sheet_stay_out_and_do_not_split_its_table():
    cols = (
        '<cols><col min="1" max="2" width="12" customWidth="1"/>'
        '<col min="3" max="4" width="0" hidden="1"/><col min="5" max="5" width="9"/></cols>'
    )
    text = body(convert(quote_sheet(cols), "a.xlsx").markdown)
    assert text.split("\n\n")[2:] == [
        "| Artikel | Verkauf | Notiz |\n| --- | ---: | --- |\n"
        "| Server | 2400 | ok |\n| Switch | 380 | ok |"
    ]


def test_hidden_rows_do_not_count_against_the_row_limit():
    text = body(convert(quote_sheet(""), "a.xlsx", max_rows=3).markdown)
    assert "Switch" in text and "more rows" not in text and "geheim" not in text


def test_column_settings_of_any_prefix_or_malformed_hide_what_they_say_and_never_fail():
    cols = (
        '<x:cols xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<x:col min="3" max="3" hidden="1"/><x:col min="zwei" max="4" hidden="1"/>'
        '<x:col min="4" max="-2" hidden="1"/><x:col min="0" hidden="1"/>'
        '<x:col min="6" max="99999999999999999999" hidden="1"/></x:cols>'
    )
    text = body(convert(quote_sheet(cols), "a.xlsx").markdown)
    assert "Einkauf" not in text and "1650" not in text
    assert "| Artikel | Verkauf | Marge | Notiz |" in text


def test_a_sheet_without_column_settings_or_with_rows_first_hides_no_column():
    for cols in ("", "<cols/>", '<cols><col min="1" max="5" hidden="0"/></cols>'):
        assert (
            "| Server | 2400 | 1650 | 750 | ok |" in convert(quote_sheet(cols), "a.xlsx").markdown
        )
    # Settings after the rows are not where a sheet has them.
    late = with_part(
        quote_sheet(""),
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(
            "</sheetData>", '</sheetData><cols><col min="1" max="5" hidden="1"/></cols>'
        ),
    )
    assert "| Server | 2400 | 1650 | 750 | ok |" in convert(late, "a.xlsx").markdown


def ods(table: str, parts: dict[str, str | bytes] | None = None) -> bytes:
    xml = (
        f"<office:document-content {ODF_NS}><office:body><office:spreadsheet>{table}"
        "</office:spreadsheet></office:body></office:document-content>"
    )
    return package(
        {
            "mimetype": "application/vnd.oasis.opendocument.spreadsheet",
            "content.xml": xml,
            **(parts or {}),
        }
    )


def test_hidden_rows_and_columns_of_an_opendocument_sheet_stay_out():
    def row(values: tuple[str, ...], props: str = "") -> str:
        return (
            f"<table:table-row{props}>"
            + "".join(
                f'<table:table-cell office:value-type="string"><text:p>{v}</text:p></table:table-cell>'
                for v in values
            )
            + "</table:table-row>"
        )

    table = (
        '<table:table table:name="Angebot">'
        "<table:table-column-group><table:table-column/>"
        '<table:table-column table:number-columns-repeated="2" table:visibility="collapse"/>'
        "</table:table-column-group>"
        '<table:table-column table:number-columns-repeated="99999999" table:visibility="visible"/>'
        + row(("Artikel", "Einkauf", "Marge", "Verkauf"))
        + row(("Server", "1650", "750", "2400"), ' table:visibility="visible"')
        + row(("Rabatt", "0", "0", "-300"), ' table:visibility="collapse"')
        + row(
            ("Gefiltert", "1", "1", "1"),
            ' table:visibility="filter" table:number-rows-repeated="3"',
        )
        + row(("Switch", "210", "170", "380"))
        + "</table:table>"
    )
    text = body(convert(ods(table), "a.ods").markdown)
    assert text.split("\n\n")[2:] == [
        "| Artikel | Verkauf |\n| --- | ---: |\n| Server | 2400 |\n| Switch | 380 |"
    ]


def test_a_picture_in_a_hidden_column_of_an_opendocument_sheet_is_not_kept():
    def picture(name: str) -> str:
        return (
            '<table:table-cell><draw:frame xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:'
            'drawing:1.0" xmlns:xlink="http://www.w3.org/1999/xlink">'
            f'<draw:image xlink:href="Pictures/{name}.png"/></draw:frame></table:table-cell>'
        )

    table = (
        '<table:table table:name="T"><table:table-column/>'
        '<table:table-column table:visibility="collapse"/><table:table-row>'
        + picture("offen")
        + picture("geheim")
        + "</table:table-row></table:table>"
    )
    pictures: dict[str, str | bytes] = {
        f"Pictures/{name}.png": f"\x89PNG {name}".encode() for name in ("offen", "geheim")
    }
    note = convert(ods(table, pictures), "a.ods", assets=True, ocr=False)
    assert sorted(note.assets) == ["offen.png"]
