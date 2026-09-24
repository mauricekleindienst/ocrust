"""Shared fixtures.

The test suite generates its inputs instead of committing binary fixtures: a
one-page PDF with known text, rendered by the same code path users hit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _winansi_literal(text: str) -> str:
    """Encodes `text` as a WinAnsi PDF literal, matching the font declaration.

    Writing raw UTF-8 into the stream would render mojibake, which OCR then
    faithfully reads back — so accented fixtures must be encoded properly.
    """
    high = {
        "\u20ac": 0x80,
        "\u201a": 0x82,
        "\u0192": 0x83,
        "\u201e": 0x84,
        "\u2026": 0x85,
        "\u2020": 0x86,
        "\u2021": 0x87,
        "\u02c6": 0x88,
        "\u2030": 0x89,
        "\u0160": 0x8A,
        "\u2039": 0x8B,
        "\u0152": 0x8C,
        "\u017d": 0x8E,
        "\u2018": 0x91,
        "\u2019": 0x92,
        "\u201c": 0x93,
        "\u201d": 0x94,
        "\u2022": 0x95,
        "\u2013": 0x96,
        "\u2014": 0x97,
        "\u02dc": 0x98,
        "\u2122": 0x99,
        "\u0161": 0x9A,
        "\u203a": 0x9B,
        "\u0153": 0x9C,
        "\u017e": 0x9E,
        "\u0178": 0x9F,
    }
    out = []
    for ch in text:
        if ch in "()\\":
            out.append("\\" + ch)
            continue
        code = ord(ch)
        if code < 0x80:
            out.append(ch)
        elif ch in high:
            out.append(f"\\{high[ch]:03o}")
        elif 0xA0 <= code <= 0xFF:
            out.append(f"\\{code:03o}")
        else:
            raise AssertionError(f"fixture character {ch!r} needs an embedded font")
    return "".join(out)


def _pdf_with_text(lines: list[tuple[str, int]], pages: int = 1, rotate: int = 0) -> bytes:
    """A minimal PDF containing `lines` of Helvetica text on each page."""
    content = ["BT"]
    y = 700
    for text, size in lines:
        content.append(f"/F1 {size} Tf 1 0 0 1 60 {y} Tm ({_winansi_literal(text)}) Tj")
        y -= 70
    content.append("ET")
    return _pdf_from_stream("\n".join(content), pages=pages, rotate=rotate)


def _pdf_from_stream(stream: str, pages: int = 1, rotate: int = 0) -> bytes:
    """Wraps one content stream into a PDF of `pages` identical pages."""
    return _pdf_from_streams([stream] * pages, rotate=rotate)


def _pdf_from_streams(streams: list[str], rotate: int = 0) -> bytes:
    """A PDF with a page for each content stream."""
    # Object layout: catalog, page tree, then one page + content per page,
    # with the font last.
    first_page_obj = 3
    pages = len(streams)
    page_ids = [first_page_obj + 2 * i for i in range(pages)]
    font_id = first_page_obj + 2 * pages
    rotate_entry = f" /Rotate {rotate}" if rotate else ""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [{}] /Count {} >>".format(
            " ".join(f"{pid} 0 R" for pid in page_ids), pages
        ),
    ]
    for pid, stream in zip(page_ids, streams, strict=True):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]{rotate_entry} "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {pid + 1} 0 R >>"
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objects.append(
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    pdf = "%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n{body}\nendobj\n"
    xref_at = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for off in offsets:
        pdf += f"{off:010} 00000 n \n"
    pdf += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
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


def _pdf_with_table(rows: list[list[tuple[str, int]]], size: int = 26) -> bytes:
    """A PDF whose text sits in columns: each row is `(text, x)` pairs."""
    content = ["BT"]
    y = 700
    for row in rows:
        for text, x in row:
            content.append(f"/F1 {size} Tf 1 0 0 1 {x} {y} Tm ({_winansi_literal(text)}) Tj")
        y -= 44
    content.append("ET")
    stream = "\n".join(content)
    return _pdf_from_stream(stream)


@pytest.fixture(scope="session")
def table_pdf(tmp_path_factory) -> Path:
    """An invoice: a heading, a four-column table, a closing line.

    The columns are set far enough apart to be gutters and the words inside a
    cell a normal space apart, which is the geometry a table has to be read from.
    """
    data = _pdf_with_table(
        [
            [("RECHNUNG 2026-0042", 60)],
            [("Position", 60), ("Menge", 250), ("Preis", 380), ("Summe", 490)],
            [("Widget A", 60), ("12", 250), ("49,90", 380), ("598,80", 490)],
            [("Widget B", 60), ("3", 250), ("233,70", 380), ("701,10", 490)],
            [("Kabel C", 60), ("7", 250), ("12,50", 380), ("87,50", 490)],
            [("Vielen Dank fuer Ihren Auftrag.", 60)],
        ]
    )
    path = tmp_path_factory.mktemp("docs") / "table.pdf"
    path.write_bytes(data)
    return path


@pytest.fixture(scope="session")
def invoice_pdf(tmp_path_factory, invoice_pdf_bytes: bytes) -> Path:
    path = tmp_path_factory.mktemp("docs") / "invoice.pdf"
    path.write_bytes(invoice_pdf_bytes)
    return path


@pytest.fixture(scope="session")
def german_pdf_bytes() -> bytes:
    """A page with German, French and Nordic text, all WinAnsi-encodable."""
    return _pdf_with_text(
        [
            ("Grüße aus München", 30),
            ("Beträge: 1.299,90 EUR", 26),
            ("Français: déjà vu, ça coûte", 26),
            ("Blåbær på Fyn", 26),
        ]
    )


@pytest.fixture(scope="session")
def two_page_pdf_bytes() -> bytes:
    return _pdf_with_text([("INVOICE 2026-0042", 34), ("Total: 199.90 EUR", 28)], pages=2)


@pytest.fixture(scope="session")
def rotated_pdf_bytes() -> bytes:
    return _pdf_with_text([("Rotated page text", 30)], rotate=90)


@pytest.fixture(scope="session")
def image_fixtures(tmp_path_factory, invoice_pdf_bytes: bytes) -> dict[str, Path]:
    """The invoice page rendered once, then written in every raster format.

    Rendering goes through ocrust itself, so the pixels are identical for every
    format and differences can only come from the codec.
    """
    from PIL import Image

    import ocrust

    directory = tmp_path_factory.mktemp("formats")
    source = directory / "source.pdf"
    source.write_bytes(invoice_pdf_bytes)

    engine = ocrust.Ocr(models_dir=os.environ.get("OCRUST_MODELS_DIR"), keep_page_images=True)
    tiff_bytes, _ = engine.to_tiff(source)
    master = directory / "master.tiff"
    master.write_bytes(tiff_bytes)

    page = Image.open(master).convert("RGB")
    written: dict[str, Path] = {"tiff": master}
    for suffix, kwargs in {
        "png": {},
        "jpg": {"quality": 92},
        "webp": {"quality": 92},
        "bmp": {},
        "ppm": {},
        "tga": {},
        "gif": {},
    }.items():
        target = directory / f"page.{suffix}"
        page.save(target, **kwargs)
        written[suffix] = target

    # A genuine multi-page TIFF, built by ocrust from a two-page PDF.
    multi_source = directory / "two.pdf"
    multi_source.write_bytes(_pdf_with_text([("Page one text", 30)], pages=2))
    multi_bytes, _ = engine.to_tiff(multi_source)
    multi = directory / "multi.tiff"
    multi.write_bytes(multi_bytes)
    written["tiff-multi"] = multi
    return written


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
