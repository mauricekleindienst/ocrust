"""Finds the terms of a search profile in scanned documents, however broken up.

A profile names what to look for — project code names, people, customer
numbers, compound words, anything — and how strictly::

    [settings]
    fuzzy = "auto"          # edits tolerated on top of OCR look-alikes
    severity = "medium"

    [[term]]
    name = "Projekt Adler"
    match = ["Projekt Adler", "Operation Adler"]
    severity = "high"
    not_near = ["Radler"]

    [[term]]
    name = "Kundennummer"
    regex = 'KD-\\d{6}'

>>> profile = ocrust.terms.load("suchprofil.toml")       # doctest: +SKIP
>>> for hit in ocrust.scan("akte.pdf").find(profile):     # doctest: +SKIP
...     print(hit.page, hit.term, hit.text, hit.how)
1 Projekt Adler Projekt Ad\\nler ('hyphenated',)

Scanned text is rarely the text that was printed. A word is letter-spaced on a
stamp ("P r o j e k t"), hyphenated at a line end, broken over two lines or two
table cells, glued to its neighbour or split by a stray space; the recognizer
reads 0 for O and rn for m; one letter writes "Müller", the next "Mueller".
Each page is therefore matched as one stream of letters and digits with every
space, dash and line break taken out, so that all of those read as the same
word; what was taken out is kept aside, to check that a hit starts and ends on
word boundaries ("Adler" is not in "Radler") and to say how it was broken.
OCR look-alikes cost almost nothing, real edits count against the term's
``fuzzy`` budget.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ._types import (
    _LINE_END_HYPHENS,
    Box,
    Document,
    Line,
    Page,
    _box_for_span,
    _boxes_for_span,
    _word_spans,
)

__all__ = ["Profile", "Term", "Hit", "TermReport", "load", "parse", "find", "SEVERITIES"]

#: Severities in rising order.
SEVERITIES = ("info", "low", "medium", "high", "critical")

_TERM_KEYS = {
    "name",
    "match",
    "regex",
    "category",
    "severity",
    "fuzzy",
    "case",
    "whole_words",
    "zones",
    "near",
    "not_near",
    "window",
}
_SETTING_KEYS = {"fuzzy", "case", "whole_words", "severity", "zones", "window", "markings"}
_ZONES = ("header", "body", "footer")
#: How far into the page the header and footer bands reach, as in markings.
_BAND = 0.12


class ProfileError(ValueError):
    """A search profile that cannot be used, with what is wrong and where."""


# ------------------------------------------------------------------ the profile


@dataclass(frozen=True)
class Term:
    """One thing to look for."""

    name: str
    #: Literal phrases; any of them is a hit.
    match: tuple[str, ...] = ()
    #: Regular expressions, matched over the page text with line breaks,
    #: hyphenation and letter-spacing undone.
    regex: tuple[str, ...] = ()
    category: str = ""
    severity: str = "medium"
    #: Real edits tolerated on top of OCR look-alikes: ``None`` picks by length
    #: (none up to 4 letters, one up to 8, two beyond), 0 turns them off.
    fuzzy: int | None = None
    case: bool = False
    whole_words: bool = True
    zones: tuple[str, ...] = _ZONES
    near: tuple[str, ...] = ()
    not_near: tuple[str, ...] = ()
    window: int = 60

    @property
    def rank(self) -> int:
        return SEVERITIES.index(self.severity)


@dataclass(frozen=True)
class Profile:
    """A set of terms, and whether classification markings are wanted too."""

    terms: tuple[Term, ...]
    #: Also run :func:`ocrust.markings.inspect` (VS-NfD, GEHEIM, TLP, …).
    markings: bool = False
    source: str = ""

    def __len__(self) -> int:
        return len(self.terms)

    def __add__(self, other: Profile) -> Profile:
        """Both profiles' terms. A name in both is an error, as it is within one
        profile: two terms of one name would report one occurrence twice."""
        mine = {t.name for t in self.terms}
        clash = next((t.name for t in other.terms if t.name in mine), None)
        if clash is not None:
            sources = " and ".join(s for s in (self.source, other.source) if s)
            where = f" (in {sources})" if sources else ""
            raise ProfileError(f"two terms are named {clash!r}{where}; names must differ")
        return Profile(
            terms=self.terms + other.terms,
            markings=self.markings or other.markings,
            source=", ".join(s for s in (self.source, other.source) if s),
        )


def load(source: str | os.PathLike[str] | Profile | dict[str, Any] | Iterable[str]) -> Profile:
    """A profile from a file, a dict, a list of phrases, or a profile.

    Files: ``.toml`` (Python 3.11+, or with ``tomli`` installed), ``.json``, or
    any other text file with one phrase per line (``#`` starts a comment).
    """
    if isinstance(source, Profile):
        return source
    if isinstance(source, dict):
        return parse(source)
    if isinstance(source, (str, os.PathLike)) and _looks_like_path(source):
        path = Path(source)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ProfileError(f"{path}: {exc.strerror or exc}") from None
        except UnicodeDecodeError as exc:
            raise ProfileError(
                f"{path}: not UTF-8 text (byte {exc.start}); save the profile as UTF-8"
            ) from None
        suffix = path.suffix.lower()
        if suffix == ".toml":
            data = _toml(text, path)
        elif suffix == ".json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProfileError(f"{path}: line {exc.lineno}: {exc.msg}") from None
        else:
            # `#` starts a comment, at the start of a line or after a space;
            # "C#" stays a phrase.
            phrases = [re.sub(r"(^|\s)#.*$", "", line).strip() for line in text.splitlines()]
            data = {"term": [{"match": phrase} for phrase in phrases if phrase]}
        return parse(data, source=str(path))
    if isinstance(source, str):
        return parse({"term": [{"match": source}]})
    return parse({"term": [{"match": str(phrase)} for phrase in source]})


def _looks_like_path(source: str | os.PathLike[str]) -> bool:
    if isinstance(source, os.PathLike):
        return True
    return Path(source).suffix.lower() in (".toml", ".json", ".txt", ".csv", ".list") or (
        os.sep in source and Path(source).exists()
    )


def _toml(text: str, path: Path) -> dict[str, Any]:
    try:
        import tomllib  # type: ignore[import-not-found,unused-ignore]
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef,unused-ignore]
        except ModuleNotFoundError:
            raise ProfileError(
                f"{path}: reading TOML needs Python 3.11 or `pip install tomli`; "
                "a .json profile works everywhere"
            ) from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"{path}: {exc}") from None


def parse(data: dict[str, Any], source: str = "") -> Profile:
    """A profile from its dict form, checked: an unknown key or a bad value is
    an error that says where, not a term that silently never matches."""
    where = f"{source}: " if source else ""
    if not isinstance(data, dict):
        raise ProfileError(
            f"{where}a profile is a table with [settings] and [[term]] entries, "
            f"not a {type(data).__name__}"
        )
    unknown = set(data) - {"settings", "term", "terms"}
    if unknown:
        raise ProfileError(f"{where}unknown section {sorted(unknown)}; use [settings] and [[term]]")
    settings = data.get("settings", {})
    if not isinstance(settings, dict):
        raise ProfileError(f"{where}[settings] must be a table")
    bad = set(settings) - _SETTING_KEYS
    if bad:
        known = sorted(_SETTING_KEYS)
        raise ProfileError(f"{where}[settings]: unknown {sorted(bad)}; known: {known}")
    raw_terms: list[Any] = []
    for key in ("term", "terms"):
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, dict):
            raise ProfileError(
                f"{where}[{key}] is a single table; write [[term]] (double brackets) "
                "before each term"
            )
        if not isinstance(value, list):
            raise ProfileError(f"{where}[[term]] must be a list of tables")
        raw_terms.extend(value)
    for i, entry in enumerate(raw_terms):
        if not isinstance(entry, dict):
            raise ProfileError(
                f"{where}term {i + 1}: {entry!r} is not a table; phrases go in "
                'match = ["…", "…"] inside a [[term]]'
            )
    defaults = {k: v for k, v in settings.items() if k != "markings"}
    terms = [
        _term({**defaults, **entry}, f"{where}term {i + 1}") for i, entry in enumerate(raw_terms)
    ]
    names = [t.name for t in terms]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ProfileError(f"{where}two terms are named {duplicates[0]!r}; names must differ")
    markings = settings.get("markings", False)
    if not isinstance(markings, bool):
        raise ProfileError(f"{where}[settings] markings must be true or false")
    if not terms and not markings:
        raise ProfileError(f"{where}the profile has no terms")
    return Profile(terms=tuple(terms), markings=markings, source=source)


def _strings(value: Any, what: str, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ProfileError(f"{where}: {what} must be a string or a list of non-empty strings")
    return tuple(v.strip() for v in value)


def _term(entry: dict[str, Any], where: str) -> Term:
    if not isinstance(entry, dict):
        raise ProfileError(f"{where}: must be a table")
    bad = set(entry) - _TERM_KEYS
    if bad:
        hint = "; markings is a [settings] key" if "markings" in bad else ""
        raise ProfileError(f"{where}: unknown {sorted(bad)}; known: {sorted(_TERM_KEYS)}{hint}")
    for key in ("name", "category"):
        if key in entry and not isinstance(entry[key], str):
            raise ProfileError(f"{where}: {key} must be a string, not {entry[key]!r}")
    match = _strings(entry.get("match"), "match", where)
    regex = _strings(entry.get("regex"), "regex", where)
    if not match and not regex and isinstance(entry.get("name"), str) and entry["name"].strip():
        # A term that is only named is looked for by its name.
        match = (entry["name"].strip(),)
    if not match and not regex:
        raise ProfileError(f"{where}: needs `match` (phrases), `regex` or at least a `name`")
    for pattern in regex:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ProfileError(f"{where}: regex {pattern!r}: {exc}") from None
    for phrase in match:
        if not _skeleton_of(phrase)[0]:
            raise ProfileError(f"{where}: {phrase!r} has no letters or digits to match")
    name = str(entry.get("name") or (match or regex)[0])
    severity = str(entry.get("severity", "medium")).lower()
    if severity not in SEVERITIES:
        raise ProfileError(f"{where} ({name}): severity {severity!r}; use one of {SEVERITIES}")
    fuzzy = entry.get("fuzzy", "auto")
    if fuzzy in ("auto", None):
        fuzzy_n: int | None = None
    elif fuzzy in ("off", False):
        fuzzy_n = 0
    elif isinstance(fuzzy, int) and not isinstance(fuzzy, bool) and 0 <= fuzzy <= 3:
        fuzzy_n = fuzzy
    else:
        raise ProfileError(f'{where} ({name}): fuzzy {fuzzy!r}; use "auto", "off" or 0-3')
    zones = _strings(entry.get("zones"), "zones", where) or _ZONES
    for zone in zones:
        if zone not in _ZONES:
            raise ProfileError(f"{where} ({name}): zone {zone!r}; use {list(_ZONES)}")
    window = entry.get("window", 60)
    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        raise ProfileError(f"{where} ({name}): window must be a positive number of characters")
    for flag in ("case", "whole_words"):
        if not isinstance(entry.get(flag, True), bool):
            raise ProfileError(f"{where} ({name}): {flag} must be true or false")
    return Term(
        name=name,
        match=match,
        regex=regex,
        category=str(entry.get("category", "")),
        severity=severity,
        fuzzy=fuzzy_n,
        case=bool(entry.get("case", False)),
        whole_words=bool(entry.get("whole_words", True)),
        zones=zones,
        near=_strings(entry.get("near"), "near", where),
        not_near=_strings(entry.get("not_near"), "not_near", where),
        window=window,
    )


# ------------------------------------------------------------------ results


@dataclass(frozen=True)
class Hit:
    """One occurrence of a term, and how it was found."""

    term: str
    category: str
    severity: str
    #: The phrase or expression of the term that matched.
    pattern: str
    #: What the page says there, lines joined by a newline.
    text: str
    #: 1-based page number, as in the source.
    page: int
    #: Around the whole hit; `boxes` has one per line it spans.
    box: Box
    boxes: tuple[Box, ...]
    #: How the text was broken: ``exact``, ``spaced`` (letter-spaced),
    #: ``hyphenated`` (at a line end), ``split`` (over lines or cells),
    #: ``glued`` (a space missing), ``broken`` (a space too many), ``ocr``
    #: (look-alike characters), ``spelling`` ("ae" for "ä"), ``fuzzy``
    #: (real edits), ``regex``.
    how: tuple[str, ...]
    #: 1.0 for an exact read, lower the more had to be forgiven.
    score: float
    #: ``header``, ``body`` or ``footer``.
    zone: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "term",
            "term": self.term,
            "category": self.category,
            "severity": self.severity,
            "pattern": self.pattern,
            "text": self.text,
            "page": self.page,
            "box": [round(v, 1) for v in self.box.as_tuple()],
            "boxes": [[round(v, 1) for v in b.as_tuple()] for b in self.boxes],
            "how": list(self.how),
            "score": round(self.score, 3),
            "zone": self.zone,
        }


@dataclass(frozen=True)
class TermReport:
    """What :func:`find` found in one document."""

    source: str
    hits: tuple[Hit, ...]

    def __iter__(self) -> Iterator[Hit]:
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)

    def __bool__(self) -> bool:
        return bool(self.hits)

    @property
    def terms(self) -> dict[str, int]:
        """Hits per term name, in the order first found."""
        counts: dict[str, int] = {}
        for hit in self.hits:
            counts[hit.term] = counts.get(hit.term, 0) + 1
        return counts

    @property
    def severity(self) -> str | None:
        """The highest severity among the hits, or ``None``."""
        if not self.hits:
            return None
        return max((h.severity for h in self.hits), key=SEVERITIES.index)

    def at_least(self, severity: str) -> bool:
        return any(SEVERITIES.index(h.severity) >= SEVERITIES.index(severity) for h in self.hits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "severity": self.severity,
            "terms": self.terms,
            "hits": [h.to_dict() for h in self.hits],
        }


# ------------------------------------------------------------------ folding

#: Gaps between two letters of the stream, by what stood between them.
_NONE, _SPACE, _PUNCT, _LINE, _HYPHEN, _BLOCK, _SPACED, _CASE, _DIGIT = range(9)
_DASHES = set("-‐‑‒–—―−﹘﹣－­")
_INVISIBLE = set("​‌‍⁠﻿")
#: Symbols the recognizer puts where a letter was, inside a word.
_SYMBOL_LETTERS = {"|": "I", "!": "I", "$": "S", "€": "E", "@": "A"}


def _fold(ch: str) -> tuple[str, bool]:
    """Capitals without diacritics, and whether an umlaut was folded away."""
    decomposed = unicodedata.normalize("NFKD", ch)
    umlaut = "̈" in decomposed
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).upper()
    return plain.replace("ẞ", "SS"), umlaut


#: Unicode categories of marks set beside a word rather than in it: a
#: footnote ¹, a fraction ½, ①, ™, ®, ℃. The NFKD fold would turn them into
#: letters and digits glued to the word ("Adler¹" into "ADLER1").
_BESIDE = ("No", "So")


def _letters(ch: str) -> tuple[str, bool]:
    """What one character adds to the stream: its letters and digits, folded,
    and whether an umlaut was folded away; nothing for a space, punctuation or
    a mark beside a word. Phrase and page are read with this one rule, so a
    term's letters line up with the page's."""
    if unicodedata.category(ch) in _BESIDE:
        return "", False
    folded, umlaut = _fold(ch)
    if not folded or not folded.isalnum():
        return "", False
    return folded, umlaut


def _skeleton_of(phrase: str) -> tuple[str, list[bool], list[bool]]:
    """A term's letters and digits, where its own word breaks fall, and which
    letters were umlauts."""
    chars: list[str] = []
    breaks: list[bool] = []
    umlauts: list[bool] = []
    pending = False
    for ch in phrase:
        if unicodedata.combining(ch):
            # Written decomposed, "u" + U+0308: the dots belong to the letter.
            if ch == "\u0308" and umlauts:
                umlauts[-1] = True
            continue
        folded, umlaut = _letters(ch)
        if not folded:
            pending = bool(chars)
            continue
        for c in folded:
            chars.append(c)
            breaks.append(pending)
            umlauts.append(umlaut)
            pending = False
    return "".join(chars), breaks, umlauts


@dataclass
class _Stream:
    """A page as one run of letters and digits, and where each came from."""

    chars: list[str] = field(default_factory=list)
    gaps: list[int] = field(default_factory=list)
    umlauts: list[bool] = field(default_factory=list)
    #: (line number, character offset in that line's text)
    origin: list[tuple[int, int]] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    #: Per line: the layout line it is part of, and where in its text it starts
    #: (a piece of a line read in column order is part of the whole line).
    parents: list[tuple[Line, int]] = field(default_factory=list)
    zones: list[str] = field(default_factory=list)

    _text: str = ""
    _positions: dict[tuple[int, int], int] = field(default_factory=dict)

    @property
    def text(self) -> str:
        if len(self._text) != len(self.chars):
            self._text = "".join(self.chars)
        return self._text

    def position(self, origin: tuple[int, int]) -> int | None:
        """The stream index of a line character, if it is a letter or digit."""
        if not self._positions:
            for k, o in enumerate(self.origin):
                self._positions.setdefault(o, k)
        return self._positions.get(origin)


def _spaced_positions(text: str) -> set[int]:
    """Offsets of characters inside a letter-spaced run ("G E H E I M"),
    other than its first: the spaces before them split no word."""
    tokens = [(m.start(), m.group()) for m in re.finditer(r"\S+", text)]
    inside: set[int] = set()
    run: list[int] = []

    def close() -> None:
        if len(run) >= 3:
            inside.update(run[1:])
        run.clear()

    for start, token in tokens:
        if len(token) == 1 and (token.isalnum() or token in "-/.:"):
            run.append(start)
        else:
            close()
    close()
    return inside


def _reading_columns(page: Page) -> list[tuple[Line, Line, int]]:
    """The page's pieces in the order a reader of columns takes them, each with
    the line it is part of and where in that line's text it starts.

    The layout reads a row at a time, and on a two-column page it can take the
    two columns' lines for one: "Sicherheit Nordlicht wird bis zum Sommer". A
    phrase that wraps inside a column or a table cell ("Zutritts" /
    "kontrollanlage"), or from the foot of one column to the head of the next
    ("Mit dem Projekt" … "Nordlicht wird"), then has other text between its
    halves. Cut the page where it is emptiest, again and again — between two
    columns, between two sections — and read the parts in order, and the
    halves meet again.
    """
    from .markings import _pieces

    pieces: list[tuple[Line, Line, int]] = []
    for block in page.blocks:
        if block.kind == "tick_box":
            continue
        for line in block.lines:
            cursor = 0
            for piece in _pieces(line):
                offset = line.text.find(piece.text, cursor) if piece is not line else 0
                if offset < 0:
                    offset = cursor
                cursor = offset + len(piece.text)
                pieces.append((piece, line, offset))
    order: list[tuple[Line, Line, int]] = []
    stack = [pieces]
    while stack:
        group = stack.pop()
        if len(group) < 2:
            order.extend(group)
            continue
        xs = [_extent(g[0].box.x0, g[0].box.x1) for g in group]
        ys = [_extent(g[0].box.y0, g[0].box.y1) for g in group]
        x_gap, x_cut = _widest_gap(xs)
        y_gap, y_cut = _widest_gap(ys)
        spans, cut = (xs, x_cut) if x_gap > y_gap else (ys, y_cut)
        before = [hi <= cut for _, hi in spans]
        # A box drawn backwards cannot make a cut that leaves one side empty
        # loop for ever: what cannot be cut is read top to bottom.
        if max(x_gap, y_gap) <= 0 or all(before) or not any(before):
            ranked = sorted(zip(ys, xs, range(len(group))))
            order.extend(group[k] for _, _, k in ranked)
            continue
        stack.append([g for g, b in zip(group, before) if not b])
        stack.append([g for g, b in zip(group, before) if b])
    return order


def _extent(a: float, b: float) -> tuple[float, float]:
    return (a, b) if a <= b else (b, a)


def _widest_gap(spans: list[tuple[float, float]]) -> tuple[float, float]:
    """The widest stretch no span covers, and its middle."""
    spans = sorted(spans)
    reach = spans[0][1]
    best = (0.0, 0.0)
    for lo, hi in spans[1:]:
        if lo - reach > best[0]:
            best = (lo - reach, (reach + lo) / 2)
        reach = max(reach, hi)
    return best


def _stream(page: Page, columns: bool = False) -> _Stream:
    """The page as one stream, in the layout's reading order or, with
    `columns`, column by column (see `_reading_columns`)."""
    s = _Stream()
    pending = _BLOCK
    if columns:
        runs = [_reading_columns(page)]
    else:
        runs = [
            [(line, line, 0) for line in block.lines]
            for block in page.blocks
            if block.kind != "tick_box"
        ]
    for run in runs:
        pending = max(pending, _BLOCK) if s.chars else pending
        for line, whole, offset in run:
            number = len(s.lines)
            s.lines.append(line)
            s.parents.append((whole, offset))
            s.zones.append(_zone(line, page))
            text = line.text
            spaced = _spaced_positions(text)
            joins = _hyphen_joins(line)
            apart = _far_apart(line)
            for i, ch in enumerate(text):
                if i in apart and pending < _BLOCK:
                    pending = _BLOCK
                if i in joins:
                    pending = _HYPHEN
                if ch in _INVISIBLE:
                    continue
                if unicodedata.combining(ch):
                    if ch == "\u0308" and s.chars and s.origin[-1][0] == number:
                        s.umlauts[-1] = True
                    continue
                folded, umlaut = _letters(ch)
                if ch in _SYMBOL_LETTERS and 0 < i < len(text) - 1:
                    before, after = text[i - 1], text[i + 1]
                    if before.isalpha() and after.isalpha():
                        folded, umlaut = _SYMBOL_LETTERS[ch], False
                if not folded:
                    pending = max(pending, _SPACE if ch.isspace() else _PUNCT)
                    continue
                gap = pending
                previous = text[i - 1] if i > 0 else ""
                if gap == _SPACE and i in spaced:
                    gap = _SPACED
                elif gap == _NONE and ch.isupper() and previous.islower():
                    # "HerrWeißmüller", "DieAuswertung": a space the scan lost
                    # between two words still shows as a capital after a small.
                    gap = _CASE
                elif gap == _NONE and previous.isalnum() and ch.isdigit() != previous.isdigit():
                    # "Adler1", "FS220": a footnote read as a digit, a code
                    # glued to its number. A word may end here; it need not.
                    gap = _DIGIT
                for c in folded:
                    s.chars.append(c)
                    s.gaps.append(gap)
                    s.umlauts.append(umlaut)
                    s.origin.append((number, i))
                    gap = _NONE
                pending = _NONE
            # How this line hands over to the next one.
            tail = text.rstrip()
            if tail and tail[-1] in _DASHES and len(tail) > 1 and tail[-2].isalpha():
                pending = _HYPHEN
            else:
                pending = _LINE
    return s


def _hyphen_joins(line: Line) -> set[int]:
    """Offsets in ``line.text`` where a word the layout joined across a line
    end goes on: "Brand-" / "meldezentrale" read as "Brandmeldezentrale"."""
    joins: set[int] = set()
    for word, start, end, _ in _word_spans(line):
        # The word kept its hyphen and the text did not: the layout joined it.
        joined = word.text[-1:] in _LINE_END_HYPHENS and end - start < len(word.text)
        if joined and end < len(line.text) and line.text[end].isalnum():
            joins.add(end)
    return joins


def _far_apart(line: Line) -> set[int]:
    """Offsets in ``line.text`` where a piece of the line starts that stands
    far from the one before — the next column, not the next word. A phrase
    across such a gap is "split", not "exact"."""
    if len(line.segments) < 2:
        return set()
    starts: list[tuple[Any, int]] = []
    cursor = 0
    for segment in line.segments:
        position = line.text.find(segment.text, cursor)
        if position < 0:
            continue
        cursor = position + len(segment.text)
        starts.append((segment, position))
    apart: set[int] = set()
    for (a, _), (b, position) in zip(starts, starts[1:]):
        height = min(abs(a.box.y1 - a.box.y0), abs(b.box.y1 - b.box.y0))
        if b.box.x0 - a.box.x1 > 2 * height:
            apart.add(position)
    return apart


def _zone(line: Line, page: Page) -> str:
    if page.height <= 0:
        return "body"
    middle = (line.box.y0 + line.box.y1) / 2 / page.height
    if middle < _BAND:
        return "header"
    if middle > 1 - _BAND:
        return "footer"
    return "body"


# ------------------------------------------------------------------ matching

#: Characters the recognizer mistakes for one another, by class.
_LOOKALIKE = {}
for _group in ("O0Q", "I1L", "S5", "B8", "G6", "Z2", "UV", "A4", "T7"):
    for _a in _group:
        for _b in _group:
            if _a != _b:
                _LOOKALIKE[(_a, _b)] = True
#: Canonical letters for the prefilter: every look-alike class to one member.
_CANON = str.maketrans(
    {
        "0": "O",
        "Q": "O",
        "1": "I",
        "L": "I",
        "5": "S",
        "8": "B",
        "6": "G",
        "2": "Z",
        "V": "U",
        "4": "A",
        "7": "T",
    }
)
# Costs, packed into one number so the alignment stays a plain minimum: real
# edits dominate, then look-alike characters, then spelling variants.
EDIT = 10_000  # a letter wrong, missing or too many
LOOK = 100  # a look-alike read for another: 0 for O, rn for m
SPELL = 1  # "ae" for "ä", "ss" for "ß"


def _budget(term: Term, length: int) -> int:
    if term.fuzzy is not None:
        return term.fuzzy
    if length <= 4:
        return 0
    return 1 if length <= 8 else 2


def _canon(text: str, collapse: bool = True) -> tuple[str, list[int]]:
    """Look-alike classes collapsed, and with `collapse` rn read as m, vv as w
    and ae as a, for finding candidate places fast; `index` maps back to the
    stream. Both views are searched: a collapse can also join the end of one
    word to the next ("SERVER NOX" reads "SERVEMOX")."""
    translated = text.translate(_CANON)
    if not collapse:
        return translated, list(range(len(text)))
    out: list[str] = []
    index: list[int] = []
    i = 0
    while i < len(translated):
        pair = text[i : i + 2]
        if pair in ("AE", "OE", "UE"):
            out.append(translated[i])
            index.append(i)
            i += 2
            continue
        if pair == "RN":
            out.append("M")
            index.append(i)
            i += 2
            continue
        if pair == "VV":
            out.append("W")
            index.append(i)
            i += 2
            continue
        out.append(translated[i])
        index.append(i)
        i += 1
    return "".join(out), index


def _table(
    pattern: str, p_umlaut: list[bool], text: str, t_umlaut: list[bool], free: bool
) -> tuple[list[list[int]], list[list[int]]]:
    """Weighted edit distance of `pattern` against `text`, and where each
    alignment began. With `free`, the text before and after costs nothing."""
    m, n = len(pattern), len(text)
    inf = 10**9
    first = [0] * (n + 1) if free else [j * EDIT for j in range(n + 1)]
    cost = [first] + [[inf] * (n + 1) for _ in range(m)]
    begin = [list(range(n + 1)) if free else [0] * (n + 1)] + [[0] * (n + 1) for _ in range(m)]
    for i in range(1, m + 1):
        cost[i][0] = i * EDIT
        pc = pattern[i - 1]
        row, prev = cost[i], cost[i - 1]
        brow, bprev = begin[i], begin[i - 1]
        for j in range(1, n + 1):
            tc = text[j - 1]
            if tc == pc:
                best, b = prev[j - 1], bprev[j - 1]
            elif (tc, pc) in _LOOKALIKE:
                best, b = prev[j - 1] + LOOK, bprev[j - 1]
            else:
                best, b = prev[j - 1] + EDIT, bprev[j - 1]
            if prev[j] + EDIT < best:  # a letter of the term missing
                best, b = prev[j] + EDIT, bprev[j]
            if row[j - 1] + EDIT < best:  # a letter too many in the text
                best, b = row[j - 1] + EDIT, brow[j - 1]
            if j >= 2:
                two = text[j - 2 : j]
                # rn read for m, vv for w
                pair = (pc == "M" and two == "RN") or (pc == "W" and two == "VV")
                if pair and prev[j - 2] + LOOK < best:
                    best, b = prev[j - 2] + LOOK, bprev[j - 2]
                # "ae" written for an "ä" of the term
                if p_umlaut[i - 1] and two == pc + "E" and prev[j - 2] + SPELL < best:
                    best, b = prev[j - 2] + SPELL, bprev[j - 2]
            # an "ä" printed for "ae" in the term
            spelled_out = i >= 2 and t_umlaut[j - 1] and pattern[i - 2 : i] == tc + "E"
            if spelled_out and cost[i - 2][j - 1] + SPELL < best:
                best, b = cost[i - 2][j - 1] + SPELL, begin[i - 2][j - 1]
            row[j] = best
            brow[j] = b
    return cost, begin


def _within(cost: int, budget: int, max_lookalikes: int) -> bool:
    return cost // EDIT <= budget and cost % EDIT // LOOK <= max_lookalikes


def _align(
    pattern: str,
    p_umlaut: list[bool],
    text: str,
    t_umlaut: list[bool],
    budget: int,
    max_lookalikes: int,
) -> list[tuple[int, int, int]]:
    """Every place `pattern` fits inside `text` within the budget: (start,
    end, cost), best first, not overlapping.

    Semi-global weighted edit distance: the pattern must be used whole, the
    text around it is free. A look-alike (0 for O, "rn" for "m") costs
    `LOOK`, "ae" for an "ä" `SPELL`, anything else `EDIT`.
    """
    m, n = len(pattern), len(text)
    cost, begin = _table(pattern, p_umlaut, text, t_umlaut, free=True)
    ends = [
        (cost[m][j], begin[m][j], j)
        for j in range(1, n + 1)
        if _within(cost[m][j], budget, max_lookalikes)
    ]
    ends.sort()
    chosen: list[tuple[int, int, int]] = []
    for c, start, end in ends:
        if end - start < max(1, m - budget):
            continue
        if any(start < e and s < end for s, e, _ in chosen):
            continue
        chosen.append((start, end, c))
    return chosen


def _fit(pattern: str, p_umlaut: list[bool], text: str, t_umlaut: list[bool]) -> int:
    """The cost of reading all of `text` as `pattern`."""
    cost, _ = _table(pattern, p_umlaut, text, t_umlaut, free=False)
    return cost[len(pattern)][len(text)]


def _candidates(
    views: Sequence[tuple[str, list[int], bool]], pattern: str, budget: int
) -> list[tuple[int, int]]:
    """Windows of the stream where `pattern` could be, by the pigeonhole rule:
    with k edits, one of k + 1 pieces of the pattern appears unchanged."""
    windows: list[tuple[int, int]] = []
    slack = budget + 2
    for canon, index, collapse in views:
        cpattern, _ = _canon(pattern, collapse)
        m = len(cpattern)
        pieces = max(1, min(budget + 1, m))
        size = m // pieces
        for p in range(pieces):
            offset = p * size
            piece = cpattern[offset : offset + size if p < pieces - 1 else m]
            if not piece:
                continue
            start = canon.find(piece)
            while start >= 0:
                s = index[start]
                lo = max(0, s - offset - slack - budget)
                hi = s - offset + len(pattern) + slack + budget
                windows.append((lo, hi))
                start = canon.find(piece, start + 1)
    windows.sort()
    merged: list[tuple[int, int]] = []
    for lo, hi in windows:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _boundary(stream: _Stream, position: int) -> bool:
    """Whether a word may start at `position` (or end just before it): any
    gap will do, a line-end hyphen and letter-spacing included, since either
    may or may not split a word."""
    if position <= 0 or position >= len(stream.chars):
        return True
    return stream.gaps[position] != _NONE


# ------------------------------------------------------------------ finding


def find(document: Document, profile: Profile | Any) -> TermReport:
    """Every occurrence of the profile's terms in `document`, page by page."""
    profile = load(profile)
    hits: list[Hit] = []
    for page in document.pages:
        streams = [_stream(page)]
        by_columns = _stream(page, columns=True)
        if by_columns.text != streams[0].text:
            streams.append(by_columns)
        if not streams[0].chars:
            continue
        prepared = [_Prepared(stream) for stream in streams]
        for term in profile.terms:
            found: list[_Found] = []
            for ready in prepared:
                for phrase in term.match:
                    found.extend(_phrase_hits(term, phrase, page, ready))
                for pattern in term.regex:
                    for flat in ready.flats():
                        found.extend(_regex_hits(term, pattern, page, ready.stream, flat))
            # Zones and context per reading order, before one hit per place is
            # chosen: `near` holds if any order puts the word beside the hit
            # ("Codename" above "Falke" in its column), `not_near` rules the
            # place out if any order does.
            found = [item for item in found if item.hit.zone in term.zones]
            vetoed = [item for item in found if not _not_near_ok(term, item)]
            allowed = [
                item
                for item in found
                if _near_ok(term, item) and not any(_same_place(item, v) for v in vetoed)
            ]
            hits.extend(item.hit for item in _distinct(allowed))
    hits.sort(key=lambda h: (h.page, h.box.y0, h.box.x0))
    return TermReport(source=document.source, hits=tuple(hits))


@dataclass
class _Found:
    hit: Hit
    stream: _Stream
    start: int
    end: int
    #: Per layout line it touches: the line, the character range in its text,
    #: and the box around that range.
    places: tuple[tuple[Line, int, int, Box], ...] = ()


def _places(stream: _Stream, spots: Iterable[tuple[int, int]]) -> tuple:
    per_line: dict[int, list[int]] = {}
    for line, offset in spots:
        per_line.setdefault(line, []).append(offset)
    places = []
    for n, offsets in per_line.items():
        line, lo, hi = stream.lines[n], min(offsets), max(offsets) + 1
        whole, shift = stream.parents[n]
        places.append((whole, lo + shift, hi + shift, _box_for_span(line, lo, hi)))
    return tuple(places)


class _Prepared:
    """A stream with its prefilter views and regex texts, built once a page."""

    def __init__(self, stream: _Stream) -> None:
        self.stream = stream
        text = stream.text
        self.views = [(*_canon(text, True), True), (*_canon(text, False), False)]
        self._flats: list[_Flat] | None = None

    def flats(self) -> list[_Flat]:
        """The page text for regular expressions, three ways: as read; with a
        line that ends in a letter or digit joined to the next ("KD-12" /
        "3456"); and with the spaces around single characters taken out, which
        is how a form's comb fields read ("K D - 4 3 8 3 0 0")."""
        if self._flats is None:
            self._flats = [
                _flat(self.stream),
                _flat(self.stream, close_lines=True),
                _flat(self.stream, compact=True),
            ]
        return self._flats


def _distinct(found: list[_Found]) -> list[_Found]:
    """One hit per place: two phrases of a term, a phrase and a regex, the two
    reading orders, or a stamp read twice that found the same words."""
    kept: list[_Found] = []
    for item in sorted(found, key=lambda f: -f.hit.score):
        if any(_same_place(item, k) for k in kept):
            continue
        kept.append(item)
    return kept


def _same_place(a: _Found, b: _Found) -> bool:
    """Whether two hits cover the same letters: on one line, the same
    characters; on two lines (a stamp read twice, a cell read in both
    orders), the same spot on the page."""
    for line_a, lo_a, hi_a, x in a.places:
        for line_b, lo_b, hi_b, y in b.places:
            if line_a is line_b:
                if lo_a < hi_b and lo_b < hi_a:
                    return True
                continue
            w = min(x.x1, y.x1) - max(x.x0, y.x0)
            h = min(x.y1, y.y1) - max(x.y0, y.y0)
            if w > 0 and h > 0:
                smaller = min(x.width * x.height, y.width * y.height) or 1.0
                if w * h > 0.3 * smaller:
                    return True
    return False


def _phrase_hits(term: Term, phrase: str, page: Page, ready: _Prepared) -> Iterator[_Found]:
    stream = ready.stream
    pattern, breaks, p_umlaut = _skeleton_of(phrase)
    budget = min(_budget(term, len(pattern)), len(pattern) // 3)
    # Look-alikes are forgiven, but not so many that the word is gone.
    lookalikes = max(1, len(pattern) // 3)
    taken: list[tuple[int, int]] = []
    for lo, hi in _candidates(ready.views, pattern, budget):
        hi = min(hi, len(stream.chars))
        window = stream.text[lo:hi]
        for start, end, c in _align(
            pattern, p_umlaut, window, stream.umlauts[lo:hi], budget, lookalikes
        ):
            start, end = start + lo, end + lo
            if term.whole_words:
                snapped = _snap(stream, pattern, p_umlaut, start, end, budget, lookalikes)
                if snapped is None:
                    continue
                start, end, c = snapped
            if any(start < e and s < end for s, e in taken):
                continue
            if term.whole_words and _another_word(pattern, p_umlaut, stream, start, end, c):
                continue
            if not _short_words_kept(pattern, breaks, stream, start, end):
                continue
            steps = _path(pattern, p_umlaut, stream.text[start:end], stream.umlauts[start:end])
            wanted = _phrase_letters(phrase)
            if term.case and not _same_case(wanted, steps, stream, start):
                continue
            taken.append((start, end))
            hit = _hit(term, phrase, page, stream, start, end, c, breaks, len(pattern))
            if _sharp_s_spelled(wanted, steps, stream, start) and "spelling" not in hit.how:
                how = tuple(sorted(set(hit.how) - {"exact"} | {"spelling"}))
                hit = replace(hit, how=how)
            yield _Found(hit, stream, start, end, _places(stream, stream.origin[start:end]))


def _another_word(
    pattern: str, p_umlaut: list[bool], stream: _Stream, start: int, end: int, cost: int
) -> bool:
    """Whether the hit is another word or number that contains the term, or
    that the term contains: "Radler" or "Adlers" for "Adler", "Adler" for
    "Radler", "Hafenstraße 12" for "Hafenstraße 120".

    That is the case when no cheapest reading of the hit aligns its first and
    its last letter with the term's: the letter too many, or too few, at the
    edge cannot be read as anything else. "Opperation" aligns both ends; its
    extra letter is inside the word, a misreading. A digit is stricter: "1200"
    is not "120" misread, so where the term ends in a digit, any cheapest
    reading with a digit too many or too few there rules the hit out.
    """
    text, dots = stream.text[start:end], stream.umlauts[start:end]
    m, n = len(pattern), len(text)
    if m < 2 or n < 2:
        return False
    if pattern[0].isdigit() and (
        _fit(pattern, p_umlaut, text[1:], dots[1:]) + EDIT <= cost
        or _fit(pattern[1:], p_umlaut[1:], text, dots) + EDIT <= cost
    ):
        return True
    if pattern[-1].isdigit() and (
        _fit(pattern, p_umlaut, text[:-1], dots[:-1]) + EDIT <= cost
        or _fit(pattern[:-1], p_umlaut[:-1], text, dots) + EDIT <= cost
    ):
        return True
    starts = _edge_moves(pattern, p_umlaut, text, dots, 0)
    ends = _edge_moves(pattern, p_umlaut, text, dots, -1)
    start_kept = any(
        c + _fit(pattern[a:], p_umlaut[a:], text[b:], dots[b:]) == cost for a, b, c in starts
    )
    end_kept = any(
        c + _fit(pattern[: m - a], p_umlaut[: m - a], text[: n - b], dots[: n - b]) == cost
        for a, b, c in ends
    )
    return not (start_kept and end_kept)


def _edge_moves(
    pattern: str, p_umlaut: list[bool], text: str, dots: list[bool], edge: int
) -> list[tuple[int, int, int]]:
    """The moves of `_table` that read the term's letter at one edge (`edge` 0
    the first, -1 the last) as the text's letter there: (term letters, text
    letters, cost)."""
    if edge == 0:
        p, t, p2, t2 = pattern[0], text[0], pattern[:2], text[:2]
    else:
        p, t, p2, t2 = pattern[-1], text[-1], pattern[-2:], text[-2:]
    if t == p:
        moves = [(1, 1, 0)]
    elif (t, p) in _LOOKALIKE:
        moves = [(1, 1, LOOK)]
    else:
        moves = [(1, 1, EDIT)]
    if (p == "M" and t2 == "RN") or (p == "W" and t2 == "VV"):
        moves.append((1, 2, LOOK))  # rn read for m, vv for w
    if p_umlaut[edge] and t2 == p + "E":
        moves.append((1, 2, SPELL))  # "ae" written for the term's "ä"
    if dots[edge] and p2 == t + "E":
        moves.append((2, 1, SPELL))  # an "ä" printed for the term's "ae"
    return moves


def _path(
    pattern: str, p_umlaut: list[bool], text: str, t_umlaut: list[bool]
) -> list[tuple[str, int, int, int, int]]:
    """How all of `text` reads as `pattern`, step by step: (kind, pattern from,
    pattern to, text from, text to), for one cheapest alignment. Kinds: "same",
    "look" (a look-alike), "edit", "spell" ("ae" for "ä"), "missing" (a letter
    of the term the text lacks) and "extra" (a letter the term lacks)."""
    cost, _ = _table(pattern, p_umlaut, text, t_umlaut, free=False)
    i, j = len(pattern), len(text)
    steps: list[tuple[str, int, int, int, int]] = []
    while i > 0 or j > 0:
        if i == 0:
            steps.append(("extra", 0, 0, j - 1, j))
            j -= 1
            continue
        if j == 0:
            steps.append(("missing", i - 1, i, 0, 0))
            i -= 1
            continue
        pc, tc = pattern[i - 1], text[j - 1]
        # The same moves as `_table`, so one of them gives the table's value.
        if tc == pc:
            options = [(cost[i - 1][j - 1], "same", i - 1, j - 1)]
        elif (tc, pc) in _LOOKALIKE:
            options = [(cost[i - 1][j - 1] + LOOK, "look", i - 1, j - 1)]
        else:
            options = [(cost[i - 1][j - 1] + EDIT, "edit", i - 1, j - 1)]
        options.append((cost[i - 1][j] + EDIT, "missing", i - 1, j))
        options.append((cost[i][j - 1] + EDIT, "extra", i, j - 1))
        if j >= 2:
            two = text[j - 2 : j]
            if (pc == "M" and two == "RN") or (pc == "W" and two == "VV"):
                options.append((cost[i - 1][j - 2] + LOOK, "look", i - 1, j - 2))
            if p_umlaut[i - 1] and two == pc + "E":
                options.append((cost[i - 1][j - 2] + SPELL, "spell", i - 1, j - 2))
        if i >= 2 and t_umlaut[j - 1] and pattern[i - 2 : i] == tc + "E":
            options.append((cost[i - 2][j - 1] + SPELL, "spell", i - 2, j - 1))
        _, kind, pi, pj = next(o for o in options if o[0] == cost[i][j])
        steps.append((kind, pi, i, pj, j))
        i, j = pi, pj
    steps.reverse()
    return steps


def _phrase_letters(phrase: str) -> list[str]:
    """For each letter of a phrase's skeleton, the character of the phrase it
    came from: "Weiß" gives W, E, I, ß, ß."""
    letters: list[str] = []
    for ch in phrase:
        if unicodedata.combining(ch):
            continue
        folded, _ = _letters(ch)
        letters.extend(ch for _ in folded)
    return letters


def _read_char(stream: _Stream, k: int) -> str:
    line, offset = stream.origin[k]
    return stream.lines[line].text[offset]


def _snap(
    stream: _Stream,
    pattern: str,
    p_umlaut: list[bool],
    start: int,
    end: int,
    budget: int,
    lookalikes: int,
) -> tuple[int, int, int] | None:
    """The hit moved onto word boundaries, or ``None`` if it cannot be.

    Two alignments often cost the same — "Opperation" read as "Operation" can
    drop the first P or turn the O into the second one — and the one the
    alignment kept may start inside the word. Nearby boundaries are tried, and
    the cheapest reading that starts and ends on one is kept.
    """
    if _boundary(stream, start) and _boundary(stream, end):
        fitted = _fit(pattern, p_umlaut, stream.text[start:end], stream.umlauts[start:end])
        return start, end, fitted
    reach = budget + 1
    starts = [s for s in range(max(0, start - reach), start + reach + 1) if _boundary(stream, s)]
    ends = [
        e
        for e in range(max(start + 1, end - reach), min(len(stream.chars), end + reach) + 1)
        if _boundary(stream, e)
    ]
    best: tuple[int, int, int] | None = None
    for s in starts:
        for e in ends:
            if e - s < max(1, len(pattern) - budget):
                continue
            c = _fit(pattern, p_umlaut, stream.text[s:e], stream.umlauts[s:e])
            if _within(c, budget, lookalikes) and (best is None or c < best[2]):
                best = (s, e, c)
    return best


def _short_words_kept(
    pattern: str, breaks: list[bool], stream: _Stream, start: int, end: int
) -> bool:
    """Whether the one- and two-letter words of a phrase — an initial, a house
    number, the X4 of "Sentinel X4" — were read as written.

    The fuzzy budget is meant for long words. Spent on an initial, it makes
    "Schöllhorn" and "Ms Schöllhorn" into "M. Schöllhorn": a letter dropped or
    added, well within the budget of an eleven-letter phrase, and a different
    person.
    """
    words: list[str] = []
    for c, starts in zip(pattern, breaks):
        if starts or not words:
            words.append(c)
        else:
            words[-1] += c
    if len(words) < 2 or all(len(w) > 2 for w in words):
        return True
    tokens = [""]
    for k in range(start, end):
        if k > start and stream.gaps[k] not in (_NONE, _SPACED, _HYPHEN, _DIGIT):
            tokens.append("")
        tokens[-1] += stream.chars[k]
    words = [w.translate(_CANON) for w in words]
    tokens = [t.translate(_CANON) for t in tokens]
    for i, word in enumerate(words):
        if len(word) > 2:
            continue
        if len(tokens) == len(words):
            kept = tokens[i] == word
        elif i == 0:
            kept = tokens[0].startswith(word)
        elif i == len(words) - 1:
            kept = tokens[-1].endswith(word)
        else:
            kept = any(word in t for t in tokens)
        if not kept:
            return False
    return True


def _same_case(
    wanted: list[str], steps: list[tuple[str, int, int, int, int]], stream: _Stream, start: int
) -> bool:
    """For a case-sensitive term: whether every letter read in place of one of
    the term's has its case — along the alignment, so "MUELLER" for "Müller"
    and "ADLLER" for "Adler" are caught as well as "ADLER"."""
    for kind, p0, p1, t0, t1 in steps:
        if kind in ("missing", "extra"):
            continue
        for p in range(p0, p1):
            for t in range(t0, t1):
                a, b = wanted[p], _read_char(stream, start + t)
                if a.isalpha() and b.isalpha() and a.isupper() != b.isupper():
                    return False
    return True


def _sharp_s_spelled(
    wanted: list[str], steps: list[tuple[str, int, int, int, int]], stream: _Stream, start: int
) -> bool:
    """Whether an ß of the term was read as "ss", or "ss" as an ß: the same
    letters once folded, but a spelling all the same."""
    for kind, p0, _, t0, _ in steps:
        if kind == "same":
            sharp_term = wanted[p0] in "ßẞ"
            sharp_read = _read_char(stream, start + t0) in "ßẞ"
            if sharp_term != sharp_read:
                return True
    return False


def _hit(
    term: Term,
    phrase: str,
    page: Page,
    stream: _Stream,
    start: int,
    end: int,
    cost: int,
    breaks: list[bool],
    length: int,
) -> Hit:
    edits, rest = divmod(cost, EDIT)
    lookalikes, spellings = divmod(rest, LOOK)
    how: set[str] = set()
    if edits:
        how.add("fuzzy")
    if lookalikes:
        how.add("ocr")
    if spellings:
        how.add("spelling")
    term_breaks = sum(breaks)
    text_breaks = 0
    for k in range(start + 1, end):
        gap = stream.gaps[k]
        if gap == _SPACED:
            how.add("spaced")
        elif gap == _HYPHEN:
            how.add("hyphenated")
        elif gap in (_LINE, _BLOCK):
            how.add("split")
            text_breaks += 1
        elif gap in (_SPACE, _PUNCT):
            text_breaks += 1
    if not how & {"spaced", "hyphenated"}:
        if text_breaks > term_breaks:
            how.add("broken")
        elif text_breaks < term_breaks:
            how.add("glued")
    if not how:
        how.add("exact")
    score = max(0.0, 1.0 - (edits + 0.25 * lookalikes) / max(length, 1))
    boxes, text = _span_boxes(stream, start, end)
    first_line = stream.origin[start][0]
    return Hit(
        term=term.name,
        category=term.category,
        severity=term.severity,
        pattern=phrase,
        text=text,
        page=page.index + 1,
        box=_union(boxes),
        boxes=tuple(boxes),
        how=tuple(sorted(how)),
        score=score,
        zone=stream.zones[first_line],
    )


def _span_boxes(stream: _Stream, start: int, end: int) -> tuple[list[Box], str]:
    """One box per line the stream positions `start:end` touch, and the text."""
    per_line: dict[int, list[int]] = {}
    for k in range(start, end):
        line, offset = stream.origin[k]
        per_line.setdefault(line, []).append(offset)
    boxes: list[Box] = []
    parts: list[str] = []
    for line_no, offsets in per_line.items():
        line = stream.lines[line_no]
        lo, hi = min(offsets), max(offsets) + 1
        boxes.extend(_boxes_for_span(line, lo, hi))
        parts.append(line.text[lo:hi])
    return boxes, "\n".join(parts)


def _union(boxes: Sequence[Box]) -> Box:
    return Box(
        min(b.x0 for b in boxes),
        min(b.y0 for b in boxes),
        max(b.x1 for b in boxes),
        max(b.y1 for b in boxes),
    )


@dataclass
class _Flat:
    """The page's text with line breaks, hyphenation and letter-spacing undone,
    for regular expressions, and where each character came from."""

    text: str
    origin: list[tuple[int, int] | None]


def _flat(stream: _Stream, close_lines: bool = False, compact: bool = False) -> _Flat:
    chars: list[str] = []
    origin: list[tuple[int, int] | None] = []
    for number, line in enumerate(stream.lines):
        text = line.text
        spaced = _spaced_positions(text) if not compact else _beside_singles(text)
        tail = text.rstrip()
        following = stream.lines[number + 1].text.lstrip() if number + 1 < len(stream.lines) else ""
        # A word hyphenated at the line end goes on, lower case, in the next
        # line, and loses its hyphen: "Kunden-" / "nummer". With `close_lines`
        # anything else that ends in a letter, a digit or a dash goes on too,
        # the dash kept: "KD-" / "123456" is "KD-123456".
        hyphenated = (
            len(tail) > 1 and tail[-1] in _DASHES and tail[-2].isalpha() and following[:1].islower()
        )
        joined = hyphenated or (
            close_lines and bool(tail) and (tail[-1].isalnum() or tail[-1] in _DASHES)
        )
        if hyphenated:
            kept = tail[:-1]  # the hyphen goes, the word goes on
        elif joined:
            kept = tail
        else:
            kept = text
        for i, ch in enumerate(kept):
            if ch == " " and i + 1 in spaced:
                continue  # letter-spacing: this space splits no word
            chars.append(ch)
            origin.append((number, i))
        if not joined:
            chars.append(" ")
            origin.append(None)
    return _Flat("".join(chars), origin)


def _continues(
    stream: _Stream, flat: _Flat, neighbour: int, spot: tuple[int, int], step: int
) -> bool:
    """Whether the printed text goes on in a letter or digit beyond this end of
    a regex match (`step` -1 before it, +1 after it): "KD-1234567" and
    "XKD-123456" are other numbers than "KD-123456"."""
    line, offset = spot
    text = stream.lines[line].text
    edge = text[offset]
    if not edge.isalnum():
        return False
    k = offset + step
    if 0 <= k < len(text) and text[k].isalnum():
        return True
    # A comb field is read with its spaces taken out ("K D - 4 3 8 3 0 0"):
    # there a digit next to a digit goes on, a word after the number does not.
    if 0 <= neighbour < len(flat.text):
        beside = flat.text[neighbour]
        return beside.isalnum() and beside.isdigit() == edge.isdigit()
    return False


def _beside_singles(text: str) -> set[int]:
    """Offsets just after every space that touches a one-character token, so
    that `_flat` drops those spaces: "KD- 5 6 6 0 8 9" reads "KD-566089"."""
    tokens = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    after: set[int] = set()
    for (s1, e1), (s2, e2) in zip(tokens, tokens[1:]):
        if e1 - s1 == 1 or e2 - s2 == 1:
            after.update(range(e1 + 1, s2 + 1))
    return after


def _regex_hits(
    term: Term, pattern: str, page: Page, stream: _Stream, flat: _Flat
) -> Iterator[_Found]:
    flags = re.UNICODE | (0 if term.case else re.IGNORECASE)
    for m in re.finditer(pattern, flat.text, flags):
        spots = [o for o in flat.origin[m.start() : m.end()] if o is not None]
        if not spots:
            continue
        if term.whole_words and (
            _continues(stream, flat, m.start() - 1, spots[0], -1)
            or _continues(stream, flat, m.end(), spots[-1], +1)
        ):
            continue
        per_line: dict[int, list[int]] = {}
        for line, offset in spots:
            per_line.setdefault(line, []).append(offset)
        boxes = [
            box
            for n, o in per_line.items()
            for box in _boxes_for_span(stream.lines[n], min(o), max(o) + 1)
        ]
        text = "\n".join(stream.lines[n].text[min(o) : max(o) + 1] for n, o in per_line.items())
        how = {"regex"}
        if len(per_line) > 1:
            how.add("split")
        positions = [stream.position(o) for o in spots]
        known = [k for k in positions if k is not None]
        span = (min(known), max(known) + 1) if known else (0, 0)
        hit = Hit(
            term=term.name,
            category=term.category,
            severity=term.severity,
            pattern=pattern,
            text=text,
            page=page.index + 1,
            box=_union(boxes),
            boxes=tuple(boxes),
            how=tuple(sorted(how)),
            score=1.0,
            zone=stream.zones[spots[0][0]],
        )
        yield _Found(hit, stream, span[0], span[1], _places(stream, spots))


def _near_ok(term: Term, item: _Found) -> bool:
    """`near`: one of its words within `window` letters to either side of the
    hit. The hit itself is not context — "Adler" does not stand near "Adler".

    Measured in the letter stream, so a context word counts across a line
    break just as the term does.
    """
    if not term.near:
        return True
    text, start, end = item.stream.text, item.start, item.end
    around = text[max(0, start - term.window) : start] + " " + text[end : end + term.window]
    return any(_skeleton_of(w)[0] in around for w in term.near)


def _not_near_ok(term: Term, item: _Found) -> bool:
    """`not_near`: none of its words within `window` letters of the hit — or
    around it: a `not_near` word the hit is part of ("Adlerhorst" for a term
    that may sit inside words) rules it out too. One inside the hit's own
    letters ("Kran" in "Kranich") does not."""
    if not term.not_near:
        return True
    text, start, end = item.stream.text, item.start, item.end
    lo = max(0, start - term.window)
    whole = text[lo : end + term.window]
    inside = (start - lo, end - lo)
    for word in term.not_near:
        needle = _skeleton_of(word)[0]
        at = whole.find(needle)
        while at >= 0:
            if at < inside[0] or at + len(needle) > inside[1]:
                return False
            at = whole.find(needle, at + 1)
    return True
