"""Finds security-classification markings in a scanned document.

German *Verschlusssachen* first — VS-NUR FÜR DEN DIENSTGEBRAUCH, VS-VERTRAULICH,
GEHEIM, STRENG GEHEIM — and the markings that stand level with them: NATO, EU,
Austria, Switzerland, the United States, the United Kingdom and France, plus the
Traffic Light Protocol and the markings companies put on their own papers.

>>> doc = ocrust.scan("akte.pdf")                     # doctest: +SKIP
>>> report = doc.markings()                           # doctest: +SKIP
>>> report.label, report.level, report.unmarked_pages  # doctest: +SKIP
('VS-NfD', 1, (4,))

Finding the words is the easy half. The hard half is telling a *marking* — the
grade stamped on a page, which the VSA puts at the top and bottom of every page
— from a *mention*: a directive explaining how VS-NfD papers are handled, a
brochure for a product "zugelassen für VS-NfD", a letter about the
Betriebsgeheimnis or a geheime Wahl. A marking stands on its own: alone on its
line, in the header or footer band, or beside nothing but a page number, a copy
number or a field label. A mention sits in running text, among the lowercase
words a sentence cannot do without. Words that are also ordinary language —
GEHEIM, VERTRAULICH, INTERN, SECRET, CONFIDENTIAL — count only in capitals.

Every finding carries its evidence (page, box, the line as read, why it was
judged a marking or a mention, whether an OCR confusion had to be undone), so a
result can be checked rather than trusted.

Grades are compared on one scale, the one the bilateral security agreements
use: 1 restricted (VS-NfD, NATO RESTRICTED, RESTREINT UE), 2 confidential
(VS-VERTRAULICH), 3 secret (GEHEIM), 4 top secret (STRENG GEHEIM). TLP and
company markings are reported beside that scale, not on it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ._types import Box, Document, Line, Page, _box_for_span

__all__ = [
    "LEVELS",
    "Finding",
    "MarkingReport",
    "inspect",
    "level_for",
]

#: The common scale, named after the German grades.
LEVELS: dict[int, str] = {
    0: "offen",
    1: "VS-NfD",
    2: "VS-VERTRAULICH",
    3: "GEHEIM",
    4: "STRENG GEHEIM",
}

#: Names accepted wherever a threshold is given (`--fail-on`, :func:`level_for`).
_LEVEL_ALIASES: dict[str, int] = {
    "vs-nfd": 1,
    "nfd": 1,
    "restricted": 1,
    "1": 1,
    "vs-v": 2,
    "vs-vertraulich": 2,
    "vertraulich": 2,
    "confidential": 2,
    "2": 2,
    "geheim": 3,
    "secret": 3,
    "3": 3,
    "streng-geheim": 4,
    "streng geheim": 4,
    "sg": 4,
    "top-secret": 4,
    "top secret": 4,
    "4": 4,
}

_TLP_ORDER = ("CLEAR", "GREEN", "AMBER", "AMBER+STRICT", "RED")
_COMPANY_ORDER = {
    "INTERNAL": 1,
    "INTERN": 1,
    "CONFIDENTIAL": 2,
    "VERTRAULICH": 2,
    "GESCHÄFTSGEHEIMNIS": 3,
    "STRENG VERTRAULICH": 3,
}


def level_for(name: str) -> int:
    """The level a grade name stands for: ``level_for("vs-nfd") == 1``.

    Accepts the German grades, their English equivalents and the digits 1–4,
    in any case.
    """
    key = " ".join(name.strip().lower().replace("_", "-").split())
    if key in _LEVEL_ALIASES:
        return _LEVEL_ALIASES[key]
    raise ValueError(f"unknown grade {name!r}; use vs-nfd, vs-v, geheim or streng-geheim (or 1-4)")


@dataclass(frozen=True)
class Finding:
    """One marking, or one mention of a marking, and the evidence for it."""

    #: ``"marking"`` when the grade is stamped on the page, ``"mention"`` when
    #: running text merely talks about it.
    kind: str
    #: ``de``, ``at``, ``ch``, ``ddr``, ``nato``, ``eu``, ``us``, ``uk``, ``fr``,
    #: ``intl`` (a grade no context tied to one country), ``tlp`` or ``company``.
    scheme: str
    #: Position on the common scale, 0–4; 0 for TLP, company markings and
    #: explicitly unclassified ones such as NATO UNCLASSIFIED.
    level: int
    #: Canonical name: ``"VS-NfD"``, ``"NATO SECRET"``, ``"TLP:AMBER"``, …
    label: str
    #: The whole line as it was read.
    text: str
    #: The part of the line that matched.
    match: str
    #: 1-based page number.
    page: int
    box: Box
    confidence: float
    #: True when an OCR confusion had to be undone to read it (``V5-NFD``,
    #: ``GEHElM``); worth a human look.
    fuzzy: bool
    #: Why it is a marking or a mention: ``header``, ``footer``, ``standalone``,
    #: ``short line``, ``running text``, ``table``, ``list of grades``, …
    reason: str
    #: Handling caveats on the same line: ``NOFORN``, ``ATOMAL``, ``REL TO …``.
    caveats: tuple[str, ...] = ()
    #: The same line says the grade was lifted or lowered ("aufgehoben").
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scheme": self.scheme,
            "level": self.level,
            "label": self.label,
            "text": self.text,
            "match": self.match,
            "page": self.page,
            "box": list(self.box.as_tuple()),
            "confidence": round(self.confidence, 4),
            "fuzzy": self.fuzzy,
            "reason": self.reason,
            "caveats": list(self.caveats),
            "cancelled": self.cancelled,
        }


@dataclass(frozen=True)
class MarkingReport:
    """What :func:`inspect` found in one document."""

    source: str
    findings: tuple[Finding, ...]
    #: Per page, in page order, the highest grade marked on it (0 for none).
    pages: tuple[int, ...]
    #: A line on some page says a grade was lifted or lowered.
    cancelled: bool = False
    #: The 1-based number of each entry in :attr:`pages` within the source
    #: document; differs from the position when only some pages were scanned.
    page_numbers: tuple[int, ...] = ()

    @property
    def markings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.kind == "marking")

    @property
    def mentions(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.kind == "mention")

    @property
    def level(self) -> int:
        """The highest grade marked anywhere in the document, 0 for none."""
        return max((f.level for f in self._graded()), default=0)

    @property
    def label(self) -> str | None:
        """Canonical name of the highest grade, or ``None`` when unmarked.

        When several names share the top level (``GEHEIM`` in the header,
        ``NATO SECRET`` in a stamp), the one read most often wins, and a clean
        read beats one that needed an OCR confusion undone.
        """
        top = [f for f in self._graded() if f.level == self.level]
        if not top:
            return None
        tally: dict[str, tuple[int, int]] = {}
        for f in top:
            clean, seen = tally.get(f.label, (0, 0))
            tally[f.label] = (clean + (not f.fuzzy), seen + 1)
        return max(tally, key=lambda label: tally[label])

    @property
    def classified(self) -> bool:
        """True when a government grade of level 1 or above is marked."""
        return self.level >= 1

    @property
    def tlp(self) -> str | None:
        """The most restrictive TLP colour marked, e.g. ``"AMBER+STRICT"``."""
        colours = [f.label.split(":", 1)[1] for f in self.markings if f.scheme == "tlp"]
        return max(colours, key=_TLP_ORDER.index) if colours else None

    @property
    def company(self) -> str | None:
        """The strongest company marking, e.g. ``"STRENG VERTRAULICH"``."""
        found = [f.label for f in self.markings if f.scheme == "company"]
        return max(found, key=lambda label: _COMPANY_ORDER.get(label, 0)) if found else None

    @property
    def unmarked_pages(self) -> tuple[int, ...]:
        """1-based pages that carry no grade in a document that has one.

        The VSA wants the grade on every page, so a gap is either a page that
        was left unmarked or one the scan could not read — both worth a look.
        """
        if self.level < 1 or len(self.pages) < 2:
            return ()
        numbers = self.page_numbers or tuple(range(1, len(self.pages) + 1))
        return tuple(n for n, level in zip(numbers, self.pages) if level == 0)

    @property
    def caveats(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for f in self.markings:
            for caveat in f.caveats:
                seen.setdefault(caveat, None)
        return tuple(seen)

    def at_least(self, level: int | str) -> bool:
        """Whether the document is marked at `level` or above."""
        wanted = level_for(level) if isinstance(level, str) else level
        return self.level >= wanted

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "level": self.level,
            "label": self.label,
            "classified": self.classified,
            "tlp": self.tlp,
            "company": self.company,
            "pages": list(self.pages),
            "page_numbers": list(self.page_numbers),
            "unmarked_pages": list(self.unmarked_pages),
            "cancelled": self.cancelled,
            "caveats": list(self.caveats),
            "findings": [f.to_dict() for f in self.findings],
        }

    def _graded(self) -> list[Finding]:
        return [f for f in self.markings if f.scheme not in ("tlp", "company")]


# ------------------------------------------------------------------ the terms


@dataclass(frozen=True)
class _Term:
    pattern: re.Pattern[str]
    #: Resolved against the document's context by `_resolve`.
    key: str
    #: The words are not ordinary language, so they count in any case and
    #: anywhere a sentence does not surround them. Ordinary words (GEHEIM,
    #: VERTRAULICH) must be in capitals and must stand nearly alone.
    distinctive: bool
    #: Only in the view that undoes OCR confusions.
    fuzzy_only: bool = False
    #: An abbreviation (NFD, GVS, CUI) means something else in running text
    #: far more often than it names a grade, so it is reported only as a marking.
    marking_only: bool = False


def _t(
    regex: str,
    key: str,
    distinctive: bool = True,
    fuzzy_only: bool = False,
    marking_only: bool = False,
) -> _Term:
    return _Term(re.compile(regex), key, distinctive, fuzzy_only, marking_only)


# Written against the folded text: capitals, no diacritics, every dash a
# hyphen, ß as SS. `\s*` rather than `\s+` between words, because letter-spaced
# stamps are read with their spaces removed.
_S = r"\s*"
_NFD = r"N\s*\.?\s*F\s*\.?\s*D\b\.?"
_NUR_FUER_DEN_DIENSTGEBRAUCH = rf"NUR{_S}FUE?R{_S}DEN{_S}DIENST{_S}-?{_S}GEBRAUCH"

_TERMS: tuple[_Term, ...] = (
    # --- Germany (VSA 2023) and its predecessors
    _t(
        rf"\b(?:(?:VS|VERSCHLUS{{2,3}}ACHE){_S}-?{_S})?{_NUR_FUER_DEN_DIENSTGEBRAUCH}\b",
        "de:1",
    ),
    _t(rf"\bVS{_S}-?{_S}{_NFD}", "de:1"),
    _t(r"\bVS\s*[-.]?\s*N\s*\.?\s*[FTIL]\s*\.?\s*[DO]\b", "de:1", fuzzy_only=True),
    _t(rf"\bVS{_S}-{_S}VERTRAULICH\b|\bVS{_S}VERTRAULICH\b", "de:2"),
    _t(rf"\bAMTLICH{_S}GEHEIM{_S}GEHALTEN\b", "de:3:historic"),
    _t(rf"\bSTRENG{_S}GEHEIM\b", "ctx:4", distinctive=False),
    _t(r"(?<![A-Z])GEHEIM(?![A-Z])", "ctx:3", distinctive=False),
    _t(r"\bVERSCHLUS{2,3}ACHE\b", "de:vs"),
    _t(r"(?<![A-Z])N\.?\s?F\.?\s?D\.?(?![A-Z])", "de:1:nfd", False, marking_only=True),
    # --- DDR
    _t(rf"\bVERTRAULICHE{_S}VERSCHLUS{{2,3}}ACHE\b", "ddr:2"),
    _t(rf"\bGEHEIME{_S}VERSCHLUS{{2,3}}ACHE\b", "ddr:3"),
    _t(rf"\bVERTRAULICHE{_S}DIENSTSACHE\b", "ddr:1"),
    _t(r"(?<![A-Z])VVS(?![A-Z])", "ddr:2:abbr", False, marking_only=True),
    _t(r"(?<![A-Z])GVS(?![A-Z])", "ddr:3:abbr", False, marking_only=True),
    # --- Austria and Switzerland (grades shared with Germany resolve by context)
    _t(r"(?<![A-Z])EINGESCHRANKT(?![A-Z])", "at:1", distinctive=False),
    _t(r"(?<![A-Z])VERTRAULICH(?![A-Z])", "ctx:vertraulich", distinctive=False),
    _t(r"(?<![A-Z])INTERN(?![A-Z])", "ctx:intern", distinctive=False),
    # --- NATO
    _t(rf"\bNATO{_S}UNCLASSIFIED\b", "nato:0"),
    _t(rf"\bNATO{_S}RESTRICTED\b", "nato:1"),
    _t(rf"\bNATO{_S}CONFIDENTIAL\b", "nato:2"),
    _t(rf"\bNATO{_S}SECRET\b", "nato:3"),
    _t(rf"\bCOSMIC{_S}TOP{_S}SECRET\b", "nato:4"),
    # --- EU (Council Decision 2013/488/EU): French and English forms
    _t(rf"\bRESTREINT{_S}UE\b|\bEU{_S}RESTRICTED\b", "eu:1"),
    _t(rf"\bCONFIDENTIEL{_S}UE\b|\bEU{_S}CONFIDENTIAL\b", "eu:2"),
    _t(rf"\bSECRET{_S}UE\b|\bEU{_S}SECRET\b", "eu:3"),
    _t(rf"\bTRES{_S}SECRET{_S}UE\b|\bEU{_S}TOP{_S}SECRET\b", "eu:4"),
    # --- France (IGI 1300; the -DÉFENSE forms predate the 2021 reform)
    _t(rf"\bDIFFUSION{_S}RESTREINTE\b", "fr:1"),
    _t(rf"\bCONFIDENTIEL{_S}DEFENSE\b", "fr:2"),
    _t(rf"\bSECRET{_S}DEFENSE\b", "fr:3"),
    _t(rf"\bTRES{_S}SECRET{_S}DEFENSE\b", "fr:4"),
    _t(rf"(?<![A-Z])TRES{_S}SECRET(?![A-Z])", "fr:4:bare", distinctive=False),
    # --- United Kingdom and United States
    _t(rf"\bOFFICIAL{_S}-{_S}SENSITIVE\b", "uk:1"),
    _t(rf"\bFOR{_S}OFFICIAL{_S}USE{_S}ONLY\b", "us:1:fouo"),
    _t(rf"\bCONTROLLED{_S}UNCLASSIFIED{_S}INFORMATION\b", "us:1:cui"),
    _t(r"(?<![A-Z])CUI(?![A-Z])", "us:1:cui", False, marking_only=True),
    _t(rf"(?<![A-Z])TOP{_S}SECRET(?![A-Z])", "ctx:4:en", distinctive=False),
    _t(r"(?<![A-Z])SECRET(?![A-Z])", "ctx:3:en", distinctive=False),
    _t(r"(?<![A-Z])CONFIDENTIAL(?![A-Z])", "ctx:confidential", distinctive=False),
    # --- Traffic Light Protocol (FIRST, 2.0; WHITE is 1.0's CLEAR)
    _t(r"\bTLP\s*[:.]?\s*(?:RED|AMBER\s*\+\s*STRICT|AMBER|GREEN|CLEAR|WHITE)\b", "tlp"),
    # --- Company markings
    _t(rf"\bSTRENG{_S}VERTRAULICH\b|\bSTRICTLY{_S}CONFIDENTIAL\b", "company:STRENG VERTRAULICH"),
    _t(rf"\bNUR{_S}FUE?R{_S}DEN{_S}INTERNEN{_S}GEBRAUCH\b", "company:INTERN"),
    _t(rf"\bFOR{_S}INTERNAL{_S}USE{_S}ONLY\b|\bINTERNAL{_S}USE{_S}ONLY\b", "company:INTERNAL"),
    _t(r"(?<![A-Z])INTERNAL(?![A-Z])", "company:INTERNAL", distinctive=False),
    _t(r"(?<![A-Z])GESCHAFTSGEHEIMNIS(?![A-Z])", "company:GESCHÄFTSGEHEIMNIS", distinctive=False),
)

#: Words OCR tends to damage, for repairing a token one or two edits away.
_VOCABULARY = (
    "VERTRAULICH",
    "DIENSTGEBRAUCH",
    "GEHEIM",
    "STRENG",
    "VERSCHLUSSSACHE",
    "EINGESCHRANKT",
    "RESTRICTED",
    "RESTREINT",
    "CONFIDENTIAL",
    "CONFIDENTIEL",
    "UNCLASSIFIED",
    "COSMIC",
    "DIFFUSION",
    "RESTREINTE",
)

# Things that stand beside a marking without making it running text.
_ACCESSORIES = re.compile(
    r"\b(?:SEITE|PAGE|BLATT|BL\.|S\.)\s*\d+(?:\s*(?:VON|OF|/|AUS)\s*\d+)?"
    r"|\b(?:KOPIE|AUSFERTIGUNG|EXEMPLAR|EXPL\.|COPY|AUSF\.)\s*(?:NR\.?|NO\.?|N°)?\s*\d+"
    r"(?:\s*(?:VON|OF|/)\s*\d+)?"
    r"|\bTGB\.?\s*-?\s*NR\.?\s*[\w/.-]*"
    r"|\b(?:VS-)?(?:GEHEIMHALTUNGSGRAD|EINSTUFUNG|KLASSIFIZIERUNG|KLASSIFIKATION|SCHUTZSTUFE|"
    r"SICHERHEITSSTUFE|VS-GRAD|KENNZEICHNUNG|SECURITY\s+CLASSIFICATION|CLASSIFICATION|"
    r"CLASSIFIED|CLASSIFICATION\s+LEVEL|SENSITIVITY|VERTRAULICHKEIT|SCHUTZKLASSE)\b\s*:?"
    r"|\b(?:ENTWURF|KOPIE|ABSCHRIFT|DUPLIKAT|DRAFT|COPY|DOPPEL|AUSZUG|ANLAGE\s*\d*)\b"
    r"|\b(?:AUFGEHOBEN|HERABGESTUFT|ENTSTUFT|DECLASSIFIED|DOWNGRADED|DECLASSIFIE|"
    r"EINSTUFUNG\s+AUFGEHOBEN)\b"
    r"|\b(?:AB|BIS|UNTIL|AM|DEN|ZUM)?\s*\d{1,2}\.\s*\d{1,2}\.\s*\d{2,4}"
    r"|\d+"
)

_CANCELLATION = re.compile(
    r"\b(?:AUFGEHOBEN|HERABGESTUFT|ENTSTUFT|DECLASSIFIED|DOWNGRADED|DECLASSIFIE)\b"
)
_CAVEATS = re.compile(
    r"\b(?:NOFORN|ORCON|PROPIN|RELIDO|IMCON|FVEY|ATOMAL|CRYPTO|BOHEMIA|BALK|EXDIS|LIMDIS|"
    r"SPERRVERMERK|NUR\s+FUE?R\s+DEUTSCHE|NICHT\s+FUE?R\s+AUSLANDER)\b"
    r"|\bREL(?:EASABLE)?\s+TO\s+[A-Z]{2,}(?:\s*,\s*[A-Z]{2,})*"
)
_NEGATION = re.compile(r"\b(?:NICHT|KEIN|KEINE|KEINER|NOT|NON|NIE|NEVER|OHNE)\b")
_LETTERS = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-ž]{2,}")

# Country cues decide what an ordinary grade word means: VERTRAULICH is a grade
# in Vienna and Bern, and a company's marking in Munich.
_CUES = {
    "at": re.compile(
        r"\b(?:REPUBLIK\s+OSTERREICH|OSTERREICH\w*|WIEN|BUNDESHEER|BMLV|INFOSIG|INFOSIV|"
        r"GZ\s*[:.]?\s*\d|LANDESVERTEIDIGUNG|BUNDESKANZLERAMT\s+OSTERREICH)\b"
    ),
    "ch": re.compile(
        r"\b(?:SCHWEIZ\w*|EIDGENOSSI\w*|CONFEDERATION|BERN|VBS|ISCHV|ISV|INFOSIV|"
        r"BUNDESKANZLEI|KANTON\w*|ARMEE\s+SUISSE|FEDPOL|NDB)\b"
    ),
    "us": re.compile(
        r"\b(?:CLASSIFIED\s+BY|DECLASSIFY\s+ON|DERIVED\s+FROM|DEPARTMENT\s+OF|UNITED\s+STATES|"
        r"U\.S\.|WASHINGTON|PENTAGON)\b"
    ),
    "uk": re.compile(
        r"\b(?:HM\s+GOVERNMENT|MINISTRY\s+OF\s+DEFENCE|CROWN\s+COPYRIGHT|WHITEHALL)\b"
    ),
    "fr": re.compile(r"\b(?:REPUBLIQUE\s+FRANCAISE|MINISTERE|IGI\s*1300|SGDSN)\b"),
}

_DASHES = set("‐‑‒–—―−﹘﹣－")
#: How far into the page the header and footer bands reach.
_BAND = 0.12


# ------------------------------------------------------------------ folding


def _fold_char(ch: str) -> str:
    if ch in _DASHES:
        return "-"
    decomposed = unicodedata.normalize("NFKD", ch)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).upper()
    return plain.replace("ẞ", "SS")


class _View:
    """A rewritten line that still knows where each character came from."""

    def __init__(self, text: str, origin: list[int], fuzzy: bool = False) -> None:
        self.text = text
        self.origin = origin
        self.fuzzy = fuzzy

    def span(self, start: int, end: int) -> tuple[int, int]:
        """The original characters behind `text[start:end]`."""
        return self.origin[start], self.origin[end - 1] + 1


def _folded(text: str) -> _View:
    chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        folded = _fold_char(ch)
        chars.append(folded)
        origin.extend([i] * len(folded))
    return _View("".join(chars), origin)


def _letter_spaced(view: _View) -> _View | None:
    """``G E H E I M`` as ``GEHEIM``, for stamps set with spaced letters."""
    tokens = view.text.split()
    if len(tokens) < 4:
        return None
    singles = sum(len(t) == 1 for t in tokens)
    if singles < 0.6 * len(tokens):
        return None
    kept = [(c, o) for c, o in zip(view.text, view.origin) if not c.isspace()]
    return _View("".join(c for c, _ in kept), [o for _, o in kept])


_CONFUSIONS = {"5": "S", "0": "O", "1": "I", "|": "I", "!": "I", "$": "S"}


def _repaired(view: _View, original: str) -> _View:
    """Undoes the confusions OCR makes in capitals: ``V5-NFD``, ``GEHElM``,
    ``VERTRAUL1CH``, a letter dropped or doubled in a long word."""
    chars = list(view.text)
    for token in re.finditer(r"\S+", original):
        letters = [c for c in token.group() if c.isalpha()]
        if not letters or sum(c.isupper() for c in letters) < 0.6 * len(letters):
            continue
        for j, o in enumerate(view.origin):
            if token.start() <= o < token.end():
                src = original[o]
                if src in _CONFUSIONS:
                    chars[j] = _CONFUSIONS[src]
                elif src == "l":
                    chars[j] = "I"
    text = "".join(chars)
    origin = list(view.origin)

    out: list[str] = []
    out_origin: list[int] = []
    pos = 0
    for word in re.finditer(r"[A-Z]{5,}", text):
        target = _closest(word.group())
        if target is None or target == word.group():
            continue
        out.append(text[pos : word.start()])
        out_origin.extend(origin[pos : word.start()])
        span = origin[word.start() : word.end()]
        out.append(target)
        out_origin.extend(
            span[min(k * len(span) // len(target), len(span) - 1)] for k in range(len(target))
        )
        pos = word.end()
    out.append(text[pos:])
    out_origin.extend(origin[pos:])
    # Returned even when nothing was repaired: the patterns that only apply to
    # repaired text (VS-NtD, VS-NlD) need a view to run on.
    return _View("".join(out), out_origin, fuzzy=True)


def _closest(word: str) -> str | None:
    for target in _VOCABULARY:
        budget = 1 if len(target) < 10 else 2
        if abs(len(target) - len(word)) <= budget and _distance(word, target, budget) <= budget:
            return target
    return None


def _distance(a: str, b: str, budget: int) -> int:
    """Levenshtein distance, giving up once it exceeds `budget`."""
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        if min(current) > budget:
            return budget + 1
        previous = current
    return previous[-1]


# ------------------------------------------------------------------ matching


@dataclass
class _Hit:
    term: _Term
    view: _View
    start: int  # in the view
    end: int
    orig_start: int  # in the line's own text
    orig_end: int

    @property
    def length(self) -> int:
        return self.orig_end - self.orig_start


def _hits(line_text: str, zone: str) -> list[_Hit]:
    exact = _folded(line_text)
    views = [exact]
    spaced = _letter_spaced(exact)
    if spaced is not None:
        views.append(spaced)
    # OCR repairs only where a marking would stand: a long sentence full of
    # near-misses is exactly where repairs would invent grades.
    if zone != "body" or len(exact.text.split()) <= 8:
        views.extend([_repaired(base, line_text) for base in views])

    found: list[_Hit] = []
    for view in views:
        for term in _TERMS:
            if term.fuzzy_only and not view.fuzzy:
                continue
            for m in term.pattern.finditer(view.text):
                if m.end() == m.start():
                    continue
                s, e = view.span(m.start(), m.end())
                found.append(_Hit(term, view, m.start(), m.end(), s, e))
    return _dedupe(found)


def _dedupe(hits: list[_Hit]) -> list[_Hit]:
    """One hit per stretch of text: the longest, and a clean read over a repaired one."""
    hits.sort(key=lambda h: (-h.length, h.view.fuzzy, h.term.fuzzy_only))
    kept: list[_Hit] = []
    for hit in hits:
        if any(hit.orig_start < k.orig_end and k.orig_start < hit.orig_end for k in kept):
            continue
        kept.append(hit)
    return sorted(kept, key=lambda h: h.orig_start)


# ------------------------------------------------------------------ judging


def _zone(line: Line, page: Page) -> str:
    if page.height <= 0:
        return "body"
    middle = (line.box.y0 + line.box.y1) / 2 / page.height
    if middle < _BAND:
        return "header"
    if middle > 1 - _BAND:
        return "footer"
    return "body"


def _compound(view: _View, hit: _Hit) -> bool:
    """``VS-NfD-Zulassung``, ``GEHEIM-Unterlagen``: a word built on the grade."""
    text = view.text
    after = text[hit.end : hit.end + 2]
    before = text[max(hit.start - 2, 0) : hit.start]
    return (len(after) == 2 and after[0] == "-" and after[1].isalpha()) or (
        len(before) == 2 and before[1] == "-" and before[0].isalpha()
    )


@dataclass
class _Context:
    """What else is on the line besides the hits."""

    words: int  # words of two letters or more
    lowercase: int  # of those, starting with a lowercase letter
    negated: bool
    cancelled: bool
    caveats: tuple[str, ...]


def _context(line_text: str, hits: Sequence[_Hit]) -> _Context:
    folded = _folded(line_text)
    covered = [False] * len(line_text)
    for hit in hits:
        for i in range(hit.orig_start, hit.orig_end):
            covered[i] = True
    caveats = []
    for m in _CAVEATS.finditer(folded.text):
        s, e = folded.span(m.start(), m.end())
        caveats.append(" ".join(m.group().split()))
        for i in range(s, e):
            covered[i] = True
    for m in _ACCESSORIES.finditer(folded.text):
        if m.end() > m.start():
            s, e = folded.span(m.start(), m.end())
            for i in range(s, e):
                covered[i] = True
    words = lowercase = 0
    free: list[str] = []
    for m in _LETTERS.finditer(line_text):
        if all(covered[m.start() : m.end()]):
            continue
        words += 1
        lowercase += m.group()[0].islower()
        free.append(m.group())
    negated = bool(_NEGATION.search(_folded(" ".join(free)).text))
    return _Context(
        words=words,
        lowercase=lowercase,
        negated=negated,
        cancelled=bool(_CANCELLATION.search(folded.text)),
        caveats=tuple(caveats),
    )


def _verdict(hit: _Hit, ctx: _Context, zone: str, line_text: str) -> str | None:
    """``"marking: <reason>"``, ``"mention: <reason>"``, or ``None`` to drop it."""
    original = line_text[hit.orig_start : hit.orig_end]
    if hit.view.fuzzy:
        # A lowercase l inside capitals is the I that OCR misread (GEHElM).
        original = original.replace("l", "I")
    capitals = not any(c.islower() for c in original)
    if not hit.term.distinctive and not capitals:
        # "die Wahl ist geheim", "vertraulich behandeln": the word, not a grade.
        return None
    if _compound(hit.view, hit):
        return "mention: compound word"
    if ctx.negated:
        return "mention: negated"
    if ctx.words == 0:
        return f"marking: {zone if zone != 'body' else 'standalone'}"
    if zone != "body":
        if hit.term.distinctive and ctx.lowercase <= 1 and ctx.words <= 6:
            return f"marking: {zone}"
        if ctx.lowercase == 0 and ctx.words <= 3:
            return f"marking: {zone}"
    elif hit.term.distinctive and ctx.lowercase == 0 and ctx.words <= 2:
        return "marking: short line"
    return "mention: running text"


def _cues(document: Document) -> set[str]:
    folded = _folded(document.text).text
    return {name for name, cue in _CUES.items() if cue.search(folded)}


def _resolve(key: str, cues: set[str], matched: str) -> tuple[str, int, str] | None:
    """(scheme, level, label) for a hit, given what country the document is from."""
    parts = key.split(":")
    head = parts[0]
    country = "at" if "at" in cues else "ch" if "ch" in cues else None
    english = "us" if "us" in cues else "uk" if "uk" in cues else "fr" if "fr" in cues else None
    if head == "de":
        if parts[1] == "vs":
            return "de", 1, "VS"
        level = int(parts[1])
        label = LEVELS[level]
        if country == "at" and level in (3, 4):
            return "at", level, f"AT {label}"
        return "de", level, label
    if head == "ddr":
        level = int(parts[1])
        return "ddr", level, {1: "VD", 2: "VVS", 3: "GVS"}[level]
    if head == "at":
        return "at", 1, "AT EINGESCHRÄNKT"
    if head == "nato":
        level = int(parts[1])
        return (
            "nato",
            level,
            {
                0: "NATO UNCLASSIFIED",
                1: "NATO RESTRICTED",
                2: "NATO CONFIDENTIAL",
                3: "NATO SECRET",
                4: "COSMIC TOP SECRET",
            }[level],
        )
    if head == "eu":
        level = int(parts[1])
        return (
            "eu",
            level,
            {
                1: "RESTREINT UE",
                2: "CONFIDENTIEL UE",
                3: "SECRET UE",
                4: "TRÈS SECRET UE",
            }[level],
        )
    if head == "fr":
        level = int(parts[1])
        return (
            "fr",
            level,
            {
                1: "FR DIFFUSION RESTREINTE",
                2: "FR CONFIDENTIEL DÉFENSE",
                3: "FR SECRET DÉFENSE",
                4: "FR TRÈS SECRET",
            }[level],
        )
    if head == "uk":
        return "uk", 1, "UK OFFICIAL-SENSITIVE"
    if head == "us":
        return "us", 1, "US CUI" if parts[2] == "cui" else "US FOUO"
    if head == "tlp":
        colour = re.sub(r"\s+", "", matched.split("TLP", 1)[1].lstrip(" :."))
        colour = "CLEAR" if colour == "WHITE" else colour
        return "tlp", 0, f"TLP:{colour}"
    if head == "company":
        return "company", 0, parts[1]
    # "ctx": the meaning depends on where the document comes from.
    what = parts[1]
    if what in ("3", "4") and len(parts) == 2:  # GEHEIM, STRENG GEHEIM
        level = int(what)
        if country:
            return country, level, f"{country.upper()} {LEVELS[level]}"
        return "de", level, LEVELS[level]
    if what == "vertraulich":
        if country:
            return country, 2, f"{country.upper()} VERTRAULICH"
        return "company", 0, "VERTRAULICH"
    if what == "intern":
        if country == "ch":
            return "ch", 1, "CH INTERN"
        return "company", 0, "INTERN"
    if what == "confidential":
        if english in ("us", "uk"):
            return english, 2, f"{english.upper()} CONFIDENTIAL"
        return "company", 0, "CONFIDENTIAL"
    if what in ("3", "4"):  # SECRET, TOP SECRET
        level = int(what)
        name = "TOP SECRET" if level == 4 else "SECRET"
        if english:
            return english, level, f"{english.upper()} {name}"
        return "intl", level, name
    return None


def _grade_lists(findings: list[Finding]) -> list[Finding]:
    """Demotes a page that lists the grades — a directive, a training slide —
    from markings to mentions.

    Such a page shows three or more different grades standing alone in its
    body, and no grade in its header or footer, which a marked page would have.
    """
    by_page: dict[int, list[int]] = {}
    for i, f in enumerate(findings):
        if f.kind == "marking" and f.scheme not in ("tlp", "company"):
            by_page.setdefault(f.page, []).append(i)
    demote: set[int] = set()
    for idxs in by_page.values():
        body = [i for i in idxs if findings[i].reason in ("standalone", "short line")]
        banded = [i for i in idxs if findings[i].reason in ("header", "footer")]
        if not banded and len({findings[i].level for i in body}) >= 3:
            demote.update(body)
    out = list(findings)
    for i in demote:
        f = out[i]
        out[i] = Finding(**{**f.__dict__, "kind": "mention", "reason": "list of grades"})
    return out


def _pieces(line: Line) -> tuple[Line, ...]:
    """The line, or the detector boxes it was merged from, each on its own.

    A stamp set at an angle is a tall box, and one tall box can bridge two
    rows of letterhead, which the layout then reads as a single line:
    "Bundesministerium für Beispiele Referat 12 VS-NfD". Judged as that line,
    the stamp sits among lowercase words and passes for running text. Each of
    the boxes is what was actually printed as one piece, so each is judged on
    its own.
    """
    if len(line.segments) < 2:
        return (line,)
    built = []
    for segment in line.segments:
        box = segment.box
        words = tuple(
            w
            for w in line.words
            if box.x0 - 1 <= (w.box.x0 + w.box.x1) / 2 <= box.x1 + 1
            and box.y0 - 1 <= (w.box.y0 + w.box.y1) / 2 <= box.y1 + 1
        )
        built.append(
            Line(
                text=segment.text,
                box=box,
                confidence=segment.confidence,
                angle=line.angle,
                margin=line.margin,
                words=words,
            )
        )
    return tuple(built)


def inspect(document: Document) -> MarkingReport:
    """Finds the classification markings in a scanned document.

    Works on what the scan read, so it sees what a person sees on the page —
    stamps, headers, footers — whatever the file format.
    """
    cues = _cues(document)
    findings: list[Finding] = []
    page_levels: list[int] = []
    cancelled = False
    # The line the latest finding came from and where on it, for merging the
    # other half of a bilingual marking into it. The same marking on another
    # line — the footer repeating the header — stays a finding of its own.
    last_line: Line | None = None
    last_start = last_end = 0
    numbers: list[int] = []
    for page in document.pages:
        # The page's own number in the source, so a finding points at the
        # right page when only some of them were scanned.
        number = page.index + 1
        numbers.append(number)
        in_table: list[bool] = []
        pieces: list[Line] = []
        for block in page.blocks:
            for whole in block.lines:
                for piece in _pieces(whole):
                    pieces.append(piece)
                    in_table.append(block.table is not None)
        for line, tabular in zip(pieces, in_table):
            text = line.text
            if not text.strip():
                continue
            zone = _zone(line, page)
            hits = _hits(text, zone)
            ctx = _context(text, hits)
            if ctx.cancelled and ctx.words == 0:
                cancelled = True
            for hit in hits:
                verdict = _verdict(hit, ctx, zone, text)
                if verdict is None:
                    continue
                kind, reason = verdict.split(": ", 1)
                if kind != "marking" and (hit.view.fuzzy or hit.term.marking_only):
                    # A repaired word, or an abbreviation in a sentence, is far
                    # likelier to be something else than a grade being discussed.
                    continue
                if kind == "marking" and tabular and zone == "body":
                    kind, reason = "mention", "table"
                matched = hit.view.text[hit.start : hit.end]
                resolved = _resolve(hit.term.key, cues, matched)
                if resolved is None:
                    continue
                scheme, level, label = resolved
                confidence = float(line.confidence) * (0.85 if hit.view.fuzzy else 1.0)
                previous = findings[-1] if findings else None
                if (
                    previous is not None
                    and last_line is line
                    and previous.label == label
                    and previous.kind == kind
                ):
                    # Both halves of a bilingual marking — RESTREINT UE/EU
                    # RESTRICTED — are one marking, boxed as one.
                    start = min(last_start, hit.orig_start)
                    end = max(last_end, hit.orig_end)
                    findings[-1] = Finding(
                        **{
                            **previous.__dict__,
                            "match": text[start:end],
                            "box": _box_for_span(line, start, end),
                            "fuzzy": previous.fuzzy and hit.view.fuzzy,
                        }
                    )
                    last_start, last_end = start, end
                    continue
                last_line, last_start, last_end = line, hit.orig_start, hit.orig_end
                findings.append(
                    Finding(
                        kind=kind,
                        scheme=scheme,
                        level=level,
                        label=label,
                        text=text,
                        match=text[hit.orig_start : hit.orig_end],
                        page=number,
                        box=_box_for_span(line, hit.orig_start, hit.orig_end),
                        confidence=confidence,
                        fuzzy=hit.view.fuzzy,
                        reason=reason,
                        caveats=ctx.caveats if kind == "marking" else (),
                        cancelled=ctx.cancelled,
                    )
                )
                if ctx.cancelled:
                    cancelled = True
        page_levels.append(0)

    findings = _grade_lists(findings)
    position = {number: i for i, number in enumerate(numbers)}
    for f in findings:
        if f.kind == "marking" and f.scheme not in ("tlp", "company"):
            i = position[f.page]
            page_levels[i] = max(page_levels[i], f.level)
    return MarkingReport(
        source=document.source,
        findings=tuple(findings),
        pages=tuple(page_levels),
        cancelled=cancelled,
        page_numbers=tuple(numbers),
    )


def inspect_many(documents: Iterable[Document]) -> Iterable[MarkingReport]:
    """:func:`inspect` over several documents, lazily."""
    for document in documents:
        yield inspect(document)
