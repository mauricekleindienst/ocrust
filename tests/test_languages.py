"""Language coverage: the model must be honest about what it can spell."""

from __future__ import annotations

import pytest

import ocrust
from conftest import squash


def test_installed_model_covers_western_europe(engine):
    codes = {entry["code"] for entry in engine.languages}
    for code in ["en", "de", "fr", "es", "it", "pt", "nl", "sv", "da", "pl", "cs", "tr"]:
        assert code in codes, f"{code} missing from {sorted(codes)}"
    assert engine.charset_size > 1000


def test_known_languages_include_every_script():
    scripts = {entry["script"] for entry in ocrust.known_languages()}
    assert {"latin", "cyrillic", "greek", "han", "kana", "hangul", "arabic"} <= scripts


def test_requesting_a_covered_language_works(engine):
    ocr = ocrust.Ocr(models_dir=engine.models["detection"].rsplit("/", 1)[0], lang="de,fr")
    assert ocr.charset_size == engine.charset_size


def test_requesting_an_uncovered_language_fails_with_detail(engine):
    covered = {entry["code"] for entry in engine.languages}
    uncovered = [e["code"] for e in ocrust.known_languages() if e["code"] not in covered]
    if not uncovered:
        pytest.skip("the installed model covers every known language")
    directory = engine.models["detection"].rsplit("/", 1)[0]
    with pytest.raises(ocrust.OcrustError) as excinfo:
        ocrust.Ocr(models_dir=directory, lang=uncovered[0])
    message = str(excinfo.value)
    assert "cannot write" in message, message
    assert "ocrust languages" in message, message


def test_unknown_language_code_is_rejected(engine):
    directory = engine.models["detection"].rsplit("/", 1)[0]
    with pytest.raises((ValueError, ocrust.OcrustError), match="unknown language"):
        ocrust.Ocr(models_dir=directory, lang="klingon")


def test_partial_languages_report_missing_characters(engine):
    near = engine.partial_languages(0.5)
    for entry in near:
        assert 0.5 <= entry["ratio"] < 1.0
        assert entry["missing"], entry


def test_german_french_nordic_text_is_recognized(engine, german_pdf_bytes):
    doc = engine.scan(german_pdf_bytes, name="de.pdf")
    flat = squash(doc.text)
    for needle in ["Grüße", "München", "Beträge", "1.299,90", "déjà", "ça", "coûte", "Blåbær"]:
        assert squash(needle) in flat, f"missing {needle!r} in {doc.text!r}"
