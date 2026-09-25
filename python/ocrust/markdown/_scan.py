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

    for block, lines, listing in _with_listings(blocks):
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
        if listing is None:
            listing = _listing([lines])
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
                spots.append(
                    _Spot(_extent(part), part[0], part[-1], right, _leading(part), len(part))
                )
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
    # The lines beside a drop cap or a small picture set at the start of a
    # paragraph start past it, and no paragraph starts there: after a flush
    # first line, the next few lines as long as they are indented.
    beside_cap = 1
    if flush[0]:
        while beside_cap < min(len(lines), _BESIDE_CAP + 1) and not flush[beside_cap]:
            beside_cap += 1
    parts: list[list[Line]] = [[lines[0]]]
    for index in range(1, len(lines)):
        line = lines[index]
        indent = line.box.x0 - left
        if (
            index >= beside_cap
            and _INDENT_MIN * height <= indent <= _INDENT_MAX * height
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

#: How many lines a drop cap stands beside, at most.
_BESIDE_CAP = 3


@dataclass
class _Spot:
    """Where a block sat on its page: its extent, and for a paragraph set
    left to right its first and last line, how many lines it has, where its
    column ends, how far apart its lines stand, and whether a paragraph goes
    on right under it."""

    extent: tuple[float, float, float, float]
    first: Line | None = None
    last: Line | None = None
    right: float = 0.0
    leading: float | None = None
    count: int = 0
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
    the next column, starting higher up to the right of the first, perhaps
    under a figure or a table set at the column's head. A column goes on
    only from one of its own kind: as wide, in the same size and language,
    and long enough to be a column of running text — not a sidebar beside
    it, the other language of a page in two, or a slide's second box.
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
    it carries on, if one does; the boxes set beside that paragraph, or at
    the head of the column it goes on in, then follow it. `page` is where
    every block on the page sat."""
    at = len(out) - 1
    while True:
        head, head_spot = out[at], placed[at]
        if head_spot is None or not isinstance(head, (Paragraph, Table)):
            return False
        boxes = [where for where in placed[at + 1 :] if where is not None]
        way = (
            _carries_on(head_spot, spot, boxes, page)
            if isinstance(head, Paragraph)
            and head_spot.last is not None
            and len(boxes) == len(out) - at - 1
            else None
        )
        if way is not None:
            break
        at -= 1
        if at < 0 or len(out) - at > _BOXES_BESIDE:
            return False
    if way == "column" and not _one_flow(head, head_spot, tail, spot, page):
        return False
    # A paragraph of several lines run on without a break, down to a full
    # last line, goes on whatever the next word begins with; so does one
    # that goes on right under itself past a box. And lines that go on under
    # a full line, a line's spacing down and not indented, are one paragraph
    # whatever it ends with, as they would be with no box beside them.
    unbroken = not any(isinstance(part, Break) for part in head.content)
    under = way == "under"
    running = unbroken and (head_spot.first is not head_spot.last or under)
    flush = abs(spot.extent[0] - head_spot.extent[0]) < _FLUSH * head_spot.last.box.height
    joined = _join(head, tail, capital=running, anyway=unbroken and under and flush)
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


def _one_flow(
    head: Paragraph, head_spot: _Spot, tail: Paragraph, tail_spot: _Spot, page: list[_Spot]
) -> bool:
    """Whether the column `tail` heads is the one `head` ends going on: no
    caption at either end, columns as wide and set as large, the same
    language, and a column of running text long enough to be one."""
    assert head_spot.last is not None and tail_spot.first is not None
    if _CAPTION.match(plain(head.content)) or _CAPTION.match(plain(tail.content)):
        return False
    height = head_spot.last.box.height
    if abs(tail_spot.first.box.height - height) > _SAME_SIZE * height:
        return False
    hx0, tx0 = head_spot.extent[0], tail_spot.extent[0]
    head_width, tail_width = head_spot.right - hx0, tail_spot.right - tx0
    if abs(head_width - tail_width) > _SAME_WIDTH * max(head_width, tail_width):
        return False
    column = sum(
        other.count
        for other in page
        if other.last is not None
        and abs(other.extent[0] - hx0) < height
        and other.extent[2] <= head_spot.right + height
    )
    if column < _COLUMN_LINES:
        return False
    languages = _language(plain(head.content)), _language(plain(tail.content))
    return None in languages or languages[0] == languages[1]


#: A figure's or a table's caption: `Figure 3:`, `Abb. 2`, `TABLE I.`.
_CAPTION = re.compile(
    r"^\s*(?:fig(?:ure|\.)?|abb(?:ildung|\.)?|tab(?:le|elle|\.)?|bild|grafik|"
    r"diagramm|chart|foto|photo|karte|listing|algorithm(?:us)?)\s*"
    r"(?:\d+(?:[.-]\d+)*|[ivxlc]+)\s*[:.)–—-]",
    re.IGNORECASE,
)
#: How much two columns of one text may differ in width, as a share of the
#: wider, and in line height, as a share of the first's.
_SAME_WIDTH = 0.15
_SAME_SIZE = 0.1
#: How many lines a column of running text has at least.
_COLUMN_LINES = 6
#: The commonest short words of the languages a page may be set in two of.
_STOPWORDS = {
    language: frozenset(words.split())
    for language, words in {
        "de": "der die das und ist nicht mit von zu den im für auf dem des sich ein eine "
        "auch als werden wird sind wurde bei nach oder über aus um",
        "en": "the and of to is are was for on with that this by from be as at it which "
        "have has not were been their or",
        "fr": "le la les et des du un une est dans pour que qui sur au aux par pas ce il "
        "elle sont avec",
        "es": "el la los las y del que en un una es por con para se su al lo como más",
        "it": "il lo la gli le e di che del della un una è per con non sono nel alla",
        "nl": "het een en van is dat op te voor met zijn niet aan er ook als bij",
    }.items()
}


def _language(text: str) -> str | None:
    """The language a text is in, by its commonest short words, when they
    say so clearly: three at least, half again as many as any other's."""
    words = re.findall(r"[^\W\d_]+", text.lower())
    counts = sorted(
        ((sum(word in stop for word in words), name) for name, stop in _STOPWORDS.items()),
        reverse=True,
    )
    (best, name), (second, _) = counts[0], counts[1]
    return name if best >= 3 and best >= 1.5 * second else None


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


def _carries_on(head: _Spot, tail: _Spot, boxes: list[_Spot], page: list[_Spot]) -> str | None:
    """How `tail` goes on from `head` on the page, if it does: `"under"` it,
    past the `boxes` set beside it, or at the head of the next `"column"`,
    under the `boxes` set there if any. `head`'s last line is full either
    way."""
    assert head.last is not None and tail.first is not None
    last, first = head.last.box, tail.first.box
    hx0, _, hx1, _ = head.extent
    tx0, ty0, _, _ = tail.extent
    height = last.height
    if boxes and _right_under(head, tail):
        if not all(box.extent[0] >= hx1 - 1 or box.extent[2] <= hx0 + 1 for box in boxes):
            return None
        # Paragraphs beside it as wide as its own are the next column: the
        # text under both goes on from that.
        if any(
            box.first is not None and box.extent[2] - box.extent[0] >= _BOX_WIDTH * (hx1 - hx0)
            for box in boxes
        ):
            return None
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
        way = "under"
    else:
        # A figure or a table at the head of the next column stands over
        # the text that goes on there.
        headed = all(
            box.extent[0] >= hx1 - 0.5 * height and box.extent[3] <= ty0 + 0.5 * height
            for box in boxes
        )
        if not headed or head.under or not (first.y0 < last.y0 and tx0 >= hx1 - 0.5 * height):
            return None
        # The paragraph's own widest line, when it has more than one, says
        # where its column ends.
        edge = hx1 if head.first is not head.last else head.right
        way = "column"
    full = edge - last.x1 <= _first_word_width(tail.first) + 0.35 * height
    return way if full else None


def _column_edges(blocks: list[tuple[ScanBlock, list[Line]]]) -> list[tuple[float, float, bool]]:
    """The horizontal extent of each paragraph on the page, and whether its
    lines wrap: running text, two lines at least, all but the last reaching
    near its end — not an address's few words a line."""
    edges = []
    for _, lines in blocks:
        x0 = min(line.box.x0 for line in lines)
        x1 = max(line.box.x1 for line in lines)
        reach = x1 - _WRAPPED_SLACK * (x1 - x0)
        wrapped = (
            len(lines) >= 2 and all(line.box.x1 >= reach for line in lines[:-1]) and _prose(lines)
        )
        edges.append((x0, x1, wrapped))
    return edges


#: How far short of a paragraph's end, as a share of its width, a line may
#: end and still have wrapped there.
_WRAPPED_SLACK = 0.15


@dataclass
class _Columns:
    """What a page's blocks say about where its columns end: the extent of
    each paragraph and whether its lines wrap, and the box of every block."""

    paragraphs: list[tuple[float, float, bool]]
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
    beside it: a paragraph beside a fact box ends a margin short of the box,
    where the paragraphs whose lines wrap beside the box end — or, with none
    to tell, where the box begins: a letter's address beside its reference
    block is no fuller for a salutation that ends short of the block.
    """
    x0, y0, x1, y1 = _extent(lines)
    beside = [
        bx0
        for bx0, by0, _, by1 in columns.blocks
        if bx0 >= x1 - 1 and min(y1, by1) - max(y0, by0) > 0
    ]
    right = x1
    wrapped_beside = 0.0
    same_start = 2 * max(line.box.height for line in lines)
    for left, end, wrapped in columns.paragraphs:
        overlap = min(x1, end) - max(x0, left)
        if overlap > 0.5 * min(x1 - x0, end - left) and abs(left - x0) <= same_start:
            right = max(right, end)
            if beside and wrapped and end <= min(beside):
                wrapped_beside = max(wrapped_beside, end)
    if beside:
        if wrapped_beside:
            return max(x1, wrapped_beside)
        return max(x1, min(right, min(beside)))
    return right


#: A word of running text: letters, hyphenated or not, with the punctuation
#: and quotes around it.
_WORD = re.compile(r"[(\"'„“‚«»]*[^\W\d_]+(?:[-'’][^\W\d_]+)*[.,;:!?)\"'“”‘’«»]*")
#: Words a line of running text carries at least, as the middle line has it.
_PROSE_WORDS = 5
#: How many of a text's tokens are words, at least.
_PROSE_SHARE = 0.85


def _advances(line: Line) -> list[float]:
    """What a line says of its font's advance per character, were it
    monospaced: from its second word on, each word that has letters, with
    the space after it, over its characters (the first word's box starts
    where its text does, the others' halfway across the space before them);
    and, for a line whose letters and figures are mostly letters, its width
    over its characters.
    Figures say nothing: a text font sets them all one width too. A line
    mostly in Chinese or Japanese says nothing either: its characters are
    all one width whatever the font."""
    text = line.text.strip()
    letters = [c for c in text if not c.isspace()]
    if not letters or sum(bool(_UNSPACED.match(c)) for c in letters) * 2 >= len(letters):
        return []
    offsets: list[tuple[int, float, str]] = []
    start = 0
    for word in line.words:
        at = line.text.find(word.text, start)
        if at < 0:
            return []
        offsets.append((at, word.box.x0, word.text))
        start = at + len(word.text)
    out = [
        (b[1] - a[1]) / (b[0] - a[0])
        for a, b in zip(offsets[1:], offsets[2:])
        if b[0] > a[0] and any(c.isalpha() for c in a[2])
    ]
    alphabetic = sum(c.isalpha() for c in letters)
    figures = sum(c.isdigit() for c in letters)
    if len(text) >= _PITCH_CHARS and alphabetic >= _PITCH_LETTERS * (alphabetic + figures):
        # From where its text starts: a box may take in the spaces before.
        begin = line.words[0].box.x0 if line.words else line.box.x0
        out.append((line.box.x1 - begin) / len(text))
    return out


#: How long a line is at least, and how much of it letters, for its width
#: over its characters to tell its font's advance.
_PITCH_CHARS = 4
_PITCH_LETTERS = 0.6


def _told(lines: Sequence[Line]) -> list[float]:
    return sorted(advance for line in lines for advance in _advances(line))


def _agree(told: list[float]) -> bool:
    """Whether what lines say of their font's advance per character agrees:
    nearly all of it to within a sixtieth."""
    if not told:
        return True
    advance = told[len(told) // 2]
    close = sum(abs(t - advance) <= _MONO_SPREAD * advance for t in told)
    return advance > 0 and close >= _MONO_AGREE * len(told)


def _monospaced(lines: Sequence[Line]) -> float | None:
    """The advance per character of lines set in a monospaced font, or None:
    what they say of it — four things at least — agrees. A text font's
    words and lines are as wide as their letters make them, never the same
    per character."""
    told = _told(lines)
    if len(told) < _MONO_SAMPLES or not _agree(told):
        return None
    return told[len(told) // 2]


_MONO_SAMPLES = 4
_MONO_SPREAD = 0.015
_MONO_AGREE = 0.9


def _listing(parts: list[list[Line]]) -> str | None:
    """A block's lines — or the parts of one listing, a blank line between
    them — as code, when they are set in a monospaced font and are not
    running text: a letter typed in Courier is text. Each line keeps its
    indent, in characters."""
    lines = [line for part in parts for line in part]
    advance = _monospaced(lines)
    if advance is None:
        return _structured(lines) if len(parts) == 1 else None
    if _prose(lines):
        return None
    left = min(_start(line) for line in lines)
    indents = [max(round((_start(line) - left) / advance), 0) for line in lines]
    if not _code_like(lines, indents):
        return None
    lines_of = iter(zip(indents, lines, strict=True))
    return "\n\n".join(
        "\n".join(
            " " * indent + line.text.strip() for indent, line in (next(lines_of) for _ in part)
        )
        for part in parts
    )


def _code_like(lines: Sequence[Line], indents: Sequence[int]) -> bool:
    """Whether monospaced lines read as code rather than a typed letter's
    address, date or subject: set with the symbols code is written in,
    indented line by line, commands a line, or a configuration's keys."""
    chars = [c for line in lines for c in line.text if not c.isspace()]
    texts = [line.text.strip() for line in lines]
    symbols = sum(c in _CODE_SYMBOLS for c in chars)
    return (
        symbols >= _CODE_SYMBOL_SHARE * len(chars)
        or sum(indent >= 2 for indent in indents) >= 2
        or sum(bool(_COMMAND.match(text)) for text in texts) * 2 >= len(texts)
        or sum(bool(_CONFIG_KEY.match(text)) for text in texts) * 2 >= len(texts)
    )


_CODE_SYMBOLS = frozenset("(){}[]=;<>_\\|#$@*&%+`~^")
#: How many of a block's characters have to be such symbols, at least.
_CODE_SYMBOL_SHARE = 0.04
#: A configuration's key, as YAML and the like write it: `image: node:20`.
_CONFIG_KEY = re.compile(r"(?:- )?[a-z_][\w.-]*:(?:\s|$)")


def _structured(lines: Sequence[Line]) -> str | None:
    """A block's lines as code when they are written as code is, whatever
    font they are set in: JSON — braces or brackets around it, a key and its
    value a line — indented by how deep each line stands; an INI file's
    sections and settings; or shell commands, a command a line."""
    texts = [line.text.strip() for line in lines]
    if len(texts) >= 2 and all(_COMMAND.match(text) for text in texts):
        return "\n".join(texts)
    if any(_INI_SECTION.fullmatch(text) for text in texts) and all(
        _INI_SECTION.fullmatch(text) or _INI_SETTING.fullmatch(text) for text in texts
    ):
        return "\n".join(texts)
    if len(texts) < 3 or not texts[0].endswith(("{", "[")) or texts[-1].rstrip(",") not in "}]":
        return None
    if not all(_JSON_LINE.fullmatch(text) for text in texts):
        return None
    out, depth = [], 0
    for text in texts:
        if text[0] in "}]":
            depth = max(depth - 1, 0)
        out.append("  " * depth + text)
        if text.endswith(("{", "[")):
            depth += 1
    return "\n".join(out) if depth == 0 else None


#: A line of JSON: a key and its value, a value, or a brace or bracket.
_JSON_LINE = re.compile(
    r'(?:"[^"]*"\s*:\s*)?(?:"[^"]*"|-?\d[\d.eE+-]*|true|false|null|[{\[])?\s*[}\]]?,?'
)
#: An INI file's section, and a setting in it.
_INI_SECTION = re.compile(r"\[[\w .:-]+\]")
_INI_SETTING = re.compile(r"[\w.-]+\s*=\s*\S.*")
#: A shell command: a prompt perhaps, then a command line tools are run by.
_COMMAND = re.compile(
    r"(?:[$#>]\s+)?(?:sudo\s+)?(?:apt(?:-get)?|dnf|yum|pacman|brew|pip3?|pipx|npm|npx|yarn|"
    r"pnpm|cargo|go|git|docker|kubectl|systemctl|journalctl|service|curl|wget|ssh|scp|"
    r"rsync|chmod|chown|mkdir|export|make|cmake|python3?|node|conda|helm|terraform|cd|ls|"
    r"cp|mv|rm|cat|echo|tar|source|FROM|RUN|CMD|COPY|ADD|WORKDIR|ENV|EXPOSE|ENTRYPOINT|"
    r"ARG|USER|VOLUME|LABEL)\s+\S"
)


def _start(line: Line) -> float:
    return line.words[0].box.x0 if line.words else line.box.x0


def _with_listings(
    blocks: list[tuple[ScanBlock, list[Line]]],
) -> list[tuple[ScanBlock, list[Line], str | None]]:
    """The page's blocks, the parts of a code listing that its blank lines
    cut into blocks of their own taken together as one listing: paragraphs
    one under the other, a blank line or two apart, set as tall, none
    starting left of the first, and all in one monospaced font. Alone, a
    part of a few short lines may say too little of its font."""
    out: list[tuple[ScanBlock, list[Line], str | None]] = []
    index = 0
    while index < len(blocks):
        block, lines = blocks[index]
        run = [lines]
        if block.kind == "paragraph":
            height = statistics.median(line.box.height for line in lines)
            left = min(_start(line) for line in lines)
            after = index + 1
            while after < len(blocks) and blocks[after][0].kind == "paragraph":
                more = blocks[after][1]
                gap = _extent(more)[1] - _extent(run[-1])[3]
                if (
                    not 0 <= gap <= _LISTING_GAP * height
                    or abs(statistics.median(line.box.height for line in more) - height)
                    > 0.05 * height
                    or min(_start(line) for line in more) < left - 0.5 * height
                    or not _agree(_told([line for part in [*run, more] for line in part]))
                ):
                    break
                run.append(more)
                after += 1
            if len(run) > 1:
                listing = _listing(run)
                if listing is not None:
                    out.append((block, [line for part in run for line in part], listing))
                    index = after
                    continue
        out.append((block, lines, None))
        index += 1
    return out


#: How far under a listing's part, in line heights, its next part may go on.
_LISTING_GAP = 2.5


def _prose(lines: Sequence[Line]) -> bool:
    """Whether lines are running text — a letter or a manuscript typed in
    Courier, a paragraph — rather than code or commands: their lines carry
    five words and more, nearly every word is one, and they are written in
    sentences — the little words of a language between them, or commas and
    full stops — where commands are made of names, flags and paths
    (`apt install nginx redis`, `--nginx`, `example.org`)."""
    tokens = [line.text.split() for line in lines]
    counts = sorted(len(t) for t in tokens)
    words = [token for line in tokens for token in line]
    if not words or counts[len(counts) // 2] < _PROSE_WORDS:
        return False
    plain = sum(bool(_WORD.fullmatch(token)) for token in words)
    if plain < _PROSE_SHARE * len(words):
        return False
    little = sum(
        any(token.lower().strip(".,;:!?\"'()") in stop for stop in _STOPWORDS.values())
        for token in words
    )
    stops = sum(token[-1] in ".,;:!?" for token in words)
    return little >= _PROSE_LITTLE * len(words) or stops >= len(lines) / 2


#: How many of running text's words are a language's little words, at least.
_PROSE_LITTLE = 0.1


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
    for start, end, _ in columns.paragraphs:
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
