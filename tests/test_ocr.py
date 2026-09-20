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


def test_mixed_inputs_convert_into_one_pdf(engine, tmp_path):
    """The converter: anything readable, in order, as the pages of one PDF.

    A bilevel TIFF, a three-page TIFF, a QOI and a PDF go in; eight A4 pages
    with a searchable text layer come out.
    """
    from PIL import Image, ImageDraw

    def page(text: str) -> Image.Image:
        im = Image.new("RGB", (1654, 2338), "white")
        ImageDraw.Draw(im).text((120, 300), text, fill="black")
        return im

    one = page("ERSTE SEITE")
    one.convert("1").save(tmp_path / "a.tiff", "TIFF", compression="group4")
    three = page("DREI SEITEN")
    three.save(tmp_path / "b.tiff", save_all=True, append_images=[three, three])
    page("EIN QOI").save(tmp_path / "c.qoi")
    page("EIN PDF").save(tmp_path / "d.pdf", resolution=200)

    data, pages = engine.searchable_pdf_many(sorted(tmp_path.iterdir()))
    assert pages == 6, f"1 + 3 + 1 + 1 pages, got {pages}"
    assert data.startswith(b"%PDF")

    import io

    reader = pytest.importorskip("pypdf").PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 6
    for number, rendered in enumerate(reader.pages):
        box = rendered.mediabox
        assert abs(float(box.width) - 595) < 15, f"page {number} is not A4: {box.width}"
        assert (rendered.extract_text() or "").strip(), f"page {number} has no text layer"


def test_a_bilevel_tiff_is_read(engine, tmp_path):
    """Mode-1 TIFF is how a scanned archive and every CCITT fax is stored.

    It used to fail outright with "unsupported pixel layout (Gray(1))": the
    packed rows, eight pixels to the byte, were measured as though there were
    one byte each. The corpus never caught it because its "1-bit fax" fixture
    was saved as RGB.
    """
    from PIL import Image, ImageOps, TiffImagePlugin

    page = Image.new("RGB", (900, 200), "white")
    from PIL import ImageDraw

    ImageDraw.Draw(page).text((20, 60), "RECHNUNG 2026", fill="black")
    bilevel = page.convert("1")

    plain = tmp_path / "bilevel.tiff"
    bilevel.save(plain, "TIFF", compression="group4")
    assert "RECHNUNG" in engine.scan(plain).text

    # The same page the other way round: bits flipped, WhiteIsZero declared —
    # the CCITT convention. Reading the tag wrong inverts the page.
    info = TiffImagePlugin.ImageFileDirectory_v2()
    info[262] = 0
    flipped = tmp_path / "white_is_zero.tiff"
    ImageOps.invert(bilevel.convert("L")).convert("1").save(
        flipped, "TIFF", compression="group4", tiffinfo=info
    )
    assert "RECHNUNG" in engine.scan(flipped).text


def test_every_offered_suffix_is_one_the_reader_opens():
    """The list the CLI filters directories with comes from the reader itself.

    `dds`, `exr`, `ico` and `avif` were offered once and none could be opened,
    and `avif` was in the Rust list alone — which is what deriving it prevents.
    """
    import ocrust

    from_reader = frozenset(f".{s}" for s in ocrust._ocrust.supported_suffixes())
    assert from_reader == ocrust.READABLE_SUFFIXES
    for gone in (".dds", ".exr", ".ico", ".avif"):
        assert gone not in ocrust.READABLE_SUFFIXES
    for kept in (".png", ".pdf", ".tiff", ".qoi", ".hdr", ".pbm"):
        assert kept in ocrust.READABLE_SUFFIXES


def test_cli_prints_on_a_console_that_is_not_utf8(engine):
    """A redirected stdout on Windows is cp1252, and `languages` prints `ẞ`.

    `ocrust languages` ended in a `UnicodeEncodeError` there, which
    `PYTHONIOENCODING` reproduces on any platform. The character that trips it
    used to be Vietnamese `ạ ả`; now that Vietnamese is no longer offered it is
    the `ẞ` in German's substitution note, which cp1252 cannot encode either.
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
    assert b"German" in result.stdout


def test_page_selection_applies_to_in_memory_images(engine, invoice_pdf):
    """An array is one page, and asking for another one says so.

    `pages` used to be dropped on the floor for numpy and PIL input, which made
    `scan(array, pages=[7])` look like a successful scan of page 1.
    """
    numpy = __import__("numpy")
    page = engine.scan(invoice_pdf, pages=[0]).pages[0]
    array = numpy.zeros((page.height, page.width, 3), dtype=numpy.uint8)

    assert len(engine.scan(array, pages=[0]).pages) == 1
    with pytest.raises(ValueError, match="no page 8"):
        engine.scan(array, pages=[7])


def test_a_batch_keeps_its_page_selection_for_every_file(engine, invoice_pdf, tmp_path, capsys):
    """The second file in a batch used to receive the first file's summary line.

    `_cmd_scan` held the page selection in `pages` and the summary rebound the
    same name to `"1 page"`, which the engine then refused — but only from the
    second file onwards, and only without `-q`, so nothing noticed.
    """
    from ocrust.cli import main

    folder = tmp_path / "batch"
    folder.mkdir()
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        (folder / name).write_bytes(invoice_pdf.read_bytes())

    out = tmp_path / "out"
    assert main(["scan", str(folder), "-o", str(out), "--pages", "1"]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["a.txt", "b.txt", "c.txt"]
    for written in out.iterdir():
        assert written.read_text().strip(), written
