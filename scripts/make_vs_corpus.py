#!/usr/bin/env python3
"""Generate a ground-truth corpus for the classification-marking detector.

A Verschlusssache carries its level at the top and bottom of every page, a NATO
paper in the same places, a CERT advisory has a TLP label in a corner and a
board paper a red "STRENG VERTRAULICH" stamp. And then there are the pages that
only *talk* about markings: the VSA handout, the brochure "zugelassen für
VS-NfD", the party invitation that is "streng geheim". A detector only ever
tried on the first kind flags the second, so this builds both, seeded, and every
file comes with the level, label, TLP and company marking it carries, page by
page. Precision and recall can then be measured instead of guessed.

    python scripts/make_vs_corpus.py --out /tmp/vs/corpus
    python scripts/evaluate_vs.py /tmp/vs/corpus

The ground truth is ``ground_truth.json`` next to the files; ``Corpus.truth``
documents every field. Needs Pillow and numpy (test-time only). The paper
ageing, skew, JPEG cycles and the image-only PDF writer are the ones
``make_corpus.py`` uses, so both corpora degrade the same way.
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import random
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from make_corpus import (
    _pdf_from_objects,
    age,
    bleed_through,
    form_page,
    image_only_pdf,
    jpeg_cycles,
    receipt_page,
    skew,
)

# ---------------------------------------------------------------- fonts

FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/freefont",
)

#: Liberation is metric-compatible with Arial, Times and Courier, which is what
#: offices actually print with; DejaVu is the fallback that is always there.
FONTS: dict[str, tuple[str, ...]] = {
    "sans": ("DejaVuSans.ttf", "LiberationSans-Regular.ttf", "FreeSans.ttf"),
    "sans-bold": ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "FreeSansBold.ttf"),
    "serif": ("DejaVuSerif.ttf", "LiberationSerif-Regular.ttf", "FreeSerif.ttf"),
    "serif-bold": ("DejaVuSerif-Bold.ttf", "LiberationSerif-Bold.ttf", "FreeSerifBold.ttf"),
    "arial": ("LiberationSans-Regular.ttf", "FreeSans.ttf", "DejaVuSans.ttf"),
    "arial-bold": ("LiberationSans-Bold.ttf", "FreeSansBold.ttf", "DejaVuSans-Bold.ttf"),
    "times": ("LiberationSerif-Regular.ttf", "FreeSerif.ttf", "DejaVuSerif.ttf"),
    "times-bold": ("LiberationSerif-Bold.ttf", "FreeSerifBold.ttf", "DejaVuSerif-Bold.ttf"),
    "mono": ("LiberationMono-Regular.ttf", "FreeMono.ttf", "DejaVuSansMono.ttf"),
    "mono-bold": ("LiberationMono-Bold.ttf", "FreeMonoBold.ttf", "DejaVuSansMono-Bold.ttf"),
}

#: Body text families: (regular, bold).
FAMILIES = {
    "times": ("times", "times-bold"),
    "arial": ("arial", "arial-bold"),
    "sans": ("sans", "sans-bold"),
    "serif": ("serif", "serif-bold"),
    "mono": ("mono", "mono-bold"),
}


@functools.cache
def font_file(kind: str) -> str:
    for name in FONTS[kind]:
        for directory in FONT_DIRS:
            candidate = Path(directory) / name
            if candidate.exists():
                return str(candidate)
    raise SystemExit(f"no font found for {kind}; install fonts-dejavu and fonts-liberation")


@functools.lru_cache(maxsize=512)
def font(kind: str, px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_file(kind), px)


def pt_to_px(pt: float, dpi: int) -> int:
    return max(6, round(pt * dpi / 72))


# ---------------------------------------------------------------- what a marking means

INK = (22, 22, 22)
RED = (196, 28, 36)
BLUE = (28, 62, 168)
BLACK = (28, 28, 28)
GREY = (150, 150, 150)
PALE = (178, 178, 178)
PALE_RED = (232, 150, 150)
#: TLP 2.0 colours as FIRST specifies them: coloured text on a black field.
TLP_INK = {
    "RED": (255, 43, 43),
    "AMBER": (255, 192, 0),
    "GREEN": (51, 255, 0),
    "CLEAR": (255, 255, 255),
}
A4 = (8.27, 11.69)


@dataclass(frozen=True)
class Kind:
    """What a drawn marking stands for: its scheme and government level."""

    scheme: str
    level: int = 0
    label: str | None = None
    tlp: str | None = None
    company: str | None = None


def _government(entries: list[tuple[str, str, int]]) -> dict[str, Kind]:
    return {label: Kind(scheme, level, label) for label, scheme, level in entries}


#: Every marking the corpus draws, keyed by the canonical label the detector is
#: expected to report. TLP and company markings have no government level.
KINDS: dict[str, Kind] = {
    **_government(
        [
            ("VS-NfD", "de", 1),
            ("VS-VERTRAULICH", "de", 2),
            ("GEHEIM", "de", 3),
            ("STRENG GEHEIM", "de", 4),
            # VSA 2023 Anlage IV prints it beside VS-VERTRAULICH and above, so
            # alone it says "at least VS-VERTRAULICH" and names no grade.
            ("VS (amtlich geheimgehalten)", "de", 2),
            ("AT EINGESCHRÄNKT", "at", 1),
            ("AT VERTRAULICH", "at", 2),
            ("AT GEHEIM", "at", 3),
            ("AT STRENG GEHEIM", "at", 4),
            ("CH INTERN", "ch", 1),
            ("CH VERTRAULICH", "ch", 2),
            ("CH GEHEIM", "ch", 3),
            # NATO UNCLASSIFIED is a marking but not a classification.
            ("NATO UNCLASSIFIED", "nato", 0),
            ("NATO RESTRICTED", "nato", 1),
            ("NATO CONFIDENTIAL", "nato", 2),
            ("NATO SECRET", "nato", 3),
            ("COSMIC TOP SECRET", "nato", 4),
            ("RESTREINT UE", "eu", 1),
            ("CONFIDENTIEL UE", "eu", 2),
            ("SECRET UE", "eu", 3),
            ("TRÈS SECRET UE", "eu", 4),
            ("US CUI", "us", 1),
            ("US CONFIDENTIAL", "us", 2),
            ("US SECRET", "us", 3),
            ("US TOP SECRET", "us", 4),
            ("UK OFFICIAL-SENSITIVE", "uk", 1),
            ("FR DIFFUSION RESTREINTE", "fr", 1),
        ]
    ),
    **{f"TLP:{name}": Kind("tlp", tlp=name) for name in ("RED", "AMBER", "AMBER+STRICT", "GREEN")},
    "TLP:CLEAR": Kind("tlp", tlp="CLEAR"),
    # TLP 1.0 WHITE became CLEAR in 2.0; the detector should report the new name.
    "TLP:WHITE": Kind("tlp", tlp="CLEAR"),
    **{
        f"company:{name}": Kind("company", company=name)
        for name in (
            "STRENG VERTRAULICH",
            "VERTRAULICH",
            "INTERN",
            "CONFIDENTIAL",
            "INTERNAL",
            "GESCHÄFTSGEHEIMNIS",
        )
    },
}

#: A marking term used as a word of its own in running text — what the detector
#: should report as a *mention*, never as a marking. Compounds do not count:
#: "Betriebsgeheimnis", "Geheimzahl", "Internet" and "Geheimrat" are traps, not
#: mentions. "NfD" is matched in its own spelling only, so the company "NFD
#: Logistik" stays a trap too.
MENTION = re.compile(
    r"(?<![\w-])(?:"
    r"VS[\s–-]*(?:NfD|nfD|NUR|Nur|VERTRAULICH|Vertraulich)"
    r"|NfD|nfD|TLP|CUI"
    r"|(?i:verschlu(?:ss|ß)sachen?"
    r"|dienstgebrauch(?:s|es)?"
    r"|streng\s+geheim|geheim(?:e|en|er|es)?"
    r"|vertraulich(?:e|en|er|es)?"
    r"|intern(?:e|en|er|es)?"
    r"|eingeschränkt(?:e|en|er|es)?"
    r"|restricted|confidential|secret|internal|unclassified|official-sensitive"
    r"|restreint|restreinte|confidentiel|cosmic)"
    r")(?![\w])"
)


# ---------------------------------------------------------------- page model


@dataclass
class Mark:
    """One marking drawn on the page: a header/footer line or a stamp.

    `kind` is a key into KINDS, or None for stamps that are not a
    classification at all (ENTWURF, KOPIE, an Eingangsstempel).
    """

    text: str
    kind: str | None
    where: str = "both"  # both | header | footer | stamp
    align: str = "center"  # left | center | right (header and footer lines)
    pt: float = 10.0
    face: str = "sans-bold"
    color: tuple[int, int, int] = BLACK
    box: bool = False
    tracking: float = 0.0  # extra space between letters, in em
    angle: float = 0.0
    #: Stamp centre as a fraction of the page. The default is the top-right
    #: margin: above the letterhead and clear of a centred header line.
    at: tuple[float, float] = (0.8, 0.05)
    double: bool = False
    opacity: float = 0.92
    background: tuple[int, int, int] | None = None
    on: str | tuple[int, ...] = "all"  # all | first | last | explicit page numbers
    strike: bool = False

    def applies(self, number: int, count: int) -> bool:
        if self.on == "all":
            return True
        if self.on == "first":
            return number == 1
        if self.on == "last":
            return number == count
        return number in self.on


def both(text: str, kind: str | None, **kwargs) -> Mark:
    return Mark(text, kind, where="both", **kwargs)


def header(text: str, kind: str | None, **kwargs) -> Mark:
    return Mark(text, kind, where="header", **kwargs)


def footer(text: str, kind: str | None, **kwargs) -> Mark:
    return Mark(text, kind, where="footer", **kwargs)


def stamp(text: str, kind: str | None, **kwargs) -> Mark:
    kwargs.setdefault("color", RED)
    kwargs.setdefault("pt", 20)
    kwargs.setdefault("box", True)
    return Mark(text, kind, where="stamp", **kwargs)


def tlp_label(name: str, **kwargs) -> Mark:
    """A TLP label the way FIRST prints it: coloured on black, top right."""
    shown = name.split(":", 1)[1].strip()
    colour = TLP_INK["CLEAR" if shown == "WHITE" else shown.split("+")[0]]
    kwargs.setdefault("align", "right")
    kwargs.setdefault("pt", 11)
    return Mark(name, f"TLP:{shown}", color=colour, background=(0, 0, 0), **kwargs)


@dataclass
class Content:
    """What a document says, before any marking is put on it."""

    pages: list[list[tuple[str, object]]]
    head_left: list[tuple[str, str]] = field(default_factory=list)
    head_right: list[tuple[str, str]] = field(default_factory=list)
    foot: list[str] = field(default_factory=list)
    size: tuple[float, float] = A4
    top: float = 0.95
    bottom: float = 1.05
    left: float = 1.0
    right: float = 0.85


@dataclass
class Page:
    """One finished page and what is on it."""

    image: Image.Image | None
    dpi: int
    marks: list[Mark]  # classification marks only
    text: list[str]  # everything else drawn: letterhead, body, numbers, other stamps


# ---------------------------------------------------------------- drawing


def text_width(face: ImageFont.FreeTypeFont, text: str, tracking: float = 0.0) -> int:
    if not tracking:
        return int(face.getlength(text))
    extra = tracking * face.size * (len(text) - 1)
    return int(sum(face.getlength(ch) for ch in text) + extra)


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    face: ImageFont.FreeTypeFont,
    fill: object,
    tracking: float = 0.0,
) -> None:
    """Draws `text`, letter-spaced when `tracking` is set — the way a word
    processor spaces a heading out, not with typed blanks."""
    if not tracking:
        draw.text(xy, text, font=face, fill=fill)
        return
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=face, fill=fill)
        x += face.getlength(ch) + tracking * face.size


def wrap(text: str, face: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and face.getlength(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


class Sheet:
    """A blank page at a given resolution."""

    def __init__(self, dpi: int, size_in: tuple[float, float] = A4) -> None:
        self.dpi = dpi
        self.w = int(size_in[0] * dpi)
        self.h = int(size_in[1] * dpi)
        self.image = Image.new("RGB", (self.w, self.h), (255, 255, 255))
        self.draw = ImageDraw.Draw(self.image)

    def inch(self, value: float) -> int:
        return int(value * self.dpi)

    def font(self, kind: str, pt: float) -> ImageFont.FreeTypeFont:
        return font(kind, pt_to_px(pt, self.dpi))

    def multiply(self, coverage: np.ndarray, x0: int, y0: int, color: tuple[int, int, int]) -> None:
        """Lays ink over the page like a rubber stamp: printed text under it
        stays dark, white paper takes the stamp's colour."""
        array = np.asarray(self.image, dtype=np.float32).copy()
        h, w = coverage.shape
        region = array[y0 : y0 + h, x0 : x0 + w]
        tint = 1.0 - np.asarray(color, dtype=np.float32) / 255.0
        region *= 1.0 - coverage[: region.shape[0], : region.shape[1], None] * tint
        self.image = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), "RGB")
        self.draw = ImageDraw.Draw(self.image)


#: Body styles: (bold?, size factor, space after in lines).
STYLES = {
    "org": (True, 1.2, 0.0),
    "org2": (True, 1.0, 0.0),
    "l": (False, 1.0, 0.0),
    "s": (False, 0.8, 0.0),
    "r": (False, 1.0, 0.0),
    "c": (False, 1.0, 0.0),
    "title": (True, 1.35, 0.5),
    "h": (True, 1.0, 0.25),
    "p": (False, 1.0, 0.5),
    "b": (False, 1.0, 0.2),
    "big": (True, 2.2, 0.4),
    "rowh": (True, 0.92, 0.0),
    "row": (False, 0.92, 0.0),
}


def layout(
    sheet: Sheet, content: Content, blocks: list, *, first: bool, family: str, pt: float
) -> list[str]:
    """Sets letterhead and body on `sheet` and returns every line it drew.

    Text that runs past the bottom margin is dropped, not squeezed in, and the
    ground truth only ever sees what was actually drawn.
    """
    regular, bold = FAMILIES[family]
    left = sheet.inch(content.left)
    right = sheet.w - sheet.inch(content.right)
    width = right - left
    bottom = sheet.h - sheet.inch(content.bottom)
    drawn: list[str] = []

    def face(style: str) -> ImageFont.FreeTypeFont:
        is_bold, factor, _ = STYLES[style]
        return sheet.font(bold if is_bold else regular, pt * factor)

    def put(style: str, text: str, x: float, y: float) -> None:
        sheet.draw.text((x, y), text, font=face(style), fill=INK)
        drawn.append(text)

    y = sheet.inch(content.top)
    if first and (content.head_left or content.head_right):
        yl = yr = y
        for style, text in content.head_left:
            put(style, text, left, yl)
            yl += int(face(style).size * 1.35)
        for style, text in content.head_right:
            put(style, text, right - face(style).getlength(text), yr)
            yr += int(face(style).size * 1.35)
        y = max(yl, yr) + int(sheet.font(regular, pt).size * 1.2)

    for style, value in blocks:
        if style == "gap":
            y += int(sheet.font(regular, pt).size * float(value))
            continue
        f = face(style)
        step = int(f.size * 1.42)
        if style in ("row", "rowh"):
            if y + step > bottom:
                break
            cells = list(value)
            weights = [2.2] + [1.0] * (len(cells) - 1) if len(cells) > 2 else [1.0, 1.25]
            x = float(left)
            for cell, weight in zip(cells, weights):
                put(style, str(cell), x + 4, y)
                x += width * weight / sum(weights)
            if style == "rowh":
                sheet.draw.line([left, y + step - 4, right, y + step - 4], fill=INK, width=2)
            y += step
            continue
        if style in ("p", "b"):
            indent = int(f.size * 1.2) if style == "b" else 0
            lines = wrap(str(value), f, width - indent)
            for index, line in enumerate(lines):
                if y + step > bottom:
                    return drawn
                if style == "b" and index == 0:
                    sheet.draw.text((left, y), "–", font=f, fill=INK)
                put(style, line, left + indent, y)
                y += step
        else:
            if y + step > bottom:
                break
            text = str(value)
            if style == "r":
                x = right - f.getlength(text)
            elif style in ("c", "big") or (style == "title" and content.size != A4):
                x = left + (width - f.getlength(text)) / 2
            else:
                x = left
            put(style, text, x, y)
            y += step
        y += int(f.size * STYLES[style][2])

    if content.foot:
        small = sheet.font(regular, pt * 0.72)
        fy = sheet.h - sheet.inch(0.98)
        sheet.draw.line([left, fy - 8, right, fy - 8], fill=(120, 120, 120), width=1)
        for line in content.foot:
            sheet.draw.text(
                (left + (width - small.getlength(line)) / 2, fy), line, font=small, fill=INK
            )
            drawn.append(line)
            fy += int(small.size * 1.3)
    return drawn


def draw_banner(sheet: Sheet, mark: Mark, *, top: bool, content: Content) -> None:
    """A header or footer marking line (or two), aligned to the text margins."""
    face = sheet.font(mark.face, mark.pt)
    lines = mark.text.split("\n")
    step = int(face.size * 1.2)
    widths = [text_width(face, line, mark.tracking) for line in lines]
    block_h = step * (len(lines) - 1) + face.size
    y = sheet.inch(0.34) if top else sheet.h - sheet.inch(0.42) - block_h
    left, right = sheet.inch(content.left), sheet.w - sheet.inch(content.right)
    xs = []
    for width in widths:
        if mark.align == "left":
            xs.append(left)
        elif mark.align == "right":
            xs.append(right - width)
        else:
            xs.append((sheet.w - width) // 2)
    cap_top = face.getbbox("ÄH")[1]
    pad = max(4, int(face.size * 0.32))
    frame = [
        min(xs) - pad,
        y + cap_top - pad,
        max(x + w for x, w in zip(xs, widths)) + pad,
        y + block_h + pad // 2,
    ]
    if mark.background:
        sheet.draw.rectangle(frame, fill=mark.background)
    if mark.box:
        sheet.draw.rectangle(frame, outline=mark.color, width=max(2, face.size // 12))
    for index, (line, x) in enumerate(zip(lines, xs)):
        ly = y + index * step
        draw_text(sheet.draw, (x, ly), line, face, mark.color, mark.tracking)
        if mark.strike:
            mid = ly + int(face.size * 0.6)
            sheet.draw.line(
                [x - 6, mid, x + widths[index] + 6, mid],
                fill=mark.color,
                width=max(2, face.size // 10),
            )


def draw_stamp(sheet: Sheet, mark: Mark, rng: random.Random) -> None:
    """A rubber stamp: boxed or not, rotated, with uneven ink and a few voids."""
    face = sheet.font(mark.face, mark.pt)
    lines = mark.text.split("\n")
    step = int(face.size * 1.18)
    widths = [text_width(face, line, mark.tracking) for line in lines]
    pad = int(face.size * 0.42)
    border = max(2, face.size // 9) if mark.box else 0
    w = max(widths) + 2 * (pad + border)
    h = step * len(lines) + 2 * (pad + border) - int(face.size * 0.1)
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    if mark.box:
        draw.rectangle([0, 0, w - 1, h - 1], outline=255, width=border)
        if mark.double:
            inset = border * 2 + 3
            draw.rectangle(
                [inset, inset, w - 1 - inset, h - 1 - inset], outline=255, width=max(1, border // 2)
            )
    lift = face.getbbox("H")[1] // 2
    for index, line in enumerate(lines):
        x = (w - widths[index]) / 2
        draw_text(draw, (x, pad + border + index * step - lift), line, face, 255, mark.tracking)
    if mark.angle:
        mask = mask.rotate(mark.angle, expand=True, resample=Image.BICUBIC)

    coverage = np.asarray(mask, dtype=np.float32) / 255.0
    noise = np.random.default_rng(rng.randrange(1 << 30))
    # Uneven pressure over the stamp face, then pinholes where no ink landed.
    coarse = noise.uniform(
        0.7, 1.0, (max(2, coverage.shape[0] // 28), max(2, coverage.shape[1] // 28))
    )
    pressure = Image.fromarray((coarse * 255).astype(np.uint8)).resize(
        (coverage.shape[1], coverage.shape[0]), Image.BILINEAR
    )
    coverage *= np.asarray(pressure, dtype=np.float32) / 255.0
    coverage *= noise.random(coverage.shape) > 0.05
    coverage *= mark.opacity

    cx, cy = int(mark.at[0] * sheet.w), int(mark.at[1] * sheet.h)
    x0 = min(max(0, cx - coverage.shape[1] // 2), max(0, sheet.w - coverage.shape[1]))
    y0 = min(max(0, cy - coverage.shape[0] // 2), max(0, sheet.h - coverage.shape[0]))
    sheet.multiply(coverage, x0, y0, mark.color)


# ---------------------------------------------------------------- degradations


def fax_scan(image: Image.Image, rng: random.Random, *, dpi: int, noise: float) -> Image.Image:
    """A 100 dpi fax: grey, thresholded, then salt and pepper over `noise` of
    the pixels, and a few transmission streaks. Genuinely bilevel (mode 1)."""
    scale = 100 / dpi
    small = image.convert("L").resize(
        (round(image.width * scale), round(image.height * scale)), Image.LANCZOS
    )
    grey = np.asarray(small.filter(ImageFilter.GaussianBlur(0.35)), dtype=np.float32)
    random_state = np.random.default_rng(rng.randrange(1 << 30))
    grey = grey + random_state.normal(0, 10, grey.shape)
    ink = grey < 150
    flip = random_state.random(ink.shape) < noise
    ink[flip] = random_state.random(int(flip.sum())) < 0.5
    for _ in range(rng.randint(1, 3)):
        y = rng.randrange(ink.shape[0])
        ink[y : y + rng.randint(1, 2), :] |= random_state.random((1, ink.shape[1])) < 0.35
    return Image.fromarray(np.where(ink, 0, 255).astype(np.uint8), "L").convert(
        "1", dither=Image.Dither.NONE
    )


def fade(image: Image.Image, amount: float) -> Image.Image:
    """Low toner: everything lighter, nothing lost outright."""
    array = np.asarray(image.convert("RGB"), dtype=np.float32)
    return Image.fromarray(np.clip(255 - (255 - array) * amount, 0, 255).astype(np.uint8), "RGB")


# ---------------------------------------------------------------- born-digital PDF


def pdf_literal(text: str) -> str:
    """A PDF string literal in WinAnsiEncoding, so – „ “ ß Ü È survive."""
    out = []
    for ch in text:
        if ch in "()\\":
            out.append("\\" + ch)
            continue
        try:
            code = ch.encode("cp1252")[0]
        except UnicodeEncodeError:
            code = ord("?")
        out.append(chr(code) if code < 0x80 else f"\\{code:03o}")
    return "".join(out)


def pdf_width(text: str, size: float, bold: bool = False, tracking: float = 0.0) -> float:
    """Width in points. Liberation Sans has Helvetica's metrics, so the layout
    measured here is the layout the PDF renderer draws."""
    face = font("arial-bold" if bold else "arial", 200)
    return face.getlength(text) * size / 200 + tracking * max(0, len(text) - 1)


class TextPdf:
    """A born-digital PDF: real text in the standard fonts, no images at all."""

    def __init__(self, width: float = 595.0, height: float = 842.0) -> None:
        self.width, self.height = width, height
        self.streams: list[list[str]] = []

    def new_page(self) -> None:
        self.streams.append([])

    def op(self, operator: str) -> None:
        self.streams[-1].append(operator)

    def text(
        self,
        x: float,
        y: float,
        text: str,
        *,
        bold: bool = False,
        size: float = 10.5,
        color: tuple[int, int, int] = (0, 0, 0),
        tracking: float = 0.0,
    ) -> None:
        r, g, b = (c / 255 for c in color)
        self.op(
            f"BT /{'F2' if bold else 'F1'} {size:.2f} Tf {r:.3f} {g:.3f} {b:.3f} rg "
            f"{tracking:.2f} Tc 1 0 0 1 {x:.2f} {y:.2f} Tm ({pdf_literal(text)}) Tj ET"
        )

    def rect(
        self, x: float, y: float, w: float, h: float, *, color: tuple, width: float = 0, fill=False
    ) -> None:
        r, g, b = (c / 255 for c in color)
        if fill:
            self.op(f"{r:.3f} {g:.3f} {b:.3f} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")
        else:
            self.op(
                f"{r:.3f} {g:.3f} {b:.3f} RG {width:.2f} w {x:.2f} {y:.2f} {w:.2f} {h:.2f} re S"
            )

    def rotated(self, cx: float, cy: float, degrees: float) -> None:
        c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
        self.op(f"q {c:.5f} {s:.5f} {-s:.5f} {c:.5f} {cx:.2f} {cy:.2f} cm")

    def restore(self) -> None:
        self.op("Q")

    def build(self) -> bytes:
        objects: list[bytes] = [
            b"",
            b"",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>",
        ]
        page_ids = []
        for stream in self.streams:
            body = "\n".join(stream).encode("latin-1")
            objects.append(f"<< /Length {len(body)} >>\nstream\n".encode() + body + b"\nendstream")
            objects.append(
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.width} {self.height}] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                f"/Contents {len(objects)} 0 R >>".encode()
            )
            page_ids.append(len(objects))
        kids = " ".join(f"{pid} 0 R" for pid in page_ids)
        objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
        objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
        return _pdf_from_objects(objects)


#: Point sizes of the body styles in a born-digital PDF.
PDF_SIZES = {"org": 12, "org2": 10.5, "s": 8.5, "title": 14, "big": 22, "rowh": 9.5, "row": 9.5}
PDF_LEFT, PDF_RIGHT = 62.0, 595.0 - 52.0


def pdf_body(pdf: TextPdf, content: Content, blocks: list, *, first: bool) -> list[str]:
    """Sets letterhead and body in real text; returns every line it wrote."""
    drawn: list[str] = []

    def put(x: float, y: float, text: str, style: str) -> None:
        pdf.text(x, y, text, bold=STYLES[style][0], size=PDF_SIZES.get(style, 10.5))
        drawn.append(text)

    y = pdf.height - 72.0
    if first:
        yl = yr = y
        for style, text in content.head_left:
            put(PDF_LEFT, yl, text, style)
            yl -= PDF_SIZES.get(style, 10.5) * 1.35
        for style, text in content.head_right:
            size = PDF_SIZES.get(style, 10.5)
            put(PDF_RIGHT - pdf_width(text, size), yr, text, style)
            yr -= size * 1.35
        y = min(yl, yr) - 14
    for style, value in blocks:
        if style == "gap":
            y -= 10.5 * float(value)
            continue
        size = PDF_SIZES.get(style, 10.5)
        bold = STYLES[style][0]
        if y < 80:
            break
        if style in ("row", "rowh"):
            for column, cell in enumerate(value):
                put(PDF_LEFT + column * (PDF_RIGHT - PDF_LEFT) / len(value), y, str(cell), style)
            y -= size * 1.45
            continue
        if style in ("p", "b"):
            indent = 12 if style == "b" else 0
            lines, current = [], ""
            for word in str(value).split():
                candidate = f"{current} {word}".strip()
                if current and pdf_width(candidate, size) > PDF_RIGHT - PDF_LEFT - indent:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            lines.append(current)
            for text in lines:
                if y < 80:
                    break
                put(PDF_LEFT + indent, y, text, style)
                y -= size * 1.42
            y -= size * STYLES[style][2]
            continue
        text = str(value)
        if style == "r":
            x = PDF_RIGHT - pdf_width(text, size, bold)
        elif style in ("c", "big"):
            x = (pdf.width - pdf_width(text, size, bold)) / 2
        else:
            x = PDF_LEFT
        put(x, y, text, style)
        y -= size * 1.42 + size * STYLES[style][2]
    return drawn


def pdf_mark(pdf: TextPdf, mark: Mark) -> None:
    """A marking in real text: header and footer lines, or a rotated stamp."""
    size, bold = mark.pt, "bold" in mark.face
    if mark.where == "stamp":
        rows = mark.text.split("\n")
        width = max(pdf_width(row, size, bold, size * mark.tracking) for row in rows)
        height = size * 1.2 * len(rows) + size * 0.5
        pdf.rotated(mark.at[0] * pdf.width, (1 - mark.at[1]) * pdf.height, mark.angle)
        if mark.box:
            pdf.rect(-width / 2 - 8, -height / 2, width + 16, height, color=mark.color, width=2.2)
        for index, row in enumerate(rows):
            x = -pdf_width(row, size, bold, size * mark.tracking) / 2
            y = height / 2 - size * 1.2 * (index + 1)
            pdf.text(x, y, row, bold=bold, size=size, color=mark.color)
        pdf.restore()
        return
    width = pdf_width(mark.text, size, bold, size * mark.tracking)
    x = {"left": PDF_LEFT, "right": PDF_RIGHT - width}.get(mark.align, (pdf.width - width) / 2)
    for where, y in (("header", pdf.height - 34 - size), ("footer", 26.0)):
        if mark.where not in (where, "both"):
            continue
        if mark.background:
            pdf.rect(x - 4, y - 3, width + 8, size + 5, color=mark.background, fill=True)
        if mark.box:
            pdf.rect(x - 4, y - 4, width + 8, size + 7, color=mark.color, width=1.2)
        pdf.text(
            x, y, mark.text, bold=bold, size=size, color=mark.color, tracking=size * mark.tracking
        )


def text_pdf(
    content: Content, marks: list[Mark], *, numbering: str | None = None
) -> tuple[bytes, list[Page]]:
    """Lays `content` out as a born-digital PDF; returns its bytes and pages."""
    pdf = TextPdf()
    count = len(content.pages)
    pages: list[Page] = []
    for index, blocks in enumerate(content.pages):
        pdf.new_page()
        number = index + 1
        drawn = pdf_body(pdf, content, blocks, first=index == 0)
        if numbering:
            label = f"Seite {number} von {count}"
            pdf.text(PDF_RIGHT - pdf_width(label, 9), 50, label, size=9)
            drawn.append(label)
        placed = [mark for mark in marks if mark.applies(number, count)]
        for mark in placed:
            pdf_mark(pdf, mark)
        drawn += [mark.text for mark in placed if not mark.kind]
        pages.append(Page(None, 72, [mark for mark in placed if mark.kind], drawn))
    return pdf.build(), pages


# ---------------------------------------------------------------- body text

DATES = [
    "3. Februar 2026",
    "17. Februar 2026",
    "4. März 2026",
    "12. März 2026",
    "26. März 2026",
    "9. April 2026",
    "21. April 2026",
    "6. Mai 2026",
]
NAMES = ["Krüger", "Albers", "Brandt", "Weber", "Schuster", "Yilmaz", "Hoffmann", "Lange", "Nowak"]

DE_ORGS = [
    (
        "Bundesministerium für Beispiele",
        "Referat Z 12 – Organisation",
        "Musterstraße 12, 10115 Berlin",
    ),
    ("Bundesamt für Musterwesen", "Abteilung IT 3", "Postfach 20 03 63, 53133 Bonn"),
    ("Kommando Musterkräfte", "Abteilung Planung G3", "Kaserne Am Musterberg, 56070 Koblenz"),
    ("Wehrtechnische Dienststelle 99", "Fachgebiet 330 – Funk", "Am Prüfgelände 4, 49716 Meppen"),
    ("Bundespolizeidirektion Musterstadt", "Stabsbereich 1 – Einsatz", "Grenzweg 7, 01099 Dresden"),
    ("Landesamt für Musterschutz", "Dezernat 45", "Behördenring 3, 30159 Hannover"),
]

DE_RECIPIENTS = [
    ["Bundesamt für Ausrüstung (Musteramt)", "Referat K 4", "Beispielallee 1", "56073 Koblenz"],
    ["Bundesministerium der Beispiele", "Referat II 3", "Am Probeplatz 2", "53123 Bonn"],
    ["Landeskommando Musterland", "Dezernat Planung", "Kasernenstraße 9", "24106 Kiel"],
    ["Zentrale Musterstelle des Bundes", "Sachgebiet 21", "Postfach 11 22", "65189 Wiesbaden"],
]

DE_TOPICS: dict[str, tuple[str, list[str]]] = {
    "beschaffung": (
        "Beschaffung von Handfunkgeräten, Lose 1 und 2",
        [
            "Mit Schreiben vom 14. Februar 2026 hat die Bedarfsträgerin die Beschaffung von 240 "
            "Handfunkgeräten der neuen Generation beantragt. Die Haushaltsmittel sind im Kapitel "
            "1405 Titel 554 01 veranschlagt.",
            "Die Markterkundung hat ergeben, dass derzeit drei Anbieter die geforderten "
            "Leistungsmerkmale erfüllen. Eine Vergabe im Verhandlungsverfahren mit "
            "Teilnahmewettbewerb wird empfohlen.",
            "Die Auslieferung soll in zwei Losen erfolgen: Los 1 bis zum 30. September 2026, Los 2 "
            "bis zum 31. März 2027. Verzögerungen sind der Projektleitung unverzüglich anzuzeigen.",
            "Für die Erprobung stellt die Wehrtechnische Dienststelle zwei Prüfplätze zur "
            "Verfügung. Die Prüfberichte werden der Abnahmekommission spätestens eine Woche vor "
            "dem Abnahmetermin übersandt.",
            "Die Kosten für Schulung und Ersatzteilpaket belaufen sich nach aktueller Schätzung "
            "auf 1,84 Mio. Euro. Eine Deckung aus dem laufenden Haushalt ist gegeben.",
            "Die Geräte müssen mit der vorhandenen Fahrzeugfunkausstattung zusammenwirken. Die "
            "Schnittstellenbeschreibung ist den Vergabeunterlagen als Anlage 3 beigefügt.",
            "Wir bitten um Mitzeichnung bis zum 27. März 2026. Rückfragen richten Sie bitte an "
            "Herrn Oberregierungsrat Krüger.",
        ],
    ),
    "it": (
        "IT-Sicherheitsvorfall im Netzsegment B",
        [
            "Am 3. März 2026 wurde im Netzsegment B der Liegenschaft Nord ein ungewöhnlich hoher "
            "ausgehender Datenverkehr festgestellt. Das betroffene System wurde vom Netz getrennt "
            "und forensisch gesichert.",
            "Die Auswertung der Protokolldaten ergab, dass ein veraltetes Fernwartungsmodul als "
            "Einfallstor genutzt wurde. Das Modul ist auf insgesamt 17 Arbeitsplätzen installiert.",
            "Als Sofortmaßnahme wurden die Zugangsdaten aller Administratorkonten zurückgesetzt "
            "und die Firewallregeln verschärft. Eine Wiederinbetriebnahme erfolgt erst nach "
            "Freigabe durch das IT-Sicherheitsmanagement.",
            "Das Kryptogerät der Außenstelle ist bis zum Austausch außer Betrieb zu nehmen. Die "
            "Schlüsselmittel sind gemäß Schlüsselnachweis an die Materialverwaltung zurückzugeben.",
            "Für die Härtung der Clients wird die Umstellung auf die aktuelle Basiskonfiguration "
            "bis Ende des zweiten Quartals vorgeschlagen. Der Aufwand wird auf 40 Personentage "
            "geschätzt.",
            "Die Nutzerinnen und Nutzer werden in einer Informationsveranstaltung am 9. April 2026 "
            "über die neuen Anmeldeverfahren unterrichtet.",
        ],
    ),
    "liegenschaft": (
        "Erneuerung der Zutrittskontrolle, Liegenschaft Nord",
        [
            "Die Zutrittskontrollanlage am Tor 2 entspricht nicht mehr dem Stand der Technik. "
            "Ausweise älterer Bauart lassen sich mit geringem Aufwand kopieren.",
            "Es wird vorgeschlagen, die Leseeinheiten gegen Geräte mit verschlüsselter "
            "Kartenkommunikation auszutauschen und die Besucherregelung anzupassen.",
            "Der Bauabschnitt III des Stabsgebäudes wird voraussichtlich im Oktober 2026 "
            "übergeben. Bis dahin bleibt der Ausweichstandort in Halle 14 in Betrieb.",
            "Die Wachanweisung ist entsprechend zu ändern. Das Wachpersonal ist vor "
            "Inbetriebnahme einzuweisen; die Einweisung ist schriftlich zu dokumentieren.",
            "Für die Umrüstung werden Kosten in Höhe von 312.000 Euro veranschlagt. Die Maßnahme "
            "ist mit der Bauverwaltung des Landes abgestimmt.",
            "Die Videoüberwachung des Außenzauns wird im selben Zug erneuert. Die Aufzeichnungen "
            "werden nach 72 Stunden automatisch gelöscht.",
        ],
    ),
    "uebung": (
        "Stabsrahmenübung FESTE BRÜCKE 2026",
        [
            "Die Stabsrahmenübung FESTE BRÜCKE 2026 findet vom 11. bis 15. Mai 2026 am Standort "
            "Musterhausen statt. Übungsziel ist die Führungsfähigkeit des Gefechtsstandes bei "
            "Ausfall der Hauptverbindungen.",
            "Die teilnehmenden Verbände melden ihre Übungsteilnehmer bis zum 10. April 2026 an die "
            "Übungsleitung. Die Unterbringung erfolgt in der Liegenschaft.",
            "Die Einspielungen werden durch die Leitungsgruppe vorbereitet und am Vorabend jedes "
            "Übungstages freigegeben. Änderungen am Drehbuch bedürfen der Zustimmung des "
            "Übungsleiters.",
            "Die Fernmeldeverbindungen werden über zwei unabhängige Wege betrieben. Der Ausfall "
            "der Satellitenverbindung wird am zweiten Übungstag eingespielt.",
            "Die Auswertung erfolgt in einer Nachbesprechung am 22. Mai 2026. Die Erkenntnisse "
            "fließen in den Erfahrungsbericht ein.",
            "Die Verpflegung stellt die Truppenküche sicher. Die sanitätsdienstliche Versorgung "
            "übernimmt die Sanitätsstaffel des Standortes.",
        ],
    ),
    "grenze": (
        "Lagebericht Grenze, Februar 2026",
        [
            "Im Berichtszeitraum wurden im Bereich der Inspektion 412 insgesamt 318 unerlaubte "
            "Einreisen festgestellt. Dies entspricht einem Anstieg um 8 Prozent gegenüber dem "
            "Vorjahresmonat.",
            "Schwerpunkt der Feststellungen war erneut die Bundesautobahn A 17. Die Mehrzahl der "
            "Personen reiste in Kleintransportern ein.",
            "Die Zusammenarbeit mit den Behörden des Nachbarstaates hat sich bewährt. Die "
            "gemeinsamen Streifen werden bis Ende Juni fortgesetzt.",
            "Für die Sommermonate wird eine Verstärkung durch zwei Einsatzhundertschaften "
            "beantragt. Die Unterbringung ist mit der Standortverwaltung abgestimmt.",
            "Die Lagebeurteilung wird zum 15. jedes Monats fortgeschrieben und den beteiligten "
            "Dienststellen übersandt.",
            "Bei Kontrollen im grenznahen Raum wurden 27 gefälschte Ausweisdokumente "
            "sichergestellt. Die Ermittlungen führt das zuständige Fachkommissariat.",
        ],
    ),
    "personal": (
        "Organisationsänderung Referat Z 12",
        [
            "Zum 1. Juli 2026 wird das Referat Z 12 mit dem Referat Z 14 zusammengelegt. Die neue "
            "Organisationseinheit führt die Bezeichnung Referat Z 12 und umfasst 23 Dienstposten.",
            "Die Leitung übernimmt Frau Regierungsdirektorin Dr. Albers. Die Vertretung nimmt "
            "Herr Oberregierungsrat Brandt wahr.",
            "Die betroffenen Beschäftigten wurden am 12. März 2026 in einer Personalversammlung "
            "informiert. Der Personalrat hat der Maßnahme zugestimmt.",
            "Nicht besetzte Dienstposten werden im zweiten Halbjahr ausgeschrieben. Eine "
            "Übersicht der Stellen ist als Anlage 2 beigefügt.",
            "Die Umzüge innerhalb des Dienstgebäudes erfolgen in der Kalenderwoche 27. Die "
            "IT-Arbeitsplätze werden am Wochenende zuvor umgestellt.",
        ],
    ),
    "haushalt": (
        "Haushaltsaufstellung 2027, Kapitel 1403",
        [
            "Für das Haushaltsjahr 2027 werden im Kapitel 1403 Mittel in Höhe von 14,2 Mio. Euro "
            "angemeldet. Dies entspricht einer Steigerung von 6,1 Prozent gegenüber dem Vorjahr.",
            "Die Mehrbedarfe ergeben sich im Wesentlichen aus gestiegenen Energiekosten und den "
            "Tarifabschlüssen. Einsparungen sind bei den Reisekosten vorgesehen.",
            "Verpflichtungsermächtigungen werden für die Folgejahre in Höhe von insgesamt 3,5 Mio. "
            "Euro benötigt.",
            "Die Anmeldung ist mit dem Haushaltsreferat abgestimmt. Abweichungen gegenüber der "
            "Finanzplanung sind in der Anlage erläutert.",
            "Sollte der Mehrbedarf nicht vollständig gedeckt werden, müssten die "
            "Ersatzbeschaffungen für Dienstfahrzeuge in das Jahr 2028 verschoben werden.",
        ],
    ),
    "technik": (
        "Vermessung der Antennenanlage Mast 3",
        [
            "Die Antennenanlage auf Mast 3 wurde am 2. März 2026 vermessen. Das "
            "Stehwellenverhältnis lag in allen Kanälen unter 1,5.",
            "Bei der Prüfung der Abstrahlung wurden im Frequenzbereich zwischen 30 und 88 MHz "
            "keine Grenzwertüberschreitungen festgestellt.",
            "Das Netzteil der Sendeeinheit erwärmte sich nach 48 Stunden Dauerbetrieb auf 71 Grad "
            "Celsius. Der zulässige Wert liegt bei 75 Grad Celsius.",
            "Es wird empfohlen, die Belüftung des Geräteschranks zu verbessern und die Messung "
            "nach dem Umbau zu wiederholen.",
            "Die Messgeräte waren zum Zeitpunkt der Prüfung kalibriert. Die Kalibrierscheine "
            "liegen der Prüfstelle vor.",
        ],
    ),
}

MEASUREMENTS = [
    ("Kanal", "Frequenz", "SWR", "Ergebnis"),
    ("1", "30,025 MHz", "1,21", "in Ordnung"),
    ("2", "46,500 MHz", "1,34", "in Ordnung"),
    ("3", "62,750 MHz", "1,48", "in Ordnung"),
    ("4", "87,975 MHz", "1,52", "nachmessen"),
]

CONTRACT = [
    (
        "§ 1 Vertragsgegenstand",
        "Der Auftragnehmer liefert dem Auftraggeber 240 Handfunkgeräte "
        "einschließlich Zubehör gemäß der Leistungsbeschreibung vom 3. Februar 2026, die "
        "Bestandteil dieses Vertrages ist.",
    ),
    (
        "§ 2 Lieferung",
        "Die Lieferung erfolgt frei Verwendungsstelle. Teillieferungen sind nach "
        "vorheriger Abstimmung zulässig.",
    ),
    (
        "§ 3 Vergütung",
        "Die Vergütung beträgt 1.482.000,00 Euro zuzüglich der gesetzlichen "
        "Umsatzsteuer. Die Rechnungsstellung erfolgt nach Abnahme.",
    ),
    (
        "§ 4 Gewährleistung",
        "Die Gewährleistungsfrist beträgt 36 Monate ab Abnahme. Mängel sind "
        "innerhalb von zehn Werktagen nach Anzeige zu beseitigen.",
    ),
    (
        "§ 5 Geheimschutz",
        "Der Auftragnehmer verpflichtet sich, die Bestimmungen des "
        "Geheimschutzhandbuchs einzuhalten und nur sicherheitsüberprüftes Personal einzusetzen.",
    ),
    (
        "§ 6 Schlussbestimmungen",
        "Änderungen und Ergänzungen dieses Vertrages bedürfen der "
        "Schriftform. Gerichtsstand ist Bonn.",
    ),
]

AT_PARAS = [
    "Mit Erlass vom 12. Jänner 2026, GZ 2026-0.018.442, wurde die Überprüfung der "
    "Alarmierungspläne in allen Bundesländern angeordnet. Die Ergebnisse liegen nunmehr vor.",
    "In den Bundesländern Tirol und Vorarlberg wurden Abweichungen bei der Erreichbarkeit der "
    "Einsatzstäbe festgestellt. Die Landespolizeidirektionen wurden um Stellungnahme ersucht.",
    "Das Österreichische Bundesheer unterstützt die Übung mit zwei Hubschraubern und einer "
    "Pioniereinheit. Die Koordination erfolgt über das Streitkräfteführungskommando in Graz.",
    "Die Kosten für die Beschaffung der Funkgeräte werden aus dem Detailbudget 11.02.01 "
    "bedeckt. Das Einvernehmen mit dem Bundesministerium für Finanzen ist hergestellt.",
    "Die Bezirkshauptmannschaften werden ersucht, die aktualisierten Kontaktlisten bis "
    "28. Februar 2026 zu übermitteln.",
    "Die Unterlagen sind nach den Bestimmungen des Informationssicherheitsgesetzes und der "
    "Informationssicherheitsverordnung zu behandeln.",
    "Für Rückfragen steht Herr Oberst Mag. Leitner, Abteilung II/4, unter der Durchwahl "
    "22 4815 zur Verfügung.",
]

CH_PARAS = [
    "Das Beschaffungsvorhaben Funkaufklärung 2028 wurde vom Bundesrat im Rahmen der "
    "Armeebotschaft 2026 genehmigt. Der Verpflichtungskredit beträgt 148 Millionen Franken.",
    "Das Bundesamt für Musterbeschaffung hat die Evaluation der Angebote abgeschlossen. Zwei "
    "Anbieter erfüllen die Mussanforderungen vollständig.",
    "Die Truppenversuche sind für das dritte Quartal 2026 auf dem Waffenplatz Musterberg "
    "vorgesehen. Die betroffenen Kantone wurden orientiert.",
    "Die Massnahmen zur Erhöhung der Cybersicherheit werden gemäss dem Aktionsplan des "
    "Departements bis Ende 2026 umgesetzt.",
    "Die Unterlagen sind nach den Vorgaben der Informationsschutzverordnung (ISchV) zu "
    "bearbeiten und nach Abschluss des Geschäfts zu vernichten.",
    "Wir danken Ihnen für Ihre Stellungnahme bis zum 15. März 2026 und stehen für Fragen gerne "
    "zur Verfügung.",
]

NATO_PARAS = [
    "1. PURPOSE. This memorandum sets out the planning timeline for the command post exercise "
    "NORTHERN EXAMPLE 2026 and invites nations to confirm their participation.",
    "2. BACKGROUND. The exercise will test the deployment of a joint headquarters and the "
    "handover of command between two rotations. It will be conducted from 14 to 25 September "
    "2026.",
    "3. The Main Planning Conference will take place on 12 May 2026. Nations are requested to "
    "nominate one delegate and one alternate by 20 April 2026.",
    "4. The communications architecture will rely on the federated mission network. Each "
    "participating nation is responsible for the accreditation of its own systems.",
    "5. Logistic support, including accommodation and transport from the airport, will be "
    "provided by the host nation. Costs will be shared in accordance with the agreed formula.",
    "6. RECOMMENDATION. The Committee is invited to note the timeline and to endorse the "
    "proposed scenario outline at its next meeting.",
    "7. Points of contact are the exercise planning officer, Lieutenant Colonel J. Andersen, "
    "and the logistics coordinator, Major P. Novak.",
]

NATO_DE_PARAS = [
    "1. ZWECK. Diese Weisung legt den Zeitplan für die Stabsübung NORTHERN EXAMPLE 2026 fest "
    "und fordert die Nationen auf, ihre Teilnahme zu bestätigen.",
    "2. Die Übung erprobt die Verlegung eines gemeinsamen Hauptquartiers und die Übergabe der "
    "Führung zwischen zwei Kontingenten. Sie findet vom 14. bis 25. September 2026 statt.",
    "3. Die Hauptplanungskonferenz findet am 12. Mai 2026 statt. Die Nationen benennen bis zum "
    "20. April 2026 je einen Delegierten und einen Vertreter.",
    "4. Die logistische Unterstützung einschließlich Unterbringung übernimmt die "
    "Gastgebernation. Die Kosten werden nach dem vereinbarten Schlüssel geteilt.",
]

US_PARAS = [
    "1. The program review was held on 4 March 2026 at the Example Test Center. Attendees are "
    "listed at enclosure 1.",
    "2. The integration schedule slipped by six weeks due to the delayed delivery of the "
    "antenna subassembly. Flight testing now begins in August 2026.",
    "3. The program office will present a recovery plan at the next quarterly review. Funding "
    "for the additional test events is available within the current allocation.",
    "4. Point of contact for this memorandum is Ms. A. Rivera, (555) 010-4471.",
]

UK_PARAS = [
    "1. This note summarises the options for the relocation of the regional data centre and "
    "asks you to agree a preferred option.",
    "2. Option A retains the current site and upgrades power and cooling at an estimated cost "
    "of £4.2 million over two years.",
    "3. Option B moves the service to a shared government facility in 2027. It is cheaper over "
    "ten years but carries a higher migration risk.",
    "4. Officials recommend Option B, subject to a full business case in the autumn.",
]

EU_EN_PARAS = [
    "Delegations will find attached the draft conclusions on the assessment of secure "
    "communication systems, as agreed by the Working Party on 12 March 2026.",
    "The Presidency intends to submit the draft to the Committee for approval at its meeting "
    "on 23 April 2026.",
    "Several delegations requested further information on the funding of the accompanying "
    "measures. The Commission undertook to provide a written reply.",
    "The Working Party will resume its examination of the technical annex at its next meeting.",
]

EU_FR_PARAS = [
    "Les délégations trouveront ci-joint le projet de conclusions relatif à l'évaluation des "
    "systèmes de communication sécurisés, tel qu'approuvé par le groupe de travail.",
    "Le groupe de travail a examiné les propositions de la présidence et a approuvé le "
    "calendrier des travaux pour le premier semestre.",
    "Plusieurs délégations ont demandé des précisions sur le financement des mesures "
    "d'accompagnement.",
    "La prochaine réunion du groupe de travail est prévue le 23 avril 2026 à Bruxelles.",
]

FR_PARAS = [
    "La présente note expose les options retenues pour la réorganisation du centre de "
    "traitement de Rennes.",
    "Le calendrier prévoit une mise en œuvre progressive à partir du 1er septembre 2026, "
    "après consultation des représentants du personnel.",
    "Le coût total est estimé à 2,3 millions d'euros, financé sur les crédits du programme 144.",
    "Je vous propose de valider l'option 2, qui préserve la continuité du service.",
]

COMPANY_DE_PARAS = [
    "Die Muster Maschinenbau GmbH prüft die Übernahme der Feinwerk Beispiel AG (Projekt "
    "Falke). Der Kaufpreis soll zwischen 42 und 48 Mio. Euro liegen.",
    "Die Due-Diligence-Prüfung wird bis Ende April abgeschlossen. Erste Ergebnisse zeigen "
    "Risiken bei den Pensionsverpflichtungen.",
    "Das Ergebnis vor Steuern lag im dritten Quartal bei 3,8 Mio. Euro und damit 12 Prozent "
    "unter Plan. Ursache sind vor allem gestiegene Materialkosten.",
    "Die Geschäftsführung schlägt vor, die Preise für Ersatzteile zum 1. Juli um "
    "durchschnittlich 4,5 Prozent anzuheben.",
    "Die Gehaltsbänder für die Entgeltgruppen E9 bis E12 werden zum 1. April angepasst. Die "
    "Übersicht ist als Anlage beigefügt.",
    "Der Rahmenvertrag mit dem Kunden Nordwerk AG läuft zum 31. Dezember aus. Die "
    "Verhandlungen über eine Verlängerung beginnen im Mai.",
    "Die Rezeptur des neuen Klebstoffs KL-7 wurde zum Patent angemeldet. Die Pilotanlage in "
    "Werk 2 geht im Juni in Betrieb.",
]

COMPANY_EN_PARAS = [
    "Project Kestrel: the board is asked to approve a non-binding offer for Harbour Sensors "
    "Ltd. at an enterprise value of 36 to 41 million pounds.",
    "Third-quarter revenue was 18.4 million pounds, 6 per cent below budget, driven by delayed "
    "orders in the marine segment.",
    "Management proposes a price increase of 4 per cent on spare parts from 1 July.",
    "The supply agreement with Northwind Marine expires on 31 December; negotiations on a "
    "renewal start in May.",
]

TLP_DE = [
    "Eine Schwachstelle in der Anmeldekomponente erlaubt es nicht authentisierten Angreifern, "
    "Befehle mit Systemrechten auszuführen (CVE-2026-31337).",
    "Es liegen Hinweise auf eine aktive Ausnutzung vor. Betroffene Organisationen sollten die "
    "bereitgestellten Updates umgehend einspielen.",
    "Als Übergangsmaßnahme kann der Zugriff auf die Weboberfläche auf Verwaltungsnetze begrenzt "
    "werden.",
    "Indikatoren einer Kompromittierung sind Anmeldungen des Dienstkontos svc_remote außerhalb "
    "der Wartungsfenster sowie Verbindungen zu 203.0.113.45.",
]

TLP_EN = [
    "A flaw in the web management interface allows remote code execution without "
    "authentication (CVE-2026-40404). Firmware 9.1 to 9.3.2 is affected.",
    "Exploitation in the wild has been observed since 2 March 2026. Apply firmware 9.3.3 or "
    "later without delay.",
    "Until the update is installed, block access to the management interface from untrusted "
    "networks.",
    "Indicators of compromise include new administrator accounts and outbound connections to "
    "198.51.100.23 on port 8443.",
]


class Deck:
    """Paragraphs dealt without repeats: the topic's own first, then `spare`
    ones from related documents, and only then the same paragraphs again.

    `ordered` deals a numbered pool as written, since a memo whose paragraphs
    run 2, 4, 1, 3 is not one anybody wrote.
    """

    def __init__(
        self, pool: list[str], rng: random.Random, spare: list[str] = (), *, ordered: bool = False
    ) -> None:
        self.rng = rng
        self.ordered = ordered
        self.cards = self._shuffled(pool)
        self.later = [p for p in spare if p not in pool]
        self.everything = list(pool) + self.later

    def _shuffled(self, cards: list[str]) -> list[str]:
        if self.ordered:
            return list(reversed(cards))
        cards = list(cards)
        self.rng.shuffle(cards)
        return cards

    def deal(self, count: int = 1) -> list[tuple[str, str]]:
        out = []
        for _ in range(count):
            if not self.cards:
                self.cards = self._shuffled(self.later or self.everything)
                self.later = []
            out.append(("p", self.cards.pop()))
        return out


def more_pages(deck: Deck, headings: list[str], count: int) -> list[list]:
    """Continuation pages: a heading and a run of paragraphs each."""
    pages = []
    for index in range(count):
        blocks: list = [("h", headings[index % len(headings)])]
        blocks += deck.deal(3)
        blocks.append(("h", headings[(index + 1) % len(headings)]))
        blocks += deck.deal(3)
        pages.append(blocks)
    return pages


def de_content(
    rng: random.Random,
    kind: str = "letter",
    *,
    topic: str | None = None,
    pages: int = 1,
    org: int | None = None,
    extra: list[str] = (),
) -> Content:
    """A German federal document: letter, Vermerk, Protokoll, Vertrag, report or Weisung."""
    topic = topic or rng.choice(sorted(DE_TOPICS))
    betreff, pool = DE_TOPICS[topic]
    spare = [text for _, texts in DE_TOPICS.values() for text in texts]
    deck = Deck(pool, rng, spare)
    name, unit, address = DE_ORGS[org if org is not None else rng.randrange(len(DE_ORGS))]
    date = rng.choice(DATES)
    az = f"{rng.randint(10, 99)}-{rng.randint(100, 999)}/{rng.randint(10, 99)}#{rng.randint(1, 9)}"
    person = rng.choice(NAMES)
    head_left = [("org", name), ("l", unit), ("s", address)]
    head_right = [("s", f"Az.: {az}"), ("s", f"Bearbeitung: {person}"), ("s", date)]
    tail = [("p", text) for text in extra]

    if kind == "letter":
        recipient = rng.choice(DE_RECIPIENTS)
        first = [("l", line) for line in recipient]
        first += [("gap", 1.2), ("h", f"Betreff: {betreff}"), ("l", f"Bezug: Schreiben vom {date}")]
        first += [("gap", 0.8), ("l", "Sehr geehrte Damen und Herren,"), ("gap", 0.4)]
        first += deck.deal(5) + tail
        first += [("l", "Mit freundlichen Grüßen"), ("l", "Im Auftrag"), ("gap", 1.0)]
        first += [("l", f"{rng.choice(['Dr. ', ''])}{rng.choice(NAMES)}")]
        headings = ["Anlage 1: Sachstand", "Anlage 2: Zeitplan", "Anlage 3: Kosten"]
    elif kind == "vermerk":
        first = [("title", "Vermerk"), ("l", f"Betr.: {betreff}"), ("gap", 0.5)]
        first += [("h", "1. Sachverhalt")] + deck.deal(3)
        first += [("h", "2. Bewertung")] + deck.deal(2) + tail
        first += [("h", "3. Vorschlag")] + deck.deal(1) + [("l", f"gez. {person}")]
        headings = ["4. Ergänzende Hinweise", "5. Weiteres Vorgehen", "6. Beteiligung"]
    elif kind == "protokoll":
        first = [("title", "Ergebnisprotokoll"), ("l", f"Sitzung der Arbeitsgruppe {betreff}")]
        first += [
            ("l", f"Datum: {date}, 10:00 bis 12:30 Uhr"),
            ("l", "Ort: Dienstgebäude, Raum 2.114"),
        ]
        first += [("l", f"Leitung: {rng.choice(NAMES)}; Protokoll: {person}"), ("gap", 0.5)]
        first += [("h", "TOP 1 Begrüßung und Tagesordnung")]
        first += [("p", "Die Tagesordnung wurde ohne Änderungen genehmigt.")]
        first += [("h", f"TOP 2 {betreff}")] + deck.deal(3) + tail
        first += [("h", "TOP 3 Verschiedenes")] + deck.deal(2)
        headings = ["TOP 4 Termine", "TOP 5 Aufträge", "TOP 6 Nächste Sitzung"]
    elif kind == "vertrag":
        first = [("title", "Vertrag über die Lieferung von Handfunkgeräten")]
        first += [("l", "zwischen der Bundesrepublik Deutschland, vertreten durch"), ("l", name)]
        first += [("l", "– nachstehend Auftraggeber genannt –"), ("gap", 0.3)]
        first += [("l", "und der Funktechnik Beispiel GmbH, Musterweg 3, 28195 Bremen")]
        first += [("l", "– nachstehend Auftragnehmer genannt –"), ("gap", 0.6)]
        for heading, text in CONTRACT[: 4 if pages > 1 else 6]:
            first += [("h", heading), ("p", text)]
        first += tail
        headings = [heading for heading, _ in CONTRACT[4:]] + ["Anlage: Leistungsbeschreibung"]
    elif kind == "bericht":
        betreff, pool = DE_TOPICS["technik"]
        deck = Deck(pool, rng, spare)
        first = [("title", f"Prüfbericht Nr. 330-2026-{rng.randint(10, 99):03d}")]
        first += [("l", f"Prüfgegenstand: {betreff}"), ("l", f"Prüfdatum: {date}"), ("gap", 0.5)]
        first += [("h", "1 Einleitung")] + deck.deal(1) + [("h", "2 Messergebnisse")]
        first += [("rowh", MEASUREMENTS[0])] + [("row", row) for row in MEASUREMENTS[1:]]
        first += [("gap", 0.6)] + deck.deal(2) + [("h", "3 Bewertung")] + deck.deal(2) + tail
        headings = ["4 Empfehlungen", "5 Messaufbau", "6 Anhang"]
    elif kind == "weisung":
        betreff, pool = DE_TOPICS["uebung"]
        deck = Deck(pool, rng, spare)
        first = [("title", f"Weisung Nr. {rng.randint(2, 9)}/2026"), ("l", f"für die {betreff}")]
        first += [("l", f"Bezug: Planungsbefehl vom {date}"), ("gap", 0.5)]
        first += [("h", "1. Lage")] + deck.deal(2) + [("h", "2. Auftrag")] + deck.deal(1)
        first += [("h", "3. Durchführung")] + deck.deal(3) + tail
        headings = ["4. Einsatzunterstützung", "5. Führung und Fernmeldewesen", "6. Zeitplan"]
    else:
        raise ValueError(kind)
    return Content(
        [first, *more_pages(deck, headings, pages - 1)], head_left=head_left, head_right=head_right
    )


def at_content(rng: random.Random, *, pages: int = 1, extra: list[str] = ()) -> Content:
    """An Austrian federal letter: Republik Österreich, GZ, Jänner, Bundesheer."""
    deck = Deck(AT_PARAS, rng)
    gz = f"GZ 2026-0.{rng.randint(100, 999)}.{rng.randint(100, 999)}"
    first: list = [
        ("h", "Betreff: Überprüfung der Alarmierungspläne; Ergebnisbericht"),
        ("gap", 0.4),
    ]
    first += [("l", "Sehr geehrte Damen und Herren!"), ("gap", 0.3)]
    first += deck.deal(6) + [("p", text) for text in extra]
    first += [("l", "Wien, am 3. Februar 2026"), ("l", "Für die Bundesministerin:")]
    first += [("l", "Mag. Dr. Eva Brunner")]
    return Content(
        [first, *more_pages(deck, ["Beilage A", "Beilage B"], pages - 1)],
        head_left=[
            ("org", "REPUBLIK ÖSTERREICH"),
            ("org2", "Bundesministerium für Musterangelegenheiten"),
            ("l", "Sektion II – Abteilung 4"),
            ("s", "Musterplatz 3, 1010 Wien"),
        ],
        head_right=[
            ("s", gz),
            ("s", "Sachbearbeiterin: Mag. Huber"),
            ("s", "Tel.: +43 1 53126-4815"),
        ],
    )


def ch_content(rng: random.Random, *, pages: int = 1, extra: list[str] = ()) -> Content:
    """A Swiss federal letter: four-language letterhead, Departement, no ß."""
    deck = Deck(CH_PARAS, rng)
    first: list = [("h", "Beschaffungsvorhaben Funkaufklärung 2028: Stand der Evaluation")]
    first += [("gap", 0.4), ("l", "Sehr geehrte Damen und Herren"), ("gap", 0.3)]
    first += deck.deal(6) + [("p", text) for text in extra]
    first += [("l", "Freundliche Grüsse"), ("gap", 0.6), ("l", "Dr. Urs Meier")]
    first += [("s", "Chef Sektion Planung")]
    return Content(
        [first, *more_pages(deck, ["Beilage 1", "Beilage 2"], pages - 1)],
        head_left=[
            ("org2", "Schweizerische Eidgenossenschaft"),
            ("s", "Confédération suisse"),
            ("s", "Confederazione Svizzera"),
            ("s", "Confederaziun svizra"),
        ],
        head_right=[
            ("s", "Eidgenössisches Departement für Musterwesen EDM"),
            ("s", "Bundesamt für Musterbeschaffung BAMB"),
            ("s", "Referenz: 023.1-00045/2026"),
            ("s", "Bern, 4. März 2026"),
        ],
    )


def nato_content(rng: random.Random, *, pages: int = 1, lang: str = "en") -> Content:
    """A NATO staff memorandum, in English or as a German translation."""
    deck = Deck(NATO_PARAS if lang == "en" else NATO_DE_PARAS, rng, ordered=True)
    if lang == "en":
        first: list = [("title", "MEMORANDUM"), ("l", "To: See distribution")]
        first += [("h", "SUBJECT: EXERCISE NORTHERN EXAMPLE 2026 – PLANNING TIMELINE")]
        first += [("l", "Reference: EJFC/J7/2026/0412 dated 3 March 2026"), ("gap", 0.4)]
        first += deck.deal(7) + [("gap", 0.6), ("l", "J. Andersen"), ("l", "Lieutenant Colonel")]
        heads = [("org", "HEADQUARTERS EXAMPLE JOINT FORCE COMMAND"), ("s", "J7 – Exercises")]
        more = ["ANNEX A – TIMELINE", "ANNEX B – DISTRIBUTION"]
    else:
        first = [("title", "Übersetzung"), ("h", "BETREFF: STABSÜBUNG NORTHERN EXAMPLE 2026")]
        first += [("gap", 0.4)] + deck.deal(4)
        heads = [("org", "Kommando Musterkräfte"), ("s", "Sprachendienst, Übersetzung Nr. 412/26")]
        more = ["ANLAGE A – ZEITPLAN", "ANLAGE B – VERTEILER"]
    return Content(
        [first, *more_pages(deck, more, pages - 1)],
        head_left=heads,
        head_right=[("s", "EJFC/J7/2026/0412"), ("s", "3 March 2026")],
    )


def eu_content(rng: random.Random, *, lang: str = "en", pages: int = 1) -> Content:
    """A Council-style working party note, English or French."""
    if lang == "en":
        deck = Deck(EU_EN_PARAS, rng)
        first: list = [
            ("title", "NOTE"),
            ("l", "From: General Secretariat"),
            ("l", "To: Delegations"),
        ]
        first += [("h", "Subject: Draft conclusions on secure communication systems"), ("gap", 0.4)]
        heads = [("org", "WORKING PARTY ON SECURITY PROCEDURES"), ("s", "Brussels, 13 March 2026")]
        more = ["ANNEX", "ANNEX II"]
    else:
        deck = Deck(EU_FR_PARAS, rng)
        first = [
            ("title", "NOTE"),
            ("l", "Origine : Secrétariat général"),
            ("l", "Destinataire : délégations"),
        ]
        first += [("h", "Objet : Projet de conclusions sur les systèmes sécurisés"), ("gap", 0.4)]
        heads = [
            ("org", "GROUPE DE TRAVAIL « PROCÉDURES DE SÉCURITÉ »"),
            ("s", "Bruxelles, le 13 mars 2026"),
        ]
        more = ["ANNEXE", "ANNEXE II"]
    first += deck.deal(4)
    return Content(
        [first, *more_pages(deck, more, pages - 1)],
        head_left=heads,
        head_right=[("s", f"{rng.randint(5000, 9999)}/26"), ("s", "WP-SEC 14")],
    )


def us_content(rng: random.Random, *, cui: bool = False) -> Content:
    """A US Department memorandum with a classification authority block."""
    deck = Deck(US_PARAS, rng, ordered=True)
    first: list = [("title", "MEMORANDUM FOR RECORD")]
    first += [("h", "SUBJECT: Program Review – Sensor Integration Phase II"), ("gap", 0.4)]
    first += deck.deal(4) + [("gap", 0.8), ("l", "A. RIVERA"), ("l", "Program Manager")]
    if cui:
        first += [("gap", 1.0), ("s", "Controlled by: Department of Examples, Program Office")]
        first += [("s", "Category: PRVCY, PROCURE"), ("s", "POC: A. Rivera, (555) 010-4471")]
    else:
        first += [("gap", 1.0), ("s", "Classified By: J. Smith, Director, Program Office")]
        first += [("s", "Derived From: Example Program SCG, dated 20250112")]
        first += [("s", "Declassify On: 20510301")]
    return Content(
        [first],
        head_left=[("org", "DEPARTMENT OF EXAMPLES"), ("s", "WASHINGTON, DC 20301-1000")],
        head_right=[("s", "5 March 2026")],
    )


def uk_content(rng: random.Random) -> Content:
    deck = Deck(UK_PARAS, rng, ordered=True)
    first: list = [("title", "Note for the Permanent Secretary")]
    first += [("h", "Relocation of the regional data centre"), ("gap", 0.4)] + deck.deal(4)
    first += [("gap", 0.6), ("l", "R. Hughes"), ("l", "Director, Digital Services")]
    return Content(
        [first],
        head_left=[("org", "Department for Examples"), ("s", "1 Example Street, London SW1A 0AA")],
        head_right=[("s", "6 March 2026")],
    )


def fr_content(rng: random.Random) -> Content:
    deck = Deck(FR_PARAS, rng, ordered=True)
    first: list = [("title", "NOTE"), ("l", "à l'attention de Monsieur le Directeur")]
    first += [("h", "Objet : Réorganisation du centre de traitement de Rennes"), ("gap", 0.4)]
    first += deck.deal(4) + [("gap", 0.6), ("l", "Le chef du bureau"), ("l", "M. Durand")]
    return Content(
        [first],
        head_left=[
            ("org", "MINISTÈRE DES EXEMPLES"),
            ("l", "Direction des affaires générales"),
            ("s", "Paris, le 5 mars 2026"),
        ],
        head_right=[("s", "N° 0412/DAG/BRH")],
    )


def company_content(rng: random.Random, *, lang: str = "de", pages: int = 1) -> Content:
    """A board paper on company letterhead, with the Handelsregister footer."""
    if lang == "de":
        deck = Deck(COMPANY_DE_PARAS, rng)
        first: list = [
            ("title", f"Vorlage für die Geschäftsführung Nr. {rng.randint(10, 40)}/2026")
        ]
        first += [
            ("l", "Thema: Quartalsbericht und Projekt Falke"),
            ("l", f"Datum: {rng.choice(DATES)}"),
        ]
        first += [("gap", 0.5)] + deck.deal(6) + [("l", "gez. Dr. Klaus Beispiel")]
        head = [("org", "MUSTER MASCHINENBAU GmbH"), ("s", "Industriestraße 14 · 85748 Garching")]
        foot = [
            "Muster Maschinenbau GmbH · Geschäftsführer: Dr. Klaus Beispiel",
            "Amtsgericht München HRB 123456 · USt-IdNr. DE123456789",
        ]
        more = ["Anlage 1: Kennzahlen", "Anlage 2: Zeitplan"]
    else:
        deck = Deck(COMPANY_EN_PARAS, rng)
        first = [("title", "Board Paper 14/2026"), ("l", "Subject: Project Kestrel and Q3 results")]
        first += [("gap", 0.5)] + deck.deal(4) + [("l", "M. Clarke, Chief Financial Officer")]
        head = [("org", "NORTHFIELD INSTRUMENTS LTD"), ("s", "42 Kingsway, Manchester M1 4BT")]
        foot = ["Northfield Instruments Ltd · Registered in England No. 01234567"]
        more = ["Appendix A: Figures", "Appendix B: Timeline"]
    return Content([first, *more_pages(deck, more, pages - 1)], head_left=head, foot=foot)


def tlp_content(rng: random.Random, *, lang: str = "de", extra: list[str] = ()) -> Content:
    """A CERT advisory, the document TLP labels were made for."""
    number = rng.randint(100, 999)
    if lang == "de":
        deck = Deck(TLP_DE, rng)
        first: list = [("title", f"Sicherheitshinweis 2026-{number}")]
        first += [("h", "Kritische Schwachstelle in Fernwartungssoftware")]
        first += [("l", "Betroffene Versionen: 7.2 bis 7.4.1"), ("gap", 0.4)]
        head = [("org", "CERT Musterland"), ("s", "Lagezentrum – Warn- und Informationsdienst")]
    else:
        deck = Deck(TLP_EN, rng)
        first = [("title", f"Security Advisory SA-2026-{number}")]
        first += [("h", "Critical vulnerability in VPN gateway firmware"), ("gap", 0.4)]
        head = [("org", "EXAMPLE NATIONAL CSIRT"), ("s", "Coordination Centre")]
    first += deck.deal(4) + [("p", text) for text in extra]
    return Content([first], head_left=head, head_right=[("s", rng.choice(DATES))])


def plain(blocks: list, *, head_left=(), head_right=(), size=A4, **kwargs) -> Content:
    """A one-page document from explicit blocks."""
    return Content(
        [blocks], head_left=list(head_left), head_right=list(head_right), size=size, **kwargs
    )


# ---------------------------------------------------------------- corpus


class Corpus:
    """Renders documents, writes them and collects the ground truth."""

    def __init__(self, root: Path, seed: int) -> None:
        self.root = root
        self.rng = random.Random(seed)
        self.files: list[dict] = []

    # -- rendering

    def render(
        self,
        content: Content,
        marks: list[Mark] = (),
        *,
        dpi: int = 200,
        family: str = "times",
        pt: float = 11.0,
        numbering: str | None = None,
        fax_line: str | None = None,
        number_from: int = 1,
        number_total: int | None = None,
    ) -> list[Page]:
        """Draws every page of `content` with `marks` on it.

        `number_from` and `number_total` let an excerpt say "Seite 3 von 12",
        and let two separately rendered parts of one fax count as one.
        """
        count = len(content.pages)
        out = []
        for index, blocks in enumerate(content.pages):
            number = index + 1
            label, total = number_from + index, number_total or count
            sheet = Sheet(dpi, content.size)
            text = layout(sheet, content, blocks, first=index == 0, family=family, pt=pt)
            if numbering:
                text.append(self._number(sheet, numbering, label, total, family, pt))
            if fax_line:
                face = sheet.font("mono", 7.5)
                line = fax_line.format(n=label, count=total)
                sheet.draw.text((sheet.inch(0.3), sheet.inch(0.1)), line, font=face, fill=INK)
                text.append(line)
            placed = [mark for mark in marks if mark.applies(number, count)]
            # Stamps go on last: they are inked over a finished page.
            for mark in sorted(placed, key=lambda m: m.where == "stamp"):
                if mark.where == "stamp":
                    draw_stamp(sheet, mark, self.rng)
                else:
                    if mark.where in ("both", "header"):
                        draw_banner(sheet, mark, top=True, content=content)
                    if mark.where in ("both", "footer"):
                        draw_banner(sheet, mark, top=False, content=content)
                if not mark.kind:
                    text.extend(mark.text.split("\n"))
            out.append(Page(sheet.image, dpi, [m for m in placed if m.kind], text))
        return out

    @staticmethod
    def _number(sheet: Sheet, style: str, number: int, count: int, family: str, pt: float) -> str:
        face = sheet.font(FAMILIES[family][0], pt * 0.9)
        label = f"- {number} -" if style == "dash" else f"Seite {number} von {count}"
        right = sheet.w - sheet.inch(0.85)
        if style == "header":
            # Bundeswehr layout: page count on the marking's line, far right.
            x, y = right - face.getlength(label), sheet.inch(0.34)
        elif style == "right":
            x, y = right - face.getlength(label), sheet.h - sheet.inch(0.85)
        else:
            x, y = (sheet.w - face.getlength(label)) / 2, sheet.h - sheet.inch(0.85)
        sheet.draw.text((x, y), label, font=face, fill=INK)
        return label

    # -- degradation

    def degrade(self, pages: list[Page], how: str, params: dict) -> list[Page]:
        rng = self.rng
        out = []
        for page in pages:
            image, dpi = page.image, page.dpi
            if how == "aged":
                image = age(image, rng, level=params.get("level", 0.8))
                if params.get("bleed"):
                    image = bleed_through(image)
            elif how == "fax":
                image, dpi = fax_scan(image, rng, dpi=dpi, noise=params.get("noise", 0.02)), 100
            elif how == "jpeg":
                image = jpeg_cycles(image, rounds=params.get("rounds", 1), quality=30)
            elif how == "skew":
                image = skew(image, params["degrees"])
            elif how == "faint" and params.get("toner"):
                image = fade(image, params["toner"])
            out.append(Page(image, dpi, page.marks, page.text))
        return out

    # -- output

    def emit(
        self,
        relative: str,
        pages: list[Page],
        *,
        category: str,
        degradation: str = "none",
        note: str = "",
        tags: tuple[str, ...] = (),
        quality: int | None = None,
        declassified: bool = False,
        data: bytes | None = None,
        **params,
    ) -> None:
        if data is None:
            pages = self.degrade(pages, degradation, params)
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if data is not None:
            target.write_bytes(data)
        else:
            self._write(target, pages, quality or (30 if degradation == "jpeg" else None))
        extra = list(tags)
        if degradation == "fax":
            extra.append(f"fax-noise-{params.get('noise', 0.02):.0%}")
        if degradation == "skew":
            extra.append(f"skew-{abs(params['degrees']):g}deg")
        self.files.append(
            self.truth(
                relative,
                pages,
                category=category,
                degradation=degradation,
                note=note,
                tags=extra,
                declassified=declassified,
                text_layer=data is not None,
            )
        )

    @staticmethod
    def _write(target: Path, pages: list[Page], quality: int | None) -> None:
        suffix = target.suffix.lower()
        dpi = pages[0].dpi
        if suffix == ".pdf":
            rgb = [page.image.convert("RGB") for page in pages]
            target.write_bytes(image_only_pdf(rgb, dpi=dpi, quality=quality or 72))
            return
        if suffix in (".tif", ".tiff"):
            # Copies, because Pillow carries encoder settings over from earlier
            # saves of the same image object (see make_corpus.py).
            frames = [page.image.copy() for page in pages]
            bilevel = all(frame.mode == "1" for frame in frames)
            frames[0].save(
                target,
                "TIFF",
                save_all=len(frames) > 1,
                append_images=frames[1:],
                compression="group4" if bilevel else "tiff_lzw",
                dpi=(dpi, dpi),
            )
            return
        if len(pages) != 1:
            raise ValueError(f"{target.name}: {len(pages)} pages cannot go into {suffix}")
        image = pages[0].image
        if suffix in (".jpg", ".jpeg"):
            image.convert("RGB").save(target, "JPEG", quality=quality or 85, dpi=(dpi, dpi))
        else:
            image.save(target, dpi=(dpi, dpi))

    @staticmethod
    def truth(
        relative: str,
        pages: list[Page],
        *,
        category: str,
        degradation: str,
        note: str,
        tags: list[str],
        declassified: bool,
        text_layer: bool,
    ) -> dict:
        """One ground-truth entry.

        file         path relative to the corpus root
        category     what the file tests (de-header, neg-mention, ...)
        scheme       de | at | ch | nato | eu | us | uk | fr | tlp | company | none
        level        highest government level marked (0 none .. 4 top secret);
                     TLP and company markings are level 0
        label        canonical label of that highest marking, or None
        tlp          RED | AMBER | AMBER+STRICT | GREEN | CLEAR, or None
        company      the company marking, or None
        pages        per page, the government level marked on it
        mentions     running text uses a marking term as a word of its own
        degradation  none | aged | fax | jpeg | skew | lowdpi | faint
        text_layer   born-digital PDF with real text
        markings     the marking texts as drawn, for reading error reports
        tags         how the markings were drawn, for per-style breakdowns
        note         anything a reader of an error report should know
        declassified present and true when the marking is struck or lifted
        """
        levels: list[int] = []
        government: list[Kind] = []
        tlps: set[str] = set()
        companies: set[str] = set()
        drawn: list[str] = []
        auto_tags: set[str] = set()
        places: set[str] = set()
        for page in pages:
            level = 0
            for mark in page.marks:
                kind = KINDS[mark.kind]
                text = mark.text.replace("\n", " / ")
                if text not in drawn:
                    drawn.append(text)
                auto_tags.update(mark_tags(mark))
                places.update(("header", "footer") if mark.where == "both" else (mark.where,))
                if kind.tlp:
                    tlps.add(kind.tlp)
                elif kind.company:
                    companies.add(kind.company)
                else:
                    government.append(kind)
                    level = max(level, kind.level)
            levels.append(level)
        if len(tlps) > 1 or len(companies) > 1:
            raise ValueError(f"{relative}: more than one TLP or company label")
        top = max(government, key=lambda kind: kind.level) if government else None

        if "header" in places and "footer" in places:
            auto_tags.add("header+footer")
        elif "header" in places:
            auto_tags.add("header-only")
        elif "footer" in places:
            auto_tags.add("footer-only")
        marked = [bool(page.marks) for page in pages]
        if len(pages) > 1 and any(marked):
            if all(marked):
                auto_tags.add("every-page")
            elif marked[0] and not any(marked[1:]):
                auto_tags.add("first-page-only")
            else:
                auto_tags.add("some-pages")
        auto_tags.add(Path(relative).suffix.lower().lstrip(".").replace("tiff", "tif"))
        if len(pages) > 1:
            auto_tags.add("multipage")

        body = "\n".join(line for page in pages for line in page.text)
        entry = {
            "file": relative,
            "category": category,
            "scheme": top.scheme if top else "tlp" if tlps else "company" if companies else "none",
            "level": top.level if top else 0,
            "label": top.label if top else None,
            "tlp": next(iter(tlps), None),
            "company": next(iter(companies), None),
            "pages": levels,
            "mentions": bool(MENTION.search(body)),
            "degradation": degradation,
            "text_layer": text_layer,
            "markings": drawn,
            "tags": sorted(auto_tags | set(tags)),
            "note": note,
        }
        if declassified:
            entry["declassified"] = True
        return entry

    def write_manifest(self) -> Path:
        path = self.root / "ground_truth.json"
        payload = {"version": 1, "files": self.files}
        path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        return path


def mark_tags(mark: Mark) -> list[str]:
    """Rendering tags for one classification mark."""
    tags = []
    if mark.where == "stamp":
        tags.append("stamp")
        turn = abs(mark.angle) % 180
        turn = min(turn, 180 - turn)
        tags.append(
            "rot-0"
            if turn < 1
            else "rot-1-20"
            if turn <= 20
            else "rot-21-60"
            if turn <= 60
            else "rot-61-90"
        )
        if "\n" in mark.text:
            tags.append("two-line")
    else:
        tags.append(f"align-{mark.align}")
        if "\n" in mark.text:
            tags.append("two-line")
    if mark.box:
        tags.append("boxed")
    if mark.tracking:
        tags.append("spaced")
    if mark.pt <= 8:
        tags.append("small")
    elif mark.pt >= 16 and mark.where != "stamp":
        tags.append("large")
    if mark.background:
        tags.append("on-black")
    if mark.strike:
        tags.append("struck")
    colour = {RED: "red", BLUE: "blue", BLACK: "black", INK: "black", GREY: "grey", PALE: "grey"}
    if mark.background is None:
        tags.append(colour.get(mark.color, "pale-red" if mark.color == PALE_RED else "colour"))
    if mark.opacity < 0.6 or mark.color in (GREY, PALE, PALE_RED):
        tags.append("faint")
    return tags


# ---------------------------------------------------------------- page notation


def blocks(text: str) -> list[tuple[str, object]]:
    """Parses the notation the one-off pages below are written in.

    One block per line, ``style text``, where style is a key of STYLES; an
    indented line continues the block above it. ``row`` and ``rowh`` cells are
    separated by `` | `` and ``gap`` takes a number of lines.
    """
    raw: list[list[str]] = []
    for line in text.strip("\n").splitlines():
        if not line.strip():
            continue
        if line[0] == " ":
            raw[-1][1] += " " + line.strip()
        else:
            style, _, rest = line.partition(" ")
            raw.append([style, rest.strip()])
    out: list[tuple[str, object]] = []
    for style, rest in raw:
        if style == "gap":
            out.append((style, float(rest)))
        elif style in ("row", "rowh"):
            out.append((style, tuple(cell.strip() for cell in rest.split(" | "))))
        elif style in STYLES:
            out.append((style, rest))
        else:
            raise ValueError(f"unknown style {style!r} before {rest!r}")
    return out


def page(body: str, head: str = "", right: str = "", **kwargs) -> Content:
    """A one-page document in the notation of `blocks`."""
    return Content([blocks(body)], head_left=blocks(head), head_right=blocks(right), **kwargs)


def documents(text: str) -> list[tuple[str, str, dict, Content]]:
    """Splits a run of one-page documents: (file, note, render options, content).

    Each document starts with ``=== file | note | key=value ...``; the rest is
    in the notation of `blocks`, with ``head`` in front of letterhead lines.
    """
    out = []
    for chunk in re.split(r"^=== ", text, flags=re.MULTILINE)[1:]:
        title, _, rest = chunk.partition("\n")
        name, note, *settings = (part.strip() for part in title.split("|"))
        options: dict[str, object] = {}
        for key, _, value in (item.partition("=") for item in " ".join(settings).split()):
            options[key] = int(value) if value.isdigit() else value
        head = [line[5:] for line in rest.splitlines() if line.startswith("head ")]
        body = [line for line in rest.splitlines() if not line.startswith("head ")]
        out.append((name, note, options, page("\n".join(body), "\n".join(head))))
    return out


# ---------------------------------------------------------------- positives

NFD = "VS – NUR FÜR DEN DIENSTGEBRAUCH"
NFD_CAPS = "VS-NUR FÜR DEN DIENSTGEBRAUCH"

#: A positive page may say what it is in running text, too.
VSA_NOTE = (
    "Diese Unterlage ist gemäß VSA als VS-NUR FÜR DEN DIENSTGEBRAUCH eingestuft und nur "
    "Personen zugänglich zu machen, die sie für ihre dienstliche Tätigkeit benötigen."
)


def build_de(c: Corpus) -> None:
    """German VSA markings: header and footer, header only, letter-spaced, stamps."""
    rng = c.rng

    def put(name: str, category: str, document, *marks: Mark, family="times", **options) -> None:
        content = de_content(rng, document) if isinstance(document, str) else document
        pages = c.render(content, list(marks), family=family)
        c.emit(f"de/{name}", pages, category=category, **options)

    # Top and bottom of every page, as the VSA wants it, in the spellings in use.
    cat = "de-header"
    put("vsnfd_hf_00.png", cat, "letter", both(NFD, "VS-NfD"))
    put("vsnfd_hf_01.png", cat, "vermerk", both(NFD_CAPS, "VS-NfD", pt=11, face="arial"))
    put("vsnfd_hf_02.png", cat, "protokoll", both("VS-NfD", "VS-NfD", pt=12, align="right"))
    mark = both("VS-nfD", "VS-NfD", face="arial-bold", align="left")
    put("vsnfd_hf_03.jpg", cat, "bericht", mark, family="arial")
    put("vsnfd_hf_04.png", cat, "vertrag", both("VS - NfD", "VS-NfD", pt=11, box=True))
    mark = both("Nur für den Dienstgebrauch", "VS-NfD", face="arial")
    put("vsnfd_hf_05.png", cat, "letter", mark, family="arial")
    mark = both("Verschlusssache – Nur für den Dienstgebrauch", "VS-NfD", pt=9, face="times-bold")
    put("vsnfd_hf_06.png", cat, de_content(rng, "vermerk", extra=[VSA_NOTE]), mark)
    put("vsnfd_small_00.png", cat, "letter", both("VS-NfD", "VS-NfD", pt=7, face="arial"))
    mark = both("VS-NfD", "VS-NfD", pt=20, face="arial-bold", box=True)
    put("vsnfd_large_00.png", cat, "protokoll", mark, family="arial")
    put("vsv_hf_00.png", cat, "vermerk", both("VS-VERTRAULICH", "VS-VERTRAULICH", pt=14, box=True))
    mark = both("VS – VERTRAULICH", "VS-VERTRAULICH", pt=12, face="arial-bold")
    put("vsv_hf_01.png", cat, "letter", mark, family="arial")
    mark = both("GEHEIM", "GEHEIM", pt=16, face="arial-bold")
    put("geheim_hf_00.png", cat, "weisung", mark, family="arial")
    put("sg_hf_00.png", cat, "vermerk", both("STRENG GEHEIM", "STRENG GEHEIM", pt=18, box=True))
    put("vsnfd_foot_00.png", cat, "letter", footer("VS-NfD", "VS-NfD", pt=9), note="footer only")

    cat, note = "de-headeronly", "header only, no footer"
    mark = header("VS-NfD", "VS-NfD", align="right", face="arial-bold")
    put("vsnfd_head_00.png", cat, "letter", mark, note=note)
    put("vsnfd_head_01.png", cat, "protokoll", header(NFD, "VS-NfD", pt=11), note=note)
    put("geheim_head_00.png", cat, "bericht", header("GEHEIM", "GEHEIM", pt=14), note=note)
    mark = header("VS-Vertraulich", "VS-VERTRAULICH", pt=11, align="left")
    put("vsv_head_00.png", cat, "vermerk", mark, note=note)

    # Letter-spaced the way a word processor does it — and with typed blanks,
    # the only way a typewriter can.
    cat = "de-spaced"
    mark = both("GEHEIM", "GEHEIM", pt=14, tracking=0.6)
    put("geheim_spaced_00.png", cat, "vermerk", mark, family="arial")
    mark = both("VS-VERTRAULICH", "VS-VERTRAULICH", pt=12, tracking=0.45)
    put("vsv_spaced_00.png", cat, "letter", mark, family="arial")
    put("vsnfd_spaced_00.png", cat, "protokoll", both(NFD, "VS-NfD", tracking=0.3))
    mark = both("G E H E I M", "GEHEIM", pt=12, face="mono-bold")
    put("geheim_spaced_01.png", cat, "vermerk", mark, family="mono")

    cat = "de-stamp"
    put("vsnfd_stamp_00.png", cat, "letter", stamp("VS-NfD", "VS-NfD"))
    put("vsnfd_stamp_01.png", cat, "vermerk", stamp("VS-NfD", "VS-NfD", angle=15))
    mark = stamp("VS-NfD", "VS-NfD", angle=30, at=(0.78, 0.06))
    put("vsnfd_stamp_02.png", cat, "protokoll", mark)
    mark = stamp("VS-NfD", "VS-NfD", angle=90, at=(0.94, 0.32))
    put("vsnfd_stamp_03.png", cat, "letter", mark, note="vertical, in the right margin")
    text = "Verschlusssache\nNUR FÜR DEN DIENSTGEBRAUCH"
    mark = stamp(text, "VS-NfD", angle=6, pt=13, at=(0.68, 0.045))
    put("vsnfd_stamp_04.png", cat, "letter", mark)
    mark = stamp("VS-NfD", "VS-NfD", angle=-8, pt=26, box=False)
    put("vsnfd_stamp_05.png", cat, "weisung", mark, note="no border")
    mark = stamp(NFD, "VS-NfD", pt=12, color=BLACK, at=(0.5, 0.94))
    put("vsnfd_stamp_06.png", cat, "vermerk", mark, note="black stamp at the foot of the page")
    mark = stamp("GEHEIM", "GEHEIM", angle=10, pt=28, double=True)
    put("geheim_stamp_00.png", cat, "weisung", mark)
    mark = stamp("GEHEIM", "GEHEIM", angle=35, color=BLACK, at=(0.26, 0.86))
    put("geheim_stamp_01.png", cat, "protokoll", mark)
    mark = stamp("VS-VERTRAULICH", "VS-VERTRAULICH", angle=-12, color=BLUE)
    put("vsv_stamp_00.png", cat, "letter", mark)
    mark = stamp("STRENG GEHEIM", "STRENG GEHEIM", angle=5, double=True, at=(0.72, 0.05))
    put("sg_stamp_00.png", cat, "vermerk", mark)
    mark = stamp("VS-NfD", "VS-NfD", angle=25, pt=36, at=(0.5, 0.45), opacity=0.8)
    put("vsnfd_stamp_overlap_00.png", cat, "vermerk", mark, tags=("overlap",), note="over the text")

    # Typewriter pages from before the spelling reform and before "VS-" was usual.
    cat = "de-historic"
    content = de_content(rng, "vermerk", topic="liegenschaft")
    marks = (
        header("Amtlich geheimgehalten", "VS (amtlich geheimgehalten)", face="mono-bold", pt=12),
        stamp(
            "Amtlich geheimgehalten!",
            "VS (amtlich geheimgehalten)",
            angle=4,
            pt=16,
            at=(0.66, 0.78),
        ),
    )
    aged = {"family": "mono", "degradation": "aged", "level": 0.9}
    note = "'amtlich geheimgehalten' alone: at least VS-VERTRAULICH"
    put("historic_00.jpg", cat, content, *marks, note=note, **aged)
    mark = both("Verschlußsache – Nur für den Dienstgebrauch", "VS-NfD", face="mono-bold", pt=11)
    content = de_content(rng, "letter", topic="personal")
    put("historic_01.jpg", cat, content, mark, note="old spelling Verschlußsache", **aged)

    # Bundeswehr layout: the marking on the left of the header line and the
    # page count on the right of the same line — one line of text to the eye.
    marks = [header(NFD_CAPS, "VS-NfD", align="left"), footer(NFD_CAPS, "VS-NfD")]
    pages = c.render(
        de_content(rng, "weisung", org=2),
        marks,
        family="arial",
        numbering="header",
        number_from=3,
        number_total=12,
    )
    c.emit("de/bw_excerpt_00.png", pages, category="de-bw", note="page 3 of 12 on the marking line")
    content = de_content(rng, "weisung", org=2, pages=6)
    pages = c.render(content, marks, family="arial", numbering="header")
    c.emit("de/bw_weisung_6p.pdf", pages, category="de-bw")
    marks = [header("VS-NfD", "VS-NfD", align="left"), footer("VS-NfD", "VS-NfD")]
    content = de_content(rng, "weisung", org=2, pages=4)
    pages = c.render(content, marks, family="arial", numbering="header")
    c.emit("de/bw_weisung_4p.tif", pages, category="de-bw")


def build_de_multipage(c: Corpus) -> None:
    """Multi-page documents: every page, first page only, some pages, lifted markings."""
    rng = c.rng
    cat = "de-multipage"
    pages = c.render(de_content(rng, "letter", pages=4), [both(NFD, "VS-NfD")], numbering="center")
    c.emit("de/vsnfd_4p.pdf", pages, category=cat)
    fax_line = "12.03.26 10:42  KDO MUSTERKRAEFTE G3  +49 261 896 1234  S.{n}/{count}"
    content = de_content(rng, "weisung", pages=3)
    marks = [both("GEHEIM", "GEHEIM", pt=14)]
    pages = c.render(content, marks, family="arial", numbering="right", fax_line=fax_line)
    c.emit("de/geheim_fax_3p.tif", pages, category=cat, degradation="fax", noise=0.01)
    marks = [stamp("VS-NfD", "VS-NfD", angle=12, on="first")]
    pages = c.render(de_content(rng, "protokoll", pages=3), marks, numbering="right")
    note = "stamp on page 1 only: the VSA wants every page marked"
    c.emit("de/vsnfd_stamp_first_3p.pdf", pages, category=cat, note=note)
    marks = [header("GEHEIM", "GEHEIM", pt=14, on="first")]
    pages = c.render(de_content(rng, "vermerk", pages=2), marks)
    c.emit("de/geheim_head_first_2p.pdf", pages, category=cat, note="header on page 1 only")

    # An unmarked cover letter with a marked annex, and marked documents with
    # one page that lost its marking.
    cat = "mixed"
    cover = de_content(rng, "letter", extra=["Anlage: Bericht zur Liegenschaft Nord (VS-NfD)"])
    annex = de_content(rng, "bericht", pages=2)
    pages = c.render(cover) + c.render(annex, [both("VS-NfD", "VS-NfD", pt=11)])
    c.emit("mixed/cover_annex_3p.pdf", pages, category=cat, note="unmarked cover, marked annex")
    marks = [both(NFD, "VS-NfD", on=(1, 2, 4))]
    pages = c.render(de_content(rng, "vermerk", pages=4), marks, numbering="right")
    c.emit("mixed/vsnfd_missing_p3_4p.pdf", pages, category=cat, note="page 3 lacks its marking")
    marks = [both("VS-VERTRAULICH", "VS-VERTRAULICH", pt=12, box=True, on=(1, 2))]
    content = de_content(rng, "protokoll", pages=3)
    pages = c.render(content, marks, family="arial", numbering="center")
    c.emit("mixed/vsv_missing_last_3p.tif", pages, category=cat, note="last page lacks its marking")
    extra = ["Anbei übersenden wir die Planungsunterlagen des Hauptquartiers im Original."]
    cover = de_content(rng, "letter", topic="uebung", extra=extra)
    marks = [both("NATO RESTRICTED", "NATO RESTRICTED", pt=11)]
    pages = c.render(cover) + c.render(nato_content(rng, pages=3), marks, family="arial")
    note = "German cover letter, NATO annex in English"
    c.emit("mixed/nato_translation_4p.pdf", pages, category=cat, note=note)
    cover = page("""
big TELEFAX
gap 1.0
row An: | Bundesamt für Musterwesen, IT 3
row Fax: | 0228 99 12-3499
row Von: | Kommando Musterkräfte, G3
row Seiten: | 2 einschließlich Deckblatt
row Datum: | 12.03.2026
gap 1.0
p Bitte leiten Sie das anliegende Schreiben umgehend an Herrn Krüger weiter.
""")
    line = "12.03.26 11:05  KDO MUSTERKRAEFTE  +49 261 896 1234  S.{n}/{count}"
    pages = c.render(cover, family="arial", fax_line=line, number_total=2)
    pages += c.render(
        de_content(rng, "letter", org=2),
        [both(NFD, "VS-NfD", pt=11)],
        family="arial",
        fax_line=line,
        number_from=2,
        number_total=2,
    )
    note = "unmarked fax cover sheet"
    c.emit("mixed/fax_cover_2p.pdf", pages, category=cat, degradation="fax", noise=0.015, note=note)

    # Lifted and downgraded: the old marking is still printed on the page.
    cat = "edge"
    text = "Einstufung aufgehoben\n12.03.2024  gez. Müller"
    lifted = stamp(text, None, color=BLUE, pt=13, angle=-4, at=(0.82, 0.06))
    pages = c.render(de_content(rng, "letter"), [both(NFD, "VS-NfD"), lifted])
    note = "classification lifted by stamp; the printed marking is VS-NfD"
    c.emit("edge/vsnfd_aufgehoben_00.png", pages, category=cat, declassified=True, note=note)
    text = "Herabgestuft auf\nVS-NUR FÜR DEN DIENSTGEBRAUCH\n04.02.2025"
    downgraded = stamp(text, None, pt=12, angle=3, at=(0.8, 0.06))
    pages = c.render(de_content(rng, "vermerk"), [both("GEHEIM", "GEHEIM", pt=14), downgraded])
    note = "GEHEIM downgraded to VS-NfD by stamp: level 1 applies now, level 3 is printed"
    c.emit("edge/geheim_herabgestuft_00.png", pages, category=cat, declassified=True, note=note)
    marks = [
        both("VS-VERTRAULICH", "VS-VERTRAULICH", pt=12, strike=True),
        stamp("OFFEN", None, color=BLACK, pt=14, box=False, at=(0.72, 0.035)),
    ]
    pages = c.render(de_content(rng, "protokoll"), marks)
    note = "VS-VERTRAULICH struck through, OFFEN written next to it"
    c.emit("edge/vsv_struck_offen_00.png", pages, category=cat, declassified=True, note=note)


def build_other_schemes(c: Corpus) -> None:
    """Austria, Switzerland, NATO, EU, US, UK and France."""
    rng = c.rng

    def put(name: str, content: Content, mark: Mark, family="arial", **options) -> None:
        pages = c.render(content, [mark], family=family)
        c.emit(name, pages, category=name.split("/")[0], **options)

    # Austria (Informationssicherheitsgesetz) and Switzerland (ISchV) use the
    # German words VERTRAULICH and GEHEIM too; only the letterhead tells them apart.
    put("at/eingeschraenkt_hf_00.png", at_content(rng), both("EINGESCHRÄNKT", "AT EINGESCHRÄNKT"))
    mark = stamp("EINGESCHRÄNKT", "AT EINGESCHRÄNKT", angle=12)
    put("at/eingeschraenkt_stamp_00.png", at_content(rng), mark)
    mark = header("EINGESCHRÄNKT", "AT EINGESCHRÄNKT", pt=8, face="arial", align="right")
    put("at/eingeschraenkt_head_00.jpg", at_content(rng), mark, degradation="jpeg")
    mark = both("VERTRAULICH", "AT VERTRAULICH", pt=12, box=True)
    put("at/vertraulich_hf_00.png", at_content(rng), mark)
    mark = stamp("GEHEIM", "AT GEHEIM", angle=20, pt=24)
    put("at/geheim_stamp_00.jpg", at_content(rng), mark, degradation="aged", level=0.7)
    marks = [both("STRENG GEHEIM", "AT STRENG GEHEIM", pt=13, box=True)]
    pages = c.render(at_content(rng, pages=2), marks, family="arial", numbering="center")
    c.emit("at/streng_geheim_2p.pdf", pages, category="at")

    put("ch/intern_hf_00.png", ch_content(rng), both("INTERN", "CH INTERN", pt=11))
    put("ch/intern_stamp_00.png", ch_content(rng), stamp("INTERN", "CH INTERN", pt=22))
    mark = header("INTERN", "CH INTERN", pt=8, face="arial", align="left")
    put("ch/intern_head_00.png", ch_content(rng), mark)
    put("ch/vertraulich_hf_00.png", ch_content(rng), both("VERTRAULICH", "CH VERTRAULICH", pt=12))
    text = "VERTRAULICH\nCONFIDENTIEL\nCONFIDENZIALE"
    mark = stamp(text, "CH VERTRAULICH", color=BLUE, pt=14, angle=-8)
    put("ch/vertraulich_stamp_00.png", ch_content(rng), mark, note="three official languages")
    mark = both("GEHEIM", "CH GEHEIM", pt=14, box=True)
    put("ch/geheim_hf_00.png", ch_content(rng), mark, degradation="skew", degrees=2.0)

    mark = both("NATO UNCLASSIFIED", "NATO UNCLASSIFIED", pt=11)
    put("nato/unclassified_00.png", nato_content(rng), mark, note="a marking, not a classification")
    mark = both("NATO RESTRICTED", "NATO RESTRICTED", pt=11)
    put("nato/restricted_00.png", nato_content(rng), mark)
    mark = both("NATO RESTRICTED", "NATO RESTRICTED")
    note = "German translation of a NATO paper"
    put("nato/restricted_de_00.png", nato_content(rng, lang="de"), mark, "times", note=note)
    mark = both("NATO RESTRICTED / OTAN DIFFUSION RESTREINTE", "NATO RESTRICTED", pt=10)
    note = "bilingual NATO marking, not FR DIFFUSION RESTREINTE"
    put("nato/restricted_bilingual_00.png", nato_content(rng), mark, note=note)
    mark = both("NATO RESTRICTED RELEASABLE TO EU", "NATO RESTRICTED")
    put("nato/restricted_rel_00.png", nato_content(rng), mark)
    mark = stamp("NATO CONFIDENTIAL", "NATO CONFIDENTIAL", angle=6, pt=16, at=(0.68, 0.045))
    put("nato/confidential_stamp_00.png", nato_content(rng), mark)
    # Inked across the letterhead: today the OCR loses the stamp and the line under it.
    mark = stamp("NATO CONFIDENTIAL", "NATO CONFIDENTIAL", angle=15, pt=18)
    note = "stamp across the letterhead"
    put("nato/confidential_stamp_01.png", nato_content(rng), mark, tags=("overlap",), note=note)
    mark = both("NATO SECRET\nRELEASABLE TO SWE", "NATO SECRET", pt=12)
    put("nato/secret_rel_00.jpg", nato_content(rng), mark, degradation="jpeg", note="two lines")
    mark = both("COSMIC TOP SECRET", "COSMIC TOP SECRET", pt=14, box=True)
    put("nato/cts_00.png", nato_content(rng), mark)
    mark = both("COSMIC TOP SECRET – ATOMAL", "COSMIC TOP SECRET", pt=12)
    put("nato/cts_atomal_00.png", nato_content(rng), mark)

    mark = both("RESTREINT UE/EU RESTRICTED", "RESTREINT UE", pt=11)
    put("eu/restreint_00.png", eu_content(rng), mark, "times")
    mark = both("RESTREINT UE/EU RESTRICTED", "RESTREINT UE", pt=10, align="right")
    put("eu/restreint_fr_00.png", eu_content(rng, lang="fr"), mark, "times")
    mark = stamp("RESTREINT UE/EU RESTRICTED", "RESTREINT UE", angle=10, pt=14)
    put("eu/restreint_stamp_00.png", eu_content(rng), mark, "times")
    mark = both("CONFIDENTIEL UE/EU CONFIDENTIAL", "CONFIDENTIEL UE", pt=11, box=True)
    put("eu/confidentiel_00.png", eu_content(rng, lang="fr"), mark, "times")
    mark = both("CONFIDENTIEL UE/EU CONFIDENTIAL", "CONFIDENTIEL UE", pt=12)
    options = {"degradation": "fax", "noise": 0.02}
    put("eu/confidentiel_fax_00.tif", eu_content(rng), mark, "times", **options)
    mark = both("SECRET UE/EU SECRET", "SECRET UE", pt=13)
    options = {"degradation": "aged", "level": 0.8}
    put("eu/secret_fr_00.jpg", eu_content(rng, lang="fr"), mark, "times", **options)
    mark = both("TRÈS SECRET UE/EU TOP SECRET", "TRÈS SECRET UE", pt=13, box=True)
    put("eu/tres_secret_00.png", eu_content(rng), mark, "times")

    # The US and UK banners are bare words; the memo around them says whose.
    put("intl/us_ts_noforn_00.png", us_content(rng), both("TOP SECRET//NOFORN", "US TOP SECRET"))
    put("intl/us_secret_00.png", us_content(rng), both("SECRET", "US SECRET", pt=12))
    mark = both("CONFIDENTIAL", "US CONFIDENTIAL", pt=12)
    put("intl/us_confidential_00.png", us_content(rng), mark)
    put("intl/us_cui_00.png", us_content(rng, cui=True), both("CUI", "US CUI", pt=12))
    mark = both("OFFICIAL-SENSITIVE", "UK OFFICIAL-SENSITIVE", pt=11)
    put("intl/uk_official_sensitive_00.png", uk_content(rng), mark)
    mark = both("DIFFUSION RESTREINTE", "FR DIFFUSION RESTREINTE", pt=11)
    put("intl/fr_dr_00.png", fr_content(rng), mark, "times")
    mark = stamp("DIFFUSION RESTREINTE", "FR DIFFUSION RESTREINTE", angle=-10, pt=16)
    put("intl/fr_dr_stamp_00.jpg", fr_content(rng), mark, "times", degradation="jpeg")


def build_tlp_company(c: Corpus) -> None:
    """TLP labels and company markings: no government level, but still markings."""
    rng = c.rng

    def put(name: str, content: Content, *marks: Mark, **options) -> None:
        pages = c.render(content, list(marks), family="arial")
        c.emit(name, pages, category=name.split("/")[0], **options)

    put("tlp/red_00.png", tlp_content(rng), tlp_label("TLP:RED"))
    mark = tlp_label("TLP:AMBER", where="header")
    put("tlp/amber_00.png", tlp_content(rng, lang="en"), mark, note="header only")
    put("tlp/amber_strict_00.png", tlp_content(rng), tlp_label("TLP:AMBER+STRICT"))
    put("tlp/green_00.png", tlp_content(rng, lang="en"), tlp_label("TLP:GREEN"))
    put("tlp/clear_00.png", tlp_content(rng), tlp_label("TLP:CLEAR"))
    mark = header("TLP:WHITE", "TLP:WHITE", pt=11, align="right")
    note = "TLP 1.0 WHITE in black text; reported as CLEAR"
    put("tlp/white_00.png", tlp_content(rng, lang="en"), mark, note=note)
    mark = both("TLP: AMBER", "TLP:AMBER", pt=11)
    put("tlp/amber_space_00.png", tlp_content(rng), mark, note="TLP 1.0 style, blank after colon")
    marks = (tlp_label("TLP:AMBER", where="header"), both("VS-NfD", "VS-NfD", pt=11))
    put("tlp/amber_vsnfd_00.png", tlp_content(rng), *marks, note="TLP label and VSA marking")

    # The same words as the government levels; the letterhead says whose they are.
    kind = "company:STRENG VERTRAULICH"
    mark = stamp("STRENG VERTRAULICH", kind, angle=8, pt=18)
    put("company/streng_vertraulich_stamp_00.png", company_content(rng), mark)
    mark = both("STRENG VERTRAULICH", kind, pt=11)
    put("company/streng_vertraulich_hf_00.png", company_content(rng), mark)
    mark = both("VERTRAULICH", "company:VERTRAULICH", pt=12, box=True)
    put("company/vertraulich_hf_00.png", company_content(rng), mark)
    mark = header("INTERN", "company:INTERN", pt=9, align="right")
    put("company/intern_head_00.png", company_content(rng), mark)
    mark = both("NUR FÜR DEN INTERNEN GEBRAUCH", "company:INTERN", pt=10, face="arial-bold")
    put("company/nur_intern_00.png", company_content(rng), mark)
    mark = both("CONFIDENTIAL", "company:CONFIDENTIAL", pt=12)
    put("company/confidential_00.png", company_content(rng, lang="en"), mark)
    mark = header("INTERNAL", "company:INTERNAL", pt=10, align="right")
    put("company/internal_00.png", company_content(rng, lang="en"), mark)
    mark = stamp("Geschäftsgeheimnis", "company:GESCHÄFTSGEHEIMNIS", angle=12, pt=20)
    put("company/geschaeftsgeheimnis_00.png", company_content(rng), mark)
    marks = [both("VERTRAULICH", "company:VERTRAULICH", pt=10)]
    pages = c.render(company_content(rng, pages=4), marks, family="arial", numbering="right")
    c.emit("company/vertraulich_4p.pdf", pages, category="company")


def build_degraded(c: Corpus) -> None:
    """Positives through the fax machine, JPEG, the shelf and a crooked scanner."""
    rng = c.rng

    def put(name: str, document: str, *marks: Mark, family="times", dpi=200, **options) -> None:
        fax_line = options.pop("fax_line", None)
        content = de_content(rng, document)
        pages = c.render(content, list(marks), family=family, dpi=dpi, fax_line=fax_line)
        c.emit(f"degraded/{name}", pages, category="de-degraded", **options)

    # Salt and pepper over 1 to 4 % of the pixels at 100 dpi: the heavy end
    # loses whole lines today, and that is what these pages are for.
    fax_line = "1{0}.03.26 09:1{0}  BUNDESAMT F. MUSTERWESEN  +49 228 99 12 3456  S.{{n}}/{{count}}"
    for index, noise in enumerate([0.01, 0.02, 0.03, 0.04]):
        name = f"fax_vsnfd_{index:02d}.{'png' if index == 2 else 'tif'}"
        marks = (both(NFD, "VS-NfD", pt=11), stamp("VS-NfD", "VS-NfD", angle=12))
        options = {"degradation": "fax", "noise": noise, "fax_line": fax_line.format(index)}
        put(name, "letter", *marks, family="arial", **options)
    content = de_content(rng, "vermerk", pages=2)
    marks = [both("VS-VERTRAULICH", "VS-VERTRAULICH", pt=13)]
    pages = c.render(content, marks, fax_line=fax_line.format(6))
    c.emit("degraded/fax_vsv_2p.pdf", pages, category="de-degraded", degradation="fax", noise=0.02)

    mark = both("VS-NfD", "VS-NfD", pt=9)
    put("jpeg_vsnfd_00.jpg", "protokoll", mark, family="arial", degradation="jpeg")
    mark = stamp("GEHEIM", "GEHEIM", angle=20, pt=24)
    note = "three rounds of JPEG at quality 30"
    put("jpeg_geheim_00.jpg", "letter", mark, degradation="jpeg", rounds=3, note=note)
    mark = both(NFD_CAPS, "VS-NfD")
    put("aged_vsnfd_00.jpg", "letter", mark, degradation="aged", level=1.0, bleed=True)
    put("skew_vsnfd_00.png", "vermerk", both(NFD, "VS-NfD"), degradation="skew", degrees=1.5)
    marks = (both("GEHEIM", "GEHEIM", pt=14), stamp("GEHEIM", "GEHEIM", angle=-15))
    put("skew_geheim_00.png", "weisung", *marks, family="arial", degradation="skew", degrees=-4.0)
    options = {"dpi": 150, "degradation": "lowdpi", "note": "150 dpi"}
    put("lowdpi_vsnfd_00.png", "letter", both("VS-NfD", "VS-NfD", pt=9), **options)
    mark = stamp("STRENG GEHEIM", "STRENG GEHEIM", angle=20)
    put("lowdpi_sg_00.png", "vermerk", mark, family="arial", **options)
    mark = stamp("VS-NfD", "VS-NfD", angle=10, color=GREY, opacity=0.55)
    note = "faint grey stamp, nothing else"
    put("faint_stamp_00.png", "letter", mark, degradation="faint", note=note)
    mark = stamp("GEHEIM", "GEHEIM", angle=-15, color=PALE_RED, pt=24)
    put("faint_stamp_01.png", "protokoll", mark, degradation="faint")
    mark = both(NFD, "VS-NfD", pt=8, color=PALE, face="arial")
    options = {"family": "arial", "degradation": "faint", "toner": 0.6}
    put("faint_header_00.png", "vermerk", mark, note="pale 8 pt marking, low toner", **options)


def build_text_layer(c: Corpus) -> None:
    """Born-digital PDFs whose text is real text, not pixels."""
    rng = c.rng

    def put(name: str, content: Content, *marks: Mark, numbering: str | None = None) -> None:
        data, pages = text_pdf(content, list(marks), numbering=numbering)
        category = "textlayer-neg" if name.startswith("neg") else "textlayer"
        c.emit(f"textlayer/{name}", pages, category=category, data=data)

    put("vsnfd_2p.pdf", de_content(rng, "letter", pages=2), both(NFD, "VS-NfD"), numbering="right")
    marks = (both("GEHEIM", "GEHEIM", pt=13), stamp("GEHEIM", "GEHEIM", angle=15, pt=22))
    put("geheim_stamp_00.pdf", de_content(rng, "vermerk"), *marks)
    put("nato_restricted_00.pdf", nato_content(rng), both("NATO RESTRICTED", "NATO RESTRICTED"))
    put("tlp_amber_00.pdf", tlp_content(rng), tlp_label("TLP:AMBER"))
    mark = both("VS-NfD", "VS-NfD", pt=11, on=(2, 3))
    put("mixed_3p.pdf", de_content(rng, "bericht", pages=3), mark, numbering="right")
    put("neg_mention_00.pdf", merkblatt())
    put("neg_invoice_00.pdf", invoice())


# ---------------------------------------------------------------- negatives


def merkblatt() -> Content:
    return page(
        """
title Merkblatt
h zur Behandlung von Verschlusssachen des Geheimhaltungsgrades
h VS-NUR FÜR DEN DIENSTGEBRAUCH (VS-NfD-Merkblatt)
gap 0.6
p 1. Verschlusssachen des Geheimhaltungsgrades VS-NUR FÜR DEN DIENSTGEBRAUCH (VS-NfD) sind so
  aufzubewahren, dass Unbefugte keinen Zugang erhalten. Außerhalb der Dienstzeit genügt ein
  verschlossener Schreibtisch oder Schrank.
p 2. Sie sind am Kopf und am Fuß jeder beschriebenen Seite mit dem Geheimhaltungsgrad zu
  kennzeichnen. Die Kennzeichnung erfolgt in der Mitte.
p 3. VS-NfD darf elektronisch nur mit zugelassenen Produkten übertragen werden. Der
  unverschlüsselte Versand über das Internet ist nicht zulässig.
p 4. Der Verlust einer Verschlusssache ist unverzüglich der oder dem Geheimschutzbeauftragten
  zu melden.
p 5. Nicht mehr benötigte Verschlusssachen des Geheimhaltungsgrades VS-NfD sind so zu
  vernichten, dass der Inhalt weder erkennbar ist noch erkennbar gemacht werden kann.
""",
        """
org Bundesministerium für Beispiele
s Geheimschutzbeauftragte
""",
    )


def invoice() -> Content:
    return page(
        """
title Rechnung Nr. 2026-04-1187
l Kunde: Stadtwerke Musterstadt, Einkauf
l Lieferdatum: 17.03.2026
gap 0.6
rowh Artikel | Menge | Preis | Summe
row Führungsschiene FS-220 | 12 | 184,50 | 2.214,00
row Kugellager 6205-2RS | 48 | 12,40 | 595,20
row Dichtring DIN 3760 | 120 | 0,85 | 102,00
gap 0.6
r Netto: 2.911,20 EUR
r Umsatzsteuer 19 %: 553,13 EUR
r Gesamtbetrag: 3.464,33 EUR
gap 0.6
p Zahlbar innerhalb von 30 Tagen ohne Abzug. Vielen Dank für Ihren Auftrag.
""",
        """
org Beispiel Werkzeugbau GmbH
s Industriestraße 14, 85748 Garching
""",
        """
s Kundennummer 40 1187
s Garching, 18.03.2026
""",
    )


#: Pages that talk about markings without carrying one, in the notation of
#: `documents`.
MENTIONS = """
=== grades_table_00.png | a table of all four levels, one per line
head org Bundesministerium für Beispiele
head s Referat Z 12
title Übersicht der Geheimhaltungsgrade
p Die Verschlusssachenanweisung unterscheidet vier Geheimhaltungsgrade. Maßgeblich ist der
  Schaden, der bei Kenntnisnahme durch Unbefugte entstehen kann:
gap 0.4
rowh Geheimhaltungsgrad | Schaden bei Kenntnisnahme
row STRENG GEHEIM | Bestand des Bundes gefährdet
row GEHEIM | Sicherheit gefährdet
row VS-VERTRAULICH | Schaden für die Interessen
row VS-NUR FÜR DEN DIENSTGEBRAUCH | nachteilig für die Interessen
gap 0.8
p Die Einstufung ist so niedrig wie möglich vorzunehmen und aufzuheben, sobald ihre
  Voraussetzungen entfallen.
=== brochure_00.png | product brochure: 'zugelassen für VS-NfD' as a heading | family=arial
head org Beispiel Secure Networks GmbH
head s Hafenstraße 4, 20457 Hamburg
big KryptoBox KX-400
c Sichere Standortvernetzung für Behörden
gap 0.8
title Zugelassen für VS-NfD
b BSI-Zulassung für VS-NUR FÜR DEN DIENSTGEBRAUCH, NATO RESTRICTED und
  RESTREINT UE/EU RESTRICTED
b Durchsatz bis 10 Gbit/s, Hochverfügbarkeit im Cluster
b Zentrales Management über die KX-Konsole
gap 0.5
p Mit der VS-NfD-Zulassung des BSI verbinden Sie Ihre Standorte ohne zusätzliche Maßnahmen.
  Fordern Sie jetzt Ihr Angebot an.
=== aufhebung_00.png | letter lifting a GEHEIM classification
head org Bundesministerium für Beispiele
head s Referat Z 12
l Bundesamt für Ausrüstung (Musteramt)
l Referat K 4
l Beispielallee 1
l 56073 Koblenz
gap 1.0
h Betreff: Aufhebung einer Einstufung
l Bezug: Studie „Standortkonzept Nord“ vom 4. Mai 2011
gap 0.6
l Sehr geehrte Damen und Herren,
p nach erneuter Prüfung ist die Einstufung der oben genannten Studie als GEHEIM aufzuheben.
  Die Unterlagen sind künftig offen zu behandeln.
p Bitte streichen Sie die Kennzeichnung auf allen Ausfertigungen durch und versehen Sie sie
  mit einem Aufhebungsvermerk. Der Registratur ist die Aufhebung bis zum 30. April 2026
  anzuzeigen.
l Mit freundlichen Grüßen
l Im Auftrag
gap 0.8
l Dr. Albers
=== dienstvorschrift_00.png | regulation text naming every level | numbering=right
head org Zentralvorschrift Musterwesen
head s A-1130/99, Stand März 2026
title Teil 3 – Materieller Geheimschutz
h 301. Aufbewahrung
p Verschlusssachen des Geheimhaltungsgrades VS-VERTRAULICH und höher sind in Verwahrgelassen
  aufzubewahren, die für den jeweiligen Geheimhaltungsgrad zugelassen sind.
h 302. Weitergabe
p Verschlusssachen der Geheimhaltungsgrade GEHEIM und STRENG GEHEIM dürfen nur gegen
  Empfangsbescheinigung weitergegeben werden. Bei VS-NUR FÜR DEN DIENSTGEBRAUCH ist ein
  Nachweis nicht erforderlich.
h 303. Vervielfältigung
p Ausfertigungen von Verschlusssachen ab VS-VERTRAULICH sind zu nummerieren. Die Zahl der
  Ausfertigungen ist auf das unbedingt Notwendige zu beschränken.
=== email_00.png | e-mail printout, VS-NfD in the subject line | family=arial
org Weber, Anna
gap 0.4
l Von: Schuster, Thomas
l Gesendet: Montag, 9. März 2026 10:14
l An: Weber, Anna
h Betreff: AW: Rückgabe VS-NfD-Laptop
gap 0.6
p Hallo Frau Weber,
p der Laptop kann am Donnerstag zwischen 9 und 12 Uhr in Raum 2.114 abgegeben werden. Bitte
  bringen Sie das Übergabeprotokoll und das Netzteil mit.
p Viele Grüße, Thomas Schuster
s -----Ursprüngliche Nachricht-----
l Von: Weber, Anna
l Betreff: Rückgabe VS-NfD-Laptop
p Hallo Herr Schuster, wann kann ich das Gerät zurückgeben? Mein Projekt endet am Freitag.
=== jobad_00.png | job advert | family=arial
head org Bundesamt für Musterwesen
title Stellenausschreibung
h Sachbearbeitung Geheimschutz (m/w/d), Entgeltgruppe 9b TVöD
p Wir suchen zum nächstmöglichen Zeitpunkt eine engagierte Persönlichkeit für unser Referat
  Z 12 in Bonn.
h Ihre Aufgaben
b Verwaltung und Nachweis von Verschlusssachen bis zum Geheimhaltungsgrad GEHEIM
b Beratung der Beschäftigten zu allen Fragen des Geheimschutzes
h Ihr Profil
b abgeschlossene Ausbildung für den gehobenen Dienst oder vergleichbar
b Bereitschaft zur erweiterten Sicherheitsüberprüfung (Ü2)
p Bewerbungen richten Sie bitte bis zum 30. April 2026 an das Referat Z 12.
=== company_policy_00.png | company policy listing its own classes | family=arial
head org MUSTER MASCHINENBAU GmbH
head s Industriestraße 14 · 85748 Garching
title Richtlinie Informationsklassifizierung
p Alle Informationen des Unternehmens sind einer von vier Schutzklassen zuzuordnen. Die
  Einstufung nimmt die Informationseigentümerin oder der Informationseigentümer vor.
rowh Schutzklasse | Wer darf die Information sehen?
row Öffentlich | jede Person
row Intern | alle Beschäftigten
row Vertraulich | ein festgelegter Personenkreis
row Streng vertraulich | nur namentlich benannte Personen
gap 0.8
p Dokumente ab der Schutzklasse Vertraulich werden in der Kopfzeile gekennzeichnet und nur
  verschlüsselt versendet.
=== invoice_krypto_00.png | invoice line item 'VS-NfD-zugelassen' | family=arial pt=10
head org Beispiel Secure Networks GmbH
head s Hafenstraße 4, 20457 Hamburg
title Rechnung Nr. 2026-0318
l Kunde: Bundesamt für Musterwesen, Referat IT 3
gap 0.5
rowh Artikel | Menge | Preis | Summe
row Kryptohandy KX-2 (VS-NfD-zugelassen) | 12 | 1.290,00 | 15.480,00
row Ladegerät KX-L | 12 | 39,00 | 468,00
row Einrichtung und Schulung | 1 | 850,00 | 850,00
gap 0.5
r Netto: 16.798,00 EUR
r USt 19 %: 3.191,62 EUR
r Gesamt: 19.989,62 EUR
=== agenda_00.png | agenda item naming VS-NfD
head org Bundesministerium für Beispiele
head s Referat Z 12
title Tagesordnung
l 7. Sitzung des IT-Rates am 16. April 2026, 14:00 Uhr
gap 0.8
h TOP 1 Genehmigung des Protokolls der 6. Sitzung
h TOP 2 Sachstand Netzmodernisierung
h TOP 3 Umstellung der VS-NfD-Kommunikation auf den neuen Messenger
p Berichterstattung: Referat IT 3. Ein Beschlussvorschlag liegt als Tischvorlage vor.
h TOP 4 Haushalt 2027
h TOP 5 Verschiedenes
=== news_00.png | newspaper article quoting markings | family=serif
head org Musterstädter Anzeiger
head s Samstag, 14. März 2026
title Akten aus den Sechzigerjahren freigegeben
p Das Archiv hat am Freitag mehr als 4.000 Seiten aus den Beständen des früheren Ministeriums
  freigegeben. Ein Teil der Unterlagen war jahrzehntelang als „streng geheim“ eingestuft.
p Historiker sprechen von einem wichtigen Schritt. Die Dokumente zeigen, wie die Planungen für
  den Ernstfall aussahen – bis hin zu Listen mit Ausweichquartieren.
p Einige Blätter tragen noch den Stempel „VS-Vertraulich“, andere sind nur als Dienstsache
  gekennzeichnet. Die Akten liegen ab Montag im Lesesaal aus.
=== nato_policy_00.png | course handout naming every NATO level | family=arial
head org EXAMPLE DEFENCE ACADEMY
head s Security Awareness Course – Handout 3
title Handling classified information
p NATO information is classified at four levels: COSMIC TOP SECRET, NATO SECRET, NATO
  CONFIDENTIAL and NATO RESTRICTED. Information marked NATO UNCLASSIFIED is not classified but
  may only be used for official purposes.
p Documents classified NATO CONFIDENTIAL and above must be registered and may only be handled
  by personnel holding a valid security clearance.
p NATO RESTRICTED information may be processed on accredited systems and sent by encrypted
  e-mail.
p The originator decides on the classification and is the only authority that may downgrade
  or declassify the information.
=== eu_rules_00.png | summary of the EU levels
head org WORKING PARTY ON SECURITY PROCEDURES
head s Information note
title Security rules for EU classified information – summary
p EU classified information (EUCI) is classified at one of four levels: TRÈS SECRET UE/EU TOP
  SECRET, SECRET UE/EU SECRET, CONFIDENTIEL UE/EU CONFIDENTIAL and RESTREINT UE/EU RESTRICTED.
p The classification marking is applied at the top and bottom centre of every page. Documents
  classified CONFIDENTIEL UE/EU CONFIDENTIAL and above are registered.
p Information at RESTREINT UE/EU RESTRICTED level may be handled on systems accredited for that
  level.
=== tlp_legend_00.png | TLP legend: every label listed, none applied | family=arial
head org CERT Musterland
head s Informationsblatt
title Traffic Light Protocol (TLP) 2.0
p Das TLP regelt, an wen empfangene Informationen weitergegeben werden dürfen. Die
  Kennzeichnung steht rechts oben auf jeder Seite.
rowh Kennzeichnung | Weitergabe
row TLP:RED | nur an die adressierten Personen
row TLP:AMBER+STRICT | nur in der eigenen Organisation
row TLP:AMBER | Organisation und deren Kunden
row TLP:GREEN | innerhalb der Community
row TLP:CLEAR | ohne Beschränkung
=== at_infosig_00.png | Austrian information sheet naming the levels | family=arial
head org REPUBLIK ÖSTERREICH
head org2 Bundesministerium für Musterangelegenheiten
head s Musterplatz 3, 1010 Wien
title Informationsblatt: Klassifizierte Informationen
p Das Informationssicherheitsgesetz unterscheidet vier Klassifizierungsstufen: EINGESCHRÄNKT,
  VERTRAULICH, GEHEIM und STRENG GEHEIM.
p Informationen der Stufe EINGESCHRÄNKT dürfen nur Personen zugänglich gemacht werden, die sie
  für ihre Tätigkeit benötigen. Ab der Stufe VERTRAULICH ist eine Sicherheitsüberprüfung
  erforderlich.
p Die Klassifizierungsstufe ist auf jeder Seite oben und unten anzubringen.
=== ch_ischv_00.png | Swiss information sheet naming the levels | family=arial
head org2 Schweizerische Eidgenossenschaft
head s Confédération suisse
head s Confederazione Svizzera
head s Confederaziun svizra
title Merkblatt Informationsschutz
p Gemäss Informationsschutzverordnung werden schutzwürdige Informationen als INTERN,
  VERTRAULICH oder GEHEIM klassifiziert.
p Als INTERN gelten Informationen, deren Kenntnisnahme durch Unbefugte den Interessen des
  Landes Nachteile zufügen kann. VERTRAULICH und GEHEIM werden bei erheblichem
  beziehungsweise schwerem Schaden verwendet.
p Die Klassifizierung wird auf jeder Seite gut sichtbar angebracht.
=== findbuch_00.png | archive finding aid: former levels in a table column | family=arial
head org Bundesarchiv Musterstadt
head s Findbuch Bestand B 141 – Standortplanung
title Findbuch B 141
p Die folgenden Akten sind nach Ablauf der Schutzfristen freigegeben. Die frühere Einstufung
  ist zur Information angegeben.
rowh Signatur | Titel | frühere Einstufung
row B 141/4711 | Standortkonzept Nord | VS-Vertraulich
row B 141/4712 | Alarmplanung 1968 | GEHEIM
row B 141/4713 | Fernmeldenetz Süd | VS-NfD
row B 141/4714 | Haushalt 1969 | offen
"""

#: Everyday words that look like, or contain, a marking term.
WORDS = """
=== pin_00.png | Geheimzahl | family=arial
head org Musterstädter Volksbank eG
head s Postfach 12 34, 80333 München
l Frau Anna Weber
l Lindenweg 3
l 80331 München
gap 1.0
title Ihre persönliche Geheimzahl (PIN)
l Sehr geehrte Frau Weber,
p anbei erhalten Sie die Geheimzahl zu Ihrer neuen Girocard mit der Endnummer 4711. Bitte
  lernen Sie die Geheimzahl auswendig und vernichten Sie dieses Schreiben.
p Bewahren Sie die Geheimzahl niemals zusammen mit der Karte auf. Wir werden Sie niemals nach
  Ihrer Geheimzahl fragen.
l Mit freundlichen Grüßen
l Ihre Volksbank
=== wahl_00.png | geheime Wahl | family=arial
head org Stadt Musterstadt – Wahlamt
title Wahlbenachrichtigung
l zur Wahl des Stadtrates am 26. April 2026
gap 0.5
l Sehr geehrte Wählerin, sehr geehrter Wähler,
p Sie sind in das Wählerverzeichnis eingetragen und können im Wahlraum Grundschule am Park,
  Parkstraße 3, wählen. Der Wahlraum ist von 8 bis 18 Uhr geöffnet.
p Die Wahl ist allgemein, unmittelbar, frei, gleich und geheim. Bitte bringen Sie diese
  Benachrichtigung und Ihren Personalausweis mit.
p Die geheime Stimmabgabe ist durch Wahlkabinen gewährleistet. Wenn Sie Briefwahl beantragen
  möchten, füllen Sie bitte die Rückseite aus.
=== party_00.png | 'Streng geheim!' in a sentence | family=serif
title Einladung
l Liebe Freundinnen und Freunde,
p am Samstag, den 18. April, wird Klaus 50 Jahre alt. Wir feiern ab 19 Uhr im Gasthaus Zur
  Linde. Streng geheim! Klaus darf auf keinen Fall davon erfahren – er glaubt, wir gehen nur
  essen.
p Bitte gebt bis zum 10. April Bescheid, ob ihr kommt. Für Musik ist gesorgt, Geschenke sind
  nicht nötig.
l Herzliche Grüße, Sabine und Jonas
=== party_01.png | large 'streng geheim!' heading on a party invitation | family=arial
gap 1.0
big Pssst … streng geheim!
c Überraschungsparty für Oma Hilde
c Sonntag, 3. Mai 2026, 15 Uhr, Café am Markt
gap 1.0
p Oma Hilde wird 80! Wir wollen sie mit Kaffee, Kuchen und allen Enkeln überraschen. Bitte
  verratet nichts und seid um Viertel vor drei da.
p Anmeldung bei Petra bis zum 25. April.
=== geheimtipp_00.png | Geheimtipp | family=serif
head org Genuss-Magazin Musterstadt
title Unser Geheimtipp für den Frühling
p Etwas versteckt in einer Seitenstraße der Altstadt liegt das kleine Café Kranich. Wer einmal
  den Rhabarberkuchen probiert hat, kommt wieder.
p Der Geheimtipp hat sich inzwischen herumgesprochen: Am Wochenende sollten Sie reservieren.
  Geöffnet ist täglich außer montags von 9 bis 18 Uhr.
=== geheimrat_00.png | GEHEIMRATSECKEN, Geheimrat | family=arial
head org Friseursalon Haarmonie
gap 0.6
big GEHEIMRATSECKEN?
c Wir beraten Sie gern – diskret und kompetent.
gap 0.8
p Ob Haarverdichtung, neuer Schnitt oder Pflegekur: Vereinbaren Sie einen Termin für ein
  kostenloses Beratungsgespräch.
h Schon gewusst?
p Der Begriff geht auf die Geheimräte des 19. Jahrhunderts zurück. Geheimrat Goethe soll
  allerdings volles Haar gehabt haben.
=== versus_00.png | VS. and VERSUS | family=arial
head org TSV Musterstadt 1890 e. V.
big FINALE
title FC MUSTERSTADT VS. SV BEISPIEL
c Samstag, 16. Mai 2026, Anstoß 15:30 Uhr
gap 0.8
p Heimmannschaft vs. Gäste: Im letzten Duell der Saison geht es um den Aufstieg. Karten gibt es
  an der Tageskasse.
p Im Halbfinale hatte Musterstadt das Spiel VERSUS Nordhausen erst im Elfmeterschießen
  gewonnen.
=== vst_00.png | VSt | family=arial
head org Steuerberatung Lange & Partner
title Umsatzsteuer-Voranmeldung März 2026
gap 0.4
rowh Position | Betrag
row Umsatzsteuer 19 % | 8.412,50 EUR
row abziehbare Vorsteuer (VSt) | 5.130,20 EUR
row VSt aus EU-Erwerb | 412,00 EUR
row Zahllast | 2.870,30 EUR
gap 0.6
p Die VSt-Beträge wurden mit den Eingangsrechnungen abgestimmt. Bitte prüfen Sie die
  Aufstellung bis zum 8. April.
=== nfd_logistik_00.png | company called NFD | family=arial
head org NFD Logistik GmbH
head s Hafenweg 12 · 28197 Bremen
l Zentrale Musterstelle des Bundes
l Sachgebiet 21
l Postfach 11 22
l 65189 Wiesbaden
gap 1.0
h Ihre Sendung 00340434161094042557
l Sehr geehrte Damen und Herren,
p die NFD Logistik GmbH hat Ihre Sendung am 12. März 2026 übernommen. Die Zustellung erfolgt
  voraussichtlich am 14. März zwischen 8 und 12 Uhr.
p Bei Fragen erreichen Sie den NFD-Kundenservice unter 0421 123 456.
l Freundliche Grüße, Ihr NFD-Team
=== internat_00.png | Internat, Internet, international | family=serif
head org Internat Schloss Beispiel
title Anmeldung zum Schuljahr 2026/27
p Unser Internat bietet 120 Plätze für Schülerinnen und Schüler ab Klasse 5. Alle Zimmer haben
  einen Internetzugang, das Lernzentrum ist bis 21 Uhr geöffnet.
p Die internationale Klasse wird zweisprachig unterrichtet. Informationen zu Kosten und
  Stipendien finden Sie im Internet.
=== fuhrpark_00.png | 'nur für den Dienstgebrauch' inside a sentence
head org Stadtverwaltung Musterstadt
head s Hauptamt – Fuhrpark
title Dienstanweisung Fuhrpark
p 1. Die Dienstfahrzeuge der Stadtverwaltung sind nur für den Dienstgebrauch bestimmt.
  Privatfahrten sind untersagt.
p 2. Fahrtenbücher sind vollständig zu führen und monatlich beim Fuhrpark abzugeben.
p 3. Schäden sind unverzüglich zu melden. Die Fahrzeugschlüssel werden an der Pforte
  ausgegeben.
=== bewerbung_00.png | 'vertraulich behandeln' and 'intern' in sentences | family=arial
head org Muster Maschinenbau GmbH
title Ihre Bewerbung als Industriemechaniker
l Sehr geehrter Herr Yilmaz,
p vielen Dank für Ihre Bewerbung. Selbstverständlich werden wir Ihre Unterlagen vertraulich
  behandeln und nach Abschluss des Verfahrens löschen.
p Die Stelle wird zunächst intern ausgeschrieben; wir melden uns spätestens bis zum 30. April
  bei Ihnen.
l Mit freundlichen Grüßen
l Personalabteilung
=== brauerei_00.png | Betriebsgeheimnis | family=serif
head org Brauerei Beispiel seit 1892
title Führung durch die Brauerei
p Seit 1892 brauen wir unser Helles nach demselben Rezept. Die genaue Hopfenmischung ist bis
  heute ein gut gehütetes Betriebsgeheimnis der Familie.
p Bei der Führung sehen Sie Sudhaus, Gärkeller und Abfüllung. Im Anschluss gibt es eine
  Verkostung.
=== secret_santa_00.png | secret, internal | family=arial
head org Northfield Instruments Ltd – Social Committee
title Secret Santa 2026
p This year's Secret Santa gift exchange takes place on Friday 18 December in the canteen. The
  spending limit is 15 pounds.
p Please keep the name you draw secret until the day! Wrapping paper is available at
  reception.
p Volunteers are needed to move the internal combustion engine exhibit out of the lobby before
  the party.
=== soiree_00.png | secret in French | family=serif
title Soirée surprise
p Chut, c'est un secret ! Nous organisons une fête pour le départ en retraite de Martine le
  vendredi 12 juin à 18 heures.
p La diffusion de cette invitation est limitée aux collègues du service. Merci de confirmer
  votre présence avant le 5 juin.
"""

#: Training slides, 16:9: the levels in the largest type on any page.
SLIDES = [
    """
gap 2.0
big Geheimschutz kompakt
c Pflichtschulung 2026 – Modul 2
c Referat Z 12, Geheimschutzbeauftragte
""",
    """
title Die vier Geheimhaltungsgrade
gap 0.5
b STRENG GEHEIM – Bestand des Staates gefährdet
b GEHEIM – Sicherheit gefährdet
b VS-VERTRAULICH – Schaden für die Interessen
b VS-NUR FÜR DEN DIENSTGEBRAUCH – nachteilig
""",
    """
title Kennzeichnung
gap 0.5
b Geheimhaltungsgrad oben und unten mittig auf jeder Seite
b Bei VS-NfD genügt ein Aufdruck in Schwarz
b Fragen? Die Geheimschutzbeauftragte hilft weiter
""",
]


def build_negatives(c: Corpus) -> None:
    """Pages that must produce no marking at all."""
    rng = c.rng

    def put(name: str, content: Content, *marks: Mark, family="times", **options) -> None:
        render = {
            key: options.pop(key)
            for key in ("pt", "dpi", "numbering", "fax_line")
            if key in options
        }
        pages = c.render(content, list(marks), family=family, **render)
        c.emit(f"neg/{name}", pages, category="neg-" + name.split("/")[0], **options)

    put("mention/merkblatt_00.png", merkblatt(), note="VSA handout: the level named in a heading")
    for name, note, options, content in documents(MENTIONS):
        put(f"mention/{name}", content, note=note, **options)
    slides = [blocks(text) for text in SLIDES]
    content = Content(slides, size=(10.0, 5.625), top=0.45, bottom=0.35, left=0.7, right=0.7)
    put("mention/slides_3p.pdf", content, family="arial", pt=20, dpi=160, note="training slides")
    # The sentence a detector must not take for a marking, where a marking would be.
    mark = footer("Dieses Dokument ist nicht als Verschlusssache eingestuft.", None, pt=8)
    note = "'nicht als Verschlusssache eingestuft' in the footer"
    put("mention/nicht_eingestuft_00.png", de_content(rng, "letter"), mark, note=note)

    for name, note, options, content in documents(WORDS):
        put(f"words/{name}", content, note=note, **options)

    put("plain/invoice_00.png", invoice(), family="arial")
    put("plain/letter_00.png", de_content(rng, "letter"))
    put("plain/vermerk_00.png", de_content(rng, "vermerk"), family="arial")
    put("plain/bericht_00.png", de_content(rng, "bericht"))
    put("plain/vertrag_00.png", de_content(rng, "vertrag"))
    put("plain/at_letter_00.png", at_content(rng), family="arial")
    put("plain/ch_letter_00.png", ch_content(rng), family="arial")
    put("plain/nato_memo_00.png", nato_content(rng), family="arial")
    put("plain/company_00.png", company_content(rng), family="arial")
    put("plain/protokoll_4p.pdf", de_content(rng, "protokoll", pages=4), numbering="right")
    put("plain/vertrag_3p.tif", de_content(rng, "vertrag", pages=3), numbering="center")
    fax = {"degradation": "fax", "noise": 0.02}
    line = "11.03.26 08:55  STADTWERKE  +49 89 1234 567  S.{n}/{count}"
    put("plain/fax_letter_00.tif", de_content(rng, "letter"), family="arial", fax_line=line, **fax)
    put("plain/aged_vermerk_00.jpg", de_content(rng, "vermerk"), degradation="aged", level=0.9)
    put("plain/jpeg_letter_00.jpg", de_content(rng, "letter"), family="arial", degradation="jpeg")
    # The layouts make_corpus.py already draws: a ruled form and a till receipt.
    for name, spec in (("form_00.png", form_page(dpi=200)), ("receipt_00.png", receipt_page())):
        c.emit(f"neg/plain/{name}", [Page(spec.image, 200, [], spec.lines)], category="neg-plain")
    blank = [Page(Sheet(200).image, 200, [], [])]
    c.emit("neg/plain/blank_00.png", blank, category="neg-plain", note="an empty page")

    # Stamps that are not a classification at all.
    note = "not a classification"
    put(
        "stamp/entwurf_00.png",
        de_content(rng, "vermerk"),
        stamp("ENTWURF", None, angle=15, pt=26),
        note=note,
    )
    put(
        "stamp/kopie_00.png",
        de_content(rng, "letter"),
        stamp("KOPIE", None, color=BLUE, pt=24),
        note=note,
    )
    mark = stamp(
        "MUSTER", None, color=PALE, pt=110, angle=35, box=False, opacity=0.5, at=(0.5, 0.5)
    )
    put("stamp/muster_00.png", de_content(rng, "vertrag"), mark, note="diagonal watermark")
    mark = stamp("EILT!\nSofort vorlegen", None, angle=-5, pt=18)
    put("stamp/eilt_00.png", de_content(rng, "letter"), mark, note=note)
    mark = stamp(
        "EINGEGANGEN\n12. MRZ. 2026\nReferat Z 12", None, color=BLUE, pt=12, at=(0.72, 0.2)
    )
    put("stamp/eingang_00.png", de_content(rng, "letter"), mark, note="Eingangsstempel")
    mark = stamp("BEZAHLT\n24.03.2026", None, angle=10, pt=20, at=(0.6, 0.6))
    put("stamp/bezahlt_00.png", invoice(), mark, family="arial", note=note)
    put("stamp/draft_00.png", nato_content(rng), stamp("DRAFT", None, angle=20, pt=40), note=note)
    mark = header("Vorab per Fax: 0228 99 12-3499", None, pt=11, align="left", face="arial-bold")
    put(
        "stamp/vorab_fax_00.png", de_content(rng, "letter"), mark, note="header line, not a marking"
    )
    mark = stamp("DUPLIKAT", None, color=BLACK, angle=-10, pt=22)
    put("stamp/duplikat_00.png", invoice(), mark, family="arial", note=note)


def build(root: Path, seed: int) -> Corpus:
    corpus = Corpus(root, seed)
    for section in (
        build_de,
        build_de_multipage,
        build_other_schemes,
        build_tlp_company,
        build_degraded,
        build_text_layer,
        build_negatives,
    ):
        print(f"{section.__doc__.splitlines()[0]} ...")
        section(corpus)
    return corpus


def summarize(files: list[dict]) -> None:
    positives = sum(1 for entry in files if entry["level"] > 0)
    marked = sum(1 for entry in files if entry["label"] or entry["tlp"] or entry["company"])
    negatives = len(files) - marked
    pages = sum(len(entry["pages"]) for entry in files)
    print(f"{len(files)} files, {pages} pages")
    print(f"  classified (level >= 1): {positives}")
    print(f"  carrying any marking:    {marked}")
    print(f"  no marking at all:       {negatives} ({negatives / len(files):.0%})")
    for key in ("category", "scheme", "degradation"):
        counts = Counter(entry[key] for entry in files)
        print(f"  by {key}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("/tmp/vs/corpus"))
    parser.add_argument("--seed", type=int, default=20260923)
    parser.add_argument(
        "--force", action="store_true", help="replace an earlier corpus in --out, nothing else"
    )
    args = parser.parse_args()

    if args.out.exists() and any(args.out.iterdir()):
        # Only ever delete something this script wrote.
        if not (args.force and (args.out / "ground_truth.json").exists()):
            raise SystemExit(f"{args.out} is not empty; remove it first or pass --force")
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True, exist_ok=True)
    corpus = build(args.out, args.seed)
    path = corpus.write_manifest()
    summarize(corpus.files)
    print(f"manifest -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
