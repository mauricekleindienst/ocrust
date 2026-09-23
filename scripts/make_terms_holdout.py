#!/usr/bin/env python3
"""Independent held-out set for the configurable term search (``ocrust.terms``).

    python scripts/make_terms_holdout.py --out /tmp/terms/holdout
    python scripts/evaluate_terms.py /tmp/terms/holdout -o /tmp/terms/holdout_report.json

The documents were written without looking at the matcher or at its tests:
business letters, invoices, delivery notes, minutes, printed e-mails, contracts,
forms with tables, comb fields and tick boxes, slide handouts, two-column staff
newsletters, file notes, fax cover sheets, data sheets and reports, mostly in
German, some in English. The search profile ``profile.toml`` holds 34 terms:
project code names, people, companies, compound words, part numbers, a customer
number, an IBAN and an e-mail pattern, short codes, a case-sensitive code, a
term that may sit inside longer words, terms with ``near``/``not_near`` and
terms restricted to zones. They are printed the way terms arrive in real scans:
different case, umlauts written out, letter-spaced, a space too many or too
few, hyphenated or broken at a line end, split over two table cells or two
columns, with a dash, underscore or colon inside, in running headers and
footers, in a rotated stamp, in 7 pt and in headline type, bold and coloured,
with an OCR look-alike printed into them, misspelt within the term's fuzzy
budget, and on pages that went through a 100 dpi fax, JPEG quality 25, blur,
a 2° skew or a faded copier. Look-alikes that must not be reported sit next to
them: the term inside a longer word, names one letter off where the term allows
no edits, a term next to its ``not_near`` word or without its ``near`` word,
a term outside its zones, customer numbers with five or seven digits, foreign
IBANs, the wrong case for a case-sensitive code, half a phrase; and a dozen
documents carry no term at all.

``ground_truth.json`` has one entry per file:

* ``expect``: per page and term, ``count`` occurrences that must be reported.
  ``how`` has one entry per occurrence: how it was printed (plain, case,
  umlaut, spaced, inner_space, glued, hyphenated, linebreak, cells, columns,
  punct, lookalike, fuzzy, inword), followed by ``+``-joined conditions
  (header, footer, stamp, small, large, bold, colour, fax, jpeg, blur, skew,
  faint). ``text`` is what was printed, a line or cell break written as
  ``\\n``; ``boxes`` the bounding box of each occurrence as fractions of the
  page width and height.
* ``absent``: terms that appear in the file only as look-alikes; none of them
  may be reported anywhere in the file.
* ``decoys``: every look-alike, with its page, text and the reason it must not
  count. A decoy of a term that is also expected on the page shows up as a
  count that must not grow.

Conventions: ``header`` and ``footer`` occurrences sit in the top or bottom
6 % of the page, body occurrences of zone-restricted terms well inside it. A
term printed with a regex term's shape (FS-220) also counts for the regex term.
Everything is seeded from the file name: a second run writes the same files.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import subprocess
import zlib
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path
from typing import Any, Union

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the profile is then not re-read
    tomllib = None

# ---------------------------------------------------------------- colours and fonts

WHITE = (255, 255, 255)
INK = (24, 24, 26)
GREY = (100, 100, 106)
LIGHT = (230, 233, 238)
RULE = (140, 140, 146)
RED = (192, 28, 40)
STAMP_RED = (184, 34, 52)
STAMP_BLUE = (36, 70, 172)
BLUE = (28, 72, 160)
NAVY = (20, 40, 96)
GREEN = (22, 112, 62)
TEAL = (0, 104, 116)
PURPLE = (98, 44, 138)
ORANGE = (206, 104, 16)
ACCENTS = (NAVY, BLUE, TEAL, GREEN, PURPLE, RED, ORANGE)

FAMILIES = {
    "arial": (
        "LiberationSans-Regular.ttf",
        "LiberationSans-Bold.ttf",
        "LiberationSans-Italic.ttf",
    ),
    "dejavu": ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"),
    "helv": ("FreeSans.ttf", "FreeSansBold.ttf", "FreeSansOblique.ttf"),
    "loma": ("Loma.otf", "Loma-Bold.otf", "Loma-Oblique.otf"),
    "times": (
        "LiberationSerif-Regular.ttf",
        "LiberationSerif-Bold.ttf",
        "LiberationSerif-Italic.ttf",
    ),
    "dvserif": ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf", "DejaVuSerif.ttf"),
    "freeserif": ("FreeSerif.ttf", "FreeSerifBold.ttf", "FreeSerifItalic.ttf"),
    "courier": (
        "LiberationMono-Regular.ttf",
        "LiberationMono-Bold.ttf",
        "LiberationMono-Italic.ttf",
    ),
    "dvmono": ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf", "DejaVuSansMono-Oblique.ttf"),
    "freemono": ("FreeMono.ttf", "FreeMonoBold.ttf", "FreeMonoOblique.ttf"),
}
WEIGHTS = {"r": 0, "b": 1, "i": 2}
FONT_ROOTS = (Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts")


@cache
def font_files() -> dict[str, str]:
    """Font file name -> path, as ``fc-list`` reports them (the font directories
    when fontconfig is missing)."""
    try:
        listing = subprocess.run(
            ["fc-list", "--format", "%{file}\n"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
        paths = [Path(line) for line in listing.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        paths = []
    if not paths:
        for root in FONT_ROOTS:
            if root.is_dir():
                paths.extend(root.rglob("*"))
    found: dict[str, str] = {}
    for path in sorted(paths):
        if path.suffix.lower() in (".ttf", ".otf"):
            found.setdefault(path.name, str(path))
    return found


@cache
def get_font(family: str, weight: str, px: int) -> ImageFont.FreeTypeFont:
    files = font_files()
    names = FAMILIES[family]
    path = files.get(names[WEIGHTS[weight]]) or files.get(names[0]) or files.get("DejaVuSans.ttf")
    if path is None:
        raise SystemExit("no usable TrueType font found; install fonts-dejavu or fonts-liberation")
    return ImageFont.truetype(path, px)


# ---------------------------------------------------------------- the search profile


@dataclass(frozen=True)
class Term:
    """One term of the profile, with what the generator needs to misprint it."""

    key: str
    name: str
    match: tuple[str, ...] = ()
    regex: str | None = None
    category: str = ""
    severity: str | None = None
    fuzzy: int | str | None = None
    case: bool | None = None
    whole_words: bool | None = None
    zones: tuple[str, ...] = ()
    near: tuple[str, ...] = ()
    not_near: tuple[str, ...] = ()
    window: int | None = None
    hy: str = ""  # the first pattern with "|" where a word may be broken
    look: str = ""  # the first pattern with an OCR look-alike printed into it
    typo: str = ""  # a real misspelling within the fuzzy budget
    inword: str = ""  # a longer word containing the term (whole_words = false)


TERMS = (
    Term(
        "nord",
        "Projekt Nordlicht",
        ("Projekt Nordlicht", "Vorhaben Nordlicht"),
        category="Projekte",
        severity="high",
        fuzzy=1,
        hy="Pro|jekt Nord|licht",
        look="Pr0jekt Nordlicht",
        typo="Projeckt Nordlicht",
    ),
    Term(
        "fuchs",
        "Operation Silberfuchs",
        ("Operation Silberfuchs",),
        category="Projekte",
        severity="critical",
        fuzzy=1,
        hy="Opera|tion Silber|fuchs",
        look="0peration Silberfuchs",
        typo="Opperation Silberfuchs",
    ),
    Term(
        "bruecke",
        "Blaue Brücke",
        ("Blaue Brücke",),
        category="Projekte",
        fuzzy="auto",
        hy="Blaue Brü|cke",
        look="B1aue Brücke",
    ),
    Term(
        "kranich",
        "Kranich",
        ("Kranich",),
        category="Projekte",
        severity="high",
        fuzzy=0,
        not_near=("Zugvögel", "Vogelzug", "Vogelwarte", "Vogelschutz"),
        window=60,
        hy="Kra|nich",
        look="KRAN1CH",
    ),
    Term(
        "falke",
        "Codename Falke",
        ("Falke",),
        category="Projekte",
        severity="high",
        fuzzy=0,
        near=("Codename", "Deckname", "Vorhaben"),
        window=40,
        look="Fa1ke",
    ),
    Term("orka", "ORKA", ("ORKA",), category="Systeme", fuzzy="off"),
    Term("vega", "VEGA", ("VEGA",), category="Systeme", fuzzy="off", case=True),
    Term(
        "nox",
        "NOX",
        ("NOX",),
        category="Systeme",
        severity="low",
        fuzzy="off",
        not_near=("Emissionen", "Emission", "Stickoxide", "Abgaswerte", "Grenzwert"),
        window=50,
    ),
    Term(
        "tarn",
        "Tarnkappe",
        ("Tarnkappe",),
        category="Projekte",
        fuzzy=0,
        whole_words=False,
        hy="Tarn|kappe",
        inword="Tarnkappenmodus",
    ),
    Term(
        "weiss",
        "Jürgen Weißmüller",
        ("Jürgen Weißmüller", "Weißmüller, Jürgen"),
        category="Personen",
        severity="high",
        fuzzy=0,
        hy="Jür|gen Weiß|mül|ler",
    ),
    Term(
        "kuehnast",
        "Sören Kühnast",
        ("Sören Kühnast", "Kühnast, Sören"),
        category="Personen",
        fuzzy=1,
        hy="Sö|ren Küh|nast",
        typo="Soren Kühnast",
    ),
    Term(
        "schoellhorn",
        "Margarete Schöllhorn",
        ("Margarete Schöllhorn", "M. Schöllhorn"),
        category="Personen",
        fuzzy="auto",
        hy="Marga|rete Schöll|horn",
        look="Margarete Schö1lhorn",
    ),
    Term(
        "assmann",
        "Friederike Aßmann-Thiel",
        ("Friederike Aßmann-Thiel",),
        category="Personen",
        fuzzy=1,
        hy="Friede|rike Aß|mann-|Thiel",
    ),
    Term("maass", "Ulrike Maaß", ("Ulrike Maaß",), category="Personen", severity="high", fuzzy=0),
    Term(
        "brandtner",
        "Brandtner & Söhne",
        ("Brandtner & Söhne", "Brandtner und Söhne"),
        category="Firmen",
        fuzzy=1,
        hy="Brandt|ner & Söh|ne",
    ),
    Term(
        "quadrant",
        "Quadrant Systems",
        ("Quadrant Systems",),
        category="Firmen",
        fuzzy=1,
        zones=("body",),
        hy="Quad|rant Sys|tems",
    ),
    Term(
        "vireon",
        "Vireon Pharma",
        ("Vireon Pharma", "Vireon Pharmaceuticals"),
        category="Firmen",
        fuzzy=1,
        hy="Vire|on Phar|ma",
    ),
    Term(
        "kessler",
        "Kessler Logistik",
        ("Kessler Logistik",),
        category="Firmen",
        fuzzy=1,
        hy="Kess|ler Logis|tik",
    ),
    Term(
        "nda_de",
        "Geheimhaltungsvereinbarung",
        ("Geheimhaltungsvereinbarung",),
        category="Verträge",
        severity="high",
        hy="Geheim|haltungs|verein|barung",
        look="GeheimhaItungsvereinbarung",
    ),
    Term(
        "nda_en",
        "Non-Disclosure Agreement",
        ("Non-Disclosure Agreement",),
        category="Verträge",
        severity="high",
        fuzzy=1,
        hy="Non-Disclo|sure Agree|ment",
        look="Non-Disc1osure Agreement",
    ),
    Term(
        "zutritt",
        "Zutrittskontrollanlage",
        ("Zutrittskontrollanlage",),
        category="Technik",
        fuzzy="auto",
        hy="Zutritts|kontroll|anlage",
        look="Zutrittskontro11anlage",
    ),
    Term(
        "dsfa",
        "Datenschutzfolgenabschätzung",
        ("Datenschutzfolgenabschätzung",),
        category="Datenschutz",
        fuzzy=2,
        hy="Datenschutz|folgen|abschätzung",
        typo="Datenschutzfolgeabschätzung",
    ),
    Term(
        "bmz",
        "Brandmeldezentrale",
        ("Brandmeldezentrale",),
        category="Technik",
        fuzzy=1,
        hy="Brand|melde|zentrale",
        look="Brandme1dezentrale",
        typo="Brandmeldzentrale",
    ),
    Term(
        "sue",
        "Sicherheitsüberprüfung",
        ("Sicherheitsüberprüfung",),
        category="Personal",
        severity="high",
        fuzzy="auto",
        hy="Sicherheits|über|prüfung",
    ),
    Term(
        "fruehwarn",
        "Frühwarnsystem",
        ("Frühwarnsystem",),
        category="Technik",
        hy="Früh|warn|system",
    ),
    Term(
        "halcyon",
        "Project Halcyon",
        ("Project Halcyon",),
        category="Projekte",
        severity="high",
        fuzzy=1,
        hy="Pro|ject Hal|cyon",
        look="Project HaIcyon",
        typo="Project Halcion",
    ),
    Term(
        "sentinel",
        "Sentinel X4",
        ("Sentinel X4", "Sentinel-X4"),
        category="Produkte",
        fuzzy=0,
        hy="Senti|nel X4",
    ),
    Term("fs220", "FS-220", ("FS-220",), category="Produkte", fuzzy=0),
    Term("fsre", "Sensor FS-xxx", regex=r"\bFS-\d{3}\b", category="Produkte", severity="low"),
    Term("kd", "Kundennummer", regex=r"\bKD-\d{6}\b", category="Kunden", severity="low"),
    Term(
        "iban",
        "IBAN (DE)",
        regex=r"\bDE\d{2}(?: ?\d{4}){4} ?\d{2}\b",
        category="Finanzen",
    ),
    Term(
        "mail",
        "Helvetor-Adresse",
        regex=r"\b[\w.+-]+@helvetor\.de\b",
        category="Kontakte",
        severity="low",
    ),
    Term(
        "weitergabe",
        "Nicht zur Weitergabe",
        ("Nicht zur Weitergabe",),
        category="Kennzeichnung",
        severity="high",
        fuzzy="auto",
        zones=("header", "footer"),
    ),
    Term("hafen", "Hafenstraße 12", ("Hafenstraße 12",), category="Standorte", fuzzy=0),
)
TERM = {term.key: term for term in TERMS}
BY_NAME = {term.name: term for term in TERMS}
SETTINGS = {"fuzzy": "auto", "case": False, "whole_words": True, "severity": "medium"}
MAILS = (
    "j.weissmueller@helvetor.de",
    "einkauf@helvetor.de",
    "projektbuero@helvetor.de",
    "s.kuehnast@helvetor.de",
    "empfang@helvetor.de",
)


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def profile_toml() -> str:
    out = [
        "# Suchprofil für den Holdout-Satz der Begriffssuche (scripts/make_terms_holdout.py).",
        "",
        "[settings]",
    ]
    out += [f"{key} = {toml_value(value)}" for key, value in SETTINGS.items()]
    for term in TERMS:
        out += ["", "[[term]]", f"name = {toml_value(term.name)}"]
        if term.regex:
            out.append(f"regex = '{term.regex}'")
        elif len(term.match) == 1 and term.match[0] == term.name and term.key in ("orka", "hafen"):
            pass  # name only: the pattern defaults to the name
        elif len(term.match) == 1:
            out.append(f"match = {toml_value(term.match[0])}")
        else:
            out.append(f"match = {toml_value(term.match)}")
        for attr in ("category", "severity", "fuzzy", "case", "whole_words", "zones"):
            value = getattr(term, attr)
            if value not in (None, "", ()):
                out.append(f"{attr} = {toml_value(value)}")
        for attr in ("near", "not_near", "window"):
            value = getattr(term, attr)
            if value not in (None, ()):
                out.append(f"{attr} = {toml_value(value)}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- spelling helpers

FOLD = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ẞ": "SS"}
)


def fold(text: str) -> str:
    return text.translate(FOLD).lower()


def compact(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", fold(text))


def edits(a: str, b: str) -> int:
    """Levenshtein distance."""
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


# printed character -> the letter it stands in for (after folding to lower case)
LOOKALIKES = {("0", "o"), ("1", "l"), ("1", "i"), ("i", "l"), ("5", "s"), ("8", "b")}


def regex_core(term: Term) -> str:
    return term.regex.replace(r"\b", "") if term.regex else ""


def break_points(term: Term, text: str) -> list[int]:
    """Offsets inside words where `text` (the term's first pattern) may break."""
    if not term.hy or term.hy.replace("|", "") != text:
        return []
    points, offset = [], 0
    for ch in term.hy:
        if ch == "|":
            points.append(offset)
        else:
            offset += 1
    return [p for p in points if 0 < p < len(text) and text[p - 1] != " " and text[p] != " "]


# ---------------------------------------------------------------- occurrences


@dataclass
class Occ:
    """One printed occurrence that must be reported."""

    terms: list[str]
    how: str
    parts: list[str]
    spaced: bool = False
    mods: set[str] = field(default_factory=set)
    page: int | None = None
    boxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    flags: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.parts)


@dataclass
class Decoy:
    """A look-alike of `term` that must not be reported."""

    term: str
    text: str
    why: str
    page: int | None = None
    boxes: list[tuple[float, float, float, float]] = field(default_factory=list)


@dataclass(frozen=True)
class Style:
    family: str = "arial"
    weight: str = "r"
    pt: float = 10.0
    color: tuple[int, int, int] = INK

    def but(self, **changes: Any) -> Style:
        return replace(self, **changes)


@dataclass
class Unit:
    """A piece of text the line breaker never splits."""

    text: str
    style: Style
    occs: list[Occ] = field(default_factory=list)
    decoy: Decoy | None = None
    spaced: bool = False
    glue: bool = False  # no space before it
    brk: bool = False  # the line ends after it


@dataclass
class Part:
    """One piece of a split occurrence, placed on its own (a table cell, a column)."""

    occ: Occ
    index: int


Content = Union[str, Part, Unit, list, tuple, None]

MARK = re.compile(
    r"\{(?P<key>\w+)(?:@(?P<alt>\d))?:(?P<how>\w+)(?:=(?P<surf>[^|}]*))?(?:\|(?P<flags>\w+))?\}"
    r"|\[\[(?P<dtext>[^|\]]+)\|(?P<dkey>\w+)\|(?P<why>[^\]]+)\]\]"
)
KINDS = (
    "plain",
    "case",
    "umlaut",
    "spaced",
    "inner_space",
    "glued",
    "hyphenated",
    "linebreak",
    "cells",
    "columns",
    "punct",
    "lookalike",
    "fuzzy",
    "inword",
)
MODS = (
    "header",
    "footer",
    "stamp",
    "small",
    "large",
    "bold",
    "colour",
    "fax",
    "jpeg",
    "blur",
    "skew",
    "faint",
)


def regex_sample(term: Term, rng: random.Random) -> str:
    digits = "".join(rng.choice("0123456789") for _ in range(24))
    if term.key == "kd":
        return f"KD-{rng.randint(1, 9)}{digits[:5]}"
    if term.key == "iban":
        raw = f"DE{rng.randint(10, 99)}{digits[:18]}"
        return " ".join(raw[i : i + 4] for i in range(0, len(raw), 4))
    if term.key == "mail":
        return rng.choice(MAILS)
    if term.key == "fsre":
        return f"FS-{rng.choice(('145', '310', '480', '265', '120'))}"
    raise ValueError(term.key)


def make_surface(
    term: Term, how: str, alt: int, surf: str | None, rng: random.Random
) -> tuple[list[str], bool, str]:
    """The printed parts of an occurrence, whether it is letter-spaced, and the
    text it reads as once the breaks are undone."""
    if surf:
        text = surf
    elif term.regex:
        text = regex_sample(term, rng)
    else:
        text = term.match[alt]
    points = break_points(term, text) if alt == 0 and not surf else []

    def split_word(hyphen: bool) -> tuple[list[str], str]:
        if not points:
            raise ValueError(f"{term.key}: no break point for {how}")
        p = rng.choice(points)
        left, right = text[:p], text[p:]
        if hyphen and not left.endswith("-"):
            return [left + "-", right], text
        return [left, right], text

    def split_space() -> tuple[list[str], str]:
        spaces = [i for i, ch in enumerate(text) if ch == " "]
        if not spaces:
            return split_word(how == "columns")
        i = spaces[len(spaces) // 2] if term.regex else rng.choice(spaces)
        return [text[:i], text[i + 1 :]], text

    if how == "plain" or (surf and how in ("case", "umlaut", "lookalike", "fuzzy", "inword")):
        return [text], False, text
    if how == "case":
        options = [v for v in (text.upper(), text.lower(), text.title()) if v != text]
        cased = options[0] if rng.random() < 0.6 else rng.choice(options)
        return [cased], False, cased
    if how == "umlaut":
        folded = text.translate(FOLD)
        if folded == text:
            raise ValueError(f"{term.key}: nothing to write out")
        return [folded], False, folded
    if how == "spaced":
        return [text], True, text
    if how == "inner_space":
        parts, _ = split_word(False)
        return [" ".join(parts)], False, " ".join(parts)
    if how == "glued":
        return [text.replace(" ", "")], False, text.replace(" ", "")
    if how == "punct":
        if " " in text:
            i = rng.choice([i for i, ch in enumerate(text) if ch == " "])
            sep = rng.choice(("-", "_", ": "))
            out = text[:i] + sep + text[i + 1 :]
        elif "-" in text:
            out = text.replace("-", " ", 1)
        else:
            parts, _ = split_word(False)
            out = parts[0] + "-" + parts[1][0].upper() + parts[1][1:]
        return [out], False, out
    if how == "hyphenated":
        parts, joined = split_word(True)
        return parts, False, joined
    if how in ("linebreak", "cells", "columns"):
        parts, joined = split_space()
        return parts, False, joined
    printed = {"lookalike": term.look, "fuzzy": term.typo, "inword": term.inword}.get(how)
    if printed:
        return [printed], False, printed
    raise ValueError(f"{term.key}: nothing to print for {how}")


def check_surface(term: Term, how: str, alt: int, joined: str) -> None:
    """The printed occurrence still is the term, once breaks and case are undone."""
    if term.regex:
        if not re.fullmatch(regex_core(term), joined.replace("\n", " ")):
            raise ValueError(f"{term.key}: {joined!r} does not match {term.regex}")
        return
    want = compact(term.match[alt])
    got = compact(joined)
    if how == "fuzzy":
        budget = term.fuzzy if isinstance(term.fuzzy, int) else 0
        if not 0 < edits(got, want) <= budget:
            raise ValueError(f"{term.key}: typo {joined!r} outside the fuzzy budget")
    elif how == "inword":
        if want not in got or term.whole_words is not False:
            raise ValueError(f"{term.key}: bad in-word surface {joined!r}")
    elif how == "lookalike":
        pairs = [(a, b) for a, b in zip(got, want) if a != b]
        if len(got) != len(want) or not pairs or not all(p in LOOKALIKES for p in pairs):
            raise ValueError(f"{term.key}: {joined!r} is not an OCR look-alike of the term")
    elif got != want:
        raise ValueError(f"{term.key}: {how} surface {joined!r} is not {term.match[alt]!r}")


# ---------------------------------------------------------------- pages


def colourful(color: tuple[int, int, int]) -> bool:
    return max(color) - min(color) > 60


def mods_for(style: Style, zone: str) -> set[str]:
    mods = set()
    if zone in ("header", "footer", "stamp"):
        mods.add(zone)
    if style.pt <= 7.5:
        mods.add("small")
    if style.pt >= 17:
        mods.add("large")
    if style.weight == "b":
        mods.add("bold")
    if colourful(style.color):
        mods.add("colour")
    return mods


class Sheet:
    """One page being drawn, in millimetres, onto an image at the document's dpi."""

    def __init__(self, doc: Doc, number: int, landscape: bool) -> None:
        self.doc = doc
        self.number = number
        self.dpi = doc.dpi
        self.wmm, self.hmm = (297.0, 210.0) if landscape else (210.0, 297.0)
        self.size = (round(self.wmm / 25.4 * self.dpi), round(self.hmm / 25.4 * self.dpi))
        self.img = Image.new("RGB", self.size, WHITE)
        self.draw = ImageDraw.Draw(self.img)
        self.lines: list[str] = []
        self.filler: list[str] = []
        self.length = 0  # characters in "\n".join(self.lines)
        self.spans: list[tuple[int, int, Unit]] = []

    def px(self, mm: float) -> float:
        return mm * self.dpi / 25.4

    def mm(self, px: float) -> float:
        return px * 25.4 / self.dpi

    def font(self, style: Style) -> ImageFont.FreeTypeFont:
        return get_font(style.family, style.weight, max(6, round(style.pt * self.dpi / 72)))

    def mark(self, unit: Unit, box: tuple[float, float, float, float], zone: str) -> None:
        for occ in unit.occs:
            if occ.page not in (None, self.number):
                raise ValueError(f"{self.doc.name}: {occ.text!r} runs over two pages")
            occ.page = self.number
            occ.boxes.append(tuple(box))
            occ.mods |= mods_for(unit.style, zone)
        if unit.decoy is not None:
            unit.decoy.page = self.number
            unit.decoy.boxes.append(tuple(box))

    def record(self, units: list[Unit]) -> None:
        text, filler = [], []
        base = self.length + (1 if self.lines else 0)
        offset = base
        for unit in units:
            sep = "" if unit.glue or not text else " "
            text.append(sep + unit.text)
            filler.append(sep + ("¤" if unit.occs or unit.decoy else unit.text))
            offset += len(sep)
            if unit.occs or unit.decoy:
                self.spans.append((offset, offset + len(unit.text), unit))
            offset += len(unit.text)
        self.lines.append("".join(text))
        self.filler.append("".join(filler))
        self.length = offset

    # -- drawing primitives

    def rect(self, x0, y0, x1, y1, *, fill=None, outline=None, width=0.25) -> None:
        self.draw.rectangle(
            (self.px(x0), self.px(y0), self.px(x1), self.px(y1)),
            fill=fill,
            outline=outline,
            width=max(1, round(self.px(width))) if outline else 0,
        )

    def hline(self, x0, x1, y, *, color=RULE, width=0.25) -> None:
        w = max(1, round(self.px(width)))
        self.draw.line((self.px(x0), self.px(y), self.px(x1), self.px(y)), fill=color, width=w)

    def vline(self, x, y0, y1, *, color=RULE, width=0.25) -> None:
        w = max(1, round(self.px(width)))
        self.draw.line((self.px(x), self.px(y0), self.px(x), self.px(y1)), fill=color, width=w)

    def tick(self, x, y, checked: bool, size=3.4) -> None:
        self.rect(x, y, x + size, y + size, outline=INK, width=0.3)
        if checked:
            w = max(2, round(self.px(0.45)))
            a, b = self.px(x + 0.6), self.px(x + size - 0.6)
            c, d = self.px(y + 0.6), self.px(y + size - 0.6)
            self.draw.line((a, c, b, d), fill=INK, width=w)
            self.draw.line((a, d, b, c), fill=INK, width=w)

    def logo(self, x, y, size, color, kind: int) -> None:
        box = (self.px(x), self.px(y), self.px(x + size), self.px(y + size))
        if kind == 0:
            self.draw.ellipse(box, fill=color)
            inner = size * 0.3
            self.draw.ellipse(
                (
                    self.px(x + inner),
                    self.px(y + inner),
                    self.px(x + size - inner),
                    self.px(y + size - inner),
                ),
                fill=WHITE,
            )
        elif kind == 1:
            self.draw.rectangle(box, fill=color)
            self.draw.polygon(
                [
                    (self.px(x), self.px(y + size)),
                    (self.px(x + size), self.px(y)),
                    (self.px(x + size), self.px(y + size)),
                ],
                fill=WHITE,
            )
        else:
            self.draw.polygon(
                [
                    (self.px(x + size / 2), self.px(y)),
                    (self.px(x + size), self.px(y + size)),
                    (self.px(x), self.px(y + size)),
                ],
                fill=color,
            )

    def scribble(self, x, y, w, color=(28, 48, 120)) -> None:
        """A handwritten signature."""
        rng = self.doc.rng
        points, n = [], 60
        loops = rng.uniform(3, 6)
        for i in range(n):
            t = i / (n - 1)
            px = x + t * w + math.sin(t * math.pi * loops * 2) * 2.2
            py = y + math.sin(t * math.pi * loops) * rng.uniform(1.5, 4.0) - t * 2
            points.append((self.px(px), self.px(py)))
        self.draw.line(points, fill=color, width=max(2, round(self.px(0.35))), joint="curve")


# ---------------------------------------------------------------- text layout

TRACK = 0.42  # letter spacing of spaced-out text, in ems


def spaced_width(font: ImageFont.FreeTypeFont, text: str) -> float:
    track, width = font.size * TRACK, 0.0
    for i, ch in enumerate(text):
        if ch == " ":
            width += font.getlength(" ") + 2 * track
        else:
            width += font.getlength(ch) + (track if i < len(text) - 1 else 0)
    return width


def unit_width(sheet: Sheet, unit: Unit) -> float:
    font = sheet.font(unit.style)
    return spaced_width(font, unit.text) if unit.spaced else font.getlength(unit.text)


def draw_unit(sheet: Sheet, unit: Unit, x: float, baseline: float) -> tuple[float, ...]:
    font = sheet.font(unit.style)
    fill = unit.style.color
    if not unit.spaced:
        sheet.draw.text((x, baseline), unit.text, font=font, fill=fill, anchor="ls")
        return sheet.draw.textbbox((x, baseline), unit.text, font=font, anchor="ls")
    track, boxes = font.size * TRACK, []
    for i, ch in enumerate(unit.text):
        if ch == " ":
            x += font.getlength(" ") + 2 * track
            continue
        sheet.draw.text((x, baseline), ch, font=font, fill=fill, anchor="ls")
        boxes.append(sheet.draw.textbbox((x, baseline), ch, font=font, anchor="ls"))
        x += font.getlength(ch) + (track if i < len(unit.text) - 1 else 0)
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


@dataclass
class Row:
    groups: list[tuple[list[Unit], float]]
    width: float
    brk: bool = False
    last: bool = False

    @property
    def units(self) -> list[Unit]:
        return [u for group, _ in self.groups for u in group]

    def continues(self) -> bool:
        """The row ends inside an occurrence that goes on in the next row."""
        units = self.units
        return bool(units) and units[-1].brk and bool(units[-1].occs)


def layout(sheet: Sheet, units: list[Unit], maxw: float) -> list[Row]:
    groups: list[list[Unit]] = []
    for unit in units:
        if unit.glue and groups and not groups[-1][-1].brk:
            groups[-1].append(unit)
        else:
            groups.append([unit])
    rows: list[Row] = []
    current: list[tuple[list[Unit], float]] = []
    width = 0.0
    for group in groups:
        gw = sum(unit_width(sheet, u) for u in group)
        space = sheet.font(group[0].style).getlength(" ")
        if current and width + space + gw > maxw:
            rows.append(Row(current, width))
            current, width = [], 0.0
        if current:
            width += space
        current.append((group, gw))
        width += gw
        if group[-1].brk:
            rows.append(Row(current, width, brk=True))
            current, width = [], 0.0
    if current:
        rows.append(Row(current, width))
    if rows:
        rows[-1].last = True
    return rows


def row_height(sheet: Sheet, row: Row, leading: float) -> float:
    return max(sheet.font(u.style).size for u in row.units) * leading


def render_row(
    sheet: Sheet, row: Row, x: float, y: float, maxw: float, align: str, leading: float, zone: str
) -> float:
    """Draws `row` with its top at pixel `y`; returns the next row's top."""
    if row.width > maxw * 1.02:
        sheet.doc.warnings.append(
            f"p{sheet.number}: {' '.join(u.text for u in row.units)!r} overflows"
        )
    fonts = [sheet.font(u.style) for u in row.units]
    height = max(f.size for f in fonts) * leading
    ascent = max(f.getmetrics()[0] for f in fonts)
    baseline = y + ascent + (height - max(f.size for f in fonts) * 1.18) / 2
    gaps = len(row.groups) - 1
    stretch = 0.0
    if align == "center":
        x += (maxw - row.width) / 2
    elif align == "right":
        x += maxw - row.width
    elif (
        align == "justify"
        and gaps
        and not row.last
        and (not row.brk or row.continues())
        and row.width > maxw * 0.7
    ):
        stretch = (maxw - row.width) / gaps
    for i, (group, _) in enumerate(row.groups):
        if i:
            x += sheet.font(group[0].style).getlength(" ") + stretch
        for unit in group:
            box = draw_unit(sheet, unit, x, baseline)
            if unit.occs or unit.decoy:
                sheet.mark(unit, box, zone)
            x += unit_width(sheet, unit)
    sheet.record(row.units)
    return y + height


def text(
    sheet: Sheet,
    x: float,
    y: float,
    w: float,
    units: list[Unit],
    *,
    align: str = "left",
    leading: float = 1.28,
    zone: str = "body",
) -> float:
    """Flows `units` into the box at (x, y) mm, `w` mm wide; returns the y below it."""
    top = sheet.px(y)
    width = fit_width(sheet, units, sheet.px(w), align)
    for row in layout(sheet, units, width):
        top = render_row(sheet, row, sheet.px(x), top, sheet.px(w), align, leading, zone)
    return sheet.mm(top)


def fit_width(sheet: Sheet, units: list[Unit], maxw: float, align: str) -> float:
    """A width up to `maxw` at which every planted line break falls where the
    line is full, as a real one would; `maxw` when there is none."""
    if align not in ("left", "justify") or not any(u.brk and u.occs for u in units):
        return maxw
    w = maxw
    while w > maxw * 0.74:
        rows = layout(sheet, units, w)
        natural = True
        for row, following in zip(rows, rows[1:]):
            if row.continues():
                group, gw = following.groups[0]
                space = sheet.font(group[0].style).getlength(" ")
                if row.width + space + gw <= w:
                    natural = False
                    break
        if natural:
            return w
        w -= sheet.px(0.8)
    return maxw


def text_height(sheet: Sheet, units: list[Unit], w: float, leading: float = 1.28) -> float:
    rows = layout(sheet, units, fit_width(sheet, units, sheet.px(w), "left"))
    return sheet.mm(sum(row_height(sheet, row, leading) for row in rows))


# ---------------------------------------------------------------- documents


class Doc:
    """One output file: its pages, the occurrences printed on them and the decoys."""

    def __init__(
        self,
        name: str,
        *,
        fmt: str = "png",
        dpi: int = 200,
        family: str = "arial",
        head: str | None = None,
        pt: float = 10.5,
        degrade: tuple[str, ...] = (),
        align: str = "left",
        lang: str = "de",
        clean: bool = False,
        notes: str = "",
    ) -> None:
        self.name = name
        self.rng = random.Random(zlib.crc32(name.encode()))
        self.fmt, self.dpi, self.family = fmt, dpi, family
        self.head = head or family
        self.pt = pt
        self.degrade = degrade
        self.align = align
        self.lang = lang
        self.clean = clean
        self.notes = notes
        self.accent = self.rng.choice(ACCENTS)
        self.sheets: list[Sheet] = []
        self.occs: list[Occ] = []
        self.decoys: list[Decoy] = []
        self.used: set[str] = set()
        self.warnings: list[str] = []

    # -- pages and styles

    def page(self, landscape: bool = False) -> Sheet:
        sheet = Sheet(self, len(self.sheets) + 1, landscape)
        self.sheets.append(sheet)
        return sheet

    def st(
        self,
        pt: float | None = None,
        weight: str = "r",
        family: str | None = None,
        color: tuple[int, int, int] = INK,
    ) -> Style:
        return Style(family or self.family, weight, pt or self.pt, color)

    def hs(self, pt: float, weight: str = "b", color: tuple[int, int, int] = INK) -> Style:
        return Style(self.head, weight, pt, color)

    # -- occurrences and decoys

    def occ(
        self, key: str, how: str, alt: int = 0, surf: str | None = None, flags: str = ""
    ) -> Occ:
        term = TERM[key]
        if how not in KINDS:
            raise ValueError(f"{self.name}: unknown kind {how}")
        parts, spaced, joined = make_surface(term, how, alt, surf, self.rng)
        check_surface(term, how, alt, joined)
        names = [term.name]
        logical = " ".join(parts) if not spaced else joined
        if how == "hyphenated" and parts[0].endswith("-"):
            logical = joined
        for other in TERMS:
            if other.regex and other is not term and re.fullmatch(regex_core(other), logical):
                names.append(other.name)
        occ = Occ(names, how, parts, spaced, flags=flags)
        self.occs.append(occ)
        return occ

    def decoy(self, text: str, key: str, why: str) -> Decoy:
        decoy = Decoy(TERM[key].name, text, why)
        self.decoys.append(decoy)
        return decoy

    def styled(self, style: Style, flags: str) -> Style:
        if "b" in flags:
            style = style.but(weight="b")
        if "i" in flags:
            style = style.but(weight="i")
        if "c" in flags:
            style = style.but(color=self.accent)
        if "R" in flags:
            style = style.but(color=RED)
        if "s" in flags:
            style = style.but(pt=7)
        if "L" in flags:
            style = style.but(pt=max(18.0, style.pt * 1.9))
        return style

    def occ_units(self, occ: Occ, style: Style, only: int | None = None) -> list[Unit]:
        style = self.styled(style, occ.flags)
        out = []
        for i, part in enumerate(occ.parts):
            if only is not None and i != only:
                continue
            brk = only is None and i < len(occ.parts) - 1
            out.append(Unit(part, style, [occ], spaced=occ.spaced, brk=brk))
        return out

    def units(self, content: Content, style: Style, out: list[Unit] | None = None) -> list[Unit]:
        """Parses markup: ``{key:how}``, ``{key@1:how}`` (second pattern),
        ``{key:how=printed}``, ``{key:how|flags}`` with flags b(old), c(olour),
        s(mall), L(arge), i(talic), R(ed); ``[[printed|key|why]]`` is a decoy."""
        out = [] if out is None else out
        if content is None:
            return out
        if isinstance(content, (list, tuple)):
            for item in content:
                self.units(item, style, out)
            return out
        if isinstance(content, Part):
            out.extend(self.occ_units(content.occ, style, content.index))
            return out
        if isinstance(content, Unit):
            out.append(content)
            return out
        glue_next = bool(out) and content[:1] in ",.:;!?)»“”'"
        pos = 0
        for m in MARK.finditer(content):
            glue_next = self._plain(content[pos : m.start()], style, out, glue_next)
            if m.group("key"):
                occ = self.occ(
                    m.group("key"),
                    m.group("how"),
                    int(m.group("alt") or 0),
                    m.group("surf"),
                    m.group("flags") or "",
                )
                new = self.occ_units(occ, style)
            else:
                decoy = self.decoy(m.group("dtext"), m.group("dkey"), m.group("why"))
                unit = Unit(decoy.text, style, decoy=decoy)
                for other in TERMS:
                    if other.regex and re.search(other.regex, decoy.text):
                        unit.occs.append(Occ([other.name], "plain", [decoy.text]))
                        self.occs.append(unit.occs[-1])
                new = [unit]
            new[0].glue = glue_next and bool(out)
            out.extend(new)
            glue_next = True
            pos = m.end()
        self._plain(content[pos:], style, out, glue_next)
        return out

    @staticmethod
    def _plain(chunk: str, style: Style, out: list[Unit], glue_first: bool) -> bool:
        """Adds the words of `chunk`; returns whether what follows sticks to it."""
        if not chunk:
            return glue_first
        words = chunk.split()
        for i, word in enumerate(words):
            glue = i == 0 and bool(out) and not chunk[0].isspace() and glue_first
            out.append(Unit(word, style, glue=glue))
        return not chunk[-1].isspace() and bool(words)

    def fill(self, pool: tuple[str, ...], n: int) -> str:
        """`n` sentences from `pool` not yet used in this document."""
        choices = [s for s in pool if s not in self.used]
        picked = self.rng.sample(choices, min(n, len(choices)))
        self.used.update(picked)
        return " ".join(picked)


# ---------------------------------------------------------------- flowing text over pages


class Story:
    """Paragraphs and tables flowing down a page and onto new ones."""

    def __init__(
        self,
        doc: Doc,
        *,
        left: float = 25,
        width: float = 165,
        top: float = 25,
        bottom: float = 270,
        furniture: Callable[[Sheet], None] | None = None,
        landscape: bool = False,
    ) -> None:
        self.doc = doc
        self.left, self.width, self.top, self.bottom = left, width, top, bottom
        self.furniture = furniture
        self.landscape = landscape
        self.sheet: Sheet | None = None
        self.y = top

    def new_page(self) -> Sheet:
        self.sheet = self.doc.page(self.landscape)
        self.y = self.top
        if self.furniture:
            self.furniture(self.sheet)
        return self.sheet

    @property
    def s(self) -> Sheet:
        return self.sheet or self.new_page()

    def space(self, mm: float) -> None:
        self.y += mm

    def para(
        self,
        content: Content,
        style: Style | None = None,
        *,
        align: str | None = None,
        before: float = 0,
        after: float = 2.6,
        leading: float = 1.3,
        indent: float = 0,
        width: float | None = None,
        keep: float = 0,
    ) -> None:
        sheet = self.s
        style = style or self.doc.st()
        units = self.doc.units(content, style)
        w = (width or self.width) - indent
        align = align or self.doc.align
        wpx = fit_width(sheet, units, sheet.px(w), align)
        rows = layout(sheet, units, wpx)
        self.y += before
        if keep and self.y + keep > self.bottom:
            sheet = self.new_page()
        for i, row in enumerate(rows):
            h = sheet.mm(row_height(sheet, row, leading))
            need = h
            if row.continues() and i + 1 < len(rows):
                need += sheet.mm(row_height(sheet, rows[i + 1], leading))
            if self.y + need > self.bottom:
                sheet = self.new_page()
            top = render_row(
                sheet,
                row,
                sheet.px(self.left + indent),
                sheet.px(self.y),
                sheet.px(w),
                align,
                leading,
                "body",
            )
            self.y = sheet.mm(top)
        self.y += after

    def heading(self, content: Content, style: Style, *, before=3.0, after=1.6) -> None:
        self.para(content, style, align="left", before=before, after=after, keep=22)

    def bullets(self, items: list[Content], style: Style | None = None, bullet="•") -> None:
        style = style or self.doc.st()
        for item in items:
            sheet = self.s
            y0 = self.y
            self.para(item, style, indent=6, after=1.2, align="left")
            if self.sheet is sheet:
                text(sheet, self.left + 1, y0, 5, self.doc.units(bullet, style))

    def table(
        self,
        widths: list[float],
        rows: list[list[Content]],
        style: Style | None = None,
        *,
        head: bool = True,
        head_style: Style | None = None,
        **kw: Any,
    ) -> None:
        style = style or self.doc.st(self.doc.pt - 1)
        parsed = parse_rows(self.doc, rows, style, head, head_style)
        start = 0
        while True:
            done, self.y = table(
                self.s,
                self.left,
                self.y,
                widths,
                [],
                style,
                head=head,
                start=start,
                bottom=self.bottom,
                parsed=parsed,
                **kw,
            )
            if done >= len(parsed):
                break
            self.new_page()
            if head:  # the header row again, then the rest
                parsed = [parsed[0]] + parsed[done:]
                start = 1
            else:
                parsed, start = parsed[done:], 0
        self.y += 3


def parse_rows(
    doc: Doc, rows: list[list[Content]], style: Style, head: bool, head_style: Style | None
) -> list[list[list[Unit]]]:
    hstyle = head_style or style.but(weight="b")
    return [
        [doc.units(cell, hstyle if head and r == 0 else style) for cell in row]
        for r, row in enumerate(rows)
    ]


def table(
    sheet: Sheet,
    x: float,
    y: float,
    widths: list[float],
    rows: list[list[Content]],
    style: Style,
    *,
    head: bool = True,
    head_fill: tuple[int, int, int] | None = LIGHT,
    head_style: Style | None = None,
    pad: float = 1.5,
    grid: bool = True,
    zone: str = "body",
    aligns: tuple[str, ...] | None = None,
    start: int = 0,
    bottom: float = 285,
    min_row: float = 0,
    parsed: list[list[list[Unit]]] | None = None,
) -> tuple[int, float]:
    """Draws rows[start:] until `bottom`; returns (next row, y below the table)."""
    if parsed is None:
        parsed = parse_rows(sheet.doc, rows, style, head, head_style)
    top = y
    for r in range(start, len(parsed)):
        cells = parsed[r]
        heights = [text_height(sheet, units, w - 2 * pad, 1.22) for units, w in zip(cells, widths)]
        h = max(max(heights, default=0) + 2 * pad, min_row)
        if y + h > bottom and r > start:
            _grid(sheet, x, top, y, widths, grid)
            return r, y
        if head and r == 0 and head_fill:
            sheet.rect(x, y, x + sum(widths), y + h, fill=head_fill)
        cx = x
        for i, (units, w) in enumerate(zip(cells, widths)):
            align = aligns[i] if aligns else "left"
            text(sheet, cx + pad, y + pad, w - 2 * pad, units, align=align, leading=1.22, zone=zone)
            cx += w
        if grid:
            sheet.hline(x, x + sum(widths), y, color=RULE)
        y += h
    _grid(sheet, x, top, y, widths, grid)
    return len(parsed), y


def _grid(sheet: Sheet, x: float, top: float, y: float, widths: list[float], grid: bool) -> None:
    if not grid:
        sheet.hline(x, x + sum(widths), y, color=RULE)
        return
    sheet.hline(x, x + sum(widths), y, color=RULE)
    cx = x
    for w in [0, *widths]:
        cx += w
        sheet.vline(cx, top, y, color=RULE)


def comb(sheet: Sheet, x: float, y: float, content: Content, style: Style, cell: float = 5.2):
    """A comb field: one printed character per box, as on forms."""
    doc = sheet.doc
    units = doc.units(content, style)
    chars = " ".join(u.text for u in units)
    occs = [o for u in units for o in u.occs]
    font = sheet.font(style)
    boxes = []
    for i, ch in enumerate(chars):
        x0 = x + i * cell
        sheet.rect(x0, y, x0 + cell, y + cell * 1.25, outline=GREY, width=0.25)
        if ch.strip():
            cx, cy = sheet.px(x0 + cell / 2), sheet.px(y + cell * 0.95)
            sheet.draw.text((cx, cy), ch, font=font, fill=style.color, anchor="ms")
            boxes.append(sheet.draw.textbbox((cx, cy), ch, font=font, anchor="ms"))
    box = (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )
    for unit in units:
        if unit.occs or unit.decoy:
            sheet.mark(unit, box, "body")
    sheet.record([Unit(" ".join(chars), style, occs)])
    return x + len(chars) * cell


def stamp(
    sheet: Sheet,
    cx: float,
    cy: float,
    lines: list[Content],
    *,
    color=STAMP_RED,
    angle: float = 14.0,
    pt: float = 15,
    family: str = "arial",
    frame: bool = True,
) -> None:
    """A rubber stamp: framed lines of text, rotated, in thin uneven ink."""
    doc = sheet.doc
    rows = [doc.units(line, Style(family, "b", pt, color)) for line in lines]
    widths = [sum(unit_width(sheet, u) for u in r) + sheet.px(1.6) * (len(r) - 1) for r in rows]
    fonts = [max(sheet.font(u.style).size for u in r) for r in rows]
    pad = sheet.px(3.0)
    w = int(max(widths) + 2 * pad)
    h = int(sum(f * 1.25 for f in fonts) + 2 * pad)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    if frame:
        lw = max(2, round(sheet.px(0.6)))
        draw.rounded_rectangle((lw, lw, w - lw, h - lw), radius=pad / 2, outline=color, width=lw)
    marks: list[tuple[Unit, tuple[float, ...]]] = []
    y = pad
    for units, width, size in zip(rows, widths, fonts):
        x = (w - width) / 2
        baseline = y + size * 0.95
        for unit in units:
            font = sheet.font(unit.style)
            draw.text((x, baseline), unit.text, font=font, fill=color, anchor="ls")
            if unit.occs or unit.decoy:
                marks.append(
                    (unit, draw.textbbox((x, baseline), unit.text, font=font, anchor="ls"))
                )
            x += unit_width(sheet, unit) + sheet.px(1.6)
        sheet.record(units)
        y += size * 1.25
    rng = np.random.default_rng(zlib.crc32(f"{doc.name}/stamp/{cx}".encode()))
    alpha = np.asarray(layer.getchannel("A"), dtype=np.float32)
    alpha *= rng.uniform(0.55, 1.0, alpha.shape).astype(np.float32)
    layer.putalpha(Image.fromarray(alpha.clip(0, 255).astype(np.uint8)))
    rotated = layer.rotate(angle, resample=Image.BICUBIC, expand=True)
    ox = sheet.px(cx) - rotated.width / 2
    oy = sheet.px(cy) - rotated.height / 2
    sheet.img.paste(rotated, (round(ox), round(oy)), rotated)
    theta = math.radians(angle)
    for unit, (x0, y0, x1, y1) in marks:
        pts = []
        for px, py in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            dx, dy = px - w / 2, py - h / 2
            rx = dx * math.cos(theta) + dy * math.sin(theta)
            ry = -dx * math.sin(theta) + dy * math.cos(theta)
            pts.append((rx + rotated.width / 2 + round(ox), ry + rotated.height / 2 + round(oy)))
        box = (
            min(p[0] for p in pts),
            min(p[1] for p in pts),
            max(p[0] for p in pts),
            max(p[1] for p in pts),
        )
        sheet.mark(unit, box, "stamp")


# ---------------------------------------------------------------- degradation and saving


def rotate_box(box, angle: float, size: tuple[int, int]) -> tuple[float, ...]:
    """The box after the page was turned by `angle` degrees counter-clockwise."""
    cx, cy = size[0] / 2, size[1] / 2
    theta = math.radians(angle)
    pts = []
    for px, py in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])):
        dx, dy = px - cx, py - cy
        pts.append(
            (
                cx + dx * math.cos(theta) + dy * math.sin(theta),
                cy - dx * math.sin(theta) + dy * math.cos(theta),
            )
        )
    return (
        min(p[0] for p in pts),
        min(p[1] for p in pts),
        max(p[0] for p in pts),
        max(p[1] for p in pts),
    )


def finish_page(doc: Doc, sheet: Sheet) -> tuple[Image.Image, float]:
    """Applies the document's degradations; returns the image and the skew angle."""
    img = sheet.img
    nrng = np.random.default_rng(zlib.crc32(f"{doc.name}/{sheet.number}".encode()))
    angle = 0.0
    if "skew" in doc.degrade:
        angle = doc.rng.choice((-1, 1)) * doc.rng.uniform(1.8, 2.2)
        img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=WHITE)
    if "fax" in doc.degrade:
        gray = img.convert("L")
        scale = 100 / doc.dpi
        gray = gray.resize((round(gray.width * scale), round(gray.height * scale)), Image.LANCZOS)
        arr = np.asarray(gray, dtype=np.float32) + nrng.normal(0, 14, (gray.height, gray.width))
        black = arr < 150
        black |= nrng.random(arr.shape) < 0.0022
        for _ in range(int(nrng.integers(25, 60))):
            y, x = int(nrng.integers(0, arr.shape[0] - 3)), int(nrng.integers(0, arr.shape[1] - 3))
            black[y : y + int(nrng.integers(1, 3)), x : x + int(nrng.integers(1, 4))] = True
        for _ in range(int(nrng.integers(1, 4))):
            y = int(nrng.integers(0, arr.shape[0]))
            x0 = int(nrng.integers(0, arr.shape[1] // 2))
            black[y, x0 : x0 + int(nrng.integers(40, arr.shape[1] // 2))] = True
        return Image.fromarray(np.where(black, 0, 255).astype(np.uint8)).convert("1"), angle
    arr = np.asarray(img, dtype=np.float32)
    if "faint" in doc.degrade:
        arr = 255 - (255 - arr) * 0.36
        arr = arr * np.array([0.99, 0.985, 0.965], dtype=np.float32)
    if not doc.clean:
        arr = arr * np.array([0.992, 0.99, 0.978], dtype=np.float32)
        arr = arr + nrng.normal(0, 3.0, arr.shape[:2])[..., None]
    img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))
    spread = arr.max(axis=2) - arr.min(axis=2)
    if np.count_nonzero(spread > 45) < 50:
        img = img.convert("L")
    if "blur" in doc.degrade:
        img = img.filter(ImageFilter.GaussianBlur(radius=1.25 * doc.dpi / 200))
    return img, angle


def save(doc: Doc, images: list[Image.Image], path: Path) -> None:
    dpi = 100 if "fax" in doc.degrade else doc.dpi
    if doc.fmt == "pdf":
        images[0].save(path, "PDF", resolution=float(dpi), save_all=True, append_images=images[1:])
    elif doc.fmt == "tif":
        compression = "group4" if images[0].mode == "1" else "tiff_lzw"
        images[0].save(
            path,
            save_all=len(images) > 1,
            append_images=images[1:],
            compression=compression,
            dpi=(dpi, dpi),
        )
    elif len(images) != 1:
        raise ValueError(f"{doc.name}: {doc.fmt} holds one page, not {len(images)}")
    elif doc.fmt == "jpg":
        quality = 25 if "jpeg" in doc.degrade else 88
        images[0].convert("RGB").save(path, quality=quality, dpi=(dpi, dpi))
    else:
        images[0].save(path, dpi=(dpi, dpi))


def how_label(occ: Occ, doc: Doc) -> str:
    mods = set(occ.mods) | {d for d in doc.degrade if d in MODS}
    return "+".join([occ.how, *[m for m in MODS if m in mods]])


def fraction(boxes: list, size: tuple[int, int], angle: float) -> list[float]:
    """The union of `boxes` as fractions of the page, after the page's skew."""
    box = (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )
    if angle:
        box = rotate_box(box, angle, size)
    return [round(max(0.0, min(1.0, v / size[i % 2])), 4) for i, v in enumerate(box)]


def finish(doc: Doc, out: Path, preview: Path | None = None) -> dict:
    images, sizes, angles = [], [], []
    for sheet in doc.sheets:
        img, angle = finish_page(doc, sheet)
        images.append(img)
        sizes.append(sheet.size)
        angles.append(angle)
    path = out / f"{doc.name}.{doc.fmt}"
    save(doc, images, path)
    if preview is not None:
        for number, img in enumerate(images, 1):
            small = img.convert("RGB")
            small = small.resize((900, round(small.height * 900 / small.width)))
            small.save(preview / f"{doc.name}-{number}.png")
    expect: dict[tuple[int, str], dict] = {}
    for occ in doc.occs:
        if occ.page is None:
            raise ValueError(f"{doc.name}: occurrence {occ.text!r} was never drawn")
        frac = fraction(occ.boxes, sizes[occ.page - 1], angles[occ.page - 1])
        for name in occ.terms:
            entry = expect.setdefault(
                (occ.page, name),
                {"term": name, "page": occ.page, "count": 0, "how": [], "text": [], "boxes": []},
            )
            entry["count"] += 1
            entry["how"].append(how_label(occ, doc))
            entry["text"].append(occ.text)
            entry["boxes"].append(frac)
    expected_terms = {name for _, name in expect}
    decoys = []
    for decoy in doc.decoys:
        if decoy.page is None:
            raise ValueError(f"{doc.name}: decoy {decoy.text!r} was never drawn")
        decoys.append(
            {
                "term": decoy.term,
                "page": decoy.page,
                "text": decoy.text,
                "why": decoy.why,
                "box": fraction(decoy.boxes, sizes[decoy.page - 1], angles[decoy.page - 1]),
            }
        )
    absent = sorted({d["term"] for d in decoys} - expected_terms)
    return {
        "file": path.name,
        "pages": len(images),
        "kind": doc.name.split("_")[1],
        "lang": doc.lang,
        "dpi": 100 if "fax" in doc.degrade else doc.dpi,
        "degrade": list(doc.degrade),
        "expect": [expect[k] for k in sorted(expect)],
        "absent": absent,
        "decoys": decoys,
        "notes": doc.notes,
    }


# ---------------------------------------------------------------- self-checks


def term_pattern(term: Term, pattern: str) -> re.Pattern:
    tokens = [t for t in re.split(r"[^0-9a-z]+", fold(pattern)) if t]
    body = r"[\W_]*".join(re.escape(t) for t in tokens)
    if term.whole_words is False:
        return re.compile(body)
    return re.compile(rf"(?<![0-9a-z]){body}(?![0-9a-z])")


CONTEXT_WORDS = sorted({w for t in TERMS for w in (*t.near, *t.not_near)}, key=len, reverse=True)


def check_filler(doc: Doc) -> list[str]:
    """Problems with the text around the planted occurrences: a term, a regex
    match, a near miss or a context word that nobody planted."""
    problems = []
    for sheet in doc.sheets:
        filler = "\n".join(sheet.filler)
        flat = fold(filler.replace("\n", " "))
        for term in TERMS:
            if term.regex:
                m = re.search(term.regex, filler.replace("\n", " "), re.IGNORECASE)
                if m:
                    problems.append(f"p{sheet.number}: unplanted {term.name}: {m.group(0)!r}")
                continue
            for pattern in term.match:
                m = term_pattern(term, pattern).search(flat)
                if m:
                    problems.append(f"p{sheet.number}: unplanted {term.name}: {m.group(0)!r}")
            if term.fuzzy in (0, "off") or len(compact(term.match[0])) < 7:
                continue
            words = re.findall(r"[0-9a-z]+", flat)
            n = len(term.match[0].split())
            want = compact(term.match[0])
            for i in range(len(words) - n + 1):
                got = "".join(words[i : i + n])
                if abs(len(got) - len(want)) <= 2 and edits(got, want) <= 2:
                    problems.append(f"p{sheet.number}: near miss of {term.name}: {got!r}")
        problems += [f"p{sheet.number}: {p}" for p in check_context(sheet)]
    return problems


def check_context(sheet: Sheet) -> list[str]:
    """A planted occurrence must not have a ``not_near`` word within its window,
    and must have a ``near`` word there; a decoy that relies on the missing
    ``near`` word must not have one."""
    page = "\n".join(sheet.lines)
    hits = {
        word: [m.span() for m in re.finditer(re.escape(word), page, re.IGNORECASE)]
        for word in CONTEXT_WORDS
    }

    def distance(start: int, end: int, words: tuple[str, ...]) -> int:
        spans = [span for word in words for span in hits[word]]
        return min((max(a - end, start - b, 0) for a, b in spans), default=10**6)

    problems = []
    for start, end, unit in sheet.spans:
        for occ in unit.occs:
            for name in occ.terms:
                term = BY_NAME[name]
                window = term.window or 60
                if term.not_near and distance(start, end, term.not_near) <= window + 15:
                    problems.append(f"{name} {unit.text!r} has a not_near word close by")
                if term.near and distance(start, end, term.near) > window - 12:
                    problems.append(f"{name} {unit.text!r} has no near word close by")
        if unit.decoy is not None:
            term = BY_NAME[unit.decoy.term]
            if term.near and distance(start, end, term.near) <= (term.window or 60) + 15:
                problems.append(f"decoy {unit.text!r} has a near word close by")
    return problems


# ---------------------------------------------------------------- text pools

LETTER_DE = (
    "vielen Dank für Ihr Schreiben vom 14. des Monats und das freundliche Telefonat mit "
    "Ihrem Kollegen.",
    "Wie besprochen übersenden wir Ihnen anbei die überarbeiteten Unterlagen zur Prüfung.",
    "Bitte prüfen Sie die Angaben und geben Sie uns bis Ende der kommenden Woche eine kurze "
    "Rückmeldung.",
    "Für Rückfragen stehen wir Ihnen selbstverständlich jederzeit gern zur Verfügung.",
    "Die Lieferung erfolgt wie vereinbart frei Haus an die von Ihnen genannte Anschrift.",
    "Wir bitten um Verständnis, dass sich die Bearbeitung wegen der Feiertage etwas verzögert hat.",
    "Die beigefügte Aufstellung enthält alle Positionen, die im laufenden Quartal abgerechnet "
    "werden.",
    "Sollten sich an den Terminen Änderungen ergeben, informieren wir Sie umgehend.",
    "Unsere Techniker werden sich rechtzeitig vor dem Einsatz mit Ihnen in Verbindung setzen.",
    "Eine Kopie dieses Schreibens geht zur Kenntnis an die Geschäftsleitung.",
    "Die Unterlagen liegen diesem Schreiben in zweifacher Ausfertigung bei.",
    "Bitte senden Sie uns ein unterschriebenes Exemplar zurück.",
    "Der Kostenvoranschlag ist bis zum Ende des laufenden Monats gültig.",
    "Die Abnahme der Arbeiten ist für die zweite Kalenderwoche des neuen Jahres geplant.",
    "Wir haben Ihre Hinweise an die zuständige Fachabteilung weitergeleitet.",
    "Die Rechnung wird nach vollständiger Leistungserbringung gesondert gestellt.",
)
CONTRACT_DE = (
    "Die Parteien verpflichten sich, alle im Rahmen der Zusammenarbeit erhaltenen Informationen "
    "vertraulich zu behandeln.",
    "Diese Verpflichtung gilt nicht für Informationen, die bei Abschluss dieses Vertrages bereits "
    "öffentlich bekannt waren.",
    "Änderungen und Ergänzungen dieses Vertrages bedürfen zu ihrer Wirksamkeit der Schriftform.",
    "Sollte eine Bestimmung dieses Vertrages unwirksam sein, bleibt die Wirksamkeit der übrigen "
    "Bestimmungen unberührt.",
    "Gerichtsstand ist, soweit gesetzlich zulässig, der Sitz des Auftraggebers.",
    "Der Vertrag tritt mit Unterzeichnung durch beide Parteien in Kraft.",
    "Er kann von jeder Partei mit einer Frist von drei Monaten zum Ende eines Kalenderjahres "
    "gekündigt werden.",
    "Die Pflichten aus diesem Vertrag bestehen auch nach seiner Beendigung für fünf Jahre fort.",
    "Auf Verlangen sind sämtliche Unterlagen einschließlich aller Kopien unverzüglich "
    "zurückzugeben oder zu vernichten.",
    "Mitarbeitende und Unterauftragnehmer sind in gleicher Weise zur Verschwiegenheit zu "
    "verpflichten.",
    "Es gilt das Recht der Bundesrepublik Deutschland unter Ausschluss des UN-Kaufrechts.",
    "Die Vergütung wird nach Abnahme der jeweiligen Leistung innerhalb von dreißig Tagen fällig.",
    "Der Auftragnehmer haftet für Vorsatz und grobe Fahrlässigkeit nach den gesetzlichen "
    "Vorschriften.",
    "Die Übertragung von Rechten und Pflichten auf Dritte bedarf der vorherigen Zustimmung.",
)
MINUTES_DE = (
    "Das Protokoll der letzten Sitzung wurde ohne Änderungen genehmigt.",
    "Die Teilnehmenden wurden über den aktuellen Stand der Planung informiert.",
    "Es wurde vereinbart, die offenen Punkte bis zur nächsten Sitzung zu klären.",
    "Herr Brenner berichtet, dass die Ausschreibung planmäßig veröffentlicht wurde.",
    "Frau Lindqvist weist darauf hin, dass die Budgetfreigabe noch aussteht.",
    "Die Diskussion ergab keinen weiteren Handlungsbedarf.",
    "Der Zeitplan bleibt unverändert; die nächste Sitzung findet in vier Wochen statt.",
    "Die Kosten liegen derzeit rund acht Prozent unter der Planung.",
    "Ein Entwurf der Stellungnahme wird vorab per E-Mail verteilt.",
    "Die Schulung der Mitarbeitenden soll im Frühjahr abgeschlossen sein.",
    "Die Raumbelegung für die Testphase wird mit der Hausverwaltung abgestimmt.",
    "Herr Dornbach sagt zu, die Zahlen bis Monatsende nachzureichen.",
    "Die Risikoliste wurde durchgesehen; zwei Einträge konnten geschlossen werden.",
)
TECH_DE = (
    "Die Messwerte der letzten vier Wochen zeigen keine Auffälligkeiten.",
    "Der Austausch der Steuerplatinen erfolgt im Rahmen der jährlichen Wartung.",
    "Die Firmware wurde auf allen Geräten auf den aktuellen Stand gebracht.",
    "Im Serverraum wurde eine zusätzliche Klimaeinheit installiert.",
    "Die Protokolldateien werden täglich gesichert und dreißig Tage aufbewahrt.",
    "Die Netzwerkverkabelung im zweiten Obergeschoss ist abgeschlossen.",
    "Für die Inbetriebnahme ist ein Zeitfenster von zwei Tagen vorgesehen.",
    "Die Dokumentation der Schnittstellen liegt in der Version 2.3 vor.",
    "Bei Stromausfall überbrückt die USV etwa vierzig Minuten.",
    "Die Kalibrierung der Sensoren wird halbjährlich durch den Hersteller durchgeführt.",
    "Alle Meldelinien wurden einzeln ausgelöst und ordnungsgemäß quittiert.",
    "Die Zugangsdaten für das Wartungsportal werden persönlich übergeben.",
)
NEWS_DE = (
    "Mit einem kleinen Empfang wurde am Freitag die neue Kantine im Erdgeschoss eröffnet.",
    "Das Sommerfest findet in diesem Jahr am zweiten Samstag im Juli auf dem Werksgelände statt.",
    "Zwölf Auszubildende haben ihre Abschlussprüfung erfolgreich bestanden.",
    "Die Betriebssportgruppe sucht noch Verstärkung für das Team beim Firmenlauf.",
    "Seit Anfang des Monats steht allen Beschäftigten ein Angebot zum Fahrradleasing offen.",
    "Die Bibliothek im dritten Stock ist ab sofort auch nachmittags geöffnet.",
    "Wir gratulieren allen Jubilarinnen und Jubilaren zu ihrem Dienstjubiläum.",
    "Die Parkplätze an der Nordseite werden ab Mai neu markiert.",
    "Der Betriebsrat lädt zur Betriebsversammlung in die Stadthalle ein.",
    "Die neue Telefonanlage wird schrittweise in allen Abteilungen eingeführt.",
    "Anmeldungen nimmt das Personalbüro bis zum Monatsende entgegen.",
    "Fotos der Veranstaltung finden Sie im Intranet unter Aktuelles.",
    "Die Kolleginnen und Kollegen aus dem Einkauf freuen sich über Ihre Vorschläge.",
    "Das Gesundheitsmanagement bietet im April wieder kostenlose Sehtests an.",
    "In der Lehrwerkstatt wurde eine neue Fräsmaschine in Betrieb genommen.",
    "Die Kinderbetreuung in den Sommerferien ist bereits zu zwei Dritteln ausgebucht.",
    "Der Werksverkehr fährt ab Montag nach dem Sommerfahrplan.",
    "Beim Firmenlauf gingen in diesem Jahr 86 Beschäftigte an den Start.",
    "Die Blutspendeaktion des Roten Kreuzes findet am 14. Mai im Foyer statt.",
    "Neue Ladesäulen für Elektroautos stehen auf dem Besucherparkplatz bereit.",
    "Das Archiv zieht in das Untergeschoss von Gebäude C um.",
    "Unsere Lernplattform bietet jetzt auch Kurse zur Tabellenkalkulation an.",
    "Die Ausbildungsmesse im Herbst sucht noch Kolleginnen und Kollegen für den Stand.",
    "Die Umstellung auf papierlose Reisekostenabrechnung ist abgeschlossen.",
    "Im Foyer zeigt eine kleine Ausstellung Fotos aus hundert Jahren Werksgeschichte.",
)
MAIL_DE = (
    "danke für die schnelle Rückmeldung.",
    "anbei wie besprochen die aktuelle Fassung.",
    "Können wir uns am Donnerstag kurz dazu abstimmen?",
    "Ich bin ab Montag wieder im Büro und melde mich dann.",
    "Bitte gebt mir bis morgen Mittag Bescheid, ob der Termin passt.",
    "Die Präsentation schicke ich separat, sie ist zu groß für den Anhang.",
    "Ich habe die Anmerkungen eingearbeitet und die Tabelle ergänzt.",
    "Die Telefonkonferenz ist für 14 Uhr eingestellt, die Einwahldaten stehen im Termin.",
    "Falls noch etwas fehlt, gib bitte kurz Bescheid.",
)
EN = (
    "Thank you for your message and for the documents you sent last week.",
    "Please find attached the revised schedule for the next phase.",
    "We would appreciate your feedback by the end of the month.",
    "The shipment is expected to arrive at your warehouse on Tuesday.",
    "All prices are quoted in euros and exclude value added tax.",
    "Let me know if you need any further information.",
    "The review meeting has been moved to the second week of March.",
    "Our engineers will be on site from 8 a.m. to prepare the installation.",
    "Specifications are subject to change without notice.",
    "Operating temperature range and power consumption are listed in the table below.",
    "Both parties agreed to keep the steering group small until the pilot is complete.",
    "The budget request will be submitted to the board in the next quarter.",
)


# ---------------------------------------------------------------- templates


def footer_cols(sheet: Sheet, cols: list[list[Content]], *, y: float = 279.5, pt: float = 7):
    """A letter footer: small columns under a rule, in the bottom 6 % of the page."""
    doc = sheet.doc
    sheet.hline(25, 190, y - 1.8, color=RULE, width=0.2)
    x = 25.0
    width = 165 / len(cols)
    for col in cols:
        yy = y
        for line in col:
            yy = text(
                sheet, x, yy, width - 3, doc.units(line, doc.st(pt, color=GREY)), zone="footer"
            )
        x += width


def running_header(sheet: Sheet, left: Content, right: Content = None, *, pt: float = 7.5):
    doc = sheet.doc
    style = doc.st(pt, color=GREY)
    top = 8.5 if sheet.hmm > 250 else 6.0
    text(sheet, 25, top, 110, doc.units(left, style), zone="header")
    if right is not None:
        text(sheet, sheet.wmm - 85, top, 65, doc.units(right, style), align="right", zone="header")
    sheet.hline(25, sheet.wmm - 20, top + 5.2, color=RULE, width=0.2)


def running_footer(sheet: Sheet, left: Content, right: Content = None, *, pt: float = 7.5):
    doc = sheet.doc
    style = doc.st(pt, color=GREY)
    y = sheet.hmm - 13.5
    sheet.hline(25, sheet.wmm - 20, y - 1.5, color=RULE, width=0.2)
    text(sheet, 25, y, 120, doc.units(left, style), zone="footer")
    if right is not None:
        text(sheet, sheet.wmm - 75, y, 55, doc.units(right, style), align="right", zone="footer")


def letter(
    d: Doc,
    *,
    sender: Content,
    tagline: Content = None,
    return_line: Content,
    recipient: list[Content],
    refs: list[tuple[str, Content]] = (),
    subject: Content,
    body: list[Content],
    greeting: str | None = "Sehr geehrte Damen und Herren,",
    closing: str | None = "Mit freundlichen Grüßen",
    signer: list[Content] = (),
    footer: list[list[Content]] = (),
    after: Callable[[Story], None] | None = None,
    header2: Content = None,
    sender_style: Style | None = None,
) -> Story:
    """A DIN 5008 business letter; later pages get a small running header."""

    def furniture(sheet: Sheet) -> None:
        if footer:
            footer_cols(sheet, footer)
        if sheet.number > 1:
            running_header(sheet, header2 or "", f"Seite {sheet.number}")

    story = Story(d, top=27, bottom=268, furniture=furniture)
    s = story.new_page()
    kind = d.rng.randrange(3)
    s.logo(25, 11, 9, d.accent, kind)
    style = sender_style or d.hs(17, color=d.accent)
    y = text(s, 37, 11.5, 150, d.units(sender, style), zone="header")
    if tagline:
        text(s, 37, max(y, 20.5), 150, d.units(tagline, d.st(8, color=GREY)))
    text(s, 25, 45, 85, d.units(return_line, d.st(7, color=GREY)))
    s.hline(25, 110, 48.3, color=GREY, width=0.15)
    y = 51.5
    for line in recipient:
        if line == "":
            y += 4.2
            continue
        y = text(s, 25, y, 85, d.units(line, d.st()), leading=1.22)
    y = 50.5
    for label, value in refs:
        text(s, 125, y, 28, d.units(label, d.st(7.5, color=GREY)))
        y = text(s, 150, y, 42, d.units(value, d.st(8.5)), leading=1.25)
    story.y = 100
    story.para(subject, d.st(weight="b"), after=5)
    if greeting:
        story.para(greeting, after=3)
    for para in body:
        story.para(para, after=3)
    if after:
        after(story)
    if closing:
        story.para(closing, before=2, after=1, keep=30)
        story.s.scribble(27, story.y + 7, 32)
        story.space(12)
    for line in signer:
        story.para(line, after=0.4)
    return story


def email(
    d: Doc,
    *,
    sender: Content,
    fields: list[tuple[str, Content]],
    body: list[Content],
    signature: list[Content] = (),
    quoted: list[Content] = (),
    quoted_fields: list[tuple[str, Content]] = (),
    printed_by: str = "",
) -> Story:
    """An e-mail printed from a mail client."""

    def furniture(sheet: Sheet) -> None:
        if printed_by:
            running_header(sheet, printed_by, f"{sheet.number}")

    story = Story(d, left=20, width=170, top=22, bottom=282, furniture=furniture)
    s = story.new_page()
    y = text(s, 20, 19, 170, d.units(sender, d.hs(14)))
    s.hline(20, 190, y + 0.8, color=INK, width=0.5)
    y += 3
    for label, value in fields:
        text(s, 20, y, 30, d.units(label, d.st(d.pt - 0.5, "b")))
        y = text(s, 50, y, 140, d.units(value, d.st(d.pt - 0.5)), leading=1.25)
    story.y = y + 6
    for para in body:
        story.para(para, after=3.2)
    for line in signature:
        story.para(line, d.st(d.pt - 1, color=GREY), after=0.3)
    if quoted or quoted_fields:
        story.para("-----Ursprüngliche Nachricht-----", d.st(d.pt - 0.5), before=6, after=1)
        for label, value in quoted_fields:
            story.para([f"{label}", value], d.st(d.pt - 0.5), after=0.3)
        story.space(3)
        for para in quoted:
            story.para(para, after=3)
    return story


def title_block(story: Story, title: Content, style: Style, meta: list[tuple[str, Content]] = ()):
    d = story.doc
    story.para(title, style, after=3)
    if meta:
        story.table(
            [38, story.width - 38],
            [[label, value] for label, value in meta],
            d.st(d.pt - 1),
            head=False,
            head_fill=None,
            grid=False,
            pad=1.0,
        )


def slide(
    d: Doc,
    *,
    title: Content,
    bullets: list[Content] = (),
    sub: Content = None,
    number: int = 1,
    footer_left: Content = "",
    footer_right: Content = None,
    columns: tuple[list[Content], list[Content]] | None = None,
    band: tuple[int, int, int] | None = None,
) -> Sheet:
    """One slide on an A4 landscape page, as printed from a presentation."""
    s = d.page(landscape=True)
    band = band or d.accent
    s.rect(0, 0, 297, 9, fill=band)
    y = text(s, 20, 20, 257, d.units(title, d.hs(26, color=band)))
    if sub:
        y = text(s, 20, y + 1, 257, d.units(sub, d.st(14, color=GREY)))
    s.hline(20, 277, y + 3, color=band, width=0.6)
    y += 10
    style = d.st(16)
    for item in bullets:
        text(s, 24, y, 8, d.units("•", style.but(color=band)))
        y = text(s, 32, y, 240, d.units(item, style), leading=1.3) + 3.5
    if columns:
        top = y
        for i, col in enumerate(columns):
            yy = top
            for item in col:
                yy = text(s, 24 + i * 128, yy, 118, d.units(item, style), leading=1.3) + 3
    running_footer(
        s, footer_left, footer_right if footer_right is not None else f"{number}", pt=8.5
    )
    return s


# ---------------------------------------------------------------- the documents

BUILDERS: list[tuple[str, dict, Callable[[Doc], None]]] = []


def doc(name: str, **kw: Any) -> Callable[[Callable[[Doc], None]], Callable[[Doc], None]]:
    def register(fn: Callable[[Doc], None]) -> Callable[[Doc], None]:
        BUILDERS.append((name, kw, fn))
        return fn

    return register


BRANDTNER_FOOTER = [
    ["{brandtner:plain} GmbH & Co. KG", "Gewerbering 7 · 73760 Ostfildern"],
    ["Amtsgericht Stuttgart HRA 72214", "Geschäftsführer: Paul Brandtner"],
    ["Kreissparkasse Esslingen", "IBAN {iban:plain} · BIC ESSLDE66XXX"],
]


def invoice_table(
    story: Story,
    rows: list[list[Content]],
    totals: list[tuple[str, str]],
    *,
    widths: tuple[float, ...] = (12, 26, 66, 17, 22, 22),
    notes: list[Content] = (),
) -> None:
    d = story.doc
    aligns = ("left", "left", "left", "right", "right", "right")[: len(widths)]
    story.table(list(widths), rows, d.st(d.pt - 1.5), aligns=aligns)
    for label, value in totals:
        st = d.st(d.pt - 1, "b" if label in ("Rechnungsbetrag", "Gesamtbetrag", "Total") else "r")
        s = story.s
        text(s, 112, story.y, 48, d.units(label, st))
        story.y = text(s, 160, story.y, 30, d.units(value, st), align="right") + 0.6
    story.space(4)
    for note in notes:
        story.para(note, d.st(d.pt - 1), after=2)


def fax_tti(sheet: Sheet, line: Content) -> None:
    """The transmit line the sending fax machine prints along the top edge."""
    text(sheet, 8, 3.2, 194, sheet.doc.units(line, Style("courier", "r", 8.5)), zone="header")


def minutes(
    d: Doc,
    *,
    title: Content,
    meta: list[tuple[str, Content]],
    tops: list[tuple[str, list[Content]]],
    org: Content = None,
    header: Content = None,
    footer: Content = None,
    extra: dict[int, Callable[[Story], None]] | None = None,
    label: str = "TOP",
    page_word: str = "Seite",
) -> Story:
    def furniture(sheet: Sheet) -> None:
        if header is not None:
            running_header(sheet, header, f"{page_word} {sheet.number}")
        if footer is not None:
            running_footer(sheet, footer)

    story = Story(d, top=24, bottom=270, furniture=furniture)
    if org:
        story.para(org, d.hs(9, color=d.accent), after=1)
    title_block(story, title, d.hs(16), meta)
    for i, (heading, paras) in enumerate(tops, 1):
        story.heading(f"{label} {i} – {heading}", d.hs(11))
        for para in paras:
            story.para(para)
        if extra and i in extra:
            extra[i](story)
    if extra and 0 in extra:
        extra[0](story)
    return story


def contract(
    d: Doc,
    *,
    title: Content,
    preamble: list[Content],
    sections: list[tuple[str, list[Content]]],
    signatures: list[tuple[Content, Content, Content]],
    subtitle: Content = None,
    header: Content = None,
    footer: Content = "",
    sign: str = "§",
    page_word: str = "Seite",
) -> Story:
    def furniture(sheet: Sheet) -> None:
        if header is not None:
            running_header(sheet, header)
        running_footer(sheet, footer, f"{page_word} {sheet.number}")

    story = Story(d, top=26, bottom=268, furniture=furniture)
    story.para(title, d.hs(18), align="center", after=2)
    if subtitle:
        story.para(subtitle, d.st(10, color=GREY), align="center", after=6)
    for para in preamble:
        story.para(para, align="center", after=2)
    story.space(4)
    for i, (heading, paras) in enumerate(sections, 1):
        story.heading(f"{sign} {i} {heading}" if sign else f"{i}. {heading}", d.hs(11))
        for para in paras:
            story.para(para)
    story.space(8)
    if story.y + 42 > story.bottom:
        story.new_page()
    s, y = story.s, story.y
    for j, (place, name, role) in enumerate(signatures):
        x = 25 + j * 88
        text(s, x, y, 76, d.units(place, d.st(9.5)))
        s.scribble(x + 3, y + 17, 30)
        s.hline(x, x + 74, y + 22, color=INK, width=0.2)
        yy = text(s, x, y + 23.5, 76, d.units(name, d.st(9.5)))
        text(s, x, yy, 76, d.units(role, d.st(8.5, color=GREY)))
    story.y = y + 42
    return story


def memo(
    d: Doc,
    *,
    kind: Content,
    fields: list[tuple[str, Content]],
    body: list[Content],
    sign: list[Content] = (),
    header: Content = None,
    footer: Content = None,
) -> Story:
    def furniture(sheet: Sheet) -> None:
        if header is not None:
            running_header(sheet, header)
        if footer is not None:
            running_footer(sheet, footer)

    story = Story(d, top=24, bottom=270, furniture=furniture)
    story.para(kind, d.hs(20, color=d.accent), after=3)
    story.table(
        [36, 129],
        [[label, value] for label, value in fields],
        d.st(d.pt - 0.5),
        head=False,
        head_fill=None,
        pad=1.7,
    )
    story.space(3)
    for para in body:
        story.para(para, after=3)
    for line in sign:
        story.para(line, after=0.4)
    return story


def fax_cover(
    d: Doc,
    *,
    tti: Content,
    company: Content,
    company_lines: list[Content],
    fields: list[tuple[str, Content, str, Content]],
    message: list[Content],
    sign: list[Content] = (),
) -> Story:
    story = Story(d, top=24, bottom=276)
    s = story.new_page()
    fax_tti(s, tti)
    y = text(s, 25, 13, 150, d.units(company, d.hs(18)))
    small = d.st(10) if "fax" in d.degrade else d.st(9, color=GREY)  # grey 9 pt does not survive
    for line in company_lines:
        y = text(s, 25, y, 150, d.units(line, small))
    story.y = y + 7
    story.para("TELEFAX", d.hs(30), after=4)
    story.table(
        [24, 58.5, 24, 58.5],
        [list(row) for row in fields],
        d.st(d.pt - 0.5),
        head=False,
        head_fill=None,
        pad=2.2,
    )
    story.space(5)
    for para in message:
        story.para(para, after=3)
    for line in sign:
        story.para(line, after=0.5)
    return story


def column_block(
    d: Doc,
    s: Sheet,
    y: float,
    cols: list[list[tuple[str, Content]]],
    *,
    left: float = 18,
    width: float = 174,
    gap: float = 7,
    body: Style | None = None,
) -> float:
    """Articles side by side; each column is a list of (h)eadline, (k)icker and
    (p)aragraph items."""
    n = len(cols)
    cw = (width - gap * (n - 1)) / n
    body = body or d.st()
    ends = []
    for i, items in enumerate(cols):
        x = left + i * (cw + gap)
        yy = y
        for kind, content in items:
            if kind == "h":
                yy = text(s, x, yy + 1, cw, d.units(content, d.hs(13)), leading=1.15) + 1.4
            elif kind == "k":
                yy = text(s, x, yy, cw, d.units(content, d.st(8, "i", color=GREY))) + 1
            else:
                yy = text(s, x, yy, cw, d.units(content, body), align="justify", leading=1.3) + 2
        ends.append(yy)
    for i in range(1, n):
        s.vline(left + i * (cw + gap) - gap / 2, y + 1, max(ends) - 1, color=LIGHT)
    return max(ends)


def newsletter(
    d: Doc,
    *,
    masthead: str,
    issue: Content,
    pages: list[list[list[list[tuple[str, Content]]]]],
    footer: Content = None,
) -> None:
    body = d.st(d.pt)
    for number, blocks in enumerate(pages, 1):
        s = d.page()
        if number == 1:
            s.rect(18, 12, 192, 31, fill=d.accent)
            text(s, 23, 14.5, 160, d.units(masthead, d.hs(30, color=WHITE)))
            y = text(s, 18, 33.5, 174, d.units(issue, d.st(8.5, color=GREY))) + 3
        else:
            running_header(s, f"{masthead} – Fortsetzung", f"Seite {number}")
            y = 21
        for block in blocks:
            y = column_block(d, s, y, block, body=body) + 3
            s.hline(18, 192, y - 1.6, color=RULE, width=0.2)
        if y < 200:
            photo(s, 18, y + 2, 174, min(95.0, 262 - y - 12), f"{d.name}/{number}")
            caption = d.rng.choice(
                (
                    "Foto: Unternehmenskommunikation",
                    "Blick in die neue Halle 3 (Foto: Werkspost)",
                    "Gut besucht: die Betriebsversammlung im März",
                )
            )
            text(
                s,
                18,
                y + 2 + min(95.0, 262 - y - 12) + 1.5,
                174,
                d.units(caption, d.st(8, "i", color=GREY)),
            )
        if footer is not None:
            running_footer(s, footer, f"{number}")


def photo(s: Sheet, x: float, y: float, w: float, h: float, seed: str) -> None:
    """A printed photograph: soft shapes and shading, no text."""
    rng = np.random.default_rng(zlib.crc32(seed.encode()))
    size = (max(8, round(s.px(w))), max(8, round(s.px(h))))
    small = rng.normal(0, 1, (6, 9, 3)).cumsum(axis=1)
    small = (small - small.min()) / (np.ptp(small) + 1e-6)
    tint = rng.uniform(0.35, 0.8, 3)
    img = Image.fromarray((small * 150 * tint + 60).clip(0, 255).astype(np.uint8))
    img = img.resize(size, Image.BICUBIC).filter(ImageFilter.GaussianBlur(radius=size[0] / 60))
    s.img.paste(img, (round(s.px(x)), round(s.px(y))))


def slide_footer(company: str) -> Content:
    return f"{company} · Stand März 2026"


# ---- letters


@doc("t001_brief_brandtner", family="times", head="arial")
def t001(d: Doc) -> None:
    letter(
        d,
        sender="{brandtner:plain}",
        tagline="Feinmechanik · Sensorik · Prüftechnik",
        return_line="{brandtner:plain} · Gewerbering 7 · 73760 Ostfildern",
        recipient=[
            "Helvetor Maschinenbau AG",
            "z. Hd. Frau {schoellhorn:plain}",
            "Seestraße 40",
            "",
            "88045 Friedrichshafen",
        ],
        refs=[
            ("Ihr Zeichen", "MS/2026-03"),
            ("Unser Zeichen", "vk-117"),
            ("Telefon", "0711 348 82-20"),
            ("Datum", "12. März 2026"),
        ],
        subject="Angebot Präsenzsensoren {fs220:plain} für das {nord:plain}",
        greeting="Sehr geehrte Frau Schöllhorn,",
        body=[
            "vielen Dank für Ihre Anfrage vom 3. März. Gern bieten wir Ihnen die Lieferung und "
            "Montage von 48 Präsenzsensoren an. Grundlage ist die zwischen unseren Häusern "
            "bestehende {nda_de:hyphenated}, deren Bedingungen auch für dieses Angebot gelten.",
            [
                "Die Sensoren werden an die vorhandene {zutritt:plain} angebunden und über die "
                "bestehende Busleitung versorgt.",
                d.fill(LETTER_DE, 1),
            ],
            d.fill(LETTER_DE, 2),
        ],
        signer=["Viktor Kaminski", "Vertrieb Industriekunden"],
        footer=BRANDTNER_FOOTER,
    )


@doc("t002_brief_kessler", fmt="jpg", dpi=150, family="arial", degrade=("jpeg",))
def t002(d: Doc) -> None:
    letter(
        d,
        sender="{kessler:plain}",
        tagline="Kontraktlogistik · Lagerhaltung · Zollabwicklung",
        return_line="{kessler:plain} GmbH · {hafen:plain} · 28217 Bremen",
        recipient=[
            "Helvetor Maschinenbau AG",
            "Werkschutz",
            "Herrn {weiss:umlaut}",
            "Seestraße 40",
            "",
            "88045 Friedrichshafen",
        ],
        refs=[
            ("Kundennummer", "{kd:plain}"),
            ("Ansprechpartnerin", "Katrin Oltmanns"),
            ("Datum", "4. Februar 2026"),
        ],
        subject="Umbau der {zutritt:inner_space} am Standort Bremen",
        greeting="Sehr geehrter Herr Weißmüller,",
        body=[
            "wie angekündigt beginnen am 16. Februar die Arbeiten an unserem Lager in der "
            "{hafen:plain}. Während der Umbauphase ist die Zufahrt über Tor 3 gesperrt; "
            "Lieferungen werden über Tor 1 abgewickelt.",
            d.fill(LETTER_DE, 2),
            ["Bitte geben Sie bei Rückfragen stets Ihre Kundennummer an.", d.fill(LETTER_DE, 1)],
        ],
        signer=["i. A. Katrin Oltmanns", "Standortleitung Bremen"],
        footer=[
            ["{kessler:plain} GmbH", "{hafen:plain} · 28217 Bremen"],
            ["Sparkasse Bremen", "IBAN {iban:plain}"],
            ["Amtsgericht Bremen HRB 30412", "USt-IdNr. DE 811 204 336"],
        ],
    )


@doc("t003_brief_quadrant", dpi=300, family="helv")
def t003(d: Doc) -> None:
    letter(
        d,
        sender="[[Quadrant Systems|quadrant|letterhead, outside the body zone]]",
        tagline="Sicherheitstechnik · Integration · Service",
        return_line="Leopoldstraße 88 · 80802 München",
        recipient=[
            "{kessler:plain} GmbH",
            "Frau Katrin Oltmanns",
            "{hafen:plain}",
            "",
            "28217 Bremen",
        ],
        refs=[
            ("Vertrag", "WV-2026-031"),
            ("Bearbeiter", "Dr. Tobias Renner"),
            ("Datum", "18. März 2026"),
        ],
        subject="Wartung der {zutritt:plain} an Ihrem Standort Bremen",
        greeting="Sehr geehrte Frau Oltmanns,",
        body=[
            "wir bestätigen den Eingang Ihres Auftrags vom 11. März. Die {quadrant:plain} GmbH "
            "übernimmt ab dem 1. April die Wartung der Anlage einschließlich der Anbindung an die "
            "{bmz:plain}.",
            d.fill(LETTER_DE, 2),
            "Mit Vertragsbeginn gilt zwischen unseren Unternehmen das beigefügte {nda_en:plain}, "
            "das auf Wunsch Ihrer Konzernmutter in englischer Sprache abgefasst ist.",
        ],
        signer=["Dr. Tobias Renner", "Leiter Service Süd"],
        footer=[
            [
                "[[Quadrant Systems|quadrant|footer, outside the body zone]] GmbH",
                "Leopoldstraße 88 · 80802 München",
            ],
            ["Geschäftsführung: Dr. Ines Albers", "Amtsgericht München HRB 240118"],
            ["HypoVereinsbank München", "IBAN {iban:plain}"],
        ],
    )


@doc("t004_brief_geheimschutz", fmt="tif", family="dvserif", head="dvserif")
def t004(d: Doc) -> None:
    letter(
        d,
        sender="Helvetor Maschinenbau AG",
        tagline="Konzernsicherheit · Geheimschutzbeauftragter",
        return_line="Helvetor Maschinenbau AG · Seestraße 40 · 88045 Friedrichshafen",
        recipient=["Personalabteilung", "Frau Annika Szymanski", "im Hause"],
        refs=[("Aktenzeichen", "GS 14/2026"), ("Datum", "16. März 2026")],
        subject="{sue:plain} für Herrn {weiss:plain}",
        greeting="Sehr geehrte Frau Szymanski,",
        body=[
            "für die vorgesehene Tätigkeit im Rahmen der {fuchs:plain} ist für Herrn Weißmüller "
            "eine erweiterte {sue:hyphenated} mit Sicherheitsermittlungen erforderlich. Die "
            "Erklärung des Betroffenen liegt vor.",
            "Die Unterlagen für Herrn [[Günter Weißmüller|weiss|another first name]] aus dem "
            "Einkauf werden gesondert nachgereicht; eine Verwechslung der beiden Vorgänge ist "
            "auszuschließen.",
            d.fill(LETTER_DE, 2),
        ],
        signer=["Holger Wendt", "Geheimschutzbeauftragter"],
    )
    stamp(d.sheets[0], 152, 84, ["EINGEGANGEN", "17. März 2026"], color=STAMP_BLUE, angle=8, pt=11)


@doc("t005_brief_vireon", family="freeserif", head="freeserif", degrade=("skew",))
def t005(d: Doc) -> None:
    letter(
        d,
        sender="{vireon@1:plain}",
        tagline="Niederlassung Deutschland · Rheinauhafen 3 · 50678 Köln",
        return_line="Rheinauhafen 3 · 50678 Köln",
        recipient=[
            "{brandtner@1:plain} GmbH & Co. KG",
            "Herrn Viktor Kaminski",
            "Gewerbering 7",
            "",
            "73760 Ostfildern",
        ],
        refs=[("Ihre Nachricht", "12. März 2026"), ("Datum", "19. März 2026")],
        subject="Besuchermanagement – {dsfa:plain}",
        greeting="Sehr geehrter Herr Kaminski,",
        body=[
            "für die geplante Erfassung von Besucherdaten an unserem Standort ist nach Art. 35 "
            "DSGVO eine {dsfa:hyphenated} durchzuführen. Wir bitten Sie, uns hierfür die "
            "technische Beschreibung der Sensorik zu überlassen.",
            "Bitte beachten Sie, dass die Lieferadresse der [[Vireon Pharmahandel|vireon|inside a "
            "longer word]] GmbH in Mannheim für diese Bestellung nicht zu verwenden ist.",
            [
                "Ansprechpartnerin bei der {vireon:plain} GmbH bleibt Frau "
                "{schoellhorn:hyphenated}.",
                d.fill(LETTER_DE, 1),
            ],
        ],
        signer=["Dr. Carla Menéndez", "Datenschutzbeauftragte"],
        footer=[
            ["{vireon@1:plain} Ltd.", "Niederlassung Deutschland"],
            ["Rheinauhafen 3 · 50678 Köln", "Tel. 0221 580 44-0"],
            ["Commerzbank Köln", "IBAN {iban:plain}"],
        ],
    )


@doc("t006_brief_silbentrennung", fmt="pdf", family="times", head="times", align="justify")
def t006(d: Doc) -> None:
    letter(
        d,
        sender="Ingenieurbüro Hartig & Partner",
        tagline="Beratende Ingenieure für Gebäudetechnik",
        return_line="Ingenieurbüro Hartig & Partner · Am Güterbahnhof 3 · 28195 Bremen",
        recipient=[
            "{kessler:plain} GmbH",
            "Technische Leitung",
            "Frau Katrin Oltmanns",
            "Postfach 10 44 21",
            "",
            "28044 Bremen",
        ],
        refs=[("Projekt", "2026-014"), ("Datum", "2. März 2026")],
        subject="Stellungnahme zur Sanierung der Leitstelle",
        body=[
            [
                "im Zuge der Sanierung der Leitstelle ist die bestehende {bmz:hyphenated} "
                "vollständig zu ersetzen.",
                d.fill(TECH_DE, 2),
            ],
            [
                "Zusätzlich empfehlen wir, die vorhandene {zutritt:hyphenated} an das neue "
                "{fruehwarn:plain} anzubinden, damit Alarme an beiden Stellen gleichzeitig "
                "anstehen.",
                d.fill(TECH_DE, 1),
            ],
            [
                "Vor der Vergabe ist eine {dsfa:hyphenated} vorzulegen; für das eingesetzte "
                "Fremdpersonal ist zudem eine {sue:hyphenated} erforderlich.",
                d.fill(LETTER_DE, 1),
            ],
            [
                "Die zwischen den Beteiligten geschlossene {nda_de:hyphenated} gilt auch für "
                "diese Stellungnahme und ihre Anlagen.",
                d.fill(LETTER_DE, 1),
            ],
        ],
        signer=["Dipl.-Ing. Svenja Hartig", "Partnerin"],
        footer=[
            ["Ingenieurbüro Hartig & Partner", "Am Güterbahnhof 3 · 28195 Bremen"],
            ["Partnerschaftsregister Bremen PR 118"],
            ["Die Sparkasse Bremen", "IBAN {iban:plain}"],
        ],
    )


@doc("t007_letter_en", family="arial", lang="en")
def t007(d: Doc) -> None:
    letter(
        d,
        sender="Northgate Advisory LLP",
        tagline="Corporate Finance · Transaction Services",
        return_line="Northgate Advisory LLP · 14 Finsbury Circus · London EC2M 7EB",
        recipient=[
            "{vireon@1:plain} Ltd.",
            "Attn. Ms {schoellhorn:plain}",
            "Cambridge Science Park",
            "",
            "Cambridge CB4 0WG",
            "United Kingdom",
        ],
        refs=[("Our ref.", "NA/HAL/031"), ("Date", "9 March 2026")],
        subject="{halcyon:plain} – next steps",
        greeting="Dear Ms Schöllhorn,",
        closing="Yours sincerely",
        body=[
            [
                d.fill(EN, 1),
                "As discussed, the {nda_en:linebreak} between your company and {quadrant:case} "
                "has been countersigned and is enclosed.",
            ],
            [
                "The technical due diligence for {halcyon:plain} will focus on the "
                "{sentinel:plain} product line and its supply chain.",
                d.fill(EN, 1),
            ],
            d.fill(EN, 2),
        ],
        signer=["Oliver Pennington", "Partner"],
        footer=[
            ["Northgate Advisory LLP", "Registered in England OC391224"],
            ["14 Finsbury Circus", "London EC2M 7EB"],
            ["+44 20 7946 0321", "www.northgate-advisory.co.uk"],
        ],
    )


@doc("t008_fax_brandtner", fmt="tif", family="arial", pt=12, degrade=("fax",))
def t008(d: Doc) -> None:
    letter(
        d,
        sender="{brandtner@1:plain}",
        tagline="Feinmechanik · Sensorik · Prüftechnik",
        return_line="Gewerbering 7 · 73760 Ostfildern",
        recipient=[
            "Helvetor Maschinenbau AG",
            "Einkauf",
            "Frau {maass:plain}",
            "per Fax 07541 204-399",
        ],
        refs=[("Kundennummer", "{kd:plain}"), ("Datum", "12. März 2026")],
        subject="Lieferverzögerung {fs220:plain}",
        greeting="Sehr geehrte Frau Maaß,",
        body=[
            [
                "leider verzögert sich die Lieferung der Präsenzsensoren für das {nord:plain} um "
                "etwa zwei Wochen, da ein Zulieferer ausgefallen ist.",
                d.fill(LETTER_DE, 1),
            ],
            d.fill(LETTER_DE, 1),
        ],
        signer=["Viktor Kaminski", "Vertrieb Industriekunden"],
    )
    fax_tti(d.sheets[0], "12.03.2026 09:41   +49 711 348820   BRANDTNER VERTRIEB   S. 01/01")


@doc(
    "t009_brief_schreibmaschine",
    dpi=150,
    family="courier",
    head="courier",
    pt=11,
    degrade=("faint",),
)
def t009(d: Doc) -> None:
    letter(
        d,
        sender="Stadtwerke Lindenau",
        tagline="Liegenschaftsverwaltung",
        return_line="Stadtwerke Lindenau · Postfach 1120 · 04416 Lindenau",
        recipient=[
            "Helvetor Maschinenbau AG",
            "Werkschutz",
            "Seestraße 40",
            "",
            "88045 Friedrichshafen",
        ],
        refs=[("Az.", "LV-7/26"), ("Datum", "28. April 2026")],
        subject="Übergabe der Räume im Objekt {hafen:umlaut}",
        body=[
            "die Räume im Erdgeschoss werden ab dem 1. Mai durch Ihre Arbeitsgruppe "
            "{kranich:plain} genutzt. Herr {weiss:case=JÜRGEN WEISSMÜLLER} hat die Schlüssel am "
            "28. April quittiert.",
            d.fill(LETTER_DE, 2),
        ],
        signer=["Rainer Pohl", "Liegenschaften"],
    )


@doc("t010_brief_albis", fmt="jpg", family="dejavu")
def t010(d: Doc) -> None:
    letter(
        d,
        sender="Albis Facility Services",
        tagline="Technisches Gebäudemanagement",
        return_line="Albis Facility Services GmbH · Kaistraße 9 · 40221 Düsseldorf",
        recipient=[
            "Helvetor Maschinenbau AG",
            "Einkauf",
            "Frau {maass:plain}",
            "Seestraße 40",
            "",
            "88045 Friedrichshafen",
        ],
        refs=[("Angebot", "AF-26-0192"), ("Datum", "6. März 2026")],
        subject="Rahmenvereinbarung Wartung {bmz:plain} und {fruehwarn:plain}",
        greeting="Sehr geehrte Frau Maaß,",
        body=[
            [
                d.fill(LETTER_DE, 1),
                "Unser Angebot umfasst die vierteljährliche Inspektion der {bmz:plain} sowie die "
                "jährliche Prüfung aller Melder.",
            ],
            [
                "Für das {fruehwarn:plain} bieten wir zusätzlich eine Rufbereitschaft an "
                "Wochenenden an.",
                d.fill(LETTER_DE, 1),
            ],
            d.fill(LETTER_DE, 1),
        ],
        signer=["Marco Albrecht", "Key Account Management"],
        footer=[
            ["Albis Facility Services GmbH", "Kaistraße 9 · 40221 Düsseldorf"],
            ["Amtsgericht Düsseldorf HRB 88120"],
            ["Stadtsparkasse Düsseldorf", "IBAN {iban:plain}"],
        ],
    )


# ---- invoices and delivery notes


@doc("t011_rechnung_brandtner", family="arial")
def t011(d: Doc) -> None:
    rows = [
        ["Pos.", "Art.-Nr.", "Bezeichnung", "Menge", "Einzelpreis", "Gesamt"],
        ["1", "{fs220:plain}", "Präsenzsensor, Deckenmontage", "48", "86,40", "4.147,20"],
        ["2", "{fsre:plain}", "Sensorkopf mit Weitwinkellinse", "12", "41,90", "502,80"],
        ["3", "{sentinel:plain}", "Auswerteeinheit, 8 Eingänge", "2", "1.240,00", "2.480,00"],
        ["4", "MK-12", "Montagekit Zwischendecke", "1", "380,00", "380,00"],
    ]
    story = letter(
        d,
        sender="{brandtner:plain}",
        tagline="Feinmechanik · Sensorik · Prüftechnik",
        return_line="{brandtner:plain} · Gewerbering 7 · 73760 Ostfildern",
        recipient=[
            "Helvetor Maschinenbau AG",
            "Kreditorenbuchhaltung",
            "Seestraße 40",
            "",
            "88045 Friedrichshafen",
        ],
        refs=[
            ("Rechnungsnr.", "2026-0415"),
            ("Kundennummer", "{kd:plain}"),
            ("Lieferschein", "LS-88121"),
            ("Datum", "20. März 2026"),
        ],
        subject="Rechnung Nr. 2026-0415",
        greeting=None,
        closing=None,
        body=["Für die im März ausgeführten Lieferungen berechnen wir Ihnen:"],
        after=lambda st: invoice_table(
            st,
            rows,
            [
                ("Nettobetrag", "7.510,00 EUR"),
                ("USt. 19 %", "1.426,90 EUR"),
                ("Rechnungsbetrag", "8.936,90 EUR"),
            ],
            notes=["Zahlbar innerhalb von 30 Tagen ohne Abzug auf unser Konto {iban:plain}."],
        ),
        footer=BRANDTNER_FOOTER,
    )
    stamp(story.s, 150, story.y + 22, ["BEZAHLT", "02.04.2026"], color=STAMP_BLUE, angle=-11, pt=13)


@doc("t012_rechnung_kessler", fmt="pdf", dpi=150, family="helv")
def t012(d: Doc) -> None:
    services = (
        ("Lagergeld je Palettenplatz und Woche", "L-100"),
        ("Kommissionierung je Position", "L-210"),
        ("Warenausgang je Sendung", "L-220"),
        ("Verpackung Sonderformat", "L-305"),
        ("Zollabwicklung je Vorgang", "Z-010"),
        ("Etikettierung je Karton", "L-240"),
        ("Transport Bremen–Friedrichshafen", "T-400"),
        ("Retourenbearbeitung", "L-260"),
        ("Inventur, Stundensatz", "L-900"),
    )
    special = {
        4: ("Einlagerung Sensoren", "{fs220:glued}"),
        11: ("Kommissionierung Sensoren", "{fs220:punct}"),
        17: ("Rücknahme Fehllieferung", "[[FS-2200|fs220|longer number]]"),
        23: (
            "Umpacken Musterteile",
            "[[FS-221|fs220|one digit off (the regex term still counts)]]",
        ),
    }
    rows: list[list[Content]] = [["Pos.", "Leistung", "Art.-Nr.", "Menge", "Preis", "Summe"]]
    total = 0.0
    for i in range(1, 31):
        label, code = special.get(i, services[(i * 7) % len(services)])
        qty = d.rng.randint(1, 60)
        price = d.rng.choice((4.2, 6.9, 12.5, 18.0, 36.0, 64.0))
        total += qty * price
        amount = f"{qty * price:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
        rows.append([str(i), label, code, str(qty), f"{price:.2f}".replace(".", ","), amount])
    net = f"{total:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    letter(
        d,
        sender="{kessler:plain}",
        tagline="Kontraktlogistik · Lagerhaltung · Zollabwicklung",
        return_line="{kessler:plain} GmbH · Postfach 10 44 21 · 28044 Bremen",
        recipient=[
            "Helvetor Maschinenbau AG",
            "Kreditorenbuchhaltung",
            "Seestraße 40",
            "",
            "88045 Friedrichshafen",
        ],
        refs=[
            ("Rechnungsnr.", "2026-1187"),
            ("Kundennummer", "{kd:plain}"),
            ("Zeitraum", "Februar 2026"),
            ("Datum", "3. März 2026"),
        ],
        subject="Rechnung Nr. 2026-1187 – Logistikleistungen Februar",
        greeting=None,
        closing=None,
        body=["Für den Abrechnungszeitraum Februar 2026 stellen wir Ihnen in Rechnung:"],
        after=lambda st: invoice_table(
            st,
            rows,
            [
                ("Nettobetrag", f"{net} EUR"),
                ("USt. 19 %", "gesondert"),
                ("Gesamtbetrag", "s. Anlage"),
            ],
            widths=(12, 64, 26, 17, 20, 26),
            notes=["Zahlbar innerhalb von 14 Tagen. Bitte geben Sie die Rechnungsnummer an."],
        ),
        header2="Rechnung 2026-1187 · Kundennummer {kd:plain}",
        footer=[
            ["{kessler:plain} GmbH", "Postfach 10 44 21 · 28044 Bremen"],
            ["Sparkasse Bremen", "IBAN {iban:plain}"],
            ["Amtsgericht Bremen HRB 30412"],
        ],
    )


@doc("t013_rechnung_helvetor", fmt="jpg", dpi=150, family="dejavu", degrade=("jpeg",))
def t013(d: Doc) -> None:
    rows = [
        ["Pos.", "Art.-Nr.", "Bezeichnung", "Menge", "Einzelpreis", "Gesamt"],
        ["1", "{fsre:plain}", "Sensorkopf, Ersatzteil", "6", "41,90", "251,40"],
        ["2", "HV-3310", "Hydraulikventil 3/2-Wege", "2", "318,00", "636,00"],
        ["3", "DK-0415", "Dichtungssatz Presse 4", "4", "27,50", "110,00"],
    ]
    letter(
        d,
        sender="Helvetor Maschinenbau AG",
        tagline="Seestraße 40 · 88045 Friedrichshafen · {mail:plain=einkauf@helvetor.de}",
        return_line="Helvetor Maschinenbau AG · Seestraße 40 · 88045 Friedrichshafen",
        recipient=["{vireon:plain} GmbH", "Standortservice", "Rheinauhafen 3", "", "50678 Köln"],
        refs=[
            ("Rechnungsnr.", "HM-26-00871"),
            ("Kundennummer", "{kd:plain}"),
            ("Datum", "24. Februar 2026"),
        ],
        subject="Rechnung Ersatzteillieferung",
        greeting=None,
        closing=None,
        body=[
            "Ihre alte Kundennummer [[KD-12345|kd|five digits]] ist seit Januar nicht mehr "
            "gültig; bitte verwenden Sie bei Zahlungen nur noch die oben genannte Nummer.",
        ],
        after=lambda st: invoice_table(
            st,
            rows,
            [
                ("Nettobetrag", "997,40 EUR"),
                ("USt. 19 %", "189,51 EUR"),
                ("Rechnungsbetrag", "1.186,91 EUR"),
            ],
            widths=(11, 25, 58, 17, 28, 26),
            notes=[
                "Bitte überweisen Sie den Betrag bis zum 24. März 2026 auf das Konto {iban:plain} "
                "bei der Sparkasse Bodensee.",
            ],
        ),
        footer=[
            ["Helvetor Maschinenbau AG", "Seestraße 40 · 88045 Friedrichshafen"],
            ["Vorstand: {assmann:plain}", "Amtsgericht Ulm HRB 631877"],
            ["Sparkasse Bodensee", "BIC SOLADES1KNZ"],
        ],
    )


@doc("t014_lieferschein", dpi=300, family="courier", head="courier", pt=10)
def t014(d: Doc) -> None:
    rows = [
        ["Pos.", "Art.-Nr.", "Bezeichnung", "Menge", "Einheit"],
        ["1", "{fsre:plain=FS-145}", "Sensorkopf Standard", "24", "Stk"],
        ["2", "{fs220:plain}", "Präsenzsensor", "48", "Stk"],
        ["3", "{sentinel@1:plain}", "Auswerteeinheit", "2", "Stk"],
        ["4", "[[Sentinel X40|sentinel|longer model number]]", "Netzteil 24 V", "2", "Stk"],
        ["5", "MK-12", "Montagekit Zwischendecke", "1", "Satz"],
    ]

    def after(story: Story) -> None:
        story.table([14, 44, 67, 20, 20], rows, d.st(9.5))
        story.para("Ware vollständig und unbeschädigt erhalten:", before=10, after=14)
        s = story.s
        s.hline(25, 95, story.y, color=INK)
        s.hline(115, 185, story.y, color=INK)
        text(s, 25, story.y + 1, 70, d.units("Datum", d.st(8, color=GREY)))
        text(s, 115, story.y + 1, 70, d.units("Unterschrift Empfänger", d.st(8, color=GREY)))

    letter(
        d,
        sender="{brandtner:case=BRANDTNER & SÖHNE}",
        sender_style=d.hs(16, color=INK),
        tagline="Gewerbering 7 · 73760 Ostfildern",
        return_line="Gewerbering 7 · 73760 Ostfildern",
        recipient=[
            "{kessler:plain} GmbH",
            "Wareneingang Tor 1",
            "{hafen:plain}",
            "",
            "28217 Bremen",
        ],
        refs=[("Lieferschein", "LS-88121"), ("Auftrag", "A-26-3391"), ("Datum", "18. März 2026")],
        subject="LIEFERSCHEIN LS-88121",
        greeting=None,
        closing=None,
        body=[
            "Lieferung im Auftrag der Helvetor Maschinenbau AG für das Lager Bremen. Die Ware "
            "bleibt bis zur vollständigen Bezahlung unser Eigentum.",
        ],
        after=after,
    )


@doc("t015_rechnung_linz", family="arial")
def t015(d: Doc) -> None:
    rows = [
        ["Pos.", "Art.-Nr.", "Bezeichnung", "Menge", "Einzelpreis", "Gesamt"],
        ["1", "AH-4410", "Hydraulikpumpe 40 bar", "1", "2.180,00", "2.180,00"],
        ["2", "AH-0072", "Schlauchleitung DN 12, 2 m", "8", "46,20", "369,60"],
        ["3", "AH-0915", "Druckschalter", "3", "112,00", "336,00"],
    ]
    letter(
        d,
        sender="Alpen Hydraulik GmbH",
        tagline="Industriezeile 36 · 4020 Linz · Österreich",
        return_line="Alpen Hydraulik GmbH · Industriezeile 36 · 4020 Linz",
        recipient=[
            "{vireon:plain} GmbH",
            "Technischer Einkauf",
            "Rheinauhafen 3",
            "",
            "50678 Köln",
            "Deutschland",
        ],
        refs=[
            ("Rechnung", "AH-2026-0310"),
            ("Kunden-Nr.", "[[KD-1234567|kd|seven digits]]"),
            ("Datum", "10. März 2026"),
        ],
        subject="Rechnung AH-2026-0310",
        greeting=None,
        closing=None,
        body=["Wir erlauben uns, für die Lieferung vom 6. März 2026 in Rechnung zu stellen:"],
        after=lambda st: invoice_table(
            st,
            rows,
            [("Nettobetrag", "2.885,60 EUR"), ("Rechnungsbetrag", "2.885,60 EUR")],
            notes=[
                "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge).",
                "Bitte überweisen Sie den Betrag ohne Abzug auf unser Konto bei der "
                "Raiffeisenlandesbank Oberösterreich, IBAN [[AT61 1904 3002 3457 "
                "3201|iban|Austrian IBAN]], BIC RZOOAT2L.",
            ],
        ),
        footer=[
            ["Alpen Hydraulik GmbH", "FN 214511 k · LG Linz"],
            ["UID ATU 57311204"],
            ["Raiffeisenlandesbank OÖ", "BIC RZOOAT2L"],
        ],
    )


@doc("t016_gutschrift", fmt="tif", family="helv", degrade=("blur",))
def t016(d: Doc) -> None:
    def after(story: Story) -> None:
        story.table(
            [34, 52],
            [
                ["Kontoinhaber", "{brandtner@1:plain}"],
                ["IBAN", "{iban:linebreak}"],
                ["Betrag", "1.284,60 EUR"],
            ],
            d.st(9.5),
            head=False,
            head_fill=None,
        )

    letter(
        d,
        sender="{kessler:spaced}",
        tagline="Kontraktlogistik · Lagerhaltung · Zollabwicklung",
        return_line="{kessler:plain} GmbH · Postfach 10 44 21 · 28044 Bremen",
        recipient=[
            "{brandtner:umlaut} GmbH & Co. KG",
            "Buchhaltung",
            "Gewerbering 7",
            "",
            "73760 Ostfildern",
        ],
        refs=[
            ("Gutschrift", "GS-26-0044"),
            ("Ihre Kundennummer", "{kd:plain}"),
            ("Datum", "30. März 2026"),
        ],
        subject="Gutschrift zur Rechnung 2026-1187",
        body=[
            "für die doppelt berechnete Einlagerung im Februar schreiben wir Ihnen 1.284,60 EUR "
            "gut. Der Betrag wird innerhalb von fünf Werktagen auf folgendes Konto überwiesen:",
        ],
        after=after,
        signer=["Buchhaltung Bremen"],
    )


CONTRACT_EN = (
    "Each party shall keep the Confidential Information of the other party strictly confidential.",
    "The receiving party shall use the Confidential Information solely for the purpose of "
    "evaluating the cooperation.",
    "The obligations under this agreement shall survive its termination for a period of five "
    "years.",
    "Upon request, all documents containing Confidential Information shall be returned or "
    "destroyed.",
    "This agreement shall be governed by the laws of the Federal Republic of Germany.",
    "No licence under any patent, copyright or other intellectual property right is granted by "
    "this agreement.",
    "Any amendment to this agreement must be made in writing and signed by both parties.",
    "Neither party may assign this agreement without the prior written consent of the other party.",
    "The place of jurisdiction shall be Munich.",
    "Confidential Information does not include information that is or becomes publicly available "
    "through no fault of the receiving party.",
)
REPORT_DE = (
    "Die Umsetzung verläuft insgesamt planmäßig; Verzögerungen einzelner Gewerke konnten "
    "aufgefangen werden.",
    "Die Abstimmung mit dem Betriebsrat zur Auswertung von Protokolldaten ist abgeschlossen.",
    "Für den Übergang in den Regelbetrieb ist eine Schulung aller Pfortenkräfte vorgesehen.",
    "Die Kostenprognose liegt unverändert bei 2,4 Millionen Euro.",
    "Zwei Nachträge des Generalunternehmers wurden geprüft und in reduzierter Höhe anerkannt.",
    "Die Abnahme der Kameratechnik erfolgt gemeinsam mit dem Sachverständigen.",
    "Im Berichtszeitraum fanden drei Sitzungen des Lenkungskreises statt.",
    "Die Dokumentation wird im Projektlaufwerk revisionssicher abgelegt.",
    "Offene Punkte aus der Begehung wurden in die Mängelliste übernommen.",
    "Die Terminschiene für das zweite Halbjahr wurde mit allen Beteiligten abgestimmt.",
    "Das Risiko eines Lieferengpasses bei Kartenlesern wird weiterhin beobachtet.",
    "Die Ausweiskarten der Beschäftigten werden schrittweise gegen neue Karten getauscht.",
    "Die Schnittstelle zum Personalsystem wurde im Testbetrieb erfolgreich geprüft.",
    "Für die Außenstandorte wird ein vereinfachtes Verfahren entwickelt.",
)


def ticks(s: Sheet, x: float, y: float, items: list[tuple[bool, Content]], style: Style) -> float:
    for checked, label in items:
        s.tick(x, y + 0.2, checked)
        y = text(s, x + 6.5, y, 150, s.doc.units(label, style)) + 2.2
    return y


def form_head(s: Sheet, title: Content, sub: Content, color: tuple[int, int, int]) -> float:
    d = s.doc
    s.rect(20, 22, 190, 38, fill=color)
    text(s, 24, 25, 162, d.units(title, d.hs(19, color=WHITE)))
    return text(s, 20, 40, 170, d.units(sub, d.st(8, color=GREY))) + 4


def sign_lines(
    s: Sheet, y: float, labels: tuple[str, str] = ("Ort, Datum", "Unterschrift")
) -> None:
    d = s.doc
    s.hline(20, 90, y, color=INK, width=0.2)
    s.hline(110, 190, y, color=INK, width=0.2)
    text(s, 20, y + 1, 70, d.units(labels[0], d.st(8, color=GREY)))
    text(s, 110, y + 1, 80, d.units(labels[1], d.st(8, color=GREY)))


# ---- minutes


@doc("t017_protokoll_nordlicht", fmt="pdf", family="arial")
def t017(d: Doc) -> None:
    w, k, m = d.occ("weiss", "cells"), d.occ("kuehnast", "cells"), d.occ("schoellhorn", "cells")

    def participants(story: Story) -> None:
        story.table(
            [38, 42, 85],
            [
                ["Vorname", "Name", "Funktion"],
                [Part(w, 0), Part(w, 1), "Leitung Werkschutz (Vorsitz)"],
                [Part(k, 0), Part(k, 1), "IT-Sicherheit"],
                [Part(m, 0), Part(m, 1), "{vireon:plain} GmbH, Standortleitung Köln"],
                ["Dr. Tobias", "Renner", "{quadrant:plain} GmbH, Service Süd"],
                ["Svenja", "Hartig", "Ingenieurbüro Hartig & Partner (Protokoll)"],
            ],
            d.st(9.5),
        )

    def actions(story: Story) -> None:
        story.heading("Aufgaben", d.hs(11))
        story.table(
            [12, 88, 38, 27],
            [
                ["Nr.", "Aufgabe", "Verantwortlich", "Termin"],
                [
                    "1",
                    "Terminplan für die Abnahme der {bmz:plain} abstimmen",
                    "Dr. T. Renner",
                    "20.03.2026",
                ],
                [
                    "2",
                    "Stellungnahme des Betriebsrats zu den Protokolldaten einholen",
                    "S. Hartig",
                    "27.03.2026",
                ],
                [
                    "3",
                    "Kostenfreigabe für den zweiten Bauabschnitt vorbereiten",
                    "{weiss:plain}",
                    "03.04.2026",
                ],
                [
                    "4",
                    "Schulungskonzept für die Pfortenkräfte vorlegen",
                    "Werkschutz",
                    "17.04.2026",
                ],
            ],
            d.st(9.5),
        )

    minutes(
        d,
        org="Helvetor Maschinenbau AG · Werkschutz",
        title="Protokoll der 7. Sitzung des Lenkungskreises",
        meta=[
            ("Datum", "5. März 2026, 10:00–12:30 Uhr"),
            ("Ort", "Friedrichshafen, Raum B 2.14"),
            ("Leitung", "{weiss:plain}"),
            ("Protokoll", "Svenja Hartig"),
        ],
        header="Lenkungskreis {nord:plain} – Protokoll Nr. 7",
        footer="{weitergabe:plain}",
        tops=[
            ("Teilnehmende und Tagesordnung", [d.fill(MINUTES_DE, 2)]),
            (
                "Stand der {zutritt:plain}",
                [
                    "Herr Dr. Renner berichtet über den Stand der Montage am Standort "
                    "{hafen:plain}. Die Kartenleser im Erdgeschoss sind installiert, die "
                    "Anbindung an die Leitstelle folgt in KW 12.",
                    d.fill(MINUTES_DE, 2),
                ],
            ),
            (
                "{bmz:plain}",
                [
                    "Die Abnahme durch den Sachverständigen ist für den 24. März vorgesehen. Bis "
                    "dahin müssen die Melder im Lager nachgerüstet werden.",
                    d.fill(REPORT_DE, 2),
                ],
            ),
            (
                "Kosten",
                [
                    "Frau [[Ulrike Maas|maass|one letter off (the term allows no edits)]] aus dem "
                    "Controlling hat die Kostenaufstellung geprüft; die Nachträge sind plausibel.",
                    d.fill(REPORT_DE, 2),
                ],
            ),
            ("Verschiedenes", [d.fill(MINUTES_DE, 2), d.fill(REPORT_DE, 2)]),
        ],
        extra={1: participants, 0: actions},
    )


@doc("t018_protokoll_betriebsrat", family="times", head="times", notes="no term")
def t018(d: Doc) -> None:
    minutes(
        d,
        org="Betriebsrat der Helvetor Maschinenbau AG",
        title="Niederschrift über die Sitzung des Betriebsrats",
        meta=[
            ("Datum", "11. Februar 2026, 14:00 Uhr"),
            ("Ort", "[[Werk Falkenstein|falke|inside a longer word]], Besprechungsraum 3"),
            ("Vorsitz", "Dieter [[Falke|falke|a surname with no near word]]"),
            ("Anwesend", "Petra Vogt, Miriam Okafor, Lukas Behrendt, Holger Wendt"),
        ],
        tops=[
            (
                "Eröffnung",
                [
                    "Herr [[Falke|falke|a surname with no near word]] eröffnet die Sitzung und "
                    "stellt die Beschlussfähigkeit fest.",
                    d.fill(MINUTES_DE, 1),
                ],
            ),
            ("Arbeitszeitregelung", [d.fill(MINUTES_DE, 2)]),
            ("Betriebsvereinbarung Kantine", [d.fill(MINUTES_DE, 2)]),
            ("Verschiedenes", [d.fill(MINUTES_DE, 2)]),
        ],
    )


@doc("t019_protokoll_it", dpi=150, family="dejavu", degrade=("skew",))
def t019(d: Doc) -> None:
    minutes(
        d,
        org="IT-Betrieb",
        title="Jour fixe IT-Betrieb – KW 11",
        meta=[
            ("Datum", "12. März 2026"),
            ("Teilnehmende", "{kuehnast:plain}, Lukas Behrendt, Miriam Okafor"),
            ("Protokoll", "Lukas Behrendt"),
        ],
        tops=[
            (
                "Datenbanken",
                [
                    "Die Migration von {orka:plain} auf die neue Datenbankversion ist "
                    "abgeschlossen.",
                    d.fill(TECH_DE, 1),
                ],
            ),
            (
                "Messdaten",
                [
                    "Für {vega:plain} wird ein zusätzlicher Knoten beschafft; Herr Kühnast klärt "
                    "die Lizenzfrage.",
                ],
            ),
            (
                "Alarmierung",
                [
                    "Das {fruehwarn:plain} liefert seine Meldungen künftig direkt an den Server "
                    "{nox:plain}.",
                    d.fill(TECH_DE, 1),
                ],
            ),
            (
                "Gebäude",
                [
                    "Die Sturmschäden durch den [[Orkan|orka|inside a longer word]] im Februar "
                    "sind behoben; das Dach über dem Rechenzentrum wurde abgedichtet.",
                ],
            ),
        ],
    )


@doc("t020_protokoll_baustelle", fmt="tif", family="freeserif", head="freeserif")
def t020(d: Doc) -> None:
    def status(story: Story) -> None:
        story.table(
            [36, 28, 45, 56],
            [
                ["Gewerk", "Stand", "Nächster Schritt", "Bemerkung"],
                ["{zutritt:linebreak}", "80 %", "Anbindung Leitstelle", "Kartenleser EG montiert"],
                [
                    "{bmz:plain}",
                    "Abnahme offen",
                    "Sachverständiger 24.03.",
                    "Melder im Lager fehlen",
                ],
                ["Elektro", "95 %", "Restarbeiten", "–"],
                ["Trockenbau", "fertig", "–", "Abnahme erfolgt"],
            ],
            d.st(9.5),
        )

    kk = d.occ("kuehnast", "cells")

    def present(story: Story) -> None:
        story.table(
            [40, 45, 80],
            [
                ["Vorname", "Name", "Firma"],
                ["Katrin", "Oltmanns", "{kessler:plain} GmbH (Bauherr)"],
                [Part(kk, 0), Part(kk, 1), "Helvetor Maschinenbau AG (Mieter)"],
                ["Svenja", "Hartig", "Ingenieurbüro Hartig & Partner"],
                ["Marco", "Albrecht", "Albis Facility Services"],
            ],
            d.st(9.5),
        )

    minutes(
        d,
        org="Ingenieurbüro Hartig & Partner",
        title="Protokoll Baubesprechung Nr. 12",
        meta=[
            ("Bauvorhaben", "Umbau Lager {hafen:plain}, 28217 Bremen"),
            ("Bauherr", "{kessler:plain} GmbH"),
            ("Datum", "10. März 2026, 9:00 Uhr, Baubüro"),
        ],
        header="Baubesprechung Nr. 12 · Umbau Lager Bremen",
        tops=[
            ("Terminplan", [d.fill(MINUTES_DE, 2), d.fill(REPORT_DE, 1)]),
            (
                "Baustelleneinrichtung",
                [
                    "Die Baustellenzufahrt erfolgt ab sofort über das Nachbargrundstück "
                    "[[Hafenstraße 120|hafen|longer house number]]; die Schranke wird vom "
                    "Werkschutz bedient.",
                    d.fill(REPORT_DE, 2),
                ],
            ),
            ("Stand der Gewerke", [d.fill(REPORT_DE, 2)]),
            ("Nachträge", [d.fill(REPORT_DE, 3), d.fill(MINUTES_DE, 2)]),
            (
                "Brandschutz",
                [
                    "Die Firma Albis meldet, dass die {bmz:lookalike} im Lager erst nach "
                    "Abschluss der Trockenbauarbeiten angeschlossen werden kann.",
                    d.fill(TECH_DE, 3),
                ],
            ),
            ("Arbeitsschutz", [d.fill(TECH_DE, 3), d.fill(MINUTES_DE, 2)]),
            ("Nächste Besprechung", [d.fill(MINUTES_DE, 2), d.fill(REPORT_DE, 2)]),
        ],
        extra={1: present, 3: status},
    )


@doc("t021_protokoll_umwelt", family="arial", notes="no term")
def t021(d: Doc) -> None:
    minutes(
        d,
        org="Helvetor Maschinenbau AG · Umweltausschuss",
        title="Protokoll der Sitzung des Umweltausschusses",
        meta=[
            ("Datum", "19. Februar 2026"),
            ("Ort", "Werk Friedrichshafen, Gebäude A"),
            ("Leitung", "Dr. Albrecht Hoyer"),
        ],
        tops=[
            (
                "Heizzentrale",
                [
                    "Die [[NOx|nox|next to its not_near word]]-Emissionen der Heizzentrale lagen "
                    "im Berichtsjahr deutlich unter dem Grenzwert.",
                    d.fill(MINUTES_DE, 1),
                ],
            ),
            (
                "Luftmessungen",
                [
                    "An der Messstation Nord werden Stickoxide ([[NOx|nox|next to its not_near "
                    "word]]) und Feinstaub kontinuierlich erfasst.",
                    d.fill(MINUTES_DE, 1),
                ],
            ),
            (
                "Außenanlagen",
                [
                    "Die Vogelwarte meldet einen [[Kranich|kranich|next to its not_near word]] am "
                    "Rückhaltebecken; die Mäharbeiten werden bis Ende April verschoben.",
                ],
            ),
            ("Verschiedenes", [d.fill(MINUTES_DE, 2)]),
        ],
    )


@doc("t022_minutes_en", family="helv", lang="en")
def t022(d: Doc) -> None:
    minutes(
        d,
        org="Northgate Advisory LLP",
        title="Minutes – Steering Committee {halcyon:plain}",
        meta=[
            ("Date", "2 March 2026"),
            ("Venue", "London, 14 Finsbury Circus"),
            ("Chair", "Oliver Pennington"),
            (
                "Attendees",
                "{schoellhorn:plain}, two representatives of {quadrant:plain}, Dr. Carla Menéndez",
            ),
        ],
        label="Item",
        page_word="Page",
        tops=[
            (
                "Confidentiality",
                [
                    "The {nda_en:case=non-disclosure agreement} was signed by all parties on 27 "
                    "February.",
                    d.fill(EN, 1),
                ],
            ),
            (
                "Product review",
                ["Samples of the {sentinel:glued} evaluation unit were presented.", d.fill(EN, 1)],
            ),
            ("Timeline", [d.fill(EN, 2)]),
            ("Any other business", [d.fill(EN, 1)]),
        ],
    )


@doc("t023_protokoll_vorstand", fmt="jpg", family="times", head="times", degrade=("jpeg",))
def t023(d: Doc) -> None:
    minutes(
        d,
        org="Helvetor Maschinenbau AG",
        title="Auszug aus dem Protokoll der Vorstandssitzung vom 23. Februar 2026",
        meta=[
            ("Anwesend", "{assmann:plain} (Vorsitz), Dr. Albrecht Hoyer, Klaus Dornbach"),
            ("Gäste", "Oliver Pennington (Northgate Advisory)"),
        ],
        tops=[
            (
                "{fuchs:plain}",
                [
                    "Der Vorstand nimmt den Bericht zum Stand der {fuchs:hyphenated} zur Kenntnis.",
                    d.fill(MINUTES_DE, 2),
                ],
            ),
            (
                "Beteiligungen",
                [
                    "Die Prüfung der {kessler:plain} GmbH ist abgeschlossen; die "
                    "{nda_de:inner_space} mit dem Verkäufer wurde bis zum Jahresende verlängert.",
                    d.fill(MINUTES_DE, 1),
                ],
            ),
            (
                "Beschlüsse",
                [
                    "Der Vorstand beschließt einstimmig, die Gespräche fortzuführen und den "
                    "Aufsichtsrat in seiner nächsten Sitzung zu unterrichten.",
                ],
            ),
        ],
    )


@doc("t024_telefonnotiz_fax", family="arial", pt=12, degrade=("fax",))
def t024(d: Doc) -> None:
    memo(
        d,
        kind="Telefonnotiz",
        fields=[
            ("Datum", "11. März 2026, 15:40 Uhr"),
            ("Anrufer", "{weiss:plain}"),
            ("Betreff", "{fuchs:fuzzy}"),
        ],
        body=[
            "Herr Weißmüller bittet um Rückruf wegen des Termins mit der Rechtsabteilung.",
            "Die Unterlagen für Codename {falke:plain} sollen bis Freitag vorliegen; er bittet "
            "darum, sie nur persönlich zu übergeben.",
            d.fill(MINUTES_DE, 1),
        ],
        sign=["gez. M. Okafor"],
    )
    fax_tti(d.sheets[0], "11.03.2026 16:02  +49 7541 204-300  SEKRETARIAT VORSTAND  S. 1")


# ---- e-mails


@doc("t025_mail_weissmueller", family="arial", clean=True)
def t025(d: Doc) -> None:
    email(
        d,
        printed_by="{weiss@1:plain}",
        sender="{weiss@1:plain}",
        fields=[
            ("Von:", "{weiss@1:plain} <{mail:plain=j.weissmueller@helvetor.de}>"),
            ("Gesendet:", "Dienstag, 10. März 2026 08:14"),
            ("An:", "{kuehnast@1:plain}; {maass:plain}"),
            ("Cc:", "Projektbüro <{mail:plain=projektbuero@helvetor.de}>"),
            ("Betreff:", "AW: {nord:case=projekt nordlicht} – Begehung"),
        ],
        body=[
            "Hallo Sören, hallo Frau Maaß,",
            [
                "die Begehung der {zutritt:plain} am Standort {hafen:plain} verschiebt sich auf "
                "Donnerstag, 9 Uhr.",
                d.fill(MAIL_DE, 1),
            ],
            d.fill(MAIL_DE, 1),
            "Viele Grüße",
            "Jürgen",
        ],
        signature=[
            "{weiss:plain}",
            "Leiter Werkschutz",
            "Helvetor Maschinenbau AG · Seestraße 40 · 88045 Friedrichshafen",
            "Tel. +49 7541 204-311 · {mail:plain=j.weissmueller@helvetor.de}",
        ],
    )


@doc("t026_mail_kuehnast", dpi=150, family="dejavu", clean=True)
def t026(d: Doc) -> None:
    email(
        d,
        printed_by="{kuehnast@1:plain}",
        sender="{kuehnast:plain}",
        fields=[
            ("Von:", "{kuehnast:plain} <{mail:plain=s.kuehnast@helvetor.de}>"),
            ("Gesendet:", "Montag, 9. März 2026 18:47"),
            ("An:", "{weiss:plain}"),
            ("Betreff:", "AW: {fuchs:punct} – Datenschutz"),
        ],
        body=[
            "Hallo Jürgen,",
            [
                "für die {fuchs:glued} brauchen wir vor dem Start noch die {dsfa:umlaut}.",
                d.fill(MAIL_DE, 1),
            ],
            "Gruß",
            "Sören",
        ],
        quoted_fields=[
            ("Von:", "{weiss:plain}"),
            ("Gesendet:", "Montag, 9. März 2026 17:02"),
            ("Betreff:", "{fuchs:plain}"),
        ],
        quoted=[
            "Sören, kannst du bitte prüfen, ob wir für die Übernahme eine gesonderte "
            "Datenschutzprüfung brauchen?",
            "Jürgen",
        ],
    )


@doc("t027_mail_en", family="helv", lang="en", clean=True)
def t027(d: Doc) -> None:
    email(
        d,
        printed_by="Oliver Pennington",
        sender="Oliver Pennington",
        fields=[
            ("From:", "Oliver Pennington <o.pennington@northgate-advisory.co.uk>"),
            ("Sent:", "Wednesday, 11 March 2026 16:20"),
            ("To:", "{schoellhorn:plain}"),
            ("Subject:", "[{halcyon:glued}] data room access"),
        ],
        body=[
            "Dear Margarete,",
            "the {nda_en:punct} is now available in the data room, folder 01; the signed copy of "
            "the {nda_en:fuzzy=Non-Disclosure Agreemnt} follows by courier.",
            ["{quadrant:glued} has requested access for two more users.", d.fill(EN, 1)],
            "Best regards,",
            "Oliver",
        ],
        signature=["Oliver Pennington · Partner", "Northgate Advisory LLP · London"],
    )


@doc("t028_mail_verwechslung", family="arial", clean=True)
def t028(d: Doc) -> None:
    email(
        d,
        printed_by="Svenja Hartig",
        sender="Svenja Hartig",
        fields=[
            ("Von:", "Svenja Hartig <s.hartig@hartig-partner.de>"),
            ("Gesendet:", "Freitag, 13. März 2026 10:05"),
            ("An:", "{schoellhorn:lookalike}"),
            ("Betreff:", "{nord:lookalike} – Unterlagen"),
        ],
        body=[
            "Sehr geehrte Frau Schöllhorn,",
            [
                "anbei erhalten Sie die unterschriebene {nda_de:lookalike} sowie die Pläne für "
                "die {zutritt:lookalike}.",
                d.fill(MAIL_DE, 1),
            ],
            "Mit freundlichen Grüßen",
            "Svenja Hartig",
        ],
        signature=["Ingenieurbüro Hartig & Partner", "Am Güterbahnhof 3 · 28195 Bremen"],
    )


@doc("t029_mail_falke", fmt="jpg", family="arial", degrade=("jpeg",))
def t029(d: Doc) -> None:
    email(
        d,
        printed_by="Lukas Behrendt",
        sender="Lukas Behrendt",
        fields=[
            ("Von:", "Lukas Behrendt"),
            ("An:", "Vertrieb Innendienst"),
            ("Betreff:", "Codename {falke:case=FALKE} – Freigabe der Musterteile"),
        ],
        body=[
            "Hallo zusammen,",
            "die Muster für Codename {falke:plain} sind freigegeben und gehen am Montag raus.",
            d.fill(MAIL_DE, 2),
            "Schöne Grüße an Herrn [[Falke|falke|a surname with no near word]] aus dem Vertrieb, "
            "er hatte nach den Mustern gefragt.",
            "Die Auswertung läuft wie gehabt über {orka:case}.",
            "Lukas",
        ],
    )


@doc("t030_mail_verteiler", family="dejavu", clean=True)
def t030(d: Doc) -> None:
    email(
        d,
        printed_by="{assmann:plain}",
        sender="{assmann:plain}",
        fields=[
            ("Von:", "{assmann:plain}"),
            ("Gesendet:", "Montag, 16. März 2026 11:30"),
            (
                "An:",
                "{weiss:plain}; {schoellhorn@1:plain}; [[Jürgen Weissmüler|weiss|one letter off "
                "(the term allows no edits)]]; Holger Wendt",
            ),
            ("Betreff:", "Einladung: Workshop {sue:plain}"),
        ],
        body=[
            "Liebe Kolleginnen und Kollegen,",
            [
                "am Donnerstag, 26. März, stellen wir die neuen Regeln zur {sue:plain} vor.",
                d.fill(MAIL_DE, 1),
            ],
            "Beste Grüße",
            "{assmann:plain}",
        ],
        signature=[
            "Rechtsabteilung · Helvetor Maschinenbau AG",
            "{mail:plain=projektbuero@helvetor.de}",
        ],
    )


@doc("t031_mail_kurz", dpi=150, family="arial", degrade=("blur",))
def t031(d: Doc) -> None:
    email(
        d,
        printed_by="Miriam Okafor",
        sender="Miriam Okafor",
        fields=[
            ("Von:", "Miriam Okafor"),
            ("An:", "Facility Management"),
            ("Betreff:", "{kranich:plain}: Raumplanung 2. OG"),
        ],
        body=[
            "Hallo,",
            [
                "für die Arbeitsgruppe {kranich:plain} brauchen wir ab April vier zusätzliche "
                "Arbeitsplätze im zweiten Obergeschoss. Alle Mitarbeitenden haben die "
                "{sue:umlaut} abgeschlossen.",
                d.fill(MAIL_DE, 1),
            ],
            "Danke und Gruß",
            "Miriam",
        ],
    )


@doc("t032_mail_ascii", family="courier", head="courier", clean=True)
def t032(d: Doc) -> None:
    email(
        d,
        sender="Lagerverwaltung Bremen (automatisch)",
        fields=[
            ("From:", "{kessler:glued} Lager Bremen <lager-bremen@kl-bremen.de>"),
            ("To:", "werkschutz@hm-fn.de"),
            ("Subject:", "Wareneingang KW 11"),
        ],
        body=[
            "Automatische Benachrichtigung - bitte nicht antworten.",
            "Wareneingang am Standort {hafen:umlaut} fuer Herrn {weiss:umlaut}:",
            "- 2 Paletten Material fuer das Vorhaben {bruecke:umlaut}",
            "- Ersatzteile fuer das {fruehwarn:umlaut}",
            "Empfang quittiert durch {maass:umlaut} (Einkauf).",
            "Rueckfragen bitte an {kuehnast:umlaut}, IT-Sicherheit.",
            "Dieses Postfach wird nicht ueberwacht.",
        ],
    )


@doc("t033_mail_scope", family="loma", head="loma", lang="en", clean=True)
def t033(d: Doc) -> None:
    email(
        d,
        printed_by="{schoellhorn:plain}",
        sender="{schoellhorn:plain}",
        fields=[
            ("From:", "{schoellhorn:plain}"),
            ("To:", "Oliver Pennington"),
            ("Subject:", "RE: {halcyon:fuzzy} – product scope"),
        ],
        body=[
            "Oliver,",
            "{vireon@1:plain} would like to include the {sentinel:plain} evaluation unit in the "
            "scope. The purchase order will be issued by {vireon:glued} GmbH, our German "
            "subsidiary.",
            d.fill(EN, 2),
            "Kind regards,",
            "Margarete",
        ],
    )


@doc("t034_mail_weiterleitung", fmt="pdf", family="arial", clean=True)
def t034(d: Doc) -> None:
    email(
        d,
        printed_by="Klaus Dornbach",
        sender="Klaus Dornbach",
        fields=[
            ("Von:", "Klaus Dornbach"),
            ("Gesendet:", "Montag, 9. März 2026 07:58"),
            ("An:", "Svenja Hartig"),
            ("Betreff:", "WG: Rückfrage {fuchs:lookalike}"),
        ],
        body=[
            "Hallo Svenja,",
            "zur Info, siehe unten. Kannst du dich bitte um die Kundennummer kümmern?",
            "Klaus",
        ],
        quoted_fields=[
            ("Von:", "Viktor Kaminski"),
            ("Gesendet:", "Freitag, 6. März 2026 11:12"),
            ("Betreff:", "Rückfrage {fuchs:plain}"),
        ],
        quoted=[
            "Sehr geehrter Herr Dornbach,",
            [
                "wir haben Ihre Bestellung erhalten. Die Lieferung erfolgt wie immer durch "
                "{brandtner:linebreak} direkt an das Lager in Bremen.",
                d.fill(LETTER_DE, 2),
            ],
            d.fill(LETTER_DE, 3),
            d.fill(LETTER_DE, 3),
            d.fill(MAIL_DE, 3),
            d.fill(LETTER_DE, 3),
            [
                "Für die Zuordnung benötigen wir noch Ihre Kundennummer; laut unseren Unterlagen "
                "lautet sie {kd:plain}. Bitte bestätigen Sie uns die Nummer kurz.",
                d.fill(LETTER_DE, 2),
            ],
            d.fill(MINUTES_DE, 3),
            d.fill(MINUTES_DE, 3),
            d.fill(TECH_DE, 3),
            d.fill(TECH_DE, 3),
            d.fill(REPORT_DE, 3),
            "Mit freundlichen Grüßen",
            "Viktor Kaminski",
        ],
    )


# ---- contracts


@doc("t035_vertrag_geheimhaltung", fmt="pdf", family="times", head="times", align="justify")
def t035(d: Doc) -> None:
    contract(
        d,
        title="{nda_de:case=GEHEIMHALTUNGSVEREINBARUNG}",
        subtitle="(Vertraulichkeitsvereinbarung)",
        preamble=[
            "zwischen der",
            "{vireon:plain} GmbH, Rheinauhafen 3, 50678 Köln,",
            "vertreten durch Frau {schoellhorn:plain}",
            "– nachfolgend „Vireon“ –",
            "und der",
            "{brandtner:plain} GmbH & Co. KG, Gewerbering 7, 73760 Ostfildern,",
            "– nachfolgend „Auftragnehmer“ –",
        ],
        sections=[
            (
                "Gegenstand",
                [
                    "Gegenstand dieser {nda_de:plain} ist der Schutz vertraulicher Informationen, "
                    "die im Zusammenhang mit dem {nord@1:plain} zwischen den Parteien "
                    "ausgetauscht werden.",
                    d.fill(CONTRACT_DE, 1),
                ],
            ),
            (
                "Vertrauliche Informationen",
                [
                    d.fill(CONTRACT_DE, 2),
                    "Als vertraulich gelten insbesondere Pläne der {zutritt:plain}, Zugangscodes, "
                    "Lagepläne und Schaltpläne.",
                ],
            ),
            (
                "Pflichten der Parteien",
                [
                    [
                        d.fill(CONTRACT_DE, 1),
                        "Die Pflichten aus dieser {nda_de:hyphenated} gelten gleichermaßen für "
                        "alle mit den Parteien verbundenen Unternehmen.",
                    ],
                    d.fill(CONTRACT_DE, 1),
                ],
            ),
            ("Ausnahmen", [d.fill(CONTRACT_DE, 2)]),
            ("Rückgabe von Unterlagen", [d.fill(CONTRACT_DE, 2)]),
            ("Laufzeit", [d.fill(CONTRACT_DE, 2)]),
            ("Schlussbestimmungen", [d.fill(CONTRACT_DE, 3)]),
        ],
        signatures=[
            ("Köln, den 2. März 2026", "{schoellhorn:plain}", "Standortleitung"),
            ("Ostfildern, den 4. März 2026", "Paul Brandtner", "Geschäftsführer"),
        ],
        footer="Vertraulich",
    )
    stamp(d.sheets[0], 168, 40, ["KOPIE"], color=STAMP_RED, angle=16, pt=16)


@doc(
    "t036_contract_nda_en",
    fmt="pdf",
    family="freeserif",
    head="freeserif",
    align="justify",
    lang="en",
)
def t036(d: Doc) -> None:
    contract(
        d,
        title="MUTUAL {nda_en:case=NON-DISCLOSURE AGREEMENT}",
        preamble=[
            "between",
            "{quadrant:plain} GmbH, Leopoldstraße 88, 80802 Munich, Germany",
            "and",
            "{vireon@1:plain} Ltd., Cambridge Science Park, Cambridge CB4 0WG, United Kingdom",
        ],
        sections=[
            (
                "Purpose",
                [
                    "The parties intend to evaluate a possible cooperation under the name "
                    "{halcyon:plain} (the “Project”).",
                    d.fill(CONTRACT_EN, 1),
                ],
            ),
            ("Confidential Information", [d.fill(CONTRACT_EN, 2)]),
            (
                "Obligations",
                [
                    [
                        "This {nda_en:hyphenated} applies to all information disclosed in "
                        "connection with the Project, whether in writing, electronically or "
                        "orally.",
                        d.fill(CONTRACT_EN, 1),
                    ],
                ],
            ),
            ("Term", [d.fill(CONTRACT_EN, 2)]),
            ("Miscellaneous", [d.fill(CONTRACT_EN, 3)]),
        ],
        signatures=[
            ("Munich, 27 February 2026", "Dr. Ines Albers", "Managing Director"),
            ("Cambridge, 27 February 2026", "{schoellhorn:plain}", "Site Director"),
        ],
        sign="",
        page_word="Page",
        footer="Mutual NDA",
    )


@doc("t037_wartungsvertrag", fmt="tif", family="dvserif", head="dvserif")
def t037(d: Doc) -> None:
    contract(
        d,
        title="Wartungsvertrag Nr. WV-2026-031",
        preamble=[
            "zwischen der",
            "{quadrant:plain} GmbH, Leopoldstraße 88, 80802 München",
            "– Auftragnehmer –",
            "und der",
            "{kessler:plain} GmbH, {hafen:plain}, 28217 Bremen",
            "Kundennummer {kd:plain}",
            "– Auftraggeber –",
        ],
        sections=[
            (
                "Vertragsgegenstand",
                [
                    "Der Auftragnehmer übernimmt die Wartung und Instandsetzung der {bmz:plain} "
                    "sowie der {zutritt:plain} am Standort des Auftraggebers. Ansprechpartnerin "
                    "bei der {kessler:linebreak} GmbH ist die Standortleitung.",
                    d.fill(CONTRACT_DE, 1),
                ],
            ),
            (
                "Leistungsumfang",
                [
                    [
                        d.fill(TECH_DE, 2),
                        "Die Inspektion der {bmz:hyphenated} erfolgt vierteljährlich nach DIN "
                        "14675.",
                    ],
                    d.fill(TECH_DE, 2),
                ],
            ),
            (
                "Reaktionszeiten",
                [
                    "Störungen, die den Betrieb beeinträchtigen, werden innerhalb von vier "
                    "Stunden bearbeitet; alle übrigen Störungen am nächsten Werktag.",
                    d.fill(TECH_DE, 1),
                ],
            ),
            ("Vergütung", [d.fill(CONTRACT_DE, 2)]),
            ("Laufzeit und Kündigung", [d.fill(CONTRACT_DE, 2)]),
            ("Haftung", [d.fill(CONTRACT_DE, 2)]),
            ("Schlussbestimmungen", [d.fill(CONTRACT_DE, 3)]),
        ],
        signatures=[
            ("München, den 18. März 2026", "Dr. Tobias Renner", "Auftragnehmer"),
            ("Bremen, den 20. März 2026", "Katrin Oltmanns", "Auftraggeber"),
        ],
        header="Wartungsvertrag WV-2026-031",
        footer="[[Quadrant Systems|quadrant|footer, outside the body zone]] GmbH · Leopoldstraße "
        "88 · München",
    )
    stamp(
        d.sheets[0],
        150,
        44,
        ["{kessler:case=KESSLER LOGISTIK}", "Eingang 21. März 2026"],
        color=STAMP_BLUE,
        angle=6,
        pt=11,
    )


@doc("t038_rahmenvertrag", family="times", head="times", align="justify", degrade=("skew",))
def t038(d: Doc) -> None:
    contract(
        d,
        title="Rahmenvertrag über Beratungsleistungen",
        preamble=[
            "zwischen der Helvetor Maschinenbau AG, Friedrichshafen, und der Northgate Advisory "
            "LLP, London",
        ],
        sections=[
            (
                "Gegenstand",
                [
                    "Der Berater unterstützt die Gesellschaft bei der Vorbereitung und "
                    "Durchführung der {fuchs:plain}.",
                ],
            ),
            (
                "Vertraulichkeit",
                [
                    "Die zwischen den Parteien am 12. Januar 2026 geschlossene "
                    "{nda_de:inner_space} bleibt unberührt.",
                ],
            ),
            (
                "Datenschutz",
                [
                    "Soweit personenbezogene Daten verarbeitet werden, erstellt der Berater vorab "
                    "eine {dsfa:punct}.",
                    d.fill(CONTRACT_DE, 1),
                ],
            ),
            ("Schlussbestimmungen", [d.fill(CONTRACT_DE, 2)]),
        ],
        signatures=[
            ("Friedrichshafen, den 14. Januar 2026", "Klaus Dornbach", "Helvetor Maschinenbau AG"),
            ("London, 15 January 2026", "Oliver Pennington", "Northgate Advisory LLP"),
        ],
    )


@doc("t039_vertrag_auftragsverarbeitung", fmt="pdf", family="arial")
def t039(d: Doc) -> None:
    contract(
        d,
        title="Vertrag zur Auftragsverarbeitung",
        subtitle="gemäß Art. 28 DSGVO",
        preamble=[
            "zwischen der {vireon:plain} GmbH, Köln (Verantwortlicher)",
            "und der Helvetor Maschinenbau AG, Friedrichshafen (Auftragsverarbeiter)",
        ],
        sections=[
            (
                "Gegenstand und Dauer",
                [
                    "Der Auftragsverarbeiter betreibt für den Verantwortlichen das "
                    "Besuchermanagement am Standort Köln. Weisungen erteilt ausschließlich die "
                    "Datenschutzbeauftragte der {vireon:linebreak} GmbH in Textform.",
                    d.fill(CONTRACT_DE, 1),
                ],
            ),
            (
                "Art der Verarbeitung",
                [
                    "Grundlage ist die {dsfa:plain} des Verantwortlichen vom 19. März 2026. Eine "
                    "zweite, im Entwurf als „{dsfa:fuzzy}“ bezeichnete Fassung ersetzt die erste "
                    "nicht.",
                    d.fill(CONTRACT_DE, 1),
                ],
            ),
            (
                "Personal",
                [
                    "Mit der Verarbeitung dürfen nur Personen betraut werden, die eine "
                    "{sue:plain} erfolgreich durchlaufen haben; die Nachweise über die "
                    "{sue:inner_space} sind auf Verlangen vorzulegen.",
                    d.fill(CONTRACT_DE, 2),
                ],
            ),
            (
                "Vertraulichkeit",
                [
                    "Ergänzend gilt die zwischen den Parteien geschlossene {nda_de:plain}.",
                    d.fill(CONTRACT_DE, 2),
                ],
            ),
            ("Unterauftragsverhältnisse", [d.fill(CONTRACT_DE, 3)]),
            ("Technische Maßnahmen", [d.fill(TECH_DE, 3)]),
            ("Löschung und Rückgabe", [d.fill(CONTRACT_DE, 3)]),
            ("Haftung", [d.fill(CONTRACT_DE, 2)]),
        ],
        signatures=[
            ("Köln, den 20. März 2026", "Dr. Carla Menéndez", "Verantwortlicher"),
            ("Friedrichshafen, den 23. März 2026", "Klaus Dornbach", "Auftragsverarbeiter"),
        ],
        header="{weitergabe:case=NICHT ZUR WEITERGABE}",
    )


@doc("t040_mietvertrag", dpi=150, family="courier", head="courier", pt=10, degrade=("faint",))
def t040(d: Doc) -> None:
    contract(
        d,
        title="Mietvertrag über Lagerflächen",
        preamble=[
            "zwischen der {kessler:plain} GmbH, {hafen:plain}, Bremen, und der Helvetor "
            "Maschinenbau AG"
        ],
        sections=[
            (
                "Mietgegenstand",
                [
                    "Vermietet werden 420 m² Lagerfläche in Halle 3 einschließlich zweier "
                    "Rolltore.",
                ],
            ),
            (
                "Nutzung",
                [
                    "Die Flächen werden ausschließlich durch die Arbeitsgruppe {kranich:plain} "
                    "genutzt. Die Einlagerung erfolgt unter der internen Bezeichnung "
                    "{tarn:plain}; das {tarn:inword=Tarnkappenlager} ist nicht zu beschildern.",
                ],
            ),
            (
                "Mietzins",
                [
                    "Die Miete beträgt monatlich 3.150,00 EUR zuzüglich Nebenkosten.",
                    d.fill(CONTRACT_DE, 1),
                ],
            ),
            ("Schlussbestimmungen", [d.fill(CONTRACT_DE, 1)]),
        ],
        signatures=[
            ("Bremen, den 2. April 2026", "Katrin Oltmanns", "Vermieterin"),
            ("Friedrichshafen, den 6. April 2026", "Klaus Dornbach", "Mieterin"),
        ],
    )


# ---- forms


@doc("t041_antrag_zutritt", family="arial")
def t041(d: Doc) -> None:
    s = d.page()
    y = form_head(
        s,
        "Antrag auf Zutrittsberechtigung",
        "Helvetor Maschinenbau AG · Werkschutz · Formular WS-12 (Stand 01/2026)",
        d.accent,
    )
    o = d.occ("nord", "cells")
    _, y = table(
        s,
        20,
        y + 2,
        [55, 115],
        [
            ["Antragsteller/in", "{kuehnast:plain}"],
            ["Abteilung", "IT-Sicherheit"],
            [[Part(o, 0), ":"], Part(o, 1)],
            ["Standort", "{hafen:plain}, 28217 Bremen"],
            ["Zeitraum", "01.04.2026 – 30.09.2026"],
        ],
        d.st(10.5),
        head=False,
        head_fill=None,
        pad=2.4,
    )
    y += 7
    text(s, 20, y + 1, 55, d.units("Kundennummer des Dienstleisters", d.st(9, color=GREY)))
    comb(s, 80, y, "{kd:spaced}", d.st(12))
    y += 14
    y = text(s, 20, y, 170, d.units("Zutritt wird beantragt für:", d.st(10.5, "b"))) + 2
    y = ticks(
        s,
        22,
        y,
        [
            (True, "Haupteingang über die {zutritt:plain}"),
            (False, "Tiefgarage"),
            (True, "Serverraum UG"),
            (False, "Labor 2. OG"),
        ],
        d.st(10.5),
    )
    y += 6
    y = text(
        s,
        20,
        y,
        170,
        d.units(
            "Begründung: Betreuung der Netzwerktechnik während der Umbauphase; Zugang nur in "
            "Begleitung.",
            d.st(10.5),
        ),
    )
    sign_lines(s, y + 24)
    stamp(s, 150, y + 50, ["GENEHMIGT", "Werkschutz 18.03.2026"], color=STAMP_BLUE, angle=-9, pt=12)
    text(
        s,
        20,
        262,
        170,
        d.units(
            "Datenschutzhinweis: Die Verarbeitung Ihrer Daten erfolgt auf Grundlage der "
            "{dsfa:plain} vom 12.01.2026. Zutrittsprotokolle werden nach 90 Tagen gelöscht.",
            d.st(7, color=GREY),
        ),
    )


@doc("t042_besucherliste", fmt="jpg", family="dejavu", degrade=("jpeg",))
def t042(d: Doc) -> None:
    s = d.page()
    y = form_head(s, "Besucherliste Pforte Nord", "Woche 10/2026 · Helvetor Maschinenbau AG", GREY)
    w, k, m, a = (d.occ(key, "cells") for key in ("weiss", "kuehnast", "maass", "assmann"))
    rows = [
        ["Datum", "Besucher/in", "Firma", "besucht: Vorname", "Name", "Zeit"],
        ["02.03.", "Katrin Oltmanns", "{kessler:plain}", Part(w, 0), Part(w, 1), "09:10"],
        ["02.03.", "Dr. Tobias Renner", "{quadrant:plain}", Part(k, 0), Part(k, 1), "10:45"],
        ["03.03.", "Viktor Kaminski", "{brandtner:plain}", Part(m, 0), Part(m, 1), "08:30"],
        ["04.03.", "Oliver Pennington", "Northgate Advisory", Part(a, 0), Part(a, 1), "13:00"],
        [
            "05.03.",
            "[[Karen Kühnast|kuehnast|another first name]]",
            "Stadtwerke Lindenau",
            "Holger",
            "Wendt",
            "11:15",
        ],
        ["06.03.", "Petra Vogt", "Druckerei Weller", "–", "Poststelle", "07:50"],
        ["06.03.", "Marco Albrecht", "Albis Facility", "–", "Haustechnik", "09:40"],
    ]
    table(s, 18, y + 4, [15, 38, 37, 29, 37, 18], rows, d.st(8.5), pad=1.6)


@doc("t043_formular_sue", dpi=300, family="times", head="times")
def t043(d: Doc) -> None:
    s = d.page()
    running_header(s, "{weitergabe:case=NICHT ZUR WEITERGABE}", "Formular SÜ-3")
    y = text(s, 20, 26, 170, d.units("Erklärung zur {sue:plain}", d.hs(19)))
    y = text(
        s, 20, y + 1, 170, d.units("nach dem Sicherheitsüberprüfungsgesetz", d.st(10, color=GREY))
    )
    _, y = table(
        s,
        20,
        y + 6,
        [50, 120],
        [
            ["Name, Vorname", "{weiss@1:plain}"],
            ["Geburtsdatum", "14.08.1971"],
            ["Dienststelle", "Werkschutz, Friedrichshafen"],
            ["Vorgang", "{fuchs:plain}"],
        ],
        d.st(11),
        head=False,
        head_fill=None,
        pad=2.4,
    )
    y = text(s, 20, y + 7, 170, d.units("Art der Überprüfung:", d.st(11, "b"))) + 2
    y = ticks(
        s,
        22,
        y,
        [
            (False, "Ü1 – einfache {sue:plain}"),
            (True, "Ü2 – erweiterte {sue:plain}"),
            (False, "Ü3 – erweiterte {sue:plain} mit Sicherheitsermittlungen"),
        ],
        d.st(11),
    )
    y = text(
        s,
        20,
        y + 6,
        170,
        d.units(
            "Ich versichere, dass meine Angaben vollständig und richtig sind. Mir ist bekannt, "
            "dass unvollständige Angaben zur Ablehnung führen können.",
            d.st(11),
        ),
    )
    sign_lines(s, y + 26)
    stamp(
        s,
        140,
        y + 52,
        ["{sue:case=SICHERHEITSÜBERPRÜFUNG}", "eingeleitet 18.03.2026"],
        color=STAMP_BLUE,
        angle=-7,
        pt=12,
    )
    text(
        s,
        20,
        266,
        170,
        d.units(
            "Angaben zur {sue:plain} werden vertraulich behandelt und nach Abschluss des "
            "Verfahrens gesondert aufbewahrt.",
            d.st(7, color=GREY),
        ),
    )


@doc("t044_bestellformular", family="helv")
def t044(d: Doc) -> None:
    s = d.page()
    y = form_head(
        s,
        "Bestellung Ersatzteile",
        "Bitte vollständig ausfüllen und an den Einkauf senden",
        d.accent,
    )
    text(s, 20, y + 3, 50, d.units("Kundennummer", d.st(10, "b")))
    comb(s, 70, y + 2, "{kd:spaced}", d.st(12))
    text(s, 20, y + 13, 50, d.units("Artikelnummer", d.st(10, "b")))
    comb(s, 70, y + 12, "{fs220:spaced}", d.st(12))
    text(s, 20, y + 23, 50, d.units("Stückzahl", d.st(10, "b")))
    comb(s, 70, y + 22, "60", d.st(12))
    sx = d.occ("sentinel", "cells")
    _, y = table(
        s,
        20,
        y + 36,
        [34, 26, 26, 64, 20],
        [
            ["Art.-Nr.", "Hersteller", "Typ", "Bezeichnung", "Menge"],
            ["{fsre:plain=FS-310}", "Brandtner", "Kopf W", "Sensorkopf Weitwinkel", "10"],
            ["AE-8", Part(sx, 0), Part(sx, 1), "Auswerteeinheit", "1"],
            ["MK-12", "Brandtner", "MK", "Montagekit Zwischendecke", "2"],
        ],
        d.st(10),
    )
    y = text(
        s,
        20,
        y + 8,
        170,
        d.units("Lieferanschrift: {kessler:plain} GmbH, {hafen:plain}, 28217 Bremen", d.st(10)),
    )
    y = text(s, 20, y + 2, 170, d.units("Rückfragen: {mail:plain=einkauf@helvetor.de}", d.st(10)))
    sign_lines(s, y + 26, ("Datum", "Unterschrift Besteller/in"))


@doc("t045_pruefprotokoll", fmt="tif", family="arial", degrade=("blur",))
def t045(d: Doc) -> None:
    s = d.page()
    y = form_head(
        s,
        "Prüfprotokoll Gefahrenmeldeanlagen",
        "Wiederkehrende Prüfung nach DIN 14675 / VDE 0833",
        RED,
    )
    _, y = table(
        s,
        20,
        y + 3,
        [30, 50, 30, 60],
        [
            ["Betreiber", "{kessler:plain} GmbH", "Prüfdatum", "12.03.2026"],
            ["Standort", "{hafen:linebreak}", "Prüfer", "Holger Wendt, Sachverständiger"],
        ],
        d.st(10),
        head=False,
        head_fill=None,
        pad=2,
    )
    _, y = table(
        s,
        20,
        y + 8,
        [70, 30, 70],
        [
            ["Prüfobjekt", "Ergebnis", "Bemerkung"],
            ["{bmz:plain}", "i. O.", "Batterien getauscht"],
            ["{fruehwarn:inner_space}", "i. O.", "–"],
            ["{zutritt:plain}", "Mängel", "Türkontakt Tor 3 defekt"],
            ["Sprachalarmanlage", "i. O.", "–"],
        ],
        d.st(10),
    )
    y = text(
        s,
        20,
        y + 8,
        170,
        d.units(
            "Der Mangel an Tor 3 ist bis zum 31.03.2026 zu beheben. Eine Nachprüfung ist nicht "
            "erforderlich, wenn die Behebung schriftlich bestätigt wird.",
            d.st(10),
        ),
    )
    sign_lines(s, y + 24, ("Ort, Datum", "Sachverständiger"))


@doc("t046_lieferantenauskunft", family="arial")
def t046(d: Doc) -> None:
    s = d.page()
    running_header(
        s,
        "[[Quadrant Systems|quadrant|running header, outside the body zone]] – Selbstauskunft für "
        "Lieferanten",
        "Stand 03/2026",
    )
    y = text(s, 20, 26, 170, d.units("Selbstauskunft Lieferant", d.hs(18)))
    y = text(
        s,
        20,
        y + 4,
        170,
        d.units(
            "Die folgenden Angaben dienen der Aufnahme in das Lieferantenverzeichnis der Helvetor "
            "Maschinenbau AG. Änderungen sind unverzüglich mitzuteilen.",
            d.st(10.5),
        ),
    )
    table(
        s,
        20,
        max(y + 16, 64),
        [55, 115],
        [
            ["Firma", "{quadrant:plain} GmbH"],
            ["Anschrift", "Leopoldstraße 88, 80802 München"],
            ["Ansprechpartner", "Dr. Tobias Renner"],
            ["E-Mail", "service@qs-gmbh.de"],
            ["IBAN", "{iban:plain}"],
            ["Konto Schweiz", "[[CH93 0076 2011 6238 5295 7|iban|Swiss IBAN]]"],
            ["Zertifikate", "ISO 9001, VdS-Errichter"],
        ],
        d.st(10.5),
        head=False,
        head_fill=None,
        pad=2.4,
    )
    running_footer(
        s, "[[Quadrant Systems|quadrant|footer, outside the body zone]] GmbH", "Seite 1 von 1"
    )


@doc("t047_checkliste", dpi=150, family="dejavu", degrade=("skew",))
def t047(d: Doc) -> None:
    s = d.page()
    y = text(
        s,
        20,
        24,
        170,
        d.units("Checkliste Inbetriebnahme {zutritt:case=ZUTRITTSKONTROLLANLAGE}", d.hs(16)),
    )
    y = text(
        s, 20, y + 2, 170, d.units("Standort Friedrichshafen, Gebäude B", d.st(10, color=GREY))
    )
    y = ticks(
        s,
        22,
        y + 8,
        [
            (True, "Verbindung zur {bmz:plain} geprüft"),
            (True, "Zeitsynchronisation mit dem Server {nox:plain} eingerichtet"),
            (False, "Protokollierung an {orka:spaced} aktiviert"),
            (True, "Notstromversorgung getestet"),
            (False, "Einweisung des Pfortenpersonals"),
            (True, "Ausweiskarten der Testgruppe freigeschaltet"),
        ],
        d.st(11),
    )
    sign_lines(s, y + 30, ("Datum", "Techniker/in"))


# ---- slides


@doc("t048_folien_nordlicht", fmt="pdf", dpi=150, family="arial")
def t048(d: Doc) -> None:
    footer = "{weitergabe:plain}"
    slide(
        d,
        title="{nord:spaced}",
        sub="Statusbericht an den Lenkungskreis · März 2026",
        bullets=["Ausgangslage und Ziele", "Stand der Umsetzung", "Nächste Schritte"],
        number=1,
        footer_left=footer,
    )
    slide(
        d,
        title="Stand der Umsetzung",
        bullets=[
            "{zutritt:plain}: Montage zu 80 % abgeschlossen",
            "{bmz:plain}: Abnahme am 24. März",
            "{fruehwarn:plain}: Testbetrieb seit KW 9",
            "Budget 2,4 Mio. EUR, davon 1,7 Mio. EUR gebunden",
        ],
        number=2,
        footer_left=footer,
    )
    slide(
        d,
        title="Nächste Schritte",
        bullets=[
            "{dsfa:fuzzy=Datenschutzfolgenabschätzng} bis Ende April abschließen",
            "{sue:plain} für das externe Wachpersonal",
            "Schulung der Pfortenkräfte im Mai",
        ],
        number=3,
        footer_left=footer,
    )


@doc("t049_folie_vega", family="dejavu")
def t049(d: Doc) -> None:
    slide(
        d,
        title="{vega:spaced}",
        sub="Plattform für Messdatenerfassung",
        bullets=[
            "Namensgeber ist der hellste Stern im Sternbild Leier, die [[Vega|vega|wrong case for "
            "a case-sensitive term]]",
            "{vega:plain|b} ersetzt ab dem dritten Quartal das Altsystem {orka:plain}",
            "Pilotbetrieb am Standort {hafen:plain}",
            "Anbindung vorhandener Sensorik über OPC UA",
        ],
        footer_left="Helvetor Maschinenbau AG · Interne Präsentation",
    )


@doc("t050_slides_en", fmt="pdf", family="helv", lang="en")
def t050(d: Doc) -> None:
    slide(
        d,
        title="{halcyon:spaced}",
        sub="Integration workshop · Munich · 12 March 2026",
        bullets=["Scope", "Organisation", "Timeline"],
        footer_left="Northgate Advisory · Confidential",
    )
    slide(
        d,
        title="Scope",
        bullets=[
            "Rollout of the {sentinel:plain} evaluation unit at three sites",
            "{quadrant:plain} leads the technical integration",
            "Security review before go-live",
            "Budget approval expected in April",
        ],
        number=2,
        footer_left="Northgate Advisory · Confidential",
    )


@doc("t051_folie_silberfuchs", dpi=150, family="arial")
def t051(d: Doc) -> None:
    slide(
        d,
        title="{fuchs:case=OPERATION SILBERFUCHS}",
        sub="Phase 2 – Integration",
        bullets=[
            "Ziel der {fuchs:plain|c}: Abschluss bis Jahresende",
            "Due Diligence abgeschlossen, keine wesentlichen Risiken",
            "Nächster Termin mit dem Aufsichtsrat am 14. April",
        ],
        footer_left="{weitergabe:case=NICHT ZUR WEITERGABE}",
        band=RED,
    )


@doc("t052_folie_bruecke", family="loma", head="loma")
def t052(d: Doc) -> None:
    slide(
        d,
        title="{bruecke:plain}",
        sub="Sanierung der Fußgängerbrücke am Werkstor West",
        bullets=[
            "Bauzeit von Mai bis August 2026",
            "Kampagne im Intranet unter #{bruecke:glued}",
            "Die [[blaue Brückenwaage|bruecke|inside a longer word]] im Lager wird im Zuge der "
            "Arbeiten ersetzt",
            "Umleitung für Fußgänger über Tor Ost",
        ],
        footer_left=slide_footer("Helvetor Maschinenbau AG"),
    )


# ---- newsletters


@doc("t053_werkspost_1", family="times", head="arial", pt=9.5)
def t053(d: Doc) -> None:
    o = d.occ("nord", "columns")
    b = d.occ("bruecke", "columns")
    newsletter(
        d,
        masthead="Werkspost",
        issue="Ausgabe 3/2026 · Mitteilungen für die Beschäftigten der Helvetor Maschinenbau AG",
        pages=[
            [
                [
                    [
                        ("k", "Sicherheit"),
                        ("h", "Neue Technik für den Standort Bremen"),
                        ("p", [d.fill(NEWS_DE, 2), "Mit dem", Part(o, 0)]),
                    ],
                    [
                        (
                            "p",
                            [
                                Part(o, 1),
                                "wird bis zum Sommer die gesamte Zutrittstechnik am Standort "
                                "erneuert.",
                                d.fill(NEWS_DE, 1),
                            ],
                        ),
                        ("h", "Reisebericht"),
                        (
                            "p",
                            "Unsere Kollegin aus der Buchhaltung hat im Winterurlaub in Tromsø "
                            "das [[Nordlicht|nord|half of the phrase]] fotografiert – die "
                            "schönsten Bilder zeigen wir im nächsten Heft.",
                        ),
                    ],
                ],
                [
                    [
                        ("h", "Spendenaktion"),
                        (
                            "p",
                            [
                                "Die Aktion {bruecke:plain} hat 12.400 Euro für die Sanierung des "
                                "Jugendhauses erbracht.",
                                d.fill(NEWS_DE, 2),
                                "Weil der Erfolg so groß war, geht auch im nächsten Jahr eine "
                                "Aktion mit dem Namen",
                                Part(b, 0),
                            ],
                        ),
                    ],
                    [
                        ("p", [Part(b, 1), "an den Start."]),
                        ("h", "Aus den Standorten"),
                        (
                            "p",
                            [
                                "Der Umzug der Entwicklung in die [[Kranichsteiner "
                                "Straße|kranich|inside a longer word]] in Darmstadt ist "
                                "abgeschlossen.",
                                d.fill(NEWS_DE, 2),
                            ],
                        ),
                    ],
                ],
                [
                    [
                        ("h", "Sommerfest"),
                        ("p", d.fill(NEWS_DE, 3)),
                        ("h", "Betriebssport"),
                        ("p", d.fill(NEWS_DE, 3)),
                    ],
                    [
                        ("h", "Personalien"),
                        ("p", d.fill(NEWS_DE, 3)),
                        ("h", "Termine"),
                        ("p", d.fill(NEWS_DE, 3)),
                    ],
                ],
            ]
        ],
        footer="Werkspost 3/2026 · Redaktion: Unternehmenskommunikation",
    )


@doc("t054_werkspost_2", fmt="pdf", family="dvserif", head="dejavu", pt=9.5)
def t054(d: Doc) -> None:
    z = d.occ("zutritt", "columns")
    kl = d.occ("kessler", "columns")
    newsletter(
        d,
        masthead="Werkspost",
        issue="Ausgabe 4/2026 · Mitteilungen für die Beschäftigten der Helvetor Maschinenbau AG",
        pages=[
            [
                [
                    [
                        ("k", "Technik"),
                        ("h", "Mehr Sicherheit an allen Toren"),
                        (
                            "p",
                            [
                                d.fill(NEWS_DE, 1),
                                "Seit Februar wird an allen Werkstoren eine neue",
                                Part(z, 0),
                            ],
                        ),
                    ],
                    [
                        (
                            "p",
                            [
                                Part(z, 1),
                                "installiert, die mit den neuen Ausweiskarten arbeitet.",
                                d.fill(NEWS_DE, 1),
                            ],
                        ),
                        ("h", "Im Gespräch"),
                        (
                            "p",
                            [
                                "Im Interview erläutert {schoellhorn:plain}, wie die Standorte in "
                                "Köln und Friedrichshafen künftig zusammenarbeiten.",
                                d.fill(NEWS_DE, 1),
                            ],
                        ),
                    ],
                ],
                [
                    [
                        ("h", "Warnung vor Unwettern"),
                        (
                            "p",
                            [
                                "Das neue {fruehwarn:hyphenated} informiert alle Beschäftigten "
                                "per SMS, sobald der Wetterdienst eine Unwetterwarnung für einen "
                                "Standort herausgibt.",
                                d.fill(NEWS_DE, 1),
                            ],
                        ),
                    ],
                    [
                        ("h", "Logistik"),
                        (
                            "p",
                            [
                                "Unser Partner {kessler:plain} übernimmt ab April auch die "
                                "Ersatzteillogistik für die Werke im Norden.",
                                d.fill(NEWS_DE, 1),
                            ],
                        ),
                    ],
                ],
            ],
            [
                [
                    [
                        ("h", "Messe"),
                        (
                            "p",
                            [
                                "Auf der Hannover Messe zeigte das Team erstmals die Funktion "
                                "{tarn:plain} der neuen Messdatenplattform; im {tarn:inword} "
                                "bleiben die Namen der Prüflinge verborgen.",
                                d.fill(NEWS_DE, 2),
                                "Den Transport der Exponate übernahm wie in den Vorjahren die",
                                Part(kl, 0),
                            ],
                        ),
                    ],
                    [
                        ("p", [Part(kl, 1), "aus Bremen."]),
                        ("h", "Kantine"),
                        ("p", d.fill(NEWS_DE, 3)),
                    ],
                ],
                [
                    [("h", "Betriebssport"), ("p", d.fill(NEWS_DE, 2))],
                    [("h", "Termine"), ("p", d.fill(NEWS_DE, 2))],
                ],
            ],
        ],
        footer="Werkspost 4/2026",
    )


@doc("t055_werkspost_3", fmt="jpg", family="freeserif", head="helv", pt=10, degrade=("jpeg",))
def t055(d: Doc) -> None:
    kr = d.occ("kranich", "columns")
    newsletter(
        d,
        masthead="Werkspost",
        issue="Ausgabe 5/2026 · Neues aus Entwicklung und Betrieb",
        pages=[
            [
                [
                    [
                        ("h", "Unsichtbar messen"),
                        (
                            "p",
                            [
                                "Im {tarn:inword} blendet die Messdatenplattform alle Namen von "
                                "Prüflingen aus. Entwickelt wurde die Funktion {tarn:plain} von "
                                "einem kleinen Team in Friedrichshafen.",
                                d.fill(NEWS_DE, 1),
                            ],
                        ),
                    ],
                    [
                        ("h", "Neue Plattform"),
                        (
                            "p",
                            [
                                "Seit März läuft die Messdatenplattform {vega:plain} im "
                                "Pilotbetrieb.",
                                d.fill(NEWS_DE, 1),
                            ],
                        ),
                    ],
                ],
                [
                    [
                        ("h", "Kantine"),
                        (
                            "p",
                            "In der Woche nach Ostern lädt die Kantine zur [[VEGANE|vega|inside a "
                            "longer word]] WOCHE ein: jeden Tag ein Gericht ohne tierische "
                            "Produkte.",
                        ),
                    ],
                    [
                        ("h", "Umzug"),
                        (
                            "p",
                            [
                                "Die Arbeitsgruppe {kranich:plain} zieht im Mai in das zweite "
                                "Obergeschoss von Gebäude B.",
                                d.fill(NEWS_DE, 2),
                            ],
                        ),
                    ],
                ],
                [
                    [
                        ("h", "Neue Räume"),
                        (
                            "p",
                            [
                                d.fill(NEWS_DE, 2),
                                "Auch die Gruppe",
                                Part(kr, 0),
                            ],
                        ),
                    ],
                    [
                        (
                            "p",
                            [
                                Part(kr, 1),
                                "bekommt dort einen eigenen Besprechungsraum.",
                                d.fill(NEWS_DE, 2),
                            ],
                        )
                    ],
                ],
                [
                    [("h", "Gesundheit"), ("p", d.fill(NEWS_DE, 3))],
                    [("h", "Termine"), ("p", d.fill(NEWS_DE, 3))],
                ],
            ]
        ],
        footer="Werkspost 5/2026",
    )


@doc("t056_werkspost_4", dpi=150, family="helv", head="helv", pt=10)
def t056(d: Doc) -> None:
    f = d.occ("fuchs", "columns")
    newsletter(
        d,
        masthead="Werkspost",
        issue="Ausgabe 6/2026 · Personalien und Termine",
        pages=[
            [
                [
                    [
                        ("h", "Jubiläum"),
                        (
                            "p",
                            [
                                "Seit 25 Jahren im Einkauf: {maass:plain} feierte im März ihr "
                                "Dienstjubiläum.",
                                d.fill(NEWS_DE, 1),
                            ],
                        ),
                        ("h", "Übernahme"),
                        (
                            "p",
                            [
                                "Der Vorstand informierte in der Betriebsversammlung über die",
                                Part(f, 0),
                            ],
                        ),
                    ],
                    [
                        (
                            "p",
                            [
                                Part(f, 1),
                                "und die nächsten Schritte bis zum Jahresende.",
                                d.fill(NEWS_DE, 1),
                            ],
                        ),
                        ("h", "Termine"),
                        ("p", d.fill(NEWS_DE, 3)),
                    ],
                ],
                [
                    [("h", "Aus dem Betriebsrat"), ("p", d.fill(NEWS_DE, 3))],
                    [("h", "Kantine"), ("p", d.fill(NEWS_DE, 3))],
                ],
            ]
        ],
        footer="Werkspost 6/2026",
    )


# ---- file notes


@doc("t057_aktennotiz_falke", family="courier", head="courier", pt=10.5)
def t057(d: Doc) -> None:
    story = memo(
        d,
        kind="Aktennotiz",
        fields=[
            ("Datum", "13. März 2026"),
            ("Verfasser", "{weiss:plain}"),
            ("Betreff", "Deckname {falke:plain}"),
            ("Verteiler", "Vorstand, Rechtsabteilung"),
        ],
        body=[
            "Im Gespräch mit der Rechtsabteilung wurde festgelegt, dass das neue Produkt bis zur "
            "Markteinführung nur intern kommuniziert wird.",
            d.fill(MINUTES_DE, 2),
            "Eine Verbindung zur laufenden Übernahme darf nach außen nicht hergestellt werden.",
        ],
        sign=["gez. J. W."],
    )
    stamp(
        story.s,
        140,
        story.y + 28,
        ["{fuchs:case=OPERATION SILBERFUCHS}"],
        color=STAMP_RED,
        angle=17,
        pt=15,
    )


@doc("t058_hausmitteilung_it", family="arial")
def t058(d: Doc) -> None:
    memo(
        d,
        kind="Hausmitteilung IT",
        fields=[
            ("An", "Alle Beschäftigten der Prüffelder"),
            ("Von", "IT-Betrieb"),
            ("Datum", "20. März 2026"),
            ("Betreff", "Neue Funktion in der Messdatenplattform"),
        ],
        body=[
            "Ab sofort steht in der Messdatenplattform {vega:plain} die Funktion {tarn:plain} zur "
            "Verfügung. Ist {tarn:case=TARNKAPPE} aktiv, werden die Namen von Prüflingen in allen "
            "Auswertungen ausgeblendet. Die {tarn:inword=Tarnkappenfunktion} lässt sich pro "
            "Prüffeld abschalten.",
            "Die Rohdaten werden weiterhin jede Nacht von {orka:plain} auf den Archivserver "
            "{nox:plain} übertragen.",
            d.fill(TECH_DE, 2),
        ],
        sign=["IT-Betrieb, Durchwahl 450"],
    )


@doc("t059_hausmitteilung", fmt="tif", family="dejavu", degrade=("faint",))
def t059(d: Doc) -> None:
    story = memo(
        d,
        kind="Hausmitteilung",
        fields=[
            ("An", "Standortleitungen"),
            ("Von", "Rechtsabteilung"),
            ("Datum", "2. März 2026"),
            ("Betreff", "Neue Zeichnungsbefugnisse"),
        ],
        body=[
            "Mit Wirkung vom 1. März 2026 zeichnet Frau {assmann:hyphenated} für alle Verträge "
            "mit einem Volumen über 250.000 Euro gemeinsam mit einem weiteren Vorstandsmitglied.",
            "Für den Standort Köln bleibt Frau {schoellhorn:plain} zuständig. Die Spendenaktion "
            "{bruecke:punct} wird von der Rechtsabteilung begleitet.",
            d.fill(LETTER_DE, 2),
        ],
        sign=["Rechtsabteilung"],
    )
    stamp(story.s, 160, 34, ["EILT"], color=STAMP_RED, angle=-8, pt=18)


@doc("t060_vermerk", dpi=300, family="times", head="times", degrade=("skew",))
def t060(d: Doc) -> None:
    memo(
        d,
        kind="Vermerk",
        fields=[
            ("Aktenzeichen", "GS 14/2026"),
            ("Datum", "18. März 2026"),
            ("Betreff", "Ergänzende Angaben"),
        ],
        body=[
            "Herr {weiss@1:plain} hat die fehlenden Angaben zur {sue:hyphenated} heute persönlich "
            "nachgereicht. Die Akte von Herrn {weiss:linebreak} wird damit zur Prüfung an die "
            "zuständige Stelle weitergeleitet.",
            "Die Rückfrage von Herrn {kuehnast:fuzzy} aus der IT-Sicherheit zu den "
            "Zugriffsrechten wurde telefonisch beantwortet.",
            d.fill(MINUTES_DE, 2),
        ],
        sign=["H. Wendt"],
    )


# ---- fax cover sheets


@doc("t061_fax_kessler", fmt="tif", family="arial", pt=12, degrade=("fax",))
def t061(d: Doc) -> None:
    fax_cover(
        d,
        tti="04.03.2026 16:22  +49 421 3398-0  {kessler:case=KESSLER LOGISTIK}  S. 01",
        company="{kessler:plain} GmbH",
        company_lines=["{hafen:plain} · 28217 Bremen", "Tel. 0421 3398-0 · Fax 0421 3398-99"],
        fields=[
            ("An:", "{brandtner:plain}", "Von:", "Katrin Oltmanns"),
            ("z. Hd.:", "Viktor Kaminski", "Datum:", "4. März 2026"),
            ("Fax:", "0711 348 82-99", "Seiten:", "1"),
        ],
        message=[
            "Sehr geehrter Herr Kaminski,",
            "die bestellten Sensoren {fs220:plain} bitte direkt an unser Lager in der "
            "{hafen:plain} liefern, nicht an die Zentrale. Unsere Kundennummer bei Ihnen lautet "
            "{kd:plain}.",
            "Vielen Dank und freundliche Grüße",
        ],
        sign=["Katrin Oltmanns"],
    )


@doc("t062_fax_brandtner", family="helv", pt=12, degrade=("fax",))
def t062(d: Doc) -> None:
    fax_cover(
        d,
        tti="05.03.2026 10:12  +49 711 348820  BRANDTNER VERTRIEB  S. 01/03",
        company="{brandtner:plain}",
        company_lines=["Gewerbering 7 · 73760 Ostfildern", "Tel. 0711 348 82-0"],
        fields=[
            ("An:", "Helvetor Maschinenbau AG", "Von:", "Viktor Kaminski"),
            ("z. Hd.:", "Rechtsabteilung", "Datum:", "5. März 2026"),
            ("Fax:", "07541 204-399", "Seiten:", "3"),
        ],
        message=[
            "Sehr geehrte Damen und Herren,",
            "anbei erhalten Sie die von Frau {assmann:plain} unterzeichnete {nda_de:plain} "
            "zurück. Das Original folgt per Post.",
            "Mit freundlichen Grüßen",
        ],
        sign=["Viktor Kaminski"],
    )


@doc("t063_fax_bruecke", fmt="tif", family="arial", pt=12, degrade=("fax",))
def t063(d: Doc) -> None:
    fax_cover(
        d,
        tti="09.03.2026 14:55  +49 421 5591-20  HARTIG PARTNER  S. 01",
        company="Ingenieurbüro Hartig & Partner",
        company_lines=["Am Güterbahnhof 3 · 28195 Bremen"],
        fields=[
            ("An:", "Helvetor Maschinenbau AG", "Von:", "Svenja Hartig"),
            ("z. Hd.:", "Werkschutz", "Datum:", "9. März 2026"),
            ("Betreff:", "{bruecke:plain}", "Seiten:", "1"),
        ],
        message=[
            "Sehr geehrte Damen und Herren,",
            "für die Baustelle {bruecke:lookalike} am Werkstor West wird die {zutritt:plain} "
            "vorübergehend auf Tor Ost umgelegt. Die Arbeitsgruppe {kranich:plain} ist informiert.",
            "Mit freundlichen Grüßen",
        ],
        sign=["Svenja Hartig"],
    )


# ---- data sheets and reports


@doc("t064_datasheet_sentinel", dpi=300, family="arial", lang="en")
def t064(d: Doc) -> None:
    story = Story(d, left=20, width=170, top=48, bottom=272)
    s = story.new_page()
    s.rect(0, 0, 210, 38, fill=d.accent)
    text(s, 20, 9, 170, d.units("{sentinel:plain}", d.hs(30, color=WHITE)))
    text(
        s, 20, 25, 170, d.units("Multi-sensor evaluation unit · Data sheet", d.st(12, color=WHITE))
    )
    story.para(
        [
            "The {sentinel@1:plain} evaluates up to eight presence and door sensors and reports "
            "events to access control and building management systems.",
            d.fill(EN, 1),
        ]
    )
    story.heading("Technical data", d.hs(13))
    story.table(
        [60, 110],
        [
            ["Parameter", "Value"],
            ["Inputs", "8 × sensor bus, 2 × relay"],
            ["Supply", "24 V DC, max. 6 W"],
            ["Operating temperature", "−10 °C to +55 °C"],
            ["Protection", "IP 54"],
            ["Compatible sensors", "{fsre:plain=FS-310}, {fs220:plain}"],
            ["Dimensions", "160 × 110 × 45 mm"],
        ],
        d.st(10),
    )
    story.heading("Ordering information", d.hs(13))
    story.table(
        [50, 120],
        [
            ["Order code", "Description"],
            ["{sentinel:punct}", "Evaluation unit, DIN rail mounting"],
            [
                "[[Sentinel X40|sentinel|longer model number]]",
                "Power supply 24 V for the evaluation unit",
            ],
            ["MK-12", "Mounting kit"],
        ],
        d.st(10),
    )
    story.para(
        "Note: the successor model [[Sentinel X5|sentinel|one character off (the term allows no "
        "edits)]] will be available from the fourth quarter of 2026.",
        d.st(9.5, "i"),
    )
    running_footer(s, "Specifications are subject to change without notice.", "Rev. 2026-02")


@doc("t065_datenblatt_fs", family="dejavu")
def t065(d: Doc) -> None:
    story = Story(d, left=20, width=170, top=26, bottom=272)
    s = story.new_page()
    story.para("Präsenzsensor {fs220:spaced}", d.hs(24, color=d.accent), after=1)
    story.para("Deckeneinbau · 360° · Busanschluss", d.st(11, color=GREY), after=8)
    story.table(
        [60, 110],
        [
            ["Merkmal", "Wert"],
            ["Typ", "{fs220:plain}"],
            ["Nachfolger", "{fsre:plain=FS-265}"],
            ["Erfassungsbereich", "360°, Durchmesser 8 m bei 2,8 m Montagehöhe"],
            ["Versorgung", "über Sensorbus, 12–30 V DC"],
            ["Schutzart", "IP 40"],
        ],
        d.st(10.5),
    )
    story.para(
        "Hinweis: Nicht kompatibel mit Sockeln der Baureihe [[FS-2200|fs220|longer number]]. Der "
        "Abdeckring [[FS-31|fsre|two digits only]] ist separat zu bestellen.",
        d.st(10),
    )
    story.para(d.fill(TECH_DE, 2), d.st(10))
    running_footer(s, "{brandtner:plain} · Technische Änderungen vorbehalten", "Ausgabe 01/2026")


def report_furniture(header: Content, footer: Content, right: str) -> Callable[[Sheet], None]:
    def furniture(sheet: Sheet) -> None:
        running_header(sheet, header, f"Seite {sheet.number}")
        running_footer(sheet, footer, right)

    return furniture


@doc("t066_bericht_nordlicht", fmt="pdf", family="times", head="arial", align="justify")
def t066(d: Doc) -> None:
    story = Story(
        d,
        top=26,
        bottom=268,
        furniture=report_furniture(
            "{nord:plain} · Zwischenbericht 1/2026",
            "{weitergabe:plain}",
            "Helvetor Maschinenbau AG · Werkschutz",
        ),
    )
    story.para("Zwischenbericht {nord:plain}", d.hs(22), after=2, align="left")
    story.para("Berichtszeitraum Oktober 2025 bis Februar 2026", d.st(11, color=GREY), after=4)
    stamp(
        story.s,
        150,
        story.y + 16,
        ["{nord:case=PROJEKT NORDLICHT}", "Freigabe 02/2026"],
        color=STAMP_BLUE,
        angle=-10,
        pt=13,
    )
    story.space(34)
    h = d.hs(12.5)
    story.heading("1 Ausgangslage", h)
    story.para(
        [
            "Das {nord@1:plain} wurde im Herbst 2025 vom Vorstand beschlossen. Ziel ist die "
            "Modernisierung der Sicherheitstechnik an allen deutschen Standorten.",
            d.fill(REPORT_DE, 2),
        ]
    )
    story.heading("2 Stand der Umsetzung", h)
    story.para(
        [
            "Die {zutritt:plain} ist in Bremen zu 80 % montiert.",
            d.fill(REPORT_DE, 2),
            "In der Projektablage heißt der Hauptordner {nord:glued}; ältere Unterlagen tragen "
            "noch die Bezeichnung {nord:punct}.",
        ]
    )
    story.table(
        [50, 30, 85],
        [
            ["Gewerk", "Stand", "Bemerkung"],
            ["Kartenleser", "80 %", "EG und 1. OG fertig"],
            ["Videotechnik", "60 %", "Kameras Außenbereich fehlen"],
            ["Leitstelle", "40 %", "Umzug im Mai"],
        ],
        d.st(9.5),
    )
    story.heading("3 Datenschutz", h)
    story.para(
        [
            "Die {dsfa:plain} liegt im Entwurf vor; die Anlage zur {dsfa:inner_space} beschreibt "
            "die Löschfristen.",
            d.fill(REPORT_DE, 3),
        ]
    )
    story.heading("4 Risiken", h)
    story.para(
        [
            d.fill(REPORT_DE, 2),
            "Ein Lieferant schrieb in seiner Rückmeldung wörtlich von „{nord:fuzzy}“; die "
            "Bestellung wurde dennoch richtig zugeordnet.",
        ]
    )
    story.para(
        [
            "Der Bericht ist [[nicht zur Weitergabe|weitergabe|body text, outside the term's "
            "zones]] an Dritte bestimmt.",
            d.fill(REPORT_DE, 2),
        ]
    )
    story.heading("5 Ausblick", h)
    story.para(
        [
            d.fill(REPORT_DE, 2),
            "Bis zum Sommer soll die Umstellung im Rahmen von {nord:linebreak} an allen "
            "Standorten abgeschlossen sein.",
            d.fill(MINUTES_DE, 2),
        ]
    )
    story.para([d.fill(TECH_DE, 3), d.fill(MINUTES_DE, 3)])


@doc("t067_wartungsbericht", fmt="pdf", dpi=150, family="helv", degrade=("blur",))
def t067(d: Doc) -> None:
    story = Story(
        d,
        top=26,
        bottom=268,
        furniture=report_furniture("Albis Facility Services · Wartungsbericht 03/2026", "", ""),
    )
    story.para("Wartungsbericht {bmz:plain}", d.hs(20), after=2)
    story.para(
        "Objekt {hafen:plain}, 28217 Bremen · Wartung am 12. März 2026",
        d.st(10.5, color=GREY),
        after=6,
    )
    story.para(
        [
            "Die {bmz:inner_space} wurde gemäß Wartungsplan geprüft; alle Meldergruppen lösten "
            "ordnungsgemäß aus.",
            d.fill(TECH_DE, 2),
        ]
    )
    story.table(
        [70, 30, 65],
        [
            ["{bmz:case=BRANDMELDEZENTRALE}", "Ergebnis", "Maßnahme"],
            ["Meldergruppe 1–12", "i. O.", "–"],
            ["Meldergruppe 13–20", "i. O.", "Melder 17 getauscht"],
            ["Akkus", "i. O.", "Tausch 2027"],
            ["Übertragungseinrichtung", "i. O.", "–"],
        ],
        d.st(9.5),
    )
    story.para(
        [
            "Die Anzeige meldete beim Neustart den Text „{bmz:fuzzy} – Systemfehler 17“; der "
            "Fehler ließ sich durch einen Neustart beheben.",
            d.fill(TECH_DE, 2),
        ]
    )
    story.para(["Das angeschlossene {fruehwarn:plain} wurde nicht geprüft.", d.fill(TECH_DE, 3)])
    for _ in range(3):
        story.para([d.fill(TECH_DE, 2), d.fill(REPORT_DE, 3)])
    story.para([d.fill(MINUTES_DE, 3), d.fill(REPORT_DE, 3)])


@doc("t068_report_halcyon", fmt="pdf", family="dvserif", head="dvserif", lang="en")
def t068(d: Doc) -> None:
    story = Story(
        d,
        top=26,
        bottom=268,
        furniture=report_furniture(
            "Northgate Advisory · Draft report", "Draft – for discussion", ""
        ),
    )
    story.para("{halcyon:plain}", d.hs(22), after=2)
    story.para("Technical due diligence – draft findings", d.st(12, color=GREY), after=8)
    story.heading("1 Background", d.hs(12.5))
    story.para(
        [
            "The target’s products were reviewed under the {nda_en:plain} dated 27 February 2026.",
            d.fill(EN, 2),
            "The scope of {halcyon:hyphenated} was agreed with the steering committee, and the "
            "team of {halcyon:linebreak} met the management of the target twice in February.",
        ]
    )
    story.heading("2 Findings", d.hs(12.5))
    story.para(
        [
            "Integration work at the pilot sites is carried out by {quadrant:hyphenated}; no "
            "material issues were found.",
            d.fill(EN, 3),
        ]
    )
    story.para(
        [
            "One supplier invoice refers to “{halcyon:lookalike}”, another to “{halcyon:punct}”; "
            "both were confirmed to relate to the transaction.",
            d.fill(EN, 2),
        ]
    )
    story.heading("3 Next steps", d.hs(12.5))
    story.para([d.fill(EN, 3), d.fill(CONTRACT_EN, 3)])
    story.para([d.fill(CONTRACT_EN, 3), d.fill(EN, 1)])
    story.para([d.fill(CONTRACT_EN, 3)])


@doc("t069_sicherheitskonzept", family="dejavu")
def t069(d: Doc) -> None:
    story = Story(d, top=26, bottom=270)
    story.para("Sicherheitskonzept", d.hs(20), after=1)
    story.para("Arbeitsgruppe {kranich:spaced} · Gebäude B, 2. OG", d.st(11, color=GREY), after=8)
    story.heading("Zugang", d.hs(12))
    story.para(
        [
            "Der Zugang zu den Räumen der Arbeitsgruppe erfolgt ausschließlich über die "
            "{zutritt:punct}; Besucher werden an der Pforte abgeholt.",
            d.fill(TECH_DE, 1),
        ]
    )
    story.heading("Personal", d.hs(12))
    story.para(
        [
            "Alle Mitglieder der Gruppe {kranich:lookalike} haben eine {sue:plain} durchlaufen.",
            "Unterlagen zum Codename {falke:lookalike} werden im Tresor im Raum B 2.07 aufbewahrt.",
            d.fill(REPORT_DE, 2),
        ]
    )
    story.heading("Notfälle", d.hs(12))
    story.para(d.fill(TECH_DE, 2))
    stamp(
        story.s,
        140,
        story.y + 30,
        ["PROJEKT", "{kranich:case=KRANICH}"],
        color=STAMP_RED,
        angle=12,
        pt=16,
    )


# ---- documents without a term


@doc("t070_rechnung_buerobedarf", family="arial", notes="no term")
def t070(d: Doc) -> None:
    rows = [
        ["Pos.", "Art.-Nr.", "Bezeichnung", "Menge", "Einzelpreis", "Gesamt"],
        ["1", "KB-1010", "Kopierpapier A4, 80 g, Karton", "20", "24,90", "498,00"],
        ["2", "KB-2203", "Toner schwarz", "6", "89,00", "534,00"],
        ["3", "KB-3140", "Ordner breit, blau", "50", "2,49", "124,50"],
    ]
    letter(
        d,
        sender="Kolibri Bürobedarf",
        tagline="Büro · Schule · Technik",
        return_line="Kolibri Bürobedarf GmbH · Lindenstraße 4 · 88046 Friedrichshafen",
        recipient=[
            "Helvetor Maschinenbau AG",
            "Poststelle",
            "Seestraße 40",
            "",
            "88045 Friedrichshafen",
        ],
        refs=[("Rechnungsnr.", "KB-26-04471"), ("Datum", "2. März 2026")],
        subject="Rechnung KB-26-04471",
        greeting=None,
        closing=None,
        body=["Wir berechnen Ihnen für die Lieferung vom 27. Februar 2026:"],
        after=lambda st: invoice_table(
            st,
            rows,
            [
                ("Nettobetrag", "1.156,50 EUR"),
                ("USt. 19 %", "219,74 EUR"),
                ("Rechnungsbetrag", "1.376,24 EUR"),
            ],
            notes=[
                "Der Betrag wird gemäß SEPA-Lastschriftmandat KB-7781 am 16. März 2026 eingezogen."
            ],
        ),
        footer=[
            ["Kolibri Bürobedarf GmbH", "Lindenstraße 4 · 88046 Friedrichshafen"],
            ["Amtsgericht Ulm HRB 710233"],
            ["Tel. 07541 3899-0", "www.kolibri-buero.de"],
        ],
    )


@doc("t071_brief_stadtwerke", family="times", head="arial", notes="no term")
def t071(d: Doc) -> None:
    letter(
        d,
        sender="Stadtwerke Darmstadt-Ost",
        tagline="Strom · Gas · Wasser",
        return_line="Stadtwerke Darmstadt-Ost · Postfach 11 02 · 64211 Darmstadt",
        recipient=[
            "Helvetor Maschinenbau AG",
            "Entwicklungszentrum",
            "[[Kranichsteiner Straße|kranich|inside a longer word]] 71",
            "",
            "64289 Darmstadt",
        ],
        refs=[("Vertragskonto", "4471 0923 18"), ("Datum", "9. März 2026")],
        subject="Ablesung Ihrer Zähler",
        body=[
            "in der Zeit vom 23. bis 27. März lesen wir die Zähler in Ihrem Gebäude ab. Bitte "
            "sorgen Sie dafür, dass unsere Mitarbeiter Zugang zu den Zählerräumen erhalten.",
            d.fill(LETTER_DE, 2),
        ],
        signer=["Kundenservice"],
    )


@doc(
    "t072_protokoll_verein", fmt="jpg", dpi=150, family="dejavu", degrade=("jpeg",), notes="no term"
)
def t072(d: Doc) -> None:
    minutes(
        d,
        org="[[SV Falkenau|falke|inside a longer word]] 1921 e. V.",
        title="Protokoll der Mitgliederversammlung",
        meta=[
            ("Datum", "6. März 2026, 19:30 Uhr"),
            ("Ort", "Vereinsheim"),
            ("Leitung", "Bernd Hoffmeister"),
        ],
        tops=[
            ("Bericht des Vorstands", [d.fill(MINUTES_DE, 2)]),
            (
                "Vereinsheim",
                [
                    "Die Schäden am Dach des Vereinsheims durch den [[Orkan|orka|inside a longer "
                    "word]] im Februar sind von der Versicherung übernommen worden.",
                ],
            ),
            ("Kassenbericht", [d.fill(MINUTES_DE, 2)]),
            ("Verschiedenes", [d.fill(MINUTES_DE, 1)]),
        ],
    )


@doc("t073_mail_kantine", family="helv", clean=True, notes="no term")
def t073(d: Doc) -> None:
    email(
        d,
        printed_by="Kantine Friedrichshafen",
        sender="Kantine Friedrichshafen",
        fields=[
            ("Von:", "Kantine Friedrichshafen"),
            ("An:", "Alle Beschäftigten"),
            ("Betreff:", "Speiseplan KW 12"),
        ],
        body=[
            "Liebe Gäste,",
            "neu auf der Karte ist in dieser Woche unser Gemüseburger „[[Vega|vega|wrong case for "
            "a case-sensitive term]]“ mit Süßkartoffelpommes.",
            "Montag: Linsensuppe · Dienstag: Schnitzel mit Kartoffelsalat · Mittwoch: "
            "Gemüselasagne · Donnerstag: Fischfilet · Freitag: Kässpätzle",
            "Guten Appetit wünscht Ihr Kantinenteam",
        ],
    )


@doc("t074_reisebericht", family="freeserif", head="helv", pt=10, notes="no term")
def t074(d: Doc) -> None:
    newsletter(
        d,
        masthead="Reiseblatt",
        issue="Die Seite für Reiselustige · Ausgabe Frühjahr 2026",
        pages=[
            [
                [
                    [
                        ("h", "Winter in Tromsø"),
                        (
                            "p",
                            "Wer im Januar nach Nordnorwegen reist, braucht warme Kleidung und "
                            "etwas Glück: In drei von fünf Nächten zeigte sich das "
                            "[[Nordlicht|nord|half of the phrase]] über dem Fjord.",
                        ),
                    ],
                    [
                        ("h", "Zugvögel am Bodden"),
                        (
                            "p",
                            "Im Herbst rasten Zehntausende [[Kraniche|kranich|inside a longer "
                            "word]] an der Ostseeküste. Die Zugvögel lassen sich am besten in der "
                            "Dämmerung beobachten.",
                        ),
                    ],
                ],
                [
                    [("h", "Tipps"), ("p", d.fill(NEWS_DE, 2))],
                    [("h", "Leserbriefe"), ("p", d.fill(NEWS_DE, 2))],
                ],
            ]
        ],
    )


@doc("t075_stellenanzeige", family="arial", notes="no term")
def t075(d: Doc) -> None:
    story = Story(d, top=30, bottom=270)
    story.para("Wir suchen Verstärkung!", d.hs(22, color=d.accent), after=3)
    story.para("Sachbearbeiter/in Einkauf (m/w/d) in Vollzeit", d.hs(15), after=6)
    story.para(
        "Die Helvetor Maschinenbau AG entwickelt und fertigt Pressen und Umformanlagen für Kunden "
        "in aller Welt. Für unseren Standort Friedrichshafen suchen wir zum nächstmöglichen "
        "Zeitpunkt eine engagierte Persönlichkeit."
    )
    story.heading("Ihre Aufgaben", d.hs(12))
    story.bullets(
        [
            "Beschaffung von Normteilen und Verbrauchsmaterial",
            "Pflege der Lieferantenstammdaten",
            "Abstimmung mit Konstruktion und Fertigung",
        ]
    )
    story.heading("Ihr Profil", d.hs(12))
    story.bullets(
        [
            "Kaufmännische Ausbildung, gern mit technischem Verständnis",
            "Sicherer Umgang mit ERP-Systemen",
            "Gute Englischkenntnisse",
        ]
    )
    story.para("Wir freuen uns auf Ihre Bewerbung über unser Karriereportal.", before=4)


@doc("t076_datenblatt_usv", dpi=300, family="helv", notes="no term")
def t076(d: Doc) -> None:
    story = Story(d, left=20, width=170, top=26, bottom=272)
    s = story.new_page()
    story.para(
        "USV-Serie [[Sentinel X5|sentinel|one character off (the term allows no edits)]]",
        d.hs(22),
        after=2,
    )
    story.para("Unterbrechungsfreie Stromversorgung 3–10 kVA", d.st(11, color=GREY), after=8)
    story.table(
        [60, 110],
        [
            ["Merkmal", "Wert"],
            ["Nennleistung", "3 / 6 / 10 kVA"],
            ["Überbrückungszeit", "12 min bei Volllast"],
            ["Erweiterung", "Batteriemodul [[Sentinel X45|sentinel|longer model number]]"],
            ["Schnittstellen", "USB, RS-232, SNMP"],
        ],
        d.st(10.5),
    )
    story.para(d.fill(TECH_DE, 2), d.st(10))
    running_footer(s, "Technische Änderungen vorbehalten", "Ausgabe 2026")


@doc("t077_fax_maler", fmt="tif", family="arial", pt=12, degrade=("fax",), notes="no term")
def t077(d: Doc) -> None:
    fax_cover(
        d,
        tti="16.03.2026 07:48  +49 7541 55210  MALER HOLZER  S. 01",
        company="Malerbetrieb Holzer",
        company_lines=["Uferweg 12 · 88048 Friedrichshafen"],
        fields=[
            ("An:", "Helvetor Maschinenbau AG", "Von:", "Georg Holzer"),
            ("z. Hd.:", "Haustechnik", "Datum:", "16. März 2026"),
            ("Fax:", "07541 204-399", "Seiten:", "1"),
        ],
        message=[
            "Sehr geehrte Damen und Herren,",
            "gern bestätigen wir den Termin für die Malerarbeiten im Treppenhaus von Gebäude C ab "
            "dem 30. März. Die Arbeiten dauern voraussichtlich vier Tage.",
            "Mit freundlichen Grüßen",
        ],
        sign=["Georg Holzer"],
    )


@doc("t078_folie_quartal", dpi=150, family="arial", notes="no term")
def t078(d: Doc) -> None:
    slide(
        d,
        title="Ergebnis erstes Quartal 2026",
        sub="Vertrieb und Service",
        bullets=[
            "Auftragseingang 41,2 Mio. EUR (Vorjahr 38,7 Mio. EUR)",
            "Umsatz 36,9 Mio. EUR, Serviceanteil 22 %",
            "Neue Kunden in Polen und Tschechien",
            "Ausblick: stabile Nachfrage im zweiten Quartal",
        ],
        footer_left=slide_footer("Helvetor Maschinenbau AG"),
    )


@doc("t079_speiseplan", dpi=150, family="dejavu", degrade=("blur",), notes="no term")
def t079(d: Doc) -> None:
    s = d.page()
    y = form_head(s, "Speiseplan KW 13", "Kantine Friedrichshafen · Ausgabe 11:30–13:30 Uhr", GREEN)
    table(
        s,
        20,
        y + 4,
        [30, 70, 70],
        [
            ["Tag", "Menü 1", "Menü 2 (vegetarisch)"],
            ["Montag", "Rinderroulade, Rotkohl", "Gemüsecurry mit Reis"],
            ["Dienstag", "Hähnchenbrust, Pommes", "Spinatknödel"],
            ["Mittwoch", "Leberkäse, Kartoffelsalat", "Käsespätzle"],
            ["Donnerstag", "Seelachsfilet, Salzkartoffeln", "Linseneintopf"],
            ["Freitag", "Currywurst", "Pasta mit Tomatensauce"],
        ],
        d.st(10.5),
        pad=2.4,
    )


@doc("t080_umweltbericht", family="arial", notes="no term")
def t080(d: Doc) -> None:
    story = Story(d, top=26, bottom=270)
    story.para("Umweltbericht 2025", d.hs(20, color=GREEN), after=6)
    story.heading("Luftreinhaltung", d.hs(12))
    story.para(
        [
            "Die Abgaswerte der Notstromaggregate lagen bei allen Messungen im zulässigen "
            "Bereich; für [[NOX|nox|next to its not_near word]] wurden höchstens 180 mg/m³ "
            "gemessen.",
            d.fill(REPORT_DE, 1),
        ]
    )
    story.heading("Natur am Standort", d.hs(12))
    story.para(
        "Im Rahmen des Vogelschutzprogramms wurde am Rückhaltebecken ein [[Kranich|kranich|next "
        "to its not_near word]] beringt; die Fläche bleibt bis Juni ungemäht."
    )
    story.heading("Energie", d.hs(12))
    story.para(d.fill(REPORT_DE, 2))


@doc("t081_brief_zonen", family="times", head="helv", notes="no term inside its zones")
def t081(d: Doc) -> None:
    letter(
        d,
        sender="[[Quadrant Systems|quadrant|letterhead, outside the body zone]]",
        tagline="Sicherheitstechnik · Integration · Service",
        return_line="Leopoldstraße 88 · 80802 München",
        recipient=[
            "Helvetor Maschinenbau AG",
            "Werkschutz",
            "Seestraße 40",
            "",
            "88045 Friedrichshafen",
        ],
        refs=[("Datum", "23. März 2026")],
        subject="Unterlagen zur Schulung",
        body=[
            "anbei erhalten Sie die Schulungsunterlagen für Ihre Pfortenkräfte. Die Unterlagen "
            "sind [[nicht zur Weitergabe|weitergabe|body text, outside the term's zones]] an "
            "Dritte bestimmt.",
            d.fill(LETTER_DE, 2),
        ],
        signer=["Dr. Tobias Renner"],
        footer=[
            [
                "[[Quadrant Systems|quadrant|footer, outside the body zone]] GmbH",
                "Leopoldstraße 88 · 80802 München",
            ],
            ["Amtsgericht München HRB 240118"],
            ["Tel. 089 3304 11-0"],
        ],
    )


@doc("t082_aushang_apotheke", family="dejavu", notes="no term")
def t082(d: Doc) -> None:
    story = Story(d, top=30, bottom=270)
    story.para("Notdienste im April", d.hs(22, color=d.accent), after=6)
    story.para(
        "Den Notdienst am Wochenende übernimmt die [[Falke|falke|no near word within 40 "
        "characters]]-Apotheke am Marktplatz. Die Apotheke in [[Falkenberg|falke|inside a longer "
        "word]] ist wegen Umbaus bis zum 20. April geschlossen."
    )
    story.para(d.fill(NEWS_DE, 2))


@doc("t083_formular_zahlungsdaten", family="arial", notes="no term")
def t083(d: Doc) -> None:
    s = d.page()
    y = form_head(
        s, "Änderung der Zahlungsdaten", "Debitorenbuchhaltung · bitte vollständig ausfüllen", GREY
    )
    table(
        s,
        20,
        y + 6,
        [60, 110],
        [
            ["Bisherige Kundennummer", "[[KD-12345|kd|five digits]]"],
            ["Neue Kundennummer (Konzern)", "[[KD-1234567|kd|seven digits]]"],
            ["Bankverbindung", "[[AT61 1904 3002 3457 3201|iban|Austrian IBAN]]"],
            ["E-Mail für Rechnungen", "[[info@helvetor-group.com|mail|another domain]]"],
            ["Ersatzteilgruppe", "[[FS-31|fsre|two digits only]]"],
        ],
        d.st(10.5),
        head=False,
        head_fill=None,
        pad=2.4,
    )
    sign_lines(s, 190)


@doc(
    "t084_teilnehmerliste", family="dvserif", head="dvserif", notes="no term: names one letter off"
)
def t084(d: Doc) -> None:
    s = d.page()
    y = text(s, 20, 24, 170, d.units("Teilnehmerliste", d.hs(20)))
    y = text(
        s,
        20,
        y + 1,
        170,
        d.units("Seminar „Arbeitsschutz im Lager“ · 17. März 2026", d.st(10.5, color=GREY)),
    )
    names = [
        ("Petra Vogt", "Druckerei Weller"),
        ("[[Ulrike Maas|maass|one letter off (the term allows no edits)]]", "Stadtwerke Lindenau"),
        ("Lukas Behrendt", "Helvetor Maschinenbau AG"),
        (
            "[[Jürgen Weissmüler|weiss|one letter off (the term allows no edits)]]",
            "Kolibri Bürobedarf",
        ),
        ("[[Karen Kühnast|kuehnast|another first name]]", "Stadtwerke Lindenau"),
        ("Miriam Okafor", "Helvetor Maschinenbau AG"),
        ("[[Ulrike Maaßen|maass|inside a longer word]]", "Albis Facility Services"),
        ("[[Günter Weißmüller|weiss|another first name]]", "Helvetor Maschinenbau AG"),
        ("Georg Holzer", "Malerbetrieb Holzer"),
    ]
    rows = [["Nr.", "Name", "Firma", "Unterschrift"]]
    rows += [[str(i), name, firm, ""] for i, (name, firm) in enumerate(names, 1)]
    table(s, 20, y + 6, [12, 58, 58, 42], rows, d.st(10.5), pad=2.6, min_row=10)


# ---------------------------------------------------------------- main


def summarize(files: list[dict]) -> str:
    occ_terms: Counter = Counter()
    docs_per_term: dict[str, set] = defaultdict(set)
    kinds: Counter = Counter()
    mods: Counter = Counter()
    decoys: Counter = Counter()
    for f in files:
        for e in f["expect"]:
            occ_terms[e["term"]] += e["count"]
            docs_per_term[e["term"]].add(f["file"])
            for how in e["how"]:
                kind, *rest = how.split("+")
                kinds[kind] += 1
                mods.update(rest)
        for dec in f["decoys"]:
            decoys[dec["term"]] += 1
    pages = sum(f["pages"] for f in files)
    out = [
        f"{len(files)} files, {pages} pages, {sum(occ_terms.values())} occurrences, "
        f"{sum(decoys.values())} decoys, "
        f"{sum(1 for f in files if not f['expect'])} files without a term",
        "per term (occurrences / files / decoys):",
    ]
    for term in TERMS:
        out.append(
            f"  {term.name:32} {occ_terms[term.name]:4} {len(docs_per_term[term.name]):3} "
            f"{decoys[term.name]:3}"
        )
    out.append("kinds: " + ", ".join(f"{k} {kinds[k]}" for k in KINDS))
    out.append("conditions: " + ", ".join(f"{m} {mods[m]}" for m in MODS))
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path("/tmp/terms/holdout"))
    parser.add_argument(
        "--only", nargs="*", help="build only files whose name contains one of these"
    )
    parser.add_argument("--strict", action="store_true", help="fail on self-check warnings")
    parser.add_argument("--preview", type=Path, help="also write small PNGs of every page here")
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    if args.preview:
        args.preview.mkdir(parents=True, exist_ok=True)
    files, problems = [], []
    for name, kw, build in BUILDERS:
        if args.only and not any(part in name for part in args.only):
            continue
        d = Doc(name, **kw)
        build(d)
        problems += [f"{name}: {p}" for p in check_filler(d) + d.warnings]
        files.append(finish(d, out, args.preview))
    (out / "profile.toml").write_text(profile_toml(), encoding="utf-8")
    if tomllib is not None:
        loaded = tomllib.loads((out / "profile.toml").read_text(encoding="utf-8"))
        assert len(loaded["term"]) == len(TERMS)
    truth = {
        "version": 1,
        "profile": "profile.toml",
        "generator": "scripts/make_terms_holdout.py",
        "files": files,
    }
    (out / "ground_truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(summarize(files))
    for problem in problems:
        print("WARNING", problem)
    return 1 if problems and args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
