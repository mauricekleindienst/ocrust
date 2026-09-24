"""What a converted document is before it is written out as Markdown.

Every reader — a scanned page, a Word file, a web page, a mail — turns its input
into these few blocks, and one writer turns them into text. That is what keeps
the output of forty file types consistent: a table from a spreadsheet and a
table from a scanned invoice are the same table by the time they are written,
and escaping is decided in exactly one place.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Union


@dataclass
class Span:
    """Inline markup around other inline content.

    `kind` is ``strong``, ``emph``, ``strike``, ``code`` or ``link``; a link
    carries its target in `url`.
    """

    kind: str
    children: list[Inline] = field(default_factory=list)
    url: str = ""


@dataclass(frozen=True)
class Break:
    """A line break the document set on purpose, inside a paragraph."""


@dataclass(frozen=True)
class FootnoteRef:
    """Where a footnote is referred to; its text is a :class:`Footnote`."""

    label: str


Inline = Union[str, Span, Break, FootnoteRef]


@dataclass
class Heading:
    level: int
    content: list[Inline]


@dataclass
class Paragraph:
    content: list[Inline]


@dataclass
class ListBlock:
    """A list; each item is a list of blocks, the first usually a paragraph."""

    ordered: bool
    items: list[list[Block]]
    #: The number an ordered list starts at: a list the document continues
    #: after a table carries on counting.
    start: int = 1


@dataclass
class Table:
    """Rows of cells, each cell inline content. The first row is the header."""

    rows: list[list[list[Inline]]]


@dataclass
class Code:
    text: str
    language: str = ""


@dataclass
class Quote:
    blocks: list[Block]


@dataclass
class Rule:
    """A thematic break."""


@dataclass
class Marker:
    """An HTML comment: where a page, slide or sheet begins.

    Invisible when rendered, and what lets a chunk of the text be traced back
    to the page it came from.
    """

    text: str


@dataclass
class Image:
    """A picture: where a copy was saved, if one was, and the text read in it."""

    alt: str = ""
    target: str = ""
    blocks: list[Block] = field(default_factory=list)


@dataclass
class Footnote:
    label: str
    blocks: list[Block]


@dataclass
class Raw:
    """Markdown written as it is: a Markdown file inside a mail or an archive."""

    text: str


Block = Union[Heading, Paragraph, ListBlock, Table, Code, Quote, Rule, Marker, Image, Footnote, Raw]


@dataclass
class Note:
    """One converted document: its blocks, what is known about it, its pictures.

    `meta` holds what the source says about itself — title, author, dates,
    a mail's sender — under the property names the front matter uses.
    `assets` maps a file name to the bytes of a picture kept beside the note;
    image blocks refer to them by that name until the store places them.
    """

    blocks: list[Block] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    assets: dict[str, bytes] = field(default_factory=dict)
    #: A Markdown source's own text, written as it is instead of `blocks`.
    raw_body: str | None = None
    #: Its own front matter, verbatim; the note's properties only add to it.
    raw_front: str | None = None


# Characters that never belong in a knowledge base's text: controls, the byte
# order mark, zero-width marks and the soft hyphen, which only says where a word
# may be broken.
# C1 controls (0x80-0x9F) are what Windows-1252 text decoded as Latin-1 looks
# like; no document means them, and YAML refuses them.
_INVISIBLE = dict.fromkeys(
    [
        *range(0x00, 0x09),
        0x0B,
        0x0C,
        *range(0x0E, 0x20),
        *range(0x7F, 0xA0),
        0xAD,
        0x200B,
        0x2060,
        0xFEFF,
    ],
)
# A surrogate on its own is half a character: RTF and JSON escapes can leave
# one behind, and no encoder will write it.
_LONE_SURROGATE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")
_SPACES = re.compile(r"[ \t\n\r\u00a0\u2000-\u200a\u202f\u205f\u3000\u2028\u2029]+")


def clean(text: str) -> str:
    """`text` with its white space collapsed and invisible characters removed.

    A no-break space becomes a space: kept, it splits `5 €` into a token a
    search does not match, and the reader never saw a difference.
    """
    return _SPACES.sub(" ", printable(text))


def printable(text: str) -> str:
    """`text` without control characters and halves of characters, white space
    kept as it is."""
    text = text.translate(_INVISIBLE)
    return _LONE_SURROGATE.sub(chr(0xFFFD), text) if _has_surrogate(text) else text


def _has_surrogate(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def plain(content: list[Inline] | Block | list[Block]) -> str:
    """The text of inline content or blocks, without markup."""
    parts: list[str] = []
    _plain(content, parts)
    return clean("".join(parts)).strip()


def _plain(item: Any, parts: list[str]) -> None:
    if isinstance(item, str):
        parts.append(item)
    elif isinstance(item, Break):
        parts.append(" ")
    elif isinstance(item, FootnoteRef):
        return
    elif isinstance(item, Span):
        for child in item.children:
            _plain(child, parts)
    elif isinstance(item, list):
        for index, child in enumerate(item):
            if index and not isinstance(child, (str, Span, Break, FootnoteRef)):
                parts.append("\n")
            _plain(child, parts)
    elif isinstance(item, (Heading, Paragraph)):
        _plain(item.content, parts)
    elif isinstance(item, ListBlock):
        for entry in item.items:
            _plain(entry, parts)
            parts.append("\n")
    elif isinstance(item, Table):
        for row in item.rows:
            for cell in row:
                _plain(cell, parts)
                parts.append(" ")
            parts.append("\n")
    elif isinstance(item, Code):
        parts.append(item.text)
    elif isinstance(item, (Quote, Footnote, Image)):
        _plain(item.blocks, parts)


def normalize(content: list[Inline]) -> list[Inline]:
    """Inline content with adjacent text joined and empty pieces dropped.

    Two runs of the same markup next to each other become one, which is what
    keeps a Word paragraph whose bold word was typed in three sittings from
    coming out as ``**Ver****trag**``. Linear in the length of the content: a
    paragraph of a million runs is joined once, not a million times.
    """
    grouped: list[Any] = []
    for item in content:
        if isinstance(item, str):
            if not item:
                continue
            if grouped and isinstance(grouped[-1], list):
                grouped[-1].append(item)
            else:
                grouped.append([item])
        elif isinstance(item, Span):
            last = grouped[-1] if grouped else None
            if (
                isinstance(last, Span)
                and last.kind == item.kind
                and last.url == item.url
                and item.kind != "code"
            ):
                last.children.extend(item.children)
            else:
                grouped.append(Span(item.kind, list(item.children), item.url))
        else:
            grouped.append(item)
    out: list[Inline] = []
    for item in grouped:
        if isinstance(item, list):
            out.append("".join(item))
        elif isinstance(item, Span):
            item.children = normalize(item.children)
            if item.children or item.kind == "code":
                out.append(item)
        else:
            out.append(item)
    return out


def strip(content: list[Inline]) -> list[Inline]:
    """Inline content without white space or breaks at either end."""
    items = normalize(content)
    while items and (
        isinstance(items[0], Break) or (isinstance(items[0], str) and not items[0].strip())
    ):
        items.pop(0)
    while items and (
        isinstance(items[-1], Break) or (isinstance(items[-1], str) and not items[-1].strip())
    ):
        items.pop()
    if items and isinstance(items[0], str):
        items[0] = items[0].lstrip()
    if items and isinstance(items[-1], str):
        items[-1] = items[-1].rstrip()
    return items


def is_empty(content: list[Inline]) -> bool:
    return not strip(content) or not any(
        not isinstance(item, Break) and (not isinstance(item, str) or item.strip())
        for item in strip(content)
    )


_SUPERSCRIPT = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")
_SUBSCRIPT = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")


def superscript(text: str) -> str:
    """`m2` set with a raised 2 as `m²`: the character a reader sees, where
    Unicode has one, and the plain text otherwise."""
    raised = text.translate(_SUPERSCRIPT)
    return raised if all(c != o or c.isspace() for c, o in zip(raised, text)) else text


def subscript(text: str) -> str:
    lowered = text.translate(_SUBSCRIPT)
    return lowered if all(c != o or c.isspace() for c, o in zip(lowered, text)) else text


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def flatten(blocks: list[Block]) -> list[Inline]:
    """Blocks as the inline content of one table cell: paragraphs on lines of
    their own, list items with a bullet, a nested table row by row."""
    out: list[Inline] = []

    def line(content: list[Inline]) -> None:
        content = strip(content)
        if not content:
            return
        if out:
            out.append(Break())
        out.extend(content)

    for block in blocks:
        if isinstance(block, (Paragraph, Heading)):
            line(block.content)
        elif isinstance(block, ListBlock):
            for number, item in enumerate(block.items, block.start):
                marker = f"{number}. " if block.ordered else "• "
                line([marker, *flatten(item)])
        elif isinstance(block, Table):
            for row in block.rows:
                cells = [plain(cell) for cell in row]
                line([" | ".join(cell for cell in cells if cell)])
        elif isinstance(block, (Quote, Footnote)):
            line(flatten(block.blocks))
        elif isinstance(block, Image):
            if block.target:
                line([Span("image", [block.alt], block.target)])
            line(flatten(block.blocks) or ([block.alt] if block.alt and not block.target else []))
        elif isinstance(block, (Raw, Code)):
            for text in block.text.splitlines():
                line([text])
    return out


#: Emphasis kinds from the outside in, so that nesting is always the same.
_NESTING = ("strong", "emph", "strike")

Run = tuple[Inline, frozenset, str]


def assemble(runs: list[Run]) -> list[Inline]:
    """Runs of text, each with its formatting and link, as inline markup.

    White space between two runs takes the formatting they share, so a bold
    phrase typed as three bold words stays one bold phrase.
    """
    runs = list(runs)
    for index, (item, fmt, link) in enumerate(runs):
        if isinstance(item, str) and not item.strip() and fmt:
            before = runs[index - 1][1] if index else frozenset()
            after = runs[index + 1][1] if index + 1 < len(runs) else frozenset()
            runs[index] = (item, fmt & before & after, link)
        elif isinstance(item, str) and not item.strip():
            before = runs[index - 1] if index else None
            after = runs[index + 1] if index + 1 < len(runs) else None
            if before and after and before[2] == after[2] == link:
                runs[index] = (item, before[1] & after[1], link)
    out: list[Inline] = []
    for item, fmt, link in runs:
        wrapped: Inline = item
        for kind in reversed(_NESTING):
            if kind in fmt:
                wrapped = Span(kind, [wrapped])
        if link:
            wrapped = Span("link", [wrapped], link)
        out.append(wrapped)
    return normalize(out)


def body_of(note: Note) -> list[Block]:
    """A note's content as blocks, a Markdown source's own text included."""
    if note.raw_body is not None:
        return [Raw(note.raw_body)] if note.raw_body.strip() else []
    return list(note.blocks)


def relabel(blocks: list[Block], prefix: str) -> list[Block]:
    """Blocks with every footnote label prefixed, so that two attachments'
    footnotes `1` stay two footnotes in the note that holds both."""

    def inline(items: list[Inline]) -> list[Inline]:
        out: list[Inline] = []
        for item in items:
            if isinstance(item, FootnoteRef):
                out.append(FootnoteRef(f"{prefix}{item.label}"))
            elif isinstance(item, Span):
                out.append(Span(item.kind, inline(item.children), item.url))
            else:
                out.append(item)
        return out

    def block(item: Block) -> Block:
        if isinstance(item, (Heading, Paragraph)):
            return type(item)(
                *([item.level] if isinstance(item, Heading) else []), inline(item.content)
            )
        if isinstance(item, ListBlock):
            return ListBlock(
                item.ordered, [relabel(entry, prefix) for entry in item.items], item.start
            )
        if isinstance(item, Table):
            return Table([[inline(cell) for cell in row] for row in item.rows])
        if isinstance(item, Quote):
            return Quote(relabel(item.blocks, prefix))
        if isinstance(item, Footnote):
            return Footnote(f"{prefix}{item.label}", relabel(item.blocks, prefix))
        if isinstance(item, Image):
            return Image(item.alt, item.target, relabel(item.blocks, prefix))
        return item

    return [block(item) for item in blocks]


@dataclass
class Entry:
    """One list paragraph, before the lists are built: how deep it sits, what
    kind of list it belongs to, and its number where the document says."""

    level: float
    ordered: bool
    blocks: list[Block]
    number: int | None = None
    #: Which list it belongs to: a paragraph of another list at the same level
    #: starts a list of its own.
    list_id: object = None


def nest(entries: list[Entry]) -> list[Block]:
    """List paragraphs as nested lists, losing none of them.

    A deeper paragraph nests under the item before it; a shallower one closes
    the lists it leaves; one of another list, or another kind, at the same
    level starts a new list. A list that starts deeper than it goes on is
    still read whole.
    """
    roots: list[Block] = []
    stack: list[tuple[float, ListBlock, object]] = []
    for entry in entries:
        while stack and entry.level < stack[-1][0]:
            stack.pop()
        top = stack[-1] if stack else None
        if (
            top is not None
            and top[0] == entry.level
            and top[1].ordered == entry.ordered
            and top[2] == entry.list_id
        ):
            top[1].items.append(list(entry.blocks))
            continue
        if top is not None and top[0] == entry.level:
            stack.pop()
            top = stack[-1] if stack else None
        fresh = ListBlock(
            ordered=entry.ordered,
            items=[list(entry.blocks)],
            start=entry.number if entry.number is not None else 1,
        )
        if top is not None and top[1].items:
            top[1].items[-1].append(fresh)
        else:
            roots.append(fresh)
        stack.append((entry.level, fresh, entry.list_id))
    return roots
