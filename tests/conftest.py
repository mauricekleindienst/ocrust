"""Shared fixtures.

The test suite generates its inputs instead of committing binary fixtures: a
one-page PDF with known text, rendered by the same code path users hit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _pdf_with_text(lines: list[tuple[str, int]]) -> bytes:
    """A minimal single-page PDF containing `lines` of Helvetica text."""
    content = ["BT"]
    y = 700
    for text, size in lines:
        content.append(f"/F1 {size} Tf 1 0 0 1 60 {y} Tm ({text}) Tj")
        y -= 70
    content.append("ET")
    stream = "\n".join(content)

    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = "%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n{body}\nendobj\n"
    xref_at = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for off in offsets:
        pdf += f"{off:010} 00000 n \n"
    pdf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    )
    return pdf.encode()


@pytest.fixture(scope="session")
def invoice_pdf_bytes() -> bytes:
    return _pdf_with_text(
        [
            ("INVOICE 2026-0042", 34),
            ("Total: 199.90 EUR", 28),
            ("Thank you for your business", 22),
        ]
    )


@pytest.fixture(scope="session")
def invoice_pdf(tmp_path_factory, invoice_pdf_bytes: bytes) -> Path:
    path = tmp_path_factory.mktemp("docs") / "invoice.pdf"
    path.write_bytes(invoice_pdf_bytes)
    return path


@pytest.fixture(scope="session")
def engine():
    """A shared engine, or a skip when no models are installed."""
    import ocrust

    try:
        return ocrust.Ocr(models_dir=os.environ.get("OCRUST_MODELS_DIR"))
    except ocrust.OcrustError as exc:
        pytest.skip(f"no OCR models available: {exc}")


def squash(text: str) -> str:
    """Lowercase, whitespace-free text for tolerant comparisons."""
    return "".join(text.lower().split())
