"""Public API tests that do not need model files."""

from __future__ import annotations

import json
import os

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


def test_models_cache_dir_is_absolute():
    assert os.path.isabs(ocrust.models_cache_dir())


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


def test_scan_rejects_unsupported_input_types(engine):
    with pytest.raises(TypeError):
        engine.scan(42)


def test_scan_reports_missing_file(engine):
    with pytest.raises((OSError, ocrust.OcrustError)):
        engine.scan("/definitely/not/here.png")


def test_scan_reports_unreadable_bytes(engine):
    with pytest.raises(ValueError):
        engine.scan(b"not an image at all", name="junk.bin")
