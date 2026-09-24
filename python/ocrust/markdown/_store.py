"""A folder of notes, kept in step with the documents it was made from.

::

    wissen/
      .ocrust/index.json          what was converted from what, and when
      berichte/2026/q1.pdf.md     one note per document, folders mirrored
      angebote/kunde.docx.md
      _assets/angebote/kunde.docx/image1.png   pictures, when kept

A note is named after its document, extension and all — `q1.pdf.md` and
`q1.docx.md` are two notes, not one overwriting the other — and it sits where
the document sits. Running the export again converts only what changed: a
document with the same size and time is not even read, one that was touched
but not changed is recognized by its hash. A note someone edited by hand is
never overwritten, and nothing the export did not write itself is deleted.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import posixpath
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ocrust import OcrustError

from . import FORMAT_VERSION, _formats, _nested, _suffix, compose, properties, read, supported
from ._context import Context, Options
from ._ir import Block, Footnote, Image, ListBlock, Note, Quote
from ._package import ConversionError

if TYPE_CHECKING:
    from ocrust import Ocr

INDEX_DIR = ".ocrust"
INDEX_NAME = "index.json"
ASSETS_DIR = "_assets"
_INDEX_FORMAT = 1
#: How often, at most, the index is saved while a long run goes on.
_SAVE_EVERY = 30.0


@dataclass
class ExportResult:
    """What an export did, file by file."""

    #: (document, note) for every note written.
    written: list[tuple[Path, Path]] = field(default_factory=list)
    #: Documents whose notes were already up to date.
    unchanged: list[Path] = field(default_factory=list)
    #: (note, reason) for notes left alone: edited by hand, or not written
    #: by an export.
    kept: list[tuple[Path, str]] = field(default_factory=list)
    #: (document, reason) for documents that could not be converted.
    failed: list[tuple[Path, str]] = field(default_factory=list)
    #: Notes deleted because their document is gone (with ``prune``).
    pruned: list[Path] = field(default_factory=list)
    #: Files in the folders that are not a kind of document this converts.
    skipped: list[Path] = field(default_factory=list)
    #: Things worth knowing that failed nothing: pictures left unread for
    #: want of the OCR models, say. Each once.
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


@dataclass
class _Source:
    path: Path
    #: Where the note goes, relative to the output folder, without `.md`.
    stem: str
    #: The document's path as the front matter names it.
    label: str
    #: The folder walked to find it, or its own folder.
    root: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str | None:
    try:
        return _sha256(path)
    except OSError:
        return None


def _write(path: Path, data: bytes) -> None:
    """Writes atomically: a reader sees the old file or the new one, and an
    interrupted run leaves no half-written note behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with partial.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        partial.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise


def _hidden(part: str) -> bool:
    return part.startswith(".") or part.startswith("~$") or part in ("__MACOSX", "__pycache__")


def _walk(root: Path, out_dir: Path, skipped: list[Path]) -> Iterator[Path]:
    """Every file below `root`, sorted, leaving out hidden files and folders
    and the output folder itself when it lies inside."""
    try:
        out_resolved = out_dir.resolve()
    except OSError:  # pragma: no cover
        out_resolved = out_dir
    for folder, dirs, files in os.walk(root):
        here = Path(folder)
        with contextlib.suppress(OSError):
            if here.resolve() == out_resolved:
                dirs[:] = []
                continue
        dirs[:] = sorted(d for d in dirs if not _hidden(d))
        for name in sorted(files):
            if _hidden(name):
                continue
            path = here / name
            if supported(name):
                yield path
            else:
                skipped.append(path)


def _sources(
    inputs: Sequence[Path], out_dir: Path, skipped: list[Path]
) -> tuple[list[_Source], list[Path]]:
    """The documents to convert and where each one's note goes.

    One folder is mirrored into the output as it is; several folders each get
    a folder of their own name. A file named on its own goes to the top.
    """
    folders = [p for p in inputs if p.is_dir()]
    several = len(folders) > 1
    sources: list[_Source] = []
    roots: list[Path] = []
    for item in inputs:
        if item.is_dir():
            root = item.resolve()
            roots.append(root)
            prefix = root.name if several else ""
            for path in _walk(item, out_dir, skipped):
                relative = path.relative_to(item).as_posix()
                stem = posixpath.join(prefix, relative) if prefix else relative
                label = posixpath.join(root.name, relative)
                sources.append(_Source(path, stem, label, root))
        elif item.is_file():
            if not supported(item.name):
                skipped.append(item)
                continue
            sources.append(_Source(item, item.name, item.name, item.resolve().parent))
            roots.append(item.resolve())
    return sources, roots


def _images(blocks: Iterable[Block]) -> Iterator[Image]:
    for block in blocks:
        if isinstance(block, Image):
            yield block
            yield from _images(block.blocks)
        elif isinstance(block, (Quote, Footnote)):
            yield from _images(block.blocks)
        elif isinstance(block, ListBlock):
            for item in block.items:
                yield from _images(item)


def _options_key(options: Options) -> str:
    return json.dumps(
        {
            "format": FORMAT_VERSION,
            "pdf_text": options.pdf_text,
            "ocr": options.ocr,
            "pictures": options.pictures,
            "assets": options.assets,
            "max_rows": options.max_rows,
        },
        sort_keys=True,
    )


class _Lock:
    """One export at a time per output folder."""

    def __init__(self, folder: Path) -> None:
        self.path = folder / "lock"

    def __enter__(self) -> _Lock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if self._stale():
                    with contextlib.suppress(OSError):
                        self.path.unlink()
                    continue
                raise ConversionError(
                    f"another export is writing to {self.path.parent.parent} "
                    f"(remove {self.path} if none is)"
                ) from None
            with os.fdopen(handle, "w") as out:
                out.write(str(os.getpid()))
            return self
        raise ConversionError(f"cannot lock {self.path}")  # pragma: no cover

    def _stale(self) -> bool:
        try:
            pid = int(self.path.read_text().strip() or 0)
        except (OSError, ValueError):
            return True
        if pid <= 0 or pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return False
        return False

    def __exit__(self, *_: Any) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()


class _Index:
    def __init__(self, out_dir: Path) -> None:
        self.path = out_dir / INDEX_DIR / INDEX_NAME
        self.entries: dict[str, dict[str, Any]] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict) and data.get("format") == _INDEX_FORMAT:
            entries = data.get("documents")
            if isinstance(entries, dict):
                self.entries = {k: v for k, v in entries.items() if isinstance(v, dict)}
        self.saved = time.monotonic()
        self.owners: dict[str, str] = {}
        for key, entry in self.entries.items():
            self._own(key, entry)

    def _own(self, key: str, entry: dict[str, Any]) -> None:
        for item in entry.get("notes", []):
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                self.owners[item["path"]] = key

    def put(self, key: str, entry: dict[str, Any]) -> None:
        old = self.entries.get(key)
        if old is not None:
            for item in old.get("notes", []):
                if self.owners.get(item.get("path")) == key:
                    del self.owners[item["path"]]
        self.entries[key] = entry
        self._own(key, entry)

    def drop(self, key: str) -> None:
        self.put(key, {})
        del self.entries[key]

    def owner_of(self, note: str) -> tuple[str, dict[str, Any]] | None:
        key = self.owners.get(note)
        if key is None or key not in self.entries:
            return None
        for item in self.entries[key].get("notes", []):
            if item.get("path") == note:
                return key, item
        return None

    def save(self) -> None:
        payload = {
            "format": _INDEX_FORMAT,
            "documents": dict(sorted(self.entries.items())),
        }
        _write(
            self.path, (json.dumps(payload, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
        )
        self.saved = time.monotonic()


def _relative_link(note: str, asset: str) -> str:
    return posixpath.relpath(asset, posixpath.dirname(note) or ".")


class _Exporter:
    def __init__(
        self,
        out_dir: Path,
        options: Options,
        engine: Ocr | Callable[[], Ocr] | None,
        force: bool,
        dry_run: bool,
        progress: Callable[[str, Path, Path | None, str], None] | None,
    ) -> None:
        self.out_dir = out_dir
        self.options = options
        self.engine = engine
        self.force = force
        self.dry_run = dry_run
        self.progress = progress or (lambda *_: None)
        self.index = _Index(out_dir)
        self.result = ExportResult()
        self.options_key = _options_key(options)
        self._built: Ocr | None = None

    def _engine(self) -> Ocr:
        """One engine for the whole run, made when the first scan needs it."""
        if self._built is None:
            source = self.engine
            if source is not None and hasattr(source, "scan"):
                self._built = source  # type: ignore[assignment]
            elif source is not None:
                self._built = source()  # type: ignore[operator]
            else:
                from ocrust import Ocr

                self._built = Ocr(pdf_text=self.options.pdf_text, password=self.options.password)
        return self._built

    # -- one document

    def up_to_date(self, source: _Source, key: str, stat: os.stat_result) -> bool:
        entry = self.index.entries.get(key)
        if self.force or entry is None or entry.get("error"):
            return False
        if entry.get("options") != self.options_key or entry.get("stem") != source.stem:
            return False
        if not all((self.out_dir / item["path"]).is_file() for item in entry.get("notes", [])):
            return False
        if entry.get("size") == stat.st_size and entry.get("mtime_ns") == stat.st_mtime_ns:
            return True
        if entry.get("size") != stat.st_size:
            return False
        if _file_sha256(source.path) == entry.get("sha256"):
            # Touched, not changed: remember the new time and read nothing.
            entry["mtime_ns"] = stat.st_mtime_ns
            return True
        return False

    def notes_of(
        self, source: _Source, data: bytes, stat: os.stat_result
    ) -> list[tuple[str, str, dict[str, bytes]]]:
        """(note path, text, assets) for every note a document gives: one,
        or one per member of an archive."""
        modified = _dt.datetime.fromtimestamp(stat.st_mtime, _dt.timezone.utc).replace(
            microsecond=0
        )
        digest = hashlib.sha256(data).hexdigest()
        if _suffix(source.path.name) == ".zip":
            out = []
            for member, content in _formats.archive_members(data):
                if not supported(member):
                    continue
                label = f"{source.label}/{member}"
                stem = f"{source.stem}/{member}"
                try:
                    converted = self._one(content, member, label, stem, modified, None, None)
                except (ConversionError, ValueError, OSError) as exc:
                    self.result.failed.append((source.path, f"{member}: {exc}"))
                    continue
                if converted is not None:
                    out.append(converted)
            return out
        converted = self._one(
            data, source.path.name, source.label, source.stem, modified, digest, source.path
        )
        return [converted] if converted is not None else []

    def _one(
        self,
        data: bytes,
        name: str,
        label: str,
        stem: str,
        modified: _dt.datetime,
        digest: str | None,
        path: Path | None,
    ) -> tuple[str, str, dict[str, bytes]] | None:
        ctx = Context(self.options, self._engine, _nested)
        ctx.path = path
        try:
            note = read(data, name, ctx)
        finally:
            for warning in ctx.warnings:
                if warning not in self.result.warnings:
                    self.result.warnings.append(warning)
        if note is None:
            return None
        note.assets.update(ctx.assets)
        note_path = f"{stem}.md"
        assets = self._place_assets(note, note_path)
        meta = properties(
            note,
            name,
            {
                "source": label,
                "source_modified": modified,
                "sha256": digest or hashlib.sha256(data).hexdigest(),
            },
        )
        return note_path, compose(note, meta), assets

    def _place_assets(self, note: Note, note_path: str) -> dict[str, bytes]:
        """Moves a note's pictures to `_assets/<note>/` and points its image
        links there."""
        if not note.assets:
            return {}
        folder = posixpath.join(ASSETS_DIR, note_path[: -len(".md")])
        placed: dict[str, bytes] = {}
        for name, content in note.assets.items():
            placed[posixpath.join(folder, name)] = content
        for image in _images(note.blocks):
            if image.target and image.target in note.assets:
                image.target = _relative_link(note_path, posixpath.join(folder, image.target))
        return placed

    def convert(self, source: _Source) -> None:
        key = str(source.path.resolve())
        try:
            stat = source.path.stat()
        except OSError as exc:
            self.result.failed.append((source.path, exc.strerror or str(exc)))
            return
        if self.up_to_date(source, key, stat):
            self.result.unchanged.append(source.path)
            self.progress("unchanged", source.path, None, "")
            return
        if self.dry_run:
            self.result.written.append((source.path, self.out_dir / f"{source.stem}.md"))
            self.progress("would write", source.path, self.out_dir / f"{source.stem}.md", "")
            return
        try:
            data = source.path.read_bytes()
            notes = self.notes_of(source, data, stat)
        except (ConversionError, OcrustError, ValueError, OSError, UnicodeError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            self.result.failed.append((source.path, reason))
            self.progress("failed", source.path, None, reason)
            previous = self.index.entries.get(key)
            if previous is not None:
                previous["error"] = reason
            return
        except Exception as exc:  # noqa: BLE001 - one document must not end the run
            reason = f"{type(exc).__name__}: {exc}"
            self.result.failed.append((source.path, reason))
            self.progress("failed", source.path, None, reason)
            return
        previous = self.index.entries.get(key, {})
        old_notes = {item["path"]: item for item in previous.get("notes", [])}
        old_assets = set(previous.get("assets", []))
        entry_notes: list[dict[str, str]] = []
        entry_assets: list[str] = []
        kept = False
        for note_path, text, assets in notes:
            target = self.out_dir / note_path
            if not self._may_write(target, note_path, key):
                # Left alone: still ours if it was, never ours if it was not,
                # and looked at again next time.
                kept = True
                if note_path in old_notes:
                    entry_notes.append(old_notes[note_path])
                continue
            payload = text.encode("utf-8")
            _write(target, payload)
            for asset_path, content in assets.items():
                _write(self.out_dir / asset_path, content)
                entry_assets.append(asset_path)
            entry_notes.append({"path": note_path, "sha256": hashlib.sha256(payload).hexdigest()})
            self.result.written.append((source.path, target))
            self.progress("written", source.path, target, "")
        written = {item["path"] for item in entry_notes}
        # Notes and pictures the document no longer gives: an archive member
        # removed, a picture replaced.
        for stale, item in old_notes.items():
            if stale not in written:
                self._remove_note(stale, item.get("sha256", ""))
        for stale in old_assets - set(entry_assets):
            with contextlib.suppress(OSError):
                (self.out_dir / stale).unlink()
            self._remove_empty((self.out_dir / stale).parent)
        self.index.put(
            key,
            {
                "source": source.label,
                "stem": source.stem,
                "root": str(source.root),
                # A note that was kept makes the next run try again.
                "size": -1 if kept else stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": hashlib.sha256(data).hexdigest(),
                "options": self.options_key,
                "converted": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
                "notes": entry_notes,
                "assets": sorted(entry_assets),
            },
        )
        if time.monotonic() - self.index.saved > _SAVE_EVERY:
            self.index.save()

    def _may_write(self, target: Path, note_path: str, key: str) -> bool:
        """Whether a note may be (over)written: it is not there, or it is
        exactly what an export wrote last time."""
        if self.force or not target.exists():
            return True
        owner = self.index.owner_of(note_path)
        if owner is None:
            self.result.kept.append((target, "not written by an export; --force replaces it"))
            self.progress("kept", target, None, "not written by an export")
            return False
        owner_key, item = owner
        if owner_key != key:
            self.result.kept.append(
                (target, f"belongs to {self.index.entries[owner_key].get('source')}")
            )
            self.progress("kept", target, None, "another document's note")
            return False
        if _file_sha256(target) != item.get("sha256"):
            self.result.kept.append((target, "edited since it was written; --force replaces it"))
            self.progress("kept", target, None, "edited by hand")
            return False
        return True

    def _remove_note(self, note_path: str, digest: str) -> bool:
        target = self.out_dir / note_path
        if target.exists() and not self.force and _file_sha256(target) != digest:
            self.result.kept.append((target, "edited since it was written; not deleted"))
            return False
        with contextlib.suppress(OSError):
            target.unlink()
        self._remove_empty(target.parent)
        return True

    def _remove_empty(self, folder: Path) -> None:
        top = self.out_dir.resolve()
        with contextlib.suppress(OSError):
            while folder.resolve() != top and top in folder.resolve().parents:
                folder.rmdir()
                folder = folder.parent

    def prune(self, roots: Sequence[Path], seen: set[str]) -> None:
        """Deletes the notes of documents that are gone from the folders this
        run looked at. Documents of other folders are none of its business."""
        for key in list(self.index.entries):
            if key in seen:
                continue
            path = Path(key)
            inside = any(path == root or root in path.parents for root in roots)
            if not inside or (
                path.exists() and supported(path.name) and not _in_hidden(path, roots)
            ):
                continue
            entry = self.index.entries[key]
            if self.dry_run:
                for item in entry.get("notes", []):
                    self.result.pruned.append(self.out_dir / item["path"])
                    self.progress("would remove", path, self.out_dir / item["path"], "")
                continue
            removed_all = True
            for item in entry.get("notes", []):
                if self._remove_note(item["path"], item.get("sha256", "")):
                    self.result.pruned.append(self.out_dir / item["path"])
                    self.progress("pruned", path, self.out_dir / item["path"], "")
                else:
                    removed_all = False
            for asset in entry.get("assets", []):
                with contextlib.suppress(OSError):
                    (self.out_dir / asset).unlink()
                self._remove_empty((self.out_dir / asset).parent)
            if removed_all:
                self.index.drop(key)


def _in_hidden(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        if root in path.parents:
            return any(_hidden(part) for part in path.relative_to(root).parts)
    return False


def export(
    sources: Iterable[str | os.PathLike[str]],
    out_dir: str | os.PathLike[str],
    *,
    options: Options | None = None,
    engine: Ocr | Callable[[], Ocr] | None = None,
    prune: bool = False,
    force: bool = False,
    dry_run: bool = False,
    progress: Callable[[str, Path, Path | None, str], None] | None = None,
    **settings: Any,
) -> ExportResult:
    """Converts documents into a folder of notes, and keeps it in sync.

    Args:
        sources: Files and folders; folders are read recursively.
        out_dir: Where the notes go. Created if needed.
        options: How to convert (or pass its fields as keywords).
        engine: The :class:`ocrust.Ocr` for scans and pictures, or a function
            that makes one; made on first use otherwise.
        prune: Delete the notes of documents that are gone from the folders
            given. Notes edited by hand stay.
        force: Convert everything again, and overwrite notes edited by hand.
        dry_run: Report what would be done, and do nothing.
        progress: Called as ``progress(event, document, note, detail)`` for
            every document: ``written``, ``unchanged``, ``failed``, ``kept``,
            ``pruned``, ``would write`` or ``would remove``.
    """
    if options is None:
        options = Options(**settings)
    elif settings:
        raise TypeError("pass options= or keyword settings, not both")
    out = Path(out_dir)
    inputs = [Path(s) for s in sources]
    for item in inputs:
        if not item.exists():
            raise FileNotFoundError(f"{item}: no such file or folder")
    exporter = _Exporter(out, options, engine, force, dry_run, progress)
    documents, roots = _sources(inputs, out, exporter.result.skipped)
    notes: dict[str, Path] = {}
    for document in documents:
        claimed = notes.setdefault(document.stem, document.path)
        if claimed != document.path:
            exporter.result.failed.append(
                (document.path, f"its note {document.stem}.md is also {claimed}'s; rename one")
            )
    clashing = {d.path for d in documents if notes.get(d.stem) != d.path}
    if dry_run:
        for document in documents:
            if document.path not in clashing:
                exporter.convert(document)
        if prune:
            exporter.prune(roots, {str(d.path.resolve()) for d in documents})
        return exporter.result
    out.mkdir(parents=True, exist_ok=True)
    with _Lock(out / INDEX_DIR):
        try:
            for document in documents:
                if document.path not in clashing:
                    exporter.convert(document)
            if prune:
                exporter.prune(roots, {str(d.path.resolve()) for d in documents})
        finally:
            exporter.index.save()
    return exporter.result
