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
    spots: list[_Spot | None] = []
    items: list[_Item] = []

    def flush_list() -> None:
        if items:
            nested = _nest(items)
            out.extend(nested)
            spots.extend([None] * len(nested))
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
                spots.append(_Spot(_extent(lines)))
                continue
        if block.kind == "heading":
            text = " ".join(line.text.strip() for line in lines)
            level = levels.get(round(lines[0].box.height, 1), 2)
            out.append(Heading(level, [text]))
            spots.append(None)
            continue
        listing = _listing(lines)
        if listing is not None:
            out.append(Code(listing))
            spots.append(None)
            continue
        if _right_to_left(lines):
            out.append(Paragraph(_joined(lines, _left_edge(lines, columns), rtl=True)))
            spots.append(_Spot(_extent(lines)))
        else:
            right = _right_edge(lines, columns)
            for part in _indented_paragraphs(lines, right):
                out.append(Paragraph(_joined(part, right)))
                spots.append(_Spot(_extent(part), part[0], part[-1], right, _leading(part)))
    flush_list()
    return _join_across_columns(out, spots)


def _indented_paragraphs(lines: list[Line], right: float) -> list[list[Line]]:
    """A block's lines cut where a paragraph set without space around it
    begins: at a line indented by an em or a few, after one that ends its
    sentence, filling the column as the lines after it do from the left
    edge again — a book's, a manuscript's or a paper's paragraphs."""
    height = statistics.median(line.box.height for line in lines)
    left = min(line.box.x0 for line in lines)
    flush = [abs(line.box.x0 - left) <= _FLUSH * height for line in lines]
    if len(lines) < 3 or sum(flush) * 5 < len(lines) * 3:
        return [lines]
    parts: list[list[Line]] = [[lines[0]]]
    for index in range(1, len(lines)):
        line = lines[index]
        indent = line.box.x0 - left
        if (
            _INDENT_MIN * height <= indent <= _INDENT_MAX * height
            and _ENDS_SENTENCE.search(lines[index - 1].text.strip())
            and index + 1 < len(lines)
            and flush[index + 1]
            and right - line.box.x1 <= _first_word_width(lines[index + 1]) + 0.35 * height
        ):
            parts.append([])
        parts[-1].append(line)
    return parts


#: How far a paragraph's first line is indented, in line heights, at least
#: and at most.
_INDENT_MIN = 0.8
_INDENT_MAX = 6.0


@dataclass
class _Spot:
    """Where a block sat on its page: its extent, and for a paragraph set
    left to right its first and last line, where its column ends, how far
    apart its lines stand, and whether a paragraph goes on right under it."""

    extent: tuple[float, float, float, float]
    first: Line | None = None
    last: Line | None = None
    right: float = 0.0
    leading: float | None = None
    under: bool = False


def _leading(lines: Sequence[Line]) -> float | None:
    """How far apart a paragraph's lines stand, or None for one line."""
    gaps = [lower.box.y0 - upper.box.y1 for upper, lower in zip(lines, lines[1:])]
    return statistics.median(gaps) if gaps else None


def _join_across_columns(blocks: list[Block], spots: list[_Spot | None]) -> list[Block]:
    """Joins a paragraph a column break or a box set beside it cut in two.

    As over a page break, the first half ends without closing its sentence
    and the second starts in lower case; and the first half's last line is
    full — the next word would not have fitted. The second half either goes
    on right under the first, past a box set beside it — a fact box, a
    sidebar — which then follows the paragraph, or, when nothing does, heads
    the next column, starting higher up to the right of the first.
    """
    found = [spot for spot in spots if spot is not None]
    for spot in found:
        if spot.last is not None:
            spot.under = any(_right_under(spot, other) for other in found if other is not spot)
    out: list[Block] = []
    placed: list[_Spot | None] = []
    for block, spot in zip(blocks, spots, strict=True):
        if (
            placed
            and isinstance(block, Paragraph)
            and spot is not None
            and spot.first
            and _carried_on(out, placed, block, spot, found)
        ):
            continue
        out.append(block)
        placed.append(spot)
    return out


def _carried_on(
    out: list[Block],
    placed: list[_Spot | None],
    tail: Paragraph,
    spot: _Spot,
    page: list[_Spot],
) -> bool:
    """Joins `tail` onto the paragraph among the blocks already placed that
    it carries on, if one does; the boxes set beside that paragraph then
    follow it. `page` is where every block on the page sat."""
    at = len(out) - 1
    while True:
        head, head_spot = out[at], placed[at]
        if head_spot is None or not isinstance(head, (Paragraph, Table)):
            return False
        boxes = [where for where in placed[at + 1 :] if where is not None]
        if (
            isinstance(head, Paragraph)
            and head_spot.last is not None
            and len(boxes) == len(out) - at - 1
            and _carries_on(head_spot, spot, boxes, page)
        ):
            break
        at -= 1
        if at < 0 or len(out) - at > _BOXES_BESIDE:
            return False
    # A paragraph of several lines run on without a break, down to a full
    # last line, goes on whatever the next word begins with; so does one
    # that goes on right under itself past a box. And lines that go on under
    # a full line, a line's spacing down and not indented, are one paragraph
    # whatever it ends with, as they would be with no box beside them.
    unbroken = not any(isinstance(part, Break) for part in head.content)
    running = unbroken and (head_spot.first is not head_spot.last or bool(boxes))
    flush = abs(spot.extent[0] - head_spot.extent[0]) < _FLUSH * head_spot.last.box.height
    joined = _join(head, tail, capital=running, anyway=unbroken and bool(boxes) and flush)
    if joined is None:
        return False
    moved = list(zip(out[at + 1 :], placed[at + 1 :], strict=True))
    del out[at:], placed[at:]
    out.append(joined)
    placed.append(dataclasses.replace(spot, first=head_spot.first))
    for block, where in moved:
        out.append(block)
        placed.append(where)
    return True


#: How many blocks — a box's caption, its table, a note under it — may stand
#: beside a paragraph that goes on under them.
_BOXES_BESIDE = 4
#: How far off the start of the lines over it, in line heights, a line may
#: start and still not be indented.
_FLUSH = 0.3
#: How much further apart or closer, in line heights, two lines may stand
#: than a paragraph's lines do and still be lines of it.
_SAME_LEADING = 0.1
#: How wide, as a share of a paragraph's, a paragraph beside it may be and
#: still be a box set beside the text — wider, it is the next column.
_BOX_WIDTH = 0.75


def _right_under(head: _Spot, other: _Spot) -> bool:
    """Whether `other` starts where `head` does, as far under it as the
    lines of either stand apart: a paragraph's next line, where a paragraph
    of its own stands further off."""
    assert head.last is not None
    leading = head.leading if head.leading is not None else other.leading
    if leading is None:
        return False
    height = head.last.box.height
    hx0, _, _, hy1 = head.extent
    ox0, oy0, _, _ = other.extent
    return abs(oy0 - hy1 - leading) <= _SAME_LEADING * height and abs(ox0 - hx0) < height


def _carries_on(head: _Spot, tail: _Spot, boxes: list[_Spot], page: list[_Spot]) -> bool:
    """Whether `tail` goes on from `head` on the page: right under it past
    the `boxes` set beside it, or, with none, at the head of the next
    column. `head`'s last line is full either way."""
    assert head.last is not None and tail.first is not None
    last, first = head.last.box, tail.first.box
    hx0, _, hx1, _ = head.extent
    tx0 = tail.extent[0]
    height = last.height
    if boxes:
        if not _right_under(head, tail):
            return False
        if not all(box.extent[0] >= hx1 - 1 or box.extent[2] <= hx0 + 1 for box in boxes):
            return False
        # Paragraphs beside it as wide as its own are the next column: the
        # text under both goes on from that.
        if any(
            box.first is not None and box.extent[2] - box.extent[0] >= _BOX_WIDTH * (hx1 - hx0)
            for box in boxes
        ):
            return False
        # The column beside the boxes ends where the paragraphs beside them
        # end, a margin short of the boxes.
        limit = min((box.extent[0] for box in boxes if box.extent[0] >= hx1 - 1), default=hx1)
        edge = max(
            [hx1]
            + [
                other.extent[2]
                for other in page
                if other.last is not None
                and abs(other.extent[0] - hx0) < height
                and other.extent[2] <= limit
            ]
        )
    else:
        if head.under or not (first.y0 < last.y0 and tx0 >= hx1 - 0.5 * height):
            return False
        # The paragraph's own widest line, when it has more than one, says
        # where its column ends.
        edge = hx1 if head.first is not head.last else head.right
    return edge - last.x1 <= _first_word_width(tail.first) + 0.35 * height


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
    an indented abstract, is another. Nor does it reach up to a block set
    beside it: a paragraph beside a fact box ends a margin short of the box,
    where the paragraphs beside the box end.
    """
    x0, y0, x1, y1 = _extent(lines)
    beside = [
        bx0
        for bx0, by0, _, by1 in columns.blocks
        if bx0 >= x1 - 1 and min(y1, by1) - max(y0, by0) > 0
    ]
    limit = min(beside) if beside else float("inf")
    right = x1
    same_start = 2 * max(line.box.height for line in lines)
    for left, end in columns.paragraphs:
        overlap = min(x1, end) - max(x0, left)
        if (
            overlap > 0.5 * min(x1 - x0, end - left)
            and abs(left - x0) <= same_start
            and end <= limit
        ):
            right = max(right, end)
    return right


#: A word of running text: letters, hyphenated or not, with the punctuation
#: and quotes around it.
_WORD = re.compile(r"[(\"'„“‚«»]*[^\W\d_]+(?:[-'’][^\W\d_]+)*[.,;:!?)\"'“”‘’«»]*")
#: Words a line of running text carries at least, as the middle line has it.
_PROSE_WORDS = 5
#: How many of a text's tokens are words, at least.
_PROSE_SHARE = 0.85


def _advance(line: Line) -> float | None:
    """A monospaced line's advance per character, or None when it is not
    monospaced or has too few words to tell: from its second word on, its
    words advance by the same width for each character of text between them.
    (The first word's box starts where its text does, the others' halfway
    across the space before them.) A line mostly in Chinese or Japanese is
    not told: its characters are all one width whatever the font."""
    letters = [c for c in line.text if not c.isspace()]
    if sum(bool(_UNSPACED.match(c)) for c in letters) * 2 >= len(letters):
        return None
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
    """A block's lines as code, when they are set in a monospaced font — most
    of the lines that can be told are, two at least — and read as code: set
    with the symbols code is written in, or indented line by line. A letter
    typed in Courier is text. Each line keeps its indent, in characters."""
    told = [_advance(line) for line in lines if len(line.words) >= 4]
    mono = sorted(advance for advance in told if advance is not None)
    if len(mono) < 2 or len(mono) * 4 < len(told) * 3:
        return None
    advance = mono[len(mono) // 2]

    def start(line: Line) -> float:
        return line.words[0].box.x0 if line.words else line.box.x0

    left = min(start(line) for line in lines)
    if _prose(lines):
        return None
    indents = [max(round((start(line) - left) / advance), 0) for line in lines]
    return "\n".join(" " * i + line.text.strip() for i, line in zip(indents, lines, strict=True))


def _prose(lines: Sequence[Line]) -> bool:
    """Whether monospaced lines are running text — a letter or a manuscript
    typed in Courier — rather than code or commands: their lines carry five
    words and more, and nearly every word is one, where commands are short
    and made of flags, paths and names (`--nginx`, `example.org`)."""
    tokens = [line.text.split() for line in lines]
    counts = sorted(len(t) for t in tokens)
    words = [token for line in tokens for token in line]
    if not words or counts[len(counts) // 2] < _PROSE_WORDS:
        return False
    plain = sum(bool(_WORD.fullmatch(token)) for token in words)
    return plain >= _PROSE_SHARE * len(words)


#: Letters of the scripts written right to left: Hebrew and Arabic.
_RIGHT_TO_LEFT = re.compile("[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufeff]")


def _right_to_left(lines: Sequence[Line]) -> bool:
    """Whether a block is written right to left: more of its letters are
    Hebrew or Arabic than not."""
    text = "".join(line.text for line in lines)
    rights = sum(1 for c in text if c.isalpha() and _RIGHT_TO_LEFT.match(c))
    return rights * 2 > sum(1 for c in text if c.isalpha())


def _left_edge(lines: list[Line], columns: _Columns) -> float:
    """Where the column a block written right to left sits in begins: as
    `_right_edge`, from the other side — its lines end at their left."""
    x0, y0, x1, y1 = _extent(lines)
    left = x0
    same_end = 2 * max(line.box.height for line in lines)
    for start, end in columns.paragraphs:
        overlap = min(x1, end) - max(x0, start)
        if overlap > 0.5 * min(x1 - x0, end - start) and abs(end - x1) <= same_end:
            left = min(left, start)
    beside = [
        bx1
        for _, by0, bx1, by1 in columns.blocks
        if bx1 <= x0 + 1 and min(y1, by1) - max(y0, by0) > 0
    ]
    if beside:
        left = min(x0, max(left, max(beside)))
    return left


def _first_word_width(line: Line) -> float:
    if line.words:
        return line.words[0].box.width
    text = line.text.strip()
    word = text.split(" ", 1)[0] if text else ""
    return line.box.width * len(word) / max(len(text), 1)


def _joined(lines: Sequence[Line], right: float, rtl: bool = False) -> list[Inline]:
    """A paragraph's lines as one run of text, with a line break kept only
    where the line ended although the next line's first word would have fit.

    That is how a line broken on purpose — an address, a list of names, a
    signature — tells itself apart from one the column was simply full at.
    Two lines that each carry their own label — `Kundennummer: K-4711` over
    `Datum: 12.09.2026` — are two fields, however full the first one is.
    Lines written right to left (`rtl`) end at their left, and `right` is
    then the column's left edge.
    """
    content: list[Inline] = []
    for index, line in enumerate(lines):
        text = line.text.strip()
        if not text:
            continue
        if content:
            previous = lines[index - 1]
            if rtl:
                room = previous.box.x0 - right
                first = line.words[-1].box.width if line.words else _first_word_width(line)
            else:
                room = right - previous.box.x1
                first = _first_word_width(line)
            needed = first + 0.35 * previous.box.height
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
    rtl = _right_to_left(lines)
    edge = _left_edge(lines, columns) if rtl else _right_edge(lines, columns)
    for group in groups:
        first = group[0].text
        numbered = _ENUMERATOR.match(first)
        bullet = None if numbered else _BULLET.match(first)
        marker = numbered or bullet
        head = first[marker.end() :] if marker else first
        content = _joined([_with_text(group[0], head), *group[1:]], edge, rtl=rtl)
        items.append(
            _Item(
                # Indented from the side a line starts at: the right, for
                # one written right to left.
                x=-group[0].box.x1 if rtl else group[0].box.x0,
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


#: A word after which a hyphen at a line end was left hanging on purpose:
#: `IT-` / `und TK-Anlagen` (as the layout's `AFTER_SUSPENDED_HYPHEN`).
_SUSPENDED = re.compile(
    r"(?:und|oder|sowie|bis|noch|wie|respektive|and|bzw\.|resp\.|u\.|o\.)(?:\s|$)"
)


def _abbreviation_hyphen(text: str) -> bool:
    """Whether a line ends in an abbreviation in capitals and a hyphen —
    `IT-`, `PDF-`: hyphenation never breaks a word right after a run of
    capitals, so the hyphen is the compound's own."""
    if not text.endswith("-"):
        return False
    word = re.split(r"[\s/-]", text[:-1])[-1]
    return sum(c.isalpha() for c in word) >= 2 and all(c.isupper() or c.isdigit() for c in word)


def _join(
    first: Paragraph, second: Paragraph, *, capital: bool = False, anyway: bool = False
) -> Paragraph | None:
    """The two halves of a paragraph a break cut in two as one, or None when
    they are two: the first half ends without closing its sentence, and the
    second starts in lower case — or, when the page says so otherwise
    (`capital`), with a capital: a noun, in German. When the page says they
    are one `anyway`, they are."""
    head, tail = plain(first.content), plain(second.content)
    unspaced = _glue(head, tail) == ""
    if not (head and tail):
        return None
    if not anyway and _ENDS_SENTENCE.search(head):
        return None
    if not (anyway or tail[0].islower() or unspaced or (capital and tail[0].isalpha())):
        return None
    content = list(first.content)
    if unspaced or (_abbreviation_hyphen(head) and not _SUSPENDED.match(tail)):
        # Chinese or Japanese runs on without a space; `IT-` over
        # `basierten` keeps the compound's own hyphen.
        content.extend(second.content)
    elif head.endswith("-") and len(head) > 1 and head[-2].isalpha():
        # A word hyphenated across the break.
        last = content[-1]
        if isinstance(last, str) and last.endswith("-"):
            content[-1] = last[:-1]
            content.extend(second.content)
        else:
            content.extend([" ", *second.content])
    else:
        content.extend([" ", *second.content])
    return Paragraph(content)


def _join_across_pages(blocks: list[Block]) -> list[Block]:
    """Joins a paragraph a page break cut in two.

    The joined paragraph stands before the marker of the page it ends on, on
    the page it began.
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
            joined = _join(block, blocks[index + 2])
            if joined is not None:
                out.append(joined)
                out.append(blocks[index + 1])
                index += 3
                continue
        out.append(block)
        index += 1
    return out
