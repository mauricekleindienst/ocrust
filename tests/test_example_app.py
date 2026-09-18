"""The example web application, exercised over real HTTP.

A demo that rots is worse than no demo, so it is tested like the rest: the server
is started on an ephemeral port, a generated PDF is posted to it, and the answers
are checked.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

app = pytest.importorskip("app")


@pytest.fixture(scope="module")
def server(engine):  # noqa: ARG001  (skips the module when no models are installed)
    """The demo server on a free port, shut down afterwards."""
    instance = app.serve("127.0.0.1", 0)
    yield f"http://127.0.0.1:{instance.server_port}"
    instance.shutdown()


def post_file(url: str, name: str, data: bytes) -> tuple[int, bytes, str]:
    """Posts `data` as a multipart file upload, the way a browser would."""
    boundary = uuid.uuid4().hex
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    request = urllib.request.Request(  # noqa: S310  (a localhost URL this test built)
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get("Content-Type", "")


def test_multipart_reader_finds_the_file():
    body = (
        b"--X\r\n"
        b'Content-Disposition: form-data; name="file"; filename="scan.png"\r\n'
        b"Content-Type: image/png\r\n\r\n"
        b"\x89PNG\r\n\x1a\n" + b"payload"
        b"\r\n--X--\r\n"
    )
    name, data = app.parse_multipart(body, "multipart/form-data; boundary=X")
    assert name == "scan.png"
    assert data == b"\x89PNG\r\n\x1a\npayload"

    with pytest.raises(ValueError, match="multipart"):
        app.parse_multipart(b"", "application/json")
    with pytest.raises(ValueError, match="no file"):
        app.parse_multipart(b"--X\r\n\r\nnothing\r\n--X--\r\n", "multipart/form-data; boundary=X")


def test_the_page_and_the_language_list_are_served(server):
    with urllib.request.urlopen(server + "/", timeout=30) as response:  # noqa: S310
        page = response.read().decode()
    assert "<title>ocrust</title>" in page
    assert "/scan" in page and "/pdf" in page

    with urllib.request.urlopen(server + "/languages", timeout=120) as response:  # noqa: S310
        payload = json.load(response)
    assert payload["charset"] > 1000
    assert any(item["code"] == "en" for item in payload["languages"])


def test_scan_returns_text_boxes_and_every_export(server, invoice_pdf_bytes):
    status, body, content_type = post_file(server + "/scan", "invoice.pdf", invoice_pdf_bytes)
    assert status == 200, body[:300]
    assert content_type.startswith("application/json")
    payload = json.loads(body)

    assert "INVOICE" in payload["exports"]["text"].upper()
    assert payload["confidence"] > 0.8
    assert len(payload["pages"]) == 1

    lines = payload["pages"][0]["lines"]
    assert lines and all(len(line["box"]) == 4 for line in lines)
    assert all(line["box"][2] > line["box"][0] for line in lines), "boxes have width"
    for fmt in ("text", "markdown", "json", "hocr", "alto", "csv"):
        assert payload["exports"][fmt].strip(), f"{fmt} export is empty"


def test_search_reports_matching_boxes(server, invoice_pdf_bytes):
    status, body, _ = post_file(server + "/scan?q=invoice", "invoice.pdf", invoice_pdf_bytes)
    assert status == 200
    matches = json.loads(body)["matches"]
    assert matches, "expected a hit for 'invoice'"
    assert matches[0]["page"] == 0
    x0, _, x1, _ = matches[0]["box"]
    assert x1 > x0


def test_pdf_endpoint_builds_a_searchable_pdf_from_an_image(server, image_fixtures):
    data = image_fixtures["png"].read_bytes()
    status, body, content_type = post_file(server + "/pdf", "page.png", data)
    assert status == 200, body[:300]
    assert content_type == "application/pdf"
    assert body.startswith(b"%PDF-")
    assert b"3 Tr" in body, "the text layer must be invisible"


def test_pdf_endpoint_leaves_a_born_digital_pdf_alone(server, invoice_pdf_bytes):
    """The fixture PDF already has text, so there is nothing to add."""
    status, body, content_type = post_file(server + "/pdf", "invoice.pdf", invoice_pdf_bytes)
    assert status == 200, body[:300]
    assert content_type == "application/pdf"
    assert body == invoice_pdf_bytes, "an already searchable PDF comes back untouched"


def test_a_broken_upload_is_rejected_cleanly(server):
    status, body, _ = post_file(server + "/scan", "truncated.png", b"\x89PNG\r\n\x1a\nnope")
    assert status == 422
    assert "error" in json.loads(body)

    status, body, _ = post_file(server + "/nowhere", "x.png", b"data")
    assert status == 404
