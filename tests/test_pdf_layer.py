"""The archival workflow: adding an OCR text layer to an existing PDF."""

from __future__ import annotations

import pytest

import ocrust
from conftest import squash


def test_plan_reports_pages_and_geometry(engine, invoice_pdf_bytes):
    plan = engine.plan_pdf(invoice_pdf_bytes)
    assert len(plan) == 1
    page = plan[0]
    assert page["width"] == pytest.approx(612.0)
    assert page["height"] == pytest.approx(792.0)
    assert page["rotate"] == 0
    # The fixture is born-digital, so it already has text and is skipped.
    assert page["needs_ocr"] is False


def test_pages_with_text_are_skipped_by_default(engine, invoice_pdf_bytes):
    pdf, report = engine.ocr_pdf(invoice_pdf_bytes)
    assert report == {
        "pages": 1,
        "pages_with_layer": 0,
        "pages_skipped": 1,
        "lines": 0,
        "unmappable_chars": 0,
    }
    # Nothing to do means the original bytes are handed back untouched.
    assert pdf == invoice_pdf_bytes


def test_force_adds_a_layer_and_keeps_the_original_pages(engine, invoice_pdf_bytes):
    pdf, report = engine.ocr_pdf(invoice_pdf_bytes, skip_pages_with_text=False)
    assert report["pages_with_layer"] == 1
    assert report["lines"] >= 3
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > len(invoice_pdf_bytes)

    # The visible content survives: re-scanning the result still reads the text.
    again = engine.scan(pdf, name="layered.pdf")
    assert squash("INVOICE") in squash(again.text)


def test_layer_is_invisible_and_searchable(engine, invoice_pdf_bytes):
    pdf, _ = engine.ocr_pdf(invoice_pdf_bytes, skip_pages_with_text=False, compress=False)
    body = pdf.decode("latin-1")
    assert "3 Tr" in body, "text render mode 3 = invisible"
    assert "OcrustHelv" in body, "overlay font not registered"
    assert "INVOICE" in body, "text layer content missing"


def test_multi_page_pdfs_get_one_layer_per_page(engine, two_page_pdf_bytes):
    _, report = engine.ocr_pdf(two_page_pdf_bytes, skip_pages_with_text=False)
    assert report["pages"] == 2
    assert report["pages_with_layer"] == 2


def test_rotated_pages_are_handled(engine, rotated_pdf_bytes):
    plan = engine.plan_pdf(rotated_pdf_bytes, skip_pages_with_text=False)
    assert plan[0]["rotate"] == 90
    pdf, report = engine.ocr_pdf(rotated_pdf_bytes, skip_pages_with_text=False)
    assert report["pages_with_layer"] == 1
    assert pdf.startswith(b"%PDF")


def test_non_pdf_input_is_rejected(engine, image_fixtures):
    with pytest.raises((ValueError, ocrust.OcrustError)):
        engine.ocr_pdf(image_fixtures["png"])
