"""Result objects returned by a scan.

These are plain dataclasses built from the engine's JSON, so they pickle,
compare and print without surprises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

__all__ = ["Box", "Word", "Line", "Block", "Page", "Document"]


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
    def _from_json(cls, data: dict[str, Any]) -> "Box":
        return cls(data["x0"], data["y0"], data["x1"], data["y1"])


@dataclass(frozen=True)
class Word:
    text: str
    box: Box
    confidence: float

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> "Word":
        return cls(data["text"], Box._from_json(data["bbox"]), data["confidence"])


@dataclass(frozen=True)
class Line:
    text: str
    box: Box
    confidence: float
    angle: float
    words: Sequence[Word] = field(default_factory=tuple)
    #: Corner points of the detection polygon, clockwise from the top left.
    polygon: Sequence[tuple[float, float]] = field(default_factory=tuple)

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> "Line":
        return cls(
            text=data["text"],
            box=Box._from_json(data["bbox"]),
            confidence=data["confidence"],
            angle=data.get("angle", 0.0),
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
    def _from_json(cls, data: dict[str, Any]) -> "Block":
        return cls(
            kind=data["kind"],
            box=Box._from_json(data["bbox"]),
            lines=tuple(Line._from_json(l) for l in data["lines"]),
        )


@dataclass(frozen=True)
class Page:
    index: int
    width: int
    height: int
    rotation: float
    origin: str
    blocks: Sequence[Block]
    elapsed_ms: float

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
        return sum(l.confidence for l in lines) / len(lines)

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> "Page":
        return cls(
            index=data["index"],
            width=data["width"],
            height=data["height"],
            rotation=data.get("rotation", 0.0),
            origin=data.get("origin", "image"),
            blocks=tuple(Block._from_json(b) for b in data["blocks"]),
            elapsed_ms=data.get("elapsed_ms", 0.0),
        )


@dataclass(frozen=True)
class Document:
    """The result of scanning one file."""

    source: str
    pages: Sequence[Page]
    elapsed_ms: float

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
        lines = self.lines
        if not lines:
            return None
        return sum(l.confidence for l in lines) / len(lines)

    def to_dict(self) -> dict[str, Any]:
        """The raw engine output, including every box and score."""
        return self._raw

    @classmethod
    def _from_json(cls, data: dict[str, Any]) -> "Document":
        doc = cls(
            source=data.get("source", ""),
            pages=tuple(Page._from_json(p) for p in data["pages"]),
            elapsed_ms=data.get("elapsed_ms", 0.0),
        )
        object.__setattr__(doc, "_raw", data)
        return doc
