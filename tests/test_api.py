"""Public API tests that do not need model files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ocrust


def test_version_is_exposed():
    assert ocrust.__version__
    assert ocrust.__version__[0].isdigit()


def test_runtime_info_finds_onnxruntime():
    info = ocrust.runtime_info()
    assert info["ocrust"] == ocrust.__version__
    # The wheel depends on onnxruntime, so the library must be discoverable.
    assert info["onnxruntime_dylib"], info
    assert "ONNX Runtime" in str(info["onnxruntime_loaded"]), info


def test_library_is_found_under_every_platform_spelling(tmp_path, monkeypatch):
    """Linux, macOS and Windows version the library's file name differently.

    macOS puts the version before the extension, which a `name + "*"` glob does
    not match — that is how a macOS wheel with the runtime installed could still
    report "no ONNX Runtime".
    """
    from ocrust import _runtime

    cases = {
        "linux": "libonnxruntime.so.1.30.0",
        "darwin": "libonnxruntime.1.30.0.dylib",
        "win32": "onnxruntime.dll",
    }
    for platform, filename in cases.items():
        directory = tmp_path / platform
        directory.mkdir()
        (directory / filename).write_bytes(b"not really a library")
        monkeypatch.setattr(_runtime.sys, "platform", platform)
        found = _runtime.library_in(directory)
        assert found is not None, f"{platform}: {filename} not found"
        assert found.name == filename

    # A directory without one says so instead of guessing.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _runtime.library_in(empty) is None
    assert _runtime.library_in(tmp_path / "nope") is None


def test_the_newest_runtime_wins_when_several_are_installed(tmp_path, monkeypatch):
    """1.30 is newer than 1.9, which sorting the file names does not know."""
    from ocrust import _runtime

    monkeypatch.setattr(_runtime.sys, "platform", "linux")
    for name in ("libonnxruntime.so.1.9.0", "libonnxruntime.so.1.30.0"):
        (tmp_path / name).write_bytes(b"not really a library")
    found = _runtime.library_in(tmp_path)
    assert found is not None and found.name == "libonnxruntime.so.1.30.0", found

    # On macOS the glob also catches the provider stubs that sit beside it.
    mac = tmp_path / "darwin"
    mac.mkdir()
    monkeypatch.setattr(_runtime.sys, "platform", "darwin")
    for name in (
        "libonnxruntime.1.30.0.dylib",
        "libonnxruntime_providers_shared.1.30.0.dylib",
    ):
        (mac / name).write_bytes(b"not really a library")
    found = _runtime.library_in(mac)
    assert found is not None and found.name == "libonnxruntime.1.30.0.dylib", found


def test_models_cache_dir_is_absolute():
    assert Path(ocrust.models_cache_dir()).is_absolute()


def test_document_from_json_builds_dataclasses():
    payload = {
        "source": "x.png",
        "elapsed_ms": 5.0,
        "pages": [
            {
                "index": 0,
                "width": 100,
                "height": 50,
                "rotation": 0.0,
                "origin": "image",
                "elapsed_ms": 4.0,
                "blocks": [
                    {
                        "kind": "paragraph",
                        "bbox": {"x0": 1.0, "y0": 2.0, "x1": 9.0, "y1": 8.0},
                        "lines": [
                            {
                                "text": "hello world",
                                "confidence": 0.9,
                                "angle": 0.0,
                                "det_score": 0.8,
                                "bbox": {"x0": 1.0, "y0": 2.0, "x1": 9.0, "y1": 8.0},
                                "quad": {
                                    "points": [
                                        {"x": 1.0, "y": 2.0},
                                        {"x": 9.0, "y": 2.0},
                                        {"x": 9.0, "y": 8.0},
                                        {"x": 1.0, "y": 8.0},
                                    ]
                                },
                                "words": [
                                    {
                                        "text": "hello",
                                        "confidence": 0.95,
                                        "bbox": {"x0": 1.0, "y0": 2.0, "x1": 4.0, "y1": 8.0},
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    doc = ocrust.Document._from_json(payload)
    assert doc.text == "hello world"
    assert len(doc.pages) == 1 and len(doc) == 1
    assert doc.lines[0].box.as_tuple() == (1.0, 2.0, 9.0, 8.0)
    assert doc.lines[0].box.width == 8.0
    assert doc.words[0].text == "hello"
    assert doc.confidence == pytest.approx(0.9)
    assert doc.pages[0].confidence == pytest.approx(0.9)
    assert len(doc.lines[0].polygon) == 4

    # Exports are rendered by the Rust core from the same payload.
    assert "hello world" in doc.markdown()
    assert "ocr_line" in doc.hocr()
    assert "<alto" in doc.alto()
    assert "hello world" in doc.csv()
    assert json.loads(doc.json())["pages"][0]["width"] == 100
    with pytest.raises(ValueError):
        doc.render("wingdings")


def _doc_with_words() -> ocrust.Document:
    """A two-line document whose words carry boxes, for search tests."""

    def word(text, x0, x1):
        return {
            "text": text,
            "confidence": 0.9,
            "bbox": {"x0": x0, "y0": 10.0, "x1": x1, "y1": 30.0},
        }

    def line(text, words, y0):
        return {
            "text": text,
            "confidence": 0.9,
            "angle": 0.0,
            "det_score": 0.9,
            "bbox": {"x0": 10.0, "y0": y0, "x1": 300.0, "y1": y0 + 20.0},
            "quad": {
                "points": [
                    {"x": 10.0, "y": y0},
                    {"x": 300.0, "y": y0},
                    {"x": 300.0, "y": y0 + 20.0},
                    {"x": 10.0, "y": y0 + 20.0},
                ]
            },
            "words": words,
        }

    payload = {
        "source": "invoice.pdf",
        "elapsed_ms": 1.0,
        "pages": [
            {
                "index": 0,
                "width": 400,
                "height": 200,
                "rotation": 0.0,
                "origin": "image",
                "elapsed_ms": 1.0,
                "blocks": [
                    {
                        "kind": "paragraph",
                        "bbox": {"x0": 10.0, "y0": 10.0, "x1": 300.0, "y1": 60.0},
                        "lines": [
                            line(
                                "Gesamtbetrag: 5.726,88 EUR",
                                [
                                    word("Gesamtbetrag:", 10.0, 120.0),
                                    word("5.726,88", 130.0, 210.0),
                                    word("EUR", 220.0, 260.0),
                                ],
                                10.0,
                            ),
                            line("Zahlbar in 30 Tagen", [], 40.0),
                        ],
                    }
                ],
            }
        ],
    }
    return ocrust.Document._from_json(payload)


def test_search_finds_text_case_insensitively():
    doc = _doc_with_words()
    hits = doc.search("gesamtbetrag")
    assert len(hits) == 1
    hit = hits[0]
    assert hit.text == "Gesamtbetrag"
    assert hit.page == 0
    # The box comes from the word, not the whole line.
    assert hit.box.as_tuple() == (10.0, 10.0, 120.0, 30.0)
    assert hit.line.text.startswith("Gesamtbetrag")

    assert doc.search("GESAMTBETRAG", case=True) == ()
    assert doc.search("") == ()


def test_search_spans_words_and_supports_regex():
    doc = _doc_with_words()
    # A hit across two words unions their boxes.
    hit = doc.search("5.726,88 EUR")[0]
    assert hit.box.as_tuple() == (130.0, 10.0, 260.0, 30.0)

    amounts = doc.search(r"\d+,\d\d", regex=True)
    assert [h.text for h in amounts] == ["726,88"]

    # Whole words only: `in` appears inside `Zahlbar` too.
    assert len(doc.search("in")) == 1
    assert len(doc.search("in", whole_words=True)) == 1
    assert len(doc.search("30", whole_words=True)) == 1


def test_search_falls_back_to_the_line_box():
    doc = _doc_with_words()
    hit = doc.search("Tagen")[0]
    # That line has no word boxes, so the line's own box is reported.
    assert hit.box.as_tuple() == (10.0, 40.0, 300.0, 60.0)


def test_scan_rejects_unsupported_input_types(engine):
    with pytest.raises(TypeError):
        engine.scan(42)


def test_scan_reports_missing_file(engine):
    with pytest.raises((OSError, ocrust.OcrustError)):
        engine.scan("/definitely/not/here.png")


def test_scan_reports_unreadable_bytes(engine):
    with pytest.raises(ValueError):
        engine.scan(b"not an image at all", name="junk.bin")


def test_match_reports_the_pages_own_index():
    # Scanning a subset of a PDF keeps the source page numbers, so a hit has to
    # be reported against `Page.index` rather than its position in the result.
    payload = {
        "source": "report.pdf",
        "elapsed_ms": 1.0,
        "pages": [
            {
                "index": 4,
                "width": 200,
                "height": 100,
                "rotation": 0.0,
                "origin": "pdf_page",
                "elapsed_ms": 1.0,
                "blocks": [
                    {
                        "kind": "paragraph",
                        "bbox": {"x0": 0.0, "y0": 0.0, "x1": 200.0, "y1": 20.0},
                        "lines": [
                            {
                                "text": "Summe 42",
                                "confidence": 0.9,
                                "angle": 0.0,
                                "bbox": {"x0": 0.0, "y0": 0.0, "x1": 200.0, "y1": 20.0},
                                "quad": {
                                    "points": [
                                        {"x": 0.0, "y": 0.0},
                                        {"x": 200.0, "y": 0.0},
                                        {"x": 200.0, "y": 20.0},
                                        {"x": 0.0, "y": 20.0},
                                    ]
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }
    doc = ocrust.Document._from_json(payload)
    assert doc.pages[0].index == 4
    assert doc.search("Summe")[0].page == 4


def test_documents_built_by_hand_still_expose_to_dict():
    # `to_dict` hands back the engine's own JSON; a document assembled in Python
    # has none, and must say so instead of raising.
    doc = ocrust.Document(source="made-up", pages=(), elapsed_ms=0.0)
    assert doc.to_dict() == {}
    assert doc.text == ""


def test_recursive_glob_patterns_are_expanded(tmp_path):
    from ocrust import _expand_sources

    nested = tmp_path / "2026" / "q1"
    nested.mkdir(parents=True)
    (nested / "invoice.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "top.pdf").write_bytes(b"%PDF-1.4\n")

    found = _expand_sources([str(tmp_path / "**" / "*.pdf")])
    # `**` descends and also matches zero directories, like a shell would.
    assert [Path(p).name for p in found] == ["invoice.pdf", "top.pdf"]

    # A plain pattern in one directory keeps working.
    found = _expand_sources([str(tmp_path / "*.pdf")])
    assert [Path(p).name for p in found] == ["top.pdf"]


def test_quality_is_reported_and_bounded():
    """`quality` estimates how much of a document is right, `confidence` does not."""
    payload = {
        "source": "two.pdf",
        "elapsed_ms": 2.0,
        "pages": [
            {
                "index": 0,
                "width": 200,
                "height": 100,
                "rotation": 0.0,
                "origin": "pdf_page",
                "elapsed_ms": 1.0,
                "quality": 0.98,
                "blocks": [
                    {
                        "kind": "paragraph",
                        "bbox": {"x0": 0.0, "y0": 0.0, "x1": 200.0, "y1": 20.0},
                        "lines": [
                            {
                                "text": "a long clean line of text",
                                "confidence": 0.99,
                                "angle": 0.0,
                                "margin": 0.97,
                                "bbox": {"x0": 0.0, "y0": 0.0, "x1": 200.0, "y1": 20.0},
                                "quad": {
                                    "points": [
                                        {"x": 0.0, "y": 0.0},
                                        {"x": 200.0, "y": 0.0},
                                        {"x": 200.0, "y": 20.0},
                                        {"x": 0.0, "y": 20.0},
                                    ]
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "index": 1,
                "width": 200,
                "height": 100,
                "rotation": 0.0,
                "origin": "pdf_page",
                "elapsed_ms": 1.0,
                "quality": None,
                "blocks": [],
            },
        ],
    }
    doc = ocrust.Document._from_json(payload)
    assert doc.pages[0].quality == 0.98
    assert doc.pages[1].quality is None
    # A page with no text has nothing to judge, so it does not drag the mean down.
    assert doc.quality == 0.98
    assert doc.lines[0].margin == 0.97
    assert doc.confidence == 0.99, "confidence still means what it always meant"


def test_quality_is_none_without_text():
    doc = ocrust.Document._from_json({"source": "blank.png", "elapsed_ms": 1.0, "pages": []})
    assert doc.quality is None
    assert doc.confidence is None


def test_a_table_is_read_as_rows_and_columns(engine, table_pdf):
    """The grid an invoice actually has, from where its cells sit."""
    doc = engine.scan(table_pdf)
    assert len(doc.tables) == 1, doc.render("markdown")
    table = doc.tables[0]
    assert (table.rows, table.columns) == (4, 4)
    assert table.row_text(0) == ["Position", "Menge", "Preis", "Summe"]
    assert table.row_text(1) == ["Widget A", "12", "49,90", "598,80"]
    assert table.as_rows()[3] == ["Kabel C", "7", "12,50", "87,50"]
    # Every cell carries its own box and confidence.
    for cell in table.cells:
        assert cell.box.width > 0 and cell.box.height > 0
        assert 0.0 <= cell.confidence <= 1.0
        assert cell.column_span >= 1


def test_the_prose_around_a_table_is_not_part_of_it(engine, table_pdf):
    """A heading above and a closing line below stay text."""
    doc = engine.scan(table_pdf)
    kinds = [block.kind for block in doc.pages[0].blocks]
    assert kinds.count("table") == 1
    assert len(kinds) >= 2, kinds
    table_text = "\n".join(block.text for block in doc.pages[0].blocks if block.kind == "table")
    assert "RECHNUNG" not in table_text
    assert "Vielen Dank" not in table_text
    # And the text of the document still has all of it.
    assert "RECHNUNG" in doc.text and "Vielen Dank" in doc.text


def test_a_table_renders_as_markdown_and_csv(engine, table_pdf):
    doc = engine.scan(table_pdf)
    markdown = doc.render("markdown")
    assert "| Position | Menge | Preis | Summe |" in markdown
    assert "| --- | --- | --- | --- |" in markdown

    csv = doc.tables[0].to_csv()
    rows = csv.strip().splitlines()
    assert rows[0] == "Position,Menge,Preis,Summe"
    # A German decimal comma has to be quoted, or the row grows two columns.
    assert rows[1] == 'Widget A,12,"49,90","598,80"'


def test_tables_can_be_turned_off(table_pdf):
    import ocrust

    plain = ocrust.Ocr(tables=False)
    doc = plain.scan(table_pdf)
    assert doc.tables == ()
    assert all(block.kind != "table" for block in doc.pages[0].blocks)
    # The text is still all there; only the grid is not read.
    assert "Widget A" in doc.text


def test_a_merged_row_keeps_the_boxes_it_was_merged_from(engine, table_pdf):
    """`Line.segments` is what makes a row's columns recoverable."""
    doc = engine.scan(table_pdf)
    merged = [line for line in doc.lines if line.segments]
    assert merged, "a table row arrives as several detection boxes"
    for line in merged:
        assert len(line.segments) >= 2
        # The row's box covers every box it was merged from.
        for segment in line.segments:
            assert segment.box.x0 >= line.box.x0 - 1
            assert segment.box.x1 <= line.box.x1 + 1
            assert segment.text.strip()
