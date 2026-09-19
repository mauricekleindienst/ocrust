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
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from . import FORMATS, Ocr, OcrustError, __version__, _glob, runtime_info

_EXTENSIONS = {
    "text": "txt",
    "markdown": "md",
    "json": "json",
    "hocr": "hocr.html",
    "alto": "alto.xml",
    "csv": "csv",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocrust",
        description="Fast document OCR: images, multi-page TIFF and PDF.",
    )
    parser.add_argument("--version", action="version", version=f"ocrust {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="read documents and write the text out")
    scan.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="files, directories (read recursively) or glob patterns",
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
        "--workers",
        type=int,
        help="pages scanned in parallel (default: 4 for several inputs, 1 otherwise)",
    )
    scan.add_argument("--dpi", type=float, help="PDF rasterization DPI (default 200)")
    scan.add_argument("--models", type=Path, help="directory holding the ONNX models")
    scan.add_argument("--no-preprocess", action="store_true", help="skip deskew/invert/rescale")
    scan.add_argument("--no-word-boxes", action="store_true", help="skip per-word geometry")
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
    scan.add_argument("-q", "--quiet", action="store_true", help="suppress the summary line")

    pdf = sub.add_parser("pdf", help="write a searchable PDF (image plus text layer)")
    pdf.add_argument("input", type=Path)
    pdf.add_argument("-o", "--output", type=Path, help="defaults to <input>.ocr.pdf")
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
    tiff.add_argument("--sidecar", choices=FORMATS, help="also write the text in this format")
    tiff.add_argument("--dpi", type=float)
    tiff.add_argument("--models", type=Path)
    tiff.add_argument("--lang")
    tiff.add_argument("-q", "--quiet", action="store_true", help="suppress the summary line")

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

    models = sub.add_parser("models", help="show which model files would be used")
    models.add_argument("--models", type=Path, dest="models_dir")

    return parser


def _bad_argument(message: str) -> SystemExit:
    """Exits 2, which is what the documented table calls a bad argument.

    `SystemExit("text")` prints the text but exits 1, the code reserved for a
    file that failed to scan.
    """
    print(f"ocrust: {message}", file=sys.stderr)
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


#: Suffixes worth reading when a directory is handed in. Content sniffing
#: decides what a file really is, but a folder should not be opened blind.
READABLE_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff",
        ".pnm", ".pbm", ".pgm", ".ppm", ".tga", ".dds", ".hdr", ".exr",
        ".qoi", ".ico", ".pdf",
    }
)  # fmt: skip


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
    """Writes a file, retrying the failures a network share produces.

    The read side has the same policy in the engine; output deserves it too,
    because `-o \\\\fileserver\\ocr` is exactly where a batch writes hundreds of
    small files and one dropped SMB connection would otherwise end the run.
    """
    for attempt in range(retries + 1):
        try:
            if isinstance(data, str):
                target.write_text(data, encoding="utf-8")
            else:
                target.write_bytes(data)
            return
        except _TRANSIENT_WRITE_ERRORS:
            if attempt == retries:
                raise
            time.sleep(delay * (2**attempt))
        except OSError as exc:
            # Windows reports a dropped share as a plain OSError carrying the
            # redirector's own code: network name deleted, unexpected network
            # error, no system resources.
            transient = getattr(exc, "winerror", None) in _TRANSIENT_WINDOWS_ERRORS
            if attempt == retries or not transient:
                raise
            time.sleep(delay * (2**attempt))


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


def _engine_from_args(args: argparse.Namespace) -> Ocr:
    return Ocr(
        models_dir=getattr(args, "models", None),
        device=getattr(args, "device", None),
        threads=getattr(args, "threads", None),
        page_workers=getattr(args, "_resolved_workers", None) or getattr(args, "workers", None),
        pdf_dpi=getattr(args, "dpi", None),
        preprocess=not getattr(args, "no_preprocess", False),
        word_boxes=not getattr(args, "no_word_boxes", False),
        drop_score=getattr(args, "min_confidence", None),
        lang=getattr(args, "lang", None),
        io_retries=getattr(args, "io_retries", None),
    )


def _cmd_scan(args: argparse.Namespace) -> int:
    pages = _parse_pages(args.pages)

    args.inputs, missing = _expand_inputs(args.inputs)
    if missing:
        for path, reason in missing:
            print(f"ocrust: {path}: {reason}", file=sys.stderr)
        return 2
    if not args.inputs:
        print("ocrust: nothing to read", file=sys.stderr)
        return 2

    args._resolved_workers = _workers_for(args, len(args.inputs))
    engine = _engine_from_args(args)

    many = len(args.inputs) > 1
    if many and args.output and args.output.suffix:
        print("ocrust: --output must be a directory when reading several files", file=sys.stderr)
        return 2
    # A path without a suffix is a directory: `-o out` writes out/<name>.<ext>,
    # so switching --format does not overwrite the previous run's output.
    write_to_dir = bool(args.output) and (many or args.output.is_dir() or not args.output.suffix)
    if write_to_dir:
        args.output.mkdir(parents=True, exist_ok=True)

    def report(page: int, total: int, lines: int) -> None:
        print(f"\r  page {page + 1}/{total}, {lines} line(s)", end="", file=sys.stderr, flush=True)

    progress = report if getattr(args, "progress", False) else None

    failures = 0
    for path in args.inputs:
        try:
            doc = engine.scan(path, pages=pages, progress=progress)
        except (OcrustError, OSError, ValueError) as exc:
            print(f"ocrust: {path}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if progress is not None:
            print("", file=sys.stderr)  # close the progress line

        rendered = doc.render(args.format)
        if args.output is None:
            sys.stdout.write(rendered)
            if not rendered.endswith("\n"):
                sys.stdout.write("\n")
        else:
            target = args.output
            if write_to_dir:
                target = target / f"{path.stem}.{_EXTENSIONS[args.format]}"
            _write(target, rendered, retries=_io_retries(args))
            if not args.quiet:
                print(f"{path} -> {target}", file=sys.stderr)

        if not args.quiet:
            confidence = doc.confidence
            print(
                f"  {len(doc.pages)} page(s), {len(doc.lines)} line(s), "
                f"confidence {confidence * 100:.1f}%, {doc.elapsed_ms:.0f} ms"
                if confidence is not None
                else f"  {len(doc.pages)} page(s), no text found, {doc.elapsed_ms:.0f} ms",
                file=sys.stderr,
            )
    return 1 if failures else 0


def _cmd_pdf(args: argparse.Namespace) -> int:
    if not args.input.exists():
        print(f"ocrust: no such file: {args.input}", file=sys.stderr)
        return 2
    engine = Ocr(models_dir=args.models, device=args.device, keep_page_images=True)
    try:
        data = engine.searchable_pdf(args.input, dpi=args.dpi, jpeg_quality=args.quality)
    except (OcrustError, OSError, ValueError) as exc:
        print(f"ocrust: {exc}", file=sys.stderr)
        return 1
    target = args.output or args.input.with_suffix(".ocr.pdf")
    _write(target, data)
    if not args.quiet:
        print(f"{args.input} -> {target} ({len(data) / 1e6:.1f} MB)", file=sys.stderr)
    return 0


def _cmd_ocr(args: argparse.Namespace) -> int:
    if not args.input.exists():
        print(f"ocrust: no such file: {args.input}", file=sys.stderr)
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
        print(f"ocrust: {exc}", file=sys.stderr)
        return 1

    target = args.output or args.input.with_suffix(".ocr.pdf")
    _write(target, pdf)
    if not args.quiet:
        print(
            f"{args.input} -> {target}: {report['pages_with_layer']} of {report['pages']} page(s) "
            f"got a text layer, {report['pages_skipped']} skipped, {report['lines']} line(s)",
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
        print(f"ocrust: no such file: {args.input}", file=sys.stderr)
        return 2
    engine = Ocr(models_dir=args.models, pdf_dpi=args.dpi, lang=args.lang)
    try:
        data, doc = engine.to_tiff(args.input, gray=args.gray)
    except (OcrustError, OSError, ValueError) as exc:
        print(f"ocrust: {exc}", file=sys.stderr)
        return 1
    target = args.output or args.input.with_suffix(".ocr.tiff")
    _write(target, data)
    if not args.quiet:
        print(
            f"{args.input} -> {target}: {len(doc.pages)} page(s), {len(data) / 1e6:.1f} MB",
            file=sys.stderr,
        )
    if args.sidecar:
        sidecar = target.with_suffix("." + _EXTENSIONS[args.sidecar])
        _write(sidecar, doc.render(args.sidecar), retries=_io_retries(args))
        if not args.quiet:
            print(f"{args.input} -> {sidecar}", file=sys.stderr)
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
            print(f"ocrust: {exc}", file=sys.stderr)
            return 1
        entries = list(engine.languages)
        label = f"covered by the installed model ({engine.charset_size} characters)"

    if args.json:
        print(json.dumps(entries, indent=2))
        return 0

    print(f"{len(entries)} language(s) {label}:\n")
    by_script: dict[str, list[str]] = {}
    for entry in entries:
        by_script.setdefault(str(entry["script"]), []).append(f"{entry['code']} ({entry['name']})")
    for script in sorted(by_script):
        print(f"  {script:<11} {', '.join(sorted(by_script[script]))}")

    if not args.all:
        near = Ocr(models_dir=args.models).partial_languages(0.8)
        if near:
            print("\nnearly covered (a few characters missing):")
            for entry in near:
                print(
                    f"  {entry['code']} ({entry['name']}): {entry['ratio'] * 100:.0f}%"
                    f", missing {entry['missing']}"
                )
    return 0


def _cmd_install_models(args: argparse.Namespace) -> int:
    from . import install_models

    try:
        paths = install_models()
    except Exception as exc:
        print(f"ocrust: {exc}", file=sys.stderr)
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
    print(f"ocrust            {info['ocrust']}")
    print(f"python            {info['python']} on {info['platform']}")
    print(f"onnxruntime       {info.get('onnxruntime_version') or 'not installed'}")
    print(f"  library         {info.get('onnxruntime_dylib') or 'NOT FOUND'}")
    print(f"  loaded          {info.get('onnxruntime_loaded')}")
    print(f"models dir        {info.get('models_dir') or 'not set'}")
    print(f"models cache      {info.get('models_cache_dir')}")
    models = info.get("models")
    if isinstance(models, dict):
        for key, value in models.items():
            print(f"  {key:<14} {value or '-'}")
    else:
        print(f"  models          {models}")
    ready = info.get("onnxruntime_dylib") and isinstance(models, dict)
    print("\nstatus: ready" if ready else "\nstatus: not ready — see the hints above")
    return 0 if ready else 1


def _cmd_models(args: argparse.Namespace) -> int:
    try:
        engine = Ocr(models_dir=args.models_dir)
    except OcrustError as exc:
        print(f"ocrust: {exc}", file=sys.stderr)
        return 1
    for key, value in engine.models.items():
        print(f"{key:<12} {value or '-'}")
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
    except KeyboardInterrupt:
        print("\nocrust: interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # `ocrust scan book.pdf | head -1`: the reader left, which is its right.
        # Python still holds a dead stdout and would print "Exception ignored"
        # while flushing it at exit, so it is pointed at the void first.
        # A stdout without a file descriptor (a test harness, say) has nothing
        # to redirect, and nothing to flush either.
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
    if args.command == "languages":
        return _cmd_languages(args)
    if args.command == "install-models":
        return _cmd_install_models(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "models":
        return _cmd_models(args)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
