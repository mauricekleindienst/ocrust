"""Scanned pages and PDFs: an ocrust :class:`~ocrust.Document` as blocks.

The engine already knows the page's paragraphs, headings, list items and
tables. What a knowledge base needs on top is what a page layout says and a
text file cannot: which heading sits under which, where a line was broken to fit
the column and where on purpose, what runs along every page's top and bottom,
and where a paragraph carried on over a page break.
"""

from __future__ import annotations

import dataclasses
import re
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from ocrust._types import Block as ScanBlock
from ocrust._types import Document, Line, Page

from ._ir import Block, Break, Heading, Inline, ListBlock, Marker, Paragraph, Table, plain

#: How much of the page height, at the top and at the bottom, running heads and
#: page numbers are looked for in.
_MARGIN_SHARE = 0.12
#: Pages a line has to repeat on, as a share of all pages, to be boilerplate.
_REPEAT_SHARE = 0.5
#: Heading sizes within this ratio of each other are one level.
_LEVEL_STEP = 0.88

_PAGE_NUMBER = re.compile(
    r"^[-–—(\[]?\s*(seite|page|pagina|página|s\.|p\.)?\s*\d{1,4}"
    r"(\s*(von|of|de|/|sur|di)\s*\d{1,4})?\s*[-–—)\]]?$",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*(?:[•◦▪▫●○■□►▶➢➤✓✔·‣⁃]\s*|[-–—*]\s+)")
_ENUMERATOR = re.compile(r"^\s*(\d{1,3})([.)])\s+")
_ENDS_SENTENCE = re.compile(r"[.!?:;…\"”»“)\]]\s*$")


def blocks_of(doc: Document, *, paged: bool) -> list[Block]:
    """The document's text as blocks, a page marker before each page when
    `paged`."""
    pages = list(doc.pages)
    boilerplate = _boilerplate(pages)
    levels = _heading_levels(pages)
    out: list[Block] = []
    for page in pages:
        if paged:
            out.append(Marker(f"page {page.index + 1}"))
        out.extend(_page(page, boilerplate, levels))
    return _join_across_pages(out)


def extraction(doc: Document) -> str:
    """``text`` when every page was read from its own text, ``ocr`` when every
    page was recognized, ``mixed`` otherwise."""
    origins = {page.origin for page in doc.pages}
    if not origins:
        return "ocr"
    if origins == {"pdf_text"}:
        return "text"
    return "mixed" if "pdf_text" in origins else "ocr"


def _key(text: str) -> str:
    """A running head's identity: its words, with the page number made
    anonymous so `Seite 3 von 9` and `Seite 4 von 9` are one line."""
    return re.sub(r"\d+", "#", " ".join(text.lower().split()))


def _margin_lines(page: Page) -> list[Line]:
    """The lines at the very top and bottom of a page."""
    top = page.height * _MARGIN_SHARE
    bottom = page.height * (1 - _MARGIN_SHARE)
    return [
        line
        for block in page.blocks
        if block.kind != "table"
        for line in block.lines
        if line.box.y1 <= top or line.box.y0 >= bottom
    ]


def _boilerplate(pages: Sequence[Page]) -> set[str]:
    """What runs along the pages' edges: page numbers everywhere, and any line
    at the top or bottom repeated on half the pages or more.

    A single page keeps everything: with nothing to compare it to, a line at
    its foot is as likely the text's last line as a footer.
    """
    if len(pages) < 2:
        return set()
    counts: Counter[str] = Counter()
    numbers: set[str] = set()
    for page in pages:
        seen: set[str] = set()
        for line in _margin_lines(page):
            key = _key(line.text)
            if _PAGE_NUMBER.match(line.text.strip()):
                numbers.add(key)
            seen.add(key)
        counts.update(seen)
    needed = max(3, len(pages) * _REPEAT_SHARE) if len(pages) >= 3 else len(pages)
    repeated = {key for key, count in counts.items() if count >= needed}
    return repeated | numbers


def _heading_levels(pages: Sequence[Page]) -> dict[float, int]:
    """Heading level by the height of a heading's first line.

    The largest size in the document is `#`, the next `##`, and so on;
    sizes a little apart are one size — a recognized line's box varies by a
    few pixels with the letters in it.
    """
    sizes = sorted(
        {
            round(block.lines[0].box.height, 1)
            for page in pages
            for block in page.blocks
            if block.kind == "heading" and block.lines
        },
        reverse=True,
    )
    levels: dict[float, int] = {}
    level, anchor = 0, None
    for size in sizes:
        if anchor is None or size < anchor * _LEVEL_STEP:
            level += 1
            anchor = size
        levels[size] = min(level, 6)
    return levels


def _page(page: Page, boilerplate: set[str], levels: dict[float, int]) -> list[Block]:
    top = page.height * _MARGIN_SHARE
    bottom = page.height * (1 - _MARGIN_SHARE)

    def kept(line: Line) -> bool:
        at_edge = line.box.y1 <= top or line.box.y0 >= bottom
        return not (at_edge and _key(line.text) in boilerplate)

    blocks: list[tuple[ScanBlock, list[Line]]] = []
    for block in page.blocks:
        lines = (
            list(block.lines) if block.kind == "table" else [ln for ln in block.lines if kept(ln)]
        )
        if lines and any(line.text.strip() for line in lines):
            blocks.append((block, lines))

    columns = _column_edges([(b, ls) for b, ls in blocks if b.kind == "paragraph"])
    out: list[Block] = []
    items: list[_Item] = []

    def flush_list() -> None:
        if items:
            out.append(_nest(items))
            items.clear()

    for block, lines in blocks:
        if block.kind == "list_item":
            items.extend(_items(lines, columns))
            continue
        flush_list()
        if block.kind == "table" and block.table is not None:
            rows = block.table.as_rows()
            if block.table.columns >= 2 and rows:
                out.append(Table([[[cell] for cell in row] for row in rows]))
                continue
        if block.kind == "heading":
            text = " ".join(line.text.strip() for line in lines)
            level = levels.get(round(lines[0].box.height, 1), 2)
            out.append(Heading(level, [text]))
            continue
        out.append(Paragraph(_joined(lines, _right_edge(lines, columns))))
    flush_list()
    return out


def _column_edges(blocks: list[tuple[ScanBlock, list[Line]]]) -> list[tuple[float, float]]:
    """The horizontal extent of each paragraph on the page."""
    return [
        (min(line.box.x0 for line in lines), max(line.box.x1 for line in lines))
        for _, lines in blocks
    ]


def _right_edge(lines: list[Line], columns: list[tuple[float, float]]) -> float:
    """Where the column this block sits in ends.

    Not where the block itself ends: a two-line signature — `Mit freundlichen
    Grüßen`, then a name — is narrower than the column, and measured against
    itself its first line would look full.
    """
    x0 = min(line.box.x0 for line in lines)
    x1 = max(line.box.x1 for line in lines)
    right = x1
    for left, end in columns:
        overlap = min(x1, end) - max(x0, left)
        if overlap > 0.5 * min(x1 - x0, end - left):
            right = max(right, end)
    return right


def _first_word_width(line: Line) -> float:
    if line.words:
        return line.words[0].box.width
    text = line.text.strip()
    word = text.split(" ", 1)[0] if text else ""
    return line.box.width * len(word) / max(len(text), 1)


def _joined(lines: Sequence[Line], right: float) -> list[Inline]:
    """A paragraph's lines as one run of text, with a line break kept only
    where the line ended although the next line's first word would have fit.

    That is how a line broken on purpose — an address, a list of names, a
    signature — tells itself apart from one the column was simply full at.
    """
    content: list[Inline] = []
    for index, line in enumerate(lines):
        text = line.text.strip()
        if not text:
            continue
        if content:
            previous = lines[index - 1]
            room = right - previous.box.x1
            needed = _first_word_width(line) + 0.35 * previous.box.height
            content.append(Break() if room > needed else " ")
        content.append(text)
    return content


@dataclass
class _Item:
    x: float
    ordered: bool
    number: int
    content: list[Inline]


def _items(lines: list[Line], columns: list[tuple[float, float]]) -> list[_Item]:
    """A list-item block's items: a line with a bullet or a number opens one,
    any other line carries on the one before."""
    items: list[_Item] = []
    groups: list[list[Line]] = []
    for line in lines:
        if not groups or _BULLET.match(line.text) or _ENUMERATOR.match(line.text):
            groups.append([line])
        else:
            groups[-1].append(line)
    right = _right_edge(lines, columns)
    for group in groups:
        first = group[0].text
        numbered = _ENUMERATOR.match(first)
        bullet = None if numbered else _BULLET.match(first)
        marker = numbered or bullet
        head = first[marker.end() :] if marker else first
        content = _joined([_with_text(group[0], head), *group[1:]], right)
        items.append(
            _Item(
                x=group[0].box.x0,
                ordered=numbered is not None,
                number=int(numbered.group(1)) if numbered else 1,
                content=content,
            )
        )
    return items


def _with_text(line: Line, text: str) -> Line:
    return dataclasses.replace(line, text=text)


def _nest(items: list[_Item]) -> ListBlock:
    """Items as a list, nested by how far each is indented."""
    step = statistics.median(
        [abs(b.x - a.x) for a, b in zip(items, items[1:]) if abs(b.x - a.x) > 4] or [1e9]
    )
    tolerance = max(step * 0.5, 6.0)
    return _build(items, 0, tolerance)[0]


def _build(items: list[_Item], start: int, tolerance: float) -> tuple[ListBlock, int]:
    first = items[start]
    result = ListBlock(ordered=first.ordered, items=[], start=first.number)
    index = start
    while index < len(items):
        item = items[index]
        if item.x < first.x - tolerance:
            break
        if item.x > first.x + tolerance and result.items:
            child, index = _build(items, index, tolerance)
            result.items[-1].append(child)
            continue
        result.items.append([Paragraph(item.content)])
        index += 1
    return result, index


def _join_across_pages(blocks: list[Block]) -> list[Block]:
    """Joins a paragraph a page break cut in two.

    The first half ends without closing its sentence, and the second starts in
    lower case. The joined paragraph stands before the marker of the page it
    ends on, on the page it began.
    """
    out: list[Block] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if (
            isinstance(block, Paragraph)
            and index + 2 < len(blocks)
            and isinstance(blocks[index + 1], Marker)
            and isinstance(blocks[index + 2], Paragraph)
        ):
            head, tail = plain(block.content), plain(blocks[index + 2].content)
            if head and tail and not _ENDS_SENTENCE.search(head) and tail[0].islower():
                content = list(block.content)
                if head.endswith("-") and len(head) > 1 and head[-2].isalpha():
                    # A word hyphenated across the page break.
                    last = content[-1]
                    if isinstance(last, str) and last.endswith("-"):
                        content[-1] = last[:-1]
                        content.extend(blocks[index + 2].content)
                    else:
                        content.extend([" ", *blocks[index + 2].content])
                else:
                    content.extend([" ", *blocks[index + 2].content])
                out.append(Paragraph(content))
                out.append(blocks[index + 1])
                index += 3
                continue
        out.append(block)
        index += 1
    return out
