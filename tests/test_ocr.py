"""OCR behaviour against real models. Skipped when none are installed."""

from __future__ import annotations

import ocrust

from conftest import squash


def test_reads_pdf_text(engine, invoice_pdf):
    doc = engine.scan(invoice_pdf)
    flat = squash(doc.text)
    for needle in ["INVOICE", "2026-0042", "199.90", "Thank you"]:
        assert squash(needle) in flat, f"missing {needle!r} in {doc.text!r}"
    assert doc.confidence > 0.8
    assert len(doc.pages) == 1
    assert doc.pages[0].origin == "pdf_page"


def test_read_returns_plain_text(engine, invoice_pdf):
    text = engine.read(invoice_pdf)
    assert "INVOICE" in text.upper()


def test_scan_from_bytes_matches_path(engine, invoice_pdf, invoice_pdf_bytes):
    from_path = engine.scan(invoice_pdf).text
    from_bytes = engine.scan(invoice_pdf_bytes, name="mem.pdf").text
    assert squash(from_path) == squash(from_bytes)


def test_lines_carry_geometry_and_words(engine, invoice_pdf):
    doc = engine.scan(invoice_pdf)
    line = doc.lines[0]
    assert line.box.width > 0 and line.box.height > 0
    assert 0.0 <= line.confidence <= 1.0
    assert line.words, "word boxes expected by default"
    assert line.words[0].box.x0 >= line.box.x0 - 2


def test_markdown_and_hocr_contain_text(engine, invoice_pdf):
    doc = engine.scan(invoice_pdf)
    assert "INVOICE" in doc.markdown().upper()
    hocr = doc.hocr()
    assert "ocrx_word" in hocr and "INVOICE" in hocr.upper()


def test_page_selection(engine, invoice_pdf):
    doc = engine.scan(invoice_pdf, pages=[0])
    assert len(doc.pages) == 1
    empty = engine.scan(invoice_pdf, pages=[5])
    assert len(empty.pages) == 0


def test_scan_many_is_ordered(engine, invoice_pdf):
    docs = list(engine.scan_many([invoice_pdf, invoice_pdf]))
    assert len(docs) == 2
    assert squash(docs[0].text) == squash(docs[1].text)


def test_numpy_input(engine):
    numpy = __import__("numpy")
    array = numpy.full((200, 600, 3), 255, dtype=numpy.uint8)
    blank = engine.scan(array, name="blank")
    assert blank.text == ""
    # Small frames are upscaled to the 800px floor; a blank page must not be
    # rotated, so the aspect ratio is preserved exactly.
    assert blank.pages[0].width == 800
    assert blank.pages[0].rotation == 0.0


def test_numpy_grayscale_and_rgba_inputs(engine):
    numpy = __import__("numpy")
    gray = numpy.full((300, 900), 255, dtype=numpy.uint8)
    rgba = numpy.full((300, 900, 4), 255, dtype=numpy.uint8)
    for array in (gray, rgba):
        doc = engine.scan(array, name="frame")
        assert doc.pages[0].width == 900


def test_searchable_pdf_round_trip(engine, invoice_pdf, tmp_path):
    data = engine.searchable_pdf(invoice_pdf)
    assert data.startswith(b"%PDF")
    assert b"3 Tr" in data, "invisible text layer expected"
    out = tmp_path / "searchable.pdf"
    out.write_bytes(data)
    # The result must be readable by our own PDF path again.
    reread = engine.scan(out)
    assert "INVOICE" in reread.text.upper()


def test_models_property(engine):
    models = engine.models
    assert models["detection"].endswith(".onnx")
    assert models["recognition"].endswith(".onnx")
