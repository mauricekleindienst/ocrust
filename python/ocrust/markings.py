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

import os
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
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
    "RESTRICTED": 2,
    "CONFIDENTIAL": 2,
    "VERTRAULICH": 2,
    "GESCHÄFTSGEHEIMNIS": 3,
    "STRENG VERTRAULICH": 3,
}


#: Labels that say a document is classified without naming the grade.
_LOWER_BOUNDS = frozenset({"VS", "VS (amtlich geheimgehalten)", "US CLASSIFIED"})


def label_names_grade(label: str) -> int:
    """1 when `label` names a grade, 0 when it only bounds one from below."""
    return int(label not in _LOWER_BOUNDS)


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
    #: 1-based page number; 0 for evidence from the file itself (its
    #: sensitivity label, its name) rather than from a page.
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
            "box": [round(v, 1) for v in self.box.as_tuple()],
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
        ``NATO SECRET`` in a stamp), a name beats a lower bound ("amtlich
        geheimgehalten" says VS-VERTRAULICH or above), a clean read beats one
        that needed an OCR confusion undone, and then the one read most often
        wins.
        """
        top = [f for f in self._graded() if f.level == self.level]
        if not top:
            return None
        tally: dict[str, tuple[int, int, int]] = {}
        for f in top:
            named, clean, seen = tally.get(f.label, (0, 0, 0))
            tally[f.label] = (label_names_grade(f.label), clean + (not f.fuzzy), seen + 1)
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
    #: A line only a classified document carries, recognised by how it starts
    #: ("Die VS-Einstufung endet mit Ablauf des Jahres 2055", "Classified By:"):
    #: a marking whatever follows it.
    anchored: bool = False


def _t(
    regex: str,
    key: str,
    distinctive: bool = True,
    fuzzy_only: bool = False,
    marking_only: bool = False,
    anchored: bool = False,
) -> _Term:
    return _Term(re.compile(regex), key, distinctive, fuzzy_only, marking_only, anchored)


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
    _t(r"\bVS\s*-\s*VERTR\.", "de:2"),
    _t(r"(?<![A-Z])VS\s*-\s*V(?![A-Z-])", "de:2", False, marking_only=True),
    # Not a grade of its own: VSA 2023 puts it beside VS-VERTRAULICH and above,
    # so on its own it says "at least VS-VERTRAULICH".
    _t(
        rf"\b(?:AMTLICH|AUF{_S}AMTLICHE{_S}VERANLASSUNG){_S}GEHEIM{_S}GEHALTEN\b",
        "de:2:amtlich",
    ),
    _t(
        r"^\s*DIE\s+VS\s*-?\s*EINSTUFUNG\s+ENDET\s+MIT\s+ABLAUF\s+DES\s+JAHRES\s+\d{4}",
        "de:vs:term",
        anchored=True,
    ),
    _t(rf"\bGEHEIME{_S}KOMMANDOSACHE\b", "de:4:gkdos"),
    _t(r"(?<![A-Z])G\.?\s?KDOS\.?(?![A-Z])", "de:4:gkdos", False, marking_only=True),
    _t(rf"\bGEHEIME{_S}REICHSSACHE\b", "de:4:grs"),
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
    _t(r"(?<![A-Z])INTERNE?(?![A-Z])", "ctx:intern", distinctive=False),
    _t(rf"\bAD{_S}USO{_S}INTERNO\b", "ctx:intern"),
    _t(r"(?<![A-Z])CONFIDENZIALE(?![A-Z])", "ctx:confidenziale", distinctive=False),
    _t(r"(?<![A-Z])CONFIDENTIEL(?![A-Z])", "ctx:confidenziale", distinctive=False),
    _t(r"(?<![A-Z])SEGRETO(?![A-Z])", "ctx:segreto", distinctive=False),
    # --- Italy
    _t(r"(?<![A-Z])RISERVATO(?![A-Z])", "it:1", distinctive=False),
    _t(r"(?<![A-Z])RISERVATISSIMO(?![A-Z])", "it:2", distinctive=False),
    _t(r"(?<![A-Z])SEGRETISSIMO(?![A-Z])", "it:4", distinctive=False),
    # --- Explicitly unclassified: a marking, at level 0
    _t(r"(?<![A-Z])OFFEN(?![A-Z])", "open:de:OFFEN", False, marking_only=True),
    _t(r"(?<![A-Z])UNCLASSIFIED(?![A-Z])", "open:intl:UNCLASSIFIED", False, marking_only=True),
    _t(rf"\bNOT{_S}PROTECTIVELY{_S}MARKED\b", "open:uk:NOT PROTECTIVELY MARKED"),
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
    _t(rf"\bOFFICIAL{_S}-?{_S}SENSITIVE\b", "uk:1"),
    _t(r"(?<![A-Z])RESTRICTED(?![A-Z])", "ctx:restricted", distinctive=False),
    _t(
        r"^\s*(?:CLASSIFIED\s+BY|DECLASSIFY\s+ON|DERIVED\s+FROM)\s*:",
        "us:2:block",
        anchored=True,
    ),
    _t(rf"\bFOR{_S}OFFICIAL{_S}USE{_S}ONLY\b", "us:1:fouo"),
    _t(rf"\bCONTROLLED{_S}UNCLASSIFIED{_S}INFORMATION\b", "us:1:cui"),
    _t(r"(?<![A-Z])CUI(?![A-Z])", "us:1:cui", False, marking_only=True),
    _t(rf"(?<![A-Z])TOP{_S}SECRET(?![A-Z])", "ctx:4:en", distinctive=False),
    _t(r"(?<![A-Z])SECRET(?![A-Z])", "ctx:3:en", distinctive=False),
    _t(r"(?<![A-Z])CONFIDENTIAL(?![A-Z])", "ctx:confidential", distinctive=False),
    # --- Traffic Light Protocol (FIRST, 2.0; WHITE is 1.0's CLEAR)
    _t(r"\bTLP\s*[:.;,-]?\s*(?:RED|AMBER\s*\+\s*STRICT|AMBER|GREEN|CLEAR|WHITE)\b", "tlp"),
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
    r"\b(?:AUFGEHOBEN|HERABGESTUFT|ENTSTUFT|DECLASSIFIED|DOWNGRADED|DECLASSIFIE|SANITIZED|"
    r"PUBLICLY\s+DISCLOSED|MISE?\s+EN\s+LECTURE\s+PUBLIQUE|APPROVED\s+FOR\s+RELEASE)\b"
)
_CAVEATS = re.compile(
    r"\b(?:NOFORN|ORCON|PROPIN|RELIDO|IMCON|FVEY|ATOMAL|CRYPTO|BOHEMIA|BALK|EXDIS|LIMDIS|"
    r"SPERRVERMERK|CHEFSACHE|UK\s+EYES\s+ONLY|RECIPIENTS\s+ONLY|HMG\s+USE\s+ONLY|"
    r"SPECIAL\s+FRANCE|PERSONLICH|NUR\s+FUE?R\s+DEN\s+EMPFANGER)\b"
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
        r"\b(?:SCHWEIZ\w*|EIDGENOSSI\w*|CONFEDERATION\s+SUISSE|CONFEDERAZIONE\s+SVIZZERA|"
        r"VBS|DDPS|ISCHV|ISV|BUNDESKANZLEI|KANTON\w*|ARMEE\s+SUISSE|FEDPOL|NDB)\b"
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


_INVISIBLE = set("\u00ad\u200b\u200c\u200d\u2060\ufeff")


def _fold_char(ch: str) -> str:
    if ch in _DASHES:
        return "-"
    if ch in _INVISIBLE:
        return ""  # a soft hyphen or zero-width space splits a word only on screen
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


#: Real words one edit from a grade word; OCR did not damage them.
_NOT_REPAIRED = frozenset(
    {
        "CONFIDENTIEL",
        "DECLASSIFIED",
        "STRONG",
        "GEHEIME",
        "VERTRAULICHE",
        "EINGESCHRANKTE",
        "RESTRICTEDLY",
        "CONFIDENTIALLY",
    }
)
#: Endings that turn a grade word into an ordinary one: GEHEIME WAHL,
#: EINGESCHRÄNKTE HAFTUNG, VERTRAULICHE MITTEILUNG.
_INFLECTIONS = ("E", "EN", "ER", "ES", "EM", "S", "N", "LY")


def _closest(word: str) -> str | None:
    if word in _VOCABULARY or word in _NOT_REPAIRED:
        return None
    for target in _VOCABULARY:
        if word.startswith(target) and word[len(target) :] in _INFLECTIONS:
            return None
    best: tuple[int, str] | None = None
    for target in _VOCABULARY:
        budget = 1 if len(target) < 10 else 2
        if abs(len(target) - len(word)) > budget:
            continue
        distance = _distance(word, target, budget)
        # The nearest word, not the first one in reach: CONFIDENTIEI is one
        # edit from CONFIDENTIEL and two from CONFIDENTIAL.
        if distance <= budget and (best is None or distance < best[0]):
            best = (distance, target)
    return best[1] if best else None


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
    for m in (*_ACCESSORIES.finditer(folded.text), *_CANCELLATION.finditer(folded.text)):
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
    if hit.term.key.endswith(":nfd"):
        if "f" not in original:
            return None  # the grade is abbreviated NfD; NFD is a Unicode normal form
        capitals = True  # and that lowercase f is how it is written
    if hit.term.anchored:
        return "marking: " + ("classification block" if "block" in hit.term.key else "VS term")
    # "die Wahl ist geheim", "vertraulich behandeln": the word, not a grade.
    # An old stamp reads "Geheim!", which is why a lone word with an
    # exclamation mark still counts.
    exclaimed = line_text[hit.orig_end :].lstrip().startswith("!") and ctx.words == 0
    if not hit.term.distinctive and not capitals and not exclaimed:
        return "quiet: standalone" if ctx.words == 0 and not hit.view.fuzzy else None
    if _compound(hit.view, hit):
        return "mention: compound word"
    if ctx.negated:
        return "mention: negated"
    if ctx.words == 0:
        return f"marking: {zone if zone != 'body' else 'standalone'}"
    if hit.term.distinctive and _leads(line_text, hit):
        # "Betreff: VS-NfD – Beschaffung …": the VSA puts the grade before an
        # e-mail's subject, and a grade opening a line reads the same way.
        return "marking: subject line"
    if zone != "body":
        if hit.term.distinctive and ctx.lowercase <= 1 and ctx.words <= 6:
            return f"marking: {zone}"
        if ctx.lowercase == 0 and ctx.words <= 3:
            return f"marking: {zone}"
    elif hit.term.distinctive and ctx.lowercase == 0 and ctx.words <= 2:
        return "marking: short line"
    return "mention: running text"


#: Words that make a line about grades rather than marked with one.
_ABOUT = re.compile(
    r"\b(?:MERKBLATT|RICHTLINIE|ANWEISUNG|HANDBUCH|SCHULUNG|LEITFADEN|HINWEISE?|"
    r"VERWALTUNGSVORSCHRIFT|ZULASSUNG|ZUGELASSEN|GEEIGNET|HANDHABUNG|UMGANG|"
    r"GUIDE|GUIDANCE|POLICY|TRAINING|HANDLING|APPROVED|APPROVAL|ACCREDITED|CERTIFIED)\b"
)
#: How a line ends when the sentence goes on in the next one.
_CARRIES_ON = re.compile(
    r"(?:,|\b(?:und|oder|sowie|bzw|and|or|et|ou|für|for|des|der|den|die|als|zu|mit|bis|"
    r"von|of|at|the|to|in|im|am|auf|nach|incl|inkl))\.?\s*$",
    re.IGNORECASE,
)


def _continues(before: str, text: str) -> bool:
    """Whether `text` carries on the sentence of the line before it.

    "… NATO RESTRICTED und" / "RESTREINT UE/EU RESTRICTED": the second line
    stands alone only because the first one wrapped. The same holds when a
    grade itself is broken over the lines, "… and RESTREINT" / "UE/EU
    RESTRICTED.", which is found by reading the break as a space.
    """
    if _CARRIES_ON.search(before):
        return True
    tail = before.split()[-1] if before.split() else ""
    joined = f"{tail} {text}"
    return any(h.orig_start < len(tail) < h.orig_end for h in _hits(joined, "body"))


_LEADERS = re.compile(r"^\s*(?:(?:BETREFF|SUBJECT|OBJET|OGGETTO|AW|WG|RE|FW|FWD)\s*:\s*)+$")


def _leads(line_text: str, hit: _Hit) -> bool:
    """Whether the grade opens an e-mail's subject: ``Betreff: VS-NfD – …``.

    Only after the subject's own label. A line that merely starts with a
    grade — "CONFIDENTIEL UE/EU CONFIDENTIAL and above are registered." — is
    as likely a sentence about it.
    """
    return bool(_LEADERS.match(_folded(line_text[: hit.orig_start]).text))


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
            return "de", 1, "VS"  # a VS whose grade the line does not name
        level = int(parts[1])
        special = parts[2] if len(parts) > 2 else ""
        if special == "amtlich":
            return "de", 2, "VS (amtlich geheimgehalten)"
        if special == "gkdos":
            return "de", 4, "GEHEIME KOMMANDOSACHE"
        if special == "grs":
            return "de", 4, "GEHEIME REICHSSACHE"
        label = LEVELS[level]
        if country == "at" and level in (3, 4):
            return "at", level, f"AT {label}"
        return "de", level, label
    if head == "ddr":
        level = int(parts[1])
        return "ddr", level, {1: "VD", 2: "VVS", 3: "GVS"}[level]
    if head == "at":
        return "at", 1, "AT EINGESCHRÄNKT"
    if head == "it":
        level = int(parts[1])
        return (
            "it",
            level,
            {
                1: "IT RISERVATO",
                2: "IT RISERVATISSIMO",
                4: "IT SEGRETISSIMO",
            }[level],
        )
    if head == "open":
        return parts[1], 0, parts[2]
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
        if parts[2] == "block":
            return "us", 2, "US CLASSIFIED"  # a grade is certain, which one is not
        return "us", 1, "US CUI" if parts[2] == "cui" else "US FOUO"
    if head == "tlp":
        colour = re.sub(r"[\s:.;,-]+", "", matched.split("TLP", 1)[1])
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
    if what == "confidenziale":  # Swiss French and Italian VERTRAULICH
        if country == "ch":
            return "ch", 2, "CH VERTRAULICH"
        return "company", 0, "CONFIDENTIAL"
    if what == "segreto":
        if country == "ch":
            return "ch", 3, "CH GEHEIM"
        return "it", 3, "IT SEGRETO"
    if what == "confidential":
        if english in ("us", "uk"):
            return english, 2, f"{english.upper()} CONFIDENTIAL"
        return "company", 0, "CONFIDENTIAL"
    if what == "restricted":
        if english in ("us", "uk"):
            return english, 1, f"{english.upper()} RESTRICTED"
        return "company", 0, "RESTRICTED"
    if what in ("3", "4"):  # SECRET, TOP SECRET
        level = int(what)
        name = "TOP SECRET" if level == 4 else "SECRET"
        if country == "ch" and level == 3:
            return "ch", 3, "CH GEHEIM"  # the French form of the Swiss grade
        if english:
            return english, level, f"{english.upper()} {name}"
        return "intl", level, name
    return None


def _grade_lists(findings: list[Finding], specimens: set[int]) -> list[Finding]:
    """Demotes what only looks like marking to mentions.

    A page that lists the grades — a directive, a training slide, the TLP
    explainer that is itself TLP:CLEAR — shows three or more different grades
    of one kind standing alone in its body. Whether its own header carries a
    grade does not matter: a VS-NfD training handout lists STRENG GEHEIM
    without being STRENG GEHEIM. A page stamped MUSTER or SPECIMEN shows
    markings as examples, and every marking on it is one.
    """
    families: dict[tuple[int, str], list[int]] = {}
    for i, f in enumerate(findings):
        if f.kind != "marking" or f.reason not in ("standalone", "short line", "coloured stamp"):
            continue
        if f.scheme == "company":
            continue
        family = "tlp" if f.scheme == "tlp" else "grade"
        families.setdefault((f.page, family), []).append(i)
    demote: dict[int, str] = {}
    for (_, family), idxs in families.items():
        distinct = {findings[i].label if family == "tlp" else findings[i].level for i in idxs}
        if len(distinct) >= 3:
            demote.update(dict.fromkeys(idxs, "list of grades"))
    for i, f in enumerate(findings):
        if f.kind == "marking" and f.page in specimens:
            demote[i] = "specimen"
    out = list(findings)
    for i, reason in demote.items():
        out[i] = Finding(**{**out[i].__dict__, "kind": "mention", "reason": reason})
    return out


_SPECIMEN = re.compile(r"^\W*(?:MUSTER|SPECIMEN|EXEMPLE|ESEMPIO|SAMPLE)\W*$")


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


def inspect(document: Document, *, file: str | os.PathLike[str] | None = None) -> MarkingReport:
    """Finds the classification markings in a scanned document.

    Works on what the scan read, so it sees what a person sees on the page —
    stamps, headers, footers — whatever the file format.

    Args:
        document: A scan result.
        file: The file it was read from. When given, the file's own evidence
            is added: a sensitivity label an Office or MIP tool stored in its
            metadata counts as a marking; a grade in the file name, which the
            VSA asks for, is reported as a mention — ``Merkblatt_VS-NfD.pdf``
            names a grade without carrying one.
    """
    cues = _cues(document)
    findings: list[Finding] = []
    if file is not None:
        findings.extend(_file_evidence(Path(file), cues))
    page_levels: list[int] = []
    cancelled = False
    # The line the latest finding came from and where on it, for merging the
    # other half of a bilingual marking into it. The same marking on another
    # line — the footer repeating the header — stays a finding of its own.
    last_line: Line | None = None
    last_start = last_end = 0
    numbers: list[int] = []
    specimens: set[int] = set()
    for page in document.pages:
        # The page's own number in the source, so a finding points at the
        # right page when only some of them were scanned.
        number = page.index + 1
        numbers.append(number)
        in_table: list[bool] = []
        stamped: list[bool] = []
        continued: list[bool] = []
        pieces: list[Line] = []
        for block in page.blocks:
            before: str | None = None
            for whole in block.lines:
                for piece in _pieces(whole):
                    pieces.append(piece)
                    in_table.append(block.table is not None)
                    stamped.append(block.kind == "stamp")
                    continued.append(before is not None and _continues(before, piece.text))
                    before = piece.text
        for line, tabular, stamp, carried in zip(pieces, in_table, stamped, continued):
            text = line.text
            if not text.strip():
                continue
            zone = _zone(line, page)
            if not any(c.islower() for c in text) and _SPECIMEN.match(_folded(text).text):
                specimens.add(number)
            hits = _hits(text, zone)
            ctx = _context(text, hits)
            # A line in the body that talks about a grade — a heading "VS-NfD
            # (Merkblatt)", "zugelassen für VS-NfD" — or that goes on from the
            # line before it is running text, however short it is.
            about = zone == "body" and (
                carried
                or bool(_ABOUT.search(_folded(text).text))
                or any(_compound(h.view, h) for h in hits)
            )
            first_on_line = len(findings)
            if ctx.cancelled and ctx.lowercase <= 1 and ctx.words <= 6:
                # A stamp ("Approved For Release 2005/01/12", "VS-NfD
                # aufgehoben"), not a sentence about lifting grades.
                cancelled = True
            for hit in hits:
                verdict = _verdict(hit, ctx, zone, text)
                if verdict is None:
                    continue
                kind, reason = verdict.split(": ", 1)
                if kind == "quiet":
                    kind = "marking"  # decided below, once the scheme is known
                elif kind == "marking" and about and not hit.term.anchored:
                    kind, reason = "mention", "running text"
                if kind != "marking" and (hit.view.fuzzy or hit.term.marking_only):
                    # A repaired word, or an abbreviation in a sentence, is far
                    # likelier to be something else than a grade being discussed.
                    continue
                if kind == "marking" and tabular and zone == "body":
                    kind, reason = "mention", "table"
                if kind == "marking" and stamp and reason in ("standalone", "short line"):
                    reason = "coloured stamp"  # read from its ink alone
                matched = hit.view.text[hit.start : hit.end]
                resolved = _resolve(hit.term.key, cues, matched)
                if resolved is None:
                    continue
                scheme, level, label = resolved
                if verdict.startswith("quiet") and (scheme != "company" or about):
                    # Lowercase, alone on its line: "Vertraulich" stamped by a
                    # company is its marking; "Geheim" is a heading, not a grade.
                    continue
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
            graded = any(f.kind == "marking" and f.level >= 1 for f in findings[first_on_line:])
            if graded:
                # CONFIDENTIEL UE/EU CONFIDENTIAL is one marking; its words are
                # not also a company's.
                findings[first_on_line:] = [
                    f for f in findings[first_on_line:] if f.scheme != "company"
                ]
        page_levels.append(0)

    findings = _grade_lists(findings, specimens)
    position = {number: i for i, number in enumerate(numbers)}
    for f in findings:
        if f.page == 0:
            continue  # the file's own evidence belongs to no page
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


#: A Microsoft Information Protection label's display name, as Office, Acrobat
#: and the MIP SDK store it: an XMP element or attribute, or a PDF string.
_MSIP_NAME = re.compile(
    rb"MSIP_Label_[0-9A-Fa-f-]{36}_Name\s*(?:>|=\s*[\"']|\()\s*([^<\"')\r\n]{1,120})"
)
#: Enough of a large file to hold its metadata: XMP sits near the start of a
#: PDF, the Info dictionary and an incremental update near the end.
_METADATA_WINDOW = 8 << 20


def _file_evidence(path: Path, cues: set[str]) -> list[Finding]:
    """Sensitivity labels in the file's metadata, and a grade in its name."""
    found: list[Finding] = []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(_METADATA_WINDOW)
            tail = b""
            if size > 2 * _METADATA_WINDOW:
                fh.seek(size - _METADATA_WINDOW)
                tail = fh.read()
            elif size > _METADATA_WINDOW:
                tail = fh.read()
    except OSError:
        head = tail = b""
    names: dict[str, None] = {}
    for m in _MSIP_NAME.finditer(head + tail):
        names.setdefault(m.group(1).decode("utf-8", "replace").strip(), None)
    for name in names:
        # A label is a deliberate classification: read it as if stamped alone
        # on a page, in capitals, whatever case the tenant spelled it in.
        resolved = _resolve_alone(name.upper(), cues)
        scheme, level, label = resolved or ("label", 0, name)
        found.append(_evidence(name, scheme, level, label, "marking", "sensitivity label"))
    stem = re.sub(r"[_.]+", " ", path.stem)
    for hit in _hits(stem, "header"):
        if not hit.term.distinctive:
            continue  # "geheim" in a file name is a word, not a stamp
        resolved = _resolve(hit.term.key, cues, hit.view.text[hit.start : hit.end])
        if resolved is not None:
            found.append(_evidence(path.name, *resolved, "mention", "file name"))
    return found


def _resolve_alone(text: str, cues: set[str]) -> tuple[str, int, str] | None:
    """The grade `text` names when it stands alone, or ``None``."""
    best: tuple[str, int, str] | None = None
    for hit in _hits(text, "header"):
        resolved = _resolve(hit.term.key, cues, hit.view.text[hit.start : hit.end])
        if resolved is not None and (best is None or resolved[1] > best[1]):
            best = resolved
    return best


def _evidence(text: str, scheme: str, level: int, label: str, kind: str, reason: str) -> Finding:
    return Finding(
        kind=kind,
        scheme=scheme,
        level=level,
        label=label,
        text=text,
        match=text,
        page=0,
        box=Box(0.0, 0.0, 0.0, 0.0),
        confidence=1.0,
        fuzzy=False,
        reason=reason,
    )


def inspect_many(documents: Iterable[Document]) -> Iterable[MarkingReport]:
    """:func:`inspect` over several documents, lazily."""
    for document in documents:
        yield inspect(document)
