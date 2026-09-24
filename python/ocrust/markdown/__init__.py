"""Any document as Markdown, for a knowledge base.

::

    from ocrust import markdown

    note = markdown.convert("Angebot.docx")
    print(note.markdown)

    markdown.export(["archive/"], "wissen/")      # a folder, kept in sync

One note per document, in CommonMark with pipe tables, under flat YAML front
matter: what Obsidian shows as properties, what a retrieval pipeline reads as
metadata, and what every Markdown tool opens. Scans and PDFs are read with the
OCR engine — a PDF's own text where it has one — and Word, PowerPoint, Excel,
OpenDocument, e-books, web pages, mails, RTF, CSV, text and source code are
read directly, without Office and without any other package.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any

from . import _formats, _html, _mail, _odf, _office, _rtf, _text
from ._context import Context, Options
from ._ir import Block, Heading, Note, nfc, plain
from ._package import ConversionError
from ._render import front_matter, render
from ._scan import blocks_of, extraction

if TYPE_CHECKING:
    from ocrust import Ocr

__all__ = [
    "SUFFIXES",
    "ConversionError",
    "Converted",
    "Options",
    "convert",
    "export",
    "supported",
]

#: Bumped whenever the same input would be written differently, so that a
#: store converts its documents again.
FORMAT_VERSION = 1

_Reader = Callable[[bytes, Context], Note]

_SCANNED = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".jpe",
    ".jfif",
    ".tif",
    ".tiff",
    ".bmp",
    ".gif",
    ".webp",
    ".pbm",
    ".pgm",
    ".ppm",
    ".pnm",
    ".tga",
    ".qoi",
}
_PAGED = {".pdf", ".tif", ".tiff"}

_READERS: dict[str, _Reader] = {}
for _suffix in (".docx", ".docm", ".dotx", ".dotm"):
    _READERS[_suffix] = _office.docx
for _suffix in (".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"):
    _READERS[_suffix] = _office.pptx
for _suffix in (".xlsx", ".xlsm", ".xltx", ".xltm"):
    _READERS[_suffix] = _office.xlsx
for _suffix in (".odt", ".ott"):
    _READERS[_suffix] = _odf.odt
for _suffix in (".ods", ".ots"):
    _READERS[_suffix] = _odf.ods
for _suffix in (".odp", ".otp"):
    _READERS[_suffix] = _odf.odp
for _suffix in (".doc", ".dot", ".xls", ".xlt", ".ppt", ".pps", ".pot"):
    _READERS[_suffix] = lambda data, ctx, s=_suffix: _formats.office_binary(data, ctx, s)
_READERS[".epub"] = _formats.epub
_READERS[".zip"] = _formats.zip_archive
for _suffix in (".eml",):
    _READERS[_suffix] = _mail.eml
for _suffix in (".mht", ".mhtml"):
    _READERS[_suffix] = _mail.mht
_READERS[".rtf"] = _rtf.rtf
for _suffix in (".txt", ".text", ".asc"):
    _READERS[_suffix] = _text.txt
for _suffix in (".md", ".markdown", ".mdown", ".mkd", ".mdx"):
    _READERS[_suffix] = _text.md
_READERS[".csv"] = _text.delimited
for _suffix in (".tsv", ".tab"):
    _READERS[_suffix] = lambda data, ctx: _text.delimited(data, ctx, "\t")
for _suffix in (".srt", ".vtt"):
    _READERS[_suffix] = _text.subtitles
for _suffix in _text.CODE_LANGUAGES:
    _READERS.setdefault(_suffix, lambda data, ctx, s=_suffix: _text.code(data, ctx, s))

#: Every file suffix a document can be converted from.
SUFFIXES = frozenset({*_READERS, *_SCANNED, ".html", ".htm", ".xhtml", ".shtml"})


def supported(path: str | os.PathLike[str]) -> bool:
    """Whether `path` is a kind of file this converts."""
    return _suffix(str(path)) in SUFFIXES


def _suffix(name: str) -> str:
    base = PurePath(name.replace("\\", "/")).name.lower()
    if base in ("dockerfile", "makefile"):
        return f".{base}"
    return PurePath(base).suffix


@dataclass
class Converted:
    """One document as Markdown.

    Attributes:
        markdown: The note: front matter, then the text.
        meta: The front matter's properties.
        assets: Pictures kept beside the note, by file name, when
            :attr:`Options.assets` is on.
        note: The blocks the Markdown was written from.
    """

    markdown: str
    meta: dict[str, Any]
    assets: dict[str, bytes] = field(default_factory=dict)
    note: Note = field(default_factory=Note)

    @property
    def body(self) -> str:
        """The Markdown without its front matter."""
        if self.note.raw_body is not None:
            return nfc(self.note.raw_body).strip("\n") + "\n"
        return render(self.note, {})


def _scan(data: bytes, ctx: Context, suffix: str, name: str) -> Note:
    if not ctx.options.ocr:
        if suffix != ".pdf":
            return Note(meta={"extraction": "none"})
        # A PDF's own text, and nothing recognized: no models needed, and a
        # scanned page stays empty.
        import json

        from ocrust import Document, _ocrust

        raw = _ocrust.read_pdf_text(data, name, ctx.options.password)
        doc = Document._from_json(json.loads(raw))
        # Pages without text of their own are scans, or pictures of text: the
        # note says which, so they can be recognized later.
        unread = [page.index + 1 for page in doc.pages if not page.lines]
        return Note(
            blocks=blocks_of(doc, paged=True),
            meta={"pages": len(doc.pages), "extraction": "text", "unread_pages": unread},
        )
    doc = ctx.scan(data, name)
    paged = suffix in _PAGED and (suffix == ".pdf" or len(doc.pages) > 1)
    meta: dict[str, Any] = {"pages": len(doc.pages) if suffix in _PAGED else None}
    how = extraction(doc)
    meta["extraction"] = how
    quality = getattr(doc, "quality", None)
    if how != "text" and isinstance(quality, (int, float)):
        meta["ocr_quality"] = round(float(quality), 2)
    return Note(blocks=blocks_of(doc, paged=paged), meta=meta)


def read(data: bytes, name: str, ctx: Context) -> Note | None:
    """The note of one file's bytes, or None for a kind of file this does not
    convert. `name` decides the kind, by its suffix."""
    suffix = _suffix(name)
    ctx.depth += 1
    try:
        if ctx.depth > 5:
            raise ConversionError("documents nested more than five deep")
        if suffix in _SCANNED:
            return _scan(data, ctx, suffix, name)
        if suffix in (".html", ".htm", ".xhtml", ".shtml"):
            return _html.html(data, ctx)
        reader = _READERS.get(suffix)
        if reader is None:
            return None
        return reader(data, ctx)
    except (ConversionError, OSError, MemoryError):
        raise
    except RecursionError as exc:
        raise ConversionError("nested too deeply to be read") from exc
    except Exception as exc:
        if type(exc).__name__ == "OcrustError":
            raise
        # A file that breaks a reader in a way nobody foresaw is a file that
        # cannot be converted — said as such, not as a traceback.
        raise ConversionError(f"cannot be read ({type(exc).__name__}: {exc})") from exc
    finally:
        ctx.depth -= 1


def _nested(data: bytes, name: str, ctx: Context) -> Note | None:
    """Converts an attachment or an archive member, with the pictures it
    keeps going to the document that holds it."""
    return read(data, name, ctx)


# -- properties


def _words(text: str) -> list[str]:
    return text.split()


_STOPWORDS = {
    "de": _words(
        "der die und in den von zu das mit sich des auf für ist im dem nicht ein eine als "
        "auch es an werden aus er hat dass sie nach wird bei einer um am sind noch wie einem "
        "über einen so zum war haben nur oder aber vor zur bis mehr durch man sein wurde sei"
    ),
    "en": _words(
        "the of and to in is that for it as was with be by on not he this are or his from at "
        "which but have an they you were her all she there would their we him been has when "
        "who will more no if out so said what up its about into than them can only other"
    ),
    "fr": _words(
        "de la le et les des en un du une que est pour qui dans par plus pas au sur ne se ce "
        "il sont avec ou son aux mais comme été être elle ses leur nous vous"
    ),
    "es": _words(
        "de la que el en y los del se las por un para con no una su al es lo como más pero "
        "sus le ya o fue este ha sí porque esta son entre cuando muy sin sobre"
    ),
    "it": _words(
        "di e il la che in a per un del è non una da sono le si con i dei al come della nel "
        "più anche ma ha questo alla gli ci delle se"
    ),
    "nl": _words(
        "de van het een en in is dat op te zijn voor met die niet aan er om ook als bij door "
        "maar uit dan wordt nog kan tot naar over hij"
    ),
    "pt": _words(
        "de a o que e do da em um para é com não uma os no se na por mais as dos como mas foi "
        "ao ele das tem à seu sua ou ser quando muito nos"
    ),
}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def language(text: str) -> str | None:
    """The language of a text, from the short words every text of it uses.

    Only said when the text is long enough and one language clearly leads;
    a wrong language in a knowledge base's filter is worse than none.
    """
    words = [w.lower() for w in _WORD.findall(text[:200_000])]
    if len(words) < 40:
        return None
    sets = {code: set(ws) for code, ws in _STOPWORDS.items()}
    scores = {code: sum(1 for w in words if w in vocab) for code, vocab in sets.items()}
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top < 0.08 * len(words) or top < 1.5 * second:
        return None
    return best


def _title(note: Note, name: str) -> str:
    title = note.meta.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    # A spreadsheet's headings are its sheets' names, not its title.
    tabular = _suffix(name) in (
        ".xlsx",
        ".xlsm",
        ".xltx",
        ".xltm",
        ".xls",
        ".ods",
        ".ots",
        ".csv",
        ".tsv",
        ".tab",
    )
    for block in [] if tabular else note.blocks[:20]:
        if isinstance(block, Heading) and block.level <= 2:
            text = plain(block.content)
            if text:
                return text[:200]
    stem = PurePath(name.replace("\\", "/")).name
    suffix = _suffix(stem)
    stem = stem[: -len(suffix)] if suffix and stem.lower().endswith(suffix) else stem
    return stem or name


_ORDER = (
    "title",
    "from",
    "to",
    "cc",
    "date",
    "author",
    "subject",
    "description",
    "keywords",
    "created",
    "modified",
    "language",
    "url",
    "source",
    "source_type",
    "source_modified",
    "sha256",
    "pages",
    "slides",
    "sheets",
    "chapters",
    "rows",
    "attachments",
    "extraction",
    "ocr_quality",
    "unread_pages",
    "truncated",
    "message_id",
    "publisher",
)


def properties(note: Note, name: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The front matter of a note: what the document says about itself, then
    where it came from, in a fixed order."""
    meta = {key: value for key, value in note.meta.items() if value not in (None, "", [])}
    meta["title"] = _title(note, name)
    if not meta.get("language"):
        text = note.raw_body if note.raw_body is not None else plain(note.blocks)
        found = language(text)
        if found:
            meta["language"] = found
    elif isinstance(meta["language"], str):
        meta["language"] = meta["language"].split("-")[0].lower()
    meta["source_type"] = _suffix(name).lstrip(".") or None
    if extra:
        meta.update({key: value for key, value in extra.items() if value is not None})
    ordered = {key: meta[key] for key in _ORDER if key in meta}
    ordered.update({key: value for key, value in meta.items() if key not in ordered})
    return ordered


def compose(note: Note, meta: dict[str, Any]) -> str:
    """The note's text. A Markdown source keeps its own body and front
    matter; the properties only add the keys it does not have."""
    if note.raw_body is None:
        return render(note, meta)
    if note.raw_front is not None:
        existing = {
            match.group(1) for match in re.finditer(r"^([A-Za-z_][\w-]*)\s*:", note.raw_front, re.M)
        }
        added = front_matter({k: v for k, v in meta.items() if k not in existing})
        added_lines = added.split("\n")[1:-1] if added else []
        head = "\n".join(["---", note.raw_front.rstrip("\n"), *added_lines, "---"])
    else:
        head = front_matter(meta)
    text = f"{head}\n\n{note.raw_body}" if head else note.raw_body
    from ._ir import nfc

    return nfc(text).rstrip("\n") + "\n"


def convert(
    source: str | os.PathLike[str] | bytes,
    *,
    name: str | None = None,
    options: Options | None = None,
    engine: Ocr | None = None,
    **settings: Any,
) -> Converted:
    """Converts one document to Markdown.

    Args:
        source: A path, or the document's bytes (then `name` says what kind
            of file it is, by its suffix).
        name: The file name to use for the bytes, and for the title when the
            document has none.
        options: How to convert; or pass its fields as keyword arguments
            (``pdf_text="always"``, ``assets=True``, …).
        engine: An :class:`ocrust.Ocr` to recognize scans and pictures with.
            One is made on first use otherwise, with ``pdf_text`` as set.

    Raises:
        ConversionError: The file is broken, or of a kind this does not read.
        OSError: The file cannot be read.
    """
    if options is None:
        options = Options(**settings)
    elif settings:
        raise TypeError("pass options= or keyword settings, not both")
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        label = name or "document"
        extra: dict[str, Any] = {}
    else:
        path = Path(source)
        data = path.read_bytes()
        label = name or path.name
        info = path.stat()
        extra = {
            "source": str(path),
            "source_modified": _dt.datetime.fromtimestamp(info.st_mtime, _dt.timezone.utc).replace(
                microsecond=0
            ),
        }
    if not supported(label):
        raise ConversionError(f"{label}: not a kind of file this converts")
    ctx = Context(options, engine, _nested)
    if not isinstance(source, (bytes, bytearray, memoryview)):
        ctx.path = Path(source)
    note = read(data, label, ctx)
    if note is None:  # pragma: no cover - supported() said otherwise
        raise ConversionError(f"{label}: not a kind of file this converts")
    note.assets.update(ctx.assets)
    extra["sha256"] = hashlib.sha256(data).hexdigest()
    meta = properties(note, label, extra)
    return Converted(markdown=compose(note, meta), meta=meta, assets=dict(note.assets), note=note)


def export(*args: Any, **kwargs: Any) -> Any:
    """Converts documents into a folder of notes and keeps it in sync; see
    :func:`ocrust.markdown._store.export`."""
    from ._store import export as _export

    return _export(*args, **kwargs)


export.__doc__ = None
_unused: tuple[Any, ...] = (Block, blocks_of)
