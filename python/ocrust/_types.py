"""Result objects returned by a scan.

These are plain dataclasses built from the engine's JSON, so they pickle,
compare and print without surprises.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Box", "Word", "Line", "Block", "Match", "Page", "Document"]


@dataclass(frozen=True)
class Box:
    """An axis-aligned box in page pixels."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> Box:
        return cls(data["x0"], data["y0"], data["x1"], data["y1"])


@dataclass(frozen=True)
class Word:
    text: str
    box: Box
    confidence: float

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> Word:
        return cls(data["text"], Box._from_json(data["bbox"]), data["confidence"])


@dataclass(frozen=True)
class Line:
    text: str
    box: Box
    confidence: float
    angle: float
    #: Mean distance between the chosen character and the runner-up, which is
    #: what tells a read apart from a guess once the softmax has saturated.
    margin: float = 0.0
    words: Sequence[Word] = field(default_factory=tuple)
    #: Corner points of the detection polygon, clockwise from the top left.
    polygon: Sequence[tuple[float, float]] = field(default_factory=tuple)

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> Line:
        return cls(
            text=data["text"],
            box=Box._from_json(data["bbox"]),
            confidence=data["confidence"],
            angle=data.get("angle", 0.0),
            margin=data.get("margin", 0.0),
            words=tuple(Word._from_json(w) for w in data.get("words", ())),
            polygon=tuple((p["x"], p["y"]) for p in data["quad"]["points"]),
        )


@dataclass(frozen=True)
class Block:
    """A paragraph, heading or list item."""

    kind: str
    box: Box
    lines: Sequence[Line]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> Block:
        return cls(
            kind=data["kind"],
            box=Box._from_json(data["bbox"]),
            lines=tuple(Line._from_json(item) for item in data["lines"]),
        )


@dataclass(frozen=True)
class Match:
    """Where a search hit sits in a document."""

    text: str
    #: The page's own :attr:`Page.index`, so a hit can be reported against the
    #: source document even when only some of its pages were scanned.
    page: int
    #: Box around the matching words, or the whole line when word boxes are off.
    box: Box
    #: The line the match was found in.
    line: Line

    def as_tuple(self) -> tuple[int, float, float, float, float]:
        return (self.page, *self.box.as_tuple())


@dataclass(frozen=True)
class Page:
    index: int
    width: int
    height: int
    rotation: float
    origin: str
    blocks: Sequence[Block]
    elapsed_ms: float
    #: Estimated share of this page's characters that are right, or ``None``
    #: for a page with no text. See :attr:`Document.quality`.
    quality: float | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks)

    @property
    def lines(self) -> tuple[Line, ...]:
        return tuple(line for block in self.blocks for line in block.lines)

    @property
    def confidence(self) -> float | None:
        lines = self.lines
        if not lines:
            return None
        return sum(line.confidence for line in lines) / len(lines)

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> Page:
        return cls(
            index=data["index"],
            width=data["width"],
            height=data["height"],
            rotation=data.get("rotation", 0.0),
            origin=data.get("origin", "image"),
            blocks=tuple(Block._from_json(b) for b in data["blocks"]),
            elapsed_ms=data.get("elapsed_ms", 0.0),
            quality=data.get("quality"),
        )


@dataclass(frozen=True)
class Document:
    """The result of scanning one file."""

    source: str
    pages: Sequence[Page]
    elapsed_ms: float
    #: The engine's own JSON, kept so that :meth:`to_dict` can hand back every
    #: field the dataclasses leave out. Empty for a document built by hand.
    _raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __str__(self) -> str:
        return self.text

    def __iter__(self) -> Iterator[Page]:
        return iter(self.pages)

    def __len__(self) -> int:
        return len(self.pages)

    @property
    def text(self) -> str:
        """All pages, separated by a form feed."""
        return "\n\f\n".join(page.text for page in self.pages)

    @property
    def lines(self) -> tuple[Line, ...]:
        return tuple(line for page in self.pages for line in page.lines)

    @property
    def words(self) -> tuple[Word, ...]:
        return tuple(word for line in self.lines for word in line.words)

    @property
    def confidence(self) -> float | None:
        """Mean line confidence: how sure the recognizer was of its characters.

        Good at what it measures and poor at what people read into it — see
        :attr:`quality` for an estimate of how much of the document is right.
        """
        lines = self.lines
        if not lines:
            return None
        return sum(line.confidence for line in lines) / len(lines)

    @property
    def quality(self) -> float | None:
        """Estimated share of the document's characters that are right.

        ``None`` when no page carried text. Confidence answers a narrower
        question: it is the recognizer's certainty about the characters it
        emitted, and it cannot see what never reached it — text the detector
        missed, a column read out of order, a label broken into fragments. Over
        the evaluation corpus this estimate ranks pages at Spearman +0.75
        against +0.50 for confidence. It is an estimate, not a guarantee.

        >>> doc = ocrust.scan("scan.pdf")            # doctest: +SKIP
        >>> if doc.quality and doc.quality < 0.95:   # doctest: +SKIP
        ...     print("worth a human look")
        """
        weighted = 0.0
        lines = 0
        for page in self.pages:
            if page.quality is None:
                continue
            count = max(len(page.lines), 1)
            weighted += page.quality * count
            lines += count
        return weighted / lines if lines else None

    def search(
        self,
        needle: str,
        *,
        regex: bool = False,
        case: bool = False,
        whole_words: bool = False,
    ) -> tuple[Match, ...]:
        """Finds `needle` in the recognized text, with the box it sits in.

        The loop everyone writes after their first scan, with the parts everyone
        gets wrong: case folding, whitespace inside the line, and mapping the hit
        back onto the word boxes so it can be highlighted.

        Args:
            needle: Text to look for, or a regular expression when `regex` is set.
            regex: Treat `needle` as a Python regular expression.
            case: Match case-sensitively. Off by default — OCR case is not
                reliable enough to search on.
            whole_words: Require word boundaries around the match.

        >>> for hit in doc.search("gesamtbetrag"):        # doctest: +SKIP
        ...     print(hit.page, hit.text, hit.box.as_tuple())
        """
        if not needle:
            return ()
        pattern = needle if regex else re.escape(needle)
        if whole_words:
            pattern = rf"\b(?:{pattern})\b"
        flags = 0 if case else re.IGNORECASE
        compiled = re.compile(pattern, flags)

        found: list[Match] = []
        for page in self.pages:
            for line in page.lines:
                for hit in compiled.finditer(line.text):
                    found.append(
                        Match(
                            text=hit.group(0),
                            page=page.index,
                            box=_box_for_span(line, hit.start(), hit.end()),
                            line=line,
                        )
                    )
        return tuple(found)

    def to_dict(self) -> dict[str, Any]:
        """The raw engine output, including every box and score."""
        return self._raw

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> Document:
        return cls(
            source=data.get("source", ""),
            pages=tuple(Page._from_json(p) for p in data["pages"]),
            elapsed_ms=data.get("elapsed_ms", 0.0),
            _raw=data,
        )


def _box_for_span(line: Line, start: int, end: int) -> Box:
    """The box around the words a character range covers.

    Word boxes come from the recognizer's character positions, so a hit can be
    highlighted instead of merely located. When they are missing — ``word_boxes``
    was turned off, or the line has none — the line's own box is the honest
    answer.
    """
    if not line.words:
        return line.box

    boxes: list[Box] = []
    cursor = 0
    for word in line.words:
        position = line.text.find(word.text, cursor)
        if position < 0:
            continue
        cursor = position + len(word.text)
        if position < end and cursor > start:
            boxes.append(word.box)
    if not boxes:
        return line.box
    return Box(
        x0=min(b.x0 for b in boxes),
        y0=min(b.y0 for b in boxes),
        x1=max(b.x1 for b in boxes),
        y1=max(b.y1 for b in boxes),
    )
