"""OpenDocument text, spreadsheets and presentations (LibreOffice, and what
public administrations exchange)."""

from __future__ import annotations

import re
from xml.etree.ElementTree import Element

from ._context import Context
from ._ir import (
    Block,
    Break,
    Code,
    Footnote,
    FootnoteRef,
    Heading,
    Inline,
    ListBlock,
    Marker,
    Note,
    Paragraph,
    Quote,
    Run,
    Span,
    Table,
    assemble,
    flatten,
    is_empty,
    plain,
)
from ._package import (
    ConversionError,
    Package,
    attr,
    child,
    children,
    descendants,
    local,
    open_document_properties,
)

#: Repeats beyond this are a spreadsheet's formatting running to the edge of
#: the sheet, not data.
_MAX_REPEAT = 1000


class _Styles:
    """Which automatic and named styles are bold, italic, struck, or lists."""

    def __init__(self, *roots: Element | None) -> None:
        self.text: dict[str, frozenset[str]] = {}
        self.parent: dict[str, str] = {}
        self.ordered_lists: set[str] = set()
        self.paragraph_names: dict[str, str] = {}
        for root in roots:
            if root is None:
                continue
            for style in descendants(root, "style"):
                name = attr(style, "name") or ""
                parent = attr(style, "parent-style-name")
                if parent:
                    self.parent[name] = parent
                self.paragraph_names[name] = (attr(style, "display-name") or name).lower()
                props = child(style, "text-properties")
                fmt: set[str] = set()
                if props is not None:
                    if attr(props, "font-weight") == "bold":
                        fmt.add("strong")
                    if attr(props, "font-style") == "italic":
                        fmt.add("emph")
                    if (attr(props, "text-line-through-style") or "none") != "none":
                        fmt.add("strike")
                self.text[name] = frozenset(fmt)
            for list_style in descendants(root, "list-style"):
                first = next(iter(list_style), None)
                if first is not None and local(first.tag) == "list-level-style-number":
                    self.ordered_lists.add(attr(list_style, "name") or "")

    def formatting(self, name: str | None) -> frozenset[str]:
        seen: set[str] = set()
        while name and name not in seen:
            seen.add(name)
            if self.text.get(name):
                return self.text[name]
            name = self.parent.get(name)
        return frozenset()

    def name(self, style: str | None) -> str:
        seen: set[str] = set()
        while style and style not in seen:
            seen.add(style)
            display = self.paragraph_names.get(style, "")
            if display and not re.match(r"^p\d+$", display):
                return display
            style = self.parent.get(style)
        return ""


class _Reader:
    def __init__(self, package: Package, ctx: Context) -> None:
        self.package = package
        self.ctx = ctx
        self.content = package.xml("content.xml")
        if self.content is None:
            raise ConversionError("no content.xml in the package")
        self.styles = _Styles(package.xml("styles.xml"), self.content)
        self.notes: list[Footnote] = []

    # -- text

    def blocks(
        self, container: Element, list_style: str | None = None, depth: int = 0
    ) -> list[Block]:
        out: list[Block] = []
        for node in container:
            name = local(node.tag)
            if name == "h":
                level = int(attr(node, "outline-level") or 1)
                content = self.inlines(node, out)
                if not is_empty(content):
                    out.insert(len(out), Heading(min(max(level, 1), 6), content))
            elif name == "p":
                extras: list[Block] = []
                content = self.inlines(node, extras)
                style = self.styles.name(attr(node, "style-name"))
                if not is_empty(content):
                    if style in ("title",):
                        out.append(Heading(1, content))
                    elif style in ("quotations", "quote"):
                        out.append(Quote([Paragraph(content)]))
                    elif style in ("preformatted text", "source text"):
                        out.append(Code(plain(content)))
                    else:
                        out.append(Paragraph(content))
                out.extend(extras)
            elif name == "list":
                style = attr(node, "style-name") or list_style
                items: list[list[Block]] = []
                for item in node:
                    if local(item.tag) in ("list-item", "list-header"):
                        items.append(self.blocks(item, style, depth + 1))
                items = [item for item in items if item]
                if items:
                    out.append(ListBlock(ordered=style in self.styles.ordered_lists, items=items))
            elif name == "table":
                out.extend(self.table(node))
            elif name in (
                "section",
                "index-body",
                "table-of-content",
                "deletion-less",
                "text-content",
            ):
                out.extend(self.blocks(node, list_style, depth))
            elif name == "frame":
                out.extend(self.frame(node))
        return out

    def inlines(self, element: Element, extras: list[Block]) -> list[Inline]:
        runs: list[Run] = []
        self._walk(element, runs, frozenset(), "", extras)
        return assemble(runs)

    def _walk(
        self, element: Element, runs: list[Run], fmt: frozenset[str], link: str, extras: list[Block]
    ) -> None:
        if element.text:
            runs.append((element.text, fmt, link))
        for node in element:
            name = local(node.tag)
            if name == "span":
                inner = fmt | self.styles.formatting(attr(node, "style-name"))
                self._walk(node, runs, inner, link, extras)
            elif name == "a":
                self._walk(node, runs, fmt, attr(node, "href") or link, extras)
            elif name == "s":
                runs.append((" " * min(int(attr(node, "c") or 1), 8), fmt, link))
            elif name == "tab":
                runs.append(("\t", fmt, link))
            elif name == "line-break":
                runs.append((Break(), frozenset(), link))
            elif name == "note":
                body = child(node, "note-body")
                citation = child(node, "note-citation")
                label = (citation.text or "").strip() if citation is not None else ""
                label = label or str(len(self.notes) + 1)
                if body is not None:
                    self.notes.append(Footnote(label, self.blocks(body)))
                    runs.append((FootnoteRef(label), frozenset(), ""))
            elif name == "frame":
                extras.extend(self.frame(node))
            elif name in (
                "tracked-changes",
                "annotation",
                "bookmark",
                "soft-page-break",
                "note-citation",
            ):
                pass
            else:
                self._walk(node, runs, fmt, link, extras)
            if node.tail:
                runs.append((node.tail, fmt, link))

    def frame(self, frame: Element) -> list[Block]:
        out: list[Block] = []
        box = child(frame, "text-box")
        if box is not None:
            out.extend(self.blocks(box))
        image = child(frame, "image")
        if image is not None:
            href = attr(image, "href") or ""
            if href and not href.startswith(("http:", "https:")):
                alt = child(frame, "desc")
                picture = self.ctx.picture(
                    self.package.read(href), href, (alt.text or "") if alt is not None else ""
                )
                if picture is not None:
                    out.append(picture)
        return out

    def table(self, table: Element) -> list[Block]:
        rows: list[list[list[Inline]]] = []
        for row in _table_rows(table):
            repeat = min(int(attr(row, "number-rows-repeated") or 1), _MAX_REPEAT)
            cells: list[list[Inline]] = []
            for cell in row:
                name = local(cell.tag)
                if name not in ("table-cell", "covered-table-cell"):
                    continue
                count = min(int(attr(cell, "number-columns-repeated") or 1), _MAX_REPEAT)
                content = [] if name == "covered-table-cell" else self.cell(cell)
                cells.extend([content] * count)
            while cells and not cells[-1]:
                cells.pop()
            if not cells:
                rows.extend([] for _ in range(1 if repeat > 1 else repeat))
                continue
            rows.extend([cells] * repeat)
        while rows and not rows[-1]:
            rows.pop()
        return self._grid(rows)

    def cell(self, cell: Element) -> list[Inline]:
        kind = attr(cell, "value-type")
        blocks = self.blocks(cell)
        if blocks:
            return flatten(blocks)
        if kind in ("float", "currency", "percentage"):
            return [attr(cell, "value") or ""]
        if kind == "date":
            return [attr(cell, "date-value") or ""]
        if kind == "boolean":
            return [(attr(cell, "boolean-value") or "").upper()]
        return []

    def _grid(self, rows: list[list[list[Inline]]]) -> list[Block]:
        filled = [row for row in rows if any(not is_empty(c) for c in row)]
        if not filled:
            return []
        width = max(len(row) for row in filled)
        used = [c for c in range(width) if any(c < len(r) and not is_empty(r[c]) for r in filled)]
        if len(used) <= 1:
            return [Paragraph(row[used[0]]) for row in filled if used and used[0] < len(row)]
        return [Table([[row[c] if c < len(row) else [] for c in used] for row in filled])]


def _table_rows(table: Element) -> list[Element]:
    rows: list[Element] = []
    for node in table:
        name = local(node.tag)
        if name == "table-row":
            rows.append(node)
        elif name in ("table-header-rows", "table-rows", "table-row-group"):
            rows.extend(_table_rows(node))
    return rows


def odt(data: bytes, ctx: Context) -> Note:
    package = Package(data)
    reader = _Reader(package, ctx)
    body = next(descendants(reader.content, "text"), None)  # type: ignore[arg-type]
    if body is None:
        raise ConversionError("no text body")
    blocks = reader.blocks(body)
    blocks.extend(reader.notes)
    return Note(blocks=blocks, meta=open_document_properties(package))


def ods(data: bytes, ctx: Context) -> Note:
    package = Package(data)
    reader = _Reader(package, ctx)
    sheet_root = next(descendants(reader.content, "spreadsheet"), None)  # type: ignore[arg-type]
    if sheet_root is None:
        raise ConversionError("no spreadsheet body")
    blocks: list[Block] = []
    count = 0
    for table in children(sheet_root, "table"):
        count += 1
        name = attr(table, "name") or f"Sheet {count}"
        blocks.append(Marker(f"sheet {name}"))
        blocks.append(Heading(2, [name]))
        grid = reader.table(table)
        limit = ctx.options.max_rows
        for block in grid:
            if isinstance(block, Table) and limit is not None and len(block.rows) > limit:
                dropped = len(block.rows) - limit
                block.rows = block.rows[:limit]
                blocks.append(block)
                blocks.append(Paragraph([Span("emph", [f"{dropped} more rows not shown"])]))
            else:
                blocks.append(block)
    meta = open_document_properties(package)
    meta["sheets"] = count
    return Note(blocks=blocks, meta=meta)


def odp(data: bytes, ctx: Context) -> Note:
    package = Package(data)
    reader = _Reader(package, ctx)
    presentation = next(descendants(reader.content, "presentation"), None)  # type: ignore[arg-type]
    if presentation is None:
        raise ConversionError("no presentation body")
    blocks: list[Block] = []
    number = 0
    for page in children(presentation, "page"):
        number += 1
        title = ""
        body: list[Block] = []
        notes: list[Block] = []
        for frame in page:
            name = local(frame.tag)
            if name == "notes":
                for inner in descendants(frame, "text-box"):
                    notes.extend(reader.blocks(inner))
                continue
            if name not in ("frame", "custom-shape", "g"):
                continue
            kind = attr(frame, "class")
            frame_blocks = reader.frame(frame) if name == "frame" else reader.blocks(frame)
            table = next(descendants(frame, "table"), None)
            if table is not None and name == "frame":
                frame_blocks.extend(reader.table(table))
            if kind == "title" and not title:
                title = " ".join(plain(b) for b in frame_blocks).strip()
                continue
            body.extend(frame_blocks)
        blocks.append(Marker(f"slide {number}"))
        blocks.append(Heading(2, [title or attr(page, "name") or f"Slide {number}"]))
        blocks.extend(body)
        if notes:
            blocks.append(Quote([Paragraph([Span("strong", ["Notes:"])]), *notes]))
    meta = open_document_properties(package)
    meta["slides"] = number
    return Note(blocks=blocks, meta=meta)
