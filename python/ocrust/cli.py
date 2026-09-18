"""Command line interface: ``ocrust``.

    ocrust scan invoice.pdf                 # text to stdout
    ocrust scan *.jpg -f markdown -o out/   # batch, one file per input
    ocrust pdf scan.jpg -o scan.pdf         # searchable PDF
    ocrust doctor                           # what is installed, what is missing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import FORMATS, Ocr, OcrustError, __version__, runtime_info

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
    scan.add_argument("inputs", nargs="+", type=Path, help="files to read")
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
    scan.add_argument("--workers", type=int, help="pages scanned in parallel")
    scan.add_argument("--dpi", type=float, help="PDF rasterization DPI (default 200)")
    scan.add_argument("--models", type=Path, help="directory holding the ONNX models")
    scan.add_argument("--no-preprocess", action="store_true", help="skip deskew/invert/rescale")
    scan.add_argument("--no-word-boxes", action="store_true", help="skip per-word geometry")
    scan.add_argument(
        "--min-confidence",
        type=float,
        help="drop lines below this mean confidence (0..1)",
    )
    scan.add_argument("-q", "--quiet", action="store_true", help="suppress the summary line")

    pdf = sub.add_parser("pdf", help="write a searchable PDF (image plus text layer)")
    pdf.add_argument("input", type=Path)
    pdf.add_argument("-o", "--output", type=Path, help="defaults to <input>.ocr.pdf")
    pdf.add_argument("--dpi", type=float, help="assumed page resolution")
    pdf.add_argument("--quality", type=int, default=80, help="JPEG quality (default 80)")
    pdf.add_argument("--models", type=Path)
    pdf.add_argument("--device")

    doctor = sub.add_parser("doctor", help="show runtime and model diagnostics")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")

    models = sub.add_parser("models", help="show which model files would be used")
    models.add_argument("--models", type=Path, dest="models_dir")

    return parser


def _parse_pages(spec: str | None) -> list[int] | None:
    """Turns ``"1,3-5"`` into zero-based indices ``[0, 2, 3, 4]``."""
    if not spec:
        return None
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            first, last = int(start), int(end)
            if first < 1 or last < first:
                raise SystemExit(f"ocrust: bad page range {part!r}")
            pages.extend(range(first - 1, last))
        else:
            number = int(part)
            if number < 1:
                raise SystemExit("ocrust: page numbers start at 1")
            pages.append(number - 1)
    return sorted(set(pages))


def _engine_from_args(args: argparse.Namespace) -> Ocr:
    return Ocr(
        models_dir=getattr(args, "models", None),
        device=getattr(args, "device", None),
        threads=getattr(args, "threads", None),
        page_workers=getattr(args, "workers", None),
        pdf_dpi=getattr(args, "dpi", None),
        preprocess=not getattr(args, "no_preprocess", False),
        word_boxes=not getattr(args, "no_word_boxes", False),
        drop_score=getattr(args, "min_confidence", None),
    )


def _cmd_scan(args: argparse.Namespace) -> int:
    pages = _parse_pages(args.pages)
    engine = _engine_from_args(args)

    missing = [p for p in args.inputs if not p.exists()]
    if missing:
        for path in missing:
            print(f"ocrust: no such file: {path}", file=sys.stderr)
        return 2

    many = len(args.inputs) > 1
    if many and args.output and args.output.suffix:
        print("ocrust: --output must be a directory when reading several files", file=sys.stderr)
        return 2
    if args.output and (many or args.output.is_dir()):
        args.output.mkdir(parents=True, exist_ok=True)

    failures = 0
    for path in args.inputs:
        try:
            doc = engine.scan(path, pages=pages)
        except (OcrustError, OSError, ValueError) as exc:
            print(f"ocrust: {path}: {exc}", file=sys.stderr)
            failures += 1
            continue

        rendered = doc.render(args.format)
        if args.output is None:
            sys.stdout.write(rendered)
            if not rendered.endswith("\n"):
                sys.stdout.write("\n")
        else:
            target = args.output
            if many or target.is_dir():
                target = target / f"{path.stem}.{_EXTENSIONS[args.format]}"
            target.write_text(rendered, encoding="utf-8")
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
    target.write_bytes(data)
    print(f"{args.input} -> {target} ({len(data) / 1e6:.1f} MB)", file=sys.stderr)
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


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``ocrust`` console script."""
    args = _build_parser().parse_args(argv)
    if args.command == "scan":
        return _cmd_scan(args)
    if args.command == "pdf":
        return _cmd_pdf(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "models":
        return _cmd_models(args)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
