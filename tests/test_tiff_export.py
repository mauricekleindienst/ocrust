"""Multi-page TIFF output."""

from __future__ import annotations

from conftest import squash


def test_tiff_round_trip(engine, invoice_pdf, tmp_path):
    data, doc = engine.to_tiff(invoice_pdf)
    assert data[:4] in (b"II*\x00", b"MM\x00*")
    assert squash("INVOICE") in squash(doc.text)

    out = tmp_path / "out.tiff"
    out.write_bytes(data)
    # Reading our own output back must give the same text.
    again = engine.scan(out)
    assert squash("INVOICE") in squash(again.text)


def test_grayscale_tiff_is_smaller(engine, invoice_pdf):
    color, _ = engine.to_tiff(invoice_pdf)
    gray, _ = engine.to_tiff(invoice_pdf, gray=True)
    assert len(gray) < len(color)


def test_multipage_pdf_becomes_multipage_tiff(engine, two_page_pdf_bytes, tmp_path):
    source = tmp_path / "two.pdf"
    source.write_bytes(two_page_pdf_bytes)
    data, doc = engine.to_tiff(source)
    assert len(doc.pages) == 2
    out = tmp_path / "two.tiff"
    out.write_bytes(data)
    assert len(engine.scan(out).pages) == 2
