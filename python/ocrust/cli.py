"""Command line interface: ``ocrust``.

ocrust scan invoice.pdf                 # text to stdout
ocrust scan *.jpg -f markdown -o out/   # batch, one file per input
ocrust ocr scan.pdf -o scan.ocr.pdf     # add a text layer, keep the pages
ocrust pdf photo.jpg -o photo.pdf       # build a searchable PDF from an image
ocrust tiff scan.pdf --gray             # deskewed multi-page TIFF
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
from collections.abc import Sequence
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
    print(f"{_paint('ocrust:', 'red', 'bold')} {_wrap(message, 8)}", file=sys.stderr)


def _note(message: str) -> None:
    """A quiet aside: part of the report, not the result."""
    print(_paint(message, "dim"), file=sys.stderr)


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
    scan.add_argument("--threads", type=int, help="threads per inference operator")
    scan.add_argument(
        "--memory",
        choices=("frugal", "fast"),
        default="frugal",
        help="frugal (default) keeps peak memory low; fast is ~10%% quicker and "
        "holds roughly twice as much",
    )
    scan.add_argument(
        "--workers",
        type=int,
        help="pages scanned in parallel (default: 4 for several inputs, 1 otherwise)",
    )
    scan.add_argument("--dpi", type=float, help="PDF rasterization DPI (default 200)")
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
        type=float,
        help="drop lines below this mean confidence (0..1)",
    )
    scan.add_argument(
        "--lang",
        help="languages to require, e.g. de or de,fr (fails when the model cannot spell them)",
    )
    scan.add_argument(
        "--io-retries",
        type=int,
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
        type=float,
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
    pdf.add_argument("--dpi", type=float, help="assumed page resolution")
    pdf.add_argument("--quality", type=int, default=80, help="JPEG quality (default 80)")
    pdf.add_argument("--models", type=Path)
    pdf.add_argument("--device")
    pdf.add_argument("-q", "--quiet", action="store_true", help="suppress the summary line")

    ocr = sub.add_parser(
        "ocr",
        help="add an invisible OCR text layer to a PDF, keeping its pages untouched",
    )
    ocr.add_argument("input", type=Path)
    ocr.add_argument("-o", "--output", type=Path, help="defaults to <input>.ocr.pdf")
    ocr.add_argument("--dpi", type=float, help="rasterization DPI for recognition")
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
    ocr.add_argument("--workers", type=int)
    ocr.add_argument("--io-retries", type=int, metavar="N")
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
    tiff.add_argument("--dpi", type=float)
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
        default="vs-nfd",
        metavar="GRADE",
        help="exit 3 when a file is marked at this grade or above: vs-nfd (default), "
        "vs-v, geheim, streng-geheim; any (also TLP and company markings); none",
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
    vs.add_argument("--workers", type=int, help="files scanned in parallel (default: 4)")
    vs.add_argument("--dpi", type=float, help="PDF rasterization DPI (default 200)")
    vs.add_argument("--models", type=Path, help="directory holding the ONNX models")
    vs.add_argument("--device", help="cpu (default), auto, cuda[:n], coreml, directml")
    vs.add_argument("-q", "--quiet", action="store_true", help="suppress the summary line")

    for reader in (scan, pdf, ocr, tiff, vs):
        reader.add_argument(
            "--password",
            help="password for encrypted PDFs; OCRUST_PASSWORD keeps it out of the shell history",
        )
        reader.add_argument(
            "--max-pixels",
            type=int,
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
    return 4 if inputs > 1 else None


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

    args._resolved_workers = _workers_for(args, len(inputs))
    engine = _engine_from_args(args)
    status = _scan_batch(args, engine, inputs, pages, destination)
    return max(status, 1) if blocked else status


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
    if many and args.output and args.output.suffix:
        _fail("--output must be a directory when reading several files")
        return None
    # A path without a suffix is a directory: `-o out` writes out/<name>.<ext>,
    # so switching --format does not overwrite the previous run's output.
    as_directory = bool(args.output) and (many or args.output.is_dir() or not args.output.suffix)
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
            print(f"\r  {_paint(text, 'dim')}\033[K", end="", file=sys.stderr, flush=True)
        else:
            # A log or a pipe: `\r` would run the whole run together on one line.
            print(f"  {text}", file=sys.stderr, flush=True)

    progress = report if getattr(args, "progress", False) else None

    failures = skipped = 0
    total_pages = total_lines = 0
    total_ms = 0.0
    for path in inputs:
        target = destination.target_for(path, args.format)
        if args.skip_existing and target is not None and target.exists():
            skipped += 1
            if not args.quiet:
                print(f"  {_paint(f'{path}: already written', 'dim')}", file=sys.stderr)
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
            print("\r\033[K" if live else "", end="" if live else "\n", file=sys.stderr)

        rendered = doc.render(args.format)
        if target is None:
            sys.stdout.write(rendered)
            if not rendered.endswith("\n"):
                sys.stdout.write("\n")
        else:
            _write(target, rendered, retries=_io_retries(args))
            if not args.quiet:
                print(_arrow(path, target), file=sys.stderr)

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
            print(f"  {body}", file=sys.stderr)

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
        print(_paint("done:", "rust", "bold") + " " + total, file=sys.stderr)
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
            print(file=sys.stderr)
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
    _write(target, data, retries=_io_retries(args))
    if not args.quiet:
        first = inputs[0] if len(inputs) == 1 else Path(f"{len(inputs)} inputs")
        print(_arrow(first, target), file=sys.stderr)
        print(f"  {pages} page(s), {_size(len(data))}", file=sys.stderr)
    return 0


def _cmd_ocr(args: argparse.Namespace) -> int:
    if not args.input.exists():
        _fail(f"no such file: {args.input}")
        return 2
    data = args.input.read_bytes()
    if not data.startswith(b"%PDF"):
        print(
            f"ocrust: {args.input} is not a PDF; use `ocrust pdf` to build a searchable "
            "PDF from an image",
            file=sys.stderr,
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
    _write(target, pdf, retries=_io_retries(args))
    if not args.quiet:
        print(_arrow(args.input, target), file=sys.stderr)
        print(
            f"  {report['pages_with_layer']} of {_count(report['pages'], 'page')} layered, "
            f"{report['pages_skipped']} skipped, {_count(report['lines'], 'line')}",
            file=sys.stderr,
        )
        if report["pages_skipped"] == report["pages"] and report["pages"]:
            _note("  every page already had text: --force writes a layer anyway")
    if _password(args) and not args.quiet:
        print(
            "note: the output is written without password protection; "
            "encrypt it again if it has to stay locked",
            file=sys.stderr,
        )
    if report["unmappable_chars"]:
        print(
            f"note: {report['unmappable_chars']} character(s) are outside WinAnsi and were "
            "written as '?' in the text layer (the visible page is unchanged)",
            file=sys.stderr,
        )
    return 0


def _cmd_tiff(args: argparse.Namespace) -> int:
    if not args.input.exists():
        _fail(f"no such file: {args.input}")
        return 2
    engine = Ocr(models_dir=args.models, pdf_dpi=args.dpi, lang=args.lang, **_guards(args))
    try:
        data, doc = engine.to_tiff(args.input, gray=args.gray, compression=args.compression)
    except (OcrustError, OSError, ValueError) as exc:
        _fail(str(exc))
        return 1
    target = args.output or args.input.with_suffix(".ocr.tiff")
    _write(target, data, retries=_io_retries(args))
    if not args.quiet:
        print(_arrow(args.input, target), file=sys.stderr)
        print(f"  {_count(len(doc.pages), 'page')}, {_size(len(data))}", file=sys.stderr)
    if args.sidecar:
        sidecar = target.with_suffix("." + _EXTENSIONS[args.sidecar])
        _write(sidecar, doc.render(args.sidecar), retries=_io_retries(args))
        if not args.quiet:
            print(_arrow(args.input, sidecar), file=sys.stderr)
    return 0


#: Exit status of `ocrust vs` when a file is marked at or above --fail-on.
_MARKED = 3


def _gate(spec: str) -> int | None:
    """`--fail-on` as a level; 0 means any marking at all, `None` never."""
    key = spec.strip().lower()
    if key == "none":
        return None
    if key == "any":
        return 0
    try:
        return markings.level_for(key)
    except ValueError as exc:
        raise _bad_argument(f"--fail-on: {exc}") from None


def _trips(report: markings.MarkingReport, gate: int | None) -> bool:
    if gate is None:
        return False
    if gate == 0:
        # Anything that restricts who may read it. OFFEN, UNCLASSIFIED and
        # TLP:CLEAR are markings too, but they say the opposite.
        return report.level >= 1 or report.tlp not in (None, "CLEAR") or bool(report.company)
    return report.level >= gate


def _scan_each(engine: Ocr, inputs: Sequence[Path], pages: Sequence[int] | None, chunk: int) -> Any:
    """Yields `(path, document or error message)` for every input, in order.

    Files are handed to the engine a chunk at a time, which scans the files of
    a chunk in parallel; a chunk keeps memory bounded and lets results appear
    while a long batch is still running. A file that fails is reported and the
    batch goes on — an unreadable file is a finding in an audit, not a reason
    to stop one.
    """
    plain = [p for p in inputs if str(p) != STDIN]
    if pages is None and len(plain) == len(inputs) and len(inputs) > 1:
        for start in range(0, len(inputs), chunk):
            batch = list(inputs[start : start + chunk])
            for path, raw in zip(batch, engine._engine.scan_many([str(p) for p in batch])):
                payload = json.loads(raw)
                if "error" in payload and "pages" not in payload:
                    yield path, str(payload["error"])
                else:
                    from ._types import Document

                    yield path, Document._from_json(payload)
        return
    for path in inputs:
        try:
            if str(path) == STDIN:
                data = _read_stdin()
                if not data:
                    yield path, "nothing arrived on stdin"
                    continue
                yield path, engine.scan(data, pages=pages, name="stdin")
            else:
                yield path, engine.scan(path, pages=pages)
        except (OcrustError, OSError, ValueError) as exc:
            yield path, str(exc)


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


def _vs_line(path: Path, report: markings.MarkingReport, show_mentions: bool, out: Any) -> str:
    """One file of the text report, with the evidence behind its grade."""
    head = _headline(report)
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
    style = ("red", "bold") if report.level >= 1 else ("amber",) if marked else ("dim",)
    line = f"{_paint(f'{head:<16}', *style, stream=out)} {path}"
    if details:
        line += "  " + _paint(" · ".join(details), "dim", stream=out)
    if show_mentions:
        for f in report.mentions:
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
)


def _cmd_vs(args: argparse.Namespace) -> int:
    import csv
    import io

    gate = _gate(args.fail_on)
    pages = _parse_pages(args.pages)
    inputs, missing = _expand_inputs(list(args.inputs))
    if missing:
        for path, reason in missing:
            _fail(f"{path}: {reason}")
        return 2
    if not inputs:
        _fail("nothing to read")
        return 2

    args._resolved_workers = _workers_for(args, len(inputs))
    # A stamp over the text is only readable by its colour; this is the
    # command that looks for stamps.
    args._read_stamps = True
    engine = _engine_from_args(args)
    chunk = max(8, 4 * (args._resolved_workers or 1))

    buffer = io.StringIO()
    out: Any = buffer if args.output else sys.stdout
    writer = csv.DictWriter(out, fieldnames=_CSV_FIELDS) if args.format == "csv" else None
    if writer is not None:
        writer.writeheader()
    records: list[dict[str, Any]] = []
    tally: dict[str, int] = {}
    marked = unreadable = tripped = 0
    started = time.monotonic()
    page_total = 0

    for path, result in _scan_each(engine, inputs, pages, chunk):
        if isinstance(result, str):
            unreadable += 1
            record: dict[str, Any] = {"source": str(path), "status": "error", "error": result}
            if args.format == "text":
                label = _paint(f"{'unreadable':<16}", "red", stream=out)
                print(f"{label} {path}  {result}", file=out)
            elif writer is not None:
                writer.writerow({"source": str(path), "status": "error", "text": result})
        else:
            report = markings.inspect(result, file=None if str(path) == STDIN else path)
            page_total += len(result.pages)
            if _trips(report, gate):
                tripped += 1
            if report.markings:
                marked += 1
                tally[_headline(report)] = tally.get(_headline(report), 0) + 1
            record = {"source": str(path), "status": "ok", **report.to_dict()}
            record["source"] = str(path)
            if args.format == "text":
                print(_vs_line(path, report, args.mentions, out), file=out)
            elif writer is not None:
                if not report.findings:
                    writer.writerow({"source": str(path), "status": "ok"})
                for f in report.findings:
                    x0, y0, x1, y1 = f.box.as_tuple()
                    writer.writerow(
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
        if args.format == "jsonl":
            print(json.dumps(record, ensure_ascii=False), file=out)
        elif args.format == "json":
            records.append(record)
        if out is sys.stdout:
            sys.stdout.flush()

    clean = len(inputs) - marked - unreadable
    if args.format == "json":
        summary = {
            "files": len(inputs),
            "marked": marked,
            "clean": clean,
            "unreadable": unreadable,
            "fail_on": args.fail_on,
            "tripped": tripped,
            "by_label": tally,
        }
        json.dump({"summary": summary, "files": records}, out, ensure_ascii=False, indent=2)
        out.write("\n")
    if args.output:
        _write(args.output, buffer.getvalue(), retries=_io_retries(args))

    if not args.quiet:
        parts = [_count(len(inputs), "file")]
        if tally:
            listed = ", ".join(
                f"{n} {label}" for label, n in sorted(tally.items(), key=lambda kv: -kv[1])
            )
            parts.append(_paint(f"{marked} marked ({listed})", "red", "bold"))
        parts.append(f"{clean} clean")
        if unreadable:
            parts.append(_paint(f"{unreadable} unreadable", "red"))
        parts.append(
            f"{_count(page_total, 'page')}, {_duration((time.monotonic() - started) * 1000)}"
        )
        if args.output:
            parts.append(f"report -> {args.output}")
        print(_paint("done:", "rust", "bold") + " " + ", ".join(parts), file=sys.stderr)

    if tripped:
        return _MARKED
    return 1 if unreadable else 0


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
        print(file=sys.stderr)
        _fail("interrupted")
        return 130
    except BrokenPipeError:
        # `ocrust scan book.pdf | head -1`: the reader left, which is its right.
        # Python still holds a dead stdout and would print "Exception ignored"
        # while flushing it at exit, so it is pointed at the void first. A stdout
        # without a file descriptor (a test harness, say) has nothing to redirect.
        with contextlib.suppress(OSError, ValueError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


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
