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
    real = Path.replace

    # The write is committed by one rename; that is where a dropped share hits.
    def flaky(self, other):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionResetError(104, "the network name is no longer available")
        return real(self, other)

    monkeypatch.setattr(Path, "replace", flaky)
    _write(target, "text", retries=2, delay=0)
    assert attempts["n"] == 3
    assert target.read_text() == "text"
    assert not list(tmp_path.glob(".*.part"))


def test_write_gives_up_and_reports(tmp_path, monkeypatch):
    def always_fails(self, other):
        raise ConnectionResetError(104, "gone")

    monkeypatch.setattr(Path, "replace", always_fails)
    with pytest.raises(ConnectionResetError):
        _write(tmp_path / "out.txt", "text", retries=1, delay=0)
    assert not (tmp_path / "out.txt").exists()
    assert not list(tmp_path.glob(".*.part")), "a failed write leaves nothing behind"


def test_write_does_not_retry_a_real_error(tmp_path, monkeypatch):
    attempts = {"n": 0}

    def denied(self, other):
        attempts["n"] += 1
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Path, "replace", denied)
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


@pytest.mark.parametrize(("cores", "workers"), [(3, 3), (4, 4), (64, 16), (None, 4)])
def test_worker_default_scales_with_the_batch(monkeypatch, cores, workers):
    from argparse import Namespace

    from ocrust import cli

    monkeypatch.setattr(cli.os, "cpu_count", lambda: cores)
    # A single file keeps all cores on one page; a batch runs one page per
    # core, at most sixteen.
    assert cli._workers_for(Namespace(workers=None), 1) is None
    assert cli._workers_for(Namespace(workers=None), 5) == workers
    # An explicit choice always wins.
    assert cli._workers_for(Namespace(workers=2), 9) == 2


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


def test_an_engine_that_cannot_be_built_prints_one_line(tmp_path, capsys, monkeypatch):
    """An unbuildable engine used to come back as a Python traceback."""
    # The model search falls back to the environment, so a developer with
    # OCRUST_MODELS_DIR set would otherwise build an engine after all.
    monkeypatch.delenv("OCRUST_MODELS_DIR", raising=False)
    document = tmp_path / "x.pdf"
    document.write_bytes(b"%PDF-1.4\n")
    code = main(["scan", str(document), "--models", str(tmp_path / "nowhere")])
    assert code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err, err
    assert err.startswith("ocrust:"), err
    assert len(err.splitlines()) <= 4, err


def test_completion_scripts_cover_every_command_and_flag():
    """Generated from the parser, so a new flag cannot be forgotten."""
    from ocrust.cli import _COMPLETION_SHELLS, _completion_model

    commands, top = _completion_model()
    names = {c.name for c in commands}
    assert {"scan", "pdf", "ocr", "tiff", "completions", "doctor"} <= names
    assert any("--version" in o.flags for o in top)

    scan = next(c for c in commands if c.name == "scan")
    flags = {flag for option in scan.options for flag in option.flags}
    assert {"--skip-existing", "--watch", "--watch-interval", "--memory"} <= flags
    assert scan.takes_files
    fmt = next(o for o in scan.options if "--format" in o.flags)
    assert fmt.takes_value and "markdown" in fmt.choices
    watch = next(o for o in scan.options if o.flags == ["--watch"])
    assert not watch.takes_value

    # `ocrust completions <shell>` offers the shells, not file names.
    subject = next(c for c in commands if c.name == "completions")
    assert sorted(subject.words) == sorted(_COMPLETION_SHELLS)


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell"])
def test_a_completion_script_is_printed_and_mentions_the_commands(shell, capsys):
    assert main(["completions", shell]) == 0
    script = capsys.readouterr().out
    assert script.strip()
    for command in ("scan", "tiff", "doctor"):
        assert command in script
    # fish spells a long option `-l skip-existing`; everyone else writes it out.
    assert ("skip-existing" if shell == "fish" else "--skip-existing") in script


def test_an_unknown_shell_is_rejected():
    with pytest.raises(SystemExit) as raised:
        main(["completions", "csh"])
    assert raised.value.code == 2


def test_stdin_is_an_input_like_any_other(invoice_pdf, tmp_path, monkeypatch, capsys):
    """`curl … | ocrust scan -` has to work, and write somewhere sensible."""
    from ocrust.cli import STDIN, _expand_inputs

    # `-` is never asked about on disk: it is not on disk.
    files, missing = _expand_inputs([Path(STDIN)])
    assert [str(f) for f in files] == [STDIN]
    assert missing == []

    out = tmp_path / "out"
    monkeypatch.setattr(sys, "stdin", _FakeStdin(invoice_pdf.read_bytes()))
    assert main(["scan", "-", "-o", str(out), "-q"]) == 0
    # A document with no name of its own is written as stdin.<ext>.
    assert (out / "stdin.txt").read_text().strip()


class _FakeStdin:
    """Stdin as the CLI sees it: a text stream with a `.buffer`."""

    def __init__(self, data: bytes) -> None:
        self.buffer = _Bytes(data)

    def read(self) -> str:  # pragma: no cover - the buffer is what gets used
        return self.buffer.data.decode("utf-8", "replace")


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data


def test_skip_existing_leaves_finished_files_alone(invoice_pdf, tmp_path, capsys):
    """Resuming an interrupted batch must not redo the expensive part."""
    folder = tmp_path / "in"
    folder.mkdir()
    for name in ("a.pdf", "b.pdf"):
        (folder / name).write_bytes(invoice_pdf.read_bytes())
    out = tmp_path / "out"

    assert main(["scan", str(folder), "-o", str(out)]) == 0
    written = {p.name: p.read_text() for p in out.iterdir()}
    assert sorted(written) == ["a.txt", "b.txt"]

    # Mark one output so a re-run can be seen to have left it alone.
    (out / "a.txt").write_text("untouched")
    assert main(["scan", str(folder), "-o", str(out), "--skip-existing"]) == 0
    assert (out / "a.txt").read_text() == "untouched"
    report = capsys.readouterr().err
    assert "already written" in report


def test_skip_existing_knows_the_format_it_would_have_written(invoice_pdf, tmp_path):
    """A text run must not make a markdown run think it is done."""
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a.pdf").write_bytes(invoice_pdf.read_bytes())
    (folder / "b.pdf").write_bytes(invoice_pdf.read_bytes())
    out = tmp_path / "out"

    assert main(["scan", str(folder), "-o", str(out), "-q"]) == 0
    assert (
        main(["scan", str(folder), "-o", str(out), "-f", "markdown", "--skip-existing", "-q"]) == 0
    )
    assert sorted(p.name for p in out.iterdir()) == ["a.md", "a.txt", "b.md", "b.txt"]


def test_watch_needs_a_directory(tmp_path, capsys):
    lonely = tmp_path / "a.pdf"
    lonely.write_bytes(b"%PDF-1.4\n")
    assert main(["scan", str(lonely), "--watch"]) == 2
    assert "needs a directory" in capsys.readouterr().err
    assert main(["scan", "-", "--watch"]) == 2
    assert "not stdin" in capsys.readouterr().err


def test_pdf_converts_a_whole_folder_into_one_file(engine, tmp_path):
    """`ocrust pdf folder/ -o out.pdf` merges everything readable it finds."""
    from PIL import Image, ImageDraw

    folder = tmp_path / "scans"
    folder.mkdir()
    for name in ("one.png", "two.tiff"):
        page = Image.new("RGB", (1200, 1600), "white")
        ImageDraw.Draw(page).text((80, 200), f"SEITE {name}", fill="black")
        page.save(folder / name)

    out = tmp_path / "merged.pdf"
    assert main(["pdf", str(folder), "-o", str(out), "-q"]) == 0
    assert out.read_bytes().startswith(b"%PDF")


def test_pdf_of_several_inputs_needs_an_output_name(engine, tmp_path):
    """Two inputs and one PDF: where it goes is not something to guess."""
    from PIL import Image

    for name in ("a.png", "b.png"):
        Image.new("RGB", (400, 300), "white").save(tmp_path / name)
    assert main(["pdf", str(tmp_path / "a.png"), str(tmp_path / "b.png"), "-q"]) == 2


def _labelled_pdf(path: Path, text: str) -> None:
    """A one-page PDF reading `text`, in a font the engine reads reliably."""
    from conftest import _pdf_with_text

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_pdf_with_text([(text, 30)]))


def test_a_folder_walk_mirrors_its_subfolders(engine, tmp_path):
    """Two invoices with one file name in two month folders stay two invoices.

    Outputs were named after the file alone, so the second silently replaced
    the first while the summary counted both as written — and with
    --skip-existing the second was never read at all.
    """
    archive = tmp_path / "archive"
    _labelled_pdf(archive / "2024" / "rechnung.pdf", "RECHNUNG ZWEITAUSENDVIER")
    _labelled_pdf(archive / "2025" / "rechnung.pdf", "RECHNUNG ZWEITAUSENDFUENF")
    out = tmp_path / "out"

    assert main(["scan", str(archive), "-o", str(out), "-q", "--skip-existing"]) == 0
    first = (out / "2024" / "rechnung.txt").read_text(encoding="utf-8")
    second = (out / "2025" / "rechnung.txt").read_text(encoding="utf-8")
    assert "VIER" in first.upper(), first
    assert "FUENF" in second.upper(), second
    assert not (out / "rechnung.txt").exists()


def test_inputs_that_would_share_an_output_are_refused(engine, tmp_path, capsys):
    """What mirroring cannot separate is refused by name, and the rest is read."""
    folder = tmp_path / "scans"
    _labelled_pdf(folder / "rechnung.pdf", "ERSTE")
    (folder / "rechnung.tiff").write_bytes(b"")  # the same stem, another format
    _labelled_pdf(folder / "lieferschein.pdf", "ZWEITE")
    out = tmp_path / "out"

    assert main(["scan", str(folder), "-o", str(out), "-q"]) == 1
    assert not (out / "rechnung.txt").exists(), "neither of the pair may be written"
    assert (out / "lieferschein.txt").exists(), "everything else is still read"
    message = " ".join(capsys.readouterr().err.split())
    assert "rechnung.pdf" in message
    assert "rechnung.tiff" in message
    assert "would both be written" in message


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    """Outputs replace the old file in one step, or not at all.

    A truncated output used to be left behind by an interrupted run, and
    --skip-existing then took it for finished work on every run after.
    """
    target = tmp_path / "page.txt"
    target.write_text("the previous, complete output", encoding="utf-8")

    def interrupted(self, other):
        raise KeyboardInterrupt

    monkeypatch.setattr(Path, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _write(target, "the new output")
    monkeypatch.undo()

    assert target.read_text(encoding="utf-8") == "the previous, complete output"
    _write(target, "the new output")
    assert target.read_text(encoding="utf-8") == "the new output"
    assert not list(tmp_path.glob(".*.part")), "no temporary file left behind"


def test_ocr_of_a_locked_pdf_fails_instead_of_copying_it(engine, tmp_path, invoice_pdf):
    """It used to exit 0, print "0 of 0 pages layered" and write a byte-for-byte
    copy of the input — success, as far as any script could tell."""
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter(clone_from=pypdf.PdfReader(invoice_pdf))
    writer.encrypt(user_password="geheim", owner_password="owner", algorithm="AES-256")
    locked = tmp_path / "locked.pdf"
    with locked.open("wb") as handle:
        writer.write(handle)
    out = tmp_path / "out.pdf"

    assert main(["ocr", str(locked), "-o", str(out), "-q"]) == 1
    assert not out.exists()
    assert main(["ocr", str(locked), "-o", str(out), "-q", "--password", "geheim"]) == 0
    assert out.read_bytes().startswith(b"%PDF")
