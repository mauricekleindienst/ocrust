"""Every input format, end to end.

The same page is written as PNG, JPEG, WebP, BMP, PPM, TGA, GIF, single-page and
multi-page TIFF, and read back through the public API. A codec that silently
loses the page would otherwise only show up in production.
"""

from __future__ import annotations

import pytest

from conftest import squash

FORMATS = ["png", "jpg", "webp", "bmp", "ppm", "tga", "tiff"]


@pytest.mark.parametrize("fmt", FORMATS)
def test_reads_every_raster_format(engine, image_fixtures, fmt):
    doc = engine.scan(image_fixtures[fmt])
    flat = squash(doc.text)
    assert squash("INVOICE") in flat, f"{fmt}: {doc.text!r}"
    assert squash("199.90") in flat, f"{fmt}: {doc.text!r}"
    assert doc.pages[0].origin in {"image", "tiff_frame"}


def test_gif_is_readable(engine, image_fixtures):
    # GIF is palette-based, so text comes out noisier; only presence is asserted.
    doc = engine.scan(image_fixtures["gif"])
    assert doc.lines, "no text found in GIF"


def test_multipage_tiff_yields_every_page(engine, image_fixtures):
    doc = engine.scan(image_fixtures["tiff-multi"])
    assert len(doc.pages) == 2, f"got {len(doc.pages)} page(s)"
    for page in doc.pages:
        assert squash("Pageonetext") in squash(page.text)
        assert page.origin == "tiff_frame"


def test_pdf_pages_are_ordered(engine, two_page_pdf_bytes):
    doc = engine.scan(two_page_pdf_bytes, name="two.pdf")
    assert [page.index for page in doc.pages] == [0, 1]
    assert all(page.origin == "pdf_page" for page in doc.pages)


def test_bytes_and_path_agree(engine, image_fixtures):
    path = image_fixtures["png"]
    assert squash(engine.scan(path).text) == squash(
        engine.scan(path.read_bytes(), name="page.png").text
    )


def test_pil_image_input(engine, image_fixtures):
    from PIL import Image

    doc = engine.scan(Image.open(image_fixtures["png"]), name="pil")
    assert squash("INVOICE") in squash(doc.text)


def test_unsupported_format_is_reported(engine, tmp_path):
    bogus = tmp_path / "note.docx"
    bogus.write_bytes(b"PK\x03\x04 not really a document")
    with pytest.raises(ValueError, match="note.docx"):
        engine.scan(bogus)
