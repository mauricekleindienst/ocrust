"""Regression tests for what an independent review of `ocrust markdown` found.

Each test names the case it is about; every one of them failed before its fix.
"""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
import zipfile
from email.message import EmailMessage

import pytest

from ocrust import markdown
from ocrust.cli import main
from ocrust.markdown import _ir, _render
from test_markdown import (
    A,
    body,
    convert,
    docx,
    package,
    para,
    pptx,
    shape,
    xlsx,
)

BS = chr(92)

ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"'
)


def odf(kind: str, content: str, styles: str = "") -> bytes:
    tag = {"odt": "text", "ods": "spreadsheet", "odp": "presentation"}[kind]
    xml = (
        f"<office:document-content {ODF_NS}><office:automatic-styles>{styles}"
        f"</office:automatic-styles><office:body><office:{tag}>{content}</office:{tag}>"
        "</office:body></office:document-content>"
    )
    return package({"mimetype": f"application/vnd.oasis.opendocument.{tag}", "content.xml": xml})


def rezip(data: bytes, name: str, change: object) -> bytes:
    """A package with one of its parts changed."""
    source = zipfile.ZipFile(io.BytesIO(data))
    parts = {n: source.read(n) for n in source.namelist()}
    parts[name] = change(parts[name].decode()).encode()  # type: ignore[operator]
    return package(parts)


def numbering(*nums: tuple[str, str, str]) -> str:
    """Abstract lists by (id, format) and instances by (num id, abstract id,
    start override or "")."""
    return "".join(
        [
            '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
            '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
            '<w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/></w:lvl>'
            "</w:abstractNum>",
            '<w:abstractNum w:abstractNumId="2"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/>'
            '<w:lvlText w:val="•"/></w:lvl></w:abstractNum>',
            *(
                f'<w:num w:numId="{num}"><w:abstractNumId w:val="{abstract}"/>'
                + (
                    f'<w:lvlOverride w:ilvl="0"><w:startOverride w:val="{start}"/></w:lvlOverride>'
                    if start
                    else ""
                )
                + "</w:num>"
                for num, abstract, start in nums
            ),
        ]
    )


def item(text: str, num: str, level: int = 0) -> str:
    return para(text, props=f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num}"/></w:numPr>')


# -- lists


def test_a_list_that_starts_deeper_loses_no_item():
    lists = numbering(("1", "2", ""))
    text = body(
        convert(
            docx(item("tief", "1", 1) + item("eins", "1") + item("zwei", "1"), numbering=lists),
            "l.docx",
        ).markdown
    )
    assert "tief" in text and "eins" in text and "zwei" in text


def test_two_word_lists_in_a_row_stay_two():
    lists = numbering(("1", "1", ""), ("2", "1", "1"), ("3", "2", ""))
    restarted = docx(
        item("A1", "1") + item("A2", "1") + item("B1", "2") + item("B2", "2"), numbering=lists
    )
    assert body(convert(restarted, "r.docx").markdown) == "1. A1\n2. A2\n\n1) B1\n2) B2"
    mixed = docx(item("N", "1") + item("x", "3") + item("y", "3"), numbering=lists)
    assert body(convert(mixed, "m.docx").markdown) == "1. N\n\n- x\n- y"


def test_numbering_starts_where_the_document_says():
    styles = (
        '<text:list-style style:name="L1"><text:list-level-style-number text:level="1"/>'
        "</text:list-style>"
    )
    li = "<text:list-item><text:p>{}</text:p></text:list-item>"
    odt = (
        f'<text:list text:style-name="L1">{li.format("one")}{li.format("two")}</text:list>'
        "<text:p>Pause.</text:p>"
        f'<text:list text:style-name="L1" text:continue-numbering="true">{li.format("three")}</text:list>'
        '<text:list text:style-name="L1"><text:list-item text:start-value="7"><text:p>seven</text:p>'
        "</text:list-item></text:list>"
    )
    assert body(convert(odf("odt", odt, styles), "n.odt").markdown).split("\n\n") == [
        "1. one\n2. two",
        "Pause.",
        "3. three",
        "7) seven",
    ]
    auto = (
        '<p:sp><p:nvSpPr><p:cNvPr id="1" name="s"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/>'
        '<p:txBody><a:p><a:pPr><a:buAutoNum type="arabicPeriod" startAt="5"/></a:pPr>'
        '<a:r><a:t>fünf</a:t></a:r></a:p><a:p><a:pPr><a:buAutoNum type="arabicPeriod" '
        'startAt="5"/></a:pPr><a:r><a:t>sechs</a:t></a:r></a:p></p:txBody></p:sp>'
    )
    assert "5. fünf\n6. sechs" in convert(pptx([auto]), "d.pptx").markdown


# -- hidden and deleted content


def test_hidden_sheets_slides_shapes_and_elements_stay_out():
    sheet = '<row r="1"><c r="A1" t="inlineStr"><is><t>{}</t></is></c><c r="B1"><v>1</v></c></row>'
    data = rezip(
        xlsx({"Sichtbar": sheet.format("offen"), "Geheim": sheet.format("hunter2")}, []),
        "xl/workbook.xml",
        lambda xml: xml.replace('<sheet name="Geheim"', '<sheet state="veryHidden" name="Geheim"'),
    )
    assert "hunter2" not in convert(data, "h.xlsx").markdown
    hidden_shape = shape(None, [(0, "sichtbar")]) + shape(None, [(0, "versteckt")]).replace(
        '<p:cNvPr id="1" name="s"/>', '<p:cNvPr id="2" name="t" hidden="1"/>'
    )
    deck = pptx([hidden_shape, shape(None, [(0, "Folie zwei")])])
    text = convert(deck, "h.pptx").markdown
    assert "sichtbar" in text and "versteckt" not in text
    assert body(convert(b"<p>Sichtbar <span hidden>GEHEIM</span> Text</p>", "a.html").markdown) == (
        "Sichtbar Text"
    )
    odt = '<text:p>offen <text:hidden-text text:string-value="geheim"/>weiter</text:p>'
    assert "geheim" not in convert(odf("odt", odt), "h.odt").markdown


def test_text_hidden_by_a_word_style_stays_out():
    styles = (
        '<w:style w:type="character" w:styleId="Geheim"><w:name w:val="Geheim"/>'
        "<w:rPr><w:vanish/></w:rPr></w:style>"
    )
    document = (
        '<w:p><w:r><w:t xml:space="preserve">offen </w:t></w:r>'
        '<w:r><w:rPr><w:rStyle w:val="Geheim"/></w:rPr><w:t>verborgen</w:t></w:r></w:p>'
    )
    assert body(convert(docx(document, styles=styles), "s.docx").markdown) == "offen"


def test_rtf_hidden_text_ends_where_it_ends():
    rtf = (
        "{"
        + BS
        + "rtf1 sichtbar "
        + BS
        + "v versteckt"
        + BS
        + "v0  wieder da"
        + BS
        + "par weiter"
        + BS
        + "par}"
    )
    assert body(convert(rtf.encode(), "v.rtf").markdown) == "sichtbar wieder da\n\nweiter"


# -- characters


def test_rtf_emoji_and_per_font_code_pages(tmp_path):
    emoji = BS + "u" + "-10179?" + BS + "u" + "-8704?"
    rtf = (
        "{" + BS + "rtf1" + BS + "ansi{" + BS + "fonttbl{" + BS + "f0" + BS + "fcharset0 Arial;}"
        "{"
        + BS
        + "f1"
        + BS
        + "fcharset204 Arial Cyr;}}"
        + "Hallo "
        + emoji
        + " und {"
        + BS
        + "f1 "
        + BS
        + "'cf"
        + BS
        + "'f0"
        + BS
        + "'e8}"
        + BS
        + "par}"
    )
    source = tmp_path / "in" / "e.rtf"
    source.parent.mkdir()
    source.write_bytes(rtf.encode("latin-1"))
    result = markdown.export([source.parent], tmp_path / "out")
    assert result.ok
    text = (tmp_path / "out" / "e.rtf.md").read_text(encoding="utf-8")
    assert "Hallo " + chr(0x1F600) + " und При" in text


def test_latin1_labelled_web_text_is_read_as_windows_1252():
    page = '<meta charset="iso-8859-1"><title>\x93Titel\x94</title><p>Er sagt \x93hallo\x94.</p>'
    note = convert(page.encode("latin-1"), "w.html")
    assert note.meta["title"] == "“Titel”"
    assert "Er sagt “hallo”." in note.markdown
    assert not any(0x80 <= ord(c) <= 0x9F for c in note.markdown)


def test_a_file_name_that_is_not_utf8_does_not_end_the_run(tmp_path):
    if sys.platform == "win32" or sys.platform == "darwin":
        pytest.skip("the file system insists on valid names")
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "b.txt").write_text("B")
    os.close(os.open(os.fsencode(folder) + b"/Bericht_M\xe4rz.txt", os.O_CREAT | os.O_WRONLY))
    result = markdown.export([folder], tmp_path / "out")
    assert len(result.written) == 2 and result.ok


# -- the folder of notes


def test_a_note_too_long_for_its_name_is_shortened_and_nothing_else_stops(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / ("L" * 240 + ".txt")).write_text("lang")
    (folder / "z.txt").write_text("z")
    (folder / "doc.txt").write_text("doc")
    (tmp_path / "out" / "doc.txt.md").mkdir(parents=True)
    result = markdown.export([folder], tmp_path / "out")
    names = sorted(p.name for _, p in result.written)
    assert "z.txt.md" in names and any(n.startswith("LLLL") and "~" in n for n in names)
    assert [p.name for p, _ in result.kept] == ["doc.txt.md"]


def test_a_link_in_one_folder_and_its_target_in_another_are_two_notes(tmp_path):
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "shared.txt").write_text("geteilt")
    try:
        (tmp_path / "A" / "link.txt").symlink_to(tmp_path / "B" / "shared.txt")
    except OSError:  # pragma: no cover - no symbolic links here
        pytest.skip("no symbolic links")
    out = tmp_path / "out"
    markdown.export([tmp_path / "A"], out)
    markdown.export([tmp_path / "B"], out)
    assert (out / "link.txt.md").exists() and (out / "shared.txt.md").exists()


def picture_docx() -> bytes:
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 3000
    return docx(
        para("Plan") + f'<w:p><w:r><w:drawing><a:blip {A} r:embed="rId9"/></w:drawing></w:r></w:p>',
        relations=(("rId9", "image", "media/image1.png"),),
        extra={"word/media/image1.png": png},
    )


def test_an_edited_note_keeps_its_pictures(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    source = folder / "r.docx"
    source.write_bytes(picture_docx())
    out = tmp_path / "out"
    markdown.export([folder], out, assets=True, ocr=False)
    note = out / "r.docx.md"
    note.write_text(note.read_text() + "\nmeine Ergänzung\n")
    asset = out / "_assets" / "r.docx" / "image1.png"
    source.write_bytes(picture_docx().replace(b"Plan", b"Neu!"))
    markdown.export([folder], out, assets=True, ocr=False)
    assert asset.exists()
    source.unlink()
    markdown.export([folder], out, assets=True, ocr=False, prune=True)
    assert asset.exists() and note.exists()


def test_a_run_killed_after_writing_a_note_still_owns_it(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "d.txt").write_text("eins")
    script = (
        "import os, signal\n"
        "from ocrust import markdown\n"
        "def stop(event, *_):\n"
        "    if event == 'written':\n"
        "        os.kill(os.getpid(), signal.SIGKILL)\n"
        f"markdown.export([{str(folder)!r}], {str(tmp_path / 'out')!r}, progress=stop)\n"
    )
    if not hasattr(signal, "SIGKILL"):  # pragma: no cover - Windows
        pytest.skip("no SIGKILL")
    subprocess.run([sys.executable, "-c", script], check=False)
    (folder / "d.txt").write_text("zwei")
    result = markdown.export([folder], tmp_path / "out")
    assert not result.kept
    assert "zwei" in (tmp_path / "out" / "d.txt.md").read_text()


def test_a_renamed_folder_takes_its_notes_along(tmp_path):
    (tmp_path / "Archiv").mkdir()
    (tmp_path / "Archiv" / "x.txt").write_text("v1")
    out = tmp_path / "out"
    markdown.export([tmp_path / "Archiv"], out)
    (tmp_path / "Archiv").rename(tmp_path / "Archive")
    (tmp_path / "Archive" / "x.txt").write_text("v2")
    result = markdown.export([tmp_path / "Archive"], out, prune=True)
    assert not result.kept and "v2" in (out / "x.txt.md").read_text()


def test_force_does_not_take_another_documents_note(tmp_path):
    for name in ("A", "B"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "x.txt").write_text(f"Inhalt {name}")
    out = tmp_path / "out"
    markdown.export([tmp_path / "A"], out)
    forced = markdown.export([tmp_path / "B"], out, force=True)
    assert forced.kept and "Inhalt A" in (out / "x.txt.md").read_text()


def test_a_dry_run_writes_nothing_and_says_what_it_would_keep(tmp_path, capsys):
    source = tmp_path / "d.txt"
    source.write_text("neu")
    target = tmp_path / "d.md"
    target.write_text("MEINE DATEI")
    assert main(["md", str(source), "-o", str(target), "-n"]) == 0
    assert target.read_text() == "MEINE DATEI"
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "e.txt").write_text("e")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "e.txt.md").write_text("fremd")
    result = markdown.export([folder], tmp_path / "out", dry_run=True)
    assert not result.written and [p.name for p, _ in result.kept] == ["e.txt.md"]
    assert not (tmp_path / "out" / ".ocrust").exists()


def test_names_that_differ_only_in_case_clash(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "Bericht.txt").write_text("a")
    (tmp_path / "in" / "bericht.txt").write_text("b")
    if len(list((tmp_path / "in").iterdir())) < 2:
        pytest.skip("a file system that folds case")
    result = markdown.export([tmp_path / "in"], tmp_path / "out")
    assert len(result.failed) == 1 and "rename one" in result.failed[0][1]


def test_an_archive_member_that_fails_is_named_and_tried_again(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "p.zip").write_bytes(package({"ok.txt": "gut", "kaputt.docx": b"kein zip"}))
    events: list[str] = []
    first = markdown.export(
        [tmp_path / "in"], tmp_path / "out", progress=lambda e, *_: events.append(e)
    )
    assert "failed" in events and "kaputt.docx" in first.failed[0][1]
    assert markdown.export([tmp_path / "in"], tmp_path / "out").failed


# -- hostile input


def test_huge_repeat_counts_and_spans_are_bounded():
    cell = (
        '<table:table-cell table:number-columns-repeated="1000000"><text:p>x</text:p>'
        "</table:table-cell>"
    )
    rows = f'<table:table-row table:number-rows-repeated="1000000">{cell}</table:table-row>'
    sheets = "".join(f'<table:table table:name="S{i}">{rows}</table:table>' for i in range(10))
    started = time.monotonic()
    note = convert(odf("ods", sheets), "bomb.ods")
    assert time.monotonic() - started < 30 and note.meta["truncated"] is True
    span = '<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="30000000"/></w:tcPr>'
    started = time.monotonic()
    convert(docx(span + para("a") + "</w:tc></w:tr></w:tbl>"), "s.docx")
    lists = numbering(("1", "1", ""))
    convert(docx(item("x", "1", 10_000_000), numbering=lists), "l.docx")
    assert time.monotonic() - started < 5


def test_long_inputs_are_joined_in_linear_time():
    started = time.monotonic()
    convert(("wort " * 600_000).encode(), "lang.txt")
    rows = "".join(
        f'<row r="{r}"><c r="A{r}"><v>{r}</v></c><c r="B{r}"><v>{r}</v></c></row>'
        for r in range(1, 20_001)
    )
    convert(xlsx({"S": rows}, []), "gross.xlsx")
    assert time.monotonic() - started < 15


def test_what_breaks_a_reader_is_a_conversion_error():
    with pytest.raises(markdown.ConversionError, match="nested too deeply"):
        convert(("<table><tr><td>" * 2000).encode(), "tief.html")
    big = ("a,b\n" + "x" * 200_000 + ",y\n").encode()
    assert "yyy" not in convert(big, "gross.csv").markdown
    raw = b"Subject: Test\r\nMessage-ID: <<<>>>\r\nContent-Type: text/plain\r\n\r\nInhalt\r\n"
    assert "Inhalt" in convert(raw, "m.eml").markdown


# -- structure


def test_html_data_tables_articles_and_preformatted_text():
    table = (
        "<table><tr><th>Produkt</th><th>Umfang</th></tr><tr><td>Basis</td>"
        "<td><ul><li>Mail</li><li>Chat</li></ul></td></tr><tr><td>Pro</td><td>Telefon</td></tr></table>"
    )
    assert "| Basis | • Mail<br>• Chat |" in convert(table.encode(), "t.html").markdown
    page = "<body><h1>Titel</h1><p>Der Inhalt.</p><aside><article><p>Anderes</p></article></aside></body>"
    assert "# Titel" in convert(page.encode(), "a.html").markdown
    assert "zeile1\nzeile2" in convert(b"<pre>zeile1<br>zeile2</pre>", "p.html").markdown


def test_markdown_inside_archives_and_mails_keeps_its_text():
    archive = package({"readme.md": "# Projekt\n\nWichtig.\n"})
    assert "Wichtig." in convert(archive, "a.zip").markdown
    mail = EmailMessage()
    mail["Subject"] = "Spez"
    mail.set_content("Anbei.")
    mail.add_attachment(
        b"# Spez\n\nDas System soll X.\n", maintype="text", subtype="markdown", filename="s.md"
    )
    assert "Das System soll X." in convert(bytes(mail), "m.eml").markdown
    assert markdown.convert(b"# Hallo\n\nWelt\n", name="n.md").body == "# Hallo\n\nWelt\n"


def test_footnotes_of_two_attachments_stay_two():
    def with_note(text: str) -> bytes:
        notes = (
            '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:footnote w:id="1">{para(text)}</w:footnote></w:footnotes>'
        )
        return docx(
            '<w:p><w:r><w:t>Vertrag</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r></w:p>',
            extra={"word/footnotes.xml": notes},
        )

    mail = EmailMessage()
    mail["Subject"] = "Zwei"
    mail.set_content("Anbei.")
    for name, text in (("a.docx", "Fußnote A"), ("b.docx", "Fußnote B")):
        mail.add_attachment(
            with_note(text), maintype="application", subtype="octet-stream", filename=name
        )
    text = convert(bytes(mail), "m.eml").markdown
    labels = [line.split("]:")[0] for line in text.splitlines() if line.startswith("[^")]
    assert len(labels) == 2 and len(set(labels)) == 2


def test_an_epub_with_encoded_paths_and_slides_with_grouped_shapes():
    container = (
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="b.opf"/></rootfiles></container>'
    )
    opf = (
        '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
        '<item id="c" href="Kapitel%201.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c"/></spine></package>'
    )
    book = package(
        {
            "META-INF/container.xml": container,
            "b.opf": opf,
            "Kapitel 1.xhtml": "<html><body><h1>Eins</h1><p>Text.</p></body></html>",
        }
    )
    assert convert(book, "b.epub").meta["chapters"] == 1
    slide = (
        '<draw:page draw:name="S1"><draw:frame presentation:class="title"><draw:text-box>'
        "<text:p>Titel</text:p></draw:text-box></draw:frame><draw:g><draw:custom-shape>"
        "<text:p>In der Gruppe</text:p></draw:custom-shape></draw:g><draw:rect>"
        "<text:p>Im Rechteck</text:p></draw:rect></draw:page>"
    )
    text = convert(odf("odp", slide), "p.odp").markdown
    assert "## Titel" in text and "In der Gruppe" in text and "Im Rechteck" in text


def test_spreadsheet_rows_repeated_are_rows_and_durations_are_durations():
    row = '<table:table-row table:number-rows-repeated="1500"><table:table-cell><text:p>w</text:p></table:table-cell><table:table-cell><text:p>1</text:p></table:table-cell></table:table-row>'
    note = convert(
        odf("ods", f'<table:table table:name="T">{row}</table:table>'), "r.ods", max_rows=None
    )
    # 1500 rows: the first is the table's header.
    assert note.markdown.count("| w | 1 |") == 1500
    styles = '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="46"/></cellXfs>'
    sheet = '<row r="1"><c r="A1" t="inlineStr"><is><t>Dauer</t></is></c><c r="B1" s="1"><v>1.5</v></c></row>'
    sheet += '<row r="2"><c r="A2" t="inlineStr"><is><t>x</t></is></c><c r="B2"><v>2</v></c></row>'
    assert "| Dauer | 36:00 |" in convert(xlsx({"S": sheet}, [], styles), "d.xlsx").markdown


def test_a_word_row_that_starts_further_right():
    cell = "<w:tc>{}</w:tc>"
    table = (
        "<w:tbl><w:tr>"
        + cell.format(para("Name"))
        + cell.format(para("Q1"))
        + cell.format(para("Q2"))
        + '</w:tr><w:tr><w:trPr><w:gridBefore w:val="1"/></w:trPr>'
        + cell.format(para("10"))
        + cell.format(para("20"))
        + "</w:tr></w:tbl>"
    )
    assert "|  | 10 | 20 |" in convert(docx(table), "g.docx").markdown


# -- writing


def test_escaping_gaps():
    link = _ir.Span("link", ["a]b"], "https://x")
    assert _render.inline([link]) == "[a\\]b](https://x)"
    assert _render.inline([_ir.Span("strong", ["Hinweis:"]), "Text"]) == "**Hinweis**:Text"
    assert _render.inline([_ir.Span("strong", ["Notes:"])]) == "**Notes:**"
    assert _render.blocks([_ir.Heading(2, ["#"])]) == "## \\#"
    image = _ir.Image(alt="Plan [1]", target="Sitzung #3/100% fertig?.png")
    assert _render.blocks([image]) == "![Plan \\[1\\]](Sitzung%20%233/100%25%20fertig%3F.png)"


def test_text_files_their_quotes_code_and_subtitles():
    assert body(convert(b"> Frage eins\n> Frage zwei\nMeine Antwort\n", "a.txt").markdown) == (
        "> Frage eins Frage zwei\n\nMeine Antwort"
    )
    assert (
        "```\nmake\nmake install\n```"
        in convert(b"Schritte:\n\n\tmake\n\tmake install\n", "a.txt").markdown
    )
    vtt = b"WEBVTT\n\nSTYLE\n::cue { color: red }\n\nNOTE eins\nzwei\n\nid-1\n00:00:01.000 --> 00:00:02.000\nHallo\n"
    assert body(convert(vtt, "a.vtt").markdown) == "Hallo"
    raw = b'{"preis":19.90,"menge":1E3,"x":"a","x":"b","p":"' + b"y" * 100 + b'"}'
    text = convert(raw, "o.json").markdown
    assert '"preis": 19.90' in text and '"menge": 1E3' in text and text.count('"x"') == 2
    assert "\r" not in convert((chr(0xFEFF) + "# H\r\n\r\nText\r\n").encode(), "n.md").markdown


def test_pictures_in_table_cells_are_linked():
    table = _ir.Table([[["a"], ["b"]], [_ir.flatten([_ir.Image("Plan", "_assets/p.png")]), ["c"]]])
    assert "![Plan](_assets/p.png)" in _render.blocks([table])


def test_duplicate_archive_members_are_one(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a/b.txt", "erste")
        archive.writestr("a/./b.txt", "zweite")
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "z.zip").write_bytes(buffer.getvalue())
    result = markdown.export([tmp_path / "in"], tmp_path / "out")
    assert len(result.written) == 1 and not result.kept


# -- PDFs without the model files


def test_without_ocr_a_pdf_is_read_from_its_text_and_a_scan_stays_empty(
    table_pdf, tmp_path, monkeypatch
):
    from conftest import _pdf_from_stream

    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))
    note = markdown.convert(table_pdf, ocr=False)
    assert "| Widget A | 12 | 49,90 | 598,80 |" in note.markdown
    assert note.meta["extraction"] == "text"
    # A page without any text drawn on it is what a scan looks like.
    empty = tmp_path / "scan.pdf"
    empty.write_bytes(_pdf_from_stream(""))
    note = markdown.convert(empty, ocr=False)
    assert body(note.markdown) == "<!-- page 1 -->"
    assert note.meta["pages"] == 1 and note.meta["unread_pages"] == [1]
    assert "\nunread_pages: [1]\n" in note.markdown
