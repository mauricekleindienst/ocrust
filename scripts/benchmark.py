#!/usr/bin/env python3
"""Benchmark ocrust against other Python OCR packages on identical pixels.

    pip install ocrust[models] rapidocr-onnxruntime pytesseract easyocr pypdfium2
    python scripts/benchmark.py page.png --runs 5

Every engine is handed the same decoded image, so the comparison is about OCR
work and not about file loading. Engines that are not installed are skipped.
PDF input is rasterized once with pypdfium2 (not with ocrust) so that no engine
gets an advantage from the renderer.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path
from typing import Callable


def _load_image(path: Path, dpi: float) -> object:
    """Returns the page as a numpy array, rasterizing PDFs if needed."""
    import numpy as np

    if path.suffix.lower() == ".pdf":
        try:
            import pypdfium2
        except ImportError as exc:
            raise SystemExit(
                "PDF input needs `pip install pypdfium2` for a neutral rasterizer"
            ) from exc
        page = pypdfium2.PdfDocument(str(path))[0]
        bitmap = page.render(scale=dpi / 72.0)
        return np.asarray(bitmap.to_pil().convert("RGB"))

    from PIL import Image

    return np.asarray(Image.open(path).convert("RGB"))


def _time(fn: Callable[[], str], runs: int) -> tuple[float, str]:
    """Warms up once, then returns the median duration in ms and the output."""
    text = fn()
    timings = []
    for _ in range(runs):
        started = time.perf_counter()
        text = fn()
        timings.append((time.perf_counter() - started) * 1000)
    return statistics.median(timings), text


def bench_ocrust(image, runs: int):
    import ocrust

    engine = ocrust.Ocr(page_workers=1)
    return _time(lambda: engine.scan(image, name="bench").text, runs)


def bench_rapidocr(image, runs: int):
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()

    def run() -> str:
        result, _ = engine(image)
        return "\n".join(line[1] for line in (result or []))

    return _time(run, runs)


def bench_pytesseract(image, runs: int):
    import pytesseract

    return _time(lambda: pytesseract.image_to_string(image), runs)


def bench_easyocr(image, runs: int):
    import easyocr

    reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _time(lambda: "\n".join(reader.readtext(image, detail=0)), runs)


def bench_paddleocr(image, runs: int):
    from paddleocr import PaddleOCR

    engine = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)

    def run() -> str:
        result = engine.ocr(image, cls=True)
        lines = result[0] if result and result[0] else []
        return "\n".join(item[1][0] for item in lines)

    return _time(run, runs)


ENGINES: dict[str, Callable] = {
    "ocrust": bench_ocrust,
    "rapidocr": bench_rapidocr,
    "pytesseract": bench_pytesseract,
    "easyocr": bench_easyocr,
    "paddleocr": bench_paddleocr,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", type=Path, help="image or single-page PDF")
    parser.add_argument("--runs", type=int, default=5, help="timed runs per engine")
    parser.add_argument("--dpi", type=float, default=200.0, help="rasterization DPI for PDFs")
    parser.add_argument("--only", nargs="*", choices=sorted(ENGINES), help="limit to these engines")
    args = parser.parse_args(argv)

    if not args.image.exists():
        raise SystemExit(f"no such file: {args.image}")
    image = _load_image(args.image, args.dpi)
    height, width = image.shape[:2]
    print(f"input: {args.image} ({width}x{height}), {args.runs} timed run(s) each\n")

    rows = []
    for name, fn in ENGINES.items():
        if args.only and name not in args.only:
            continue
        try:
            ms, text = fn(image, args.runs)
        except ImportError:
            print(f"{name:<12} not installed — skipped")
            continue
        except Exception as exc:  # pragma: no cover - engine specific
            print(f"{name:<12} failed: {exc}")
            continue
        chars = len(text.replace("\n", ""))
        rows.append((name, ms, chars, text))
        print(f"{name:<12} {ms:8.1f} ms   {chars:4d} chars")

    if len(rows) > 1:
        fastest = min(rows, key=lambda r: r[1])
        print(f"\nfastest: {fastest[0]} ({fastest[1]:.1f} ms)")
        for name, ms, _, _ in rows:
            if name != fastest[0]:
                print(f"  {fastest[0]} is {ms / fastest[1]:.2f}x faster than {name}")

    print("\n--- recognized text ---")
    for name, _, _, text in rows:
        preview = text.replace("\n", " | ")[:100]
        print(f"{name:<12} {preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
