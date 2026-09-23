"""ocrust — fast document OCR for Python, with a Rust core.

    >>> import ocrust
    >>> ocrust.read("invoice.pdf")          # doctest: +SKIP
    'INVOICE 2026-0042\\nTotal: 199.90 EUR'

Reads images (PNG, JPEG, WebP, TIFF, BMP, GIF, …), multi-page TIFF and PDF.
No Tesseract, no PaddlePaddle, no PyTorch: the engine is a self-contained Rust
extension that runs PP-OCR models through ONNX Runtime.

Structured results, when you need more than the text::

    doc = ocrust.scan("scan.jpg")
    doc.text                      # plain text, reading order applied
    doc.markdown()                # headings, lists, paragraphs
    doc.hocr()                    # hOCR for downstream tooling
    for line in doc.lines:
        print(line.text, line.confidence, line.box.as_tuple())

Reuse an engine when scanning more than one file — it loads the models once::

    ocr = ocrust.Ocr(device="auto", page_workers=8)
    for doc in ocr.scan_many(["a.pdf", "b.png"]):
        print(doc.text)
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from ._runtime import default_models_dir, ensure_runtime, runtime_report
from ._types import Block, Box, Cell, Document, Line, Match, Page, Segment, Table, Word

# The extension dlopens libonnxruntime on first use, so the path has to be in
# the environment before it is imported.
ensure_runtime()

from . import _ocrust, markings  # noqa: E402  (import must follow ensure_runtime)

__all__ = [
    "Ocr",
    "markings",
    "Document",
    "Page",
    "Block",
    "Line",
    "Word",
    "Segment",
    "Table",
    "Cell",
    "Match",
    "Box",
    "read",
    "scan",
    "scan_many",
    "searchable_pdf",
    "searchable_pdf_many",
    "ocr_pdf",
    "to_tiff",
    "languages",
    "known_languages",
    "install_models",
    "models_cache_dir",
    "runtime_info",
    "OcrustError",
    "__version__",
]

__version__ = _ocrust.__version__

#: Formats accepted by :meth:`Document.render`.
FORMATS = ("text", "markdown", "json", "hocr", "alto", "csv")


class OcrustError(RuntimeError):
    """Raised when the engine cannot be built or a document cannot be read."""


def _attach_render_methods() -> None:
    """Adds the export helpers to :class:`Document`.

    Rendering happens in Rust against the document JSON, so the formats stay in
    exactly one place.
    """

    def render(self: Document, format: str = "text") -> str:
        """Renders the document as ``text``, ``markdown``, ``json``, ``hocr``,
        ``alto`` or ``csv``."""
        return _ocrust.render_document(json.dumps(self.to_dict()), format)

    def markdown(self: Document) -> str:
        """Markdown with headings, bullet lists and paragraphs."""
        return render(self, "markdown")

    def hocr(self: Document) -> str:
        """hOCR (HTML) with line and word geometry."""
        return render(self, "hocr")

    def alto(self: Document) -> str:
        """ALTO XML, as used by archives and digital libraries."""
        return render(self, "alto")

    def csv(self: Document) -> str:
        """One CSV row per recognized line."""
        return render(self, "csv")

    def json_(self: Document, indent: int | None = 2) -> str:
        """The full result as JSON, including every box and score."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    Document.render = render  # type: ignore[attr-defined]
    Document.markdown = markdown  # type: ignore[attr-defined]
    Document.hocr = hocr  # type: ignore[attr-defined]
    Document.alto = alto  # type: ignore[attr-defined]
    Document.csv = csv  # type: ignore[attr-defined]
    Document.json = json_  # type: ignore[attr-defined]


_attach_render_methods()


def _coerce_image(obj: Any) -> tuple[bytes, int, int] | None:
    """Converts a numpy array or PIL image into raw RGB bytes.

    Returns ``None`` when `obj` is not an image-like object, so callers can fall
    back to treating it as a path or encoded bytes. Neither numpy nor Pillow is
    a dependency; both are used only if the caller already has them.
    """
    # PIL.Image
    if hasattr(obj, "convert") and hasattr(obj, "size") and hasattr(obj, "tobytes"):
        rgb = obj.convert("RGB")
        width, height = rgb.size
        return rgb.tobytes(), width, height

    # numpy array (H, W, 3) / (H, W) / (H, W, 4)
    shape = getattr(obj, "shape", None)
    if shape is None or len(shape) not in (2, 3):
        return None
    try:
        import numpy as np
    except Exception:  # pragma: no cover - numpy present whenever arrays are
        return None

    array = np.asarray(obj)
    if array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)
    if array.ndim == 2:
        array = np.repeat(array[:, :, None], 3, axis=2)
    elif array.shape[2] == 4:
        array = array[:, :, :3]
    elif array.shape[2] != 3:
        raise ValueError(f"expected 1, 3 or 4 channels, got {array.shape[2]}")
    array = np.ascontiguousarray(array)
    height, width = array.shape[:2]
    return array.tobytes(), width, height


#: Suffixes worth reading when a directory is handed to :meth:`Ocr.scan_many`.
#: Content sniffing decides what a file really is, but a folder should not be
#: opened blind.
#: Taken from the reader itself, so this cannot drift from what it can open.
READABLE_SUFFIXES = frozenset(f".{suffix}" for suffix in _ocrust.supported_suffixes())


def _glob(pattern: Path) -> list[Path]:
    """Files matching a glob pattern, sorted.

    Anchored at the last directory that is spelled out, so the magic part may
    span directories: ``reports/**/*.pdf`` has to walk, and ``Path.glob`` only
    does that when the pattern it is given carries the ``**`` itself.
    """
    parts = pattern.parts
    magic = next(
        (i for i, part in enumerate(parts) if any(ch in part for ch in "*?[")),
        len(parts),
    )
    base = Path(*parts[:magic]) if magic else Path()
    try:
        return sorted(item for item in base.glob(str(Path(*parts[magic:]))) if item.is_file())
    except (OSError, ValueError):
        # An unreachable share, or a pattern the platform rejects.
        return []


def _expand_sources(sources: Iterable[Any]) -> list[Any]:
    """Expands directories and glob patterns, keeping everything else as is.

    Files the caller named stay exactly as given, duplicates included — one
    document comes back per input, which is what makes ``zip(paths, docs)``
    work. Only *expanded* files are de-duplicated, because two overlapping
    patterns should not read the same file twice.
    """
    out: list[Any] = []
    expanded: set[Path] = set()

    def add_expanded(found: Iterable[Path]) -> None:
        for item in found:
            if item not in expanded:
                expanded.add(item)
                out.append(item)

    for source in sources:
        if not isinstance(source, (str, os.PathLike)):
            out.append(source)
            continue
        path = Path(source)
        if path.is_dir():
            add_expanded(
                sorted(
                    child
                    for child in path.rglob("*")
                    if child.is_file() and child.suffix.lower() in READABLE_SUFFIXES
                )
            )
        elif not path.exists() and any(ch in str(path) for ch in "*?["):
            add_expanded(_glob(path))
        else:
            out.append(path)
    return out


class Ocr:
    """A loaded OCR engine.

    Building one loads the models and prepares the inference sessions, so keep
    it around and call :meth:`scan` repeatedly. Instances are thread-safe and
    release the GIL while scanning.

    Args:
        models_dir: Directory holding the ONNX models. Defaults to the models
            shipped by ``ocrust-models``, ``OCRUST_MODELS_DIR``, then the
            per-user cache.
        device: ``"cpu"`` (default), ``"auto"``, ``"cuda"``, ``"cuda:1"``,
            ``"coreml"`` or ``"directml"``. Accelerators need a matching build.
        threads: Threads per inference operator. ``None`` lets the runtime decide.
        page_workers: Pages scanned in parallel. ``None`` means one per core.
        pdf_dpi: Rasterization resolution for PDF pages (default 200).
        password: Password for encrypted PDFs. PDFs protected by an owner
            password alone open without one. A text layer added to a
            password-protected PDF is written without the password.
        max_pixels: Largest image, in pixels, that will be decoded — a guard
            against decompression bombs, checked before any decoding happens.
            Defaults to Pillow's threshold (about 179 million), which admits an
            A0 sheet at 300 dpi. ``0`` removes the limit.
        io_retries: Extra attempts when reading a file fails transiently
            (default 2). Reading off a network share is not a local read: SMB
            and NFS time out and drop connections for reasons that have nothing
            to do with the file, and a batch of four hundred documents should
            not die on one of them. ``0`` disables it.
        preprocess: Auto-invert, deskew and rescale pages before OCR.
        word_boxes: Compute per-word boxes (needed for hOCR/ALTO word output).
        memory: ``"frugal"`` (the default) or ``"fast"``. Frugal stops ONNX
            Runtime keeping an allocation arena and planning tensor reuse, both
            of which assume the tensor shapes repeat — pages are all different
            sizes, so they hold memory they never reuse. Measured over a 40-page
            PDF: 253 MB peak instead of 584 MB, for 10% more time at one page
            worker and no extra time at four (where it is 991 MB against
            2006 MB). Choose ``"fast"`` when the time matters more.
        tables: Read blocks whose cells line up into columns as tables
            (:attr:`Document.tables`, and Markdown pipe tables in
            ``render("markdown")``). Set it to False to keep every block as
            running text.
        drop_score: Minimum mean confidence for a line to be kept.
        read_stamps: Read coloured ink — a red or blue stamp across the text —
            a second time on its own; lines found only that way arrive as
            blocks of kind ``"stamp"``. A stamp over black text is otherwise
            lost among the lines it crosses. A page without coloured ink
            costs about 1 % more, a page with a stamp about 13 %.
            ``ocrust vs`` turns it on; it is off by default because it is for
            finding stamps, not for reading text.
        tick_boxes: Find the tick boxes on forms and whether each is ticked;
            each arrives as a block of kind ``"tick_box"`` reading ``☒`` or
            ``☐``. The recognizer reads the words beside a box but hardly
            ever the box, so "☐ offen ☒ VS-NfD" otherwise loses the choice.
        lang: Languages the documents are in (``"de"``, ``["de", "fr"]``,
            ``"de,fr"``). Building the engine fails when the recognition model
            cannot spell one of them, because a model that silently drops ``ö``
            and ``ß`` returns text that looks right and is wrong.
    """

    def __init__(
        self,
        models_dir: str | os.PathLike[str] | None = None,
        *,
        device: str | None = None,
        threads: int | None = None,
        page_workers: int | None = None,
        memory: str | None = None,
        pdf_dpi: float | None = None,
        password: str | None = None,
        max_pixels: int | None = None,
        io_retries: int | None = None,
        preprocess: bool = True,
        deskew: bool | None = None,
        word_boxes: bool = True,
        drop_score: float | None = None,
        detection_model: str | os.PathLike[str] | None = None,
        recognition_model: str | os.PathLike[str] | None = None,
        orientation_model: str | os.PathLike[str] | None = None,
        dictionary: str | os.PathLike[str] | None = None,
        fix_orientation: bool = True,
        det_limit_side: int | None = None,
        det_box_threshold: float | None = None,
        det_unclip_ratio: float | None = None,
        rec_batch_size: int | None = None,
        rec_image_height: int | None = None,
        rec_space_gap: float | None = None,
        keep_page_images: bool = False,
        tables: bool = True,
        lang: str | Sequence[str] | None = None,
        read_stamps: bool = False,
        tick_boxes: bool = False,
    ) -> None:
        if isinstance(lang, str):
            languages = [part.strip() for part in lang.replace(",", " ").split() if part.strip()]
        elif lang is None:
            languages = None
        else:
            languages = [str(item) for item in lang]

        resolved = Path(models_dir) if models_dir is not None else default_models_dir()
        # Kept so a searchable-PDF engine can be built with the same settings.
        self._kwargs: dict[str, Any] = {
            "models_dir": str(resolved) if resolved else None,
            "detection_model": str(detection_model) if detection_model else None,
            "recognition_model": str(recognition_model) if recognition_model else None,
            "orientation_model": str(orientation_model) if orientation_model else None,
            "dictionary": str(dictionary) if dictionary else None,
            "device": device,
            "threads": threads,
            "page_workers": page_workers,
            "memory": memory,
            "pdf_dpi": pdf_dpi,
            "pdf_password": password,
            "max_pixels": max_pixels,
            "io_retries": io_retries,
            "preprocess": preprocess,
            "deskew": deskew,
            "word_boxes": word_boxes,
            "keep_page_images": keep_page_images,
            "drop_score": drop_score,
            "det_limit_side": det_limit_side,
            "det_box_threshold": det_box_threshold,
            "det_unclip_ratio": det_unclip_ratio,
            "rec_batch_size": rec_batch_size,
            "rec_image_height": rec_image_height,
            "rec_space_gap": rec_space_gap,
            "fix_orientation": fix_orientation,
            "tables": tables,
            "languages": languages,
            "read_stamps": read_stamps,
            "tick_boxes": tick_boxes,
        }
        self._keeps_images = keep_page_images
        self._pdf_engine_cache: Any = None
        try:
            self._engine = _ocrust.Engine(**self._kwargs)
        except Exception as exc:  # pragma: no cover - depends on the environment
            raise OcrustError(str(exc)) from exc

    @property
    def models(self) -> dict[str, str | None]:
        """The model files in use."""
        detection, recognition, orientation, dictionary = self._engine.models()
        return {
            "detection": detection,
            "recognition": recognition,
            "orientation": orientation,
            "dictionary": dictionary,
        }

    def scan(
        self,
        source: Any,
        *,
        pages: Sequence[int] | None = None,
        name: str | None = None,
        progress: Callable[[int, int, int], None] | None = None,
    ) -> Document:
        """Scans a path, bytes, numpy array or PIL image.

        Args:
            source: File path, encoded bytes, ``numpy`` array or PIL image.
            pages: Zero-based page indices to read from a multi-page source.
            name: Label used in the result when `source` is not a path.
            progress: Called after every page with ``(page_index, total_pages,
                lines)``. A 30-page PDF takes half a minute; this is how you show
                that something is happening. Raising inside it aborts the scan.
        """
        page_list = list(pages) if pages is not None else None

        image = _coerce_image(source)
        if image is not None:
            data, width, height = image
            raw = self._engine.scan_rgb(data, width, height, name or "<image>", page_list, progress)
        elif isinstance(source, (bytes, bytearray, memoryview)):
            raw = self._engine.scan_bytes(bytes(source), name or "<bytes>", page_list, progress)
        elif isinstance(source, (str, os.PathLike)):
            raw = self._engine.scan_path(str(source), page_list, progress)
        else:
            raise TypeError(
                f"cannot scan {type(source).__name__}; pass a path, bytes, numpy array or PIL image"
            )
        return Document._from_json(json.loads(raw))

    def read(self, source: Any, **kwargs: Any) -> str:
        """Scans `source` and returns just the text."""
        return self.scan(source, **kwargs).text

    def scan_many(self, sources: Iterable[Any]) -> Iterator[Document]:
        """Scans several inputs, using all page workers.

        Paths are handed to the Rust side in one batch so documents are scanned
        in parallel; other inputs fall back to one call each. A directory or a
        glob pattern stands for the readable files inside it, sorted, so
        ``scan_many(["archive/"])`` does what it looks like.
        """
        items = _expand_sources(sources)
        if items and all(isinstance(s, (str, os.PathLike)) for s in items):
            for raw in self._engine.scan_many([str(s) for s in items]):
                payload = json.loads(raw)
                if "error" in payload and "pages" not in payload:
                    raise OcrustError(payload["error"])
                yield Document._from_json(payload)
        else:
            for item in items:
                yield self.scan(item)

    @property
    def languages(self) -> tuple[dict[str, str], ...]:
        """Languages the loaded model covers completely."""
        return tuple(
            {"code": code, "name": name, "script": script}
            for code, name, script in self._engine.languages()
        )

    def partial_languages(self, min_ratio: float = 0.8) -> tuple[dict[str, object], ...]:
        """Languages the model nearly covers, with the characters it is missing."""
        return tuple(
            {"code": code, "name": name, "ratio": ratio, "missing": missing}
            for code, name, ratio, missing in self._engine.partial_languages(min_ratio)
        )

    def optional_language_gaps(self) -> tuple[dict[str, object], ...]:
        """Covered languages the model lacks an optional character for.

        German is the case in point: without ``ẞ`` the model returns ``STRAßE``
        for ``STRAẞE`` — the right letters, one in the wrong case. The language
        is covered, so this is a note rather than a refusal.
        """
        return tuple(
            {"code": code, "name": name, "missing": missing}
            for code, name, missing in self._engine.optional_language_gaps()
        )

    @property
    def charset_size(self) -> int:
        """Number of characters the recognition model can emit."""
        return self._engine.charset_size()

    def ocr_pdf(
        self,
        source: str | os.PathLike[str] | bytes,
        *,
        dpi: float | None = None,
        skip_pages_with_text: bool = True,
        compress: bool = True,
    ) -> tuple[bytes, dict[str, int]]:
        """Adds an invisible OCR text layer to an existing PDF.

        The pages themselves are untouched — same images, same compression — so
        this is the archival path: the file looks identical and becomes
        searchable. Pages that already contain text are skipped by default.

        Returns the new PDF bytes and a report with the counts.
        """
        data = source if isinstance(source, bytes) else Path(source).read_bytes()
        engine = self._pdf_engine()
        try:
            pdf, pages, with_layer, skipped, lines, unmappable = engine.pdf_text_layer(
                data, dpi, skip_pages_with_text, compress
            )
        except OcrustError:
            raise
        except Exception as exc:
            raise OcrustError(str(exc)) from exc
        return pdf, {
            "pages": pages,
            "pages_with_layer": with_layer,
            "pages_skipped": skipped,
            "lines": lines,
            "unmappable_chars": unmappable,
        }

    def plan_pdf(
        self,
        source: str | os.PathLike[str] | bytes,
        *,
        skip_pages_with_text: bool = True,
    ) -> tuple[dict[str, object], ...]:
        """Reports what :meth:`ocr_pdf` would do, without running OCR."""
        data = source if isinstance(source, bytes) else Path(source).read_bytes()
        return tuple(
            {
                "index": index,
                "width": width,
                "height": height,
                "rotate": rotate,
                "needs_ocr": needs_ocr,
            }
            for index, width, height, rotate, needs_ocr in self._engine.plan_pdf_text_layer(
                data, skip_pages_with_text
            )
        )

    def to_tiff(
        self,
        source: str | os.PathLike[str],
        *,
        gray: bool = False,
        compression: str = "lzw",
    ) -> tuple[bytes, Document]:
        """Scans `source` and returns it as one multi-page TIFF plus the result.

        The pages written are the preprocessed ones, so the archive copy is
        already deskewed and upright.

        `compression` is ``"lzw"`` (the default, understood by every TIFF reader
        since 1992), ``"deflate"`` (a little tighter, a little slower) or
        ``"none"``. Uncompressed pages are roughly ten times the size: a 40-page
        colour scan is half a gigabyte raw.
        """
        try:
            data, raw = self._engine.to_tiff(str(source), gray, compression)
        except Exception as exc:
            raise OcrustError(str(exc)) from exc
        return data, Document._from_json(json.loads(raw))

    def searchable_pdf(
        self,
        source: str | os.PathLike[str],
        *,
        dpi: float | None = None,
        jpeg_quality: int = 80,
    ) -> bytes:
        """Scans `source` and returns a PDF with an invisible text layer.

        The result looks exactly like the input but is searchable and
        selectable — the usual goal when archiving scans.
        """
        try:
            return self._pdf_engine().searchable_pdf(str(source), dpi, jpeg_quality)
        except OcrustError:
            raise
        except Exception as exc:
            raise OcrustError(str(exc)) from exc

    def searchable_pdf_many(
        self,
        sources: Iterable[str | os.PathLike[str]],
        *,
        dpi: float | None = None,
        jpeg_quality: int = 80,
    ) -> tuple[bytes, int]:
        """Converts every source, in order, into the pages of one PDF.

        Anything :data:`READABLE_SUFFIXES` covers can go in — images in any of
        the formats the reader offers, multi-page TIFFs, PDFs — and they come out
        as one searchable document, each page sized from its own pixels rather
        than forced onto a common sheet. Returns the PDF and its page count.

        Pages are compressed as they are scanned, so a hundred files cost the
        memory of one.
        """
        # A folder or a glob is one input as far as the caller is concerned.
        paths = [str(source) for source in _expand_sources(sources)]
        if not paths:
            raise OcrustError("a PDF needs at least one input")
        try:
            return self._pdf_engine().searchable_pdf_many(paths, dpi, jpeg_quality)
        except OcrustError:
            raise
        except Exception as exc:
            raise OcrustError(str(exc)) from exc

    def _pdf_engine(self) -> Any:
        """An engine that keeps page images, which a text layer needs.

        Reuses this engine when it already keeps them, otherwise builds a
        sibling once, with the very same settings.
        """
        if self._keeps_images:
            return self._engine
        if self._pdf_engine_cache is None:
            kwargs = dict(self._kwargs, keep_page_images=True)
            try:
                self._pdf_engine_cache = _ocrust.Engine(**kwargs)
            except Exception as exc:  # pragma: no cover - environment dependent
                raise OcrustError(str(exc)) from exc
        return self._pdf_engine_cache

    def __repr__(self) -> str:
        return repr(self._engine)


_default_lock = threading.Lock()
_default_engine: Ocr | None = None


def _default() -> Ocr:
    """The lazily created engine behind the module-level helpers."""
    global _default_engine
    with _default_lock:
        if _default_engine is None:
            _default_engine = Ocr()
        return _default_engine


def read(source: Any, **kwargs: Any) -> str:
    """Reads the text of a document with the default engine.

    >>> ocrust.read("receipt.jpg")  # doctest: +SKIP
    'REWE Markt GmbH\\nSumme 12,90'
    """
    return _default().read(source, **kwargs)


def scan(source: Any, **kwargs: Any) -> Document:
    """Scans a document with the default engine and returns the full result."""
    return _default().scan(source, **kwargs)


def scan_many(sources: Iterable[Any]) -> Iterator[Document]:
    """Scans several documents with the default engine."""
    return _default().scan_many(sources)


def searchable_pdf(source: str | os.PathLike[str], **kwargs: Any) -> bytes:
    """Produces a searchable PDF for `source` with the default engine."""
    return _default().searchable_pdf(source, **kwargs)


def searchable_pdf_many(
    sources: Iterable[str | os.PathLike[str]], **kwargs: Any
) -> tuple[bytes, int]:
    """Converts several sources into one searchable PDF with the default engine."""
    return _default().searchable_pdf_many(sources, **kwargs)


def ocr_pdf(source: Any, **kwargs: Any) -> tuple[bytes, dict[str, int]]:
    """Adds an OCR text layer to a PDF using the default engine."""
    return _default().ocr_pdf(source, **kwargs)


def to_tiff(source: Any, **kwargs: Any) -> tuple[bytes, Document]:
    """Converts a document to a multi-page TIFF using the default engine."""
    return _default().to_tiff(source, **kwargs)


def languages() -> tuple[dict[str, str], ...]:
    """Languages the installed model covers completely."""
    return _default().languages


def known_languages() -> tuple[dict[str, str], ...]:
    """Every language ocrust can validate, whether or not a model covers it."""
    return tuple(
        {"code": code, "name": name, "script": script}
        for code, name, script in _ocrust.known_languages()
    )


def install_models() -> dict[str, str | None]:
    """Downloads the bundled model set from GitHub into the model cache.

    Only needed when the ``ocrust-models`` wheel is not installed. Everything is
    fetched from ``raw.githubusercontent.com`` and checksum-verified, so a
    network that allows GitHub and nothing else is enough.
    """
    detection, recognition, orientation, dictionary = _ocrust.install_models()
    return {
        "detection": detection,
        "recognition": recognition,
        "orientation": orientation,
        "dictionary": dictionary,
    }


def models_cache_dir() -> str:
    """Directory model bundles are cached in."""
    return _ocrust.models_cache_dir()


def runtime_info() -> dict[str, object]:
    """Environment diagnostics: runtime, models and versions."""
    report = dict(runtime_report())
    report["ocrust"] = __version__
    try:
        report["onnxruntime_loaded"] = _ocrust.runtime_version()
    except Exception as exc:
        report["onnxruntime_loaded"] = f"unavailable: {exc}"
    try:
        detection, recognition, orientation, dictionary = _ocrust.resolve_models(
            str(default_models_dir()) if default_models_dir() else None
        )
        report["models"] = {
            "detection": detection,
            "recognition": recognition,
            "orientation": orientation,
            "dictionary": dictionary,
        }
    except Exception as exc:
        report["models"] = f"unavailable: {exc}"
    report["models_cache_dir"] = models_cache_dir()
    return report
