"""CLI tests."""

from __future__ import annotations

import json

import pytest

from ocrust.cli import _expand_inputs, _parse_pages, main


def test_page_spec_parsing():
    assert _parse_pages(None) is None
    assert _parse_pages("1") == [0]
    assert _parse_pages("1,3-5") == [0, 2, 3, 4]
    assert _parse_pages("2, 2 ,1") == [0, 1]
    with pytest.raises(SystemExit):
        _parse_pages("0")
    with pytest.raises(SystemExit):
        _parse_pages("5-2")


def test_expand_inputs_walks_directories(tmp_path):
    (tmp_path / "sub").mkdir()
    keep = [tmp_path / "a.png", tmp_path / "sub" / "b.pdf"]
    for path in keep:
        path.write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("skipped")

    files, missing = _expand_inputs([tmp_path])
    assert files == sorted(keep)
    assert missing == []


def test_expand_inputs_handles_patterns_and_duplicates(tmp_path):
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    for path in (first, second):
        path.write_bytes(b"x")

    files, missing = _expand_inputs([tmp_path / "*.png", first])
    assert files == [first, second], "a repeated file is read once, in a stable order"
    assert missing == []

    files, missing = _expand_inputs([tmp_path / "*.tiff"])
    assert files == []
    assert missing == [tmp_path / "*.tiff"]


def test_expand_inputs_reports_an_empty_directory(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    files, missing = _expand_inputs([empty])
    assert files == []
    assert missing == [empty]


def test_scan_reads_a_directory(engine, invoice_pdf, tmp_path):
    folder = tmp_path / "archive"
    folder.mkdir()
    (folder / "invoice.pdf").write_bytes(invoice_pdf.read_bytes())
    outdir = tmp_path / "out"

    code = main(["scan", str(folder), "-f", "text", "-o", str(outdir), "-q"])
    assert code == 0
    assert (outdir / "invoice.txt").read_text().strip()


def test_doctor_reports_json(capsys):
    code = main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "ocrust" in payload
    assert code in (0, 1)


def test_scan_missing_file_exits_two(capsys):
    assert main(["scan", "/nope/missing.png"]) == 2
    assert "no such file" in capsys.readouterr().err


def test_scan_writes_requested_format(engine, invoice_pdf, tmp_path, capsys):
    out = tmp_path / "out.md"
    code = main(["scan", str(invoice_pdf), "-f", "markdown", "-o", str(out), "-q"])
    assert code == 0
    assert "INVOICE" in out.read_text().upper()


def test_scan_batch_writes_one_file_per_input(engine, invoice_pdf, tmp_path):
    outdir = tmp_path / "results"
    code = main(["scan", str(invoice_pdf), str(invoice_pdf), "-f", "json", "-o", str(outdir), "-q"])
    assert code == 0
    written = list(outdir.glob("*.json"))
    assert written, "expected json output"
    assert json.loads(written[0].read_text())["pages"]


def test_pdf_command_writes_searchable_pdf(engine, invoice_pdf, tmp_path):
    out = tmp_path / "searchable.pdf"
    assert main(["pdf", str(invoice_pdf), "-o", str(out)]) == 0
    assert out.read_bytes().startswith(b"%PDF")


def test_suffixless_output_is_treated_as_a_directory(engine, invoice_pdf, tmp_path):
    # `-o out` must produce out/invoice.txt, not a file called "out", so that
    # scanning again with another format does not overwrite the first result.
    outdir = tmp_path / "out"
    assert main(["scan", str(invoice_pdf), "-f", "text", "-o", str(outdir), "-q"]) == 0
    assert main(["scan", str(invoice_pdf), "-f", "markdown", "-o", str(outdir), "-q"]) == 0
    assert outdir.is_dir()
    assert (outdir / "invoice.txt").exists()
    assert (outdir / "invoice.md").exists()


def test_output_with_suffix_stays_a_file(engine, invoice_pdf, tmp_path):
    target = tmp_path / "result.txt"
    assert main(["scan", str(invoice_pdf), "-o", str(target), "-q"]) == 0
    assert target.is_file()


def test_batch_output_rejects_a_file_path(capsys, invoice_pdf, tmp_path):
    second = tmp_path / "copy.pdf"
    second.write_bytes(invoice_pdf.read_bytes())
    code = main(["scan", str(invoice_pdf), str(second), "-o", str(tmp_path / "x.txt")])
    assert code == 2
    assert "must be a directory" in capsys.readouterr().err


def test_a_repeated_input_is_read_once(engine, invoice_pdf, capsys):
    code = main(["scan", str(invoice_pdf), str(invoice_pdf), "-q"])
    assert code == 0
    assert capsys.readouterr().out.upper().count("INVOICE") == 1


def test_worker_default_scales_with_the_batch():
    from argparse import Namespace

    from ocrust.cli import _workers_for

    # A single file keeps all cores on one page; a batch runs pages in parallel.
    assert _workers_for(Namespace(workers=None), 1) is None
    assert _workers_for(Namespace(workers=None), 5) == 4
    # An explicit choice always wins.
    assert _workers_for(Namespace(workers=2), 9) == 2
