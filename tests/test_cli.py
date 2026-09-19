"""CLI tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ocrust.cli import _build_parser, _expand_inputs, _io_retries, _parse_pages, _write, main


def test_page_spec_parsing():
    assert _parse_pages(None) is None
    assert _parse_pages("1") == [0]
    assert _parse_pages("1,3-5") == [0, 2, 3, 4]
    assert _parse_pages("2, 2 ,1") == [0, 1]
    with pytest.raises(SystemExit):
        _parse_pages("0")
    with pytest.raises(SystemExit):
        _parse_pages("5-2")
    # A typo must not come back as a traceback, and it is a bad argument: the
    # documented exit code for that is 2, not 1.
    for bad in ("1-x", "3-", "abc", "-4", "0", "5-2"):
        with pytest.raises(SystemExit) as raised:
            _parse_pages(bad)
        assert raised.value.code == 2, bad


def test_quiet_is_available_wherever_a_summary_is_printed():
    """`find | xargs -P4 ocrust ocr` needs to be able to shut it up."""
    parser = _build_parser()
    assert parser.parse_args(["scan", "a.pdf", "-q"]).quiet
    assert parser.parse_args(["ocr", "a.pdf", "-q"]).quiet
    assert parser.parse_args(["pdf", "a.png", "--quiet"]).quiet
    assert parser.parse_args(["tiff", "a.pdf", "-q"]).quiet
    assert not parser.parse_args(["ocr", "a.pdf"]).quiet


def test_expand_inputs_explains_what_is_missing(tmp_path):
    """A batch over a share needs the reason, not just "no"."""
    files, missing = _expand_inputs([tmp_path / "gone.pdf"])
    assert files == []
    assert missing == [(tmp_path / "gone.pdf", "no such file")]

    empty = tmp_path / "empty"
    empty.mkdir()
    _, missing = _expand_inputs([empty])
    assert missing[0][1] == "no readable files in this directory"

    _, missing = _expand_inputs([tmp_path / "*.tiff"])
    assert missing[0][1] == "nothing matched this pattern"


def test_expand_inputs_keeps_unc_paths_intact():
    """A Windows share path must survive the expansion unchanged.

    On POSIX it is simply a file that does not exist, which is enough to prove
    the string is not mangled on the way through.
    """
    unc = Path(r"\\fileserver\scans\invoice.pdf")
    files, missing = _expand_inputs([unc])
    assert files == []
    assert missing[0][0] == unc


def test_write_retries_a_dropped_share(tmp_path, monkeypatch):
    target = tmp_path / "out.txt"
    attempts = {"n": 0}
    real = Path.write_text

    def flaky(self, data, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionResetError(104, "the network name is no longer available")
        return real(self, data, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky)
    _write(target, "text", retries=2, delay=0)
    assert attempts["n"] == 3
    assert target.read_text() == "text"


def test_write_gives_up_and_reports(tmp_path, monkeypatch):
    def always_fails(self, data, **kwargs):
        raise ConnectionResetError(104, "gone")

    monkeypatch.setattr(Path, "write_text", always_fails)
    with pytest.raises(ConnectionResetError):
        _write(tmp_path / "out.txt", "text", retries=1, delay=0)


def test_write_does_not_retry_a_real_error(tmp_path, monkeypatch):
    attempts = {"n": 0}

    def denied(self, data, **kwargs):
        attempts["n"] += 1
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Path, "write_text", denied)
    with pytest.raises(PermissionError):
        _write(tmp_path / "out.txt", "text", retries=3, delay=0)
    assert attempts["n"] == 1, "a permission error will not fix itself"


def test_io_retries_defaults_to_two():
    from argparse import Namespace

    assert _io_retries(Namespace()) == 2
    assert _io_retries(Namespace(io_retries=None)) == 2
    assert _io_retries(Namespace(io_retries=0)) == 0
    assert _io_retries(Namespace(io_retries=5)) == 5


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
    assert [p for p, _ in missing] == [tmp_path / "*.tiff"]


def test_a_reader_that_leaves_is_not_an_error():
    """A reader that goes away before reading must not end in a traceback."""
    # Next to the interpreter running the tests first: a venv's script directory
    # is not always on PATH.
    beside = Path(sys.executable).with_name("ocrust")
    exe = str(beside) if beside.exists() else shutil.which("ocrust")
    if os.name != "posix" or exe is None:
        pytest.skip("needs the installed console script and a POSIX shell")
    # `| true` closes the pipe before the child writes a byte; `head -n` would
    # race with it, and BSD `head -0` is an error in itself.
    result = subprocess.run(  # noqa: S602 - the command is built here, not by a user
        f"{exe} doctor | true", shell=True, capture_output=True
    )
    assert b"Traceback" not in result.stderr, result.stderr.decode()
    assert b"BrokenPipe" not in result.stderr, result.stderr.decode()


def test_expand_inputs_walks_a_recursive_pattern(tmp_path):
    """`ocrust scan "archive/**/*.pdf"` has to descend, not come back empty."""
    nested = tmp_path / "2026" / "q1"
    nested.mkdir(parents=True)
    deep = nested / "invoice.pdf"
    deep.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "top.pdf").write_bytes(b"%PDF-1.4\n")

    files, missing = _expand_inputs([tmp_path / "**" / "*.pdf"])
    # `**` matches zero directories too, so the top-level file comes along —
    # exactly what a shell with globstar would have passed in.
    assert files == sorted([deep, tmp_path / "top.pdf"])
    assert missing == []


def test_expand_inputs_reports_an_empty_directory(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    files, missing = _expand_inputs([empty])
    assert files == []
    assert [p for p, _ in missing] == [empty]


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


def test_colour_is_off_for_a_pipe_and_can_be_forced(monkeypatch):
    from ocrust import cli

    class Tty:
        def isatty(self):
            return True

    class Pipe:
        def isatty(self):
            return False

    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert cli._colourful(Tty())
    assert not cli._colourful(Pipe())
    assert cli._paint("x", "red", stream=Pipe()) == "x"
    assert "\033[" in cli._paint("x", "red", stream=Tty())

    monkeypatch.setenv("NO_COLOR", "1")
    assert not cli._colourful(Tty())
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert cli._colourful(Pipe()), "FORCE_COLOR wins, for a CI log that renders ANSI"


def test_counts_and_durations_read_like_english():
    from ocrust import cli

    assert cli._count(1, "page") == "1 page"
    assert cli._count(0, "page") == "0 pages"
    assert cli._count(2, "line") == "2 lines"
    assert cli._duration(464) == "464 ms"
    assert cli._duration(1900) == "1.9 s"
    assert cli._duration(125_000).startswith("2 min")


def test_long_messages_are_folded_to_the_terminal(monkeypatch):
    from ocrust import cli

    monkeypatch.setenv("COLUMNS", "60")
    words = " ".join(f"code{i}" for i in range(40))
    folded = cli._wrap(words, 8).splitlines()
    assert len(folded) > 1
    assert all(len(line) <= cli._width() for line in folded)
    assert folded[1].startswith(" " * 8), folded[1]
    # A short message is left alone.
    assert cli._wrap("short", 8) == "short"


def test_an_engine_that_cannot_be_built_prints_one_line(tmp_path, capsys):
    """An unbuildable engine used to come back as a Python traceback."""
    document = tmp_path / "x.pdf"
    document.write_bytes(b"%PDF-1.4\n")
    code = main(["scan", str(document), "--models", str(tmp_path / "nowhere")])
    assert code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err, err
    assert err.startswith("ocrust:"), err
    assert len(err.splitlines()) <= 4, err
