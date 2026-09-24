"""Regression tests for what an independent review of `ocrust markdown` found.

Each test names the case it is about; every one of them failed before its fix.
"""

from __future__ import annotations

import io
import os
import re
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
    (folder / "b.txt").write_text("B", encoding="utf-8")
    os.close(os.open(os.fsencode(folder) + b"/Bericht_M\xe4rz.txt", os.O_CREAT | os.O_WRONLY))
    result = markdown.export([folder], tmp_path / "out")
    assert len(result.written) == 2 and result.ok


# -- the folder of notes


def test_a_note_too_long_for_its_name_is_shortened_and_nothing_else_stops(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / ("L" * 240 + ".txt")).write_text("lang", encoding="utf-8")
    (folder / "z.txt").write_text("z", encoding="utf-8")
    (folder / "doc.txt").write_text("doc", encoding="utf-8")
    (tmp_path / "out" / "doc.txt.md").mkdir(parents=True)
    result = markdown.export([folder], tmp_path / "out")
    names = sorted(p.name for _, p in result.written)
    assert "z.txt.md" in names and any(n.startswith("LLLL") and "~" in n for n in names)
    assert [p.name for p, _ in result.kept] == ["doc.txt.md"]


def test_a_link_in_one_folder_and_its_target_in_another_are_two_notes(tmp_path):
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "shared.txt").write_text("geteilt", encoding="utf-8")
    try:
        (tmp_path / "A" / "link.txt").symlink_to(tmp_path / "B" / "shared.txt")
    except OSError:  # pragma: no cover - no symbolic links here
        pytest.skip("no symbolic links")
    out = tmp_path / "out"
    markdown.export([tmp_path / "A"], out)
    markdown.export([tmp_path / "B"], out)
    assert (out / "link.txt.md").exists() and (out / "shared.txt.md").exists()


def picture_docx(png: bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 3000) -> bytes:
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
    note.write_text(note.read_text(encoding="utf-8") + "\nmeine Ergänzung\n", encoding="utf-8")
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
    (folder / "d.txt").write_text("eins", encoding="utf-8")
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
    (folder / "d.txt").write_text("zwei", encoding="utf-8")
    result = markdown.export([folder], tmp_path / "out")
    assert not result.kept
    assert "zwei" in (tmp_path / "out" / "d.txt.md").read_text(encoding="utf-8")


def test_a_renamed_folder_takes_its_notes_along(tmp_path):
    (tmp_path / "Archiv").mkdir()
    (tmp_path / "Archiv" / "x.txt").write_text("v1", encoding="utf-8")
    out = tmp_path / "out"
    markdown.export([tmp_path / "Archiv"], out)
    (tmp_path / "Archiv").rename(tmp_path / "Archive")
    (tmp_path / "Archive" / "x.txt").write_text("v2", encoding="utf-8")
    result = markdown.export([tmp_path / "Archive"], out, prune=True)
    assert not result.kept and "v2" in (out / "x.txt.md").read_text(encoding="utf-8")


def test_force_does_not_take_another_documents_note(tmp_path):
    for name in ("A", "B"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "x.txt").write_text(f"Inhalt {name}", encoding="utf-8")
    out = tmp_path / "out"
    markdown.export([tmp_path / "A"], out)
    forced = markdown.export([tmp_path / "B"], out, force=True)
    assert forced.kept and "Inhalt A" in (out / "x.txt.md").read_text(encoding="utf-8")


def test_a_dry_run_writes_nothing_and_says_what_it_would_keep(tmp_path, capsys):
    source = tmp_path / "d.txt"
    source.write_text("neu", encoding="utf-8")
    target = tmp_path / "d.md"
    target.write_text("MEINE DATEI", encoding="utf-8")
    assert main(["md", str(source), "-o", str(target), "-n"]) == 0
    assert target.read_text(encoding="utf-8") == "MEINE DATEI"
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "e.txt").write_text("e", encoding="utf-8")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "e.txt.md").write_text("fremd", encoding="utf-8")
    result = markdown.export([folder], tmp_path / "out", dry_run=True)
    assert not result.written and [p.name for p, _ in result.kept] == ["e.txt.md"]
    assert not (tmp_path / "out" / ".ocrust").exists()


def test_names_that_differ_only_in_case_clash(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "Bericht.txt").write_text("a", encoding="utf-8")
    (tmp_path / "in" / "bericht.txt").write_text("b", encoding="utf-8")
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


def test_what_breaks_a_reader_is_a_conversion_error(monkeypatch):
    def deep(data: bytes, ctx: object) -> None:
        raise RecursionError

    monkeypatch.setitem(markdown._READERS, ".rtf", deep)
    with pytest.raises(markdown.ConversionError, match="nested too deeply"):
        convert(b"{\\rtf1 x}", "tief.rtf")
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


# -- second review round: the folder of notes


def test_force_and_prune_never_delete_a_note_edited_by_hand(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a.txt").write_text("alpha", encoding="utf-8")
    out = tmp_path / "out"
    markdown.export([folder], out)
    note = out / "a.txt.md"
    note.write_text(note.read_text(encoding="utf-8") + "\nWICHTIG\n", encoding="utf-8")
    (folder / "a.txt").unlink()
    preview = markdown.export([folder], out, prune=True, force=True, dry_run=True)
    assert not preview.pruned and [p for p, _ in preview.kept] == [note]
    result = markdown.export([folder], out, prune=True, force=True)
    assert not result.pruned and "WICHTIG" in note.read_text(encoding="utf-8")


def test_an_archive_member_that_fails_keeps_its_last_note(tmp_path):
    good = docx(para("Vertrag mit Kunde A"))

    def archive(member: bytes) -> bytes:
        return package({"a.txt": "alpha", "b.docx": member})

    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "p.zip").write_bytes(archive(good))
    out = tmp_path / "out"
    markdown.export([tmp_path / "in"], out, ocr=False)
    (tmp_path / "in" / "p.zip").write_bytes(archive(good[:200]))
    result = markdown.export([tmp_path / "in"], out, ocr=False)
    assert result.failed
    assert "Vertrag" in (out / "p.zip" / "b.docx.md").read_text(encoding="utf-8")
    # Mended, it is written again.
    (tmp_path / "in" / "p.zip").write_bytes(archive(docx(para("Vertrag, neu"))))
    markdown.export([tmp_path / "in"], out, ocr=False)
    assert "neu" in (out / "p.zip" / "b.docx.md").read_text(encoding="utf-8")


def test_a_kept_note_does_not_make_every_run_convert_again(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "v.txt").write_text("eins", encoding="utf-8")
    out = tmp_path / "out"
    markdown.export([folder], out)
    note = out / "v.txt.md"
    note.write_text(note.read_text(encoding="utf-8") + "\nAnmerkung\n", encoding="utf-8")
    (folder / "v.txt").write_text("zwei", encoding="utf-8")
    assert markdown.export([folder], out).kept
    again = markdown.export([folder], out)
    assert again.unchanged == [folder / "v.txt"] and not again.written
    # Once the note is let go, the document is written again.
    note.unlink()
    assert markdown.export([folder], out).written
    assert "zwei" in note.read_text(encoding="utf-8")


def test_a_run_killed_after_writing_a_picture_still_owns_it(tmp_path):
    if not hasattr(signal, "SIGKILL"):  # pragma: no cover - Windows
        pytest.skip("no SIGKILL")
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "r.docx").write_bytes(picture_docx())
    out = tmp_path / "out"
    script = (
        "import os, signal\n"
        "from ocrust.markdown import _store\n"
        "write = _store._write\n"
        "def stop(path, data):\n"
        "    write(path, data)\n"
        "    if path.suffix == '.png':\n"
        "        os.kill(os.getpid(), signal.SIGKILL)\n"
        "_store._write = stop\n"
        f"_store.export([{str(folder)!r}], {str(out)!r}, assets=True, ocr=False)\n"
    )
    subprocess.run([sys.executable, "-c", script], check=False)
    markdown.export([folder], out, assets=True, ocr=False)
    new = b"\x89PNG\r\n\x1a\n" + b"2" * 3000
    (folder / "r.docx").write_bytes(picture_docx(new))
    markdown.export([folder], out, assets=True, ocr=False)
    asset = out / "_assets" / "r.docx" / "image1.png"
    assert asset.read_bytes() == new
    (folder / "r.docx").unlink()
    markdown.export([folder], out, assets=True, ocr=False, prune=True)
    assert not asset.exists()


def test_a_case_twin_does_not_take_the_note_of_the_document_that_has_it(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "report.txt").write_text("geprüft", encoding="utf-8")
    out = tmp_path / "out"
    markdown.export([folder], out)
    (folder / "REPORT.txt").write_text("Entwurf", encoding="utf-8")
    if (folder / "report.txt").read_text(encoding="utf-8") != "geprüft":
        pytest.skip("names differing in case are one file here")
    result = markdown.export([folder], out, prune=True)
    assert [p.name for p, _ in result.failed] == ["REPORT.txt"]
    assert sorted(p.name for p in out.glob("*.md")) == ["report.txt.md"]


def test_an_output_folder_that_cannot_be_made_is_reported(tmp_path, capsys):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "file").write_text("x", encoding="utf-8")
    assert main(["markdown", str(tmp_path / "in"), "-o", str(tmp_path / "file" / "kb")]) == 1
    assert "cannot create" in capsys.readouterr().err
    with pytest.raises(markdown.ConversionError):
        markdown.export([tmp_path / "in"], tmp_path / "file" / "kb")


def test_a_lock_is_taken_over_only_from_a_process_that_is_gone():
    from ocrust.markdown._store import _alive

    assert _alive(os.getpid())
    assert not _alive(999_999_999)


# -- second review round: what is read


def test_excel_numbers_keep_every_digit_a_sheet_keeps():
    rows = '<row r="1"><c r="A1" t="s"><v>0</v></c></row>' + "".join(
        f'<row r="{i}"><c r="A{i}"><v>{v}</v></c></row>'
        for i, v in enumerate(
            ["123456789.12", "9876543210.55", "3.14159265358979", "1234567890123456"], 2
        )
    )
    text = convert(xlsx({"Bilanz": rows}, ["Betrag"]), "b.xlsx").markdown
    for value in ("123456789.12", "9876543210.55", "3.14159265358979", "1234567890123456"):
        assert f"\n{value}\n" in text
    rows = '<row r="1"><c r="A1"><v>0.30000000000000004</v></c></row>'
    assert "\n0.3\n" in convert(xlsx({"S": rows}, []), "f.xlsx").markdown


def test_excel_escapes_are_the_characters_they_stand_for():
    rows = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>Lieferung_x000D_\nam Montag</t></is></c></row>'
        '<row r="3"><c r="A3" t="s"><v>1</v></c></row>'
    )
    text = convert(xlsx({"S": rows}, ["Anmerkung", "a_x0009_b _x005F_x000D_"]), "e.xlsx").markdown
    assert "Lieferung am Montag" in text and "a b \\_x000D\\_" in text


def test_one_byte_that_is_not_utf8_leaves_the_rest_of_the_file_alone():
    data = "Müller;München\n".encode() + "Café;Köln\n".encode("cp1252")
    text = convert(data, "k.csv").markdown
    assert "Müller" in text and "Café" in text and "Ã" not in text


def test_a_log_keeps_a_line_for_each_record():
    log = "".join(
        f"2026-01-{d:02d} 12:00 INFO backup of server{d:02d} finished ok\n" for d in range(1, 9)
    )
    names = "Teilnehmer:\n" + "".join(f"Name {i}, Firma {chr(65 + i)}\n" for i in range(6))
    assert len(body(convert(log.encode(), "b.txt").markdown).splitlines()) == 8
    assert len(body(convert(names.encode(), "t.txt").markdown).splitlines()) == 7


def test_code_styled_paragraphs_keep_their_indentation():
    styles = (
        '<w:style w:type="paragraph" w:styleId="SourceCode"><w:name w:val="Source Code"/></w:style>'
    )
    lines = ["def total(items):", "    s = 0", "    for i in items:", "        s += i"]
    word = docx("".join(para(line, "SourceCode") for line in lines), styles=styles)
    assert "```\n" + "\n".join(lines) + "\n```" in convert(word, "c.docx").markdown
    content = (
        f"<office:document-content {ODF_NS}><office:styles>"
        '<style:style style:name="Pre" style:display-name="Preformatted Text" style:family="paragraph"/>'
        "</office:styles><office:body><office:text>"
        '<text:p text:style-name="Pre">if x:</text:p>'
        '<text:p text:style-name="Pre"><text:s text:c="4"/>y()</text:p>'
        "</office:text></office:body></office:document-content>"
    )
    odt = package({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": content})
    assert "```\nif x:\n    y()\n```" in convert(odt, "c.odt").markdown


def test_archive_members_differing_in_case_are_both_kept():
    archive = package({"Protokoll.txt": "Montag", "protokoll.txt": "Dienstag"})
    text = convert(archive, "p.zip").markdown
    assert "Montag" in text and "Dienstag" in text and "protokoll~2.txt" in text


def test_pictures_differing_in_case_are_kept_under_two_names(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"A" * 3000)
    (tmp_path / "b" / "Logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"B" * 3000)
    page = tmp_path / "p.html"
    page.write_text(
        '<img src="a/logo.png" alt="A"><img src="b/Logo.png" alt="B">', encoding="utf-8"
    )
    assets = markdown.convert(page, assets=True, ocr=False).assets
    assert len({name.casefold() for name in assets}) == 2


def test_a_figure_along_the_page_edges_is_text_and_a_page_number_is_not():
    from conftest import _pdf_from_streams, _winansi_literal

    def page(lines: list[tuple[str, int, int]]) -> str:
        shown = (f"/F1 11 Tf 1 0 0 1 {x} {y} Tm ({_winansi_literal(t)}) Tj" for t, x, y in lines)
        return "BT\n" + "\n".join(shown) + "\nET"

    balances = ["1523.10", "2210.55", "987.40"]
    streams = [
        page(
            [
                (f"Buchung {n}: Lastschrift Stadtwerke", 60, 700),
                (f"Kontostand am Seitenende: {balance} EUR", 60, 60),
                (f"Seite {n + 1} von 3", 480, 30),
            ]
        )
        for n, balance in enumerate(balances)
    ]
    text = markdown.convert(_pdf_from_streams(streams), name="konto.pdf", ocr=False).markdown
    assert all(balance in text for balance in balances)
    assert "Seite 1 von 3" not in text


def test_a_page_of_a_thousand_unclosed_tags_is_read():
    lines = "".join(f"<font color=red>Fehler {i}<br>" for i in range(1200))
    text = convert(f"<html><body>{lines}</body></html>".encode(), "log.html").markdown
    assert "Fehler 0" in text and "Fehler 1199" in text


def test_a_picture_named_pdf_without_ocr_is_an_empty_note():
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    assert markdown.convert(png, name="scan.pdf", ocr=False).meta["extraction"] == "none"


# -- second review round: writing


def test_emphasis_at_punctuation_next_to_a_letter():
    nested = _ir.Span("strong", [_ir.Span("emph", ["Wichtig:"])])
    assert _render.inline([nested, "Die Frist"]) == "***Wichtig***:Die Frist"
    spaced = _ir.Span("strong", ["Remarque :"])
    assert _render.inline([spaced, "le délai"]) == "**Remarque** :le délai"
    both = _ir.Span("strong", [_ir.Span("emph", ["(netto)"])])
    assert _render.inline(["Betrag", both, "ist"]) == "Betrag(***netto***)ist"


def test_neighbouring_markup_stays_apart():
    link = _ir.Span("link", ["Jetzt bestellen"], "https://shop.example")
    assert _render.inline(["Neu!", link]) == "Neu\\![Jetzt bestellen](https://shop.example)"
    keys = [_ir.Span("code", ["Strg"]), _ir.Span("code", ["C"])]
    assert _render.inline(keys) == "`StrgC`"
    cited = ["Urteil", _ir.FootnoteRef("1"), "(2019)"]
    assert _render.inline(cited) == "Urteil[^1]\\(2019)"


# -- second review round: PDFs read from their own text


def _text_stream(lines: list[tuple[str, float, float]], size: int = 11, mode: str = "") -> str:
    from conftest import _winansi_literal

    shown = (f"/F1 {size} Tf 1 0 0 1 {x} {y} Tm ({_winansi_literal(t)}) Tj" for t, x, y in lines)
    return f"BT {mode}\n" + "\n".join(shown) + "\nET"


def test_a_form_drawn_labels_first_keeps_its_values_beside_them(monkeypatch, tmp_path):
    from conftest import _pdf_from_stream

    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))
    pairs = [("Rechnungsnummer:", "RE-2026-0042"), ("Kundennummer:", "K-10077")]
    pairs += [("Zahlungsziel:", "14 Tage netto"), ("Betrag:", "1.299,90 EUR")]
    template = _text_stream([(label, 72, 700 - i * 16) for i, (label, _) in enumerate(pairs)])
    data = _text_stream([(value, 190, 700 - i * 16) for i, (_, value) in enumerate(pairs)])
    text = markdown.convert(_pdf_from_stream(template + "\n" + data), name="r.pdf", ocr=False).body
    for label, value in pairs:
        assert any(label in line and value in line for line in text.splitlines()), text


def test_body_text_over_a_table_in_smaller_print_is_not_a_heading(monkeypatch, tmp_path):
    from conftest import _pdf_from_stream

    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))
    body_lines = ["The committee met on Monday to review", "the budget for the coming year."]
    stream = _text_stream([(t, 60, 740 - i * 14) for i, t in enumerate(body_lines)])
    rows = [(f"Item {i}", 60, 700 - i * 11) for i in range(12)]
    rows += [(f"{i * 10},00", 300, 700 - i * 11) for i in range(12)]
    stream += "\n" + _text_stream(rows, size=9)
    note = markdown.convert(_pdf_from_stream(stream), name="minutes.pdf", ocr=False)
    assert not [line for line in note.body.splitlines() if line.startswith("#")]
    assert note.meta["title"] == "minutes"


def test_without_ocr_an_invisible_layer_over_shown_text_is_not_read_twice(monkeypatch, tmp_path):
    from conftest import _pdf_from_stream

    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))
    lines = ["Der Vertrag beginnt am 1. April.", "Die Miete betraegt 850 EUR."]
    shown = _text_stream([(t, 72, 700 - i * 18) for i, t in enumerate(lines)], size=12)
    hidden = _text_stream([(t, 72.5, 700.6 - i * 18) for i, t in enumerate(lines)], mode="3 Tr")
    text = markdown.convert(_pdf_from_stream(shown + "\n" + hidden), name="v.pdf", ocr=False).body
    assert text.count("Der Vertrag beginnt") == 1


def test_without_ocr_a_font_without_unicode_leaves_the_page_unread(monkeypatch, tmp_path):
    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))
    names = " ".join(f"/g{i}" for i in range(65, 91))
    font = (
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        f"/Encoding << /Type /Encoding /Differences [65 {names}] >> >>"
    )
    stream = "BT /F1 14 Tf 72 700 Td (HELLO WORLD THIS IS TEXT) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        font,
    ]
    pdf, offsets = "%PDF-1.4\n", []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += f"{number} 0 obj\n{obj}\nendobj\n"
    xref = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    pdf += "".join(f"{offset:010} 00000 n \n" for offset in offsets)
    pdf += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    note = markdown.convert(pdf.encode("latin-1"), name="f.pdf", ocr=False)
    assert "�" not in note.markdown and note.meta["unread_pages"] == [1]


def test_scan_reads_a_pdf_from_its_text_alone_without_the_models(monkeypatch, tmp_path, capsys):
    from conftest import _pdf_with_text

    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(_pdf_with_text([("Hello text layer", 20)]))
    assert main(["scan", str(pdf), "--pdf-text", "only", "-q"]) == 0
    assert "Hello text layer" in capsys.readouterr().out


# -- third review round


def _pdf_of(lines: list[tuple[str, float, float, float]], mode: str = "") -> bytes:
    """One page of `(text, size, x, y)` lines."""
    from conftest import _pdf_from_stream, _winansi_literal

    shown = (f"/F1 {s} Tf 1 0 0 1 {x} {y} Tm ({_winansi_literal(t)}) Tj" for t, s, x, y in lines)
    return _pdf_from_stream(f"BT {mode}\n" + "\n".join(shown) + "\nET")


@pytest.fixture
def no_models(monkeypatch, tmp_path):
    monkeypatch.setenv("OCRUST_MODELS_DIR", str(tmp_path / "no-models"))


def test_a_letter_over_small_print_is_not_headings(no_models):
    letter = ["Sehr geehrte Frau Müller,", "vielen Dank für Ihre Bestellung vom 3. März."]
    letter += ["Die Ware wurde heute an Sie versandt.", "Mit freundlichen Grüßen"]
    lines = [(t, 11, 60, 740 - i * 24) for i, t in enumerate(letter)]
    small = "Es gelten unsere Allgemeinen Geschäftsbedingungen in der gültigen Fassung; " * 2
    lines += [(small, 6, 60, 600 - i * 8) for i in range(5)]
    note = markdown.convert(_pdf_of(lines), name="r.pdf", ocr=False)
    assert not [line for line in note.body.splitlines() if line.startswith("#")]


def test_a_watermark_across_a_scan_leaves_its_ocr_layer_alone(no_models):
    import math

    from conftest import _pdf_from_stream

    hidden = [
        f"/F1 11 Tf 1 0 0 1 72 {720 - i * 20} Tm (Zeile {i:02d} erkannt.) Tj" for i in range(20)
    ]
    turn = math.radians(45)
    cos, sin = math.cos(turn), math.sin(turn)
    stamp = (
        f"BT 0 Tr /F1 90 Tf {cos:.4f} {sin:.4f} {-sin:.4f} {cos:.4f} 120 150 Tm (VERTRAULICH) Tj ET"
    )
    pdf = _pdf_from_stream("BT 3 Tr\n" + "\n".join(hidden) + "\nET\n" + stamp)
    text = markdown.convert(pdf, name="s.pdf", ocr=False).markdown
    assert all(f"Zeile {i:02d}" in text for i in range(20))


def test_a_title_that_is_also_the_running_head_stays(no_models):
    from conftest import _pdf_from_streams, _winansi_literal

    pages = []
    for n in range(1, 4):
        lines = [("Projektbericht Nord", 9, 72, 770), (f"Seite {n}", 9, 480, 770)]
        if n == 1:
            lines.append(("Projektbericht Nord", 24, 72, 720))
        lines += [
            (f"Abschnitt {n} beginnt hier mit einem ganzen Satz Text.", 11, 72, 640 - i * 15)
            for i in range(10)
        ]
        shown = (
            f"/F1 {s} Tf 1 0 0 1 {x} {y} Tm ({_winansi_literal(t)}) Tj" for t, s, x, y in lines
        )
        pages.append("BT\n" + "\n".join(shown) + "\nET")
    note = markdown.convert(_pdf_from_streams(pages), name="b.pdf", ocr=False)
    assert "# Projektbericht Nord" in note.markdown
    assert note.markdown.count("Projektbericht Nord") == 2  # the heading, and the title


def test_a_plain_text_list_under_a_lead_in_is_a_list():
    text = "Offene Punkte:\n- Budget\n- Termin\n- Raum\n- Catering\n"
    assert "- Budget\n- Termin\n- Raum\n- Catering" in convert(text.encode(), "t.txt").markdown
    numbered = "Schritte:\n1. Oeffnen\n2. Pruefen\n3. Schliessen\n4. Melden\n"
    assert "1. Oeffnen\n2. Pruefen" in convert(numbered.encode(), "n.txt").markdown


def test_superscripts_stay_with_their_word(no_models):
    from conftest import _pdf_from_stream

    raised = "/F1 7 Tf 4.12 Ts (2) Tj 0 Ts /F1 10 Tf"
    stream = f"BT /F1 10 Tf 1 0 0 1 72 700 Tm (The energy is E = mc) Tj {raised} ( for a body at rest.) Tj ET"
    text = markdown.convert(_pdf_from_stream(stream), name="p.pdf", ocr=False).body
    assert "E = mc2 for a body at rest." in text


def test_two_footnote_references_side_by_side():
    assert _render.inline(["Wie festgestellt.", _ir.FootnoteRef("1"), _ir.FootnoteRef("2")]) == (
        "Wie festgestellt.[^1][^2]"
    )
    link = _ir.Span("link", ["Quelle"], "https://example.org")
    assert _render.inline(["x", _ir.FootnoteRef("1"), link]) == "x[^1][Quelle](https://example.org)"


def test_a_page_past_the_depth_cap_hides_its_scripts_and_keeps_its_tables():
    lines = "".join(f"<font color=navy>Zeile {i}<br>\n" for i in range(150))
    page = (
        f"<body>{lines}<script>var tracker = 'JS';</script><style>.x{{}} /* CSS */</style>"
        "<p hidden>VERSTECKT</p><table><tr><th>Name</th><th>Wert</th></tr>"
        "<tr><td>A</td><td>1</td></tr></table></body>"
    )
    text = convert(page.encode(), "p.html").markdown
    assert "tracker" not in text and "CSS" not in text and "VERSTECKT" not in text
    assert "| Name | Wert |" in text
    wrapped = "<font face=Arial><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table></font>"
    assert "| A | B |" in convert(wrapped.encode(), "f.html").markdown


def test_a_table_whose_cells_wrap_keeps_each_row_together(no_models):
    from conftest import _pdf_from_stream

    rows = [
        [["Aufgabe"], ["Verantwortlich"], ["Frist"]],
        [["Server migrieren und alte", "Maschinen abschalten"], ["Mueller"], ["30.06.2026"]],
        [["Monitoring einrichten"], ["Schmidt,", "Weber"], ["15.07.2026"]],
        [["Abnahme"], ["Kunde"], ["31.07.2026"]],
    ]
    shown, y = ["BT /F1 10 Tf"], 700
    for row in rows:
        for x, cell in zip([72, 300, 420], row, strict=True):
            shown += [f"1 0 0 1 {x} {y - i * 12} Tm ({line}) Tj" for i, line in enumerate(cell)]
        y -= 12 * max(len(cell) for cell in row) + 10
    text = markdown.convert(
        _pdf_from_stream("\n".join([*shown, "ET"])), name="a.pdf", ocr=False
    ).body
    assert "| Server migrieren und alte Maschinen abschalten | Mueller | 30.06.2026 |" in text
    assert "| Monitoring einrichten | Schmidt, Weber | 15.07.2026 |" in text


def test_a_paragraph_led_by_a_date_is_not_a_list_item(no_models):
    lines = [("Chronik", 18, 72, 700), ("Das Projekt begann im Fruehjahr.", 11, 72, 670)]
    lines += [("12.03.2026 fand die Abnahme statt.", 11, 72, 640)]
    lines += [("3.5 Tonnen Material wurden geliefert.", 11, 72, 610)]
    text = markdown.convert(_pdf_of(lines), name="c.pdf", ocr=False).body
    assert "- 12.03" not in text and "12.03.2026 fand" in text and "- 3.5" not in text


def test_a_picture_alone_in_a_table_cell_keeps_its_cell(tmp_path):
    table = (
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:drawing>"
        f'<a:blip {A} r:embed="rId9"/></w:drawing></w:r></w:p></w:tc>'
        "<w:tc>" + para("Plan") + "</w:tc></w:tr><w:tr><w:tc>" + para("a") + "</w:tc>"
        "<w:tc>" + para("b") + "</w:tc></w:tr></w:tbl>"
    )
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 3000
    word = docx(
        table,
        relations=(("rId9", "image", "media/image1.png"),),
        extra={"word/media/image1.png": png},
    )
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "t.docx").write_bytes(word)
    markdown.export([tmp_path / "in"], tmp_path / "out", assets=True, ocr=False)
    text = (tmp_path / "out" / "t.docx.md").read_text(encoding="utf-8")
    assert "| ![](_assets/t.docx/image1.png) | Plan |" in text


def test_one_note_with_assets_gets_its_pictures(tmp_path, capsys):
    (tmp_path / "plan.docx").write_bytes(picture_docx())
    note = tmp_path / "out" / "plan.md"
    assert (
        main(
            ["markdown", str(tmp_path / "plan.docx"), "-o", str(note), "--assets", "--no-ocr", "-q"]
        )
        == 0
    )
    link = re.search(r"!\[[^\]]*\]\(([^)]+)\)", note.read_text(encoding="utf-8")).group(1)
    assert (note.parent / link).is_file()
    assert main(["markdown", str(tmp_path / "plan.docx"), "--assets", "--no-ocr"]) == 2


def test_a_note_written_before_its_picture_failed_stays_the_exports(tmp_path, monkeypatch):
    from ocrust.markdown import _store

    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "r.docx").write_bytes(picture_docx())
    write = _store._write

    def full(path, data):
        if path.suffix == ".png":
            raise OSError(28, "No space left on device")
        write(path, data)

    monkeypatch.setattr(_store, "_write", full)
    assert markdown.export([folder], tmp_path / "out", assets=True, ocr=False).failed
    monkeypatch.setattr(_store, "_write", write)
    result = markdown.export([folder], tmp_path / "out", assets=True, ocr=False)
    assert not result.kept and not result.failed
    assert (tmp_path / "out" / "_assets" / "r.docx" / "image1.png").is_file()


def test_an_archive_back_after_a_prune_gets_all_its_notes_again(tmp_path):
    folder, away = tmp_path / "in", tmp_path / "away"
    folder.mkdir()
    away.mkdir()
    (folder / "akte.zip").write_bytes(package({"a.txt": "Vermerk A", "b.txt": "Vermerk B"}))
    out = tmp_path / "kb"
    markdown.export([folder], out)
    note = out / "akte.zip" / "a.txt.md"
    note.write_text(note.read_text(encoding="utf-8") + "\nmeine Notiz\n", encoding="utf-8")
    (folder / "akte.zip").rename(away / "akte.zip")
    markdown.export([folder], out, prune=True)
    assert not (out / "akte.zip" / "b.txt.md").exists()
    (away / "akte.zip").rename(folder / "akte.zip")
    markdown.export([folder], out)
    assert (out / "akte.zip" / "b.txt.md").is_file() and "meine Notiz" in note.read_text(
        encoding="utf-8"
    )


def test_a_named_pipe_in_a_folder_is_skipped(tmp_path):
    if not hasattr(os, "mkfifo"):  # pragma: no cover - Windows
        pytest.skip("no named pipes")
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a.txt").write_text("a", encoding="utf-8")
    os.mkfifo(folder / "pipe.txt")
    result = markdown.export([folder], tmp_path / "out")
    assert len(result.written) == 1 and folder / "pipe.txt" in result.skipped


def test_scan_only_says_when_a_page_is_missing(no_models, tmp_path, capsys):
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(_pdf_of([("Hallo", 20, 72, 700)]))
    assert main(["scan", str(pdf), "--pdf-text", "only", "--pages", "3", "-q"]) == 1
    assert "no page 3" in " ".join(capsys.readouterr().err.split())


# -- fourth review round


def test_a_row_with_an_empty_cell_stays_a_row(no_models):
    rows = [("Bezeichnung", "Menge", "Preis"), ("Schrauben M4", "100", "4,90")]
    rows += [("Muttern M4", "", "2,10"), ("Unterlegscheiben M4", "50", "1,20")]
    lines = [
        (text, 10, x, 700 - i * 11.5)
        for i, row in enumerate(rows)
        for text, x in zip(row, (72, 300, 420), strict=True)
        if text
    ]
    text = markdown.convert(_pdf_of(lines), name="t.pdf", ocr=False).body
    assert "| Muttern M4 |  | 2,10 |" in text and "| Schrauben M4 | 100 | 4,90 |" in text


def test_a_slide_title_over_one_line_is_a_heading(no_models):
    lines = [
        ("What comes next for the team", 28, 72, 700),
        ("We open two new offices.", 14, 72, 640),
    ]
    text = markdown.convert(_pdf_of(lines), name="s.pdf", ocr=False).body
    assert "# What comes next for the team" in text and "\nWe open two new offices." in text


def test_terms_set_in_small_print_keep_their_headings(no_models):
    lines, y = [], 760
    for section in range(1, 5):
        lines.append((f"§ {section} Geltungsbereich", 9, 60, y))
        y -= 14
        for _ in range(6):
            lines.append(("Diese Bedingungen gelten fuer alle Vertraege mit dem Kunden.", 7, 60, y))
            y -= 9
        y -= 8
    text = markdown.convert(_pdf_of(lines), name="agb.pdf", ocr=False).body
    assert text.count("# § ") == 4


def test_a_letterhead_set_large_is_not_a_heading_on_every_page(no_models):
    from conftest import _pdf_from_streams, _winansi_literal

    pages = []
    for n in range(1, 4):
        lines = [("ACME Consulting GmbH", 16, 72, 770)]
        lines += [
            (f"Absatz {n} beginnt hier mit einem ganzen Satz Text.", 11, 72, 700 - i * 15)
            for i in range(12)
        ]
        shown = (
            f"/F1 {s} Tf 1 0 0 1 {x} {y} Tm ({_winansi_literal(t)}) Tj" for t, s, x, y in lines
        )
        pages.append("BT\n" + "\n".join(shown) + "\nET")
    text = markdown.convert(_pdf_from_streams(pages), name="b.pdf", ocr=False).body
    assert "ACME Consulting" not in text


def test_a_date_beside_the_address_stays_apart_on_a_page_with_a_table(no_models):
    lines = [("Firma Beispiel GmbH", 10, 72, 720), ("Herrn Max Mustermann", 10, 72, 708)]
    lines += [("Berlin, 3. Juni 2026", 10, 420, 708), ("Musterstrasse 12", 10, 72, 696)]
    lines += [("12345 Berlin", 10, 72, 684), ("Angebot Nr. 4711", 10, 72, 650)]
    rows = [
        ("Pos.", "Leistung", "Preis"),
        ("1", "Beratung", "960,00"),
        ("2", "Umsetzung", "2.880,00"),
    ]
    rows += [("3", "Schulung", "480,00")]
    lines += [
        (t, 10, x, 600 - i * 14)
        for i, row in enumerate(rows)
        for t, x in zip(row, (72, 110, 450), strict=True)
    ]
    text = markdown.convert(_pdf_of(lines), name="a.pdf", ocr=False).body
    assert "Mustermann Berlin, 3. Juni" not in text and "Herrn Max Mustermann" in text


def test_a_heading_a_little_larger_than_its_text_does_not_swallow_it(no_models):
    lines = [("Section 1", 13.5, 72, 700), ("The committee met on Monday.", 11, 72, 686)]
    lines += [("It agreed on the budget.", 11, 72, 672)]
    text = markdown.convert(_pdf_of(lines), name="h.pdf", ocr=False).body
    assert "# Section 1\n" in text and "The committee met on Monday." in text
    assert "# Section 1 The" not in text


def test_link_cards_keep_their_links():
    cards = "".join(
        f'<a href="https://example.com/posts/{i}"><h3>Beitrag {i}</h3><p>Worum es geht.</p></a>'
        for i in range(2)
    )
    text = convert(f"<main>{cards}</main>".encode(), "i.html").markdown
    assert "[Beitrag 0](https://example.com/posts/0)" in text and "Worum es geht." in text


def test_a_page_of_unclosed_divs_is_read():
    page = "<body>" + "".join(f"<div>Zeile {i}" for i in range(600)) + "</body>"
    text = convert(page.encode(), "d.html").markdown
    assert "Zeile 0" in text and "Zeile 599" in text


def test_assets_to_a_standard_stream_are_refused(tmp_path):
    (tmp_path / "plan.docx").write_bytes(picture_docx())
    args = ["markdown", str(tmp_path / "plan.docx"), "-o", "/dev/stdout", "--assets", "--no-ocr"]
    assert main(args) == 2
