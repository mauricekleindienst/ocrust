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

from ._ir import Block, Break, Code, Entry, Heading, Inline, Marker, Paragraph, Table, nest, plain

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
    repeated = _repeated_headings(pages)
    out: list[Block] = []
    for page in pages:
        if paged:
            out.append(Marker(f"page {page.index + 1}"))
        out.extend(_page(page, boilerplate, levels, repeated))
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


#: Characters of the scripts written without spaces between words: Chinese,
#: Japanese, and their punctuation and full-width forms.
_UNSPACED = re.compile(
    "[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef"
    "\U00020000-\U0002ffff]"
)


def _glue(before: str, after: str) -> str:
    """What joins two lines of one paragraph: a space, or nothing between
    two characters of Chinese or Japanese, which set no spaces between words."""
    if before and after and _UNSPACED.match(before[-1]) and _UNSPACED.match(after[0]):
        return ""
    return " "


#: A line that starts with its own label: a few words, a colon, the value.
_LABELLED = re.compile(r"[^\W\d][\w .\-/()]{0,31}:\s+\S")


def _key(text: str) -> str:
    """A running head's identity: its words, with the page number made
    anonymous so `Seite 3 von 9` and `Seite 4 von 9` are one line."""
    return re.sub(r"\d+", "#", " ".join(text.lower().split()))


def _keys(line: Line) -> list[str]:
    """The running heads a margin line may be: itself, and each of its parts
    when it was merged from several — a browser's date and page title, set on
    one baseline, are one line on one page and two on the next."""
    keys = [_key(line.text)]
    if len(line.segments) >= 2:
        keys += [_key(segment.text) for segment in line.segments]
    return keys


def _is_boilerplate(line: Line, boilerplate: set[str]) -> bool:
    """Whether a margin line is a running head, whole or in all its parts."""
    whole, *parts = _keys(line)
    return whole in boilerplate or bool(parts) and all(part in boilerplate for part in parts)


def _heading_key(text: str) -> str:
    """A heading's identity: its words as they are, numbers included — the
    chapters "Kapitel 1" and "Kapitel 2" are two headings."""
    return " ".join(text.lower().split())


def _repeated_headings(pages: Sequence[Page]) -> set[str]:
    """Headings at a page's edge that come back, word for word, on three
    pages and half of them: a letterhead set large is a running head too. A
    title that says what the running heads say, once, is not, and nor is a
    slide title used twice in a deck."""
    counts: Counter[str] = Counter()
    top_share, bottom_share = _MARGIN_SHARE, 1 - _MARGIN_SHARE
    for page in pages:
        seen = {
            _heading_key(line.text)
            for block in page.blocks
            if block.kind == "heading"
            for line in block.lines
            if line.box.y1 <= page.height * top_share or line.box.y0 >= page.height * bottom_share
        }
        counts.update(seen)
    needed = max(3, (len(pages) + 1) // 2)
    return {key for key, count in counts.items() if count >= needed}


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
    # The numbers in each repeated line, by page: a page number counts up with
    # the pages, a running balance does not.
    found: dict[str, dict[int, list[int]]] = {}
    for page in pages:
        seen: set[str] = set()
        for line in _margin_lines(page):
            texts = [line.text] + [s.text for s in line.segments if len(line.segments) >= 2]
            for key, text in zip(_keys(line), texts, strict=True):
                if _PAGE_NUMBER.match(text.strip()):
                    numbers.add(key)
                if key not in seen:
                    found.setdefault(key, {})[page.index] = [
                        int(n) for n in re.findall(r"\d+", text)
                    ]
                seen.add(key)
        counts.update(seen)
    needed = max(3, len(pages) * _REPEAT_SHARE) if len(pages) >= 3 else len(pages)
    repeated = {
        key
        for key, count in counts.items()
        if count >= needed and _numbered_like_pages(found.get(key, {}))
    }
    return repeated | numbers


def _numbered_like_pages(occurrences: dict[int, list[int]]) -> bool:
    """Whether the numbers of a line repeated along the pages are the same on
    each, or count with the pages (`Seite 3 von 9`): then it is a running head.
    A subtotal carried from page to page is text."""
    shapes = {len(numbers) for numbers in occurrences.values()}
    if len(shapes) != 1:
        return False
    for position in range(shapes.pop()):
        values = {index: numbers[position] for index, numbers in occurrences.items()}
        if len(set(values.values())) > 1 and len({v - i for i, v in values.items()}) > 1:
            return False
    return True


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


def _page(
    page: Page, boilerplate: set[str], levels: dict[float, int], repeated: set[str]
) -> list[Block]:
    top = page.height * _MARGIN_SHARE
    bottom = page.height * (1 - _MARGIN_SHARE)

    def kept(line: Line) -> bool:
        at_edge = line.box.y1 <= top or line.box.y0 >= bottom
        return not (at_edge and _is_boilerplate(line, boilerplate))

    def kept_heading(line: Line) -> bool:
        # A page number set large is dropped with the other page numbers.
        at_edge = line.box.y1 <= top or line.box.y0 >= bottom
        numbered = _PAGE_NUMBER.match(line.text.strip()) and _key(line.text) in boilerplate
        return not (at_edge and (_heading_key(line.text) in repeated or numbered))

    blocks: list[tuple[ScanBlock, list[Line]]] = []
    for block in page.blocks:
        # A heading is a running head only when it comes back as a heading:
        # the title on page one is what a browser repeats small at every top.
        if block.kind == "table":
            lines = list(block.lines)
        elif block.kind == "heading":
            lines = [ln for ln in block.lines if kept_heading(ln)]
        else:
            lines = [ln for ln in block.lines if kept(ln)]
        if lines and any(line.text.strip() for line in lines):
            blocks.append((block, lines))

    columns = _Columns(
        _column_edges([(b, ls) for b, ls in blocks if b.kind == "paragraph"]),
        [_extent(ls) for _, ls in blocks],
    )
    out: list[Block] = []
    items: list[_Item] = []

    def flush_list() -> None:
        if items:
            out.extend(_nest(items))
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
        listing = _listing(lines)
        if listing is not None:
            out.append(Code(listing))
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


@dataclass
class _Columns:
    """What a page's blocks say about where its columns end: the extent of
    each paragraph, and the box of every block."""

    paragraphs: list[tuple[float, float]]
    blocks: list[tuple[float, float, float, float]]


def _extent(lines: Sequence[Line]) -> tuple[float, float, float, float]:
    return (
        min(line.box.x0 for line in lines),
        min(line.box.y0 for line in lines),
        max(line.box.x1 for line in lines),
        max(line.box.y1 for line in lines),
    )


def _right_edge(lines: list[Line], columns: _Columns) -> float:
    """Where the column this block sits in ends.

    Not where the block itself ends: a two-line signature — `Mit freundlichen
    Grüßen`, then a name — is narrower than the column, and measured against
    itself its first line would look full. The column is that of the
    paragraphs starting where the block starts: a column beside it, or under
    an indented abstract, is another. Nor does it reach past a block set
    beside it: a paragraph beside a fact box ends where the box begins.
    """
    x0, y0, x1, y1 = _extent(lines)
    right = x1
    same_start = 2 * max(line.box.height for line in lines)
    for left, end in columns.paragraphs:
        overlap = min(x1, end) - max(x0, left)
        if overlap > 0.5 * min(x1 - x0, end - left) and abs(left - x0) <= same_start:
            right = max(right, end)
    beside = [
        bx0
        for bx0, by0, _, by1 in columns.blocks
        if bx0 >= x1 - 1 and min(y1, by1) - max(y0, by0) > 0
    ]
    if beside:
        right = max(x1, min(right, min(beside)))
    return right


def _advance(line: Line) -> float | None:
    """A monospaced line's advance per character, or None when it is not
    monospaced or has too few words to tell: from its second word on, its
    words advance by the same width for each character of text between them.
    (The first word's box starts where its text does, the others' halfway
    across the space before them.)"""
    offsets: list[tuple[int, float]] = []
    start = 0
    for word in line.words:
        at = line.text.find(word.text, start)
        if at < 0:
            return None
        offsets.append((at, word.box.x0))
        start = at + len(word.text)
    steps = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(offsets[1:], offsets[2:]) if b[0] > a[0]]
    if len(steps) < 2 or min(steps) <= 0 or max(steps) > min(steps) * 1.03:
        return None
    return sorted(steps)[len(steps) // 2]


def _listing(lines: Sequence[Line]) -> str | None:
    """A block's lines as code, when they are set in a monospaced font: most
    of the lines that can be told are, two at least, and none is running
    text. Each line keeps its indent, counted in characters."""
    told = [_advance(line) for line in lines if len(line.words) >= 4]
    mono = sorted(advance for advance in told if advance is not None)
    if len(mono) < 2 or len(mono) * 4 < len(told) * 3:
        return None
    advance = mono[len(mono) // 2]

    def start(line: Line) -> float:
        return line.words[0].box.x0 if line.words else line.box.x0

    left = min(start(line) for line in lines)
    return "\n".join(
        " " * max(round((start(line) - left) / advance), 0) + line.text.strip() for line in lines
    )


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
    Two lines that each carry their own label — `Kundennummer: K-4711` over
    `Datum: 12.09.2026` — are two fields, however full the first one is.
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
            fields = _LABELLED.match(previous.text.strip()) and _LABELLED.match(text)
            if room > needed or fields:
                content.append(Break())
            elif _glue(previous.text.strip(), text):
                content.append(" ")
        content.append(text)
    return content


@dataclass
class _Item:
    x: float
    ordered: bool
    number: int
    content: list[Inline]


def _items(lines: list[Line], columns: _Columns) -> list[_Item]:
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


def _nest(items: list[_Item]) -> list[Block]:
    """Items as lists, nested by how far each is indented.

    Indents a little apart are one level — a recognized bullet's box moves by a
    few pixels — and every item ends up in a list, whatever level the first
    one sits at.
    """
    step = statistics.median(
        [abs(b.x - a.x) for a, b in zip(items, items[1:]) if abs(b.x - a.x) > 4] or [1e9]
    )
    tolerance = max(step * 0.5, 6.0)
    levels: list[float] = []
    for x in sorted(item.x for item in items):
        if not levels or x - levels[-1] > tolerance:
            levels.append(x)

    def level(x: float) -> int:
        return min(range(len(levels)), key=lambda i: abs(levels[i] - x))

    entries: list[Entry] = []
    last: dict[int, int] = {}
    lists = 0
    for item in items:
        depth = level(item.x)
        if item.ordered and item.number <= last.get(depth, 0):
            # Counting starts again: another list.
            lists += 1
        last[depth] = item.number if item.ordered else 0
        for deeper in [d for d in last if d > depth]:
            del last[deeper]
        entries.append(
            Entry(
                level=depth,
                ordered=item.ordered,
                blocks=[Paragraph(item.content)],
                number=item.number,
                list_id=lists,
            )
        )
    return nest(entries)


def _abbreviation_hyphen(text: str) -> bool:
    """Whether a line ends in an abbreviation in capitals and a hyphen —
    `IT-`, `PDF-`: hyphenation never breaks a word right after a run of
    capitals, so the hyphen is the compound's own."""
    if not text.endswith("-"):
        return False
    word = re.split(r"[\s/-]", text[:-1])[-1]
    return sum(c.isalpha() for c in word) >= 2 and all(c.isupper() or c.isdigit() for c in word)


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
            unspaced = _glue(head, tail) == ""
            if (
                head
                and tail
                and not _ENDS_SENTENCE.search(head)
                and (tail[0].islower() or unspaced)
            ):
                content = list(block.content)
                if unspaced or _abbreviation_hyphen(head):
                    # Chinese or Japanese runs on without a space; `IT-` over
                    # `basierten` keeps the compound's own hyphen.
                    content.extend(blocks[index + 2].content)
                elif head.endswith("-") and len(head) > 1 and head[-2].isalpha():
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
