"""E-books, zip archives, and the binary Office formats of before 2007."""

from __future__ import annotations

import posixpath
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ._context import Context
from ._html import decode_html, html_blocks
from ._ir import Block, Heading, Marker, Note, body_of, relabel
from ._package import (
    ConversionError,
    Package,
    attr,
    child,
    children,
    descendants,
    local,
    parse_date,
)

#: Members of an archive read at most, and bytes unpacked at most: a zip bomb
#: stops here instead of filling the disk.
_MAX_MEMBERS = 10_000
_MAX_UNPACKED = 2 * 1024**3


def epub(data: bytes, ctx: Context) -> Note:
    package = Package(data)
    container = package.xml("META-INF/container.xml")
    rootfile = next(descendants(container, "rootfile"), None) if container is not None else None
    opf_path = attr(rootfile, "full-path") if rootfile is not None else None
    if not opf_path:
        raise ConversionError("no package document in META-INF/container.xml")
    opf = package.xml(opf_path)
    if opf is None:
        raise ConversionError(f"{opf_path} is missing")
    folder = posixpath.dirname(opf_path)
    meta: dict[str, Any] = {}
    metadata = child(opf, "metadata")
    if metadata is not None:
        for item in metadata:
            name = local(item.tag)
            text = (item.text or "").strip()
            if not text:
                continue
            if name == "title":
                meta.setdefault("title", text)
            elif name == "creator":
                meta.setdefault("author", text)
            elif name == "language":
                meta.setdefault("language", text.split("-")[0].lower())
            elif name == "date":
                meta.setdefault("created", parse_date(text))
            elif name == "subject":
                meta.setdefault("keywords", []).append(text)
            elif name == "description":
                meta.setdefault("description", text)
            elif name == "publisher":
                meta.setdefault("publisher", text)
    manifest: dict[str, tuple[str, str]] = {}
    for item in (
        children(child(opf, "manifest"), "item") if child(opf, "manifest") is not None else []
    ):
        href = unquote(attr(item, "href") or "")
        manifest[attr(item, "id") or ""] = (
            posixpath.normpath(posixpath.join(folder, href)),
            attr(item, "media-type") or "",
        )
    blocks: list[Block] = []
    spine = child(opf, "spine")
    chapter = 0
    for ref in children(spine, "itemref") if spine is not None else []:
        if attr(ref, "linear") == "no":
            continue
        entry = manifest.get(attr(ref, "idref") or "")
        if entry is None or "html" not in entry[1]:
            continue
        path = entry[0]
        raw = package.read(path)
        if raw is None:
            continue
        chapter_folder = posixpath.dirname(path)

        def resolve(src: str, base: str = chapter_folder) -> tuple[bytes, str] | None:
            target = posixpath.normpath(posixpath.join(base, unquote(src.split("#", 1)[0])))
            content = package.read(target)
            return (content, target) if content is not None else None

        chapter_blocks, _ = html_blocks(decode_html(raw), ctx, resolve)
        if chapter_blocks:
            chapter += 1
            blocks.append(Marker(f"chapter {chapter}"))
            blocks.extend(chapter_blocks)
    meta["chapters"] = chapter
    return Note(blocks=blocks, meta=meta)


def zip_archive(data: bytes, ctx: Context) -> Note:
    """An archive as one note, a section per member it can convert.

    The store writes an archive's members as notes of their own instead; this
    is what converting a single archive gives.
    """
    blocks: list[Block] = []
    for name, content in archive_members(data):
        if ctx.convert is None:
            break
        try:
            note = ctx.convert(content, name, ctx)
        except Exception:  # noqa: BLE001 - one member must not sink the archive
            continue
        content = body_of(note) if note is not None else []
        if not content:
            continue
        blocks.append(Marker(f"member {name}"))
        blocks.append(Heading(2, [name]))
        blocks.extend(
            Heading(min(b.level + 2, 6), b.content) if isinstance(b, Heading) else b
            for b in relabel(content, f"z{len(blocks)}-")
        )
    return Note(blocks=blocks)


def archive_members(data: bytes) -> list[tuple[str, bytes]]:
    """The files of a zip archive, name and bytes, within safe limits."""
    try:
        archive = zipfile.ZipFile(__import__("io").BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ConversionError("not a valid zip archive") from exc
    out: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    folded: set[str] = set()
    total = 0
    for info in archive.infolist()[:_MAX_MEMBERS]:
        name = info.filename.replace("\\", "/")
        base = posixpath.basename(name)
        if info.is_dir() or not base or base.startswith((".", "~$")) or "__MACOSX/" in name:
            continue
        if info.flag_bits & 0x1:
            # Encrypted: nothing to read without the password.
            continue
        remaining = _MAX_UNPACKED - total
        if remaining <= 0:
            break
        with archive.open(info) as handle:
            content = handle.read(remaining + 1)
        if len(content) > remaining:
            break
        total += len(content)
        clean = posixpath.normpath(name).lstrip("/")
        if clean.startswith("../") or clean == ".." or clean in seen:
            # Outside the archive, or a second entry of one name: the first
            # is the one kept.
            continue
        seen.add(clean)
        if clean.casefold() in folded:
            # `Protokoll.txt` beside `protokoll.txt`: two files on Linux, one
            # on Windows and macOS. The second is kept under a name of its own.
            stem, dot, suffix = clean.rpartition(".")
            if not dot or "/" in suffix:
                stem, dot, suffix = clean, "", ""
            counter = 2
            while f"{stem}~{counter}{dot}{suffix}".casefold() in folded:
                counter += 1
            clean = f"{stem}~{counter}{dot}{suffix}"
        folded.add(clean.casefold())
        out.append((clean, content))
    return out


def office_binary(data: bytes, ctx: Context, suffix: str) -> Note:
    """`.doc`, `.xls`, `.ppt` and friends, through LibreOffice when it is
    installed: converted to their XML successors, then read like those."""
    target = {
        ".doc": "docx",
        ".dot": "docx",
        ".wpd": "docx",
        ".wps": "docx",
        ".xls": "xlsx",
        ".xlt": "xlsx",
        ".ppt": "pptx",
        ".pps": "pptx",
        ".pot": "pptx",
    }[suffix]
    office = next(
        (found for name in ("soffice", "libreoffice") if (found := shutil.which(name))), None
    )
    if office is None:
        raise ConversionError(
            f"{suffix} files need LibreOffice (soffice) to convert; "
            f"save it as .{target} or install LibreOffice"
        )
    with tempfile.TemporaryDirectory(prefix="ocrust-") as temporary:
        folder = Path(temporary)
        source = folder / f"document{suffix}"
        source.write_bytes(data)
        command = [
            office,
            f"-env:UserInstallation={(folder / 'profile').as_uri()}",
            "--headless",
            "--norestore",
            "--convert-to",
            target,
            "--outdir",
            str(folder),
            str(source),
        ]
        try:
            subprocess.run(command, capture_output=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConversionError(f"LibreOffice failed: {exc}") from exc
        converted = folder / f"document.{target}"
        if not converted.is_file():
            raise ConversionError(f"LibreOffice could not convert this {suffix} file")
        result = converted.read_bytes()
    from . import _office

    return getattr(_office, target)(result, ctx)
