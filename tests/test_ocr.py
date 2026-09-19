"""OCR behaviour against real models. Skipped when none are installed."""

from __future__ import annotations

import pytest

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


def test_page_selection_beyond_the_document_says_so(engine, invoice_pdf):
    # An empty document reads like a blank scan, so asking for a page that is
    # not there has to fail out loud.
    with pytest.raises(ValueError, match="no page 6"):
        engine.scan(invoice_pdf, pages=[5])


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


def test_progress_is_reported_per_page(engine, invoice_pdf):
    seen: list[tuple[int, int, int]] = []
    doc = engine.scan(
        invoice_pdf, progress=lambda page, total, lines: seen.append((page, total, lines))
    )
    assert seen == [(0, 1, len(doc.lines))]


def test_a_raising_progress_callback_aborts_the_scan(engine, invoice_pdf):
    class Stop(Exception):
        pass

    def boom(page: int, total: int, lines: int) -> None:
        raise Stop("enough")

    with pytest.raises(Stop):
        engine.scan(invoice_pdf, progress=boom)


def test_search_locates_text_on_a_real_scan(engine, invoice_pdf):
    doc = engine.scan(invoice_pdf)
    hits = doc.search("invoice")
    assert hits, doc.text
    assert hits[0].page == 0
    assert hits[0].box.width > 0
    assert doc.search("no-such-string") == ()


def test_scan_many_reads_a_directory(engine, invoice_pdf, tmp_path):
    folder = tmp_path / "batch"
    folder.mkdir()
    for name in ("a.pdf", "b.pdf"):
        (folder / name).write_bytes(invoice_pdf.read_bytes())
    (folder / "notes.txt").write_text("ignored")

    docs = list(engine.scan_many([folder]))
    assert len(docs) == 2
    assert all("INVOICE" in d.text.upper() for d in docs)


def test_cli_prints_on_a_console_that_is_not_utf8(engine):
    """A redirected stdout on Windows is cp1252, and `languages` lists Vietnamese.

    `ocrust languages` ended in a `UnicodeEncodeError` there, which
    `PYTHONIOENCODING` reproduces on any platform.
    """
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    beside = Path(sys.executable).with_name("ocrust")
    exe = str(beside) if beside.exists() else shutil.which("ocrust")
    if exe is None:
        pytest.skip("the console script is not installed")

    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    result = subprocess.run([exe, "languages"], capture_output=True, env=env)  # noqa: S603
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert b"UnicodeEncodeError" not in result.stderr
    assert b"Vietnamese" in result.stdout
