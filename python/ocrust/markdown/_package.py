"""Zip packages and the XML inside them: Office, OpenDocument, EPUB.

Everything here reads files someone else wrote, so it is careful about it: a
part is read only up to a size limit whatever its header claims, and XML with
a document type declaration is refused, which is where entity expansion attacks
live — no Office or OpenDocument part has one.
"""

from __future__ import annotations

import datetime as _dt
import io
import posixpath
import re
import zipfile
from collections.abc import Iterator
from typing import Any
from xml.etree import ElementTree

#: The most any one part of a package may unpack to: pictures, and parts read
#: as a stream (a worksheet, the shared strings).
PART_LIMIT = 512 * 1024 * 1024
#: The most an XML part read whole may unpack to. Its tree takes twenty times
#: that in memory; a Word document of this size is a few thousand pages.
XML_LIMIT = 64 * 1024 * 1024


class ConversionError(Exception):
    """A file that cannot be converted, with the reason in words."""


class Package:
    """A zip archive, read part by part."""

    def __init__(self, data: bytes) -> None:
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ConversionError("not a valid zip package") from exc
        self.names = {info.filename for info in self.zip.infolist() if not info.is_dir()}
        # Some writers vary the case of part names; the lookup should not care.
        self._folded = {name.lower(): name for name in self.names}

    def find(self, name: str) -> str | None:
        name = name.lstrip("/")
        return name if name in self.names else self._folded.get(name.lower())

    def read(self, name: str, limit: int = PART_LIMIT) -> bytes | None:
        found = self.find(name)
        if found is None:
            return None
        with self.zip.open(found) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ConversionError(f"{found} unpacks to more than {limit // 2**20} MB")
        return data

    def xml(self, name: str) -> ElementTree.Element | None:
        data = self.read(name, XML_LIMIT)
        return parse_xml(data) if data is not None else None

    def stream_elements(
        self, name: str, tag: str, limit: int = PART_LIMIT
    ) -> Iterator[ElementTree.Element]:
        """The elements called `tag` of an XML part, one at a time, each let go
        once it has been looked at: a sheet of a million rows in the memory
        of one."""
        found = self.find(name)
        if found is None:
            return
        with self.zip.open(found) as raw:
            stream = _Limited(raw, limit, found)
            head = stream.peek_head()
            if b"<!doctype" in head[:4096].lower() or b"<!entity" in head.lower():
                raise ConversionError("XML with a document type declaration is not read")
            stack: list[ElementTree.Element] = []
            try:
                for event, element in ElementTree.iterparse(stream, events=("start", "end")):
                    if event == "start":
                        stack.append(element)
                        continue
                    stack.pop()
                    if local(element.tag) == tag:
                        yield element
                        element.clear()
                        if stack:
                            stack[-1].remove(element)
            except ElementTree.ParseError as exc:
                raise ConversionError(f"broken XML in {found}: {exc}") from exc


class _Limited(io.RawIOBase):
    """A zip member's stream that refuses to unpack past a limit."""

    def __init__(self, raw: Any, limit: int, name: str) -> None:
        self.raw = raw
        self.limit = limit
        self.name = name
        self.count = 0
        self.head = b""

    def peek_head(self) -> bytes:
        self.head = self.raw.read(65536)
        return self.head

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self.head:
            size = min(len(buffer), len(self.head))
            buffer[:size] = self.head[:size]
            self.head = self.head[size:]
        else:
            chunk = self.raw.read(len(buffer))
            size = len(chunk)
            buffer[:size] = chunk
        self.count += size
        if self.count > self.limit:
            raise ConversionError(f"{self.name} unpacks to more than {self.limit // 2**20} MB")
        return size


def parse_xml(data: bytes) -> ElementTree.Element:
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data[:65536].lower():
        raise ConversionError("XML with a document type declaration is not read")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ConversionError(f"broken XML: {exc}") from exc


def local(tag: Any) -> str:
    """An element's name without its namespace."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def attr(element: ElementTree.Element, name: str, default: str | None = None) -> str | None:
    """An attribute by its local name, whatever namespace it is in."""
    value = element.get(name)
    if value is not None:
        return value
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1] == name:
            return value
    return default


def rel_id(element: ElementTree.Element | None, name: str = "id") -> str:
    """A relationship id (`r:id`, `r:embed`, …): the attribute in the
    relationships namespace, not a plain one of the same name — a slide list
    entry carries both an `id` and an `r:id`."""
    if element is None:
        return ""
    for key, value in element.attrib.items():
        if key.endswith("}" + name) and "relationships" in key:
            return value
    return ""


def children(element: ElementTree.Element, name: str) -> Iterator[ElementTree.Element]:
    for child in element:
        if local(child.tag) == name:
            yield child


def child(element: ElementTree.Element | None, name: str) -> ElementTree.Element | None:
    if element is None:
        return None
    return next(children(element, name), None)


def descendants(element: ElementTree.Element, name: str) -> Iterator[ElementTree.Element]:
    for item in element.iter():
        if local(item.tag) == name:
            yield item


def relationships(package: Package, part: str) -> dict[str, tuple[str, str, bool]]:
    """A part's relationships: id → (type, target, external).

    Targets inside the package are resolved against the part's folder, so
    `media/image1.png` of `word/document.xml` comes back as
    `word/media/image1.png`.
    """
    folder, name = posixpath.split(part)
    rels = package.xml(posixpath.join(folder, "_rels", f"{name}.rels"))
    out: dict[str, tuple[str, str, bool]] = {}
    if rels is None:
        return out
    for rel in rels:
        rid = rel.get("Id")
        target = rel.get("Target") or ""
        kind = (rel.get("Type") or "").rsplit("/", 1)[-1]
        external = rel.get("TargetMode") == "External"
        if rid is None:
            continue
        if not external:
            target = (
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join(folder, target))
            )
        out[rid] = (kind, target, external)
    return out


def text_of(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def parse_date(value: str | None) -> _dt.datetime | _dt.date | None:
    """An ISO 8601 date or timestamp as written by Office and OpenDocument."""
    if not value:
        return None
    value = value.strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?", value)
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        if hour is None:
            return _dt.date(int(year), int(month), int(day))
        stamp = _dt.datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second or 0)
        )
    except ValueError:
        return None
    rest = value[match.end() :]
    rest = re.sub(r"^\.\d+", "", rest)
    if rest == "Z":
        return stamp.replace(tzinfo=_dt.timezone.utc)
    zone = re.match(r"^([+-])(\d{2}):?(\d{2})$", rest)
    if zone:
        sign = 1 if zone.group(1) == "+" else -1
        offset = _dt.timedelta(hours=int(zone.group(2)), minutes=int(zone.group(3)))
        return stamp.replace(tzinfo=_dt.timezone(sign * offset))
    return stamp


def office_properties(package: Package) -> dict[str, Any]:
    """Title, author, dates and keywords from `docProps/core.xml`."""
    core = package.xml("docProps/core.xml")
    meta: dict[str, Any] = {}
    if core is None:
        return meta
    fields = {local(item.tag): (item.text or "").strip() for item in core}
    meta["title"] = fields.get("title") or None
    meta["subject"] = fields.get("subject") or None
    meta["author"] = fields.get("creator") or None
    meta["description"] = fields.get("description") or None
    keywords = fields.get("keywords") or ""
    meta["keywords"] = [k.strip() for k in re.split(r"[;,]", keywords) if k.strip()] or None
    meta["created"] = parse_date(fields.get("created"))
    meta["modified"] = parse_date(fields.get("modified"))
    meta["language"] = fields.get("language") or None
    return meta


def open_document_properties(package: Package) -> dict[str, Any]:
    """The same, from an OpenDocument's `meta.xml`."""
    root = package.xml("meta.xml")
    meta: dict[str, Any] = {}
    if root is None:
        return meta
    office_meta = next(descendants(root, "meta"), None)
    if office_meta is None:
        return meta
    fields: dict[str, str] = {}
    keywords: list[str] = []
    for item in office_meta:
        name = local(item.tag)
        text = (item.text or "").strip()
        if name == "keyword" and text:
            keywords.append(text)
        elif text:
            fields.setdefault(name, text)
    meta["title"] = fields.get("title") or None
    meta["subject"] = fields.get("subject") or None
    meta["author"] = fields.get("initial-creator") or fields.get("creator") or None
    meta["description"] = fields.get("description") or None
    meta["keywords"] = keywords or None
    meta["created"] = parse_date(fields.get("creation-date"))
    meta["modified"] = parse_date(fields.get("date"))
    meta["language"] = fields.get("language") or None
    return meta
