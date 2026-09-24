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


Block = Union[Heading, Paragraph, ListBlock, Table, Code, Quote, Rule, Marker, Image, Footnote]


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
_INVISIBLE = dict.fromkeys(
    [*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F, 0xAD, 0x200B, 0x2060, 0xFEFF],
)
_SPACES = re.compile(r"[ \t\n\r\u00a0\u2000-\u200a\u202f\u205f\u3000\u2028\u2029]+")


def clean(text: str) -> str:
    """`text` with its white space collapsed and invisible characters removed.

    A no-break space becomes a space: kept, it splits `5 €` into a token a
    search does not match, and the reader never saw a difference.
    """
    return _SPACES.sub(" ", text.translate(_INVISIBLE))


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
    coming out as ``**Ver****trag**``.
    """
    out: list[Inline] = []
    for item in content:
        if isinstance(item, Span):
            item = Span(item.kind, normalize(item.children), item.url)
            if not item.children and item.kind != "code":
                continue
            last = out[-1] if out else None
            if (
                isinstance(last, Span)
                and last.kind == item.kind
                and last.url == item.url
                and item.kind != "code"
            ):
                last.children = normalize([*last.children, *item.children])
                continue
        elif isinstance(item, str):
            if not item:
                continue
            if out and isinstance(out[-1], str):
                out[-1] += item
                continue
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
            line(flatten(block.blocks) or ([block.alt] if block.alt else []))
        elif isinstance(block, Code):
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
