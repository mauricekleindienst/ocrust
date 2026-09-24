"""Command line interface: ``ocrust``.

ocrust scan invoice.pdf                 # text to stdout
ocrust scan *.jpg -f markdown -o out/   # batch, one file per input
ocrust ocr scan.pdf -o scan.ocr.pdf     # add a text layer, keep the pages
ocrust pdf photo.jpg -o photo.pdf       # build a searchable PDF from an image
ocrust tiff scan.pdf --gray             # deskewed multi-page TIFF
ocrust markdown archive/ -o wissen/     # every document as a Markdown note
ocrust languages                        # what the installed model covers
ocrust doctor                           # what is installed, what is missing
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import textwrap
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import (
    FORMATS,
    READABLE_SUFFIXES,
    Ocr,
    OcrustError,
    __version__,
    _glob,
    markings,
    runtime_info,
    terms,
)

#: The input that means "read the document from standard input", the convention
#: every unix tool uses. A file really called `-` is reachable as `./-`.
STDIN = "-"

_EXTENSIONS = {
    "text": "txt",
    "markdown": "md",
    "json": "json",
    "hocr": "hocr.html",
    "alto": "alto.xml",
    "csv": "csv",
}

#: The palette, in three depths: 24-bit, 256 colours, and the eight every
#: terminal has. Rust orange (#F74C00) is the accent — it is this project's
#: colour — with the darker #B7410E for anything secondary. Green, amber and red
#: stay semantic: they say how much a number can be trusted, not whose tool this is.
_STYLES = {
    "bold": ("1", "1", "1"),
    "dim": ("2", "2", "2"),
    "rust": ("38;2;247;76;0", "38;5;208", "33"),
    "ember": ("38;2;183;65;14", "38;5;130", "33"),
    "green": ("38;2;63;185;80", "38;5;34", "32"),
    "amber": ("38;2;210;153;34", "38;5;178", "33"),
    "red": ("38;2;248;81;73", "38;5;203", "31"),
}


def _depth(stream: object) -> int:
    """How many colours `stream` can take: 0, 8, 256 or 16.7 million.

    `COLORTERM` is how terminals announce 24-bit support; `TERM` carries the
    256-colour hint. `FORCE_COLOR=truecolor` asks for the full palette outright.
    """
    forced = os.environ.get("FORCE_COLOR")
    if forced in (None, "", "0"):
        if os.environ.get("NO_COLOR") is not None or os.environ.get("TERM") == "dumb":
            return 0
        try:
            if not stream.isatty():  # type: ignore[attr-defined]
                return 0
        except Exception:
            return 0
    if forced in ("truecolor", "24bit") or os.environ.get("COLORTERM") in ("truecolor", "24bit"):
        return 16_777_216
    if "256" in os.environ.get("TERM", "") or forced == "256":
        return 256
    return 16_777_216 if forced else 8


def _colourful(stream: object) -> bool:
    """Whether to write escape codes to `stream` at all.

    A pipe, a log file and `NO_COLOR` all mean no; `FORCE_COLOR` overrides the
    lot, which is what a CI job that renders ANSI needs.
    """
    return _depth(stream) > 0


def _paint(text: str, *styles: str, stream: object | None = None) -> str:
    """`text` in `styles`, or unchanged when nobody is there to see them."""
    target = stream if stream is not None else sys.stderr
    depth = _depth(target)
    if not styles or depth == 0:
        return text
    tier = 0 if depth > 256 else (1 if depth == 256 else 2)
    codes = ";".join(_STYLES[name][tier] for name in styles)
    return f"\033[{codes}m{text}\033[0m"


def _width() -> int:
    """Usable line width: the terminal's, kept between sane bounds."""
    return max(48, min(shutil.get_terminal_size((100, 24)).columns, 100))


def _wrap(text: str, indent: int) -> str:
    """`text` folded to the terminal, with every line after the first indented."""
    return textwrap.fill(
        text,
        width=_width(),
        subsequent_indent=" " * indent,
        initial_indent=" " * indent,
        break_long_words=False,
        break_on_hyphens=False,
    )[indent:]


def _fold(text: str, indent: int) -> str:
    """Folds a value to the terminal, breaking a long path on its separators.

    `textwrap` needs spaces to work with, and a model path inside a virtualenv
    has none — so it would run off the screen instead of wrapping. A directory
    boundary is the one place a path may be broken without becoming unreadable.
    """
    width = max(24, _width() - indent)
    if len(text) <= width:
        return text
    if " " in text:
        return _wrap(text, indent)
    lines: list[str] = []
    current = ""
    for index, part in enumerate(text.split("/")):
        piece = part if index == 0 else f"/{part}"
        if current and len(current) + len(piece) > width:
            lines.append(current)
            current = piece
        else:
            current += piece
    lines.append(current)
    return ("\n" + " " * indent).join(lines)


def _fail(message: str) -> None:
    """One message, on stderr, the way every other command-line tool does it.

    Wrapped: the engine's own errors list things — the 35 language codes it
    knows, the places it looked for a model — and a 350-column line is not a
    message, it is a wall.
    """
    _tell(f"{_paint('ocrust:', 'red', 'bold')} {_wrap(message, 8)}")


def _note(message: str) -> None:
    """A quiet aside: part of the report, not the result."""
    _tell(_paint(message, "dim"))


def _tell(text: str = "", end: str = "\n", flush: bool = False) -> None:
    """A line on stderr that a closed pipe cannot turn into a crash: when
    `2>&1 | head` has stopped reading, the message is lost, and the batch and
    the exit status the command decided are not. Every message of the command
    line goes through here."""
    try:
        print(text, end=end, file=sys.stderr, flush=flush)
    except BrokenPipeError:
        _silence(sys.stderr)


def _duration(ms: float) -> str:
    """Milliseconds for a page, seconds once it is a document."""
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    return f"{int(ms // 60_000)} min {ms % 60_000 / 1000:.0f} s"


def _size(bytes_: int) -> str:
    return f"{bytes_ / 1e6:.1f} MB" if bytes_ >= 1e6 else f"{bytes_ / 1e3:.0f} kB"


def _count(n: int, noun: str) -> str:
    """`1 page`, `2 pages` — nobody writes `1 page(s)` on purpose."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _quality(value: float) -> str:
    """The page-quality estimate, coloured by what it says.

    The thresholds come from the evaluation corpus, where a clean page lands
    around 0.97 and the worst around 0.89 — a flat "95% and up is green" would
    be green for everything, which is the trap the old confidence line fell into.
    """
    text = f"quality {value * 100:.0f}%"
    if value >= 0.96:
        return _paint(text, "green")
    return _paint(text, "amber" if value >= 0.92 else "red")


def _arrow(source: object, target: object) -> str:
    """`in -> out`, with the file that was just written in the accent colour."""
    return f"{source} {_paint('->', 'dim')} {_paint(str(target), 'rust', 'bold')}"


_EXAMPLES = """examples:
  ocrust scan invoice.pdf                  the text, on stdout
  ocrust scan archive/ -f markdown -o out/ a folder, one Markdown file each
  ocrust scan book.pdf --pages 1,3-5       just those pages, 1-based
  ocrust ocr scan.pdf                      the same PDF, now searchable
  ocrust pdf photo.jpg                     a photo, as a searchable PDF
  ocrust tiff scan.pdf --gray              an archive-ready TIFF
  ocrust vs archive/                       which files are VS-NfD, GEHEIM, …
  ocrust find archive/ --terms profil.toml every term of a profile, however broken
  ocrust markdown archive/ -o wissen/      any document as Markdown, kept in sync
  ocrust doctor                            what is installed, what is missing
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocrust",
        description="Fast document OCR: images, multi-page TIFF and PDF.",
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"ocrust {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="read documents and write the text out")
    scan.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="files, directories (read recursively), glob patterns, or - for stdin",
    )
    scan.add_argument(
        "-f",
        "--format",
        default="text",
        choices=FORMATS,
        help="output format (default: text)",
    )
    scan.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output file, or a directory when reading several inputs",
    )
    scan.add_argument("--pages", help="page selection for multi-page inputs, e.g. 1,3-5")
    scan.add_argument("--device", help="cpu (default), auto, cuda[:n], coreml, directml")
    scan.add_argument(
        "--threads", type=_whole(1, _MAX_PARALLEL), help="threads per inference operator"
    )
    scan.add_argument(
        "--memory",
        choices=("frugal", "fast"),
        default="frugal",
        help="frugal (default) keeps peak memory low; fast is ~10%% quicker and "
        "holds roughly twice as much",
    )
    scan.add_argument(
        "--workers",
        type=_whole(1, _MAX_PARALLEL),
        help="pages scanned in parallel (default: one per core, at most 16, for several "
        "inputs; 1 for one)",
    )
    scan.add_argument("--dpi", type=_real(20, 2400), help="PDF rasterization DPI (default 200)")
    scan.add_argument(
        "--pdf-text",
        choices=("never", "auto", "always", "only"),
        help="use a PDF page's own text instead of recognizing it: auto where it can be "
        "trusted, always, only (a page without text stays empty), or never (default)",
    )
    scan.add_argument("--models", type=Path, help="directory holding the ONNX models")
    scan.add_argument("--no-preprocess", action="store_true", help="skip deskew/invert/rescale")
    scan.add_argument("--no-word-boxes", action="store_true", help="skip per-word geometry")
    scan.add_argument(
        "--no-tables",
        action="store_true",
        help="do not read aligned cells as tables (markdown keeps them as text)",
    )
    scan.add_argument(
        "--min-confidence",
        type=_real(0, 1),
        help="drop lines below this mean confidence (0..1)",
    )
    scan.add_argument(
        "--lang",
        help="languages to require, e.g. de or de,fr (fails when the model cannot spell them)",
    )
    scan.add_argument(
        "--io-retries",
        type=_whole(0, 20),
        metavar="N",
        help="extra attempts when a read or write fails transiently "
        "(default 2, for network shares)",
    )
    scan.add_argument(
        "--progress",
        action="store_true",
        help="report each page on stderr while reading (long PDFs)",
    )
    scan.add_argument(
        "--skip-existing",
        action="store_true",
        help="leave inputs whose output file is already there (resume a batch)",
    )
    scan.add_argument(
        "--watch",
        action="store_true",
        help="keep running and scan files as they appear in the input directories",
    )
    scan.add_argument(
        "--watch-interval",
        type=_real(0.1, 3600),
        default=2.0,
        metavar="SECONDS",
        help="how often --watch looks for new files (default: 2)",
    )
    scan.add_argument("-q", "--quiet", action="store_true", help="suppress the summary line")

    pdf = sub.add_parser("pdf", help="write a searchable PDF (image plus text layer)")
    pdf.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="files, folders or patterns; several become one PDF, in the order given",
    )
    pdf.add_argument(
        "-o",
        "--output",
        type=Path,
        help="defaults to <input>.ocr.pdf, and is required for more than one input",
    )
    pdf.add_argument("--dpi", type=_real(20, 2400), help="assumed page resolution")
    pdf.add_argument("--quality", type=_whole(1, 100), default=80, help="JPEG quality (default 80)")
    pdf.add_argument("--models", type=Path)
    pdf.add_argument("--device")
    pdf.add_argument("-q", "--quiet", action="store_true", help="suppress the summary line")

    ocr = sub.add_parser(
        "ocr",
        help="add an invisible OCR text layer to a PDF, keeping its pages untouched",
    )
    ocr.add_argument("input", type=Path)
    ocr.add_argument("-o", "--output", type=Path, help="defaults to <input>.ocr.pdf")
    ocr.add_argument("--dpi", type=_real(20, 2400), help="rasterization DPI for recognition")
    ocr.add_argument(
        "--force",
        action="store_true",
        help="also OCR pages that already contain text",
    )
    ocr.add_argument("--no-compress", action="store_true", help="store the text layer uncompressed")
    ocr.add_argument(
        "--dry-run", action="store_true", help="report what would happen, change nothing"
    )
    ocr.add_argument("--lang")
    ocr.add_argument("--models", type=Path)
    ocr.add_argument("--device")
    ocr.add_argument("--workers", type=_whole(1, _MAX_PARALLEL))
    ocr.add_argument("--io-retries", type=_whole(0, 20), metavar="N")
    ocr.add_argument("-q", "--quiet", action="store_true", help="suppress the summary line")

    tiff = sub.add_parser("tiff", help="convert a document into a deskewed multi-page TIFF")
    tiff.add_argument("input", type=Path)
    tiff.add_argument("-o", "--output", type=Path, help="defaults to <input>.ocr.tiff")
    tiff.add_argument("--gray", action="store_true", help="write greyscale instead of colour")
    tiff.add_argument(
        "--compression",
        default="lzw",
        choices=("lzw", "deflate", "none"),
        help="how to pack the pages (default: lzw; none is roughly ten times the size)",
    )
    tiff.add_argument("--sidecar", choices=FORMATS, help="also write the text in this format")
    tiff.add_argument("--dpi", type=_real(20, 2400))
    tiff.add_argument("--models", type=Path)
    tiff.add_argument("--lang")
    tiff.add_argument("-q", "--quiet", action="store_true", help="suppress the summary line")

    vs = sub.add_parser(
        "vs",
        help="find classification markings: VS-NfD, GEHEIM, NATO, EU, TLP, company",
        description="Find security-classification markings in scanned documents: the\n"
        "German grades (VS-NUR FÜR DEN DIENSTGEBRAUCH, VS-VERTRAULICH, GEHEIM, STRENG\n"
        "GEHEIM), their NATO, EU, Austrian, Swiss, US, UK and French equivalents, TLP\n"
        "and company markings. A marking stamped on a page is told apart from a\n"
        "sentence that mentions one; a coloured stamp across the text is read by its\n"
        "colour.\n\n"
        "exit status: 3 when a file is marked at or above --fail-on, else 1 when a\n"
        "file could not be read (its grade is unknown), else 0.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    vs.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="files, directories (read recursively), glob patterns, or - for stdin",
    )
    vs.add_argument(
        "--fail-on",
        action="append",
        metavar="GRADE",
        help="exit 3 when a file is marked at this grade or above: vs-nfd (default), "
        "vs-v, geheim, streng-geheim; any (also TLP and company markings); none. "
        "A term severity (low … critical) applies to --terms; repeat to combine",
    )
    vs.add_argument(
        "-f",
        "--format",
        default="text",
        choices=("text", "json", "jsonl", "csv"),
        help="report format (default: text); json and csv carry every finding with its box",
    )
    vs.add_argument("-o", "--output", type=Path, help="write the report here instead of stdout")
    vs.add_argument(
        "--mentions",
        action="store_true",
        help="also list sentences that mention a grade without being marked with it",
    )
    vs.add_argument("--pages", help="page selection for multi-page inputs, e.g. 1,3-5")
    vs.add_argument(
        "--workers",
        type=_whole(1, _MAX_PARALLEL),
        help="pages scanned in parallel (default: one per core, at most 16, for several "
        "inputs; 1 for one)",
    )
    vs.add_argument("--dpi", type=_real(20, 2400), help="PDF rasterization DPI (default 200)")
    vs.add_argument("--models", type=Path, help="directory holding the ONNX models")
    vs.add_argument("--device", help="cpu (default), auto, cuda[:n], coreml, directml")
    vs.add_argument("-q", "--quiet", action="store_true", help="suppress the summary line")

    find = sub.add_parser(
        "find",
        help="find the terms of a search profile, however broken up the scan reads them",
        description="Find every term of a search profile in scanned documents: names, "
        "code words,\n"
        "numbers, anything. Letter-spaced, hyphenated, line-broken, glued and misread\n"
        "words are found; a term inside a longer word is not.\n\n"
        "exit status: 3 when a file has a hit at or above --fail-on (default: any hit),\n"
        "else 1 when a file could not be read, else 0.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    find.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="files, directories (read recursively), glob patterns, or - for stdin",
    )
    find.add_argument(
        "--markings",
        action="store_true",
        help="also report classification markings, as ocrust vs does",
    )
    find.add_argument(
        "--fail-on",
        action="append",
        metavar="LEVEL",
        help="exit 3 at this term severity or above (info, low, medium, high, "
        "critical), at a grade (vs-nfd …) with --markings, any (default) or none",
    )
    find.add_argument(
        "-f",
        "--format",
        default="text",
        choices=("text", "json", "jsonl", "csv"),
        help="report format (default: text); json and csv carry every hit with its box",
    )
    find.add_argument("-o", "--output", type=Path, help="write the report here instead of stdout")
    find.add_argument("--pages", help="page selection for multi-page inputs, e.g. 1,3-5")
    find.add_argument(
        "--workers",
        type=_whole(1, _MAX_PARALLEL),
        help="pages scanned in parallel (default: one per core, at most 16, for several "
        "inputs; 1 for one)",
    )
    find.add_argument("--dpi", type=_real(20, 2400), help="PDF rasterization DPI (default 200)")
    find.add_argument("--models", type=Path, help="directory holding the ONNX models")
    find.add_argument("--device", help="cpu (default), auto, cuda[:n], coreml, directml")
    find.add_argument("-q", "--quiet", action="store_true", help="suppress the summary line")
    find.set_defaults(mentions=False)

    markdown = sub.add_parser(
        "markdown",
        aliases=["md"],
        help="convert documents of any kind to Markdown notes for a knowledge base",
        description="Convert documents to Markdown: PDF and scans (a PDF's own text where it\n"
        "has one, OCR where not), Word, PowerPoint, Excel, OpenDocument, EPUB, HTML,\n"
        "mail (.eml, .mht), RTF, CSV, text, Markdown and source code. One note per\n"
        "document, flat YAML front matter, page markers, tables as pipe tables.\n\n"
        "With -o FOLDER the notes mirror the input folders and stay in sync: a second\n"
        "run converts only what changed, never overwrites a note edited by hand, and\n"
        "with --prune removes the notes of documents that are gone.\n\n"
        "exit status: 1 when a document could not be converted, else 0.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    markdown.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="files and folders (read recursively), or - for stdin with --name",
    )
    markdown.add_argument(
        "-o",
        "--output",
        type=Path,
        help="a folder of notes kept in sync, or a .md file for one document "
        "(default: the note on stdout)",
    )
    markdown.add_argument(
        "--prune",
        action="store_true",
        help="delete the notes of documents that are no longer there",
    )
    markdown.add_argument(
        "--force",
        action="store_true",
        help="convert everything again, and replace notes edited by hand",
    )
    markdown.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show what would be written or deleted, and change nothing",
    )
    markdown.add_argument(
        "--pdf-text",
        choices=("auto", "always", "never"),
        default="auto",
        help="use a PDF page's own text: auto (default) where it can be trusted, "
        "always, or never (recognize every page)",
    )
    markdown.add_argument(
        "--no-ocr",
        action="store_true",
        help="recognize nothing: PDFs are read from their own text alone, images and "
        "the pictures inside documents are left unread, and no model files are needed",
    )
    markdown.add_argument(
        "--no-pictures",
        action="store_true",
        help="do not read the text in pictures inside documents (diagrams, screenshots)",
    )
    markdown.add_argument(
        "--assets",
        action="store_true",
        help="keep the pictures inside documents as files in _assets/ and link them",
    )
    markdown.add_argument(
        "--max-rows",
        type=_whole(0),
        default=5000,
        metavar="N",
        help="rows of a spreadsheet or CSV table to write, the rest counted (default "
        "5000; 0 for all)",
    )
    markdown.add_argument("--name", help="file name of a document read from stdin")
    markdown.add_argument(
        "--workers",
        type=_whole(1, _MAX_PARALLEL),
        help="pages recognized in parallel (default: one per core, at most 16)",
    )
    markdown.add_argument("--dpi", type=_real(20, 2400), help="PDF rasterization DPI (default 200)")
    markdown.add_argument("--models", type=Path, help="directory holding the ONNX models")
    markdown.add_argument("--device", help="cpu (default), auto, cuda[:n], coreml, directml")
    markdown.add_argument(
        "--threads", type=_whole(1, _MAX_PARALLEL), help="threads per inference operator"
    )
    markdown.add_argument("-q", "--quiet", action="store_true", help="suppress the summary line")

    for searcher in (vs, find):
        searcher.add_argument(
            "--terms",
            action="append",
            metavar="FILE",
            help="search profile: .toml, .json, or one phrase per line; repeat to combine",
        )
        searcher.add_argument(
            "--term",
            action="append",
            metavar="PHRASE",
            help="a phrase to find, with the default settings; repeatable",
        )
        searcher.add_argument(
            "--shard",
            metavar="K/N",
            help="scan only the K-th of N parts of the inputs, the same part on every "
            "machine: run 1/N … N/N side by side to split a large share",
        )
        searcher.add_argument(
            "--threads",
            type=_whole(1, _MAX_PARALLEL),
            help="threads per inference; set cores/processes when several runs share a machine",
        )
        searcher.add_argument(
            "--resume",
            action="store_true",
            help="with -f jsonl -o FILE: skip the files FILE already has and append the rest",
        )

    for reader in (scan, pdf, ocr, tiff, vs, find, markdown):
        reader.add_argument(
            "--password",
            help="password for encrypted PDFs; OCRUST_PASSWORD keeps it out of the shell history",
        )
        reader.add_argument(
            "--max-pixels",
            type=_whole(0, 10**15),
            metavar="N",
            help="refuse images larger than N pixels, a decompression-bomb guard "
            "(default: about 179 million; 0 removes it)",
        )

    languages = sub.add_parser("languages", help="list the languages the installed model covers")
    languages.add_argument("--models", type=Path)
    languages.add_argument("--all", action="store_true", help="list every known language")
    languages.add_argument("--json", action="store_true")

    install = sub.add_parser(
        "install-models",
        help="download the bundled model set from GitHub into the model cache",
    )
    install.add_argument("--json", action="store_true")

    doctor = sub.add_parser("doctor", help="show runtime and model diagnostics")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")

    completions = sub.add_parser(
        "completions",
        help="print a shell completion script",
        description="Print a completion script for your shell.\n\n"
        "  bash:       ocrust completions bash       > /etc/bash_completion.d/ocrust\n"
        '  zsh:        ocrust completions zsh        > "${fpath[1]}/_ocrust"\n'
        "  fish:       ocrust completions fish       > ~/.config/fish/completions/ocrust.fish\n"
        "  powershell: ocrust completions powershell >> $PROFILE",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    completions.add_argument("shell", choices=sorted(_COMPLETION_SHELLS))

    models = sub.add_parser("models", help="show which model files would be used")
    models.add_argument("--models", type=Path, dest="models_dir")

    return parser


def _bad_argument(message: str) -> SystemExit:
    """Exits 2, which is what the documented table calls a bad argument.

    `SystemExit("text")` prints the text but exits 1, the code reserved for a
    file that failed to scan.
    """
    _fail(message)
    return SystemExit(2)


def _whole(low: int, high: int | None = None) -> Callable[[str], int]:
    """An argparse type: a whole number in ``low..high``, or exit 2 saying why."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
        if value < low or (high is not None and value > high):
            bounds = f"from {low}" + (f" to {high}" if high is not None else " up")
            raise argparse.ArgumentTypeError(f"{value} is out of range ({bounds})")
        return value

    return parse


def _real(low: float, high: float) -> Callable[[str], float]:
    """An argparse type: a number in ``low..high``, or exit 2 saying why."""

    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{value:g} is out of range ({low:g} to {high:g})")
        return value

    return parse


#: Page workers and inference threads: past this, memory grows and nothing is
#: gained on any machine ocrust runs on.
_MAX_PARALLEL = 256


def _parse_pages(spec: str | None) -> list[int] | None:
    """Turns ``"1,3-5"`` into zero-based indices ``[0, 2, 3, 4]``."""
    if not spec:
        return None
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start, _, end = part.partition("-")
                first, last = int(start), int(end)
                if first < 1 or last < first:
                    raise _bad_argument(f"bad page range {part!r}")
                pages.extend(range(first - 1, last))
            else:
                number = int(part)
                if number < 1:
                    raise _bad_argument("page numbers start at 1")
                pages.append(number - 1)
        except ValueError:
            # `--pages 1-x` is a typo, not a crash.
            raise _bad_argument(f"{part!r} is not a page or a page range") from None
    if not pages:
        raise _bad_argument(f"--pages {spec!r} names no page; write e.g. 1,3-5")
    return sorted(set(pages))


#: Windows redirector codes that mean "the share hiccuped", not "no".
_TRANSIENT_WINDOWS_ERRORS = frozenset({51, 52, 54, 59, 64, 121, 1450})

#: Errors a write to a network share can survive on a second attempt.
_TRANSIENT_WRITE_ERRORS = (
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
    InterruptedError,
    BlockingIOError,
)


def _write(target: Path, data: bytes | str, *, retries: int = 2, delay: float = 0.15) -> None:
    """Writes a file atomically, retrying the failures a network share produces.

    The bytes go to a hidden temporary file beside the target, which then
    replaces it in one step. An interrupted run leaves the previous file or no
    file — never a truncated one, which `--skip-existing` would otherwise take
    for finished work and skip on every run after.

    Retrying matters because `-o \\\\fileserver\\ocr` is exactly where a batch
    writes hundreds of small files, and one dropped SMB connection would
    otherwise end the run. Folders a mirrored batch needs are created here.
    """
    if _special_file(target):
        # A device or a pipe (`-o /dev/stdout`, a FIFO): there is nothing to
        # replace atomically, and replacing it would be wrong. Standard output
        # and error are written through the streams this process already has
        # — reopening them would truncate a file the shell appends to, and
        # may not be allowed at all; anything else is opened to append.
        stream = _STANDARD_STREAMS.get(str(target))
        if stream is not None:
            handle = sys.stdout if stream == 1 else sys.stderr
            if isinstance(data, str):
                handle.write(data)
            else:
                handle.flush()
                handle.buffer.write(data)
            handle.flush()
            return
        if isinstance(data, str):
            with target.open("a", encoding="utf-8") as handle:
                handle.write(data)
        else:
            with target.open("ab") as handle:
                handle.write(data)
        return
    if target.is_symlink():
        # Write where the link points; the link stays a link.
        target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.{os.getpid()}.part")
    for attempt in range(retries + 1):
        try:
            # Text keeps text mode, so line endings stay what they were.
            if isinstance(data, str):
                with partial.open("w", encoding="utf-8") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            else:
                with partial.open("wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            partial.replace(target)
            return
        except _TRANSIENT_WRITE_ERRORS:
            _discard(partial)
            if attempt == retries:
                raise
            time.sleep(delay * (2**attempt))
        except OSError as exc:
            _discard(partial)
            # Windows reports a dropped share as a plain OSError carrying the
            # redirector's own code: network name deleted, unexpected network
            # error, no system resources.
            transient = getattr(exc, "winerror", None) in _TRANSIENT_WINDOWS_ERRORS
            if attempt == retries or not transient:
                raise
            time.sleep(delay * (2**attempt))


def _save(target: Path, data: bytes | str, args: argparse.Namespace) -> bool:
    """`_write`, with a failure said in one line instead of a traceback."""
    try:
        _write(target, data, retries=_io_retries(args))
    except OSError as exc:
        _fail(f"{target}: {exc.strerror or exc}")
        return False
    return True


def _discard(path: Path) -> None:
    """Removes a temporary file if it is there; its absence is the goal."""
    with contextlib.suppress(OSError):
        path.unlink()


def _io_retries(args: argparse.Namespace) -> int:
    """How often a write may be retried, matching what the engine does on reads."""
    value = getattr(args, "io_retries", None)
    return 2 if value is None else max(0, value)


def _expand_inputs(paths: Sequence[Path]) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Resolves directories and glob patterns into files.

    `ocrust scan archive/` is what everyone tries first, and on Windows the shell
    does not expand `*.pdf` either. Directories are walked recursively and
    filtered by suffix; patterns are expanded; everything is sorted so a batch
    writes the same output twice in a row.

    UNC paths (`\\\\fileserver\\scans`) work like any other directory, and an
    input that cannot be reached says *why* — "the network path was not found"
    is a different problem from "no such file", and on a share it is the one you
    actually have.

    Returns the files to read, and the inputs that matched nothing with the
    reason they did not.
    """
    files: list[Path] = []
    missing: list[tuple[Path, str]] = []
    for path in paths:
        if str(path) == STDIN:
            # Kept as-is; the caller reads the bytes. Every other branch here
            # asks the filesystem about it, and stdin is not on the filesystem.
            files.append(path)
            continue
        try:
            is_directory = path.is_dir()
            exists = is_directory or path.exists()
        except OSError as exc:  # pragma: no cover - needs an unreachable share
            missing.append((path, str(exc)))
            continue

        if is_directory:
            found = sorted(
                child
                for child in path.rglob("*")
                if child.is_file() and child.suffix.lower() in READABLE_SUFFIXES
            )
            if found:
                files.extend(found)
            else:
                missing.append((path, "no readable files in this directory"))
        elif exists:
            files.append(path)
        elif any(ch in str(path) for ch in "*?["):
            # A pattern the shell left alone, e.g. every `ocrust scan *.pdf` on
            # Windows, or a recursive one it cannot expand at all.
            found = _glob(path)
            if found:
                files.extend(found)
            else:
                missing.append((path, "nothing matched this pattern"))
        else:
            missing.append((path, _why_unreachable(path)))
    # Keep the first occurrence of each file: two patterns may overlap.
    seen: set[Path] = set()
    unique = [f for f in files if not (f in seen or seen.add(f))]
    return unique, missing


def _why_unreachable(path: Path) -> str:
    """Turns a path that is not there into the reason it is not there.

    `Path.exists()` answers False for a missing file and for a share nobody can
    reach, which are very different things to be told at the start of a batch.
    """
    try:
        path.stat()
    except FileNotFoundError:
        return "no such file"
    except OSError as exc:
        return str(exc)
    return "no such file"


def _workers_for(args: argparse.Namespace, inputs: int) -> int | None:
    """Picks a sensible worker count when the user did not.

    Workers share the cores rather than adding any, so more of them pay off only
    when there is more than one page in flight. A batch qualifies; a single file
    is left to run with all cores on one page.
    """
    if getattr(args, "workers", None):
        return args.workers
    # One worker per core, each with a thread of its own, is what keeps every
    # core busy on a batch; past sixteen the memory grows faster than the gain.
    return min(os.cpu_count() or 4, 16) if inputs > 1 else None


def _password(args: argparse.Namespace) -> str | None:
    """The PDF password: the flag, else OCRUST_PASSWORD, which keeps it out of
    shell history and out of the process list other users can read."""
    return getattr(args, "password", None) or os.environ.get("OCRUST_PASSWORD") or None


def _guards(args: argparse.Namespace) -> dict[str, Any]:
    """The reading safeguards every command shares."""
    return {"password": _password(args), "max_pixels": getattr(args, "max_pixels", None)}


def _engine_from_args(args: argparse.Namespace) -> Ocr:
    return Ocr(
        **_guards(args),
        models_dir=getattr(args, "models", None),
        device=getattr(args, "device", None),
        threads=getattr(args, "threads", None),
        page_workers=getattr(args, "_resolved_workers", None) or getattr(args, "workers", None),
        memory=getattr(args, "memory", None),
        pdf_dpi=getattr(args, "dpi", None),
        preprocess=not getattr(args, "no_preprocess", False),
        word_boxes=not getattr(args, "no_word_boxes", False),
        tables=not getattr(args, "no_tables", False),
        drop_score=getattr(args, "min_confidence", None),
        lang=getattr(args, "lang", None),
        io_retries=getattr(args, "io_retries", None),
        read_stamps=getattr(args, "_read_stamps", False),
        tick_boxes=getattr(args, "_tick_boxes", False),
        pdf_text=getattr(args, "pdf_text", None),
    )


def _cmd_scan(args: argparse.Namespace) -> int:
    pages = _parse_pages(args.pages)
    requested = list(args.inputs)

    if args.watch:
        return _scan_watching(args, requested, pages)

    inputs, missing = _expand_inputs(requested)
    if missing:
        for path, reason in missing:
            _fail(f"{path}: {reason}")
        return 2
    if not inputs:
        _fail("nothing to read")
        return 2

    destination = _destination(args, len(inputs), _walk_roots(requested))
    if destination is None:
        return 2
    # Checked before a model is loaded or a page read: an input that shares its
    # output with another is refused, not left to overwrite or be skipped.
    blocked = _report_clashes(destination.clashes(inputs, args.format))
    inputs = [path for path in inputs if path not in blocked]
    if not inputs:
        return 1
    targets = [destination.target_for(path, args.format) for path in inputs]
    overwritten = _overwrites_input([t for t in targets if t is not None], inputs)
    if overwritten is not None:
        _fail(f"{overwritten} is an input; writing to it would destroy it before it is read")
        return 2

    args._resolved_workers = _workers_for(args, len(inputs))
    engine = _engine_from_args(args)
    status = _scan_batch(args, engine, inputs, pages, destination)
    return max(status, 1) if blocked else status


#: Paths that name this process's standard output (1) and error (2).
_STANDARD_STREAMS = {
    "/dev/stdout": 1,
    "/dev/fd/1": 1,
    "/proc/self/fd/1": 1,
    "/dev/stderr": 2,
    "/dev/fd/2": 2,
    "/proc/self/fd/2": 2,
}


def _special_file(path: Path) -> bool:
    """Whether `path` is a stream rather than a file to replace: standard
    output or error by any of their names — `/dev/stdout` is a regular file
    when the shell sends standard output to one, and replacing that file
    would lose what `>>` was appending to — or anything that exists and is
    neither a regular file nor a folder: a device, a named pipe. `/dev/shm`
    and the files in it are what they look like."""
    if str(path) in _STANDARD_STREAMS:
        return True
    try:
        return path.exists() and not path.is_file() and not path.is_dir()
    except OSError:  # pragma: no cover - an unreachable share
        return False


def _overwrites_input(targets: Sequence[Path], inputs: Sequence[Path]) -> Path | None:
    """The first output that is one of the inputs, if any — by path, or by
    the file itself, which a hard link or a second mount also reaches."""

    def key(path: Path) -> str:
        try:
            return os.path.normcase(str(path.resolve()))
        except OSError:  # pragma: no cover - an unreachable share
            return os.path.normcase(str(path))

    def identity(path: Path) -> tuple[int, int] | None:
        try:
            info = path.stat()
        except OSError:
            return None
        return (info.st_dev, info.st_ino) if info.st_ino else None

    sources = [p for p in inputs if str(p) != STDIN]
    by_path = {key(p) for p in sources}
    by_file = {i for i in (identity(p) for p in sources) if i is not None}
    for target in targets:
        if _special_file(target):
            continue
        if key(target) in by_path or identity(target) in by_file:
            return target
    return None


class _Destination:
    """Where a batch writes, resolved once so every file agrees on it.

    Outputs used to be named after the input's file name alone, and a folder
    walk is recursive — so `archive/2024/rechnung.pdf` and
    `archive/2025/rechnung.pdf` both became `out/rechnung.txt`, the second
    silently replacing the first while the summary counted two files written.
    With `--skip-existing` the second was never read at all.

    A walked folder is now mirrored: `out/2024/rechnung.txt` and
    `out/2025/rechnung.txt`. Files named on the command line, and the files at
    the top of a walked folder, stay flat exactly as before. Whatever still
    lands on one name — `rechnung.pdf` beside `rechnung.png`, or two named
    files from different folders — is refused by `clashes` instead of guessed.
    """

    def __init__(self, output: Path | None, as_directory: bool, roots: Sequence[Path] = ()) -> None:
        self.output = output
        self.as_directory = as_directory
        self.roots = list(roots)

    def target_for(self, source: Path, fmt: str) -> Path | None:
        """The file `source` will be written to, or `None` for stdout."""
        if self.output is None:
            return None
        if self.as_directory:
            stem = "stdin" if str(source) == STDIN else source.stem
            return self.output / self._subfolder(source) / f"{stem}.{_EXTENSIONS[fmt]}"
        return self.output

    def _subfolder(self, source: Path) -> Path:
        """Where under the output a file found by walking a folder belongs."""
        if str(source) == STDIN or not self.roots:
            return Path()
        try:
            resolved = source.resolve()
        except OSError:  # pragma: no cover - an unreachable share
            return Path()
        nearest: Path | None = None
        for root in self.roots:
            try:
                relative = resolved.relative_to(root)
            except ValueError:
                continue
            # The deepest folder asked for wins: the shortest path below it.
            if nearest is None or len(relative.parts) < len(nearest.parts):
                nearest = relative
        return nearest.parent if nearest is not None else Path()

    def clashes(self, sources: Sequence[Path], fmt: str) -> dict[Path, list[Path]]:
        """Outputs that more than one input would be written to."""
        claimed: dict[Path, list[Path]] = {}
        for source in sources:
            target = self.target_for(source, fmt)
            if target is not None:
                claimed.setdefault(target, []).append(source)
        return {target: found for target, found in claimed.items() if len(found) > 1}


def _walk_roots(requested: Sequence[Path]) -> list[Path]:
    """The folders a batch was asked to walk, which its output then mirrors.

    A folder is its own root; a pattern walks from its literal leading part, so
    `scans/**/*.tif` mirrors what it finds below `scans`.
    """
    roots: list[Path] = []
    for path in requested:
        if str(path) == STDIN:
            continue
        try:
            if path.is_dir():
                roots.append(path.resolve())
                continue
            if path.exists() or not any(ch in str(path) for ch in "*?["):
                continue
            literal: list[str] = []
            for part in path.parts:
                if any(ch in part for ch in "*?["):
                    break
                literal.append(part)
            roots.append(Path(*literal).resolve() if literal else Path.cwd())
        except OSError:  # pragma: no cover - an unreachable share
            continue
    return roots


def _report_clashes(clashes: dict[Path, list[Path]]) -> set[Path]:
    """Says which inputs share an output, and returns them so none is read."""
    blocked: set[Path] = set()
    for target, sources in clashes.items():
        names = [str(s) for s in sources]
        listed = ", ".join(names[:-1]) + f" and {names[-1]}"
        quantity = "both" if len(names) == 2 else "all"
        _fail(
            f"{listed} would {quantity} be written to {target}; none of them was read — "
            "rename one, or scan them into separate folders"
        )
        blocked.update(sources)
    return blocked


def _destination(
    args: argparse.Namespace, inputs: int, roots: Sequence[Path] = ()
) -> _Destination | None:
    """Resolves `--output` against the number of inputs, or reports why not."""
    many = inputs > 1 or bool(getattr(args, "watch", False))
    if args.output is not None and _special_file(args.output):
        # `-o /dev/stdout`, a named pipe: one stream, written into as it is.
        if many:
            _fail(f"-o {args.output}: several files need a folder to be written to")
            return None
        return _Destination(args.output, False, roots)
    if many and args.output and (args.output.suffix or args.output.is_file()):
        _fail("--output must be a directory when reading several files")
        return None
    # A path without a suffix is a directory: `-o out` writes out/<name>.<ext>,
    # so switching --format does not overwrite the previous run's output.
    # An existing file without a suffix is a file, not a folder to create.
    as_directory = bool(args.output) and (
        many or args.output.is_dir() or (not args.output.suffix and not args.output.exists())
    )
    if as_directory and args.output is not None:
        args.output.mkdir(parents=True, exist_ok=True)
    return _Destination(args.output, as_directory, roots)


def _scan_batch(
    args: argparse.Namespace,
    engine: Ocr,
    inputs: Sequence[Path],
    pages: Sequence[int] | None,
    destination: _Destination,
    *,
    summarize: bool = True,
) -> int:
    live = _colourful(sys.stderr)

    def report(page: int, total: int, lines: int) -> None:
        text = f"page {page + 1}/{total}, {lines} line(s)"
        if live:
            # Rewrite one line in place, and clear whatever was longer before it.
            _tell(f"\r  {_paint(text, 'dim')}\033[K", end="", flush=True)
        else:
            # A log or a pipe: `\r` would run the whole run together on one line.
            _tell(f"  {text}", flush=True)

    progress = report if getattr(args, "progress", False) else None

    failures = skipped = 0
    total_pages = total_lines = 0
    total_ms = 0.0
    for path in inputs:
        target = destination.target_for(path, args.format)
        if args.skip_existing and target is not None and target.exists():
            skipped += 1
            if not args.quiet:
                _tell(f"  {_paint(f'{path}: already written', 'dim')}")
            continue

        try:
            if str(path) == STDIN:
                data = _read_stdin()
                if not data:
                    _fail("nothing arrived on stdin")
                    failures += 1
                    continue
                doc = engine.scan(data, pages=pages, name="stdin", progress=progress)
            else:
                doc = engine.scan(path, pages=pages, progress=progress)
        except (OcrustError, OSError, ValueError) as exc:
            _fail(f"{path}: {exc}")
            failures += 1
            continue

        if progress is not None:
            # Leave the line to the summary that follows.
            _tell("\r\033[K" if live else "", end="" if live else "\n")

        rendered = doc.render(args.format)
        if target is None:
            sys.stdout.write(rendered)
            if not rendered.endswith("\n"):
                sys.stdout.write("\n")
        else:
            if not _save(target, rendered, args):
                failures += 1
                continue
            if not args.quiet:
                _tell(_arrow(path, target))

        total_pages += len(doc.pages)
        total_lines += len(doc.lines)
        total_ms += doc.elapsed_ms
        if not args.quiet:
            # Not `pages`: that name holds the page selection for every file
            # still to come, and shadowing it hands the next scan a string.
            page_count = _count(len(doc.pages), "page")
            line_count = _count(len(doc.lines), "line")
            elapsed = _duration(doc.elapsed_ms)
            if doc.quality is not None:
                body = f"{page_count}, {line_count}, {_quality(doc.quality)}, {elapsed}"
            elif doc.lines:
                # Too little text to judge, which is not the same as none.
                body = f"{page_count}, {line_count}, {elapsed}"
            else:
                body = f"{page_count}, no text found, {elapsed}"
            _tell(f"  {body}")

    if summarize and not args.quiet and len(inputs) > 1:
        done = len(inputs) - failures - skipped
        total = (
            f"{_count(done, 'file')}, {_count(total_pages, 'page')}, "
            f"{_count(total_lines, 'line')}, {_duration(total_ms)}"
        )
        if skipped:
            total += f", {skipped} already written"
        if failures:
            total += f", {_paint(_count(failures, 'failure'), 'red')}"
        _tell(_paint("done:", "rust", "bold") + " " + total)
    return 1 if failures else 0


def _read_stdin() -> bytes:
    """Reads a whole document from standard input.

    `ocrust scan -` is how this fits into a pipeline — `curl … | ocrust scan -`,
    or a scanner writing to a pipe — and the document has to be decoded as a
    whole, so it is read as a whole.
    """
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:  # a test or a host that replaced stdin with text
        text = sys.stdin.read()
        return text.encode("utf-8", "surrogateescape") if text else b""
    return stream.read()


def _scan_watching(
    args: argparse.Namespace,
    requested: Sequence[Path],
    pages: Sequence[int] | None,
) -> int:
    """Scans what is there, then keeps scanning whatever else turns up.

    For a directory a scanner or a colleague drops files into. Files already
    written are remembered, so a file is scanned once; a file still being copied
    is left until its size stops changing, because half a PDF is not a PDF.
    """
    if any(str(p) == STDIN for p in requested):
        _fail("--watch reads directories, not stdin")
        return 2
    folders = [p for p in requested if p.is_dir()]
    if not folders:
        _fail("--watch needs a directory to watch")
        return 2
    # Watching a single file has nothing to wait for, and silently ignoring it
    # would leave the user watching a directory they did not ask about.
    for other in (p for p in requested if not p.is_dir()):
        _fail(f"--watch needs a directory, and {other} is not one")
        return 2

    args._resolved_workers = _workers_for(args, len(folders) + 1)
    engine = _engine_from_args(args)
    destination = _destination(args, len(folders), _walk_roots(folders))
    if destination is None:
        return 2

    done: set[Path] = set()
    # Clashes already reported, so a watch does not repeat itself each round.
    reported: set[frozenset[Path]] = set()
    sizes: dict[Path, int] = {}
    # Whether anything failed, which decides the exit status. A count would be
    # misleading: `_scan_batch` reports per round, not per file.
    anything_failed = False
    interval = max(0.1, args.watch_interval)
    if not args.quiet:
        where = ", ".join(str(f) for f in folders)
        _note(f"watching {where} — press Ctrl-C to stop")

    try:
        while True:
            found, _ = _expand_inputs(folders)
            # Judged against everything present, not only what is new: a file
            # arriving beside one already written must not be skipped or
            # written over because the two share an output name.
            clashes = destination.clashes(found, args.format)
            fresh_clashes = {
                target: sources
                for target, sources in clashes.items()
                if frozenset(sources) not in reported
            }
            if fresh_clashes:
                _report_clashes(fresh_clashes)
                reported.update(frozenset(v) for v in fresh_clashes.values())
                anything_failed = True
            blocked = {source for sources in clashes.values() for source in sources}
            fresh = [f for f in found if f not in done and f not in blocked]
            ready = []
            for path in fresh:
                try:
                    size = path.stat().st_size
                except OSError:
                    # Gone again before we got to it; nothing to report.
                    done.add(path)
                    continue
                if sizes.get(path) == size and size > 0:
                    ready.append(path)
                else:
                    # Seen at a new size: still arriving, look again next round.
                    sizes[path] = size
            if ready:
                if _scan_batch(args, engine, ready, pages, destination, summarize=False):
                    anything_failed = True
                for path in ready:
                    done.add(path)
                    sizes.pop(path, None)
            time.sleep(interval)
    except KeyboardInterrupt:
        if not args.quiet:
            _tell()
            _note(f"stopped after {_count(len(done), 'file')}")
        return 1 if anything_failed else 0


#: Shells `ocrust completions` can write a script for.
_COMPLETION_SHELLS = ("bash", "fish", "powershell", "zsh")


class _Option:
    """One flag as a completion script needs to know it."""

    def __init__(self, flags: Sequence[str], takes_value: bool, choices: Sequence[str], help_: str):
        self.flags = list(flags)
        self.takes_value = takes_value
        self.choices = [str(c) for c in choices]
        self.help = " ".join((help_ or "").split())


class _Command:
    def __init__(
        self,
        name: str,
        help_: str,
        options: Sequence[_Option],
        takes_files: bool,
        words: Sequence[str] = (),
    ):
        self.name = name
        self.help = " ".join((help_ or "").split())
        self.options = list(options)
        self.takes_files = takes_files
        #: Values its positional accepts, when it accepts a fixed set of them.
        self.words = [str(w) for w in words]


def _completion_model() -> tuple[list[_Command], list[_Option]]:
    """Reads the parser and describes it as commands and flags.

    Generated rather than written out: a completion script that lists yesterday's
    flags is worse than none, and hand-maintaining four of them guarantees it.
    argparse has no public way to walk a built parser, so this reaches for
    `_actions` and `_subparsers` — the shape of both has been stable since
    Python 2.7.
    """

    def options_of(parser: argparse.ArgumentParser) -> tuple[list[_Option], bool, list[str]]:
        options: list[_Option] = []
        takes_files = False
        words: list[str] = []
        for action in parser._actions:
            if not action.option_strings:
                # A positional. `type=Path` is the tell that it wants files; a
                # fixed set of choices is a list of words to offer instead.
                if isinstance(action, argparse._SubParsersAction):
                    continue
                takes_files = takes_files or action.type is Path
                words.extend(str(c) for c in action.choices or ())
                continue
            if isinstance(action, argparse._SubParsersAction):
                continue
            takes_value = not isinstance(
                action,
                (
                    argparse._StoreTrueAction,
                    argparse._StoreFalseAction,
                    argparse._StoreConstAction,
                    argparse._HelpAction,
                    argparse._VersionAction,
                    argparse._CountAction,
                ),
            )
            options.append(
                _Option(action.option_strings, takes_value, action.choices or (), action.help or "")
            )
        return options, takes_files, words

    parser = _build_parser()
    top, _, _ = options_of(parser)
    commands: list[_Command] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        helps = {c.dest: c.help for c in action._choices_actions}
        for name, sub in action.choices.items():
            options, takes_files, words = options_of(sub)
            commands.append(_Command(name, helps.get(name, ""), options, takes_files, words))
    commands.sort(key=lambda c: c.name)
    return commands, top


def _completions_bash(commands: Sequence[_Command], top: Sequence[_Option]) -> str:
    lines = [
        "# ocrust completion for bash. Source it, or drop it in",
        "# /etc/bash_completion.d/ocrust (or /usr/local/etc/bash_completion.d on macOS).",
        "_ocrust() {",
        "  local cur prev command i",
        '  cur="${COMP_WORDS[COMP_CWORD]}"',
        '  prev="${COMP_WORDS[COMP_CWORD-1]}"',
        '  command=""',
        "  for ((i = 1; i < COMP_CWORD; i++)); do",
        '    case "${COMP_WORDS[i]}" in',
        "      -*) ;;",
        '      *) command="${COMP_WORDS[i]}"; break ;;',
        "    esac",
        "  done",
        "",
        '  case "$prev" in',
    ]
    # Options whose value is one of a fixed set: complete the set, not files.
    valued = {}
    for command in commands:
        for option in command.options:
            if option.choices:
                for flag in option.flags:
                    valued.setdefault(flag, set()).update(option.choices)
    for flag, choices in sorted(valued.items()):
        words = " ".join(sorted(choices))
        lines.append(f'    {flag}) COMPREPLY=($(compgen -W "{words}" -- "$cur")); return ;;')
    lines += [
        "  esac",
        "",
        '  if [[ -z "$command" ]]; then',
        f'    COMPREPLY=($(compgen -W "{" ".join(c.name for c in commands)} '
        f'{" ".join(f for o in top for f in o.flags)}" -- "$cur"))',
        "    return",
        "  fi",
        "",
        '  case "$command" in',
    ]
    for command in commands:
        flags = " ".join(flag for option in command.options for flag in option.flags)
        lines.append(f"    {command.name})")
        if command.takes_files or command.words:
            # A flag if it starts with a dash, otherwise what the command reads:
            # file names, or the words its positional accepts.
            rest = (
                f'COMPREPLY=($(compgen -W "{" ".join(command.words)}" -- "$cur"))'
                if command.words
                else 'COMPREPLY=($(compgen -f -- "$cur"))'
            )
            lines += [
                '      if [[ "$cur" == -* ]]; then',
                f'        COMPREPLY=($(compgen -W "{flags}" -- "$cur"))',
                "      else",
                f"        {rest}",
                "      fi",
            ]
        else:
            lines.append(f'      COMPREPLY=($(compgen -W "{flags}" -- "$cur"))')
        lines.append("      ;;")
    lines += ["  esac", "}", "complete -F _ocrust ocrust", ""]
    return "\n".join(lines)


def _completions_zsh(commands: Sequence[_Command], top: Sequence[_Option]) -> str:
    def quote(text: str) -> str:
        return (
            text.replace("\\", "\\\\")
            .replace("'", "'\\''")
            .replace("[", "\\[")
            .replace("]", "\\]")
            .replace(":", "\\:")
        )

    lines = [
        "#compdef ocrust",
        "# ocrust completion for zsh. Save as _ocrust somewhere on your $fpath,",
        '# e.g. "${fpath[1]}/_ocrust", then restart the shell.',
        "",
        "_ocrust() {",
        "  local context state state_descr line",
        "  typeset -A opt_args",
        "  _arguments -C \\",
        "    '1: :->command' \\",
        "    '*:: :->argument' && return",
        "",
        "  case $state in",
        "    command)",
        "      local -a commands",
        "      commands=(",
    ]
    for command in commands:
        lines.append(f"        '{command.name}:{quote(command.help)}'")
    for option in top:
        for flag in option.flags:
            lines.append(f"        '{flag}:{quote(option.help)}'")
    lines += [
        "      )",
        "      _describe -t commands 'ocrust command' commands && return",
        "      ;;",
        "    argument)",
        "      case $words[1] in",
    ]
    for command in commands:
        lines.append(f"        {command.name})")
        lines.append("          _arguments \\")
        for option in command.options:
            spec = "{" + ",".join(option.flags) + "}" if len(option.flags) > 1 else option.flags[0]
            described = f"'[{quote(option.help)}]'" if not option.takes_value else None
            if option.takes_value:
                if option.choices:
                    body = f"'[{quote(option.help)}]:value:({' '.join(option.choices)})'"
                else:
                    body = f"'[{quote(option.help)}]:value:_files'"
            else:
                body = described
            lines.append(f"            {spec}{body} \\")
        if command.words:
            lines.append(f"            '1:value:({' '.join(command.words)})'")
        elif command.takes_files:
            lines.append("            '*:file:_files'")
        else:
            lines.append("            && return")
        lines.append("          ;;")
    lines += ["      esac", "      ;;", "  esac", "}", "", '_ocrust "$@"', ""]
    return "\n".join(lines)


def _completions_fish(commands: Sequence[_Command], top: Sequence[_Option]) -> str:
    def quote(text: str) -> str:
        return text.replace("\\", "\\\\").replace("'", "\\'")

    names = " ".join(c.name for c in commands)
    lines = [
        "# ocrust completion for fish. Save as",
        "# ~/.config/fish/completions/ocrust.fish",
        "",
        f"complete -c ocrust -n 'not __fish_seen_subcommand_from {names}' -f",
    ]
    for command in commands:
        lines.append(
            f"complete -c ocrust -n 'not __fish_seen_subcommand_from {names}' "
            f"-a {command.name} -d '{quote(command.help)}'"
        )
    for option in top:
        parts = [f"complete -c ocrust -n 'not __fish_seen_subcommand_from {names}'"]
        for flag in option.flags:
            parts.append(f"-l {flag[2:]}" if flag.startswith("--") else f"-s {flag[1:]}")
        parts.append(f"-d '{quote(option.help)}'")
        lines.append(" ".join(parts))
    lines.append("")
    for command in commands:
        guard = f"-n '__fish_seen_subcommand_from {command.name}'"
        if command.words:
            lines.append(f"complete -c ocrust {guard} -f -a '{' '.join(command.words)}'")
        elif command.takes_files:
            lines.append(f"complete -c ocrust {guard} -F")
        for option in command.options:
            parts = [f"complete -c ocrust {guard}"]
            for flag in option.flags:
                parts.append(f"-l {flag[2:]}" if flag.startswith("--") else f"-s {flag[1:]}")
            if option.takes_value:
                # `-x` rather than `-r` for a fixed set: it also stops fish
                # offering file names alongside the values, which the command's
                # own file completion would otherwise mix in.
                parts.append("-x" if option.choices else "-r")
                if option.choices:
                    parts.append(f"-a '{' '.join(option.choices)}'")
            parts.append(f"-d '{quote(option.help)}'")
            lines.append(" ".join(parts))
    lines.append("")
    return "\n".join(lines)


def _completions_powershell(commands: Sequence[_Command], top: Sequence[_Option]) -> str:
    def quote(text: str) -> str:
        return text.replace("'", "''")

    top_flags = "', '".join(flag for option in top for flag in option.flags)

    lines = [
        "# ocrust completion for PowerShell. Append to your profile:",
        "#   ocrust completions powershell >> $PROFILE",
        "",
        "Register-ArgumentCompleter -Native -CommandName ocrust -ScriptBlock {",
        "  param($wordToComplete, $commandAst, $cursorPosition)",
        "",
        "  $commands = @{",
    ]
    for command in commands:
        flags = "', '".join(flag for option in command.options for flag in option.flags)
        lines.append(f"    '{command.name}' = @('{flags}')")
    lines += [
        "  }",
        "  $descriptions = @{",
    ]
    for command in commands:
        lines.append(f"    '{command.name}' = '{quote(command.help)}'")
    lines += [
        "  }",
        "",
        "  $words = $commandAst.CommandElements | Select-Object -Skip 1 |",
        "    ForEach-Object { $_.ToString() }",
        "  $command = $words | Where-Object { $commands.ContainsKey($_) } | Select-Object -First 1",
        "",
        "  if (-not $command) {",
        f"    $top = @('{top_flags}')",
        "    return ($commands.Keys + $top) | Sort-Object |",
        '      Where-Object { $_ -like "$wordToComplete*" } |',
        "      ForEach-Object {",
        "        [System.Management.Automation.CompletionResult]::new(",
        "          $_, $_, 'ParameterValue', $descriptions[$_])",
        "      }",
        "  }",
        "",
        "  return $commands[$command] |",
        '    Where-Object { $_ -like "$wordToComplete*" } |',
        "    ForEach-Object {",
        "      [System.Management.Automation.CompletionResult]::new(",
        "        $_, $_, 'ParameterName', $_)",
        "    }",
        "}",
        "",
    ]
    return "\n".join(lines)


def _cmd_completions(args: argparse.Namespace) -> int:
    commands, top = _completion_model()
    writers = {
        "bash": _completions_bash,
        "zsh": _completions_zsh,
        "fish": _completions_fish,
        "powershell": _completions_powershell,
    }
    sys.stdout.write(writers[args.shell](commands, top))
    return 0


def _cmd_pdf(args: argparse.Namespace) -> int:
    """Converts everything named into one searchable PDF.

    Any readable input can be mixed — images, multi-page TIFFs, PDFs — and the
    pages come out in the order the inputs were given, each sized from its own
    pixels. `ocrust ocr` is the other path: it leaves an existing PDF's pages
    exactly as they are and only adds the text layer.
    """
    inputs, missing = _expand_inputs(list(args.inputs))
    if missing:
        for path, reason in missing:
            _fail(f"{path}: {reason}")
        return 2
    if not inputs:
        _fail("no readable input")
        return 2
    if len(inputs) > 1 and args.output is None:
        _fail(f"{len(inputs)} inputs become one PDF, so --output is needed")
        return 2

    engine = Ocr(models_dir=args.models, device=args.device, keep_page_images=True, **_guards(args))
    try:
        data, pages = engine.searchable_pdf_many(inputs, dpi=args.dpi, jpeg_quality=args.quality)
    except (OcrustError, OSError, ValueError) as exc:
        _fail(str(exc))
        return 1
    target = args.output or inputs[0].with_suffix(".ocr.pdf")
    if not _save(target, data, args):
        return 2
    if not args.quiet:
        first = inputs[0] if len(inputs) == 1 else Path(f"{len(inputs)} inputs")
        _tell(_arrow(first, target))
        _tell(f"  {pages} page(s), {_size(len(data))}")
    return 0


def _cmd_ocr(args: argparse.Namespace) -> int:
    if not args.input.exists():
        _fail(f"no such file: {args.input}")
        return 2
    data = args.input.read_bytes()
    if not data.startswith(b"%PDF"):
        _tell(
            f"ocrust: {args.input} is not a PDF; use `ocrust pdf` to build a searchable "
            "PDF from an image",
        )
        return 2

    engine = Ocr(
        models_dir=args.models,
        device=args.device,
        page_workers=args.workers,
        pdf_dpi=args.dpi,
        lang=args.lang,
        keep_page_images=True,
        **_guards(args),
    )

    if args.dry_run:
        plan = engine.plan_pdf(data, skip_pages_with_text=not args.force)
        todo = sum(1 for page in plan if page["needs_ocr"])
        for page in plan:
            rotation = f", rotated {page['rotate']}deg" if page["rotate"] else ""
            action = "ocr" if page["needs_ocr"] else "skip (has text)"
            print(
                f"page {page['index'] + 1}: {page['width']:.0f}x{page['height']:.0f} pt"
                f"{rotation} -> {action}"
            )
        print(f"\n{todo} of {len(plan)} page(s) would get a text layer")
        return 0

    try:
        pdf, report = engine.ocr_pdf(
            data,
            dpi=args.dpi,
            skip_pages_with_text=not args.force,
            compress=not args.no_compress,
        )
    except (OcrustError, OSError, ValueError) as exc:
        _fail(str(exc))
        return 1

    target = args.output or args.input.with_suffix(".ocr.pdf")
    if not _save(target, pdf, args):
        return 2
    if not args.quiet:
        _tell(_arrow(args.input, target))
        _tell(
            f"  {report['pages_with_layer']} of {_count(report['pages'], 'page')} layered, "
            f"{report['pages_skipped']} skipped, {_count(report['lines'], 'line')}",
        )
        if report["pages_skipped"] == report["pages"] and report["pages"]:
            _note("  every page already had text: --force writes a layer anyway")
    if _password(args) and not args.quiet:
        _tell(
            "note: the output is written without password protection; "
            "encrypt it again if it has to stay locked",
        )
    if report["unmappable_chars"]:
        _tell(
            f"note: {report['unmappable_chars']} character(s) are outside WinAnsi and were "
            "written as '?' in the text layer (the visible page is unchanged)",
        )
    return 0


def _cmd_tiff(args: argparse.Namespace) -> int:
    if not args.input.exists():
        _fail(f"no such file: {args.input}")
        return 2
    if args.sidecar and args.output is not None and _special_file(args.output):
        _fail(f"--sidecar is written beside the TIFF; -o {args.output} is a stream, not a file")
        return 2
    engine = Ocr(models_dir=args.models, pdf_dpi=args.dpi, lang=args.lang, **_guards(args))
    try:
        data, doc = engine.to_tiff(args.input, gray=args.gray, compression=args.compression)
    except (OcrustError, OSError, ValueError) as exc:
        _fail(str(exc))
        return 1
    target = args.output or args.input.with_suffix(".ocr.tiff")
    if not _save(target, data, args):
        return 2
    if not args.quiet:
        _tell(_arrow(args.input, target))
        _tell(f"  {_count(len(doc.pages), 'page')}, {_size(len(data))}")
    if args.sidecar:
        sidecar = target.with_suffix("." + _EXTENSIONS[args.sidecar])
        if not _save(sidecar, doc.render(args.sidecar), args):
            return 2
        if not args.quiet:
            _tell(_arrow(args.input, sidecar))
    return 0


#: Exit status of `ocrust vs` and `ocrust find` when a file trips --fail-on.
_MARKED = 3


class _Gate:
    """One `--fail-on` criterion: a grade, a term severity, or anything."""

    def __init__(self, spec: str) -> None:
        key = spec.strip().lower()
        self.spec = key
        self.grade: int | None = None
        self.severity: int | None = None
        if key in ("any", "none"):
            return
        if key in terms.SEVERITIES:
            self.severity = terms.SEVERITIES.index(key)
            return
        try:
            self.grade = markings.level_for(key)
        except ValueError:
            raise _bad_argument(
                f"--fail-on: unknown {spec!r}; use a grade (vs-nfd, vs-v, geheim, "
                f"streng-geheim), a term severity ({', '.join(terms.SEVERITIES)}), any or none"
            ) from None

    def trips(self, marks: markings.MarkingReport | None, hits: terms.TermReport | None) -> bool:
        return self._decide(
            level=marks.level if marks is not None else 0,
            tlp=marks.tlp if marks is not None else None,
            company=bool(marks.company) if marks is not None else False,
            severities=[h.severity for h in hits] if hits is not None else [],
        )

    def trips_record(self, record: dict[str, Any]) -> bool:
        """The same decision for a file already in a JSON Lines report."""
        found = record.get("terms")
        hits = found.get("hits") if isinstance(found, dict) else None
        return self._decide(
            level=int(record.get("level") or 0),
            tlp=record.get("tlp"),
            company=bool(record.get("company")),
            severities=[str(h.get("severity")) for h in hits or [] if isinstance(h, dict)],
        )

    def _decide(self, level: int, tlp: str | None, company: bool, severities: list[str]) -> bool:
        if self.spec == "none":
            return False
        if self.spec == "any":
            # Anything that restricts who may read it, and any term found.
            # OFFEN, UNCLASSIFIED and TLP:CLEAR are markings that say the opposite.
            return level >= 1 or tlp not in (None, "CLEAR") or company or bool(severities)
        if self.grade is not None:
            return level >= self.grade
        wanted = self.severity or 0
        return any(
            s in terms.SEVERITIES and terms.SEVERITIES.index(s) >= wanted for s in severities
        )


def _gates(
    args: argparse.Namespace, marking: bool, profile: terms.Profile | None, command: str
) -> list[_Gate]:
    """The `--fail-on` criteria, or the command's defaults: for `ocrust find`
    anything found, for `ocrust vs` a grade from VS-NfD up (and any term, when
    it was given a profile)."""
    if args.fail_on:
        return [_Gate(spec) for spec in args.fail_on]
    if command == "find":
        return [_Gate("any")]
    defaults = []
    if marking:
        defaults.append("vs-nfd")
    if profile is not None and profile.terms:
        defaults.append(terms.SEVERITIES[0])  # any term found
    return [_Gate(spec) for spec in defaults]


def _profile(args: argparse.Namespace) -> terms.Profile | None:
    """Every `--terms` file and `--term` phrase, as one profile."""
    parts: list[terms.Profile] = []
    try:
        for path in getattr(args, "terms", None) or []:
            parts.append(terms.load(Path(path)))
        phrases = getattr(args, "term", None) or []
        if phrases:
            parts.append(terms.load(list(phrases)))
        if not parts:
            return None
        merged = parts[0]
        for extra in parts[1:]:
            merged = merged + extra
    except terms.ProfileError as exc:
        raise _bad_argument(str(exc)) from None
    return merged


def _shard(spec: str | None) -> tuple[int, int] | None:
    """`--shard 2/4` as (1, 4): the second of four parts, zero-based."""
    if not spec:
        return None
    try:
        k, n = (int(x) for x in spec.split("/"))
    except ValueError:
        raise _bad_argument(f"--shard {spec!r}: write it as K/N, e.g. 1/4") from None
    if not (n >= 1 and 1 <= k <= n):
        raise _bad_argument(f"--shard {spec!r}: K must be between 1 and N")
    return k - 1, n


def _in_shard(path: Path, roots: Sequence[Path], shard: tuple[int, int]) -> bool:
    """Whether `path` belongs to this shard.

    Decided by a hash of the path below the folder that was walked, so the
    same file falls into the same shard on every machine, whatever the share
    is mounted as — `N` runs of `--shard K/N` cover every file exactly once.
    """
    import zlib

    key = path.as_posix()
    try:
        resolved = path.resolve()
        for root in roots:
            try:
                key = resolved.relative_to(root).as_posix()
                break
            except ValueError:
                continue
    except OSError:  # pragma: no cover - an unreachable share
        pass
    return zlib.crc32(key.encode("utf-8")) % shard[1] == shard[0]


def _already_done(report: Path) -> dict[str, dict[str, Any]]:
    """The records a JSON Lines report already has, by source, for `--resume`.

    The file is checked before anything is written to it: one that is not a
    report of ours (not UTF-8, or no line of it a record) is refused and left
    as it is. An interrupted run can leave half a record at the end; that one
    is cut off, so the file it belonged to is read again and its new record
    starts on a line of its own instead of being glued to the fragment.
    """
    done: dict[str, dict[str, Any]] = {}
    try:
        data = report.read_bytes()
    except FileNotFoundError:
        return done
    except OSError as exc:
        raise _bad_argument(f"--resume: cannot read {report}: {exc.strerror or exc}") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _bad_argument(f"--resume: {report} is not a JSON Lines report") from None
    complete, newline, fragment = text.rpartition("\n")
    if not newline:
        complete, fragment = "", text
    lines = [line for line in complete.split("\n") if line.strip()]
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line an older run garbled; its file is read again
        if isinstance(record, dict) and "source" in record:
            done[str(record["source"])] = record
    if (lines and not done) or (fragment.strip() and not fragment.lstrip().startswith("{")):
        raise _bad_argument(f"--resume: {report} is not a JSON Lines report of ocrust")
    try:
        last = json.loads(fragment) if fragment.strip() else None
    except json.JSONDecodeError:
        last = None
    if isinstance(last, dict) and "source" in last:
        # A whole record that only lacks its line end: keep it, end the line.
        done[str(last["source"])] = last
        try:
            with report.open("ab") as fh:
                fh.write(b"\n")
        except OSError as exc:
            reason = exc.strerror or exc
            raise _bad_argument(f"--resume: cannot repair {report}: {reason}") from None
    elif fragment:
        keep = len((complete + newline).encode("utf-8"))
        try:
            with report.open("r+b") as fh:
                fh.truncate(keep)
        except OSError as exc:
            reason = exc.strerror or exc
            raise _bad_argument(f"--resume: cannot repair {report}: {reason}") from None
    return done


def _empty_report(fmt: str, gates: Sequence[_Gate]) -> str:
    """What a report with no files in it looks like, in each format."""
    if fmt == "json":
        summary = {
            "files": 0,
            "marked": 0,
            "with_hits": 0,
            "unreadable": 0,
            "clean": 0,
            "fail_on": [g.spec for g in gates],
            "tripped": 0,
            "by_label": {},
        }
        return json.dumps({"summary": summary, "files": []}, indent=2) + "\n"
    if fmt == "csv":
        return ",".join(_CSV_FIELDS) + "\r\n"
    return ""


def _page_ranges(numbers: Sequence[int]) -> str:
    """`1-3, 5`: page numbers the way a person writes them."""
    numbers = sorted(set(numbers))
    runs: list[list[int]] = []
    for n in numbers:
        if runs and n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    parts = [f"{r[0]}-{r[-1]}" if len(r) > 2 else ", ".join(map(str, r)) for r in runs]
    return ("p. " if len(numbers) == 1 else "pp. ") + ", ".join(parts)


def _headline(report: markings.MarkingReport) -> str:
    """The one name a file is filed under: its grade, else TLP, else company."""
    if report.label:
        return report.label
    if report.tlp:
        return f"TLP:{report.tlp}"
    if report.company:
        return report.company
    return "-"


def _marking_details(report: markings.MarkingReport, head: str) -> list[str]:
    """The evidence behind a file's grade, for the text report."""
    marked = report.markings
    details: list[str] = []
    if marked:
        top = [f for f in marked if f.label == head or f"TLP:{f.label}" == head] or list(marked)
        on_pages = [f.page for f in top if f.page > 0]
        if on_pages:
            details.append(_page_ranges(on_pages))
        order = {"header": 0, "footer": 1}
        where = sorted({f.reason for f in top}, key=lambda r: (order.get(r, 2), r))
        details.append(", ".join(where))
        if all(f.fuzzy for f in top):
            details.append("read through OCR errors, check by eye")
        others = sorted({f.label for f in marked} - {head})
        if others:
            details.append("also " + ", ".join(others))
        if report.unmarked_pages:
            details.append(f"unmarked: {_page_ranges(report.unmarked_pages)}")
        if report.caveats:
            details.append(" ".join(report.caveats))
        if report.cancelled:
            details.append("a grade is marked as lifted or lowered")
    elif report.mentions:
        named = [f for f in report.mentions if f.reason == "file name"]
        if named:
            details.append(f"file name says {named[0].label}")
        others = len(report.mentions) - len(named)
        if others:
            details.append(_count(others, "mention"))
    return details


def _term_details(report: terms.TermReport) -> list[str]:
    """Each term found, how often, where and how it was broken."""
    grouped: dict[str, list[terms.Hit]] = {}
    for hit in report.hits:
        grouped.setdefault(hit.term, []).append(hit)
    details = []
    for name, hits in grouped.items():
        text = name if len(hits) == 1 else f"{name} ×{len(hits)}"
        how = sorted({h for hit in hits for h in hit.how} - {"exact"})
        where = _page_ranges([h.page for h in hits])
        details.append(f"{text} ({where}{', ' + ', '.join(how) if how else ''})")
    return details


def _findings_line(
    path: Path,
    marks: markings.MarkingReport | None,
    hits: terms.TermReport | None,
    show_mentions: bool,
    out: Any,
) -> str:
    """One file of the text report: what it is marked with and what it contains."""
    head = _headline(marks) if marks is not None else "-"
    details = _marking_details(marks, head) if marks is not None else []
    if hits:
        count = _count(len(hits), "hit")
        if head == "-":
            head = count
        details.extend(_term_details(hits))
    if marks is not None and marks.level >= 1:
        style: tuple[str, ...] = ("red", "bold")
    elif marks is not None and marks.markings:
        style = ("amber",)
    elif hits:
        severe = terms.SEVERITIES.index(hits.severity or "info") >= terms.SEVERITIES.index("high")
        style = ("red", "bold") if severe else ("amber",)
    else:
        style = ("dim",)
    line = f"{_paint(f'{head:<16}', *style, stream=out)} {path}"
    if details:
        line += "  " + _paint(" · ".join(details), "dim", stream=out)
    if show_mentions and marks is not None:
        for f in marks.mentions:
            place = f"p. {f.page}" if f.page else f.reason
            line += f'\n{"":<17}{place}: {f.label} in "{f.text}"'
    return line


_CSV_FIELDS = (
    "source",
    "status",
    "page",
    "kind",
    "level",
    "label",
    "scheme",
    "match",
    "text",
    "confidence",
    "fuzzy",
    "reason",
    "x0",
    "y0",
    "x1",
    "y1",
    "severity",
)


def _csv_rows(
    path: Path, marks: markings.MarkingReport | None, hits: terms.TermReport | None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in marks.findings if marks is not None else ():
        x0, y0, x1, y1 = f.box.as_tuple()
        rows.append(
            {
                "source": str(path),
                "status": "ok",
                "page": f.page,
                "kind": f.kind,
                "level": f.level,
                "label": f.label,
                "scheme": f.scheme,
                "match": f.match,
                "text": f.text,
                "confidence": round(f.confidence, 4),
                "fuzzy": f.fuzzy,
                "reason": f.reason,
                "x0": round(x0, 1),
                "y0": round(y0, 1),
                "x1": round(x1, 1),
                "y1": round(y1, 1),
            }
        )
    for h in hits or ():
        x0, y0, x1, y1 = h.box.as_tuple()
        rows.append(
            {
                "source": str(path),
                "status": "ok",
                "page": h.page,
                "kind": "term",
                "label": h.term,
                "scheme": h.category,
                "match": h.pattern,
                "text": h.text,
                "confidence": round(h.score, 4),
                "fuzzy": bool({"fuzzy", "ocr"} & set(h.how)),
                "reason": "+".join(h.how),
                "x0": round(x0, 1),
                "y0": round(y0, 1),
                "x1": round(x1, 1),
                "y1": round(y1, 1),
                "severity": h.severity,
            }
        )
    return rows or [{"source": str(path), "status": "ok"}]


def _cmd_vs(args: argparse.Namespace) -> int:
    return _cmd_findings(args, marking=True, command="vs")


def _cmd_find(args: argparse.Namespace) -> int:
    return _cmd_findings(args, marking=bool(args.markings), command="find")


def _cmd_findings(args: argparse.Namespace, marking: bool, command: str) -> int:
    import csv
    import io

    profile = _profile(args)
    if profile is not None and profile.markings:
        marking = True
    requested_gates = [_Gate(spec) for spec in args.fail_on or []]
    if any(g.grade is not None for g in requested_gates):
        marking = True  # a gate on a grade needs the grades read
    if not marking and profile is None:
        _fail("nothing to look for: give --terms FILE or --term PHRASE (or --markings)")
        return 2
    if profile is None:
        severity = next((g.spec for g in requested_gates if g.severity is not None), None)
        if severity is not None:
            _fail(f"--fail-on {severity} is a term severity: give --terms FILE or --term PHRASE")
            return 2
    gates = _gates(args, marking, profile, command)
    if args.output is not None and not _special_file(args.output):
        # Found out now, not after an hour of scanning.
        if args.output.is_dir():
            _fail(f"-o {args.output}: is a folder; give the report a file name")
            return 2
        folder = args.output.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _fail(f"-o {args.output}: {exc.strerror or exc}")
            return 2
        if not os.access(folder, os.W_OK):
            _fail(f"-o {args.output}: cannot write in {folder}")
            return 2
    pages = _parse_pages(args.pages)
    shard = _shard(getattr(args, "shard", None))
    requested = list(args.inputs)
    inputs, missing = _expand_inputs(requested)
    if missing:
        for path, reason in missing:
            _fail(f"{path}: {reason}")
        return 2
    if args.output is not None and _overwrites_input([args.output], inputs) is not None:
        _fail(f"-o {args.output} is an input; writing the report would destroy it")
        return 2
    if shard is not None:
        roots = _walk_roots(requested)
        inputs = [p for p in inputs if str(p) == STDIN or _in_shard(p, roots, shard)]
    streaming = args.format == "jsonl" and args.output is not None
    # A resumed run answers for the whole report: files the earlier run
    # already tripped on, or could not read, count toward the exit status.
    earlier_tripped = earlier_unreadable = 0
    if args.resume:
        if not streaming or _special_file(args.output):
            _fail("--resume continues a JSON Lines report: use it with -f jsonl -o FILE")
            return 2
        done = _already_done(args.output)
        inputs = [p for p in inputs if str(p) not in done]
        for record in done.values():
            if record.get("status") == "error":
                earlier_unreadable += 1
            elif any(g.trips_record(record) for g in gates):
                earlier_tripped += 1
        if not args.quiet and done:
            _note(f"resuming: {_count(len(done), 'file')} already in {args.output}")
    if not inputs:
        if args.resume or shard is not None:
            # Nothing left to do is a finished job, not an error; a shard that
            # got no files still leaves its (empty) report where one is expected.
            if not args.resume:
                empty = _empty_report(args.format, gates)
                if args.output is not None:
                    try:
                        args.output.parent.mkdir(parents=True, exist_ok=True)
                        args.output.write_text(empty, encoding="utf-8", newline="")
                    except OSError as exc:
                        _fail(f"{args.output}: {exc.strerror or exc}")
                        return 2
                else:
                    sys.stdout.write(empty)
            if earlier_tripped:
                return _MARKED
            return 1 if earlier_unreadable else 0
        _fail("nothing to read")
        return 2

    args._resolved_workers = _workers_for(args, len(inputs))
    # A stamp over the text is only readable by its colour, and a grade ticked
    # on a form only by its box; the terms of a profile may sit in a stamp too.
    args._read_stamps = True
    args._tick_boxes = marking
    engine = _engine_from_args(args)

    buffer = io.StringIO()
    stream_file = None
    standard = _STANDARD_STREAMS.get(str(args.output)) if streaming else None
    if standard is not None:
        stream_file = None
        out: Any = sys.stdout if standard == 1 else sys.stderr
    elif streaming:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            # A device or a pipe is appended to, never truncated.
            mode = "a" if args.resume or _special_file(args.output) else "w"
            stream_file = args.output.open(mode, encoding="utf-8")
        except OSError as exc:
            _fail(f"{args.output}: {exc.strerror or exc}")
            return 2
        out = stream_file
    else:
        out = buffer if args.output else sys.stdout
    writer = csv.DictWriter(out, fieldnames=_CSV_FIELDS) if args.format == "csv" else None
    if writer is not None:
        writer.writeheader()
    records: list[dict[str, Any]] = []
    tally: dict[str, int] = {}
    marked = with_hits = unreadable = tripped = clean = 0
    started = time.monotonic()
    page_total = 0

    # Standard input is read here; everything else goes to the engine in
    # parallel batches, results in input order.
    items: list[Any] = []
    for path in inputs:
        items.append(_read_stdin() if str(path) == STDIN else path)
    reader_gone = False
    try:
        for path, (_, result) in zip(inputs, engine.scan_each(items, pages=pages)):
            if isinstance(result, Exception):
                unreadable += 1
                message = str(result)
                record: dict[str, Any] = {"source": str(path), "status": "error", "error": message}
                if args.format == "text":
                    label = _paint(f"{'unreadable':<16}", "red", stream=out)
                    print(f"{label} {path}  {message}", file=out)
                elif writer is not None:
                    writer.writerow({"source": str(path), "status": "error", "text": message})
            else:
                page_total += len(result.pages)
                marks = (
                    markings.inspect(result, file=None if str(path) == STDIN else path)
                    if marking
                    else None
                )
                hits = terms.find(result, profile) if profile is not None else None
                if any(g.trips(marks, hits) for g in gates):
                    tripped += 1
                if marks is not None and marks.markings:
                    marked += 1
                    tally[_headline(marks)] = tally.get(_headline(marks), 0) + 1
                if hits:
                    with_hits += 1
                if not hits and not (marks is not None and marks.markings):
                    clean += 1
                record = {"source": str(path), "status": "ok"}
                if marks is not None:
                    record.update(marks.to_dict())
                    record["source"] = str(path)
                if hits is not None:
                    record["terms"] = hits.to_dict()
                    record["terms"]["source"] = str(path)
                if args.format == "text":
                    print(_findings_line(path, marks, hits, args.mentions, out), file=out)
                elif writer is not None:
                    writer.writerows(_csv_rows(path, marks, hits))
            if args.format == "jsonl":
                print(json.dumps(record, ensure_ascii=False), file=out)
            elif args.format == "json":
                records.append(record)
            if out is sys.stdout or out is sys.stderr or out is stream_file:
                out.flush()
        if args.format == "json":
            summary = {
                "files": len(inputs),
                "marked": marked,
                "with_hits": with_hits,
                "unreadable": unreadable,
                "clean": clean,
                "fail_on": [g.spec for g in gates],
                "tripped": tripped,
                "by_label": tally,
            }
            json.dump({"summary": summary, "files": records}, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
    except BrokenPipeError:
        # `ocrust find … | head -1`: the reader left. What was found so far
        # still decides the exit status a gate (`set -o pipefail`) sees.
        reader_gone = True
        _silence_stdout()
    finally:
        if stream_file is not None:
            stream_file.close()
    if reader_gone:
        if tripped or earlier_tripped:
            return _MARKED
        return 1 if unreadable or earlier_unreadable else 0

    if args.output and not streaming:
        try:
            _write(args.output, buffer.getvalue(), retries=_io_retries(args))
        except OSError as exc:
            _fail(f"{args.output}: {exc.strerror or exc}")
            return 2

    if not args.quiet:
        parts = [_count(len(inputs), "file")]
        if tally:
            listed = ", ".join(
                f"{n} {label}" for label, n in sorted(tally.items(), key=lambda kv: -kv[1])
            )
            parts.append(_paint(f"{marked} marked ({listed})", "red", "bold"))
        if profile is not None:
            parts.append(_paint(f"{with_hits} with hits", "amber") if with_hits else "no hits")
        parts.append(f"{clean} clean")
        if unreadable:
            parts.append(_paint(f"{unreadable} unreadable", "red"))
        parts.append(
            f"{_count(page_total, 'page')}, {_duration((time.monotonic() - started) * 1000)}"
        )
        if args.output:
            parts.append(f"report -> {args.output}")
        _tell(_paint("done:", "rust", "bold") + " " + ", ".join(parts))

    if tripped or earlier_tripped:
        return _MARKED
    return 1 if unreadable or earlier_unreadable else 0


def _markdown_options(args: argparse.Namespace) -> Any:
    from .markdown import Options

    return Options(
        pdf_text=args.pdf_text,
        ocr=not args.no_ocr,
        pictures=not args.no_pictures,
        assets=args.assets,
        max_rows=args.max_rows or None,
        password=_password(args),
    )


def _cmd_markdown(args: argparse.Namespace) -> int:
    from . import markdown

    options = _markdown_options(args)
    engine: Ocr | None = None

    def make_engine() -> Ocr:
        # Made when the first scan or picture needs it: a folder of Word files
        # converts without the model files installed.
        nonlocal engine
        if engine is None:
            engine = Ocr(
                **_guards(args),
                models_dir=args.models,
                device=args.device,
                threads=args.threads,
                page_workers=args.workers or min(os.cpu_count() or 4, 16),
                pdf_dpi=args.dpi,
                pdf_text=args.pdf_text,
            )
        return engine

    inputs = list(args.inputs)
    single = len(inputs) == 1 and (str(inputs[0]) == STDIN or not inputs[0].is_dir())
    to_file = args.output is not None and (
        _special_file(args.output)
        or args.output.suffix.lower() in (".md", ".markdown")
        or args.output.is_file()
    )
    if args.output is None or to_file:
        if not single:
            _fail("several documents need a folder to be written to: -o FOLDER")
            return 2
        return _markdown_one(args, inputs[0], options, make_engine)
    if any(str(item) == STDIN for item in inputs):
        _fail("- (stdin) is converted on its own, to stdout or to a .md file")
        return 2
    missing = [item for item in inputs if not item.exists()]
    for item in missing:
        _fail(f"{item}: {_why_unreachable(item)}")
    inputs = [item for item in inputs if item.exists()]
    if not inputs:
        return 1

    started = time.monotonic()
    quiet = args.quiet

    def progress(event: str, source: Path, note: Path | None, detail: str) -> None:
        if event == "failed":
            _fail(f"{source}: {detail}")
        elif quiet:
            return
        elif event in ("written", "would write") and note is not None:
            prefix = "" if event == "written" else _paint("would write ", "dim")
            _tell(prefix + _arrow(source, note))
        elif event == "kept":
            _tell(f"  {_paint('kept', 'amber')} {source}: {detail}")
        elif event in ("pruned", "would remove") and note is not None:
            verb = "removed" if event == "pruned" else "would remove"
            _tell(f"  {_paint(verb, 'dim')} {note}")

    try:
        result = markdown.export(
            inputs,
            args.output,
            options=options,
            engine=make_engine,
            prune=args.prune,
            force=args.force,
            dry_run=args.dry_run,
            progress=progress,
        )
    except markdown.ConversionError as exc:
        _fail(str(exc))
        return 1
    except OSError as exc:
        _fail(f"{exc.filename or args.output}: {exc.strerror or exc}")
        return 1
    for source, reason in result.failed:
        # Clashes are reported here; conversion failures came through progress.
        if "also" in reason and reason.endswith("rename one"):
            _fail(f"{source}: {reason}")
    for warning in result.warnings:
        _tell(f"{_paint('note:', 'amber', 'bold')} {_wrap(warning, 6)}")
    if not quiet:
        parts = [
            _count(len(result.written), "note") + (" to write" if args.dry_run else " written")
        ]
        if result.unchanged:
            parts.append(f"{len(result.unchanged)} unchanged")
        if result.kept:
            parts.append(_paint(f"{len(result.kept)} kept", "amber"))
        if result.pruned:
            parts.append(f"{len(result.pruned)} " + ("to remove" if args.dry_run else "removed"))
        if result.failed:
            parts.append(_paint(_count(len(result.failed), "failure"), "red"))
        if result.skipped:
            parts.append(f"{len(result.skipped)} other files skipped")
        parts.append(_duration((time.monotonic() - started) * 1000))
        _tell(_paint("done:", "rust", "bold") + " " + ", ".join(parts))
    return 1 if (result.failed or missing) else 0


def _markdown_one(
    args: argparse.Namespace, source: Path, options: Any, make_engine: Callable[[], Ocr]
) -> int:
    """One document, to stdout or to one file; no index, nothing kept in sync."""
    from . import markdown

    try:
        if str(source) == STDIN:
            data = _read_stdin()
            if not data:
                _fail("nothing arrived on stdin")
                return 1
            name = args.name or "stdin"
            if not markdown.supported(name):
                _fail("stdin needs --name with the document's file name, e.g. --name brief.docx")
                return 2
            result = markdown.convert(data, name=name, options=options, engine=make_engine)
        else:
            if not source.exists():
                _fail(f"{source}: {_why_unreachable(source)}")
                return 1
            result = markdown.convert(source, options=options, engine=make_engine)
    except (markdown.ConversionError, OcrustError, OSError, ValueError) as exc:
        _fail(f"{source}: {getattr(exc, 'strerror', None) or exc}")
        return 1
    if args.output is None:
        sys.stdout.write(result.markdown)
        return 0
    if args.dry_run:
        if not args.quiet:
            _tell(_paint("would write ", "dim") + _arrow(source, args.output))
        return 0
    if _overwrites_input([args.output], [source]):
        _fail(f"{args.output} is the input itself; write the note somewhere else")
        return 2
    if not _save(args.output, result.markdown, args):
        return 1
    if not args.quiet:
        _tell(_arrow(source, args.output))
    return 0


def _cmd_languages(args: argparse.Namespace) -> int:
    from . import known_languages

    if args.all:
        entries = list(known_languages())
        label = "known to ocrust"
    else:
        try:
            engine = Ocr(models_dir=args.models)
        except OcrustError as exc:
            _fail(str(exc))
            return 1
        entries = list(engine.languages)
        label = f"covered by the installed model ({engine.charset_size} characters)"

    if args.json:
        print(json.dumps(entries, indent=2))
        return 0

    out = sys.stdout
    count = _paint(str(len(entries)), "rust", "bold", stream=out)
    print(f"{count} language(s) {label}:\n")
    by_script: dict[str, list[str]] = {}
    for entry in entries:
        by_script.setdefault(str(entry["script"]), []).append(f"{entry['code']} ({entry['name']})")
    for script in sorted(by_script):
        names = ", ".join(sorted(by_script[script]))
        label_text = f"{script:<11}"
        print(f"  {_paint(label_text, 'rust', stream=out)} {_wrap(names, 14)}")

    if not args.all:
        engine = Ocr(models_dir=args.models)
        near = engine.partial_languages(0.8)
        if near:
            heading = _paint("nearly covered", "amber", stream=out)
            print(f"\n{heading} (a few characters missing):")
            for entry in near:
                print(
                    f"  {entry['code']} ({entry['name']}): {entry['ratio'] * 100:.0f}%"
                    f", missing {entry['missing']}"
                )
        optional = engine.optional_language_gaps()
        if optional:
            heading = _paint("covered, with a substitution", "amber", stream=out)
            print(f"\n{heading} (the language does not require these):")
            for entry in optional:
                print(f"  {entry['code']} ({entry['name']}): no {entry['missing']}")
    return 0


def _cmd_install_models(args: argparse.Namespace) -> int:
    from . import install_models

    try:
        paths = install_models()
    except Exception as exc:
        _fail(str(exc))
        return 1
    if args.json:
        print(json.dumps(paths, indent=2))
        return 0
    for key, value in paths.items():
        print(f"{key:<12} {value or '-'}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    info = runtime_info()
    if args.json:
        print(json.dumps(info, indent=2, default=str))
        return 0
    out = sys.stdout

    def row(label: str, value: object, indent: int = 0) -> None:
        """One diagnostic line: a dim label, and a value that stays on screen.

        Model paths inside a virtualenv are long enough to run off any terminal,
        and a diagnostic that has to be scrolled sideways is no diagnostic — so
        they fold under themselves instead of being cut.
        """
        pad = "  " * indent
        name = _paint(f"{pad}{label}".ljust(18), "dim", stream=out)
        print(f"{name}{_fold(str(value), 18)}", file=out)

    row("ocrust", _paint(str(info["ocrust"]), "rust", "bold", stream=out))
    row("python", f"{info['python']} on {info['platform']}")
    missing = _paint("not installed", "red", stream=out)
    row("onnxruntime", info.get("onnxruntime_version") or missing)
    row("library", info.get("onnxruntime_dylib") or _paint("NOT FOUND", "red", stream=out), 1)
    row("loaded", info.get("onnxruntime_loaded"), 1)
    row("models dir", info.get("models_dir") or "not set")
    row("models cache", info.get("models_cache_dir"))
    models = info.get("models")
    if isinstance(models, dict):
        for key, value in models.items():
            row(key, value or "-", 1)
    else:
        row("models", models, 1)
    ready = info.get("onnxruntime_dylib") and isinstance(models, dict)
    print(file=out)
    if ready:
        print(f"status: {_paint('ready', 'green', 'bold', stream=out)}", file=out)
    else:
        print(
            f"status: {_paint('not ready', 'red', 'bold', stream=out)} — see the hints above",
            file=out,
        )
    return 0 if ready else 1


def _cmd_models(args: argparse.Namespace) -> int:
    try:
        engine = Ocr(models_dir=args.models_dir)
    except OcrustError as exc:
        _fail(str(exc))
        return 1
    out = sys.stdout
    for key, value in engine.models.items():
        label = _paint(f"{key:<12}", "dim", stream=out)
        print(f"{label} {_fold(str(value or '-'), 13)}")
    return 0


def _prepare_streams() -> None:
    """Makes the output streams able to carry what this CLI prints.

    A redirected stdout on Windows is cp1252, and `ocrust languages` prints
    Vietnamese, Greek and CJK: it ended in a `UnicodeEncodeError` there. Files
    written with `--output` are UTF-8 either way, so this only settles what a
    console or a pipe gets.
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``ocrust`` console script."""
    _prepare_streams()
    try:
        code = _dispatch(_build_parser().parse_args(argv))
        # Flushing inside the `try` is what routes a closed pipe to the handler
        # below: a pipe is block-buffered, so `ocrust scan x | head -1` fails
        # when the buffer is written, which otherwise happens on the way out of
        # the interpreter, too late to catch.
        sys.stdout.flush()
        return code
    except OcrustError as exc:
        # Building the engine fails before the command itself runs: a model that
        # is not there, a language the model cannot spell, an unknown device. One
        # line is what a command-line tool owes the reader, not a traceback.
        _fail(str(exc))
        return 1
    except KeyboardInterrupt:
        _tell()
        _fail("interrupted")
        return 130
    except BrokenPipeError:
        # `ocrust scan book.pdf | head -1`: the reader left, which is its right.
        _silence_stdout()
        return 0


def _silence_stdout() -> None:
    _silence(sys.stdout)


def _silence(stream: Any) -> None:
    """Points a stream whose reader left at the void: Python still holds it and
    would print "Exception ignored" while flushing it at exit. A stream without
    a file descriptor (a test harness, say) has nothing to redirect."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), stream.fileno())


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "scan":
        return _cmd_scan(args)
    if args.command == "pdf":
        return _cmd_pdf(args)
    if args.command == "ocr":
        return _cmd_ocr(args)
    if args.command == "tiff":
        return _cmd_tiff(args)
    if args.command == "vs":
        return _cmd_vs(args)
    if args.command == "find":
        return _cmd_find(args)
    if args.command in ("markdown", "md"):
        return _cmd_markdown(args)
    if args.command == "languages":
        return _cmd_languages(args)
    if args.command == "install-models":
        return _cmd_install_models(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "completions":
        return _cmd_completions(args)
    if args.command == "models":
        return _cmd_models(args)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
