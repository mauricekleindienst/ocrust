"""What a conversion needs besides the file: settings, and the OCR engine.

The engine is made on first use. Converting a folder of Word files never loads
a model, and a machine without the model files installed can still convert
everything that has its text in it; only scans and pictures need them.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._ir import Block, Image

if TYPE_CHECKING:
    from ocrust import Document, Ocr

#: Cells of all a document's sheets written out at most. A table of a million
#: cells is already more than anyone reads; a file of a few kilobytes can
#: claim a trillion by repeating one cell.
CELL_BUDGET = 1_000_000

#: Pictures smaller than this are icons, bullets and rules, not text.
_MIN_PICTURE_BYTES = 2048

_PICTURE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".jpe",
    ".jfif",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
}


@dataclass
class Options:
    """How documents are converted.

    Attributes:
        pdf_text: How a PDF page's own text is used: ``"auto"`` (default)
            reads it where it can be trusted and recognizes the rest,
            ``"always"`` reads any text layer, ``"never"`` recognizes every
            page.
        ocr: Whether scans and pictures are recognized at all. Off, a PDF is
            read from its own text alone — a scanned page stays empty — images
            and pictures are left unread, and nothing needs the model files.
        pictures: Whether the text in pictures inside documents — a diagram
            in a Word file, a screenshot on a slide — is read too.
        assets: Whether pictures inside documents are kept as files beside
            the notes and linked from them.
        max_rows: Rows of a spreadsheet or CSV table written out; the rest is
            counted in a note. ``None`` writes every row.
    """

    pdf_text: str = "auto"
    ocr: bool = True
    pictures: bool = True
    assets: bool = False
    max_rows: int | None = 5000
    password: str | None = None


class Context:
    """One conversion's settings, engine, and the pictures it keeps."""

    def __init__(
        self,
        options: Options | None = None,
        engine: Ocr | Callable[[], Ocr] | None = None,
        convert: Callable[[bytes, str, Context], Any] | None = None,
    ) -> None:
        self.options = options or Options()
        self._engine = engine
        self._built: Ocr | None = None
        self.assets: dict[str, bytes] = {}
        self._asset_by_hash: dict[str, str] = {}
        #: Converts an embedded file — an attachment, a zip member — by name.
        self.convert = convert
        self.depth = 0
        #: The file on disk being converted, when it is one: what a web page's
        #: pictures are found relative to. Not set for attachments or members.
        self.path: Path | None = None
        #: Why pictures went unread, once each: a missing model is worth
        #: saying, not worth failing a Word file over.
        self.warnings: list[str] = []
        #: Sheet cells still to be written, over the whole document.
        self.cells_left = CELL_BUDGET

    def engine(self) -> Ocr:
        if self._built is None:
            source = self._engine
            if source is not None and hasattr(source, "scan"):
                self._built = source  # type: ignore[assignment]
            elif source is not None:
                self._built = source()  # type: ignore[operator]
            else:
                from ocrust import Ocr

                self._built = Ocr(pdf_text=self.options.pdf_text, password=self.options.password)
        return self._built

    def scan(self, data: bytes, name: str) -> Document:
        return self.engine().scan(data, name=name)

    def keep(self, data: bytes, name: str) -> str:
        """Keeps a picture as an asset; returns the name it is kept under.

        The same picture twice — a logo on every slide — is kept once.
        """
        digest = hashlib.sha256(data).hexdigest()
        if digest in self._asset_by_hash:
            return self._asset_by_hash[digest]
        base = posixpath.basename(name) or "image"
        base = re.sub(r"[^\w.-]+", "_", base).strip("._") or "image"
        stem, dot, suffix = base.rpartition(".")
        if not dot:
            stem, suffix = base, "bin"
        candidate = f"{stem}.{suffix}"
        counter = 1
        # `Logo.png` and `logo.png` are one file on Windows and macOS.
        taken = {name.casefold() for name in self.assets}
        while candidate.casefold() in taken:
            counter += 1
            candidate = f"{stem}-{counter}.{suffix}"
        self.assets[candidate] = data
        self._asset_by_hash[digest] = candidate
        return candidate

    def picture(self, data: bytes | None, name: str, alt: str = "") -> Image | None:
        """A picture inside a document: kept, read, or both — or nothing, when
        it is neither kept nor has text in it."""
        if not data:
            return None
        suffix = posixpath.splitext(name.lower())[1]
        target = self.keep(data, name) if self.options.assets else ""
        blocks: list[Block] = []
        if (
            self.options.ocr
            and self.options.pictures
            and suffix in _PICTURE_SUFFIXES
            and len(data) >= _MIN_PICTURE_BYTES
        ):
            from ._scan import blocks_of

            try:
                blocks = blocks_of(self.scan(data, name), paged=False)
            except Exception as exc:  # noqa: BLE001 - a picture that cannot be read has no text
                blocks = []
                if type(exc).__name__ == "OcrustError" and "model" in str(exc):
                    message = f"pictures inside documents are not read: {exc}"
                    if message not in self.warnings:
                        self.warnings.append(message)
        if not target and not blocks and not alt:
            return None
        return Image(alt=alt, target=target, blocks=blocks)
