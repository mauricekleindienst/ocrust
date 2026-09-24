"""OpenDocument text, spreadsheets and presentations (LibreOffice, and what
public administrations exchange)."""

from __future__ import annotations

import contextlib
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
    verbatim,
)
from ._office import _sheet_blocks
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
#: the sheet, not data — in a table of a text document.
_MAX_REPEAT = 1000
#: The most rows and columns of a sheet ever written out, whatever the limit.
_MAX_SHEET_ROWS = 1_000_000
_MAX_SHEET_COLUMNS = 1024
#: Drawn shapes whose text is read.
_DRAWN_SHAPES = {
    "custom-shape",
    "rect",
    "ellipse",
    "circle",
    "polygon",
    "polyline",
    "path",
    "regular-polygon",
    "caption",
    "measure",
    "connector",
    "line",
}


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
        #: The last number each list style reached, for a list that goes on
        #: counting where an earlier one stopped.
        self.numbers: dict[str | None, int] = {}

    # -- text

    def blocks(
        self, container: Element, list_style: str | None = None, depth: int = 0
    ) -> list[Block]:
        out: list[Block] = []
        code_run: Code | None = None
        for node in container:
            name = local(node.tag)
            if name in ("h", "p") and _hidden_paragraph(node):
                continue
            if name == "h":
                try:
                    level = int(attr(node, "outline-level") or 1)
                except ValueError:
                    level = 1
                extras_h: list[Block] = []
                content = self.inlines(node, extras_h)
                if not is_empty(content):
                    out.append(Heading(min(max(level, 1), 6), content))
                out.extend(extras_h)
            elif name == "p":
                extras: list[Block] = []
                content = self.inlines(node, extras)
                style = self.styles.name(attr(node, "style-name"))
                if style in ("preformatted text", "source text"):
                    # A line of code each: one block for the lines in a row,
                    # blank ones and indentation kept.
                    text = verbatim(content)
                    if out and out[-1] is code_run:
                        out[-1] = code_run = Code(code_run.text + "\n" + text)
                    elif text.strip():
                        out.append(code_run := Code(text))
                elif not is_empty(content):
                    if style in ("title",):
                        out.append(Heading(1, content))
                    elif style in ("quotations", "quote"):
                        out.append(Quote([Paragraph(content)]))
                    else:
                        out.append(Paragraph(content))
                out.extend(extras)
            elif name == "list":
                style = attr(node, "style-name") or list_style
                ordered = style in self.styles.ordered_lists
                start = 1
                if (attr(node, "continue-numbering") or "") == "true" or attr(
                    node, "continue-list"
                ):
                    start = self.numbers.get(style, 0) + 1
                items: list[list[Block]] = []
                for item in node:
                    if local(item.tag) in ("list-item", "list-header"):
                        if not items and attr(item, "start-value"):
                            with contextlib.suppress(ValueError):
                                start = max(int(attr(item, "start-value") or 1), 0)
                        items.append(self.blocks(item, style, depth + 1))
                items = [item for item in items if item]
                if items:
                    out.append(ListBlock(ordered=ordered, items=items, start=start))
                    if ordered:
                        self.numbers[style] = start + len(items) - 1
            elif name == "table":
                out.extend(self.table(node))
            elif name in (
                "section",
                "index-body",
                "table-of-content",
                "deletion-less",
                "text-content",
            ):
                if (attr(node, "display") or "") == "none":
                    # A hidden section.
                    continue
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
                if attr(node, "note-class") == "endnote":
                    # Footnote 1 and endnote 1 are two notes.
                    label = f"e{label}"
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
                "hidden-text",
                "hidden-paragraph",
                "deletion",
            ):
                pass
            else:
                self._walk(node, runs, fmt, link, extras)
            if node.tail:
                runs.append((node.tail, fmt, link))

    def shapes(self, container: Element) -> tuple[str, list[Block]]:
        """The text of a slide's shapes, in the order they are drawn, and the
        title among them: frames, text boxes, tables, pictures, drawn shapes
        with text in them, and groups of any of these."""
        title = ""
        out: list[Block] = []
        for node in container:
            name = local(node.tag)
            if name == "g":
                inner_title, inner = self.shapes(node)
                title = title or inner_title
                out.extend(inner)
                continue
            if name == "frame":
                found = self.frame(node)
                table = child(node, "table")
                if table is not None:
                    found.extend(self.table(table))
            elif name in _DRAWN_SHAPES:
                found = self.blocks(node)
            else:
                continue
            if attr(node, "class") == "title" and not title:
                title = " ".join(plain(b) for b in found).strip()
                if title:
                    continue
            out.extend(found)
        return title, out

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


def _sheet_rows(
    reader: _Reader, table: Element, limit: int | None
) -> tuple[dict[int, dict[int, str]], int]:
    """A sheet's non-empty rows, row → column → text, and how many rows past
    `limit` there were.

    Repeats are what an OpenDocument sheet is written with — a thousand
    identical rows are one row repeated a thousand times — so they are counted
    as rows, kept up to the limit, and never multiplied past it: a file of two
    kilobytes may claim a million rows of a million cells.
    """
    rows: dict[int, dict[int, str]] = {}
    dropped = 0
    index = 0
    cap = limit if limit is not None else _MAX_SHEET_ROWS
    for row in _table_rows(table):
        repeat = _repeat(attr(row, "number-rows-repeated"))
        values: dict[int, str] = {}
        column = 0
        for cell in row:
            name = local(cell.tag)
            if name not in ("table-cell", "covered-table-cell"):
                continue
            count = _repeat(attr(cell, "number-columns-repeated"))
            text = "" if name == "covered-table-cell" else plain(reader.cell(cell))
            if text:
                for offset in range(min(count, max(_MAX_SHEET_COLUMNS - column, 0))):
                    values[column + offset] = text
            column += count
        if values:
            kept = min(repeat, max(cap - len(rows), 0), reader.ctx.cells_left // len(values))
            reader.ctx.cells_left -= kept * len(values)
            for _ in range(kept):
                rows[index] = values
                index += 1
            dropped += repeat - kept
            index += repeat - kept
        else:
            index += repeat
    return rows, dropped


def _repeat(value: str | None) -> int:
    try:
        return max(int(value or 1), 1)
    except ValueError:
        return 1


def _hidden_paragraph(node: Element) -> bool:
    """Whether a paragraph is hidden by a field in it."""
    for item in node.iter():
        if local(item.tag) == "hidden-paragraph" and (attr(item, "is-hidden") or "true") != "false":
            return True
    return False


def ods(data: bytes, ctx: Context) -> Note:
    package = Package(data)
    reader = _Reader(package, ctx)
    sheet_root = next(descendants(reader.content, "spreadsheet"), None)  # type: ignore[arg-type]
    if sheet_root is None:
        raise ConversionError("no spreadsheet body")
    blocks: list[Block] = []
    count = 0
    truncated = False
    for table in children(sheet_root, "table"):
        if (attr(table, "display") or "true") == "false":
            continue
        count += 1
        name = attr(table, "name") or f"Sheet {count}"
        rows, dropped = _sheet_rows(reader, table, ctx.options.max_rows)
        truncated |= dropped > 0
        blocks.append(Marker(f"sheet {name}"))
        blocks.append(Heading(2, [name]))
        blocks.extend(_sheet_blocks(rows, dropped))
    meta = open_document_properties(package)
    meta["sheets"] = count
    if truncated:
        meta["truncated"] = True
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
        notes: list[Block] = []
        for part in children(page, "notes"):
            for inner in descendants(part, "text-box"):
                notes.extend(reader.blocks(inner))
        title, body = reader.shapes(page)
        blocks.append(Marker(f"slide {number}"))
        blocks.append(Heading(2, [title or attr(page, "name") or f"Slide {number}"]))
        blocks.extend(body)
        if notes:
            blocks.append(Quote([Paragraph([Span("strong", ["Notes:"])]), *notes]))
    meta = open_document_properties(package)
    meta["slides"] = number
    return Note(blocks=blocks, meta=meta)
