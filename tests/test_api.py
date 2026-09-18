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
