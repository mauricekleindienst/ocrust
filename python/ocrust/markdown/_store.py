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
JOURNAL_NAME = "journal.jsonl"
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


_TEMPORARY = iter(range(1, 1 << 62))


def _write(path: Path, data: bytes) -> None:
    """Writes atomically: a reader sees the old file or the new one, and an
    interrupted run leaves no half-written note behind. The temporary file's
    name is short, so a note whose own name is as long as a name may be can
    still be written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".ocrust-{os.getpid()}-{next(_TEMPORARY)}.part")
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


def _key(path: Path) -> str:
    """A document's identity: its absolute path as given, symbolic links not
    followed — a link in one folder and its target in another are two
    documents, each with its note."""
    return os.path.normcase(str(_absolute(path)))


def _absolute(path: Path) -> Path:
    """`path` made absolute and `..` taken out, links left as they are."""
    return Path(os.path.normpath(Path(path).absolute()))


def _under(key: str, root: str) -> bool:
    return key == root or key.startswith(root.rstrip(os.sep) + os.sep)


#: The longest a name in a note's path may be, in bytes, before it is cut and
#: given a hash: file systems allow 255, and `.md` and a picture's folder come
#: on top.
_NAME_BYTES = 200


def _safe_stem(stem: str) -> str:
    """A note's path with every overlong name cut short, and made unique again
    by a hash of what was cut."""
    parts = []
    for part in stem.split("/"):
        raw = part.encode("utf-8", "surrogateescape")
        if len(raw) > _NAME_BYTES:
            digest = hashlib.sha1(raw).hexdigest()[:10]
            head = raw[: _NAME_BYTES - 12].decode("utf-8", "ignore")
            part = f"{head}~{digest}"
        parts.append(part)
    return "/".join(parts)


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
            root = _absolute(item)
            roots.append(root)
            prefix = root.name if several else ""
            for path in _walk(item, out_dir, skipped):
                relative = path.relative_to(item).as_posix()
                stem = posixpath.join(prefix, relative) if prefix else relative
                label = posixpath.join(root.name, relative)
                sources.append(_Source(path, _safe_stem(stem), label, root))
        elif item.is_file():
            if not supported(item.name):
                skipped.append(item)
                continue
            absolute = _absolute(item)
            sources.append(_Source(item, _safe_stem(item.name), item.name, absolute.parent))
            roots.append(absolute)
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
    """What was converted from what: `index.json`, and beside it a journal of
    every document converted since it was last written, so that a run killed
    halfway loses none of what it did."""

    def __init__(self, out_dir: Path, *, record: bool = True) -> None:
        self.path = out_dir / INDEX_DIR / INDEX_NAME
        self.journal = out_dir / INDEX_DIR / JOURNAL_NAME
        self.record = record
        self.entries: dict[str, dict[str, Any]] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict) and data.get("format") == _INDEX_FORMAT:
            entries = data.get("documents")
            if isinstance(entries, dict):
                self.entries = {k: v for k, v in entries.items() if isinstance(v, dict)}
        #: Notes a run was about to write when it was stopped: note → (key,
        #: hash). Such a note is the export's, not a stranger's.
        self.claims: dict[str, tuple[str, str]] = {}
        self._replay()
        self.saved = time.monotonic()
        self.owners: dict[str, str] = {}
        self.assets: dict[str, str] = {}
        for key, entry in self.entries.items():
            self._own(key, entry)

    def _replay(self) -> None:
        try:
            lines = self.journal.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                # The line a killed run was writing.
                continue
            if not isinstance(record, dict) or not isinstance(record.get("key"), str):
                continue
            if isinstance(record.get("entry"), dict):
                self.entries[record["key"]] = record["entry"]
            elif record.get("drop"):
                self.entries.pop(record["key"], None)
            elif isinstance(record.get("claim"), str):
                self.claims[record["claim"]] = (record["key"], str(record.get("sha256", "")))

    def _log(self, record: dict[str, Any]) -> None:
        if not self.record:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(_json(record) + "\n")

    def claim(self, key: str, note_path: str, digest: str) -> None:
        """Says, before a note is written, that it is about to be."""
        self._log({"key": key, "claim": note_path, "sha256": digest})

    def _own(self, key: str, entry: dict[str, Any]) -> None:
        for item in entry.get("notes", []):
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                self.owners[item["path"]] = key
        for asset in entry.get("assets", []):
            if isinstance(asset, str):
                self.assets[asset] = key

    def _disown(self, key: str) -> None:
        old = self.entries.get(key)
        if old is None:
            return
        for item in old.get("notes", []):
            if self.owners.get(item.get("path")) == key:
                del self.owners[item["path"]]
        for asset in old.get("assets", []):
            if self.assets.get(asset) == key:
                del self.assets[asset]

    def put(self, key: str, entry: dict[str, Any]) -> None:
        self._disown(key)
        self.entries[key] = entry
        self._own(key, entry)
        self._log({"key": key, "entry": entry})

    def drop(self, key: str) -> None:
        self._disown(key)
        self.entries.pop(key, None)
        self._log({"key": key, "drop": True})

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
        _write(self.path, (_json(payload, indent=1) + "\n").encode("utf-8"))
        with contextlib.suppress(OSError):
            self.journal.unlink()
        self.saved = time.monotonic()


def _json(value: Any, indent: int | None = None) -> str:
    """JSON that is valid UTF-8 whatever the paths in it: a file name that is
    not UTF-8 is written as escapes, and read back as the same name."""
    text = json.dumps(value, ensure_ascii=False, indent=indent)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, ensure_ascii=True, indent=indent)
    return text


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
        self.index = _Index(out_dir, record=not dry_run)
        self.result = ExportResult()
        self.options_key = _options_key(options)
        self._built: Ocr | None = None
        self.seen: set[str] = set()
        self.member_failed = ""

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
                    converted = self._one(
                        content, member, label, _safe_stem(stem), modified, None, None
                    )
                except (ConversionError, OcrustError, ValueError, OSError) as exc:
                    reason = f"{member}: {exc}"
                    self.result.failed.append((source.path, reason))
                    self.progress("failed", source.path, None, reason)
                    self.member_failed = reason
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
        key = _key(source.path)
        self.seen.add(key)
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
            target = self.out_dir / f"{source.stem}.md"
            reason = self._keep_reason(target, f"{source.stem}.md", key)
            if reason:
                self.result.kept.append((target, reason))
                self.progress("kept", target, None, reason)
            else:
                self.result.written.append((source.path, target))
                self.progress("would write", source.path, target, "")
            return
        self.member_failed = ""
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
        failure = self.member_failed
        for note_path, text, assets in notes:
            target = self.out_dir / note_path
            reason = self._keep_reason(target, note_path, key)
            if reason:
                # Left alone: still ours if it was, never ours if it was not,
                # and looked at again next time. Its pictures stay with it.
                kept = True
                self.result.kept.append((target, reason))
                self.progress("kept", target, None, reason)
                if note_path in old_notes:
                    entry_notes.append(old_notes[note_path])
                    entry_assets.extend(a for a in old_assets if _asset_of(a, note_path))
                continue
            try:
                payload = text.encode("utf-8")
                self.index.claim(key, note_path, hashlib.sha256(payload).hexdigest())
                _write(target, payload)
                for asset_path, content in assets.items():
                    owner = self.index.assets.get(asset_path)
                    destination = self.out_dir / asset_path
                    if owner not in (None, key) or (owner is None and destination.exists()):
                        # A picture the export did not put there: left alone.
                        continue
                    _write(destination, content)
                    entry_assets.append(asset_path)
            except (OSError, UnicodeError) as exc:
                failure = f"{note_path}: {getattr(exc, 'strerror', None) or exc}"
                self.result.failed.append((source.path, failure))
                self.progress("failed", source.path, None, failure)
                if note_path in old_notes:
                    entry_notes.append(old_notes[note_path])
                continue
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
        entry: dict[str, Any] = {
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
            "assets": sorted(set(entry_assets)),
        }
        if failure:
            # Tried again, and said again, on the next run.
            entry["error"] = failure
        self.index.put(key, entry)
        if time.monotonic() - self.index.saved > _SAVE_EVERY:
            self.index.save()

    def _keep_reason(self, target: Path, note_path: str, key: str) -> str:
        """Why a note must be left as it is, or "" when it may be written: it
        is not there, or it is what an export wrote for this document.

        A note of another document is never taken, `--force` or not — unless
        that document is gone and the note untouched since: then the folder
        was moved or renamed, and the note is this document's now.
        """
        owner = self.index.owner_of(note_path)
        if owner is not None and owner[0] != key:
            owner_key, item = owner
            gone = not Path(owner_key).exists()
            if gone and (not target.exists() or _file_sha256(target) == item.get("sha256")):
                self._release(owner_key, note_path)
                return ""
            return f"belongs to {self.index.entries[owner_key].get('source')}"
        if self.force or not target.exists():
            return ""
        if target.is_dir():
            return "a folder of that name is in the way"
        if owner is None:
            claim = self.index.claims.get(note_path)
            if claim is not None and claim[0] == key and _file_sha256(target) == claim[1]:
                # Written by a run that was stopped before it could say so.
                return ""
            return "not written by an export; --force replaces it"
        if _file_sha256(target) != owner[1].get("sha256"):
            return "edited since it was written; --force replaces it"
        return ""

    def _release(self, owner_key: str, note_path: str) -> None:
        """Takes a note, and its pictures, from a document that is gone."""
        entry = dict(self.index.entries[owner_key])
        entry["notes"] = [n for n in entry.get("notes", []) if n.get("path") != note_path]
        entry["assets"] = [a for a in entry.get("assets", []) if not _asset_of(a, note_path)]
        if entry["notes"]:
            self.index.put(owner_key, entry)
        else:
            self.index.drop(owner_key)

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
        root_keys = [_key(root) for root in roots]
        for key in list(self.index.entries):
            if key in seen:
                continue
            path = Path(key)
            inside = any(_under(key, root) for root in root_keys)
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
            kept_notes = []
            for item in entry.get("notes", []):
                if self._remove_note(item["path"], item.get("sha256", "")):
                    self.result.pruned.append(self.out_dir / item["path"])
                    self.progress("pruned", path, self.out_dir / item["path"], "")
                else:
                    kept_notes.append(item)
            kept_assets = []
            for asset in entry.get("assets", []):
                if any(_asset_of(asset, item["path"]) for item in kept_notes):
                    # An edited note keeps its pictures.
                    kept_assets.append(asset)
                    continue
                with contextlib.suppress(OSError):
                    (self.out_dir / asset).unlink()
                self._remove_empty((self.out_dir / asset).parent)
            if kept_notes:
                self.index.put(key, {**entry, "notes": kept_notes, "assets": kept_assets})
            else:
                self.index.drop(key)


def _in_hidden(path: Path, roots: Sequence[Path]) -> bool:
    key = _key(path)
    for root in roots:
        root_key = _key(root)
        if key != root_key and _under(key, root_key):
            relative = key[len(root_key.rstrip(os.sep)) + 1 :]
            return any(_hidden(part) for part in Path(relative).parts)
    return False


def _asset_of(asset: str, note_path: str) -> bool:
    """Whether a picture was kept for a note: it lives in the note's folder
    under `_assets/`."""
    return asset.startswith(posixpath.join(ASSETS_DIR, note_path[: -len(".md")]) + "/")


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
    # Names that differ only in case are one file on Windows and macOS, and
    # a knowledge base is synced to both: they count as a clash here too.
    notes: dict[str, Path] = {}
    for document in documents:
        claimed = notes.setdefault(document.stem.casefold(), document.path)
        if claimed != document.path:
            exporter.result.failed.append(
                (document.path, f"its note {document.stem}.md is also {claimed}'s; rename one")
            )
    clashing = {d.path for d in documents if notes.get(d.stem.casefold()) != d.path}
    seen = {_key(d.path) for d in documents}
    if dry_run:
        for document in documents:
            if document.path not in clashing:
                exporter.convert(document)
        if prune:
            exporter.prune(roots, seen)
        return exporter.result
    out.mkdir(parents=True, exist_ok=True)
    with _Lock(out / INDEX_DIR):
        try:
            for document in documents:
                if document.path not in clashing:
                    exporter.convert(document)
            if prune:
                exporter.prune(roots, seen)
        finally:
            exporter.index.save()
    return exporter.result
