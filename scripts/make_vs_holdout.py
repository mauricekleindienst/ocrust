#!/usr/bin/env python3
"""Independent held-out set for the classification-marking detector.

    python scripts/make_vs_holdout.py --out /tmp/vs/holdout
    python scripts/evaluate_vs.py /tmp/vs/holdout -o /tmp/vs/holdout_report.json

The documents were designed without looking at the detector or at the corpus it
was tuned on, so the harness numbers on this set estimate how the detector does
on documents it has never seen. Each document has a template of its own:
printed Outlook e-mails, minutes with tables, two-column bulletins, slide
handouts, forms with checkboxes, letters from German, Austrian, Swiss, French,
British and US offices, a Bundeswehr "Weisung", NATO memos, EU Council notes,
CERT advisories, board papers, contracts and fax cover sheets; the negatives
are policy handouts, product sheets, job ads, news pages, invoices, recipes and
the like that talk about grades, or merely contain their letters, without
carrying one.

Labelling conventions of ``ground_truth.json``:

* ``level`` / ``label``: the highest government grade marked in the document.
  ``pages`` holds, per page, the government level marked on that page (0 for a
  page without a government marking). TLP and company markings are level 0 and
  go in ``tlp`` / ``company``.
* ``mentions``: running text names a grade or a marking as such ("zugelassen
  bis VS-NfD", "diese E-Mail ist vertraulich", "streng geheime Akten", the
  unticked options of a form). Ordinary words that merely contain the letters
  of a grade (Geheimzahl, Internet, Internat, geheime Wahl, Secret Santa,
  VS-Bericht, "Zufahrt eingeschränkt") do not count.
* A form whose ticked box is "offen" is unclassified; a specimen marking under
  a MUSTER stamp is not a marking; "Persönlich" in an address block is not a
  marking (the set avoids "Persönlich/Vertraulich" altogether); a US
  UNCLASSIFIED banner is level 0 without a label.
* Austrian and Swiss documents show their origin in the letterhead; VERTRAULICH
  or INTERN on a German or English company paper is a company marking.

Everything is seeded from the file name: a second run writes the same files.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import zlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageSequence

# ---------------------------------------------------------------- colours and fonts

WHITE = (255, 255, 255)
INK = (22, 22, 24)
GREY = (110, 110, 115)
LIGHT = (228, 230, 234)
RED = (196, 22, 28)
STAMP_RED = (186, 32, 48)
BLUE = (22, 64, 150)
STAMP_BLUE = (38, 72, 176)
NAVY = (16, 38, 92)
PURPLE = (104, 46, 142)
GREEN = (18, 118, 62)
TEAL = (0, 110, 120)
GOLD = (255, 204, 0)

FONT_FILES = {
    "sans": ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"),
    "arial": (
        "LiberationSans-Regular.ttf",
        "LiberationSans-Bold.ttf",
        "LiberationSans-Italic.ttf",
    ),
    "helv": ("FreeSans.ttf", "FreeSansBold.ttf", "FreeSansOblique.ttf"),
    "serif": ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf", "DejaVuSerif.ttf"),
    "times": (
        "LiberationSerif-Regular.ttf",
        "LiberationSerif-Bold.ttf",
        "LiberationSerif-Italic.ttf",
    ),
    "garamond": ("FreeSerif.ttf", "FreeSerifBold.ttf", "FreeSerifItalic.ttf"),
    "courier": (
        "LiberationMono-Regular.ttf",
        "LiberationMono-Bold.ttf",
        "LiberationMono-Italic.ttf",
    ),
    "typewriter": ("FreeMono.ttf", "FreeMonoBold.ttf", "FreeMonoOblique.ttf"),
    "mono": ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf", "DejaVuSansMono-Oblique.ttf"),
    "round": ("Loma.otf", "Loma-Bold.otf", "Loma-Oblique.otf"),
}
STYLES = {"r": 0, "b": 1, "i": 2}
FONT_ROOTS = (Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts")


@cache
def font_index() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for root in FONT_ROOTS:
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.suffix.lower() in (".ttf", ".otf"):
                    found.setdefault(path.name, path)
    return found


@cache
def get_font(family: str, style: str, px: int) -> ImageFont.FreeTypeFont:
    names = FONT_FILES[family]
    index = font_index()
    path = index.get(names[STYLES[style]]) or index.get(names[0]) or index.get("DejaVuSans.ttf")
    if path is None:
        raise SystemExit("no usable TrueType font found; install fonts-dejavu")
    return ImageFont.truetype(str(path), px)


def text_block(s: str) -> list[str]:
    """Paragraphs of a triple-quoted block: blank lines separate, whitespace collapses."""
    return [" ".join(chunk.split()) for chunk in s.strip().split("\n\n") if chunk.strip()]


def one(s: str) -> str:
    return " ".join(s.split())


def swiss(s: str) -> str:
    return s.replace("ß", "ss")


# ---------------------------------------------------------------- the page


MM_PER_INCH = 25.4


class Page:
    """One A4 page; coordinates in millimetres from the top left corner."""

    def __init__(self, dpi: int, paper: tuple[int, int, int] = WHITE) -> None:
        self.dpi = dpi
        self.img = Image.new("RGB", (self.px(210), self.px(297)), paper)
        self.draw = ImageDraw.Draw(self.img)
        self.level = 0

    def px(self, mm: float) -> int:
        return round(mm * self.dpi / MM_PER_INCH)

    def font(self, family: str, pt: float, style: str = "r") -> ImageFont.FreeTypeFont:
        return get_font(family, style, max(6, round(pt * self.dpi / 72)))

    def width(self, s: str, pt: float, family: str = "arial", style: str = "r", track=0.0):
        font = self.font(family, pt, style)
        extra = track * pt * self.dpi / 72 * max(0, len(s) - 1)
        return (font.getlength(s) + extra) * MM_PER_INCH / self.dpi

    def text(
        self,
        x: float,
        y: float,
        s: str,
        pt: float = 10.5,
        family: str = "arial",
        style: str = "r",
        fill=INK,
        align: str = "l",
        track: float = 0.0,
    ) -> float:
        """Draws one line with its top at `y`; returns its width in mm."""
        font = self.font(family, pt, style)
        w = self.width(s, pt, family, style, track)
        if align == "c":
            x -= w / 2
        elif align == "r":
            x -= w
        if track:
            cx = float(self.px(x))
            step = track * pt * self.dpi / 72
            for ch in s:
                self.draw.text((cx, self.px(y)), ch, font=font, fill=fill)
                cx += font.getlength(ch) + step
        else:
            self.draw.text((self.px(x), self.px(y)), s, font=font, fill=fill)
        return w

    def wrap(self, s: str, w: float, pt: float, family: str = "arial", style: str = "r"):
        font = self.font(family, pt, style)
        limit = self.px(w)
        out: list[str] = []
        for block in s.split("\n"):
            line: list[str] = []
            for word in block.split():
                if line and font.getlength(" ".join([*line, word])) > limit:
                    out.append(" ".join(line))
                    line = [word]
                else:
                    line.append(word)
            out.append(" ".join(line))
        return out

    def para(
        self,
        x: float,
        y: float,
        w: float,
        s: str,
        pt: float = 10.5,
        family: str = "arial",
        style: str = "r",
        fill=INK,
        lead: float = 1.38,
        justify: bool = False,
    ) -> float:
        """A wrapped paragraph; returns the y below it."""
        font = self.font(family, pt, style)
        step = pt * lead * MM_PER_INCH / 72
        lines = self.wrap(s, w, pt, family, style)
        for i, line in enumerate(lines):
            words = line.split()
            last = i == len(lines) - 1 or (i + 1 < len(lines) and not lines[i + 1])
            if justify and not last and len(words) > 1:
                total = sum(font.getlength(word) for word in words)
                gap = (self.px(w) - total) / (len(words) - 1)
                cx = float(self.px(x))
                for word in words:
                    self.draw.text((cx, self.px(y)), word, font=font, fill=fill)
                    cx += font.getlength(word) + gap
            else:
                self.draw.text((self.px(x), self.px(y)), line, font=font, fill=fill)
            y += step
        return y

    def paras(self, x, y, w, paragraphs, pt=10.5, family="arial", gap=2.2, **kw) -> float:
        for p in paragraphs:
            y = self.para(x, y, w, p, pt, family, **kw) + gap
        return y

    def bullets(
        self, x, y, w, items, pt=10.5, family="arial", mark="•", indent=5.0, gap=1.0, **kw
    ) -> float:
        for item in items:
            self.text(x, y, mark, pt, family, kw.get("style", "r"), kw.get("fill", INK))
            y = self.para(x + indent, y, w - indent, item, pt, family, **kw) + gap
        return y

    def hline(self, x1, x2, y, width=0.3, fill=INK) -> None:
        self.draw.line(
            [(self.px(x1), self.px(y)), (self.px(x2), self.px(y))],
            fill=fill,
            width=max(1, self.px(width)),
        )

    def vline(self, x, y1, y2, width=0.3, fill=INK) -> None:
        self.draw.line(
            [(self.px(x), self.px(y1)), (self.px(x), self.px(y2))],
            fill=fill,
            width=max(1, self.px(width)),
        )

    def rect(self, x, y, w, h, outline=INK, fill=None, width=0.3, radius=0.0) -> None:
        box = [self.px(x), self.px(y), self.px(x + w), self.px(y + h)]
        stroke = max(1, self.px(width)) if outline is not None else 0
        if radius:
            self.draw.rounded_rectangle(
                box, radius=self.px(radius), outline=outline, fill=fill, width=stroke
            )
        else:
            self.draw.rectangle(box, outline=outline, fill=fill, width=stroke)

    def checkbox(self, x, y, size=3.4, checked=False, fill=INK) -> None:
        self.rect(x, y, size, size, outline=fill, width=0.25)
        if checked:
            m = size * 0.18
            width = max(1, self.px(0.35))
            for a, b in (
                ((x + m, y + m), (x + size - m, y + size - m)),
                ((x + m, y + size - m), (x + size - m, y + m)),
            ):
                self.draw.line(
                    [(self.px(a[0]), self.px(a[1])), (self.px(b[0]), self.px(b[1]))],
                    fill=fill,
                    width=width,
                )

    def table(
        self,
        x: float,
        y: float,
        widths: list[float],
        rows: list[list[str]],
        pt: float = 9.0,
        family: str = "arial",
        header: bool = True,
        head_fill=LIGHT,
        lead: float = 1.25,
        pad: float = 1.3,
        border=INK,
        rule: float = 0.25,
        grid: bool = True,
        bold_cols: tuple[int, ...] = (),
    ) -> float:
        step = pt * lead * MM_PER_INCH / 72
        for r, row in enumerate(rows):
            head = header and r == 0
            cells = []
            for c, (cell, w) in enumerate(zip(row, widths)):
                style = "b" if head or c in bold_cols else "r"
                cells.append((self.wrap(str(cell), w - 2 * pad, pt, family, style), style))
            h = max(len(lines) for lines, _ in cells) * step + 2 * pad
            if head and head_fill:
                self.rect(x, y, sum(widths), h, outline=None, fill=head_fill)
            cx = x
            for (lines, style), w in zip(cells, widths):
                ly = y + pad
                for line in lines:
                    self.text(cx + pad, ly, line, pt, family, style)
                    ly += step
                if grid:
                    self.rect(cx, y, w, h, outline=border, width=rule)
                cx += w
            if not grid:
                self.hline(x, x + sum(widths), y + h, rule, border)
            y += h
        return y

    def shade(self, x, y, w, h, fill) -> None:
        self.rect(x, y, w, h, outline=None, fill=fill)


# ---------------------------------------------------------------- the document


@dataclass
class Doc:
    name: str
    category: str
    build: Callable[[Doc], None]
    scheme: str = "none"
    level: int = 0
    label: str | None = None
    tlp: str | None = None
    company: str | None = None
    fmt: str = "png"
    dpi: int = 200
    degradation: str = "none"
    mentions: bool = False
    tags: tuple[str, ...] = ()
    note: str = ""
    pages: list[Page] = field(default_factory=list)
    markings: list[str] = field(default_factory=list)
    rng: random.Random = field(default_factory=random.Random)
    noise: np.random.Generator = field(default_factory=np.random.default_rng)
    used: dict[int, set[int]] = field(default_factory=dict)

    def page(self, paper=WHITE) -> Page:
        pg = Page(self.dpi, paper)
        self.pages.append(pg)
        return pg

    def mark(self, pg: Page, text: str, gov: bool = True) -> None:
        """Records a marking drawn on `pg`; a government one sets the page level."""
        if text not in self.markings:
            self.markings.append(text)
        if gov:
            pg.level = max(pg.level, self.level)

    def pick(self, pool: list[str], n: int) -> str:
        """`n` sentences of `pool`, none repeated within the document while any are left."""
        used = self.used.setdefault(id(pool), set())
        free = [i for i in range(len(pool)) if i not in used]
        if len(free) < n:
            used.clear()
            free = list(range(len(pool)))
        chosen = self.rng.sample(free, n)
        used.update(chosen)
        return " ".join(pool[i] for i in chosen)


DOCS: list[Doc] = []


def document(name: str, category: str, **meta):
    """Registers the decorated function as the builder of one file."""

    def register(build: Callable[[Doc], None]) -> Callable[[Doc], None]:
        DOCS.append(Doc(name=name, category=category, build=build, **meta))
        return build

    return register


# ---------------------------------------------------------------- markings


ALIGN_X = {"l": 20.0, "c": 105.0, "r": 190.0}


def header(
    d: Doc,
    pg: Page,
    text: str,
    align: str = "c",
    pt: float = 11,
    family: str = "arial",
    style: str = "b",
    fill=INK,
    y: float = 8.0,
    track: float = 0.0,
    box: bool = False,
    gov: bool = True,
    x: float | None = None,
) -> None:
    """A printed marking line, at the top by default (pass a larger `y` for a footer)."""
    x = ALIGN_X[align] if x is None else x
    w = pg.text(x, y, text, pt, family, style, fill, align, track)
    if box:
        left = {"l": x, "c": x - w / 2, "r": x - w}[align]
        h = pt * MM_PER_INCH / 72
        pg.rect(left - 2.2, y - 1.4, w + 4.4, h * 1.2 + 2.4, outline=fill, width=0.4)
    d.mark(pg, text, gov)


def footer(d: Doc, pg: Page, text: str, y: float = 284.0, **kw) -> None:
    header(d, pg, text, y=y, **kw)


def ink_texture(noise: np.random.Generator, w: int, h: int, strength: float) -> np.ndarray:
    small = noise.uniform(1 - strength, 1.0, size=(max(2, h // 10), max(2, w // 10)))
    tex = Image.fromarray((small * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR)
    out = np.asarray(tex, np.float32) / 255
    holes = noise.random((h, w)) < 0.05
    out[holes] *= 0.3
    return out


def ink(pg: Page, mask: Image.Image, x0: int, y0: int, color) -> None:
    """Multiplies ink of `color` through `mask` onto the page, as a stamp prints."""
    width, height = pg.img.size
    left, top = max(0, x0), max(0, y0)
    right, bottom = min(width, x0 + mask.width), min(height, y0 + mask.height)
    if right <= left or bottom <= top:
        return
    m = np.asarray(mask.crop((left - x0, top - y0, right - x0, bottom - y0)), np.float32)
    m = m[..., None] / 255
    base = np.asarray(pg.img.crop((left, top, right, bottom)), np.float32)
    tint = np.asarray(color, np.float32) / 255
    out = base * (1 - m + m * tint)
    pg.img.paste(Image.fromarray(out.clip(0, 255).astype(np.uint8)), (left, top))


def stamp(
    d: Doc,
    pg: Page,
    lines: list[str],
    cx: float,
    cy: float,
    pt: float = 20,
    fill=STAMP_RED,
    angle: float = 0.0,
    border: str = "box",
    opacity: float = 0.9,
    family: str = "arial",
    style: str = "b",
    track: float = 0.05,
    sizes: list[float] | None = None,
    marks: list[str] | None = None,
    gov: bool = True,
    texture: float = 0.4,
) -> None:
    """A rubber stamp centred on (cx, cy) mm: inked, optionally framed and rotated.

    Every line is recorded as a marking unless `marks` says otherwise (pass []
    for a stamp that is not a classification, EINGANG or BEZAHLT)."""
    scale = pg.dpi / 72
    sizes = sizes or [pt] * len(lines)
    fonts = [pg.font(family, s, style) for s in sizes]
    widths = [
        f.getlength(t) + track * s * scale * (len(t) - 1) for t, f, s in zip(lines, fonts, sizes)
    ]
    heights = [s * scale * 1.22 for s in sizes]
    bw = max(2, round(pt * scale * 0.075))
    pad = pt * scale * 0.42
    inset = 0 if border in ("none", "box", "round", "bar") else 3 * bw
    w = int(max(widths) + 2 * (pad + bw + inset))
    h = int(sum(heights) + 2 * (pad * 0.7 + bw + inset))
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    y = pad * 0.7 + bw + inset
    for text, font, size, tw, th in zip(lines, fonts, sizes, widths, heights):
        x = (w - tw) / 2
        for ch in text:
            md.text((x, y + th / 2), ch, font=font, fill=255, anchor="lm")
            x += font.getlength(ch) + track * size * scale
        y += th
    if border in ("box", "double"):
        md.rectangle([0, 0, w - 1, h - 1], outline=255, width=bw)
    if border == "double":
        md.rectangle(
            [2 * bw, 2 * bw, w - 1 - 2 * bw, h - 1 - 2 * bw], outline=255, width=max(1, bw // 2)
        )
    if border == "round":
        md.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(h * 0.3), outline=255, width=bw)
    if border == "bar":
        md.rectangle([0, 0, w - 1, bw], fill=255)
        md.rectangle([0, h - 1 - bw, w - 1, h - 1], fill=255)
    tex = ink_texture(d.noise, w, h, texture)
    arr = np.asarray(mask, np.float32) * tex * opacity
    mask = Image.fromarray(arr.clip(0, 255).astype(np.uint8))
    if angle:
        mask = mask.rotate(angle, resample=Image.BICUBIC, expand=True)
    ink(pg, mask, pg.px(cx) - mask.width // 2, pg.px(cy) - mask.height // 2, fill)
    for text in lines if marks is None else marks:
        d.mark(pg, text, gov)


def date_stamp(d: Doc, pg: Page, word: str, date: str, cx, cy, fill=STAMP_BLUE, angle=0.0):
    """An office stamp such as EINGANG with a date and a hand-written style initial box."""
    stamp(
        d,
        pg,
        [word, date, "Az.: ______  Anl.: ____"],
        cx,
        cy,
        pt=12,
        fill=fill,
        angle=angle,
        border="round",
        sizes=[12, 11, 7],
        family="helv",
        marks=[],
        gov=False,
        texture=0.3,
    )


def page_numbers(
    pg: Page,
    n: int,
    total: int,
    fmt: str = "Seite {n} von {total}",
    y=289.0,
    align="c",
    pt=8.5,
    family="arial",
) -> None:
    pg.text(ALIGN_X[align], y, fmt.format(n=n, total=total), pt, family, "r", GREY, align)


# ---------------------------------------------------------------- emblems and letterheads


def star(pg: Page, cx: float, cy: float, r: float, fill, points: int = 5, inner=0.42) -> None:
    pts = []
    for i in range(points * 2):
        rad = r if i % 2 == 0 else r * inner
        a = -math.pi / 2 + i * math.pi / points
        pts.append((pg.px(cx + rad * math.cos(a)), pg.px(cy + rad * math.sin(a))))
    pg.draw.polygon(pts, fill=fill)


def emblem_eu(pg: Page, x: float, y: float, w: float = 18.0) -> None:
    h = w * 2 / 3
    pg.rect(x, y, w, h, outline=None, fill=(0, 51, 153))
    for i in range(12):
        a = i * math.pi / 6
        star(pg, x + w / 2 + h / 3 * math.cos(a), y + h / 2 + h / 3 * math.sin(a), h / 18, GOLD)


def emblem_ch(pg: Page, x: float, y: float, s: float = 9.0) -> None:
    pg.rect(x, y, s, s, outline=None, fill=(218, 41, 28))
    arm, thick = s * 0.6, s * 0.2
    pg.rect(x + (s - thick) / 2, y + (s - arm) / 2, thick, arm, outline=None, fill=WHITE)
    pg.rect(x + (s - arm) / 2, y + (s - thick) / 2, arm, thick, outline=None, fill=WHITE)


def emblem_at(pg: Page, x: float, y: float, w: float = 14.0) -> None:
    h = w * 2 / 3
    pg.rect(x, y, w, h / 3, outline=None, fill=(200, 16, 46))
    pg.rect(x, y + h / 3, w, h / 3, outline=None, fill=WHITE)
    pg.rect(x, y + 2 * h / 3, w, h / 3, outline=None, fill=(200, 16, 46))
    pg.rect(x, y, w, h, outline=GREY, width=0.15)


def emblem_nato(pg: Page, cx: float, cy: float, r: float = 8.0) -> None:
    pg.draw.ellipse([pg.px(cx - r), pg.px(cy - r), pg.px(cx + r), pg.px(cy + r)], fill=NAVY)
    star(pg, cx, cy, r * 0.8, WHITE, points=4, inner=0.28)
    star(pg, cx, cy, r * 0.32, NAVY, points=4, inner=0.5)


def emblem_seal(pg: Page, cx: float, cy: float, r: float = 10.0, fill=NAVY) -> None:
    for k, width in ((1.0, 0.6), (0.82, 0.3)):
        pg.draw.ellipse(
            [pg.px(cx - r * k), pg.px(cy - r * k), pg.px(cx + r * k), pg.px(cy + r * k)],
            outline=fill,
            width=max(1, pg.px(width)),
        )
    star(pg, cx, cy, r * 0.45, fill)
    for i in range(13):
        a = math.pi * (0.15 + 0.7 * i / 12)
        star(pg, cx - r * 0.66 * math.cos(a), cy - r * 0.66 * math.sin(a) * -1, r * 0.06, fill)


def emblem_federal_bar(pg: Page, x: float, y: float, h: float = 16.0) -> None:
    """The narrow black-red-gold bar of the German federal corporate design."""
    for i, colour in enumerate(((0, 0, 0), (221, 0, 0), (255, 206, 0))):
        pg.rect(x, y + i * h / 3, 2.4, h / 3, outline=None, fill=colour)


def emblem_tricolore(pg: Page, x: float, y: float, w: float = 12.0) -> None:
    for i, colour in enumerate(((0, 35, 149), WHITE, (237, 41, 57))):
        pg.rect(x + i * w / 3, y, w / 3, w * 0.66, outline=None, fill=colour)
    pg.rect(x, y, w, w * 0.66, outline=GREY, width=0.15)


def letterhead_federal(
    pg: Page, lines: list[str], address: list[str], family="arial", top: float = 14.0
) -> float:
    """German federal style: the flag bar, the office's name, the address column."""
    emblem_federal_bar(pg, 20, top, 17)
    y = top + 0.5
    for line in lines:
        pg.text(25, y, line, 11, family, "r")
        y += 5
    y = top + 0.5
    for line in address:
        pg.text(135, y, line, 7.2, family, "b" if line.isupper() else "r", GREY)
        y += 3.4
    return max(y, top + 24) + 4


def letterhead_logo(
    pg: Page,
    name: str,
    sub: str,
    colour,
    shape: str = "circle",
    family="helv",
    right: list[str] = (),
    top: float = 13.0,
) -> float:
    """A coloured logo with the organisation's name: a Land office, a company, a school."""
    t = top
    if shape == "circle":
        pg.draw.ellipse([pg.px(20), pg.px(t), pg.px(33), pg.px(t + 13)], fill=colour)
        pg.draw.ellipse([pg.px(24), pg.px(t + 4), pg.px(29), pg.px(t + 9)], fill=WHITE)
    elif shape == "leaf":
        pg.draw.pieslice([pg.px(19), pg.px(t - 1), pg.px(33), pg.px(t + 13)], 180, 360, fill=colour)
        pg.draw.pieslice([pg.px(21), pg.px(t + 1), pg.px(35), pg.px(t + 15)], 0, 180, fill=colour)
    elif shape == "bars":
        for i in range(4):
            pg.rect(20 + i * 3.4, t + 13 - (i + 1) * 3, 2.4, (i + 1) * 3, outline=None, fill=colour)
    elif shape == "square":
        pg.rect(20, t, 13, 13, outline=None, fill=colour)
        pg.text(26.5, t + 2.2, name[:2].upper(), 16, "arial", "b", WHITE, "c")
    elif shape == "triangle":
        pg.draw.polygon(
            [(pg.px(20), pg.px(t + 13)), (pg.px(27), pg.px(t)), (pg.px(34), pg.px(t + 13))],
            fill=colour,
        )
    pg.text(37, t + 0.5, name, 14, family, "b", colour)
    pg.text(37, t + 7.5, sub, 8.5, family, "r", GREY)
    y = t + 0.5
    for line in right:
        pg.text(190, y, line, 7.5, family, "r", GREY, "r")
        y += 3.5
    pg.hline(20, 190, t + 18, 0.5, colour)
    return t + 24


def letterhead_at(
    pg: Page, ministry: list[str], address: list[str], top: float = 16.0, family="arial"
) -> float:
    """Austrian federal style: red-white-red, "Republik Österreich", the ministry."""
    emblem_at(pg, 20, top, 13)
    pg.text(37, top - 0.5, "REPUBLIK ÖSTERREICH", 7.5, family, "b", GREY, track=0.12)
    y = top + 4
    for line in ministry:
        pg.text(37, y, line, 12, family, "b")
        y += 5.6
    ay = top - 0.5
    for line in address:
        pg.text(190, ay, line, 7.5, family, "r", GREY, "r")
        ay += 3.5
    return max(y, ay) + 8


def letterhead_ch(
    pg: Page, department: str, office: str, top: float = 14.0, family="arial"
) -> float:
    """Swiss federal style: the cross, the four languages, department and office."""
    emblem_ch(pg, 20, top, 8)
    for i, line in enumerate(
        (
            "Schweizerische Eidgenossenschaft",
            "Confédération suisse",
            "Confederazione Svizzera",
            "Confederaziun svizra",
        )
    ):
        pg.text(31, top + i * 3.3, line, 7.5, family)
    y = pg.para(105, top, 85, department, 7.5, family, fill=INK, lead=1.25)
    pg.para(105, y + 1.5, 85, office, 7.5, family, "b", lead=1.25)
    return top + 26


def address_window(pg: Page, y: float, sender: str, lines: list[str], family="arial", pt=10.5):
    pg.text(20, y, sender, 6.5, family, "r", GREY)
    pg.hline(20, 100, y + 3.2, 0.15, GREY)
    y += 5.5
    for line in lines:
        pg.text(20, y, line, pt, family)
        y += pt * 0.47
    return y


# ---------------------------------------------------------------- degradation and output


FIXED_DATE = datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc).timetuple()


def degrade(d: Doc, img: Image.Image) -> tuple[Image.Image, int]:
    """What the scanner, the fax line or the copier did to one page."""
    kind, dpi, noise = d.degradation, d.dpi, d.noise
    if kind == "fax":
        size = (round(img.width * 100 / dpi), round(img.height * 100 / dpi))
        grey = img.convert("L").resize(size, Image.BILINEAR)
        arr = np.asarray(grey, np.float32) + noise.normal(0, 16, (size[1], size[0]))
        black = arr < 150
        black ^= noise.random(black.shape) < 0.004
        for row in noise.integers(0, size[1], 3):
            start = int(noise.integers(0, size[0] // 2))
            black[row, start : start + int(noise.integers(40, size[0] // 2))] = True
        return Image.fromarray(np.where(black, 0, 255).astype(np.uint8)).convert("1"), 100
    if kind == "aged":
        arr = np.asarray(img.convert("L"), np.float32)
        h, w = arr.shape
        arr = 38 + arr * (180 / 255)
        yy, xx = np.mgrid[-1 : 1 : complex(0, h), -1 : 1 : complex(0, w)]
        arr = arr * (1 - 0.13 * (xx**2 + yy**2)) + noise.normal(0, 9, arr.shape)
        arr[:, : max(3, w // 90)] *= 0.55
        arr[noise.random(arr.shape) < 0.0007] = 50
        out = Image.fromarray(arr.clip(0, 255).astype(np.uint8))
        return out.filter(ImageFilter.GaussianBlur(dpi / 400)), dpi
    if kind == "skew":
        angle = d.rng.choice((-1, 1)) * d.rng.uniform(1.0, 3.0)
        return img.rotate(angle, resample=Image.BICUBIC, fillcolor=(247, 247, 245)), dpi
    if kind == "lowdpi":
        target = d.rng.choice((96, 100, 110))
        size = (round(img.width * target / dpi), round(img.height * target / dpi))
        return img.convert("L").resize(size, Image.BILINEAR), target
    if kind == "blur":
        radius = dpi / 100 * d.rng.uniform(0.8, 1.1)
        return img.filter(ImageFilter.GaussianBlur(radius)), dpi
    return img, dpi


def zero_tiff_padding(path: Path) -> None:
    """libtiff leaves the alignment byte after a page's strips uninitialised; zeroing it
    makes a rerun write byte-identical files."""
    data = bytearray(path.read_bytes())
    with Image.open(path) as tif:
        for frame in ImageSequence.Iterator(tif):
            end = max(o + c for o, c in zip(frame.tag_v2[273], frame.tag_v2[279]))
            if end % 2 and end < len(data):
                data[end] = 0
    path.write_bytes(bytes(data))


def save(d: Doc, images: list[Image.Image], dpi: int, path: Path) -> None:
    if d.fmt in ("png", "jpg") and len(images) != 1:
        raise ValueError(f"{d.name}: {len(images)} pages cannot go into one {d.fmt}")
    if d.fmt == "png":
        images[0].save(path, dpi=(dpi, dpi))
    elif d.fmt == "jpg":
        quality = 25 if d.degradation == "jpeg" else 85
        images[0].convert("RGB").save(path, quality=quality, dpi=(dpi, dpi))
    elif d.fmt == "tif":
        compression = "group4" if images[0].mode == "1" else "tiff_lzw"
        images[0].save(
            path, save_all=True, append_images=images[1:], compression=compression, dpi=(dpi, dpi)
        )
        zero_tiff_padding(path)
    elif d.fmt == "pdf":
        images = [im.convert("RGB") if im.mode == "1" else im for im in images]
        images[0].save(
            path,
            "PDF",
            save_all=True,
            append_images=images[1:],
            resolution=float(dpi),
            quality=80,
            creationDate=FIXED_DATE,
            modDate=FIXED_DATE,
            producer="make_vs_holdout",
        )
    else:
        raise ValueError(d.fmt)


# ---------------------------------------------------------------- running text

DE_ADMIN = text_block("""
Die Abstimmung mit den beteiligten Dienststellen ist bis zum Ende des dritten
Quartals abzuschließen.

Der Rahmenterminplan sieht vor, die Arbeiten an der Liegenschaft in zwei Bauabschnitten
durchzuführen.

Für den ersten Bauabschnitt stehen im laufenden Haushaltsjahr Mittel in Höhe von 2,4 Mio. Euro zur
Verfügung.

Die Nutzeranforderungen wurden in der Besprechung am 14. Juli nochmals im Einzelnen erörtert.

Abweichungen vom Terminplan sind dem Projektleiter unverzüglich schriftlich anzuzeigen.

Die Ergebnisse der Bestandsaufnahme sind in Anlage 2 zusammengefasst.

Bis zu einer abschließenden Entscheidung bleibt die bisherige Regelung in Kraft.

Die Fachaufsicht verbleibt beim zuständigen Referat; die Dienstaufsicht ist hiervon nicht berührt.

Soweit vertragliche Fragen berührt sind, ist das Justiziariat frühzeitig zu beteiligen.

Während der Bauphase wird das Personal in Containern auf dem Parkplatz Nord untergebracht.

Die Kosten für den Rückbau der Altanlage sind in der Kostenschätzung noch nicht enthalten.

Ein erster Entwurf des Raumprogramms wird bis Mitte Oktober vorgelegt.

Die Zufahrt für Baufahrzeuge erfolgt ausschließlich über Tor 3.

Die beigefügte Liste der Ansprechpersonen ist zu ergänzen und bis zum 30. September
zurückzusenden.

Die Heizungsanlage des Gebäudes 12 hat das Ende ihrer Nutzungsdauer erreicht und ist zu ersetzen.

Mit der Planung der Ersatzmaßnahme wurde das Staatliche Baumanagement beauftragt.

Der Bedarf an Stellplätzen für Dienstfahrzeuge ist nach dem Umzug neu zu ermitteln.

Die Brandschutzbegehung hat mehrere Mängel an den Fluchtwegen im Erdgeschoss ergeben.

Die Mängel sind bis zur nächsten Begehung im November nachweislich zu beseitigen.

Eine Übersicht der Raumbelegung nach Abschluss der Maßnahme ist als Anlage 3 beigefügt.

Die Personalvertretung wurde über das Vorhaben unterrichtet und hat keine Einwände erhoben.

Der Umzug der Werkstätten soll in den Kalenderwochen 44 und 45 erfolgen.

Für die Übergangszeit wird ein Pendelverkehr zwischen beiden Standorten eingerichtet.

Über den Fortgang der Maßnahme wird in der nächsten Sitzung des Lenkungskreises berichtet.
""")

DE_EXERCISE = text_block("""
Die Übung dient der Überprüfung der Führungsfähigkeit des Stabes unter zeitlichem Druck.

Übungsbeginn ist am Montag, 5. Oktober, 06:00 Uhr; Übungsende am Freitag, 9. Oktober, 14:00 Uhr.

Die Übungsleitung liegt beim Chef des Stabes; sein Vertreter ist der Abteilungsleiter Einsatz.

Die beteiligten Truppenteile verlegen im Straßenmarsch in die zugewiesenen Räume.

Die Versorgung mit Betriebsstoff erfolgt über die Tankstelle der Liegenschaft.

Flurschäden sind unverzüglich über die Übungszentrale an die Übungsleitung zu melden.

Die Verbindung zwischen den Gefechtsständen wird über Richtfunk und Satellit sichergestellt.

Die Verpflegung wird als Kaltverpflegung ausgegeben; warme Mahlzeiten stellt die Feldküche bereit.

Die Schiedsrichtergruppe meldet ihre Beobachtungen täglich um 18:00 Uhr an die Übungsleitung.

Der Sanitätsdienst wird durch einen Rettungstrupp am Standort Nord sichergestellt.

Die Nachbereitung der Übung erfolgt in einer Auswertebesprechung am 16. Oktober.

Fahrten auf öffentlichen Straßen sind auf das notwendige Maß zu beschränken.

Die Lagekarte ist täglich mit Stand 12:00 Uhr fortzuschreiben.

Die Ergebnisse fließen in die Ausbildungsplanung des kommenden Jahres ein.
""")

DE_TECH = text_block("""
Die Messungen wurden im reflexionsarmen Raum bei einer Umgebungstemperatur von 21 °C durchgeführt.

Als Referenz diente eine kalibrierte Hornantenne mit bekanntem Gewinn.

Im Frequenzbereich von 225 bis 400 MHz liegt das Stehwellenverhältnis durchgehend unter 1,8.

Oberhalb von 380 MHz fällt der Gewinn der Prüflingsantenne um etwa 1,5 dB ab.

Die Abweichung ist auf die Kopplung mit der Montageplatte zurückzuführen.

Die Wiederholungsmessung nach Änderung der Erdung bestätigt diese Annahme.

Die Messunsicherheit wird für alle Werte mit ±0,8 dB angegeben.

Tabelle 3 fasst die gemessenen Werte für die drei Prüflinge zusammen.

Die Störfestigkeit gegenüber gepulsten Feldern wurde nach dem vereinbarten Prüfplan nachgewiesen.

Bei Prüfling 2 trat während der Klimaprüfung ein kurzzeitiger Ausfall der Stromversorgung auf.

Nach dem Austausch des Netzteils verlief die Prüfung ohne weitere Auffälligkeiten.

Die Ergebnisse erfüllen die Anforderungen der technischen Lieferbedingungen mit einer Ausnahme.

Es wird empfohlen, die Befestigung der Antenne konstruktiv zu überarbeiten.

Eine erneute Abnahmeprüfung ist nach Umsetzung der Änderung vorzusehen.

Die Rohdaten der Messung sind im Projektlaufwerk abgelegt.

Abbildung 2 zeigt den Verlauf des Gewinns über der Frequenz für beide Polarisationen.
""")

DE_IT = text_block("""
Der Wechsel auf die neue Plattform ist für das Wochenende 17./18. Oktober geplant.

Während des Wechsels stehen die Fachverfahren für etwa zwölf Stunden nicht zur Verfügung.

Die Datensicherung wird vor Beginn der Arbeiten vollständig geprüft.

Die Anwenderinnen und Anwender werden über das Service-Portal rechtzeitig informiert.

Nach der Umstellung ist eine Anmeldung nur noch mit Smartcard möglich.

Für Rückfragen steht der Anwenderservice werktags von 7 bis 18 Uhr zur Verfügung.

Die Altsysteme werden nach einer Übergangsfrist von drei Monaten abgeschaltet.

Die Lizenzkosten sinken durch die Konsolidierung um rund ein Fünftel.

Das Rückfallverfahren wurde in der Testumgebung zweimal erfolgreich erprobt.

Die Protokolldaten werden 90 Tage aufbewahrt und danach automatisch gelöscht.

Auffälligkeiten in der Anmeldestatistik sind dem Sicherheitsbeauftragten zu melden.

Die Schnittstelle zum Personalverfahren wird in einer zweiten Stufe angebunden.

Der Betrieb der Server erfolgt weiterhin im Rechenzentrum am Standort Süd.

Die Abnahme der Leistung erfolgt anhand der vereinbarten Testfälle.
""")

DE_CORP = text_block("""
Der Umsatz im ersten Halbjahr lag mit 48,3 Mio. Euro um 6 Prozent über dem Vorjahr.

Die Ergebnismarge ist infolge gestiegener Energiekosten leicht zurückgegangen.

Der Vorstand schlägt vor, den Standort Kassel bis Ende 2027 in das Werk Fulda zu integrieren.

Die Integration erfordert Investitionen von rund 7,5 Mio. Euro über zwei Jahre.

Durch die Zusammenlegung werden jährliche Einsparungen von 2,1 Mio. Euro erwartet.

Mit dem Betriebsrat wurden erste Gespräche über einen Interessenausgleich geführt.

Die Kaufpreisverhandlungen mit der Zielgesellschaft befinden sich in der Schlussphase.

Ein Abschluss des Vertrags wird bis Ende November angestrebt.

Die Finanzierung erfolgt aus vorhandener Liquidität und einer bestehenden Kreditlinie.

Die Due-Diligence-Prüfung hat keine wesentlichen Risiken ergeben.

Die Kundenzufriedenheit ist nach der Umstellung des Servicemodells deutlich gestiegen.

Die Preisanpassung zum 1. Januar wird den Kunden bis Ende Oktober mitgeteilt.

Der Aufsichtsrat wird um Zustimmung zu dem vorgeschlagenen Vorgehen gebeten.

Die Kommunikation an die Belegschaft erfolgt erst nach der Beschlussfassung.
""")

EN_GOV = text_block("""
The working group met three times during the reporting period and agreed on a revised schedule.

Delivery of the first increment is now expected in the second quarter of 2027.

The delay is mainly due to late availability of test ranges and spare parts.

Nations are invited to confirm their contributions by 15 October.

The costs of the additional trials will be shared according to the agreed formula.

A detailed breakdown of the costs is provided at Annex A.

The interoperability tests showed that both data links can exchange tracks without loss.

Minor discrepancies in time stamps were corrected by a software update.

The next milestone review is scheduled for the week of 23 November.

Participants agreed that the training package should be translated into French.

The logistics concept assumes two maintenance sites, one in the north and one in the south.

Transport of the equipment will be arranged through the movement coordination centre.

The committee is invited to note the progress and to endorse the proposed way ahead.

Any comments should be sent to the point of contact below by the end of the month.

The host nation has offered to provide accommodation for up to forty staff.

Lessons identified during the exercise have been recorded in the database.
""")

EN_CYBER = text_block("""
The vulnerability allows an unauthenticated attacker to execute code on the management interface.

Exploitation has been observed against devices whose management port is reachable from public
networks.

The vendor has released fixed versions and recommends updating without delay.

Where an update is not possible, access to the interface should be limited to trusted hosts.

Indicators of compromise are listed in the table below.

Affected organisations should review their logs for connections from the listed addresses.

The campaign appears to target municipal utilities and hospitals in the region.

Several of the phishing messages impersonate parcel delivery services.

Please report any related incident to the CERT through the usual channel.

This advisory will be updated as new information becomes available.
""")

DE_CYBER = text_block("""
Die Schwachstelle erlaubt es einem nicht angemeldeten Angreifer, Code auf der Verwaltungsoberfläche
auszuführen.

Ausnutzungsversuche wurden gegen Geräte beobachtet, deren Verwaltungszugang aus öffentlichen Netzen
erreichbar ist.

Der Hersteller hat korrigierte Versionen veröffentlicht und empfiehlt ein sofortiges Update.

Ist ein Update nicht möglich, sollte der Zugang auf bekannte Administrationsrechner beschränkt
werden.

Die bekannten Indikatoren einer Kompromittierung sind in der Tabelle aufgeführt.

Betroffene Einrichtungen sollten ihre Protokolle auf Verbindungen zu den genannten Adressen prüfen.

Die Kampagne richtet sich nach bisherigen Erkenntnissen gegen Stadtwerke und Kliniken.

Die Phishing-Nachrichten geben sich als Benachrichtigungen von Paketdiensten aus.

Vorfälle melden Sie bitte über den bekannten Meldeweg an das CERT.

Dieser Hinweis wird aktualisiert, sobald neue Informationen vorliegen.
""")

FR_ADMIN = text_block("""
La réunion du comité de pilotage s'est tenue le 9 septembre dans les locaux de la direction.

Le calendrier des essais a été confirmé par l'ensemble des participants.

Les livraisons du premier lot sont prévues pour le premier trimestre 2027.

Les surcoûts liés aux essais complémentaires seront répartis entre les partenaires.

Un point d'étape sera présenté lors de la prochaine réunion.

Les services concernés sont invités à transmettre leurs observations avant le 15 octobre.

La formation des opérateurs débutera à l'issue de la qualification du système.

Le soutien logistique sera assuré par l'établissement de Bourges.
""")


# ---------------------------------------------------------------- shared layouts


def outlook(pg: Page, owner: str, fields: list[tuple[str, str]], family="arial", y=15.0):
    """The head of an e-mail printed from Outlook: owner, rule, Von/Gesendet/An/Betreff."""
    pg.text(20, y, owner, 13, family, "b")
    pg.hline(20, 190, y + 7.2, 0.9)
    y += 10.5
    for label, value in fields:
        pg.text(20, y, label, 10, family, "b")
        y = max(pg.para(50, y, 140, value, 10, family), y + 4.9)
    return y + 5


def signature(pg: Page, x: float, y: float, lines: list[str], family="arial", pt=10.0):
    for i, line in enumerate(lines):
        pg.text(x, y, line, pt, family, "b" if i == 0 else "r")
        y += pt * 0.46
    return y


def scribble(d: Doc, pg: Page, x: float, y: float, w: float = 35.0, fill=(30, 40, 120)):
    """A hand signature: a few connected loops in ballpoint blue."""
    pts = []
    steps = 60
    for i in range(steps):
        t = i / (steps - 1)
        px = x + w * t
        py = (
            y
            + 3 * math.sin(t * 14 + d.rng.uniform(0, 0.3)) * (1 - t * 0.5)
            + d.rng.uniform(-0.3, 0.3)
        )
        pts.append((pg.px(px), pg.px(py)))
    pg.draw.line(pts, fill=fill, width=max(1, pg.px(0.35)))


def bar_chart(
    pg: Page,
    x: float,
    y: float,
    w: float,
    h: float,
    values: list[float],
    colour,
    caption: str = "",
    family="arial",
) -> float:
    pg.rect(x, y, w, h, outline=GREY, width=0.2)
    top = max(values)
    bw = w / (len(values) * 1.6)
    for i, v in enumerate(values):
        bh = (h - 6) * v / top
        pg.rect(x + 4 + i * bw * 1.6, y + h - 2 - bh, bw, bh, outline=None, fill=colour)
    for k in range(1, 4):
        pg.hline(x + 1, x + w - 1, y + h - 2 - (h - 6) * k / 4, 0.1, LIGHT)
    if caption:
        pg.text(x, y + h + 1.5, caption, 8, family, "i", GREY)
        return y + h + 6
    return y + h + 2


def line_chart(pg: Page, x, y, w, h, series, colours, caption="", family="arial") -> float:
    pg.rect(x, y, w, h, outline=GREY, width=0.2)
    for k in range(1, 5):
        pg.hline(x, x + w, y + h * k / 5, 0.1, LIGHT)
    lo = min(min(s) for s in series)
    hi = max(max(s) for s in series)
    for values, colour in zip(series, colours):
        pts = [
            (
                pg.px(x + 2 + (w - 4) * i / (len(values) - 1)),
                pg.px(y + h - 2 - (h - 4) * (v - lo) / (hi - lo or 1)),
            )
            for i, v in enumerate(values)
        ]
        pg.draw.line(pts, fill=colour, width=max(1, pg.px(0.4)))
    if caption:
        pg.text(x, y + h + 1.5, caption, 8, family, "i", GREY)
        return y + h + 6
    return y + h + 2


def slide_frame(
    pg: Page,
    x: float,
    y: float,
    w: float,
    title: str,
    bullets: list[str],
    colour,
    family="arial",
    corner: str = "",
    corner_fill=INK,
    foot: str = "",
):
    """One presentation slide (16:9) as printed in a handout."""
    h = w * 9 / 16
    pg.rect(x, y, w, h, outline=GREY, width=0.25)
    pg.rect(x, y, w, h * 0.17, outline=None, fill=colour)
    pg.text(x + 3, y + h * 0.04, title, w * 0.1, family, "b", WHITE)
    by = y + h * 0.26
    for b in bullets:
        pg.text(x + 4, by, "–", w * 0.075, family)
        by = pg.para(x + 9, by, w - 13, b, w * 0.075, family, lead=1.25) + 1.0
    if corner:
        pg.text(x + w - 2, y + h * 0.19, corner, w * 0.065, family, "b", corner_fill, "r")
    if foot:
        pg.text(x + 3, y + h - 4, foot, w * 0.045, family, "r", GREY)
    return y + h


def two_columns(
    pg: Page,
    top: float,
    left: list,
    right: list,
    family="helv",
    colour=INK,
    pt=9.5,
    head_pt=11.0,
    width=80.0,
) -> float:
    """Sections (title, paragraphs) set in two justified columns; returns the lower end."""
    bottoms = []
    for x, sections in ((20.0, left), (110.0, right)):
        y = top
        for title, paras in sections:
            pg.text(x, y, title, head_pt, family, "b", colour)
            y = pg.paras(x, y + head_pt * 0.55, width, paras, pt, family, gap=1.8, justify=True) + 3
        bottoms.append(y)
    return max(bottoms)


def body_text(
    pg: Page,
    y: float,
    salutation: str,
    paras: list[str],
    closing: list[str],
    family="arial",
    pt=10.5,
    justify=False,
    width=170.0,
    x=20.0,
) -> float:
    """Salutation, paragraphs and the closing lines of a letter."""
    if salutation:
        pg.text(x, y, salutation, pt, family)
        y += pt * 0.35 * 2.2
    y = pg.paras(x, y, width, paras, pt, family, gap=pt * 0.22, justify=justify)
    y += 3
    for line in closing:
        pg.text(x, y, line, pt, family)
        y += pt * 0.47
    return y


def numbered(
    pg: Page,
    y: float,
    items: list[tuple[str, str]],
    family="arial",
    pt=10.5,
    x=20.0,
    w=170.0,
    indent=10.0,
    gap=2.0,
    heading_style="b",
) -> float:
    """Numbered paragraphs: ("1.", "Lage") headings or ("1.1", "text") paragraphs."""
    for num, text in items:
        if num.endswith(".") and num.count(".") == 1 and len(text) < 40:
            y += 1.5
            pg.text(x, y, f"{num} {text}", pt, family, heading_style)
            y += pt * 0.47 + 1
        else:
            pg.text(x, y, num, pt, family)
            y = pg.para(x + indent, y, w - indent, text, pt, family) + gap
    return y


# ================================================================ positives: Germany


@document(
    "email",
    "de-email",
    scheme="de",
    level=1,
    label="VS-NfD",
    dpi=200,
    tags=("email-subject", "abbrev"),
    note="only the subject line carries the grade",
)
def p_email_subject(d: Doc) -> None:
    pg = d.page()
    y = outlook(
        pg,
        "Schneider, Tobias",
        [
            ("Von:", "Wagner, Petra BAIUDBw GS II 3"),
            ("Gesendet:", "Dienstag, 8. September 2026 14:12"),
            ("An:", "Schneider, Tobias; Brandt, Lea; Poststelle GS II"),
            ("Cc:", "Kühn, Matthias"),
            ("Betreff:", "VS-NfD – Belegungsplanung Liegenschaft Nordheide, Stand 3"),
            ("Anlagen:", "Belegungsplan_Nordheide_v3.xlsx; Raumliste_Geb12.pdf"),
        ],
    )
    d.mark(pg, "VS-NfD")
    paras = [
        "anbei erhalten Sie den überarbeiteten Belegungsplan für die Liegenschaft Nordheide. "
        "Gegenüber Stand 2 haben sich vor allem Änderungen in den Gebäuden 12 und 14 ergeben.",
        d.pick(DE_ADMIN, 3),
        "Ich bitte um kurze Rückmeldung bis Freitag, ob die Flächen für die Werkstatt so passen. "
        "Die Raumliste ist nach Gebäuden sortiert und enthält die geplanten Nutzungen.",
    ]
    y = body_text(
        pg,
        y,
        "Liebe Kolleginnen und Kollegen,",
        paras,
        ["Mit freundlichen Grüßen", "Im Auftrag", "", "Petra Wagner"],
    )
    y += 4
    for line in (
        "Bundesamt für Infrastruktur, Umweltschutz und",
        "Dienstleistungen der Bundeswehr",
        "Referat GS II 3 – Liegenschaftsmanagement",
        "Fontainengraben 200, 53123 Bonn",
        "Tel.: +49 228 5504-3317",
    ):
        pg.text(20, y, line, 9, "arial", "r", GREY)
        y += 4.2


@document(
    "email",
    "de-email",
    scheme="de",
    level=1,
    label="VS-NfD",
    fmt="pdf",
    dpi=200,
    tags=("email-subject", "body-line", "multi-page", "incomplete"),
    note="Outlook printout: the second page continues the quoted thread and is unmarked",
)
def p_email_thread(d: Doc) -> None:
    pg = d.page()
    y = outlook(
        pg,
        "Lehmann, Jörg",
        [
            ("Von:", "Arslan, Deniz, OTL"),
            ("Gesendet:", "Mittwoch, 16. September 2026 08:47"),
            ("An:", "Lehmann, Jörg; Fischer, Anna"),
            ("Betreff:", "AW: VS-NfD - Ersatzbeschaffung Handfunkgeräte Wachzug"),
        ],
        family="sans",
    )
    d.mark(pg, "VS-NfD")
    header(d, pg, "VS – NUR FÜR DEN DIENSTGEBRAUCH", "l", 10.5, "sans", "b", y=y)
    y += 9
    reply = [
        "Hallo Jörg,",
        "danke für die Zahlen. Ich habe die Stückzahl mit dem Wachzug abgeglichen: Wir brauchen "
        "24 Geräte zuzüglich 6 Reserve. Die Ladeschalen können wir aus dem Altbestand übernehmen.",
        "Bitte nimm die Position noch in die Bedarfsmeldung für das 4. Quartal auf.",
        "Gruß Deniz",
    ]
    y = pg.paras(20, y, 170, reply, 10, "sans", gap=2.5)
    y += 4
    pg.text(20, y, "-----Ursprüngliche Nachricht-----", 10, "sans")
    y += 5.5
    for label, value in (
        ("Von:", "Lehmann, Jörg"),
        ("Gesendet:", "Dienstag, 15. September 2026 16:20"),
        ("An:", "Arslan, Deniz, OTL"),
        ("Betreff:", "VS-NfD - Ersatzbeschaffung Handfunkgeräte Wachzug"),
    ):
        pg.text(20, y, label, 10, "sans", "b")
        pg.text(45, y, value, 10, "sans")
        y += 5
    y += 4
    quoted = [
        "Hallo Deniz,",
        "wie besprochen die Übersicht zum Bestand. Von den 30 Geräten des Wachzugs sind 11 nicht "
        "mehr instandsetzbar, weitere 7 fallen nach Auskunft der Werkstatt innerhalb des nächsten "
        "Jahres aus, weil keine Ersatzteile mehr geliefert werden.",
        d.pick(DE_IT, 2),
        "Die Beschaffung über den Rahmenvertrag ist möglich; die Lieferzeit beträgt nach Auskunft "
        "des Auftragnehmers derzeit etwa 14 Wochen. Für die Programmierung der Geräte brauchen wir "
        "zusätzlich einen Termin mit dem Fernmeldezug.",
        d.pick(DE_ADMIN, 3),
        d.pick(DE_ADMIN, 2),
    ]
    y = pg.paras(20, y, 170, quoted, 10, "sans", gap=2.5)
    pg2 = d.page()
    rest = [
        d.pick(DE_IT, 3),
        "Offen ist noch, ob die Geräte auch für den Einsatz im Außenbereich der Liegenschaft "
        "zugelassen werden müssen. Ich kläre das mit dem Sicherheitsbeauftragten.",
        "Viele Grüße",
        "Jörg",
    ]
    y = pg2.paras(20, 18, 170, rest, 10, "sans", gap=2.5)
    y += 4
    for line in (
        "Jörg Lehmann",
        "Stabsfeldwebel",
        "Versorgungsbataillon 147 – S4",
        "Kasernenstraße 12, 74736 Hardheim",
        "App. 2231",
    ):
        pg2.text(20, y, line, 9, "sans", "r", GREY)
        y += 4.2


def weisung_page_head(d: Doc, pg: Page, n: int, total: int) -> None:
    header(d, pg, "VS – NUR FÜR DEN DIENSTGEBRAUCH", "c", 10, "arial", "b", y=9)
    footer(d, pg, "VS – NUR FÜR DEN DIENSTGEBRAUCH", pt=10, family="arial", style="b", y=283)
    if n > 1:
        pg.text(105, 17, f"- {n} -", 10, "arial", "r", INK, "c")
    page_numbers(pg, n, total, y=289, align="r")


@document(
    "weisung",
    "de-weisung",
    scheme="de",
    level=1,
    label="VS-NfD",
    fmt="pdf",
    dpi=200,
    mentions=True,
    tags=("header", "footer", "every-page", "multi-page"),
    note="text says documents from VS-VERTRAULICH up go into the safe: a mention",
)
def p_weisung(d: Doc) -> None:
    pg = d.page()
    weisung_page_head(d, pg, 1, 3)
    pg.text(20, 20, "Versorgungsbataillon 147", 11, "arial", "b")
    pg.text(20, 25, "- Kommandeur -", 10.5, "arial")
    pg.text(20, 34, "Az 31-02-05", 10.5, "arial")
    pg.text(190, 34, "Hardheim, 2. September 2026", 10.5, "arial", "r", INK, "r")
    pg.text(190, 39, "App. 2215", 10.5, "arial", "r", INK, "r")
    pg.text(105, 52, "WEISUNG Nr. 4/2026", 14, "arial", "b", INK, "c")
    pg.text(
        105,
        59,
        "für die Teilnahme an der Stabsrahmenübung „NORDLICHT 26“",
        11,
        "arial",
        "r",
        INK,
        "c",
    )
    y = 70
    pg.text(20, y, "Bezug:", 10.5, "arial")
    for line in (
        "1. Befehl Nr. 12/2026 der Division vom 12.08.2026",
        "2. Ausbildungsweisung 2026 des Bataillons",
        "3. Besprechung beim Kommandeur am 28.08.2026",
    ):
        pg.text(40, y, line, 10.5, "arial")
        y += 5
    y += 2
    pg.text(20, y, "Anlagen:", 10.5, "arial")
    for line in ("1. Zeitplan", "2. Gliederung der Übungsleitung", "3. Verteiler"):
        pg.text(40, y, line, 10.5, "arial")
        y += 5
    y += 4
    numbered(
        pg,
        y,
        [
            ("1.", "Lage"),
            ("1.1", d.pick(DE_EXERCISE, 3)),
            (
                "1.2",
                "Das Bataillon stellt für die Übung einen verkleinerten Gefechtsstand sowie die "
                "Versorgungsteile für zwei Kompanien. " + d.pick(DE_EXERCISE, 2),
            ),
            ("2.", "Auftrag"),
            (
                "2.1",
                "Versorgungsbataillon 147 nimmt mit Teilen an der Stabsrahmenübung NORDLICHT 26 "
                "teil, übt die Führung der logistischen Unterstützung und stellt die Verbindung "
                "zur Division sicher.",
            ),
            ("3.", "Durchführung"),
            ("3.1", d.pick(DE_EXERCISE, 3)),
        ],
    )
    pg = d.page()
    weisung_page_head(d, pg, 2, 3)
    numbered(
        pg,
        26,
        [
            ("3.2", d.pick(DE_EXERCISE, 3)),
            (
                "3.3",
                "Die Kompaniechefs melden die Übungsbereitschaft ihrer Teileinheiten bis "
                "Donnerstag, 1. Oktober, 12:00 Uhr an S3.",
            ),
            ("4.", "Versorgung"),
            ("4.1", d.pick(DE_EXERCISE, 2)),
            ("4.2", d.pick(DE_ADMIN, 2)),
            ("5.", "Umgang mit Übungsunterlagen"),
            (
                "5.1",
                "Die Übungsunterlagen sind nach der Verschlusssachenanweisung zu behandeln. "
                "Unterlagen, die als VS-VERTRAULICH oder höher eingestuft sind, dürfen nur im "
                "Stahlschrank des S2-Bereichs verwahrt und nicht in den Gefechtsstand mitgenommen "
                "werden.",
            ),
            (
                "5.2",
                "Kopien werden ausschließlich durch die VS-Registratur gefertigt. Der Verlust "
                "von Unterlagen ist sofort dem S2-Offizier zu melden.",
            ),
            ("6.", "Führung und Verbindung"),
            ("6.1", d.pick(DE_EXERCISE, 2)),
        ],
    )
    pg = d.page()
    weisung_page_head(d, pg, 3, 3)
    y = numbered(
        pg,
        26,
        [
            (
                "6.2",
                "Der Gefechtsstand ist ab Übungsbeginn ständig besetzt. Die Rufnamen ergeben sich "
                "aus der Fernmeldeunterlage der Division.",
            ),
            ("7.", "Schlussbestimmungen"),
            (
                "7.1",
                "Diese Weisung tritt mit Bekanntgabe in Kraft und mit Ende der Nachbereitung "
                "außer Kraft.",
            ),
        ],
    )
    y += 10
    signature(pg, 120, y, ["Im Auftrag", "", "", "Kramer", "Major und S3-Stabsoffizier"])
    scribble(d, pg, 120, y + 10, 30)
    pg.text(20, y + 40, "Verteiler: gem. Anlage 3", 10, "arial")


@document(
    "minutes",
    "de-minutes",
    scheme="de",
    level=1,
    label="VS-NfD",
    fmt="tif",
    dpi=240,
    tags=("header", "right", "abbrev", "small", "multi-page", "table"),
)
def p_minutes(d: Doc) -> None:
    for n in (1, 2):
        pg = d.page()
        header(d, pg, "VS-NfD", "r", 9, "helv", "b", y=9)
        page_numbers(pg, n, 2, family="helv")
        if n == 1:
            letterhead_logo(
                pg,
                "Bundesamt für Materialwirtschaft",
                "Abteilung L – IT-Service",
                BLUE,
                "bars",
                right=["Referat L 4", "Bearbeiter: RDir Hoffmann", "Tel. 2284"],
            )
            pg.text(20, 42, "Ergebnisprotokoll", 16, "helv", "b")
            y = pg.table(
                20,
                52,
                [40, 130],
                [
                    ["Besprechung", "Jour fixe Migration Arbeitsplatzsysteme (Nr. 11)"],
                    ["Datum / Zeit", "10. September 2026, 09:30 – 11:15 Uhr"],
                    ["Ort", "Gebäude 3, Raum 214 und Videokonferenz"],
                    ["Leitung", "RDir Hoffmann (L 4)"],
                    ["Protokoll", "TRAng Weiß (L 4)"],
                ],
                pt=9.5,
                family="helv",
                header=False,
                bold_cols=(0,),
            )
            pg.text(20, y + 6, "Teilnehmende", 11, "helv", "b")
            y = pg.table(
                20,
                y + 12,
                [55, 60, 55],
                [
                    ["Name", "Organisationseinheit", "Erreichbarkeit"],
                    ["Hoffmann, Stefan", "L 4 (Leitung)", "App. 2284"],
                    ["Weiß, Karin", "L 4", "App. 2291"],
                    ["Özdemir, Can", "L 2 – Rechenzentrum", "App. 2410"],
                    ["Brenner, Ute", "Z 1 – Personal", "App. 1102"],
                    ["Jansen, Frank", "Auftragnehmer", "extern"],
                ],
                pt=9,
                family="helv",
            )
            pg.text(20, y + 6, "Ergebnisse", 11, "helv", "b")
            rows = [
                ["TOP", "Thema / Ergebnis", "Verantw.", "Termin"],
                [
                    "1",
                    "Begrüßung, Genehmigung des Protokolls Nr. 10 ohne Änderungen.",
                    "Hoffmann",
                    "–",
                ],
                ["2", "Stand der Migration: " + d.pick(DE_IT, 3), "Özdemir", "17.10."],
                ["3", "Schulung: " + d.pick(DE_IT, 2), "Brenner", "30.09."],
            ]
            pg.table(20, y + 12, [12, 112, 26, 20], rows, pt=9, family="helv")
        else:
            rows = [
                ["TOP", "Thema / Ergebnis", "Verantw.", "Termin"],
                ["4", "Risiken: " + d.pick(DE_IT, 3), "Jansen", "laufend"],
                ["5", "Rückfallverfahren: " + d.pick(DE_IT, 2), "Özdemir", "09.10."],
                ["6", "Verschiedenes: " + d.pick(DE_ADMIN, 2), "alle", "–"],
            ]
            y = pg.table(20, 22, [12, 112, 26, 20], rows, pt=9, family="helv")
            y = pg.para(
                20,
                y + 8,
                170,
                "Nächster Termin: 24. September 2026, 09:30 Uhr, gleicher Ort.",
                10,
                "helv",
            )
            pg.text(20, y + 10, "gez. Weiß", 10, "helv")
            pg.text(20, y + 15, "Verteiler: Teilnehmende, AL L, Registratur", 9, "helv", "r", GREY)


@document(
    "letter",
    "de-letter",
    scheme="de",
    level=2,
    label="VS-VERTRAULICH",
    dpi=300,
    tags=("header", "footer", "red", "amtlich", "logo"),
    note="amtlich geheimgehalten printed under the grade",
)
def p_letter_vsv_amtlich(d: Doc) -> None:
    pg = d.page()
    for y in (8.0, 280.0):
        header(d, pg, "VS – VERTRAULICH", "c", 13, "arial", "b", RED, y=y)
        header(d, pg, "amtlich geheimgehalten", "c", 8.5, "arial", "r", RED, y=y + 5.8)
    y = letterhead_logo(
        pg,
        "Polizeipräsidium Nordhafen",
        "Abteilung Einsatz · Stabsstelle Planung",
        BLUE,
        "circle",
        family="arial",
        right=["Hafenstraße 40", "27580 Nordhafen", "Tel. 0471 953-0"],
        top=20,
    )
    y = address_window(
        pg,
        y + 6,
        "Polizeipräsidium Nordhafen · Hafenstraße 40 · 27580 Nordhafen",
        [
            "Ministerium für Inneres und Sport",
            "Referat 22 – Einsatzangelegenheiten",
            "z. Hd. Herrn MinR Dr. Albers",
            "Lavesallee 6",
            "30169 Hannover",
        ],
    )
    pg.text(190, 60, "Nordhafen, 11.09.2026", 10.5, "arial", "r", INK, "r")
    pg.text(190, 65, "Az.: ST P – 1203/26", 10.5, "arial", "r", INK, "r")
    y += 14
    pg.text(20, y, "Einsatzkonzept Hafenschutz 2027 – Zwischenbericht", 11, "arial", "b")
    y += 9
    paras = [
        "wie mit Erlass vom 3. Juli 2026 erbeten, lege ich den Zwischenbericht zum Einsatzkonzept "
        "für den Schutz der Hafenanlagen vor. Der Bericht beruht auf den Ergebnissen der "
        "Arbeitsgruppe, an der die Wasserschutzpolizei und die Hafenbehörde beteiligt waren.",
        d.pick(DE_ADMIN, 3),
        "Der endgültige Bericht wird nach Abstimmung mit der Hafenbehörde bis zum 30. November "
        "vorgelegt. Ich bitte, die Anlage nur dem dort genannten Personenkreis zugänglich zu "
        "machen.",
    ]
    y = body_text(
        pg,
        y,
        "Sehr geehrter Herr Dr. Albers,",
        paras,
        ["Mit freundlichen Grüßen", "", "", "Dr. Katrin Wolters", "Polizeipräsidentin"],
        justify=True,
    )
    scribble(d, pg, 22, y - 14, 32)
    pg.text(20, y + 6, "Anlage: Zwischenbericht (12 Blatt)", 9.5, "arial")


def report_page(d: Doc, pg: Page, n: int, total: int) -> None:
    header(d, pg, "GEHEIM", "c", 14, "arial", "b", y=8)
    footer(d, pg, "GEHEIM", pt=14, family="arial", style="b", y=281)
    pg.hline(20, 190, 17, 0.2, GREY)
    pg.text(20, 13, "TB-2026-114 · Antennenanlage AF-400", 8, "arial", "r", GREY)
    pg.hline(20, 190, 277, 0.2, GREY)
    page_numbers(pg, n, total, y=289, align="c", pt=8.5)


@document(
    "report",
    "de-report",
    scheme="de",
    level=3,
    label="GEHEIM",
    fmt="tif",
    dpi=200,
    tags=("header", "footer", "every-page", "multi-page", "term-line", "cover"),
    note="the classification-term line is on the cover page",
)
def p_report_geheim(d: Doc) -> None:
    pg = d.page()
    report_page(d, pg, 1, 4)
    pg.text(
        105, 24, "Die VS-Einstufung endet mit Ablauf des Jahres 2056.", 9, "arial", "r", INK, "c"
    )
    d.mark(pg, "Die VS-Einstufung endet mit Ablauf des Jahres 2056.")
    pg.text(20, 50, "Wehrtechnisches Prüfzentrum Nord", 13, "arial", "b", NAVY)
    pg.text(20, 57, "Abteilung Funk und Antennen", 11, "arial", "r", NAVY)
    pg.hline(20, 190, 66, 1.2, NAVY)
    pg.text(20, 90, "Technischer Bericht", 22, "arial", "b")
    pg.text(20, 101, "TB-2026-114", 16, "arial")
    pg.para(
        20,
        115,
        170,
        "Messung der Strahlungseigenschaften der Antennenanlage AF-400 am Trägerfahrzeug, Baulos 2",
        14,
        "arial",
        "r",
    )
    pg.table(
        20,
        160,
        [55, 115],
        [
            ["Auftraggeber", "Projektgruppe Taktische Kommunikation"],
            ["Bearbeitung", "Dipl.-Ing. M. Seidel, Dr. R. Okafor"],
            ["Prüfzeitraum", "22. Juni bis 17. Juli 2026"],
            ["Ausgabe", "1 vom 4. August 2026"],
            ["Ausfertigung", "3 von 6"],
        ],
        pt=10,
        header=False,
        bold_cols=(0,),
    )
    pg = d.page()
    report_page(d, pg, 2, 4)
    pg.text(20, 26, "1  Zweck und Umfang", 12, "arial", "b")
    y = 34.0
    y = pg.paras(20, y, 170, [d.pick(DE_TECH, 3), d.pick(DE_TECH, 3)], 10.5, justify=True)
    pg.text(20, y + 3, "2  Messaufbau", 12, "arial", "b")
    y = pg.paras(20, y + 11, 170, [d.pick(DE_TECH, 3)], 10.5, justify=True)
    y = pg.table(
        20,
        y + 4,
        [30, 35, 35, 35, 35],
        [
            ["Prüfling", "f / MHz", "Gewinn / dBi", "VSWR", "Befund"],
            ["AF-400/1", "243", "2,1", "1,42", "i. O."],
            ["AF-400/1", "380", "0,6", "1,77", "i. O."],
            ["AF-400/2", "243", "2,0", "1,45", "i. O."],
            ["AF-400/2", "380", "-0,9", "1,93", "Abw."],
            ["AF-400/3", "300", "1,7", "1,51", "i. O."],
        ],
        pt=9.5,
    )
    pg.text(20, y + 2, "Tabelle 3: Messwerte der Prüflinge", 8.5, "arial", "i", GREY)
    pg = d.page()
    report_page(d, pg, 3, 4)
    pg.text(20, 26, "3  Ergebnisse", 12, "arial", "b")
    y = pg.paras(20, 34, 170, [d.pick(DE_TECH, 3)], 10.5, justify=True)
    y = line_chart(
        pg,
        30,
        y + 4,
        150,
        70,
        [[2.1, 2.2, 2.0, 1.8, 1.5, 0.9, 0.6], [1.9, 2.0, 2.0, 1.7, 1.2, 0.4, -0.9]],
        [BLUE, RED],
        "Abbildung 2: Gewinn über der Frequenz (225 – 400 MHz)",
    )
    pg.paras(20, y + 4, 170, [d.pick(DE_TECH, 4)], 10.5, justify=True)
    pg = d.page()
    report_page(d, pg, 4, 4)
    pg.text(20, 26, "4  Bewertung und Empfehlung", 12, "arial", "b")
    y = pg.paras(20, 34, 170, [d.pick(DE_TECH, 3), d.pick(DE_TECH, 2)], 10.5, justify=True)
    signature(pg, 20, y + 12, ["Seidel", "Dipl.-Ing., Prüfleiter"])
    signature(pg, 110, y + 12, ["Dr. Okafor", "Abteilungsleiterin"])


@document(
    "cover",
    "de-cover",
    scheme="de",
    level=4,
    label="STRENG GEHEIM",
    dpi=200,
    tags=("stamp", "double-border", "large", "red", "header", "footer", "cover"),
)
def p_cover_sg(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "STRENG GEHEIM", "c", 16, "arial", "b", RED, y=8)
    footer(d, pg, "STRENG GEHEIM", pt=16, family="arial", style="b", fill=RED, y=280)
    pg.rect(25, 30, 160, 235, outline=RED, width=1.2)
    pg.rect(28, 33, 154, 229, outline=RED, width=0.4)
    pg.text(105, 45, "Deckblatt", 12, "arial", "r", GREY, "c")
    stamp(d, pg, ["STRENG GEHEIM"], 105, 80, pt=30, fill=STAMP_RED, border="double", track=0.08)
    pg.table(
        40,
        110,
        [50, 80],
        [
            ["VS-Registriernummer", "0417/26"],
            ["Ausfertigung", "2 von 3"],
            ["Anzahl Blatt", "14 (ohne Deckblatt)"],
            ["Absender", "Abteilung Politik, Referat Pol II 1"],
            ["Datum", "21. August 2026"],
        ],
        pt=10.5,
        header=False,
        bold_cols=(0,),
    )
    pg.para(
        40,
        170,
        130,
        "Hinweise zur Behandlung: Nur gegen Empfangsbescheinigung aushändigen. "
        "Kenntnisnahme ist auf den Verteiler beschränkt. Vervielfältigung nur mit Zustimmung "
        "des Herausgebers.",
        9.5,
        "arial",
        fill=GREY,
    )
    pg.table(
        40,
        205,
        [45, 45, 40],
        [
            ["Empfangen", "Name", "Unterschrift"],
            ["", "", ""],
            ["", "", ""],
        ],
        pt=9,
        lead=2.2,
    )


@document(
    "form",
    "de-form",
    scheme="de",
    level=1,
    label="VS-NfD",
    dpi=200,
    mentions=True,
    tags=("checkbox", "form-field", "abbrev"),
    note="the ticked box is VS-NfD; the unticked grades are mentions",
)
def p_form_checkbox(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 15, "Bundesamt für Materialwirtschaft · Zentrale Poststelle", 9, "arial", "r", GREY)
    pg.text(20, 22, "Begleitschein für Kuriersendungen", 16, "arial", "b")
    pg.text(190, 23, "Vordruck ZP 12 (03/2024)", 8, "arial", "r", GREY, "r")
    fields = [
        ("Absender (Organisationseinheit)", "L 4 – IT-Service, Gebäude 3"),
        ("Empfänger", "Bundesministerium, Referat Org 2, Bonn"),
        ("Inhalt / Bezeichnung", "Betriebshandbuch Migration Arbeitsplatzsysteme, Teil B"),
        ("Anzahl Blatt / Datenträger", "46 Blatt, 1 USB-Stick (verschlüsselt)"),
        ("Datum der Übergabe", "14.09.2026"),
    ]
    y = 34.0
    for label, value in fields:
        pg.rect(20, y, 170, 13, outline=INK, width=0.25)
        pg.text(22, y + 1.2, label, 7, "arial", "r", GREY)
        pg.text(24, y + 5.8, value, 11, "courier")
        y += 13
    pg.rect(20, y, 170, 16, outline=INK, width=0.25)
    pg.text(22, y + 1.2, "Geheimhaltungsgrad", 7, "arial", "r", GREY)
    x = 24.0
    for option, checked in (
        ("offen", False),
        ("VS-NfD", True),
        ("VS-VERTRAULICH", False),
        ("GEHEIM", False),
    ):
        pg.checkbox(x, y + 7, 3.6, checked)
        w = pg.text(x + 5, y + 6.8, option, 10.5, "arial", "b" if checked else "r")
        x += w + 14
    d.mark(pg, "VS-NfD")
    y += 16
    pg.rect(20, y, 170, 22, outline=INK, width=0.25)
    pg.text(22, y + 1.2, "Versandart", 7, "arial", "r", GREY)
    x = 24.0
    for option, checked in (("Kurier", True), ("Einschreiben", False), ("Selbstabholung", False)):
        pg.checkbox(x, y + 8, 3.6, checked)
        x += pg.text(x + 5, y + 7.8, option, 10.5, "arial") + 16
    y += 22
    pg.rect(20, y, 85, 28, outline=INK, width=0.25)
    pg.rect(105, y, 85, 28, outline=INK, width=0.25)
    pg.text(22, y + 1.2, "Übergeben (Name, Unterschrift)", 7, "arial", "r", GREY)
    pg.text(107, y + 1.2, "Übernommen (Name, Unterschrift)", 7, "arial", "r", GREY)
    pg.text(24, y + 8, "Weiß", 11, "courier")
    scribble(d, pg, 40, y + 20, 28)
    y += 34
    pg.para(
        20,
        y,
        170,
        "Der Begleitschein verbleibt beim Absender; eine Durchschrift erhält "
        "der Empfänger. Rückfragen an die Zentrale Poststelle, App. 1000.",
        8.5,
        "arial",
        fill=GREY,
    )


@document(
    "form",
    "de-form",
    scheme="de",
    level=2,
    label="VS-VERTRAULICH",
    dpi=150,
    tags=("form-field", "typed"),
    note="grade only as a typed form field",
)
def p_form_field(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 16, "Bundesministerium für Wirtschaft und Klimaschutz", 10, "garamond", "b")
    pg.text(20, 21, "Abteilung IV – Referat IV B 3", 10, "garamond")
    pg.text(105, 38, "V O R L A G E", 16, "garamond", "b", INK, "c")
    pg.hline(20, 190, 46, 0.5)
    rows = [
        ("An", "Herrn Abteilungsleiter IV"),
        ("über", "Herrn Unterabteilungsleiter IV B"),
        ("von", "Referat IV B 3, RefL: MinR’in Szymanski, Bearb.: ORR Petersen"),
        ("Datum", "15. September 2026"),
        ("Betreff", "Exportkontrolle – Voranfrage eines Herstellers von Nachtsichtgeräten"),
        ("Geheimhaltungsgrad", "VS-VERTRAULICH"),
        ("Anlagen", "2"),
    ]
    y = 52.0
    for label, value in rows:
        pg.text(20, y, label + ":", 10.5, "garamond", "b")
        y = max(pg.para(65, y, 125, value, 11, "typewriter"), y + 6.5) + 1
    d.mark(pg, "VS-VERTRAULICH")
    pg.hline(20, 190, y + 2, 0.5)
    y += 8
    pg.text(20, y, "I. Votum", 11, "garamond", "b")
    y = pg.para(
        20,
        y + 7,
        170,
        "Kenntnisnahme und Billigung der vorgeschlagenen Antwort an den Hersteller.",
        11,
        "garamond",
    )
    pg.text(20, y + 4, "II. Sachverhalt", 11, "garamond", "b")
    y = pg.paras(
        20,
        y + 11,
        170,
        [
            "Ein Hersteller hat mit Schreiben vom 2. September angefragt, ob für die Lieferung von "
            "Nachtsichtgeräten an einen Abnehmer in einem Drittstaat eine Genehmigung in Aussicht "
            "gestellt werden kann. Die Geräte sollen nach Angaben des Herstellers ausschließlich "
            "für "
            "den Grenzschutz verwendet werden.",
            "Das Referat hat die Anfrage mit dem Auswärtigen Amt und dem Bundesamt für Wirtschaft "
            "und Ausfuhrkontrolle erörtert. " + d.pick(DE_ADMIN, 2),
        ],
        11,
        "garamond",
        justify=True,
    )
    pg.text(20, y + 4, "III. Bewertung", 11, "garamond", "b")
    y = pg.paras(20, y + 11, 170, [d.pick(DE_ADMIN, 3)], 11, "garamond", justify=True)
    signature(pg, 120, y + 8, ["Petersen"], "garamond", 11)


@document(
    "letter",
    "de-letter",
    scheme="de",
    level=1,
    label="VS-NfD",
    dpi=240,
    tags=("header", "footer", "mixed-case", "serif", "left"),
)
def p_letter_mixed_case(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "VS – Nur für den Dienstgebrauch", "l", 10, "times", "b", y=6.5)
    footer(
        d, pg, "VS – Nur für den Dienstgebrauch", align="l", pt=10, family="times", style="b", y=285
    )
    y = letterhead_federal(
        pg,
        ["Bundesamt für Küstenschutz", "und Wasserwirtschaft"],
        [
            "HAUSANSCHRIFT",
            "Bernhard-Nocht-Straße 78, 20359 Hamburg",
            "POSTANSCHRIFT",
            "Postfach 30 12 20, 20305 Hamburg",
            "TEL +49 40 3190-0",
            "FAX +49 40 3190-5000",
        ],
        family="times",
        top=15,
    )
    y = address_window(
        pg,
        y + 8,
        "BKW, Postfach 30 12 20, 20305 Hamburg",
        [
            "Wasserstraßen- und Schifffahrtsamt",
            "Elbe-Nordsee",
            "Sachgebiet Neubau",
            "Moorweg 12",
            "25541 Brunsbüttel",
        ],
        family="times",
        pt=11,
    )
    pg.text(190, y - 12, "Hamburg, 10. September 2026", 11, "times", "r", INK, "r")
    pg.text(190, y - 6.5, "Az.: K 3 – 5510/26", 11, "times", "r", INK, "r")
    y += 10
    pg.text(
        20, y, "Deichschau 2026 – Ergebnisse für den Abschnitt Elbinsel Nord", 11.5, "times", "b"
    )
    y += 9
    paras = [
        "bei der Deichschau am 2. September wurden am Abschnitt Elbinsel Nord mehrere Schäden an "
        "der Deckschicht festgestellt. Die Schadstellen liegen überwiegend im Bereich der "
        "Zufahrten zu den Sielbauwerken und sind auf den Baustellenverkehr des Vorjahres "
        "zurückzuführen.",
        d.pick(DE_ADMIN, 3),
        "Die Lagepläne der Schadstellen und die Angaben zu den Sielbauwerken bitte ich nur für "
        "die Planung der Instandsetzung zu verwenden und nicht an Dritte weiterzugeben.",
        d.pick(DE_ADMIN, 2),
    ]
    body_text(
        pg,
        y,
        "Sehr geehrte Damen und Herren,",
        paras,
        ["Mit freundlichen Grüßen", "Im Auftrag", "", "", "Dr. Henrike Ahlers"],
        family="times",
        pt=11,
        justify=True,
    )


@document(
    "memo",
    "de-stamp",
    scheme="de",
    level=1,
    label="VS-NfD",
    dpi=200,
    tags=("stamp", "rotated", "purple", "over-text"),
)
def p_memo_purple_stamp(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 18, "Referat Z 3 – Innerer Dienst", 10.5, "arial")
    pg.text(20, 23, "Z 3 – 0410/26", 10.5, "arial")
    pg.text(190, 18, "Berlin, 9. September 2026", 10.5, "arial", "r", INK, "r")
    pg.text(190, 23, "App. 4417", 10.5, "arial", "r", INK, "r")
    pg.text(20, 40, "Vermerk", 15, "arial", "b")
    pg.text(20, 50, "Betr.: Verlagerung der Registratur in das Gebäude C", 10.5, "arial", "b")
    pg.text(20, 55.5, "Bezug: Besprechung beim Abteilungsleiter Z am 3. September 2026", 10.5)
    y = numbered(
        pg,
        66,
        [
            ("1.", "Sachstand"),
            ("", d.pick(DE_ADMIN, 4)),
            (
                "",
                "Die Registratur belegt derzeit elf Räume im Erdgeschoss des Gebäudes A. Nach dem "
                "Umbau stehen im Gebäude C neun Räume mit höherer Deckenlast zur Verfügung, sodass "
                "die Rollregalanlage vollständig übernommen werden kann.",
            ),
            ("2.", "Bewertung"),
            ("", d.pick(DE_ADMIN, 4)),
            ("3.", "Vorschlag"),
            (
                "",
                "Es wird vorgeschlagen, den Umzug der Registratur auf die Kalenderwochen 44 und 45 "
                "zu legen und die Ausleihe in dieser Zeit auf dringende Fälle zu beschränken.",
            ),
        ],
        indent=0,
    )
    pg.text(20, y + 6, "Leitung Z 3 zur Billigung", 10.5)
    signature(pg, 130, y + 16, ["Köhler", "RR’in"])
    stamp(
        d,
        pg,
        ["VS-NUR FÜR DEN DIENSTGEBRAUCH"],
        110,
        108,
        pt=15,
        fill=PURPLE,
        angle=12,
        border="box",
        opacity=0.85,
    )


@document(
    "letter",
    "de-multipage",
    scheme="de",
    level=2,
    label="VS-VERTRAULICH",
    fmt="pdf",
    dpi=200,
    tags=("header", "footer", "boxed", "multi-page", "incomplete"),
    note="the attachment page (page 2) was printed without the marking",
)
def p_vsv_incomplete(d: Doc) -> None:
    for n in (1, 2, 3):
        pg = d.page()
        if n != 2:
            header(d, pg, "VS-VERTRAULICH", "c", 12, "arial", "b", y=8, box=True)
            footer(d, pg, "VS-VERTRAULICH", pt=12, family="arial", style="b", y=283, box=True)
        page_numbers(pg, n, 3, y=290, align="r")
        if n == 1:
            y = letterhead_federal(
                pg,
                ["Bundesamt für Materialwirtschaft", "Abteilung K – Einkauf"],
                [
                    "HAUSANSCHRIFT",
                    "Ferdinand-Sauerbruch-Str. 1, 56073 Koblenz",
                    "TEL +49 261 400-0",
                ],
                top=20,
            )
            y = address_window(
                pg,
                y + 6,
                "BAM, Ferdinand-Sauerbruch-Str. 1, 56073 Koblenz",
                ["Bundesministerium", "Referat Plan II 4", "Fontainengraben 150", "53123 Bonn"],
            )
            pg.text(190, y - 6, "Koblenz, 7. September 2026", 10.5, "arial", "r", INK, "r")
            y += 12
            pg.text(
                20,
                y,
                "Beschaffung geschützter Transportfahrzeuge – Stand der Verhandlungen",
                10.5,
                "arial",
                "b",
            )
            paras = [
                "zu den Verhandlungen mit dem Bieterkonsortium berichte ich wie folgt. Die "
                "technischen Anforderungen sind abschließend geklärt; offen sind noch Fragen der "
                "Gewährleistung und der Ersatzteilbevorratung.",
                d.pick(DE_ADMIN, 3),
                "Die Kostenaufstellung (Anlage 1) und der Terminplan (Anlage 2) sind beigefügt. "
                "Ich rege an, die Vertragsunterzeichnung für Mitte November vorzusehen.",
            ]
            body_text(
                pg,
                y + 9,
                "Sehr geehrte Damen und Herren,",
                paras,
                ["Mit freundlichen Grüßen", "Im Auftrag", "", "Brückner"],
            )
        elif n == 2:
            pg.text(20, 24, "Anlage 1 – Kostenaufstellung", 13, "arial", "b")
            pg.table(
                20,
                34,
                [70, 30, 35, 35],
                [
                    ["Position", "Menge", "Einzelpreis €", "Gesamt €"],
                    ["Transportfahrzeug, Grundausstattung", "42", "612.400", "25.720.800"],
                    ["Zusatzausstattung Funk", "42", "38.900", "1.633.800"],
                    ["Erstausstattung Ersatzteile", "1", "2.450.000", "2.450.000"],
                    ["Ausbildung Fahr- und Wartungspersonal", "1", "380.000", "380.000"],
                    ["Dokumentation", "1", "95.000", "95.000"],
                    ["Summe netto", "", "", "30.279.600"],
                ],
                pt=9.5,
            )
            pg.para(
                20,
                100,
                170,
                "Preisstand: Angebot vom 28. August 2026; Preisgleitklausel "
                "gemäß Entwurf § 7. " + d.pick(DE_ADMIN, 2),
                10,
            )
        else:
            pg.text(20, 24, "Anlage 2 – Terminplan", 13, "arial", "b")
            pg.table(
                20,
                34,
                [100, 70],
                [
                    ["Meilenstein", "Termin"],
                    ["Vertragsunterzeichnung", "November 2026"],
                    ["Vorserie (4 Fahrzeuge)", "Juli 2027"],
                    ["Truppenversuch", "September bis Dezember 2027"],
                    ["Serienlieferung", "ab März 2028, 2 Fahrzeuge je Monat"],
                    ["Abschluss der Lieferungen", "Juni 2029"],
                ],
                pt=9.5,
            )


@document(
    "memo",
    "de-letter",
    scheme="de",
    level=3,
    label="GEHEIM",
    fmt="jpg",
    dpi=200,
    degradation="jpeg",
    tags=("header", "footer", "amtlich"),
    note="amtlich geheimgehalten beside the grade",
)
def p_memo_geheim_jpeg(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "GEHEIM", "c", 14, "arial", "b", y=8)
    header(d, pg, "amtlich geheimgehalten", "r", 8, "arial", "r", y=10)
    footer(d, pg, "GEHEIM", pt=14, family="arial", style="b", y=282)
    y = letterhead_federal(
        pg,
        ["Zentralstelle für Sicherheitstechnik"],
        ["Referat ST 2", "Heinrich-Hertz-Straße 5", "53229 Bonn"],
        top=20,
    )
    pg.text(20, y + 6, "Vermerk", 15, "arial", "b")
    pg.text(190, y + 7, "Bonn, 4. September 2026", 10.5, "arial", "r", INK, "r")
    y += 17
    pg.text(20, y, "Betr.: Auswertung der Störungen im Richtfunknetz Süd", 10.5, "arial", "b")
    y = pg.paras(
        20,
        y + 9,
        170,
        [
            "Im August wurden im Richtfunknetz Süd an vier Standorten wiederholt kurze Ausfälle "
            "festgestellt. Die Auswertung der Protokolle ergab ein wiederkehrendes Muster, das auf "
            "eine gezielte Störung hindeutet.",
            d.pick(DE_TECH, 3),
            d.pick(DE_IT, 3),
            "Die Standorte und Zeitpunkte der Ausfälle sind in der Anlage aufgeführt. Eine "
            "Unterrichtung der Fachaufsicht ist vorgesehen.",
        ],
        10.5,
        justify=True,
    )
    signature(pg, 120, y + 10, ["Dr. Lindqvist", "Referatsleiter ST 2"])


@document(
    "letter",
    "de-letter",
    scheme="de",
    level=1,
    label="VS-NfD",
    dpi=200,
    degradation="skew",
    tags=("header", "footer", "left", "right", "logo", "green"),
)
def p_letter_skew(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "VS-NUR FÜR DEN DIENSTGEBRAUCH", "l", 9, "helv", "b", y=7)
    footer(d, pg, "VS-NUR FÜR DEN DIENSTGEBRAUCH", align="r", pt=9, family="helv", style="b", y=286)
    y = letterhead_logo(
        pg,
        "Landesamt für Brand- und Katastrophenschutz",
        "Sachsen-Weiden",
        GREEN,
        "leaf",
        right=["Dezernat 3", "Am Zeughaus 4", "04109 Leipzig"],
        top=17,
    )
    y = address_window(
        pg,
        y + 6,
        "LBK · Am Zeughaus 4 · 04109 Leipzig",
        [
            "Landkreis Mittelaue",
            "Amt für Brandschutz und Rettungsdienst",
            "Postfach 1120",
            "04701 Mittelaue",
        ],
        family="helv",
    )
    pg.text(190, y - 6, "Leipzig, 1. September 2026", 10.5, "helv", "r", INK, "r")
    y += 12
    pg.text(20, y, "Alarm- und Ausrückeordnung für kritische Infrastrukturen", 11, "helv", "b")
    paras = [
        "anliegend übersende ich die überarbeitete Alarm- und Ausrückeordnung für die Objekte "
        "der kritischen Infrastruktur in Ihrem Zuständigkeitsbereich. Die Objektlisten wurden "
        "mit den Betreibern abgestimmt.",
        d.pick(DE_ADMIN, 3),
        d.pick(DE_EXERCISE, 2),
        "Bitte stellen Sie sicher, dass die Unterlagen nur den Führungskräften der Leitstelle "
        "zugänglich gemacht werden.",
    ]
    body_text(
        pg,
        y + 9,
        "Sehr geehrte Damen und Herren,",
        paras,
        ["Mit freundlichen Grüßen", "", "", "Tilo Rehberg", "Dezernatsleiter"],
        family="helv",
    )


@document(
    "letter",
    "de-stamp",
    scheme="de",
    level=1,
    label="VS-NfD",
    dpi=200,
    degradation="faint",
    tags=("stamp", "faint", "blue", "abbrev", "rotated"),
    note="the only marking is a weakly inked stamp",
)
def p_letter_faint_stamp(d: Doc) -> None:
    pg = d.page()
    y = letterhead_logo(
        pg,
        "Landesamt für Katastrophenschutz",
        "Stabsbereich Planung",
        TEAL,
        "square",
        right=["Friedrichstraße 20", "65185 Wiesbaden"],
        top=14,
    )
    stamp(
        d,
        pg,
        ["VS-NfD"],
        168,
        48,
        pt=17,
        fill=STAMP_BLUE,
        angle=4,
        border="box",
        opacity=0.3,
        texture=0.5,
    )
    y = address_window(
        pg,
        y + 6,
        "LKS · Friedrichstraße 20 · 65185 Wiesbaden",
        [
            "Stadt Neuweiler",
            "Ordnungsamt – Abteilung Bevölkerungsschutz",
            "Marktplatz 1",
            "35510 Neuweiler",
        ],
        family="helv",
    )
    y += 12
    pg.text(
        20, y, "Notfallplanung Trinkwasserversorgung – Standorte der Notbrunnen", 11, "helv", "b"
    )
    paras = [
        "wie in der Dienstbesprechung angekündigt, erhalten Sie die aktualisierte Übersicht der "
        "Notbrunnen im Stadtgebiet. Die Übersicht enthält die genauen Standorte, die "
        "Förderleistung und die Zuständigkeiten für die Inbetriebnahme.",
        d.pick(DE_ADMIN, 3),
        d.pick(DE_ADMIN, 2),
    ]
    body_text(
        pg,
        y + 9,
        "Sehr geehrte Damen und Herren,",
        paras,
        ["Mit freundlichen Grüßen", "Im Auftrag", "", "Marion Seeger"],
        family="helv",
    )


@document(
    "letter",
    "de-stamp",
    scheme="de",
    level=2,
    label="VS (amtlich geheimgehalten)",
    dpi=200,
    tags=("stamp", "amtlich", "blue"),
    note="only the old-style 'amtlich geheimgehalten' stamp, no grade next to it",
)
def p_letter_amtlich_only(d: Doc) -> None:
    pg = d.page()
    y = letterhead_federal(
        pg,
        ["Bundesanstalt für Flugsicherung", "und Luftraumordnung"],
        ["Referat LR 4", "Am DFS-Campus 3", "63225 Langen"],
        top=15,
    )
    stamp(
        d,
        pg,
        ["amtlich geheimgehalten"],
        160,
        50,
        pt=12,
        fill=STAMP_BLUE,
        angle=-3,
        border="box",
        family="arial",
        style="r",
        opacity=0.9,
        track=0.02,
    )
    pg.text(20, y + 14, "Vermerk", 14, "arial", "b")
    pg.text(
        20,
        y + 22,
        "Betr.: Temporäre Luftraumbeschränkung während der Übung „HALLIG 26“",
        10.5,
        "arial",
        "b",
    )
    paras = [
        "Für die Dauer der Übung wird über dem Übungsgebiet ein Flugbeschränkungsgebiet "
        "eingerichtet. Die genauen Koordinaten und Zeiten werden erst 24 Stunden vor Beginn "
        "veröffentlicht.",
        d.pick(DE_EXERCISE, 3),
        d.pick(DE_ADMIN, 2),
    ]
    y = pg.paras(20, y + 31, 170, paras, 10.5, justify=True)
    signature(pg, 120, y + 8, ["Behrendt", "Referat LR 4"])


@document(
    "letter",
    "de-letter",
    scheme="de",
    level=1,
    label="VS",
    dpi=200,
    tags=("term-line", "cut-off", "footer"),
    note="the top of the page with the grade was cut off in copying; only the line "
    "'Die VS-Einstufung endet mit Ablauf des Jahres 2056.' remains",
)
def p_letter_term_only(d: Doc) -> None:
    pg = d.page()
    letterhead_federal(
        pg,
        ["Bundesamt für Materialwirtschaft", "Abteilung K – Einkauf"],
        ["HAUSANSCHRIFT", "Ferdinand-Sauerbruch-Str. 1", "56073 Koblenz"],
        top=-6,
    )
    pg.rect(0, 0, 210, 3.5, outline=None, fill=(60, 60, 60))
    pg.text(190, 24, "Koblenz, 25. August 2026", 10.5, "arial", "r", INK, "r")
    pg.text(20, 34, "Ergebnis der Marktsichtung Schutzausrüstung", 11, "arial", "b")
    paras = [
        "die Marktsichtung zur persönlichen Schutzausrüstung ist abgeschlossen. Von den "
        "angefragten 14 Herstellern haben neun Unterlagen eingereicht; sechs Angebote erfüllen "
        "die Mindestanforderungen.",
        d.pick(DE_ADMIN, 3),
        "Die Bewertungsmatrix ist beigefügt. Eine Entscheidung über das weitere Vorgehen wird "
        "in der Sitzung des Lenkungskreises am 8. Oktober erbeten.",
        d.pick(DE_ADMIN, 2),
    ]
    body_text(
        pg,
        43,
        "Sehr geehrte Damen und Herren,",
        paras,
        ["Mit freundlichen Grüßen", "Im Auftrag", "", "Brückner"],
        justify=True,
    )
    text = "Die VS-Einstufung endet mit Ablauf des Jahres 2056."
    pg.text(20, 276, text, 8.5, "arial")
    d.mark(pg, text)


BULLETIN_TEXT = text_block("""
Die Pegel an der mittleren Oder sind seit Dienstag leicht rückläufig. Der Deich bei Kilometer 612
bleibt unter Beobachtung; die Sandsackreserve wurde auf 40.000 Stück aufgefüllt. Die
Kreisverwaltung hat die Alarmstufe 2 bis auf Weiteres beibehalten.

Im Berichtszeitraum wurden drei Anträge auf Amtshilfe gestellt. Zwei davon wurden bewilligt; ein
Antrag auf Bereitstellung von Unterkünften wurde an die zivilen Stellen zurückverwiesen, weil
ausreichend Hotelkapazitäten zur Verfügung stehen.

Die Verbindungskommandos der Kreise und kreisfreien Städte haben ihre Erreichbarkeit überprüft.
In zwei Fällen mussten Telefonlisten berichtigt werden. Die Liste mit Stand 10. September ist im
Portal abgelegt.

Für den Winter ist eine gemeinsame Übung mit dem Technischen Hilfswerk geplant. Schwerpunkt ist
die Versorgung abgeschnittener Ortschaften bei Eisgang. Die Planungskonferenz findet am
29. Oktober in Frankfurt (Oder) statt.
""")


@document(
    "bulletin",
    "de-bulletin",
    scheme="de",
    level=1,
    label="VS-NfD",
    fmt="pdf",
    dpi=200,
    tags=("header", "footer", "green", "two-column", "every-page", "multi-page"),
)
def p_bulletin(d: Doc) -> None:
    for n in (1, 2):
        pg = d.page()
        header(d, pg, "VS-NUR FÜR DEN DIENSTGEBRAUCH", "c", 10, "helv", "b", GREEN, y=7)
        footer(
            d,
            pg,
            "VS-NUR FÜR DEN DIENSTGEBRAUCH",
            pt=10,
            family="helv",
            style="b",
            fill=GREEN,
            y=284,
        )
        page_numbers(pg, n, 2, fmt="{n}/{total}", y=284, align="r", family="helv")
        if n == 1:
            pg.rect(20, 15, 170, 20, outline=None, fill=GREEN)
            pg.text(24, 17, "LAGEBULLETIN", 20, "helv", "b", WHITE)
            pg.text(
                24,
                27.5,
                "Zivil-militärische Zusammenarbeit · Landeskommando Nordost",
                9,
                "helv",
                "r",
                WHITE,
            )
            pg.text(186, 18, "Nr. 37/2026", 11, "helv", "b", WHITE, "r")
            pg.text(186, 27.5, "11. September 2026", 9, "helv", "r", WHITE, "r")
            two_columns(
                pg,
                42,
                [
                    ("Hochwasserlage an der Oder", [BULLETIN_TEXT[0], d.pick(DE_EXERCISE, 2)]),
                    ("Amtshilfe", [BULLETIN_TEXT[1], d.pick(DE_ADMIN, 2)]),
                ],
                [
                    ("Verbindungskommandos", [BULLETIN_TEXT[2], d.pick(DE_ADMIN, 2)]),
                    ("Ausblick", [BULLETIN_TEXT[3], d.pick(DE_EXERCISE, 2)]),
                ],
                colour=GREEN,
            )
        else:
            two_columns(
                pg,
                22,
                [
                    ("Personal", [d.pick(DE_ADMIN, 3), d.pick(DE_EXERCISE, 2)]),
                    ("Ausbildung", [d.pick(DE_EXERCISE, 3)]),
                ],
                [
                    ("Liegenschaften", [d.pick(DE_ADMIN, 4)]),
                ],
                colour=GREEN,
            )
            bar_chart(
                pg,
                110,
                110,
                80,
                45,
                [120, 340, 510, 460, 280, 150],
                GREEN,
                "Einsatzstunden je Woche (KW 32–37)",
                "helv",
            )


@document(
    "slides",
    "de-slides",
    scheme="de",
    level=1,
    label="VS-NfD",
    fmt="pdf",
    dpi=200,
    tags=("slides", "header", "abbrev", "small", "every-page", "multi-page"),
)
def p_slides(d: Doc) -> None:
    slides = [
        ("Funkausstattung Wachzug", ["Ersatzbeschaffung 2026/27", "Vortrag S4, 16.09.2026"]),
        (
            "Ausgangslage",
            [
                "30 Handfunkgeräte, Baujahr 2009",
                "11 Geräte nicht instandsetzbar",
                "keine Ersatzteile ab 2027",
            ],
        ),
        (
            "Bedarf",
            [
                "24 Geräte zzgl. 6 Reserve",
                "Ladeschalen aus Altbestand",
                "Programmierung durch Fernmeldezug",
            ],
        ),
        (
            "Zeitplan",
            ["Bedarfsmeldung Q4/2026", "Lieferzeit ca. 14 Wochen", "Einführung bis Mai 2027"],
        ),
        (
            "Kosten",
            [
                "ca. 96.000 € inkl. Zubehör",
                "Finanzierung aus Titel 554 01",
                "keine Folgekosten für Lizenzen",
            ],
        ),
        (
            "Entscheidungsbedarf",
            ["Billigung der Bedarfsmeldung", "Priorität vor Ersatz der Kamerasysteme"],
        ),
    ]
    for n in (1, 2):
        pg = d.page()
        header(d, pg, "VS – NUR FÜR DEN DIENSTGEBRAUCH", "c", 9, "arial", "b", y=8)
        pg.text(20, 14, "Ersatzbeschaffung Handfunkgeräte", 8, "arial", "r", GREY)
        pg.text(190, 14, "16.09.2026", 8, "arial", "r", GREY, "r")
        for k in range(3):
            title, bullets = slides[(n - 1) * 3 + k]
            y = 24 + k * 88
            slide_frame(
                pg,
                20,
                y,
                95,
                title,
                bullets,
                NAVY,
                "arial",
                corner="VS-NfD",
                corner_fill=RED,
                foot="VersBtl 147 · S4",
            )
            for j in range(8):
                pg.hline(125, 190, y + 8 + j * 7, 0.15, GREY)
        d.mark(pg, "VS-NfD")
        page_numbers(pg, n, 2, y=289)


@document(
    "memo",
    "de-letter",
    scheme="de",
    level=3,
    label="GEHEIM",
    dpi=200,
    degradation="aged",
    tags=("header", "footer", "letter-spaced", "typewriter"),
)
def p_typewriter_geheim(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "G E H E I M", "c", 13, "typewriter", "b", y=10)
    footer(d, pg, "G E H E I M", pt=13, family="typewriter", style="b", y=280)
    fam = "typewriter"
    pg.text(20, 24, "Amt für Fernmeldewesen", 11, fam, "b")
    pg.text(20, 29, "- Außenstelle Süd -", 11, fam)
    pg.text(20, 36, "AfF AS Süd - 2 - Az 55-12", 11, fam)
    pg.text(190, 36, "Memmingen, 03.09.2026", 11, fam, "r", INK, "r")
    pg.text(20, 50, "Betr.: Überprüfung der Sende- und Empfangsanlage Standort", 11, fam, "b")
    pg.text(35, 55, '"Hochfirst"', 11, fam, "b")
    pg.text(20, 62, "Bezug: Auftrag Abt. 2 vom 12.08.2026", 11, fam)
    y = numbered(
        pg,
        74,
        [
            ("1.", d.pick(DE_TECH, 3)),
            ("2.", d.pick(DE_TECH, 3)),
            ("3.", d.pick(DE_ADMIN, 2)),
            ("4.", "Ein ausführlicher Bericht folgt nach Abschluss der Wiederholungsmessung."),
        ],
        family=fam,
        pt=11,
        indent=8,
    )
    pg.text(120, y + 10, "Im Auftrag", 11, fam)
    pg.text(120, y + 25, "Obermaier", 11, fam)
    scribble(d, pg, 118, y + 20, 28, INK)


@document(
    "letter",
    "de-multipage",
    scheme="de",
    level=1,
    label="VS-NfD",
    fmt="pdf",
    dpi=200,
    mentions=True,
    tags=("header", "footer", "multi-page", "incomplete", "attachment"),
    note="unmarked cover letter (it says it is unclassified without its attachment); the "
    "two attachment pages carry the grade",
)
def p_cover_letter_attachment(d: Doc) -> None:
    pg = d.page()
    y = letterhead_logo(
        pg,
        "Wehrtechnisches Prüfzentrum Nord",
        "Abteilung Funk und Antennen",
        NAVY,
        "bars",
        family="arial",
        right=["Am Fliegerhorst 9", "26345 Bockhorn", "Tel. 04453 931-0"],
    )
    y = address_window(
        pg,
        y + 6,
        "WPZ Nord · Am Fliegerhorst 9 · 26345 Bockhorn",
        [
            "Projektgruppe Taktische Kommunikation",
            "Herrn Oberstleutnant Weber",
            "Kurt-Schumacher-Damm 41",
            "13405 Berlin",
        ],
    )
    pg.text(190, y - 6, "Bockhorn, 18. September 2026", 10.5, "arial", "r", INK, "r")
    y += 12
    pg.text(20, y, "Übersendung des Prüfberichts zur Abnahme AF-400, Baulos 2", 11, "arial", "b")
    paras = [
        "anbei übersende ich Ihnen den Prüfbericht zur Abnahme der Antennenanlage AF-400 "
        "(Anlage, VS-NfD, 2 Blatt). Die Abnahme wurde mit einer Einschränkung erteilt; "
        "Einzelheiten entnehmen Sie bitte dem Bericht.",
        "Dieses Schreiben ist ohne Anlage offen.",
        d.pick(DE_TECH, 2),
    ]
    body_text(
        pg,
        y + 9,
        "Sehr geehrter Herr Oberstleutnant,",
        paras,
        ["Mit freundlichen Grüßen", "Im Auftrag", "", "Seidel"],
    )
    pg.text(20, 262, "Anlage: Prüfbericht PB-2026-077", 9.5, "arial")
    for n in (1, 2):
        pg = d.page()
        header(d, pg, "VS – NUR FÜR DEN DIENSTGEBRAUCH", "c", 10.5, "arial", "b", y=8)
        footer(d, pg, "VS – NUR FÜR DEN DIENSTGEBRAUCH", pt=10.5, family="arial", style="b", y=283)
        page_numbers(pg, n, 2, fmt="Anlage, Blatt {n} von {total}", y=289, align="r")
        if n == 1:
            pg.text(20, 22, "Prüfbericht PB-2026-077", 14, "arial", "b")
            pg.text(20, 30, "Abnahmeprüfung Antennenanlage AF-400, Baulos 2", 11)
            y = pg.table(
                20,
                40,
                [60, 110],
                [
                    ["Prüfgegenstand", "AF-400, Seriennummern 2-017 bis 2-032"],
                    ["Prüfgrundlage", "Technische Lieferbedingungen, Ausgabe 3"],
                    ["Ergebnis", "Abnahme mit Einschränkung (Befestigung)"],
                ],
                pt=10,
                header=False,
                bold_cols=(0,),
            )
            pg.paras(20, y + 8, 170, [d.pick(DE_TECH, 4), d.pick(DE_TECH, 3)], 10.5, justify=True)
        else:
            y = pg.paras(20, 22, 170, [d.pick(DE_TECH, 4), d.pick(DE_TECH, 3)], 10.5, justify=True)
            signature(pg, 20, y + 10, ["Seidel", "Prüfleiter"])


@document(
    "letter",
    "de-stamp",
    scheme="de",
    level=2,
    label="VS-VERTRAULICH",
    dpi=200,
    degradation="blur",
    tags=("stamp", "rotated", "double-border", "large", "red", "over-text"),
)
def p_letter_big_stamp(d: Doc) -> None:
    pg = d.page()
    y = letterhead_federal(
        pg,
        ["Bundesamt für Materialwirtschaft", "Abteilung R – Rüstung"],
        ["HAUSANSCHRIFT", "Ferdinand-Sauerbruch-Str. 1", "56073 Koblenz"],
    )
    y = address_window(
        pg,
        y + 6,
        "BAM, Ferdinand-Sauerbruch-Str. 1, 56073 Koblenz",
        ["Bundesministerium", "Referat Plan I 3", "Stauffenbergstraße 18", "10785 Berlin"],
    )
    pg.text(190, y - 6, "Koblenz, 14. September 2026", 10.5, "arial", "r", INK, "r")
    y += 12
    pg.text(20, y, "Erprobung des Aufklärungssystems LUCHS – Zwischenergebnisse", 11, "arial", "b")
    paras = [
        "die Erprobung des Aufklärungssystems LUCHS wurde im August planmäßig fortgesetzt. Die "
        "Reichweite der Sensoren liegt in allen Wetterlagen über den geforderten Werten.",
        d.pick(DE_TECH, 3),
        d.pick(DE_TECH, 3),
        d.pick(DE_ADMIN, 2),
    ]
    body_text(
        pg,
        y + 9,
        "Sehr geehrte Damen und Herren,",
        paras,
        ["Mit freundlichen Grüßen", "Im Auftrag", "", "Dr. Voss"],
        justify=True,
    )
    stamp(
        d,
        pg,
        ["VS-VERTRAULICH"],
        108,
        150,
        pt=26,
        fill=STAMP_RED,
        angle=25,
        border="double",
        opacity=0.82,
    )


@document(
    "letter",
    "de-letter",
    scheme="de",
    level=3,
    label="GEHEIM",
    dpi=200,
    degradation="lowdpi",
    tags=("header", "footer", "right"),
)
def p_letter_geheim_lowdpi(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "GEHEIM", "r", 12, "arial", "b", y=8)
    footer(d, pg, "GEHEIM", align="r", pt=12, family="arial", style="b", y=284)
    y = letterhead_federal(
        pg,
        ["Zentralstelle für Sicherheitstechnik"],
        ["Referat ST 4", "Heinrich-Hertz-Straße 5", "53229 Bonn"],
        top=18,
    )
    y = address_window(
        pg,
        y + 6,
        "ZST, Heinrich-Hertz-Straße 5, 53229 Bonn",
        ["Bundesministerium des Innern", "Referat ÖS III 2", "Alt-Moabit 140", "10557 Berlin"],
    )
    y += 12
    pg.text(20, y, "Schwachstellen in der Steuerungstechnik von Umspannwerken", 11, "arial", "b")
    paras = [
        "bei der Untersuchung der Steuerungstechnik von drei Umspannwerken wurden "
        "Schwachstellen festgestellt, die einen Zugriff aus dem Wartungsnetz ermöglichen. Die "
        "Betreiber wurden noch nicht unterrichtet.",
        d.pick(DE_CYBER, 3),
        d.pick(DE_IT, 2),
    ]
    body_text(
        pg,
        y + 9,
        "Sehr geehrte Damen und Herren,",
        paras,
        ["Mit freundlichen Grüßen", "Im Auftrag", "", "Dr. Lindqvist"],
    )


@document(
    "letter",
    "de-letter",
    scheme="de",
    level=1,
    label="VS-NfD",
    dpi=300,
    tags=("footer", "small", "left", "abbrev"),
    note="7 pt footer only",
)
def p_letter_tiny_footer(d: Doc) -> None:
    pg = d.page()
    footer(d, pg, "VS-NfD", align="l", pt=7, family="arial", style="r", y=287)
    pg.text(190, 287, "Seite 1 von 1", 7, "arial", "r", INK, "r")
    y = letterhead_logo(
        pg,
        "Staatliches Baumanagement Weserbergland",
        "Geschäftsbereich Bund",
        (150, 30, 40),
        "triangle",
        right=["Ritterstraße 7", "31785 Hameln"],
    )
    y = address_window(
        pg,
        y + 6,
        "SBW · Ritterstraße 7 · 31785 Hameln",
        [
            "Bundesamt für Infrastruktur",
            "Kompetenzzentrum Baumanagement",
            "Postfach 29 63",
            "30029 Hannover",
        ],
        family="helv",
    )
    y += 12
    pg.text(20, y, "Sanierung Unterkunftsgebäude 12 – Sicherungstechnik", 11, "helv", "b")
    paras = [
        "für die Sanierung des Unterkunftsgebäudes 12 wurde die Planung der Sicherungstechnik "
        "abgeschlossen. Die Pläne enthalten die Lage der Einbruchmeldeanlage, der "
        "Zutrittskontrolle "
        "und der Kameras.",
        d.pick(DE_ADMIN, 4),
        d.pick(DE_ADMIN, 3),
        d.pick(DE_ADMIN, 3),
        "Die Ausführungspläne werden nur in Papierform an die ausführenden Firmen gegeben.",
    ]
    body_text(
        pg,
        y + 9,
        "Sehr geehrte Damen und Herren,",
        paras,
        ["Mit freundlichen Grüßen", "Im Auftrag", "", "Dipl.-Ing. Ruth Marquardt"],
        family="helv",
        justify=True,
    )


@document(
    "briefing",
    "de-multipage",
    scheme="de",
    level=4,
    label="STRENG GEHEIM",
    fmt="tif",
    dpi=200,
    degradation="aged",
    tags=("header", "footer", "red", "every-page", "multi-page"),
)
def p_briefing_sg(d: Doc) -> None:
    for n in (1, 2):
        pg = d.page()
        header(d, pg, "STRENG GEHEIM", "c", 13, "arial", "b", RED, y=8)
        footer(d, pg, "STRENG GEHEIM", pt=13, family="arial", style="b", fill=RED, y=282)
        pg.text(190, 15, "Ausfertigung 2 von 4", 9, "arial", "r", INK, "r")
        page_numbers(pg, n, 2, y=289)
        if n == 1:
            pg.text(20, 26, "Lagevortrag für die Leitung", 15, "arial", "b")
            pg.text(20, 34, "Stand: 15. September 2026, 07:00 Uhr", 10, "arial", "r", GREY)
            y = numbered(
                pg,
                44,
                [
                    ("1.", "Gesamtlage"),
                    ("1.1", d.pick(DE_EXERCISE, 3)),
                    ("1.2", d.pick(DE_EXERCISE, 3)),
                    ("2.", "Eigene Kräfte"),
                    ("2.1", d.pick(DE_EXERCISE, 3)),
                    ("2.2", d.pick(DE_ADMIN, 2)),
                ],
            )
        else:
            y = numbered(
                pg,
                24,
                [
                    ("3.", "Bewertung"),
                    ("3.1", d.pick(DE_EXERCISE, 3)),
                    ("3.2", d.pick(DE_TECH, 2)),
                    ("4.", "Vorschläge"),
                    ("4.1", d.pick(DE_ADMIN, 3)),
                ],
            )
            signature(pg, 20, y + 10, ["Oberst i.G. Brandauer", "Leiter Lagezentrum"])


@document(
    "contract",
    "de-contract",
    scheme="de",
    level=1,
    label="VS-NfD",
    fmt="pdf",
    dpi=200,
    degradation="skew",
    mentions=True,
    tags=("header", "footer", "every-page", "multi-page", "serif"),
    note="§ 9 names the grade of the work in running text",
)
def p_contract(d: Doc) -> None:
    fam = "garamond"
    sections = [
        (
            "§ 1 Gegenstand",
            "Der Auftragnehmer übernimmt die Instandsetzung der Funkanlagen der "
            "Baureihe FA 300 gemäß Leistungsbeschreibung (Anlage 1). " + d.pick(DE_TECH, 2),
        ),
        (
            "§ 2 Leistungszeit",
            "Die Leistungen sind in der Zeit vom 1. Januar 2027 bis zum "
            "31. Dezember 2028 zu erbringen. " + d.pick(DE_ADMIN, 2),
        ),
        (
            "§ 3 Vergütung",
            "Die Vergütung richtet sich nach den Einheitspreisen der Anlage 2. "
            "Sie beträgt höchstens 1.840.000 Euro netto. Rechnungen sind monatlich nachträglich "
            "zu stellen.",
        ),
        (
            "§ 4 Abnahme",
            "Die Abnahme erfolgt für jedes instandgesetzte Gerät anhand der "
            "vereinbarten Prüfprotokolle. " + d.pick(DE_TECH, 2),
        ),
        (
            "§ 5 Gewährleistung",
            "Die Gewährleistungsfrist beträgt 24 Monate ab Abnahme. "
            "Mängel sind innerhalb von zehn Arbeitstagen zu beseitigen.",
        ),
        (
            "§ 6 Vertragsstrafe",
            "Bei Überschreitung der Instandsetzungsfrist ist eine "
            "Vertragsstrafe von 0,2 Prozent des Auftragswerts je Arbeitstag, höchstens jedoch "
            "5 Prozent, verwirkt.",
        ),
        (
            "§ 7 Unterauftragnehmer",
            "Der Einsatz von Unterauftragnehmern bedarf der vorherigen "
            "schriftlichen Zustimmung des Auftraggebers.",
        ),
        (
            "§ 8 Kündigung",
            "Das Recht zur Kündigung aus wichtigem Grund bleibt unberührt. " + d.pick(DE_ADMIN, 1),
        ),
        (
            "§ 9 Geheimschutz",
            "Die Leistung ist als VS-NUR FÜR DEN DIENSTGEBRAUCH eingestuft. Der "
            "Auftragnehmer verpflichtet sich, das Merkblatt zur Behandlung von Verschlusssachen "
            "dieses Grades zu beachten und seine Beschäftigten entsprechend zu belehren.",
        ),
        (
            "§ 10 Schlussbestimmungen",
            "Änderungen und Ergänzungen dieses Vertrags bedürfen der "
            "Schriftform. Gerichtsstand ist Koblenz.",
        ),
    ]
    pages = [sections[:6], sections[6:]]
    for n, part in enumerate(pages, 1):
        pg = d.page()
        header(d, pg, "VS – NUR FÜR DEN DIENSTGEBRAUCH", "c", 9.5, fam, "b", y=9)
        footer(d, pg, "VS – NUR FÜR DEN DIENSTGEBRAUCH", pt=9.5, family=fam, style="b", y=284)
        page_numbers(pg, n, 2, y=290, family=fam)
        y = 22.0
        if n == 1:
            pg.text(105, 22, "Vertrag Nr. 4711-26-0032", 15, fam, "b", INK, "c")
            pg.text(105, 30, "über die Instandsetzung von Funkanlagen", 11.5, fam, "r", INK, "c")
            y = (
                pg.para(
                    20,
                    42,
                    170,
                    "zwischen der Bundesrepublik Deutschland, vertreten durch "
                    "das Bundesamt für Materialwirtschaft, Koblenz (Auftraggeber), und der "
                    "Nordfunk Service GmbH, Kiel (Auftragnehmer)",
                    11,
                    fam,
                )
                + 6
            )
        for title, text in part:
            pg.text(20, y, title, 11, fam, "b")
            y = pg.para(20, y + 5.8, 170, text, 11, fam, justify=True) + 4
        if n == 2:
            y += 12
            pg.hline(20, 85, y + 12, 0.2)
            pg.hline(120, 185, y + 12, 0.2)
            pg.text(20, y + 14, "Koblenz, den ...............", 10, fam)
            pg.text(120, y + 14, "Kiel, den ...............", 10, fam)
            pg.text(20, y + 19, "Für den Auftraggeber", 10, fam)
            pg.text(120, y + 19, "Für den Auftragnehmer", 10, fam)


@document(
    "email",
    "de-email",
    scheme="de",
    level=1,
    label="VS-NfD",
    dpi=150,
    degradation="blur",
    tags=("email-subject", "abbrev", "forward"),
)
def p_email_forward_blur(d: Doc) -> None:
    pg = d.page()
    y = outlook(
        pg,
        "Brandt, Lea",
        [
            ("Von:", "Kühn, Matthias"),
            ("Gesendet:", "Freitag, 11. September 2026 10:03"),
            ("An:", "Brandt, Lea"),
            ("Betreff:", "WG: VS-NfD: Personalplanung Lehrgang 3/26"),
        ],
        family="sans",
    )
    d.mark(pg, "VS-NfD")
    y = pg.paras(
        20,
        y,
        170,
        [
            "Lea, zur Info und mit der Bitte um Prüfung bis Mittwoch. Die Liste ist noch nicht "
            "mit dem Personalrat abgestimmt. Gruß Matthias",
        ],
        10,
        "sans",
    )
    pg.text(20, y + 3, "________________________________", 10, "sans")
    y += 10
    for label, value in (
        ("Von:", "Poststelle Ausbildung"),
        ("Gesendet:", "Donnerstag, 10. September 2026 16:44"),
        ("An:", "Kühn, Matthias; Wagner, Petra"),
    ):
        pg.text(20, y, label, 10, "sans", "b")
        pg.text(45, y, value, 10, "sans")
        y += 5
    y += 4
    pg.table(
        20,
        y + 2,
        [50, 40, 40, 40],
        [
            ["Name", "Dienstgrad", "Einheit", "Lehrgang"],
            ["Kaya, Emre", "Feldwebel", "2./147", "3/26 (Nov.)"],
            ["Lorenz, Julia", "Oberfeldwebel", "3./147", "3/26 (Nov.)"],
            ["Nowak, Pawel", "Stabsunteroffizier", "1./147", "Nachrücker"],
            ["Schulte, Ben", "Feldwebel", "2./147", "3/26 (Nov.)"],
        ],
        pt=9.5,
        family="sans",
    )


# ================================================================ positives: Austria, Switzerland


def at_close(pg: Page, y: float, name: str, role: str) -> float:
    pg.text(20, y, "Mit freundlichen Grüßen", 10.5)
    pg.text(20, y + 6, "Für die Bundesministerin:", 10.5)
    pg.text(20, y + 17, name, 10.5)
    pg.text(20, y + 22, role, 9.5, "arial", "r", GREY)
    return y + 28


@document(
    "letter",
    "at-letter",
    scheme="at",
    level=1,
    label="AT EINGESCHRÄNKT",
    dpi=200,
    tags=("header", "footer", "centre"),
)
def p_at_eingeschraenkt(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "EINGESCHRÄNKT", "c", 11, "arial", "b", y=7)
    footer(d, pg, "EINGESCHRÄNKT", pt=11, family="arial", style="b", y=283)
    y = letterhead_at(
        pg,
        ["Bundesministerium", "Landesverteidigung"],
        [
            "Roßauer Lände 1, 1090 Wien",
            "Geschäftszahl: 2026-0.583.771",
            "Sachbearbeiterin: ObstLt Mag. Huber",
            "Tel.: +43 50201-10 23 417",
        ],
    )
    y = address_window(
        pg,
        y + 4,
        "BMLV · Roßauer Lände 1 · 1090 Wien",
        ["Kommando Streitkräfte", "Abteilung Planung", "Hessenplatz 1", "4020 Linz"],
    )
    pg.text(190, y - 6, "Wien, am 9. September 2026", 10.5, "arial", "r", INK, "r")
    y += 10
    pg.text(
        20,
        y,
        "Betreff: Österreichische Beteiligung an der Übung „ALPENWALL 27“; Planungsstand",
        10.5,
        "arial",
        "b",
    )
    paras = [
        "Das Bundesministerium für Landesverteidigung teilt mit, dass die Beteiligung an der "
        "multinationalen Übung im Frühjahr 2027 im Einvernehmen mit dem Bundesministerium für "
        "europäische und internationale Angelegenheiten grundsätzlich gebilligt wurde.",
        "Die Übungsplanung ist bis Ende Jänner 2027 abzuschließen. " + d.pick(DE_EXERCISE, 2),
        d.pick(DE_ADMIN, 2),
        "Um Vorlage eines Kräfte- und Mittelansatzes bis 15. November 2026 wird ersucht.",
    ]
    y = pg.paras(20, y + 9, 170, paras, 10.5, justify=True)
    at_close(pg, y + 4, "Mag. Johanna Huber, ObstLt", "Abteilung Militärpolitik")


@document(
    "letter",
    "at-stamp",
    scheme="at",
    level=2,
    label="AT VERTRAULICH",
    fmt="jpg",
    dpi=200,
    degradation="jpeg",
    tags=("stamp", "rotated", "red"),
)
def p_at_vertraulich(d: Doc) -> None:
    pg = d.page()
    y = letterhead_at(
        pg,
        ["Bundesministerium", "Inneres"],
        ["Herrengasse 7, 1010 Wien", "Geschäftszahl: 2026-0.612.004", "Referat II/BK/3.2"],
    )
    stamp(d, pg, ["VERTRAULICH"], 150, 55, pt=20, fill=STAMP_RED, angle=-6, border="box")
    y = address_window(
        pg,
        y + 10,
        "BMI · Herrengasse 7 · 1010 Wien",
        [
            "Landespolizeidirektion Tirol",
            "Abteilung Einsatz",
            "Kaiserjägerstraße 8",
            "6020 Innsbruck",
        ],
    )
    pg.text(190, y - 6, "Wien, am 2. September 2026", 10.5, "arial", "r", INK, "r")
    y += 10
    pg.text(
        20,
        y,
        "Betreff: Schutz von Großveranstaltungen 2027; Gefährdungseinschätzung",
        10.5,
        "arial",
        "b",
    )
    paras = [
        "Das Bundesministerium für Inneres übermittelt in der Anlage die aktuelle "
        "Gefährdungseinschätzung für die im kommenden Jahr in Tirol geplanten "
        "Großveranstaltungen.",
        d.pick(DE_ADMIN, 3),
        "Die Einschätzung ist ausschließlich für die mit der Einsatzplanung befassten "
        "Bediensteten bestimmt.",
    ]
    y = pg.paras(20, y + 9, 170, paras, 10.5)
    pg.text(20, y + 4, "Mit freundlichen Grüßen", 10.5)
    pg.text(20, y + 10, "Für den Bundesminister:", 10.5)
    pg.text(20, y + 21, "Dr. Markus Pichler", 10.5)


@document(
    "report",
    "at-multipage",
    scheme="at",
    level=3,
    label="AT GEHEIM",
    fmt="tif",
    dpi=150,
    tags=("header", "footer", "every-page", "multi-page"),
)
def p_at_geheim(d: Doc) -> None:
    for n in (1, 2):
        pg = d.page()
        header(d, pg, "GEHEIM", "c", 12, "arial", "b", y=7)
        footer(d, pg, "GEHEIM", pt=12, family="arial", style="b", y=283)
        page_numbers(pg, n, 2, y=289, align="r")
        if n == 1:
            y = letterhead_at(
                pg,
                ["Bundesministerium", "Europäische und internationale", "Angelegenheiten"],
                [
                    "Minoritenplatz 8, 1010 Wien",
                    "GZ: 2026-0.598.112",
                    "Sektion II – Politische Angelegenheiten",
                ],
            )
            pg.text(20, y + 4, "Information für den Herrn Bundesminister", 13, "arial", "b")
            pg.text(
                20,
                y + 12,
                "Betreff: Lage in der Region; Einschätzung der Botschaft",
                10.5,
                "arial",
                "b",
            )
            pg.paras(
                20,
                y + 22,
                170,
                [
                    "Die Botschaft berichtet über eine deutliche Verschlechterung der "
                    "Sicherheitslage "
                    "in der Hauptstadt. Österreichische Staatsbürgerinnen und Staatsbürger sind "
                    "derzeit nicht unmittelbar betroffen.",
                    d.pick(EN_GOV, 2),
                    d.pick(DE_ADMIN, 3),
                ],
                10.5,
                justify=True,
            )
        else:
            y = pg.paras(
                20, 20, 170, [d.pick(DE_ADMIN, 3), d.pick(DE_EXERCISE, 2)], 10.5, justify=True
            )
            pg.text(20, y + 6, "Vorschlag: Kenntnisnahme.", 10.5, "arial", "b")
            pg.text(20, y + 18, "Wien, am 11. September 2026", 10.5)
            pg.text(120, y + 18, "Gesandter Dr. Leitner", 10.5)


@document(
    "letter",
    "at-letter",
    scheme="at",
    level=4,
    label="AT STRENG GEHEIM",
    dpi=200,
    degradation="skew",
    tags=("header", "footer", "red"),
)
def p_at_streng_geheim(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "STRENG GEHEIM", "c", 12, "arial", "b", RED, y=7)
    footer(d, pg, "STRENG GEHEIM", pt=12, family="arial", style="b", fill=RED, y=283)
    y = letterhead_at(
        pg,
        ["Bundeskanzleramt"],
        ["Ballhausplatz 2, 1010 Wien", "GZ: 2026-0.600.013", "Abteilung I/8 – Krisenmanagement"],
    )
    pg.text(
        20, y + 6, "Information an die Mitglieder des Nationalen Sicherheitsrates", 12, "arial", "b"
    )
    pg.text(
        20,
        y + 14,
        "Betreff: Vorsorgeplanung für den Ausfall der Stromversorgung",
        10.5,
        "arial",
        "b",
    )
    y = pg.paras(
        20,
        y + 24,
        170,
        [
            "Die Vorsorgeplanung für einen länger andauernden Ausfall der Stromversorgung wurde "
            "auf "
            "Grundlage der Übungserfahrungen überarbeitet. Die Standorte der Ersatzführungsstellen "
            "sind in der Anlage angeführt.",
            d.pick(DE_EXERCISE, 3),
            d.pick(DE_ADMIN, 3),
        ],
        10.5,
        justify=True,
    )
    pg.text(20, y + 6, "Wien, am 29. August 2026", 10.5)
    pg.text(120, y + 6, "Für den Bundeskanzler:", 10.5)
    pg.text(120, y + 17, "Mag. Eva Fuchs", 10.5)


def ch_footer(pg: Page, office: str, street: str) -> None:
    y = 272.0
    for line in (office, street, "3003 Bern", "Tel. +41 58 462 00 00"):
        pg.text(20, y, line, 7, "arial", "r", GREY)
        y += 3.1


@document(
    "letter",
    "ch-letter",
    scheme="ch",
    level=1,
    label="CH INTERN",
    dpi=200,
    tags=("header", "right"),
)
def p_ch_intern(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "INTERN", "r", 10, "arial", "b", y=10)
    y = letterhead_ch(
        pg,
        "Eidgenössisches Departement für Verteidigung, Bevölkerungsschutz und Sport VBS",
        "Bundesamt für Bevölkerungsschutz BABS",
        top=16,
    )
    ch_footer(pg, "Bundesamt für Bevölkerungsschutz BABS", "Guisanplatz 1B")
    y = address_window(
        pg,
        y + 10,
        "BABS, Guisanplatz 1B, 3003 Bern",
        ["Kantonale Führungsorgane", "gemäss Verteiler"],
    )
    pg.text(20, y + 10, "Referenz/Aktenzeichen: 350.12-0045", 8.5, "arial", "r", GREY)
    pg.text(20, y + 14, "Unser Zeichen: kue", 8.5, "arial", "r", GREY)
    pg.text(120, y + 14, "Bern, 7. September 2026", 10.5)
    y += 26
    pg.text(20, y, "Überarbeitung der Alarmierungskonzepte der Kantone", 11, "arial", "b")
    paras = [
        swiss(p)
        for p in (
            "Die Überprüfung der kantonalen Alarmierungskonzepte ist abgeschlossen. Die Ergebnisse "
            "werden den kantonalen Führungsorganen mit diesem Schreiben zur Kenntnis gebracht.",
            d.pick(DE_ADMIN, 3),
            "Wir danken Ihnen für die gute Zusammenarbeit und stehen für Fragen gerne zur "
            "Verfügung.",
        )
    ]
    body_text(
        pg,
        y + 9,
        "Sehr geehrte Damen und Herren",
        paras,
        [
            "Freundliche Grüsse",
            "",
            "Bundesamt für Bevölkerungsschutz BABS",
            "",
            "",
            "Regula Kühne",
            "Chefin Geschäftsbereich Alarmierung",
        ],
    )


@document(
    "letter",
    "ch-multipage",
    scheme="ch",
    level=2,
    label="CH VERTRAULICH",
    fmt="pdf",
    dpi=200,
    tags=("header", "stamp", "every-page", "multi-page", "red"),
)
def p_ch_vertraulich(d: Doc) -> None:
    for n in (1, 2):
        pg = d.page()
        header(d, pg, "VERTRAULICH", "r", 11, "arial", "b", y=10)
        page_numbers(pg, n, 2, fmt="{n}/{total}", y=290, align="r")
        if n == 1:
            y = letterhead_ch(
                pg,
                "Eidgenössisches Finanzdepartement EFD",
                "Bundesamt für Informatik und Telekommunikation BIT",
                top=16,
            )
            ch_footer(
                pg, "Bundesamt für Informatik und Telekommunikation BIT", "Monbijoustrasse 74"
            )
            stamp(d, pg, ["VERTRAULICH"], 160, 47, pt=16, fill=STAMP_RED, angle=-2, border="box")
            pg.text(20, y + 12, "Aktennotiz", 14, "arial", "b")
            pg.text(
                20,
                y + 20,
                "Sicherheitsvorfall im Netz der Bundesverwaltung vom 28. August",
                10.5,
                "arial",
                "b",
            )
            pg.paras(
                20,
                y + 30,
                170,
                [
                    swiss(p)
                    for p in (
                        "Am 28. August wurden auf zwei Servern der Bundesverwaltung ungewöhnliche "
                        "Verbindungen festgestellt. Die betroffenen Systeme wurden vom Netz "
                        "getrennt.",
                        d.pick(DE_CYBER, 3),
                        d.pick(DE_IT, 3),
                    )
                ],
                10.5,
                justify=True,
            )
        else:
            y = pg.paras(
                20,
                24,
                170,
                [swiss(d.pick(DE_IT, 3)), swiss(d.pick(DE_CYBER, 2))],
                10.5,
                justify=True,
            )
            pg.text(20, y + 8, "Bern, 3. September 2026", 10.5)
            pg.text(120, y + 8, "Stefan Imhof", 10.5)
            pg.text(120, y + 13, "Leiter Sicherheit BIT", 9.5, "arial", "r", GREY)


@document(
    "letter",
    "ch-letter",
    scheme="ch",
    level=3,
    label="CH GEHEIM",
    dpi=240,
    degradation="blur",
    tags=("header", "footer", "centre"),
)
def p_ch_geheim(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "GEHEIM", "c", 12, "arial", "b", y=8)
    footer(d, pg, "GEHEIM", pt=12, family="arial", style="b", y=287)
    y = letterhead_ch(
        pg,
        "Eidgenössisches Departement für Verteidigung, Bevölkerungsschutz und Sport VBS",
        "Nachrichtendienst des Bundes NDB",
        top=16,
    )
    ch_footer(pg, "Nachrichtendienst des Bundes NDB", "Papiermühlestrasse 20")
    pg.text(20, y + 12, "Lagebeurteilung zuhanden des Sicherheitsausschusses", 13, "arial", "b")
    pg.text(20, y + 20, "Bern, 10. September 2026", 10.5)
    y = pg.paras(
        20,
        y + 30,
        170,
        [
            swiss(p)
            for p in (
                "Die Beurteilung stützt sich auf Meldungen aus den Kantonen und auf Erkenntnisse "
                "ausländischer Partnerdienste. Sie gibt den Stand vom 9. September wieder.",
                d.pick(DE_CYBER, 3),
                d.pick(DE_EXERCISE, 2),
                d.pick(DE_ADMIN, 2),
            )
        ],
        10.5,
        justify=True,
    )
    pg.text(20, y + 6, "Christian Aebischer, Chef Beschaffung", 10.5)


@document(
    "study",
    "de-multipage",
    scheme="de",
    level=3,
    label="GEHEIM",
    fmt="tif",
    dpi=150,
    tags=("stamp", "rotated", "steep", "red", "first-page-only", "multi-page", "incomplete"),
    note="only the cover page is stamped; pages 2 and 3 carry no marking",
)
def p_study_cover_stamp(d: Doc) -> None:
    for n in (1, 2, 3):
        pg = d.page()
        page_numbers(pg, n, 3, y=288, align="r", family="garamond")
        if n == 1:
            pg.text(105, 60, "Studie", 14, "garamond", "i", GREY, "c")
            pg.para(
                35,
                72,
                140,
                "Resilienz der Treibstoffversorgung im Spannungsfall",
                22,
                "garamond",
                "b",
                lead=1.2,
            )
            pg.text(
                105,
                110,
                "Arbeitsgruppe Logistik · Stand August 2026",
                11,
                "garamond",
                "r",
                INK,
                "c",
            )
            stamp(
                d,
                pg,
                ["GEHEIM"],
                105,
                150,
                pt=30,
                fill=STAMP_RED,
                angle=-35,
                border="box",
                opacity=0.88,
                track=0.12,
            )
            pg.table(
                45,
                205,
                [50, 70],
                [
                    ["Auftraggeber", "Abteilung Planung"],
                    ["Bearbeitung", "AG Logistik"],
                    ["Umfang", "3 Seiten"],
                ],
                pt=10.5,
                family="garamond",
                header=False,
                bold_cols=(0,),
            )
        else:
            pg.text(
                20,
                22,
                f"{n - 1}. " + ("Ausgangslage" if n == 2 else "Empfehlungen"),
                13,
                "garamond",
                "b",
            )
            pg.paras(
                20,
                32,
                170,
                [d.pick(DE_ADMIN, 3), d.pick(DE_EXERCISE, 3), d.pick(DE_ADMIN, 3)],
                11,
                "garamond",
                justify=True,
            )


# ================================================================ positives: NATO, EU


def nato_head(pg: Page, ref: str, date: str, top: float = 20.0) -> float:
    emblem_nato(pg, 29, top + 8, 8)
    pg.text(41, top + 1, "NORTH ATLANTIC TREATY ORGANIZATION", 10.5, "arial", "b", NAVY)
    pg.text(41, top + 6, "ORGANISATION DU TRAITÉ DE L'ATLANTIQUE NORD", 8.5, "arial", "r", NAVY)
    pg.text(
        41, top + 11, "INTERNATIONAL STAFF · DEFENCE INVESTMENT DIVISION", 8, "arial", "r", GREY
    )
    pg.text(190, top + 22, ref, 10, "arial", "r", INK, "r")
    pg.text(190, top + 27, date, 10, "arial", "r", INK, "r")
    return top + 36


@document(
    "memo",
    "nato-memo",
    scheme="nato",
    level=1,
    label="NATO RESTRICTED",
    dpi=200,
    tags=("header", "footer", "centre"),
)
def p_nato_restricted(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "NATO RESTRICTED", "c", 12, "arial", "b", y=8)
    footer(d, pg, "NATO RESTRICTED", pt=12, family="arial", style="b", y=283)
    y = nato_head(pg, "AC/326-N(2026)0041", "15 September 2026")
    pg.text(105, y, "MEMORANDUM", 13, "arial", "b", INK, "c")
    y += 10
    for label, value in (
        ("To:", "Members of the Life Cycle Management Group"),
        ("From:", "Head, Capability Delivery Section"),
        ("Subject:", "Revised schedule for the air-defence data link upgrade"),
        ("Reference:", "AC/326-D(2026)0012, dated 3 June 2026"),
    ):
        pg.text(20, y, label, 10.5, "arial", "b")
        y = max(pg.para(50, y, 140, value, 10.5), y + 5.2)
    y += 5
    paras = [d.pick(EN_GOV, 3), d.pick(EN_GOV, 3), d.pick(EN_GOV, 2)]
    for i, p in enumerate(paras, 1):
        pg.text(20, y, f"{i}.", 10.5)
        y = pg.para(28, y, 162, p, 10.5, justify=True) + 3
    signature(pg, 120, y + 10, ["(Signed) J. Van den Berg", "Head, Capability Delivery Section"])
    pg.text(105, 277, "1", 9, "arial", "r", GREY, "c")


@document(
    "memo",
    "nato-stamp",
    scheme="nato",
    level=2,
    label="NATO CONFIDENTIAL",
    dpi=200,
    degradation="aged",
    tags=("stamp", "top", "bottom"),
)
def p_nato_confidential(d: Doc) -> None:
    pg = d.page()
    stamp(d, pg, ["NATO CONFIDENTIAL"], 105, 11, pt=13, fill=STAMP_RED, border="box", angle=0.8)
    stamp(d, pg, ["NATO CONFIDENTIAL"], 105, 285, pt=13, fill=STAMP_RED, border="box", angle=-0.6)
    y = nato_head(pg, "JWC/PLANS/2026/0877", "2 September 2026", top=22)
    pg.text(20, y, "SUBJECT: EXERCISE STEADFAST HORIZON 27 – LESSONS IDENTIFIED", 11, "arial", "b")
    y = pg.paras(
        20,
        y + 10,
        170,
        [d.pick(EN_GOV, 3), d.pick(EN_GOV, 3), d.pick(EN_GOV, 2)],
        10.5,
        justify=True,
    )
    y = pg.table(
        20,
        y + 4,
        [15, 110, 45],
        [
            ["No", "Lesson identified", "Action body"],
            ["1", "Track numbering conflicts between the two air pictures", "CIS Division"],
            ["2", "Late arrival of liaison officers at the combined HQ", "J1"],
            ["3", "Insufficient fuel storage at the forward airfield", "J4"],
        ],
        pt=9.5,
    )
    signature(pg, 120, y + 12, ["Brigadier General L. Moreau", "Chief of Staff"])


@document(
    "minutes",
    "nato-multipage",
    scheme="nato",
    level=3,
    label="NATO SECRET",
    fmt="tif",
    dpi=200,
    tags=("header", "footer", "every-page", "multi-page", "table"),
)
def p_nato_secret(d: Doc) -> None:
    for n in (1, 2, 3):
        pg = d.page()
        header(d, pg, "NATO SECRET", "c", 13, "times", "b", y=8)
        footer(d, pg, "NATO SECRET", pt=13, family="times", style="b", y=282)
        pg.text(105, 289, f"- {n} -", 9, "times", "r", GREY, "c")
        if n == 1:
            y = nato_head(pg, "MC-WG(2026)SR/07", "8 September 2026")
            pg.text(105, y, "SUMMARY RECORD", 13, "times", "b", INK, "c")
            pg.text(
                105,
                y + 6,
                "of the meeting of the Military Committee Working Group",
                11,
                "times",
                "r",
                INK,
                "c",
            )
            pg.text(105, y + 11, "held on 1 September 2026", 11, "times", "r", INK, "c")
            y = pg.paras(
                20, y + 22, 170, [d.pick(EN_GOV, 3), d.pick(EN_GOV, 3)], 11, "times", justify=True
            )
        elif n == 2:
            y = pg.paras(20, 22, 170, [d.pick(EN_GOV, 3)], 11, "times", justify=True)
            y = pg.table(
                20,
                y + 4,
                [20, 100, 50],
                [
                    ["Item", "Decision / action", "Lead"],
                    ["1", "Endorse revised force package for the southern flank", "IMS Ops"],
                    ["2", "Prepare options for additional air-to-air refuelling", "IMS Plans"],
                    ["3", "Report on readiness of the high-readiness brigade", "SHAPE"],
                    ["4", "Review of the intelligence sharing arrangements", "JISD"],
                ],
                pt=10,
                family="times",
            )
            pg.paras(20, y + 6, 170, [d.pick(EN_GOV, 3)], 11, "times", justify=True)
        else:
            y = pg.paras(
                20, 22, 170, [d.pick(EN_GOV, 3), d.pick(EN_GOV, 2)], 11, "times", justify=True
            )
            signature(pg, 120, y + 10, ["Captain (N) R. Almeida", "Secretary"], "times", 11)


@document(
    "cover",
    "nato-cover",
    scheme="nato",
    level=4,
    label="COSMIC TOP SECRET",
    fmt="jpg",
    dpi=200,
    degradation="jpeg",
    tags=("cover", "header", "footer", "red", "large"),
)
def p_cosmic(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "COSMIC TOP SECRET", "c", 16, "arial", "b", RED, y=8)
    footer(d, pg, "COSMIC TOP SECRET", pt=16, family="arial", style="b", fill=RED, y=280)
    pg.rect(22, 28, 166, 240, outline=RED, width=1.5)
    emblem_nato(pg, 105, 52, 12)
    pg.text(105, 70, "COVER AND RECEIPT SHEET", 14, "arial", "b", INK, "c")
    pg.table(
        40,
        85,
        [55, 75],
        [
            ["Control number", "CTS-26-0019"],
            ["Copy", "1 of 4"],
            ["Originator", "SACEUR – Strategic Planning"],
            ["Date of document", "12 August 2026"],
            ["Number of pages", "22"],
        ],
        pt=11,
        header=False,
        bold_cols=(0,),
    )
    pg.text(40, 145, "ACCESS RECORD", 11, "arial", "b")
    pg.table(
        40,
        152,
        [45, 45, 40],
        [
            ["Name", "Date", "Signature"],
            ["", "", ""],
            ["", "", ""],
            ["", "", ""],
            ["", "", ""],
        ],
        pt=10,
        lead=2.0,
    )


def eu_head(pg: Page, number: str, date: str, top: float = 22.0) -> float:
    emblem_eu(pg, 20, top, 20)
    pg.text(44, top + 1, "COUNCIL OF", 11, "times", "b")
    pg.text(44, top + 6, "THE EUROPEAN UNION", 11, "times", "b")
    pg.text(190, top + 1, date, 10.5, "times", "r", INK, "r")
    pg.text(190, top + 6, "(OR. en)", 10.5, "times", "r", INK, "r")
    pg.text(190, top + 14, number, 12, "times", "b", INK, "r")
    return top + 26


def eu_foot(d: Doc, pg: Page, number: str, n: int, marking: str) -> None:
    pg.hline(20, 190, 277, 0.2)
    pg.text(20, 279, number, 9, "times")
    pg.text(50, 279, "KL/mf", 9, "times")
    pg.text(190, 279, str(n), 9, "times", "r", INK, "r")
    pg.text(20, 284, "RELEX.2.B", 9, "times")
    footer(d, pg, marking, pt=9, family="times", style="b", y=284)
    pg.text(190, 284, "EN", 9, "times", "r", INK, "r")


@document(
    "note",
    "eu-note",
    scheme="eu",
    level=1,
    label="RESTREINT UE",
    dpi=200,
    tags=("header", "footer", "boxed", "bilingual"),
)
def p_eu_restreint(d: Doc) -> None:
    pg = d.page()
    marking = "RESTREINT UE/EU RESTRICTED"
    header(d, pg, marking, "c", 11, "times", "b", y=9, box=True)
    y = eu_head(pg, "10432/26", "Brussels, 4 June 2026")
    eu_foot(d, pg, "10432/26", 1, marking)
    pg.text(105, y + 4, "NOTE", 13, "times", "b", INK, "c")
    y += 14
    for label, value in (
        ("From:", "General Secretariat of the Council"),
        ("To:", "Delegations"),
        ("Subject:", "Civilian mission in the region – options for the renewal of the mandate"),
    ):
        pg.text(20, y, label, 11, "times", "b")
        y = max(pg.para(50, y, 140, value, 11, "times"), y + 5.5) + 1
    pg.hline(20, 190, y + 2, 0.3)
    y += 8
    for i, p in enumerate([d.pick(EN_GOV, 3), d.pick(EN_GOV, 3), d.pick(EN_GOV, 3)], 1):
        pg.text(20, y, f"{i}.", 11, "times")
        y = pg.para(30, y, 160, p, 11, "times", justify=True) + 3


@document(
    "note",
    "eu-multipage",
    scheme="eu",
    level=2,
    label="CONFIDENTIEL UE",
    fmt="pdf",
    dpi=200,
    tags=("header", "footer", "boxed", "bilingual", "every-page", "multi-page"),
)
def p_eu_confidentiel(d: Doc) -> None:
    marking = "CONFIDENTIEL UE/EU CONFIDENTIAL"
    for n in (1, 2):
        pg = d.page()
        header(d, pg, marking, "c", 11, "times", "b", y=9, box=True)
        eu_foot(d, pg, "11873/26", n, marking)
        if n == 1:
            y = eu_head(pg, "11873/26", "Brussels, 30 June 2026")
            pg.text(105, y + 4, "NOTE", 13, "times", "b", INK, "c")
            y += 14
            for label, value in (
                ("From:", "European External Action Service"),
                ("To:", "Political and Security Committee"),
                ("Subject:", "Threat assessment for the maritime operation"),
            ):
                pg.text(20, y, label, 11, "times", "b")
                y = max(pg.para(50, y, 140, value, 11, "times"), y + 5.5) + 1
            pg.hline(20, 190, y + 2, 0.3)
            pg.paras(
                20,
                y + 8,
                170,
                [d.pick(EN_GOV, 3), d.pick(EN_CYBER, 3), d.pick(EN_GOV, 3)],
                11,
                "times",
                justify=True,
            )
        else:
            pg.paras(
                20,
                24,
                170,
                [d.pick(EN_GOV, 4), d.pick(EN_GOV, 3), d.pick(EN_CYBER, 2)],
                11,
                "times",
                justify=True,
            )


@document(
    "note",
    "eu-note",
    scheme="eu",
    level=3,
    label="SECRET UE",
    dpi=200,
    degradation="fax",
    tags=("header", "footer", "bilingual"),
)
def p_eu_secret_fax(d: Doc) -> None:
    pg = d.page()
    pg.text(
        20,
        3,
        "14/09/2026 17:02  FAX +32 2 281 0000   GSC SECURITY OFFICE   P.001/002",
        8,
        "courier",
    )
    marking = "SECRET UE/EU SECRET"
    header(d, pg, marking, "c", 13, "times", "b", y=10)
    footer(d, pg, marking, pt=13, family="times", style="b", y=283)
    y = eu_head(pg, "12002/26", "Brussels, 14 September 2026")
    pg.text(105, y + 4, "NOTE", 13, "times", "b", INK, "c")
    y += 14
    for label, value in (
        ("From:", "EU Intelligence and Situation Centre"),
        ("To:", "Political and Security Committee"),
        ("Subject:", "Situation update"),
    ):
        pg.text(20, y, label, 11, "times", "b")
        y = max(pg.para(50, y, 140, value, 11, "times"), y + 5.5) + 1
    pg.paras(20, y + 8, 170, [d.pick(EN_GOV, 3), d.pick(EN_CYBER, 3)], 11, "times", justify=True)


@document(
    "cover",
    "eu-cover",
    scheme="eu",
    level=4,
    label="TRÈS SECRET UE",
    dpi=200,
    degradation="lowdpi",
    tags=("cover", "header", "footer", "bilingual", "large"),
)
def p_eu_tres_secret(d: Doc) -> None:
    pg = d.page()
    marking = "TRÈS SECRET UE/EU TOP SECRET"
    header(d, pg, marking, "c", 16, "times", "b", y=10)
    footer(d, pg, marking, pt=16, family="times", style="b", y=278)
    emblem_eu(pg, 90, 40, 30)
    pg.text(105, 66, "COUNCIL OF THE EUROPEAN UNION", 13, "times", "b", INK, "c")
    pg.text(105, 74, "Registry Control Sheet", 12, "times", "r", INK, "c")
    pg.table(
        35,
        92,
        [60, 80],
        [
            ["Registration number", "TS-26/004"],
            ["Copy number", "2 of 3"],
            ["Originator", "Crisis Management Directorate"],
            ["Number of pages", "9"],
        ],
        pt=11,
        family="times",
        header=False,
        bold_cols=(0,),
    )
    pg.para(
        35,
        140,
        140,
        "This document may only be handled by persons who are listed on the "
        "access register below. Each access is to be recorded.",
        10.5,
        "times",
    )
    pg.table(
        35,
        160,
        [50, 45, 45],
        [
            ["Name", "Date and time", "Signature"],
            ["", "", ""],
            ["", "", ""],
            ["", "", ""],
        ],
        pt=10,
        family="times",
        lead=2.0,
    )


# ================================================================ positives: US, UK, FR


def us_head(pg: Page, agency: str, office: str, city: str, top: float = 20.0) -> float:
    emblem_seal(pg, 32, top + 10, 11)
    pg.text(112, top + 1, agency, 13, "times", "b", NAVY, "c")
    pg.text(112, top + 8, office, 10, "times", "r", NAVY, "c")
    pg.text(112, top + 13, city, 10, "times", "r", NAVY, "c")
    return top + 30


def us_block(pg: Page, y: float, by: str, derived: str, declassify: str) -> None:
    for label, value in (
        ("Classified By:", by),
        ("Derived From:", derived),
        ("Declassify On:", declassify),
    ):
        pg.text(20, y, label, 10, "courier")
        pg.text(55, y, value, 10, "courier")
        y += 4.6


@document(
    "memo",
    "us-memo",
    scheme="us",
    level=3,
    label="US SECRET",
    dpi=200,
    tags=("banner", "caveat", "portion-marks", "authority-block"),
)
def p_us_secret(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "SECRET//NOFORN", "c", 12, "arial", "b", y=8)
    footer(d, pg, "SECRET//NOFORN", pt=12, family="arial", style="b", y=284)
    y = us_head(
        pg,
        "DEPARTMENT OF DEFENSE",
        "OFFICE OF THE UNDER SECRETARY FOR ACQUISITION",
        "WASHINGTON, DC 20301-3010",
    )
    pg.text(190, y, "September 14, 2026", 11, "times", "r", INK, "r")
    y += 10
    pg.text(20, y, "MEMORANDUM FOR DIRECTOR, JOINT RAPID ACQUISITION CELL", 11, "times")
    pg.text(20, y + 9, "SUBJECT: (U) Accelerated Fielding of Counter-UAS Sensors", 11, "times")
    y += 20
    for mark, text in (
        ("(S//NF)", d.pick(EN_GOV, 3)),
        ("(U)", d.pick(EN_GOV, 2)),
        ("(S//NF)", d.pick(EN_CYBER, 2)),
        ("(U)", d.pick(EN_GOV, 2)),
    ):
        y = pg.para(20, y, 170, f"{mark} {text}", 11, "times", justify=True) + 3.5
    signature(
        pg,
        115,
        y + 10,
        ["Michael T. Reyes", "Deputy Director, Acquisition Integration"],
        "times",
        11,
    )
    us_block(pg, 252, "M. T. Reyes, Deputy Director", "DoD SCG ACQ-12, dated 20240315", "20511231")


@document(
    "memo",
    "us-memo",
    scheme="us",
    level=1,
    label="US CUI",
    dpi=200,
    tags=("banner", "portion-marks", "designation-block"),
)
def p_us_cui(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "CUI", "c", 13, "arial", "b", y=8)
    footer(d, pg, "CUI", pt=13, family="arial", style="b", y=284)
    y = us_head(
        pg,
        "U.S. DEPARTMENT OF ENERGY",
        "Office of Cybersecurity and Emergency Response",
        "Washington, DC 20585",
    )
    pg.text(190, y, "September 2, 2026", 11, "times", "r", INK, "r")
    y += 10
    pg.text(20, y, "MEMORANDUM FOR SITE SECURITY MANAGERS", 11, "times")
    pg.text(
        20, y + 9, "SUBJECT: Reporting Timelines for Grid Control System Incidents", 11, "times"
    )
    y += 20
    for mark, text in (
        ("(CUI)", d.pick(EN_CYBER, 3)),
        ("(U)", d.pick(EN_GOV, 2)),
        ("(CUI)", d.pick(EN_CYBER, 3)),
    ):
        y = pg.para(20, y, 170, f"{mark} {text}", 11, "times", justify=True) + 3.5
    signature(pg, 115, y + 8, ["Dana L. Whitfield", "Director, Incident Coordination"], "times", 11)
    pg.rect(20, 244, 95, 26, outline=INK, width=0.3)
    yy = 246.0
    for line in (
        "Controlled by: Department of Energy",
        "Controlled by: CESER / IC",
        "CUI Category: CRIT",
        "Distribution/Dissemination Control: FEDCON",
        "POC: D. Whitfield, (202) 555-0147",
    ):
        pg.text(22, yy, line, 8.5, "arial")
        yy += 4.4


@document(
    "memo",
    "us-memo",
    scheme="us",
    level=2,
    label="US CONFIDENTIAL",
    fmt="jpg",
    dpi=200,
    degradation="jpeg",
    tags=("banner", "portion-marks", "authority-block"),
)
def p_us_confidential(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "CONFIDENTIAL", "c", 12, "arial", "b", y=8)
    footer(d, pg, "CONFIDENTIAL", pt=12, family="arial", style="b", y=284)
    y = us_head(
        pg, "DEPARTMENT OF THE NAVY", "NAVAL SUPPLY SYSTEMS COMMAND", "MECHANICSBURG, PA 17055"
    )
    pg.text(20, y, "IN REPLY REFER TO:", 8, "times")
    pg.text(20, y + 4, "4400 Ser N41/0233", 10, "times")
    pg.text(190, y + 4, "8 Sep 26", 10, "times", "r", INK, "r")
    y += 14
    for label, value in (
        ("From:", "Commander, Naval Supply Systems Command"),
        ("To:", "Commanding Officer, Fleet Logistics Center Norfolk"),
        ("Subj:", "(U) SPARE PARTS POSITIONING FOR DEPLOYED UNITS"),
    ):
        pg.text(20, y, label, 11, "times")
        pg.text(40, y, value, 11, "times")
        y += 5.5
    y += 5
    for i, (mark, text) in enumerate(
        (("(C)", d.pick(EN_GOV, 3)), ("(U)", d.pick(EN_GOV, 3)), ("(C)", d.pick(EN_GOV, 2))), 1
    ):
        y = pg.para(20, y, 170, f"{i}. {mark} {text}", 11, "times", justify=True) + 3.5
    signature(pg, 115, y + 8, ["K. J. OSBORNE", "By direction"], "times", 11)
    us_block(pg, 256, "K. J. Osborne, N41", "OPNAVINST S4400.9", "20360908")


@document(
    "memo",
    "us-multipage",
    scheme="us",
    level=4,
    label="US TOP SECRET",
    fmt="pdf",
    dpi=150,
    tags=("banner", "caveat", "portion-marks", "authority-block", "multi-page", "every-page"),
)
def p_us_top_secret(d: Doc) -> None:
    banner = "TOP SECRET//SI//NOFORN"
    for n in (1, 2):
        pg = d.page()
        header(d, pg, banner, "c", 12, "arial", "b", y=8)
        footer(d, pg, banner, pt=12, family="arial", style="b", y=284)
        pg.text(190, 289, str(n), 9, "times", "r", GREY, "r")
        if n == 1:
            y = us_head(
                pg, "NATIONAL COORDINATION CENTER", "Office of the Director", "Fort Meade, MD 20755"
            )
            pg.text(20, y + 4, "MEMORANDUM FOR THE RECORD", 11, "times", "b")
            pg.text(
                20, y + 12, "SUBJECT: (U) Collection Priorities for Fiscal Year 2027", 11, "times"
            )
            y += 24
            texts = [
                ("(TS//SI//NF)", d.pick(EN_GOV, 3)),
                ("(U)", d.pick(EN_GOV, 2)),
                ("(TS//SI//NF)", d.pick(EN_CYBER, 3)),
            ]
        else:
            y = 22.0
            texts = [("(S//NF)", d.pick(EN_GOV, 3)), ("(U)", d.pick(EN_GOV, 2))]
        for mark, text in texts:
            y = pg.para(20, y, 170, f"{mark} {text}", 11, "times", justify=True) + 3.5
        if n == 2:
            signature(pg, 115, y + 8, ["A. R. Lindgren", "Director"], "times", 11)
            us_block(
                pg,
                254,
                "A. R. Lindgren, Director",
                "NCC/CSS Manual 1-52, 20250108",
                "25X1, 20760101",
            )


@document(
    "letter",
    "uk-letter",
    scheme="uk",
    level=1,
    label="UK OFFICIAL-SENSITIVE",
    dpi=200,
    tags=("header", "footer", "centre"),
)
def p_uk_official_sensitive(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "OFFICIAL-SENSITIVE", "c", 11, "arial", "b", y=8)
    footer(d, pg, "OFFICIAL-SENSITIVE", pt=11, family="arial", style="b", y=284)
    emblem_seal(pg, 30, 32, 9, (0, 90, 70))
    pg.text(44, 25, "Department for Infrastructure Resilience", 13, "arial", "b", (0, 90, 70))
    pg.text(44, 32, "2 Marsham Street, London SW1P 4DF", 9, "arial", "r", GREY)
    pg.text(44, 36.5, "www.gov.uk/dir", 9, "arial", "r", GREY)
    y = address_window(
        pg,
        52,
        "DIR, 2 Marsham Street, London SW1P 4DF",
        ["Ms Helen Carter", "Chief Executive", "Northern Water Resilience Board", "Leeds LS1 4AP"],
    )
    pg.text(190, 60, "Our ref: DIR/2026/0419", 10, "arial", "r", INK, "r")
    pg.text(190, 65, "11 September 2026", 10, "arial", "r", INK, "r")
    y += 10
    pg.text(20, y, "Dear Ms Carter,", 10.5)
    pg.text(
        20, y + 9, "Resilience of treatment works: findings of the 2026 review", 10.5, "arial", "b"
    )
    y = pg.paras(
        20,
        y + 18,
        170,
        [
            "Thank you for your support during this year's review. I enclose the findings for the "
            "treatment works in your area, including the sites where single points of failure were "
            "identified.",
            d.pick(EN_GOV, 3),
            "Please share the findings only with those staff who need them to plan the remedial "
            "work.",
        ],
        10.5,
    )
    signature(
        pg,
        20,
        y + 6,
        ["Yours sincerely,", "", "", "Robert Anand", "Director, Critical Infrastructure"],
    )


@document(
    "note",
    "fr-note",
    scheme="fr",
    level=1,
    label="FR DIFFUSION RESTREINTE",
    dpi=240,
    tags=("header", "footer", "centre"),
)
def p_fr_dr(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "DIFFUSION RESTREINTE", "c", 12, "arial", "b", y=8)
    footer(d, pg, "DIFFUSION RESTREINTE", pt=12, family="arial", style="b", y=284)
    emblem_tricolore(pg, 20, 20, 13)
    pg.text(20, 30, "RÉPUBLIQUE", 8.5, "arial", "b")
    pg.text(20, 34, "FRANÇAISE", 8.5, "arial", "b")
    pg.text(20, 39, "Liberté", 7.5, "garamond", "i")
    pg.text(20, 42.2, "Égalité", 7.5, "garamond", "i")
    pg.text(20, 45.4, "Fraternité", 7.5, "garamond", "i")
    pg.text(70, 22, "MINISTÈRE DES ARMÉES", 11, "arial", "b")
    pg.text(70, 27.5, "Direction générale de l'armement", 10, "arial")
    pg.text(70, 32, "Service des programmes de communication", 9, "arial", "r", GREY)
    pg.text(190, 55, "Paris, le 10 septembre 2026", 10.5, "arial", "r", INK, "r")
    pg.text(190, 60, "N° 2026-004512/DGA/SPC", 10.5, "arial", "r", INK, "r")
    pg.text(105, 75, "NOTE", 14, "arial", "b", INK, "c")
    y = 86.0
    for label, value in (
        ("à l'attention de :", "Monsieur le directeur des opérations"),
        ("Objet :", "Point d'avancement du programme de radios tactiques"),
        ("Référence :", "Décision de lancement du 12 mars 2025"),
    ):
        pg.text(20, y, label, 10.5, "arial", "b")
        y = max(pg.para(62, y, 128, value, 10.5), y + 5.5) + 1
    y = pg.paras(
        20,
        y + 8,
        170,
        [d.pick(FR_ADMIN, 3), d.pick(FR_ADMIN, 3), d.pick(FR_ADMIN, 2)],
        10.5,
        justify=True,
    )
    signature(pg, 120, y + 10, ["L'ingénieur général de l'armement", "", "", "Claire Dumont"])


# ================================================================ positives: TLP


def tlp_badge(
    d: Doc,
    pg: Page,
    label: str,
    x: float,
    y: float,
    colour,
    pt: float = 11,
    align: str = "r",
    gov: bool = False,
) -> None:
    """A TLP label as the standard prints it: coloured letters on a black box."""
    w = pg.width(label, pt, "arial", "b")
    left = x - w if align == "r" else x
    pg.rect(left - 2, y - 1, w + 4, pt * 0.47 + 1.6, outline=None, fill=(0, 0, 0))
    pg.text(left, y, label, pt, "arial", "b", colour)
    d.mark(pg, label, gov)


TLP_COLOURS = {
    "RED": (255, 43, 43),
    "AMBER": (255, 192, 0),
    "GREEN": (51, 255, 0),
    "CLEAR": (255, 255, 255),
}


def advisory(pg: Page, org: str, ident: str, title: str, lang: str = "de") -> float:
    pg.rect(20, 18, 170, 16, outline=None, fill=(28, 36, 52))
    pg.text(24, 20.5, org, 13, "arial", "b", WHITE)
    pg.text(
        24,
        27.5,
        "Sicherheitshinweis" if lang == "de" else "Security Advisory",
        9,
        "arial",
        "r",
        (190, 200, 215),
    )
    pg.text(20, 42, title, 14, "arial", "b")
    rows = (
        [
            ["Kennung", ident],
            ["Datum", "14.09.2026"],
            ["Schweregrad", "hoch (CVSS 8.8)"],
            ["Betroffen", "Fernwartungsgateways RG-200 und RG-210, Firmware < 4.2.7"],
        ]
        if lang == "de"
        else [
            ["Identifier", ident],
            ["Date", "14 September 2026"],
            ["Severity", "critical (9.8)"],
            ["Affected", "VPN appliances of the NX series, firmware before 7.1.3"],
        ]
    )
    return pg.table(20, 52, [40, 130], rows, pt=10, header=False, bold_cols=(0,))


@document(
    "advisory",
    "tlp-advisory",
    scheme="tlp",
    tlp="RED",
    dpi=200,
    tags=("tlp-box", "header", "footer"),
)
def p_tlp_red(d: Doc) -> None:
    pg = d.page()
    tlp_badge(d, pg, "TLP:RED", 190, 8, TLP_COLOURS["RED"], 12)
    tlp_badge(d, pg, "TLP:RED", 190, 283, TLP_COLOURS["RED"], 12)
    y = advisory(
        pg,
        "CERT-Nord Energieverbund",
        "CN-2026-0913",
        "Aktive Ausnutzung einer Schwachstelle in Fernwartungsgateways",
    )
    pg.text(20, y + 6, "Beschreibung", 11, "arial", "b")
    y = pg.paras(20, y + 12, 170, [d.pick(DE_CYBER, 3), d.pick(DE_CYBER, 2)], 10.5)
    pg.text(20, y + 3, "Indikatoren", 11, "arial", "b")
    y = pg.table(
        20,
        y + 9,
        [60, 110],
        [
            ["Typ", "Wert"],
            ["IP-Adresse", "203.0.113.47"],
            ["IP-Adresse", "198.51.100.212"],
            ["SHA-256", "9f2c…e41a (Loader)"],
            ["Domain", "update-check.example.net"],
        ],
        pt=9.5,
        family="mono",
    )
    pg.text(20, y + 5, "Empfehlung", 11, "arial", "b")
    pg.paras(20, y + 11, 170, [d.pick(DE_CYBER, 2)], 10.5)


@document(
    "advisory",
    "tlp-multipage",
    scheme="tlp",
    tlp="AMBER+STRICT",
    fmt="pdf",
    dpi=200,
    tags=("tlp-plain", "header", "footer", "multi-page"),
)
def p_tlp_amber_strict(d: Doc) -> None:
    for n in (1, 2):
        pg = d.page()
        header(d, pg, "TLP:AMBER+STRICT", "r", 11, "arial", "b", y=8, gov=False)
        footer(
            d, pg, "TLP:AMBER+STRICT", align="c", pt=10, family="arial", style="b", y=285, gov=False
        )
        page_numbers(pg, n, 2, fmt="Page {n} of {total}", y=290, align="r")
        if n == 1:
            y = advisory(
                pg,
                "Water Sector ISAC",
                "WS-ISAC-26-117",
                "Critical vulnerability in NX-series VPN appliances",
                lang="en",
            )
            pg.text(20, y + 6, "Summary", 11, "arial", "b")
            pg.paras(20, y + 12, 170, [d.pick(EN_CYBER, 4), d.pick(EN_CYBER, 3)], 10.5)
        else:
            pg.text(20, 22, "Recommended actions", 11, "arial", "b")
            y = pg.bullets(
                20,
                30,
                170,
                [
                    "Apply firmware 7.1.3 or later on all appliances.",
                    "Restrict access to the management interface to the administration network.",
                    "Reset credentials of all local administrator accounts.",
                    "Review logs from 1 August onwards for the indicators listed below.",
                ],
                10.5,
            )
            pg.paras(20, y + 4, 170, [d.pick(EN_CYBER, 3)], 10.5)


@document(
    "email",
    "tlp-email",
    scheme="tlp",
    tlp="GREEN",
    dpi=200,
    tags=("email-subject", "tlp-plain", "body-line"),
)
def p_tlp_green_email(d: Doc) -> None:
    pg = d.page()
    y = outlook(
        pg,
        "IT-Sicherheit Stadtwerke Neuweiler",
        [
            ("Von:", "CERT-Nord Energieverbund <cert@cert-nord.example>"),
            ("Gesendet:", "Montag, 14. September 2026 09:15"),
            ("An:", "Verteiler Mitglieder CERT-Nord"),
            ("Betreff:", "[TLP:GREEN] Phishing-Welle mit gefälschten Paketbenachrichtigungen"),
        ],
    )
    d.mark(pg, "TLP:GREEN", gov=False)
    pg.text(20, y, "TLP:GREEN", 11, "arial", "b", GREEN)
    y = pg.paras(
        20,
        y + 8,
        170,
        [
            "Liebe Kolleginnen und Kollegen,",
            d.pick(DE_CYBER, 3),
            "Die Absenderadressen und Betreffzeilen der bisher gemeldeten Nachrichten finden Sie "
            "in "
            "der Tabelle.",
        ],
        10.5,
    )
    y = pg.table(
        20,
        y + 2,
        [80, 90],
        [
            ["Absender", "Betreff"],
            ["zustellung@paket-info.example", "Ihre Sendung konnte nicht zugestellt werden"],
            ["service@dhl-status.example", "Zollgebühr offen – Sendung 7781"],
            ["info@hermes-track.example", "Neuer Zustelltermin"],
        ],
        pt=9.5,
    )
    pg.paras(20, y + 6, 170, ["Viele Grüße", "Ihr CERT-Nord"], 10.5)


@document(
    "bulletin",
    "tlp-bulletin",
    scheme="tlp",
    tlp="CLEAR",
    dpi=150,
    tags=("tlp-box", "two-column", "header"),
)
def p_tlp_clear(d: Doc) -> None:
    pg = d.page()
    tlp_badge(d, pg, "TLP:CLEAR", 190, 9, TLP_COLOURS["CLEAR"], 11)
    pg.rect(20, 18, 170, 18, outline=None, fill=(0, 84, 140))
    pg.text(24, 20.5, "Sicherheits-Newsletter", 18, "arial", "b", WHITE)
    pg.text(24, 29.5, "Ausgabe 9/2026 · für alle Mitarbeitenden", 9, "arial", "r", WHITE)
    two_columns(
        pg,
        44,
        [
            ("Updates am Wochenende", [d.pick(DE_IT, 3)]),
            ("Vorsicht bei Paket-SMS", [d.pick(DE_CYBER, 2)]),
        ],
        [
            ("Neue Passwortregeln", [d.pick(DE_IT, 2)]),
            ("Meldewege", [d.pick(DE_CYBER, 2), d.pick(DE_IT, 1)]),
        ],
        family="arial",
        colour=(0, 84, 140),
        pt=10,
        head_pt=12,
    )
    page_numbers(pg, 1, 1, fmt="Seite {n}", y=289)


@document(
    "advisory",
    "tlp-advisory",
    scheme="tlp",
    tlp="AMBER",
    fmt="jpg",
    dpi=200,
    degradation="jpeg",
    tags=("tlp-box", "header"),
)
def p_tlp_amber(d: Doc) -> None:
    pg = d.page()
    tlp_badge(d, pg, "TLP:AMBER", 190, 8, TLP_COLOURS["AMBER"], 12)
    y = advisory(
        pg,
        "CERT-Nord Energieverbund",
        "CN-2026-0921",
        "Kompromittierte Zugangsdaten eines Wartungsdienstleisters",
    )
    pg.text(20, y + 6, "Sachverhalt", 11, "arial", "b")
    y = pg.paras(20, y + 12, 170, [d.pick(DE_CYBER, 3), d.pick(DE_CYBER, 3)], 10.5)
    pg.text(20, y + 3, "Weitergabe", 11, "arial", "b")
    pg.para(
        20,
        y + 9,
        170,
        "Weitergabe nur innerhalb der eigenen Organisation und an "
        "Dienstleister, die zur Umsetzung der Empfehlungen beauftragt sind.",
        10.5,
    )


# ================================================================ positives: companies


@document(
    "board",
    "company-multipage",
    scheme="company",
    company="STRENG VERTRAULICH",
    fmt="pdf",
    dpi=200,
    tags=("header", "red", "every-page", "multi-page", "table"),
)
def p_board_streng_vertraulich(d: Doc) -> None:
    for n in (1, 2):
        pg = d.page()
        header(d, pg, "STRENG VERTRAULICH", "c", 11, "helv", "b", RED, y=7, gov=False)
        page_numbers(pg, n, 2, y=289, align="r", family="helv")
        if n == 1:
            y = letterhead_logo(
                pg,
                "Hansewerk Pumpen AG",
                "Vorstandsvorlage Nr. 2026/41",
                (0, 90, 160),
                "triangle",
                right=["Sitzung am 28.09.2026", "TOP 4"],
                top=15,
            )
            pg.text(
                20, y + 4, "Integration des Standorts Kassel in das Werk Fulda", 14, "helv", "b"
            )
            y = pg.paras(
                20,
                y + 14,
                170,
                [d.pick(DE_CORP, 3), d.pick(DE_CORP, 3)],
                10.5,
                "helv",
                justify=True,
            )
            y = pg.table(
                20,
                y + 2,
                [80, 30, 30, 30],
                [
                    ["Finanzielle Auswirkungen (Mio. €)", "2026", "2027", "2028"],
                    ["Investitionen", "1,2", "4,8", "1,5"],
                    ["Einmalkosten Personal", "0,4", "2,6", "0,3"],
                    ["Einsparungen", "–", "0,9", "2,1"],
                ],
                pt=9.5,
                family="helv",
            )
        else:
            y = pg.paras(20, 22, 170, [d.pick(DE_CORP, 3)], 10.5, "helv", justify=True)
            pg.rect(20, y + 4, 170, 34, outline=(0, 90, 160), width=0.6)
            pg.text(24, y + 7, "Beschlussvorschlag", 11, "helv", "b", (0, 90, 160))
            pg.para(
                24,
                y + 14,
                162,
                "Der Vorstand beschließt die Integration des Standorts "
                "Kassel in das Werk Fulda bis Ende 2027 und beauftragt den Finanzvorstand, "
                "die Zustimmung des Aufsichtsrats einzuholen.",
                10.5,
                "helv",
            )
            pg.paras(20, y + 46, 170, [d.pick(DE_CORP, 2)], 10.5, "helv")


@document(
    "letter",
    "company-stamp",
    scheme="company",
    company="VERTRAULICH",
    dpi=200,
    tags=("stamp", "rotated", "red"),
)
def p_company_vertraulich(d: Doc) -> None:
    pg = d.page()
    y = letterhead_logo(
        pg,
        "Brandt & Söhne Maschinenbau GmbH",
        "Präzisionsteile seit 1952",
        (200, 90, 0),
        "circle",
        right=["Industriestraße 18", "72762 Reutlingen", "Tel. 07121 9340-0"],
    )
    stamp(d, pg, ["VERTRAULICH"], 155, 52, pt=19, fill=STAMP_RED, angle=-8, border="box", gov=False)
    y = address_window(
        pg,
        y + 6,
        "Brandt & Söhne · Industriestraße 18 · 72762 Reutlingen",
        ["Kessler Automotive SE", "Einkauf – Frau Dr. Yilmaz", "Hafenstraße 3", "70327 Stuttgart"],
        family="helv",
    )
    pg.text(190, y - 6, "Reutlingen, 16. September 2026", 10.5, "helv", "r", INK, "r")
    y += 12
    pg.text(20, y, "Preisgestaltung Rahmenvertrag 2027", 11, "helv", "b")
    body_text(
        pg,
        y + 9,
        "Sehr geehrte Frau Dr. Yilmaz,",
        [
            "wie besprochen erhalten Sie unsere Kalkulation für die Verlängerung des "
            "Rahmenvertrags. "
            "Die Preise berücksichtigen die gestiegenen Energie- und Materialkosten.",
            d.pick(DE_CORP, 3),
            "Für Rückfragen stehe ich Ihnen gerne zur Verfügung.",
        ],
        ["Mit freundlichen Grüßen", "", "", "Katharina Brandt", "Geschäftsführerin"],
        family="helv",
    )


@document(
    "memo",
    "company-footer",
    scheme="company",
    company="INTERN",
    dpi=150,
    tags=("footer", "small", "mixed-case"),
)
def p_company_intern(d: Doc) -> None:
    pg = d.page()
    footer(
        d,
        pg,
        "Klassifizierung: INTERN",
        align="l",
        pt=8,
        family="arial",
        style="r",
        y=286,
        gov=False,
    )
    pg.text(190, 286, "Seite 1/1", 8, "arial", "r", GREY, "r")
    letterhead_logo(pg, "Rheintal Logistik GmbH", "Hausmitteilung", (0, 120, 90), "bars")
    pg.table(
        20,
        44,
        [35, 135],
        [
            ["An", "alle Beschäftigten am Standort Mannheim"],
            ["Von", "Geschäftsleitung / Personal"],
            ["Datum", "7. September 2026"],
            ["Betreff", "Umstellung der Schichtpläne ab 1. November"],
        ],
        pt=10,
        header=False,
        bold_cols=(0,),
        grid=False,
    )
    pg.paras(
        20,
        78,
        170,
        [
            "Liebe Kolleginnen und Kollegen,",
            "ab dem 1. November stellen wir im Lager Mannheim auf ein Drei-Schicht-Modell um. Die "
            "neuen Schichtpläne hängen ab nächster Woche am schwarzen Brett aus.",
            d.pick(DE_CORP, 2),
            "Fragen beantworten Ihre Teamleitungen und die Personalabteilung.",
            "Ihre Geschäftsleitung",
        ],
        10.5,
        justify=True,
    )


@document(
    "board",
    "company-letter",
    scheme="company",
    company="CONFIDENTIAL",
    dpi=200,
    degradation="skew",
    tags=("header", "footer", "english"),
)
def p_company_confidential(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "CONFIDENTIAL", "c", 11, "arial", "b", y=8, gov=False)
    footer(d, pg, "CONFIDENTIAL", pt=9, family="arial", style="b", y=286, gov=False)
    letterhead_logo(
        pg,
        "Northwind Analytics Ltd",
        "Board of Directors",
        (90, 40, 130),
        "square",
        right=["12 Queen Street", "Manchester M2 5HS"],
        top=15,
    )
    pg.text(20, 45, "Board Paper 2026/17", 10, "arial", "r", GREY)
    pg.text(20, 51, "Proposed acquisition of DataHarbour GmbH", 15, "arial", "b")
    y = 62.0
    for heading, text in (
        (
            "Purpose",
            "To seek the Board's approval to submit a binding offer "
            "for DataHarbour GmbH, a data engineering company based in Hamburg.",
        ),
        ("Background", d.pick(EN_GOV, 3)),
        (
            "Financial impact",
            "The proposed purchase price is EUR 18.5m on a "
            "cash-free, debt-free basis, funded from existing facilities.",
        ),
        ("Risks", d.pick(EN_GOV, 2)),
        (
            "Recommendation",
            "The Board is asked to approve the submission of a "
            "binding offer on the terms set out above.",
        ),
    ):
        pg.text(20, y, heading, 11, "arial", "b")
        y = pg.para(20, y + 6, 170, text, 10.5, justify=True) + 4


@document(
    "slides",
    "company-slides",
    scheme="company",
    company="INTERNAL",
    dpi=200,
    tags=("slides", "footer", "small", "english"),
)
def p_company_internal_slides(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 12, "Q3 Sales Kick-off – Handout", 9, "arial", "r", GREY)
    footer(
        d,
        pg,
        "Classification: INTERNAL",
        align="c",
        pt=9,
        family="arial",
        style="b",
        y=284,
        gov=False,
    )
    for k, (title, bullets) in enumerate(
        (
            (
                "Q3 pipeline",
                [
                    "Pipeline up 14 % on Q2",
                    "Two large renewals in October",
                    "Focus: public utilities",
                ],
            ),
            (
                "Priorities",
                [
                    "Win back churned accounts",
                    "Bundle support with licences",
                    "New partner programme from January",
                ],
            ),
        )
    ):
        y = 22 + k * 125
        slide_frame(pg, 25, y, 160, title, bullets, (200, 70, 20), "arial", foot="INTERNAL")
    d.mark(pg, "INTERNAL", gov=False)


@document(
    "offer",
    "company-stamp",
    scheme="company",
    company="GESCHÄFTSGEHEIMNIS",
    dpi=200,
    tags=("stamp", "blue"),
)
def p_company_geschaeftsgeheimnis(d: Doc) -> None:
    pg = d.page()
    letterhead_logo(
        pg,
        "Kranz Umwelttechnik GmbH",
        "Wasser · Abwasser · Energie",
        (0, 110, 170),
        "leaf",
        right=["Gewerbepark 5", "99084 Erfurt"],
    )
    stamp(
        d,
        pg,
        ["GESCHÄFTSGEHEIMNIS"],
        140,
        44,
        pt=14,
        fill=STAMP_BLUE,
        angle=0,
        border="box",
        gov=False,
    )
    pg.text(20, 58, "Angebot Nr. A-26-1187", 14, "arial", "b")
    pg.text(20, 66, "Ausschreibung Kläranlage Ostheim – Los 2, Maschinentechnik", 10.5)
    y = pg.table(
        20,
        76,
        [15, 95, 20, 40],
        [
            ["Pos.", "Leistung", "Menge", "Preis €"],
            ["1", "Belüftungssystem inkl. Gebläsestation", "1", "412.500,00"],
            ["2", "Rücklaufschlammpumpen, frequenzgeregelt", "4", "96.800,00"],
            ["3", "Mess- und Regeltechnik", "1", "133.200,00"],
            ["4", "Montage und Inbetriebnahme", "1", "88.400,00"],
            ["", "Summe netto", "", "730.900,00"],
        ],
        pt=9.5,
    )
    pg.paras(
        20,
        y + 8,
        170,
        [
            "Die Kalkulationsgrundlagen (Anlage 3) enthalten unsere Einkaufspreise und Zuschläge. "
            "Wir bitten, diese Anlage von der Akteneinsicht Dritter auszunehmen.",
            d.pick(DE_CORP, 2),
        ],
        10.5,
    )


# ================================================================ positives: mixed


@document(
    "report",
    "de-letter",
    scheme="de",
    level=1,
    label="VS-NfD",
    tlp="AMBER",
    dpi=200,
    tags=("header", "tlp-box", "mixed-schemes"),
    note="a government grade and a TLP label on the same report",
)
def p_nfd_with_tlp(d: Doc) -> None:
    pg = d.page()
    header(d, pg, "VS – NUR FÜR DEN DIENSTGEBRAUCH", "c", 10, "arial", "b", y=8)
    tlp_badge(d, pg, "TLP:AMBER", 190, 14, TLP_COLOURS["AMBER"], 10)
    footer(d, pg, "VS – NUR FÜR DEN DIENSTGEBRAUCH", pt=10, family="arial", style="b", y=284)
    y = letterhead_federal(
        pg,
        ["Landeszentrale für Cybersicherheit"],
        ["Lagezentrum", "Taubenstraße 10", "10117 Berlin"],
        top=22,
    )
    pg.text(20, y + 4, "Lagebericht IT-Sicherheit – KW 37/2026", 15, "arial", "b")
    y = pg.paras(20, y + 16, 170, [d.pick(DE_CYBER, 3), d.pick(DE_CYBER, 3)], 10.5, justify=True)
    y = pg.table(
        20,
        y + 2,
        [40, 90, 40],
        [
            ["Datum", "Vorfall", "Bewertung"],
            ["08.09.", "Ausfall der Terminvergabe eines Bürgeramts", "mittel"],
            ["10.09.", "Phishing gegen Beschäftigte einer Hochschule", "gering"],
            ["11.09.", "Verschlüsselungstrojaner bei einem Klinikverbund", "hoch"],
        ],
        pt=9.5,
    )
    pg.paras(20, y + 6, 170, [d.pick(DE_IT, 2)], 10.5)


# ================================================================ negatives: talking about grades


GRADE_DEFINITIONS = [
    (
        "STRENG GEHEIM",
        "wenn die Kenntnisnahme durch Unbefugte den Bestand oder lebenswichtige "
        "Interessen des Bundes oder eines Landes gefährden kann,",
    ),
    (
        "GEHEIM",
        "wenn die Kenntnisnahme durch Unbefugte die Sicherheit des Bundes oder eines "
        "Landes gefährden oder ihren Interessen schweren Schaden zufügen kann,",
    ),
    (
        "VS-VERTRAULICH",
        "wenn die Kenntnisnahme durch Unbefugte für die Interessen des Bundes "
        "oder eines Landes schädlich sein kann,",
    ),
    (
        "VS-NUR FÜR DEN DIENSTGEBRAUCH",
        "wenn die Kenntnisnahme durch Unbefugte für die "
        "Interessen des Bundes oder eines Landes nachteilig sein kann.",
    ),
]


@document(
    "handout",
    "neg-policy",
    fmt="pdf",
    dpi=200,
    mentions=True,
    tags=("definition-list", "wrapped-grade", "headings"),
)
def n_policy_merkblatt(d: Doc) -> None:
    for n in (1, 2):
        pg = d.page()
        pg.text(
            20,
            12,
            "Bundesamt für Materialwirtschaft · Geheimschutzbeauftragter",
            8,
            "arial",
            "r",
            GREY,
        )
        page_numbers(
            pg, n, 2, fmt="Merkblatt GSB-04 · Stand 06/2026 · Seite {n} von {total}", y=287
        )
        if n == 1:
            pg.text(20, 22, "Merkblatt: Umgang mit Verschlusssachen", 17, "arial", "b")
            pg.text(
                20,
                31,
                "für Beschäftigte, die gelegentlich mit Verschlusssachen arbeiten",
                10.5,
                "arial",
                "i",
                GREY,
            )
            pg.text(20, 44, "1. Was ist eine Verschlusssache?", 12, "arial", "b")
            y = pg.para(
                20,
                51,
                170,
                "Verschlusssachen sind im öffentlichen Interesse "
                "geheimhaltungsbedürftige Tatsachen, Gegenstände oder Erkenntnisse, "
                "gleich in welcher Form sie vorliegen. Je nach Schutzbedarf werden sie "
                "in einen von vier Geheimhaltungsgraden eingestuft. Eingestuft wird "
                "durch die herausgebende Stelle.",
                10.5,
                justify=True,
            )
            pg.text(20, y + 5, "2. Die Geheimhaltungsgrade", 12, "arial", "b")
            y += 13
            for grade, text in GRADE_DEFINITIONS:
                lines = pg.wrap(grade, 50, 10.5, "arial", "b")
                gy = y
                for line in lines:
                    pg.text(25, gy, line, 10.5, "arial", "b")
                    gy += 5
                y = max(pg.para(80, y, 110, text, 10.5), gy) + 3
            pg.text(20, y + 3, "3. Kennzeichnung", 12, "arial", "b")
            pg.paras(
                20,
                y + 10,
                170,
                [
                    "Der Geheimhaltungsgrad steht auf jeder Seite oben und unten in der Mitte. "
                    "Schriftstücke des Grades VS-NUR FÜR DEN\nDIENSTGEBRAUCH dürfen die Abkürzung "
                    "VS-NfD tragen; bei E-Mails steht die Abkürzung am Anfang des Betreffs.",
                    "Ab dem Grad VS-VERTRAULICH wird jedes Stück in einem VS-Bestandsverzeichnis "
                    "nachgewiesen. Kopien und Auszüge sind wie das Original zu kennzeichnen.",
                ],
                10.5,
                justify=True,
            )
        else:
            pg.text(20, 22, "4. Aufbewahrung", 12, "arial", "b")
            y = pg.bullets(
                20,
                29,
                170,
                [
                    "VS-NfD: in einem verschlossenen Schrank oder Schreibtisch; beim Verlassen des "
                    "Raumes nicht offen liegen lassen.",
                    "VS-VERTRAULICH und höher: nur in zugelassenen Verwahrgelassen "
                    "(Stahlschränken) mit Zahlenschloss.",
                    "Schlüssel und Zahlenkombinationen sind wie die Verschlusssache selbst zu "
                    "schützen.",
                ],
                10.5,
            )
            pg.text(20, y + 4, "5. Weitergabe", 12, "arial", "b")
            y = pg.bullets(
                20,
                y + 11,
                170,
                [
                    "Weitergabe nur an Personen, die die Kenntnis für ihre Aufgabe benötigen.",
                    "Elektronisch nur über zugelassene Verschlüsselungsprodukte; unverschlüsselt "
                    "per E-Mail ist nichts über VS-NfD zu versenden.",
                    "GEHEIM und STRENG GEHEIM eingestufte Unterlagen werden nur durch die "
                    "VS-Registratur versandt.",
                ],
                10.5,
            )
            pg.text(20, y + 4, "6. Ansprechpartner", 12, "arial", "b")
            pg.para(
                20,
                y + 11,
                170,
                "Geheimschutzbeauftragter, Gebäude 1, Raum 104, "
                "App. 1234. Verluste oder Verdachtsfälle sind sofort zu melden.",
                10.5,
            )


@document(
    "slides", "neg-training", fmt="pdf", dpi=200, mentions=True, tags=("slides", "grade-bullets")
)
def n_training_slides(d: Doc) -> None:
    slides = [
        (
            "Schulung Geheimschutz – Modul 2",
            ["Geheimhaltungsgrade und Kennzeichnung", "Geheimschutzbeauftragter, 22.09.2026"],
        ),
        (
            "Die vier Geheimhaltungsgrade",
            ["STRENG GEHEIM", "GEHEIM", "VS-VERTRAULICH", "VS-NUR FÜR DEN DIENSTGEBRAUCH"],
        ),
        (
            "Wo steht der Grad?",
            [
                "oben und unten mittig auf jeder Seite",
                "in E-Mails am Anfang des Betreffs: VS-NfD – …",
                "auf Datenträgern als Aufkleber",
            ],
        ),
        (
            "NATO und EU",
            [
                "NATO RESTRICTED entspricht VS-NfD",
                "RESTREINT UE/EU RESTRICTED entspricht VS-NfD",
                "COSMIC TOP SECRET entspricht STRENG GEHEIM",
            ],
        ),
        (
            "Typische Fehler",
            [
                "Kopien ohne Kennzeichnung",
                "Weiterleitung an private Adressen",
                "Ausdrucke im Etagendrucker vergessen",
            ],
        ),
        ("Fragen?", ["Geheimschutzbeauftragter, App. 1234", "Merkblatt GSB-04 im Portal"]),
    ]
    for n in (1, 2):
        pg = d.page()
        pg.text(20, 10, "Schulung Geheimschutz · Modul 2", 8, "arial", "r", GREY)
        pg.text(190, 10, "22.09.2026", 8, "arial", "r", GREY, "r")
        for k in range(3):
            title, bullets = slides[(n - 1) * 3 + k]
            y = 20 + k * 88
            slide_frame(
                pg, 20, y, 95, title, bullets, (120, 20, 40), "arial", foot="Geheimschutz · Modul 2"
            )
            for j in range(8):
                pg.hline(125, 190, y + 8 + j * 7, 0.15, GREY)
        page_numbers(pg, n, 2, y=289)


@document("datasheet", "neg-product", dpi=200, mentions=True, tags=("badge", "feature-box"))
def n_product_sheet(d: Doc) -> None:
    pg = d.page()
    pg.rect(0, 0, 210, 42, outline=None, fill=(20, 45, 80))
    pg.text(20, 12, "SecuLink S3", 26, "arial", "b", WHITE)
    pg.text(20, 25, "VPN-Gateway für Behördennetze", 13, "arial", "r", (200, 215, 235))
    pg.rect(140, 12, 50, 20, outline=None, fill=(0, 150, 110), radius=3)
    pg.text(165, 15, "BSI-Zulassung", 10, "arial", "b", WHITE, "c")
    pg.text(165, 21.5, "zugelassen bis VS-NfD", 9, "arial", "r", WHITE, "c")
    y = pg.paras(
        20,
        54,
        170,
        [
            "SecuLink S3 verbindet Liegenschaften und mobile Arbeitsplätze über öffentliche Netze. "
            "Das Gerät ist vom Bundesamt für Sicherheit in der Informationstechnik für die "
            "Übertragung von Verschlusssachen zugelassen und wird in über 400 Dienststellen "
            "eingesetzt.",
            "Die Verwaltung erfolgt zentral über den SecuLink Manager; Schlüssel werden auf einer "
            "Smartcard erzeugt und verlassen das Gerät nicht.",
        ],
        10.5,
        justify=True,
    )
    pg.rect(20, y + 2, 170, 30, outline=None, fill=(232, 244, 238))
    pg.text(25, y + 5, "Zulassung", 12, "arial", "b", (0, 110, 80))
    pg.para(
        25,
        y + 12,
        160,
        "Zugelassen für VS-NUR FÜR DEN DIENSTGEBRAUCH sowie für NATO "
        "RESTRICTED und RESTREINT UE/EU RESTRICTED. Einsatz für höhere Grade auf Anfrage "
        "im Verbund mit SecuLink S5.",
        10.5,
    )
    y = pg.table(
        20,
        y + 40,
        [60, 110],
        [
            ["Technische Daten", ""],
            ["Durchsatz", "bis 2,5 Gbit/s (IPsec, AES-256-GCM)"],
            ["Schnittstellen", "4 × 1 GbE, 2 × 10 GbE SFP+"],
            ["Stromversorgung", "redundant, 100–240 V AC"],
            ["Bauform", "19 Zoll, 1 HE"],
            ["Betriebstemperatur", "0 bis 45 °C"],
        ],
        pt=10,
    )
    pg.text(20, y + 10, "Bestellnummer SL-S3-2026 · Preise auf Anfrage", 10, "arial", "b")
    pg.text(
        20,
        280,
        "Nordfunk Secure Systems GmbH · Holtenauer Str. 90 · 24105 Kiel",
        8,
        "arial",
        "r",
        GREY,
    )


@document("jobad", "neg-jobad", dpi=200, mentions=True, tags=("two-column", "banner"))
def n_job_ad(d: Doc) -> None:
    pg = d.page()
    pg.rect(0, 0, 210, 50, outline=None, fill=(230, 110, 20))
    pg.text(20, 12, "Wir suchen Sie!", 12, "arial", "b", WHITE)
    pg.text(20, 20, "IT-Sicherheitsadministrator (m/w/d)", 20, "arial", "b", WHITE)
    pg.text(
        20,
        32,
        "Bundesamt für Materialwirtschaft, Koblenz · Entgeltgruppe 11 TVöD · unbefristet",
        10,
        "arial",
        "r",
        WHITE,
    )
    blocks = [
        (
            "Ihre Aufgaben",
            [
                "Betrieb und Härtung der Server- und Netzkomponenten",
                "Auswertung von Sicherheitsereignissen",
                "Mitarbeit im Notfallmanagement",
                "Betreuung der Kryptogeräte in den Außenstellen",
            ],
        ),
        (
            "Ihr Profil",
            [
                "abgeschlossenes Studium der Informatik oder vergleichbare Qualifikation",
                "Bereitschaft zur Sicherheitsüberprüfung (Ü2); eine Ermächtigung bis "
                "GEHEIM ist erforderlich oder wird beantragt",
                "Erfahrung im Umgang mit Verschlusssachen bis VS-VERTRAULICH wünschenswert",
                "sehr gute Deutschkenntnisse",
            ],
        ),
        (
            "Wir bieten",
            [
                "flexible Arbeitszeiten und mobiles Arbeiten",
                "Fortbildung, z. B. zum IT-Grundschutz-Praktiker",
                "Jobticket und betriebliche Altersvorsorge",
            ],
        ),
    ]
    for (title, items), (x, y) in zip(blocks, ((20.0, 60.0), (110.0, 60.0), (20.0, 125.0))):
        pg.text(x, y, title, 13, "arial", "b", (230, 110, 20))
        pg.bullets(x, y + 8, 80, items, 10)
    pg.rect(20, 235, 170, 30, outline=(230, 110, 20), width=0.6)
    pg.para(
        25,
        239,
        160,
        "Bewerbungen bitte bis 15. Oktober 2026 über das Karriereportal unter "
        "der Kennziffer BAM-2026-147. Schwerbehinderte Menschen werden bei gleicher "
        "Eignung bevorzugt berücksichtigt.",
        10,
    )


@document(
    "news",
    "neg-news",
    dpi=200,
    degradation="lowdpi",
    mentions=True,
    tags=("two-column", "headline"),
)
def n_news(d: Doc) -> None:
    pg = d.page()
    pg.text(105, 10, "Weserblick", 30, "garamond", "b", INK, "c")
    pg.hline(20, 190, 23, 0.8)
    pg.text(
        20,
        24.5,
        "Tageszeitung für die Region · Samstag, 12. September 2026 · 2,40 €",
        8.5,
        "garamond",
    )
    pg.hline(20, 190, 29, 0.3)
    pg.text(20, 34, "STADTGESCHICHTE", 9, "arial", "b", RED)
    pg.para(
        20, 39, 170, "Der streng geheime Bunker unter dem Rathaus", 22, "garamond", "b", lead=1.15
    )
    pg.rect(20, 52, 80, 50, outline=None, fill=(190, 190, 190))
    pg.text(
        20,
        103,
        "Der Zugang zum Bunker liegt hinter einer Stahltür im Keller.",
        8,
        "garamond",
        "i",
        GREY,
    )
    left = [
        "Jahrzehntelang wusste kaum jemand in der Stadt, was sich unter dem Rathaus befindet. "
        "Die Baupläne galten als geheim, die wenigen Eingeweihten schwiegen. Erst jetzt hat das "
        "Stadtarchiv die Unterlagen freigegeben.",
        "Auf vielen Akten prangt noch der rote Stempel „STRENG GEHEIM“. Andere tragen den "
        "Vermerk „VS – Nur für den Dienstgebrauch“. Archivleiterin Sabine Ortmann lacht: „Heute "
        "kann jeder Schüler die Pläne einsehen.“",
    ]
    right = [
        "Der Bunker wurde 1962 für die Stadtverwaltung gebaut und bot Platz für 80 Personen. "
        "Er verfügt über eine eigene Stromversorgung, einen Brunnen und eine Funkstation.",
        "Im Oktober bietet das Archiv Führungen an. Die Plätze sind begrenzt; Anmeldungen "
        "nimmt die Touristinformation am Markt entgegen.",
    ]
    pg.paras(20, 109, 80, left, 9.5, "garamond", gap=1.5, justify=True)
    pg.paras(
        110,
        52,
        80,
        right
        + [
            "„Wir wollten zeigen, wie ernst man die Gefahr damals nahm“, sagt Ortmann. Die als "
            "geheim eingestuften Pläne sind ab November auch digital abrufbar.",
        ],
        9.5,
        "garamond",
        gap=1.5,
        justify=True,
    )
    pg.hline(20, 190, 170, 0.3)
    pg.text(20, 174, "Stadtrat beschließt neue Radwege", 16, "garamond", "b")
    pg.paras(
        20,
        184,
        80,
        [
            "Mit großer Mehrheit hat der Stadtrat am Donnerstag das Radwegekonzept beschlossen. "
            "Bis 2029 sollen 14 Kilometer neue Wege entstehen, vor allem entlang der Weser.",
        ],
        9.5,
        "garamond",
        justify=True,
    )
    pg.paras(
        110,
        184,
        80,
        [
            "Die Kosten von rund 6 Millionen Euro trägt zu zwei Dritteln das Land. Die Opposition "
            "kritisierte, dass die Innenstadt erst im letzten Bauabschnitt an der Reihe ist.",
        ],
        9.5,
        "garamond",
        justify=True,
    )


@document("table", "neg-table", dpi=240, mentions=True, tags=("grade-table",))
def n_grade_table(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 14, "Anlage 5 zur Schulungsunterlage Geheimschutz", 9, "arial", "r", GREY)
    pg.text(20, 24, "Vergleichbare Geheimhaltungsgrade", 16, "arial", "b")
    y = pg.para(
        20,
        34,
        170,
        "Die folgende Übersicht zeigt, welche Grade einander bei der "
        "Weitergabe zwischen nationalen und internationalen Stellen entsprechen. Sie "
        "dient nur der Orientierung; maßgeblich sind die jeweiligen "
        "Geheimschutzabkommen.",
        10.5,
        justify=True,
    )
    y = pg.table(
        20,
        y + 5,
        [38, 34, 44, 30, 24],
        [
            ["Deutschland", "NATO", "EU", "Österreich", "USA"],
            [
                "STRENG GEHEIM",
                "COSMIC TOP SECRET",
                "TRÈS SECRET UE/EU TOP SECRET",
                "STRENG GEHEIM",
                "TOP SECRET",
            ],
            ["GEHEIM", "NATO SECRET", "SECRET UE/EU SECRET", "GEHEIM", "SECRET"],
            [
                "VS-VERTRAULICH",
                "NATO CONFIDENTIAL",
                "CONFIDENTIEL UE/EU CONFIDENTIAL",
                "VERTRAULICH",
                "CONFIDENTIAL",
            ],
            [
                "VS-NUR FÜR DEN DIENSTGEBRAUCH",
                "NATO RESTRICTED",
                "RESTREINT UE/EU RESTRICTED",
                "EINGESCHRÄNKT",
                "–",
            ],
        ],
        pt=9,
        lead=1.3,
        bold_cols=(0,),
    )
    pg.paras(
        20,
        y + 6,
        170,
        [
            "Hinweis: Die Schweiz kennt die Stufen INTERN, VERTRAULICH und GEHEIM. Controlled "
            "Unclassified Information (CUI) ist in den USA keine Einstufung im engeren Sinn.",
            "Fragen zur Weitergabe an ausländische Stellen richten Sie an den "
            "Geheimschutzbeauftragten.",
        ],
        9.5,
        fill=GREY,
    )


@document("handout", "neg-tlp", dpi=200, mentions=True, tags=("tlp-badges",))
def n_tlp_explainer(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 18, "Traffic Light Protocol (TLP) – Kurzübersicht", 17, "arial", "b")
    y = (
        pg.para(
            20,
            29,
            170,
            "Das Traffic Light Protocol legt fest, an wen eine Information "
            "weitergegeben werden darf. Die Kennzeichnung erfolgt durch die Quelle; "
            "Empfänger halten sich an die Vorgabe.",
            10.5,
            justify=True,
        )
        + 4
    )
    rows = [
        (
            "TLP:RED",
            "RED",
            "Nur für die anwesenden oder direkt adressierten Personen. Keine Weitergabe.",
        ),
        ("TLP:AMBER+STRICT", "AMBER", "Weitergabe nur innerhalb der eigenen Organisation."),
        (
            "TLP:AMBER",
            "AMBER",
            "Weitergabe innerhalb der eigenen Organisation und an deren "
            "Kunden, soweit zum Schutz erforderlich.",
        ),
        ("TLP:GREEN", "GREEN", "Weitergabe innerhalb der Community, aber nicht öffentlich."),
        ("TLP:CLEAR", "CLEAR", "Keine Einschränkung; Urheberrecht bleibt unberührt."),
    ]
    for label, colour, text in rows:
        w = pg.width(label, 11, "arial", "b")
        pg.rect(20, y - 1, w + 4, 6.8, outline=None, fill=(0, 0, 0))
        pg.text(22, y, label, 11, "arial", "b", TLP_COLOURS[colour])
        y = max(pg.para(70, y, 120, text, 10.5), y + 7) + 5
    pg.text(20, y + 4, "Wie wird gekennzeichnet?", 12, "arial", "b")
    pg.paras(
        20,
        y + 11,
        170,
        [
            "Die Bezeichnung steht oben rechts auf jeder Seite und bei E-Mails am Anfang des "
            "Betreffs, zum Beispiel „[TLP:AMBER] Lagebericht“.",
            "Fehlt eine Kennzeichnung, ist vor einer Weitergabe beim Absender nachzufragen.",
        ],
        10.5,
    )
    page_numbers(pg, 1, 1, fmt="CERT-Nord · Merkblatt 7 · Seite {n}", y=287)


@document(
    "email",
    "neg-disclaimer",
    dpi=200,
    mentions=True,
    tags=("disclaimer",),
    note="confidentiality disclaimers at the foot of an ordinary e-mail",
)
def n_email_disclaimer(d: Doc) -> None:
    pg = d.page()
    y = outlook(
        pg,
        "Hoffmann, Stefan",
        [
            ("Von:", "Facility Management <facility@hansewerk-pumpen.example>"),
            ("Gesendet:", "Dienstag, 15. September 2026 11:30"),
            ("An:", "Alle Beschäftigten Standort Fulda"),
            ("Betreff:", "Umbau der Kantine – Essensausgabe ab Montag im Foyer"),
        ],
    )
    y = pg.paras(
        20,
        y,
        170,
        [
            "Liebe Kolleginnen und Kollegen,",
            "ab Montag, 21. September, wird die Kantine für sechs Wochen umgebaut. Die "
            "Essensausgabe findet in dieser Zeit im Foyer von Gebäude A statt. Es gibt täglich "
            "zwei "
            "Gerichte und eine Salatbar.",
            "Die Kaffeebar bleibt geöffnet. Für die Lärmbelästigung während der Abbrucharbeiten in "
            "der ersten Woche bitten wir um Verständnis.",
            "Viele Grüße",
            "Ihr Facility Management",
        ],
        10.5,
        gap=2.5,
    )
    y += 8
    pg.hline(20, 80, y, 0.2, GREY)
    pg.paras(
        20,
        y + 4,
        170,
        [
            "Hansewerk Pumpen AG · Am Pumpwerk 1 · 36037 Fulda · Sitz der Gesellschaft: Fulda · "
            "Amtsgericht Fulda HRB 1234 · Vorstand: Dr. J. Brenner (Vors.), M. Koch",
            "Diese E-Mail ist vertraulich und kann rechtlich geschützte Informationen enthalten. "
            "Wenn Sie nicht der richtige Adressat sind oder diese E-Mail irrtümlich erhalten "
            "haben, "
            "informieren Sie bitte sofort den Absender und vernichten Sie diese E-Mail.",
            "This message is confidential and may contain privileged information. If you are not "
            "the intended recipient, please notify the sender and delete this message.",
        ],
        8,
        gap=2,
        fill=GREY,
    )


@document(
    "letter",
    "neg-words",
    dpi=200,
    degradation="blur",
    mentions=True,
    tags=("address-block", "persoenlich"),
    note="'Persönlich' above the address is a postal note, not a marking; the text asks "
    "to keep the PIN confidential",
)
def n_bank_letter(d: Doc) -> None:
    pg = d.page()
    y = letterhead_logo(
        pg,
        "Nordbank Direkt",
        "Ihre Bank im Norden",
        (200, 20, 40),
        "square",
        right=["Kundenservice", "Tel. 0431 777 00", "www.nordbank.example"],
    )
    pg.text(20, y + 5, "Nordbank Direkt · Postfach 11 22 · 24001 Kiel", 6.5, "arial", "r", GREY)
    pg.text(20, y + 10, "Persönlich", 10.5, "arial", "b")
    yy = y + 15
    for line in ("Herrn", "Lukas Petersen", "Lindenweg 7", "24119 Kronshagen"):
        pg.text(20, yy, line, 10.5)
        yy += 5
    pg.text(190, y + 30, "Kiel, 10.09.2026", 10.5, "arial", "r", INK, "r")
    y = yy + 12
    pg.text(20, y, "Ihre neue Geheimzahl (PIN) für die Girocard", 11, "arial", "b")
    body_text(
        pg,
        y + 9,
        "Sehr geehrter Herr Petersen,",
        [
            "Sie erhalten heute Ihre neue Geheimzahl für die Girocard mit der Endnummer 4471. Die "
            "Karte selbst senden wir Ihnen aus Sicherheitsgründen in einem separaten Brief.",
            "Bitte bewahren Sie die PIN niemals zusammen mit der Karte auf und behandeln Sie sie "
            "vertraulich. Unsere Mitarbeiterinnen und Mitarbeiter werden Sie nie nach Ihrer PIN "
            "fragen.",
            "Sollte der Brief beschädigt bei Ihnen ankommen, lassen Sie Ihre Karte bitte sofort "
            "unter 116 116 sperren.",
        ],
        ["Mit freundlichen Grüßen", "", "Ihre Nordbank Direkt"],
    )
    pg.rect(120, 230, 60, 22, outline=GREY, width=0.3)
    pg.text(150, 233, "Hier freirubbeln", 8, "arial", "r", GREY, "c")
    pg.rect(125, 239, 50, 9, outline=None, fill=(170, 170, 175))


@document(
    "newsletter",
    "neg-words",
    dpi=200,
    tags=("ordinary-words", "at"),
    note="VS = Volksschule; Internat, Internet, Geheimtipp are ordinary words",
)
def n_school_newsletter(d: Doc) -> None:
    pg = d.page()
    for i, colour in enumerate(((230, 60, 60), (250, 180, 0), (40, 160, 80), (40, 110, 200))):
        pg.rect(20 + i * 6, 14, 4.5, 18, outline=None, fill=colour, radius=1)
    pg.text(48, 14, "VS Wien-Donaustadt", 16, "round", "b", (40, 110, 200))
    pg.text(
        48, 22, "Öffentliche Volksschule · Konstanziagasse 24 · 1220 Wien", 9, "round", "r", GREY
    )
    pg.text(20, 44, "Elternbrief Nr. 1 – Schuljahr 2026/27", 15, "round", "b")
    pg.paras(
        20,
        56,
        170,
        [
            "Liebe Eltern,",
            "herzlich willkommen im neuen Schuljahr! Die VS Wien-Donaustadt freut sich über 52 "
            "neue "
            "Taferlklassler. Die Klassenlehrerinnen stellen sich beim Elternabend am 24. September "
            "vor.",
            "Alle Termine und den Speiseplan finden Sie ab sofort im Internet auf unserer "
            "Homepage. "
            "Die Anmeldung für die Nachmittagsbetreuung erfolgt über das Elternportal.",
            "Für die Schülerinnen und Schüler der 4. Klassen, die im Herbst ein Internat besuchen "
            "möchten, bieten wir am 8. Oktober eine Informationsveranstaltung an.",
            "Unser Geheimtipp für den Herbst: der Lehrpfad in der Lobau. Die Wandertage der "
            "Klassen führen alle dorthin – bitte an feste Schuhe denken!",
            "Mit herzlichen Grüßen",
            "Dipl.-Päd. Martina Gruber, Schulleiterin",
        ],
        10.5,
        "round",
        gap=3,
    )
    pg.rect(20, 240, 170, 26, outline=(40, 110, 200), width=0.5, radius=2)
    pg.text(25, 244, "Rückmeldung (bitte bis 18. September abgeben)", 9.5, "round", "b")
    pg.checkbox(25, 252, 3.4)
    pg.text(30, 251.8, "Wir kommen zum Elternabend.", 9.5, "round")
    pg.checkbox(105, 252, 3.4)
    pg.text(110, 251.8, "Wir können leider nicht kommen.", 9.5, "round")


@document(
    "press",
    "neg-words",
    dpi=200,
    tags=("ordinary-words", "vs-bericht"),
    note="VS-Bericht = Verfassungsschutzbericht, not a marking",
)
def n_vs_bericht(d: Doc) -> None:
    pg = d.page()
    letterhead_logo(pg, "Ministerium für Inneres Weserland", "Pressestelle", (0, 70, 140), "leaf")
    pg.text(20, 42, "PRESSEMITTEILUNG", 12, "arial", "b", (0, 70, 140))
    pg.text(190, 42, "Nr. 114/2026 · 16.09.2026", 9.5, "arial", "r", GREY, "r")
    pg.para(
        20,
        52,
        170,
        "VS-Bericht 2025 vorgestellt: Zahl der Cyberangriffe auf Behörden deutlich gestiegen",
        15,
        "arial",
        "b",
        lead=1.2,
    )
    pg.paras(
        20,
        72,
        170,
        [
            "Innenministerin Dr. Claudia Behrens hat heute gemeinsam mit dem Präsidenten des "
            "Landesamts für Verfassungsschutz den VS-Bericht 2025 vorgestellt. Der Bericht gibt "
            "einen Überblick über extremistische Bestrebungen, Spionage und Sabotage im Land.",
            "„Die Zahl der Angriffe auf die IT der Landesverwaltung hat sich gegenüber dem Vorjahr "
            "fast verdoppelt“, sagte Behrens. Besonders betroffen seien Kommunen und Hochschulen.",
            "Der Verfassungsschutz hat im vergangenen Jahr 38 Informationsveranstaltungen für "
            "Unternehmen durchgeführt. Das Angebot soll 2026 ausgebaut werden.",
            "Der vollständige VS-Bericht 2025 steht auf der Website des Ministeriums zum Download "
            "bereit. Gedruckte Exemplare können kostenlos bestellt werden.",
        ],
        10.5,
        justify=True,
    )
    pg.text(
        20,
        262,
        "Pressestelle · Tel. 0421 361-0 · presse@mi.weserland.example",
        8.5,
        "arial",
        "r",
        GREY,
    )


@document(
    "letter",
    "neg-stamps",
    dpi=200,
    degradation="skew",
    tags=("stamp-eingang", "stamp-erledigt", "stamp-kopie"),
)
def n_stamps(d: Doc) -> None:
    pg = d.page()
    y = letterhead_logo(
        pg,
        "Stadtwerke Neuweiler",
        "Energie · Wasser · Bäder",
        (0, 120, 180),
        "circle",
        right=["Bahnhofstraße 12", "35510 Neuweiler"],
    )
    y = address_window(
        pg,
        y + 6,
        "Stadtwerke Neuweiler · Bahnhofstraße 12 · 35510 Neuweiler",
        ["Landratsamt Mittelaue", "Untere Wasserbehörde", "Postfach 1120", "04701 Mittelaue"],
    )
    y += 12
    pg.text(
        20,
        y,
        "Antrag auf Verlängerung der wasserrechtlichen Erlaubnis Brunnen III",
        11,
        "arial",
        "b",
    )
    body_text(
        pg,
        y + 9,
        "Sehr geehrte Damen und Herren,",
        [
            "hiermit beantragen wir die Verlängerung der Erlaubnis zur Entnahme von Grundwasser "
            "aus "
            "dem Brunnen III um weitere 20 Jahre. Die Fördermenge soll unverändert bleiben.",
            d.pick(DE_ADMIN, 3),
            "Die hydrogeologische Stellungnahme und die Messreihen der letzten zehn Jahre liegen "
            "bei.",
        ],
        ["Mit freundlichen Grüßen", "", "", "Ingo Scholz", "Technischer Leiter"],
    )
    date_stamp(d, pg, "EINGANG", "14. SEP. 2026", 160, 52, STAMP_BLUE, angle=-4)
    stamp(d, pg, ["KOPIE"], 160, 36, pt=16, fill=STAMP_RED, angle=6, marks=[], gov=False)
    stamp(
        d,
        pg,
        ["ERLEDIGT"],
        150,
        245,
        pt=15,
        fill=GREEN,
        angle=-10,
        border="round",
        marks=[],
        gov=False,
    )
    pg.text(142, 253, "22.9. Wa", 11, "typewriter", "b", (30, 40, 120))


@document("draft", "neg-stamps", dpi=150, tags=("watermark-entwurf",))
def n_entwurf(d: Doc) -> None:
    pg = d.page()
    pg.text(105, 25, "Dienstvereinbarung", 16, "times", "b", INK, "c")
    pg.text(105, 33, "über mobiles Arbeiten", 13, "times", "r", INK, "c")
    pg.text(
        105, 40, "zwischen der Dienststellenleitung und dem Personalrat", 11, "times", "r", INK, "c"
    )
    y = 52.0
    for title, text in (
        (
            "§ 1 Geltungsbereich",
            "Diese Vereinbarung gilt für alle Beschäftigten der "
            "Dienststelle mit Ausnahme der Auszubildenden.",
        ),
        (
            "§ 2 Grundsätze",
            "Mobiles Arbeiten ist freiwillig. Ein Anspruch besteht nicht; "
            "Anträge sind jedoch wohlwollend zu prüfen. " + d.pick(DE_IT, 2),
        ),
        (
            "§ 3 Umfang",
            "Mobiles Arbeiten ist an bis zu drei Tagen pro Woche möglich. Die "
            "Erreichbarkeit während der Kernzeit ist sicherzustellen.",
        ),
        (
            "§ 4 Ausstattung",
            "Die Dienststelle stellt ein Notebook und ein Headset zur "
            "Verfügung. " + d.pick(DE_IT, 2),
        ),
        (
            "§ 5 Arbeitszeit",
            "Die Arbeitszeit wird elektronisch erfasst. Mehrarbeit ist vorher zu genehmigen.",
        ),
        (
            "§ 6 Inkrafttreten",
            "Die Vereinbarung tritt am 1. Januar 2027 in Kraft und kann "
            "mit einer Frist von sechs Monaten gekündigt werden.",
        ),
    ):
        pg.text(20, y, title, 11, "times", "b")
        y = pg.para(20, y + 5.5, 170, text, 11, "times", justify=True) + 4
    stamp(
        d,
        pg,
        ["ENTWURF"],
        105,
        150,
        pt=90,
        fill=(150, 150, 150),
        angle=35,
        border="none",
        opacity=0.35,
        marks=[],
        gov=False,
        texture=0.1,
    )
    page_numbers(pg, 1, 1, fmt="Stand: 03.09.2026 – Seite {n}", y=287, family="times")


@document(
    "handout",
    "neg-specimen",
    dpi=200,
    mentions=True,
    tags=("specimen", "stamp-muster"),
    note="a scaled-down specimen page shows where the marking goes; the MUSTER stamp "
    "across it makes clear it is an example",
)
def n_muster_specimen(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 16, "Anlage 3 zum Merkblatt GSB-04", 9, "arial", "r", GREY)
    pg.text(20, 25, "Muster: Kennzeichnung eines Schriftstücks", 15, "arial", "b")
    pg.para(
        20,
        35,
        170,
        "So wird ein Schriftstück des niedrigsten Geheimhaltungsgrades "
        "gekennzeichnet. Die Kennzeichnung steht oben und unten mittig auf jeder Seite.",
        10.5,
    )
    x0, y0, w, h = 55.0, 55.0, 100.0, 141.0
    pg.rect(x0 + 1.5, y0 + 1.5, w, h, outline=None, fill=(200, 200, 200))
    pg.rect(x0, y0, w, h, outline=GREY, fill=WHITE, width=0.3)
    pg.text(x0 + w / 2, y0 + 4, "VS – NUR FÜR DEN DIENSTGEBRAUCH", 7, "arial", "b", INK, "c")
    pg.text(x0 + w / 2, y0 + h - 7, "VS – NUR FÜR DEN DIENSTGEBRAUCH", 7, "arial", "b", INK, "c")
    for i in range(22):
        lw = w - 16 if i % 6 != 5 else w * 0.45
        pg.rect(x0 + 8, y0 + 18 + i * 5, lw, 1.6, outline=None, fill=(215, 215, 215))
    pg.text(x0 - 3, y0 + 4, "①", 10, "sans", "r", RED, "r")
    pg.text(x0 - 3, y0 + h - 7, "②", 10, "sans", "r", RED, "r")
    stamp(
        d,
        pg,
        ["MUSTER"],
        x0 + w / 2,
        y0 + h / 2,
        pt=44,
        fill=STAMP_RED,
        angle=30,
        border="box",
        marks=[],
        gov=False,
        opacity=0.8,
    )
    pg.bullets(
        20,
        206,
        170,
        [
            "① Kennzeichnung oben mittig, fett, mindestens so groß wie der Text.",
            "② Kennzeichnung unten mittig, auf jeder Seite wiederholt.",
            "Bei den höheren Graden kommen Registriernummer und Ausfertigungsnummer hinzu.",
        ],
        10,
        "sans",
        mark="",
    )


# ================================================================ negatives: everyday documents


@document("invoice", "neg-invoice", dpi=200, tags=("stamp-bezahlt", "table"))
def n_invoice_bezahlt(d: Doc) -> None:
    pg = d.page()
    y = letterhead_logo(
        pg,
        "Elektro Hansen GmbH",
        "Meisterbetrieb seit 1987",
        (230, 150, 0),
        "triangle",
        right=["Werkstraße 3", "24537 Neumünster", "USt-IdNr. DE 123 456 789"],
    )
    address_window(
        pg,
        y + 6,
        "Elektro Hansen · Werkstraße 3 · 24537 Neumünster",
        ["Familie Schröder", "Birkenallee 22", "24539 Neumünster"],
    )
    pg.text(20, 80, "Rechnung Nr. 2026-0931", 14, "arial", "b")
    pg.text(190, 80, "Datum: 02.09.2026", 10, "arial", "r", INK, "r")
    pg.text(190, 85, "Kundennr.: 10442", 10, "arial", "r", INK, "r")
    y = pg.table(
        20,
        94,
        [12, 98, 20, 20, 20],
        [
            ["Pos.", "Bezeichnung", "Menge", "EP €", "GP €"],
            [
                "1",
                "Netzwerkdose Cat. 6 für Internet-Anschluss im Arbeitszimmer",
                "2",
                "38,50",
                "77,00",
            ],
            ["2", "Verlegekabel Cat. 7, Meterware", "25", "1,90", "47,50"],
            ["3", "Austausch Fehlerstromschutzschalter", "1", "89,00", "89,00"],
            ["4", "Arbeitszeit Geselle", "3,5", "62,00", "217,00"],
            ["5", "Anfahrt", "1", "25,00", "25,00"],
        ],
        pt=9.5,
    )
    pg.table(
        110,
        y + 3,
        [50, 30],
        [
            ["Summe netto", "455,50"],
            ["USt 19 %", "86,55"],
            ["Gesamtbetrag", "542,05"],
        ],
        pt=10,
        header=False,
        bold_cols=(0,),
        grid=False,
    )
    pg.para(
        20,
        y + 30,
        170,
        "Zahlbar innerhalb von 14 Tagen ohne Abzug auf das Konto IBAN "
        "DE12 2105 0170 0000 1234 56. Vielen Dank für Ihren Auftrag!",
        10,
    )
    stamp(
        d,
        pg,
        ["BEZAHLT", "08.09.26"],
        150,
        200,
        pt=22,
        fill=STAMP_RED,
        angle=-14,
        sizes=[22, 12],
        marks=[],
        gov=False,
    )


@document("letter", "neg-stamps", dpi=200, tags=("stamp-eilt",))
def n_eilt(d: Doc) -> None:
    pg = d.page()
    y = letterhead_logo(
        pg,
        "Landratsamt Mittelaue",
        "Bauaufsichtsbehörde",
        (0, 100, 60),
        "leaf",
        right=["Schlossplatz 1", "04701 Mittelaue"],
    )
    stamp(d, pg, ["EILT!"], 165, 50, pt=22, fill=STAMP_RED, angle=-5, marks=[], gov=False)
    y = address_window(
        pg,
        y + 6,
        "Landratsamt Mittelaue · Schlossplatz 1 · 04701 Mittelaue",
        ["Herrn", "Dieter Kowalski", "Mühlgasse 4", "04703 Bad Auen"],
    )
    y += 12
    pg.text(20, y, "Bauantrag Carport, Flurstück 118/4 – fehlende Unterlagen", 11, "arial", "b")
    body_text(
        pg,
        y + 9,
        "Sehr geehrter Herr Kowalski,",
        [
            "Ihr Bauantrag vom 12. August ist bei uns eingegangen. Für die weitere Bearbeitung "
            "fehlen noch der amtliche Lageplan und die Zustimmung des Nachbarn.",
            "Bitte reichen Sie die Unterlagen bis zum 30. September nach. Andernfalls müssen wir "
            "den Antrag als zurückgenommen behandeln.",
        ],
        ["Mit freundlichen Grüßen", "Im Auftrag", "", "Simone Arnold"],
    )


@document("blank", "neg-blank", dpi=200, tags=("blank",))
def n_blank(d: Doc) -> None:
    pg = d.page()
    arr = np.full((pg.img.height, pg.img.width), 252, np.float32)
    arr += d.noise.normal(0, 2.5, arr.shape)
    arr[d.noise.random(arr.shape) < 0.00015] = 90
    pg.img = Image.fromarray(arr.clip(0, 255).astype(np.uint8)).convert("RGB")
    pg.draw = ImageDraw.Draw(pg.img)


@document(
    "blank",
    "neg-blank",
    fmt="tif",
    dpi=150,
    degradation="aged",
    tags=("blank", "punch-holes", "multi-page"),
)
def n_blank_scan(d: Doc) -> None:
    for _ in range(2):
        pg = d.page((246, 244, 238))
        for cy in (118.5, 178.5):
            pg.draw.ellipse([pg.px(7), pg.px(cy - 3), pg.px(13), pg.px(cy + 3)], fill=(60, 60, 60))


@document(
    "recipe",
    "neg-words",
    dpi=200,
    tags=("ordinary-words",),
    note="Geheimrezept, Geheimtipp: ordinary words",
)
def n_recipe(d: Doc) -> None:
    pg = d.page((253, 250, 240))
    pg.text(105, 20, "Omas Apfelstrudel", 24, "garamond", "b", (120, 50, 20), "c")
    pg.text(105, 32, "– mein Geheimrezept –", 13, "garamond", "i", (120, 50, 20), "c")
    pg.text(20, 48, "Zutaten (für ein Blech)", 13, "garamond", "b")
    pg.bullets(
        20,
        56,
        80,
        [
            "250 g Mehl",
            "1 Ei",
            "125 ml lauwarmes Wasser",
            "3 EL Öl",
            "1 Prise Salz",
            "1,5 kg säuerliche Äpfel",
            "100 g Zucker",
            "80 g Rosinen",
            "1 TL Zimt",
            "80 g Butter",
            "Semmelbrösel",
        ],
        11,
        "garamond",
        gap=0.5,
    )
    pg.text(110, 48, "Zubereitung", 13, "garamond", "b")
    y = 56.0
    for i, step in enumerate(
        [
            "Mehl, Ei, Wasser, Öl und Salz zu einem glatten Teig kneten und 30 Minuten ruhen "
            "lassen.",
            "Äpfel schälen, in dünne Scheiben schneiden und mit Zucker, Zimt und Rosinen mischen.",
            "Den Teig auf einem bemehlten Tuch hauchdünn ausziehen.",
            "Mit Butter bestreichen, Brösel und Äpfel darauf verteilen und mit Hilfe des Tuchs "
            "aufrollen.",
            "Bei 190 °C etwa 40 Minuten goldbraun backen.",
        ],
        1,
    ):
        pg.text(110, y, f"{i}.", 11, "garamond", "b")
        y = pg.para(116, y, 74, step, 11, "garamond") + 2
    pg.rect(20, 190, 170, 28, outline=(120, 50, 20), width=0.4, radius=3)
    pg.para(
        25,
        194,
        160,
        "Geheimtipp: Die Rosinen am Vorabend in einem Schuss Rum einweichen "
        "und den Strudel noch warm mit Vanillesoße servieren.",
        11.5,
        "garamond",
        "i",
    )


@document(
    "notice", "neg-words", dpi=150, tags=("ordinary-words",), note="'geheim' describes the ballot"
)
def n_wahl(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 16, "Stadt Neuweiler · Wahlamt", 10, "arial", "r", GREY)
    pg.text(20, 26, "Wahlbenachrichtigung", 18, "arial", "b")
    pg.text(20, 36, "zur Wahl der Bürgermeisterin / des Bürgermeisters am 18. Oktober 2026", 11)
    pg.table(
        20,
        48,
        [60, 110],
        [
            ["Wählerverzeichnis Nr.", "0417"],
            ["Wahlbezirk", "012 – Nordstadt"],
            ["Wahlraum", "Grundschule am Park, Eingang Turnhalle, barrierefrei"],
            ["Wahlzeit", "8:00 bis 18:00 Uhr"],
        ],
        pt=10.5,
        header=False,
        bold_cols=(0,),
    )
    pg.paras(
        20,
        90,
        170,
        [
            "Die Wahl ist allgemein, unmittelbar, frei, gleich und geheim. Bitte bringen Sie diese "
            "Benachrichtigung und Ihren Personalausweis mit.",
            "In der Wahlkabine kennzeichnen Sie Ihren Stimmzettel unbeobachtet. Das Wahlgeheimnis "
            "ist auch bei der Briefwahl zu wahren.",
            "Wenn Sie per Brief wählen möchten, füllen Sie den Antrag auf der Rückseite aus. Die "
            "Unterlagen werden Ihnen dann zugeschickt.",
        ],
        10.5,
        justify=True,
    )
    pg.rect(20, 150, 170, 40, outline=INK, width=0.3)
    pg.text(24, 153, "Antrag auf Briefwahl", 11, "arial", "b")
    pg.checkbox(24, 162, 3.5)
    pg.text(30, 161.8, "Ich beantrage einen Wahlschein mit Briefwahlunterlagen.", 10)
    pg.text(24, 178, "Datum, Unterschrift: ________________________________", 10)


@document(
    "contract",
    "neg-contract",
    fmt="pdf",
    dpi=200,
    mentions=True,
    tags=("nda", "headings"),
    note="non-disclosure agreement: 'vertraulich' and 'Geschäftsgeheimnis' in running text "
    "and headings; the document itself carries no marking",
)
def n_nda(d: Doc) -> None:
    sections = [
        (
            "§ 1 Vertrauliche Informationen",
            "Vertrauliche Informationen sind alle Unterlagen, "
            "Daten und Kenntnisse, die eine Partei der anderen im Rahmen der Gespräche über eine "
            "mögliche Zusammenarbeit bei der Entwicklung von Dosierpumpen zugänglich macht, "
            "gleich in welcher Form.",
        ),
        (
            "§ 2 Pflichten des Empfängers",
            "Der Empfänger verpflichtet sich, die vertraulichen "
            "Informationen streng vertraulich zu behandeln, sie nur für den vereinbarten Zweck zu "
            "verwenden und sie nur solchen Mitarbeitern zugänglich zu machen, die sie für diesen "
            "Zweck benötigen.",
        ),
        (
            "§ 3 Ausnahmen",
            "Die Pflichten gelten nicht für Informationen, die öffentlich bekannt "
            "sind, die dem Empfänger bereits bekannt waren oder die er von einem Dritten "
            "rechtmäßig erhalten hat.",
        ),
        (
            "§ 4 Geschäftsgeheimnisse",
            "Informationen, die als Geschäftsgeheimnis im Sinne des "
            "Gesetzes zum Schutz von Geschäftsgeheimnissen anzusehen sind, bleiben auch nach "
            "Ende dieser Vereinbarung geschützt.",
        ),
        (
            "§ 5 Rückgabe",
            "Auf Verlangen sind alle Unterlagen einschließlich Kopien "
            "zurückzugeben oder zu vernichten; die Vernichtung ist schriftlich zu bestätigen.",
        ),
        ("§ 6 Laufzeit", "Diese Vereinbarung gilt für drei Jahre ab Unterzeichnung."),
        (
            "§ 7 Schlussbestimmungen",
            "Es gilt deutsches Recht. Gerichtsstand ist Fulda. "
            "Änderungen bedürfen der Schriftform.",
        ),
    ]
    for n, part in enumerate((sections[:4], sections[4:]), 1):
        pg = d.page()
        page_numbers(pg, n, 2, y=288, family="times")
        y = 22.0
        if n == 1:
            pg.text(105, 22, "Vertraulichkeitsvereinbarung", 17, "times", "b", INK, "c")
            y = (
                pg.para(
                    20,
                    36,
                    170,
                    "zwischen der Hansewerk Pumpen AG, Am Pumpwerk 1, 36037 "
                    "Fulda, und der Kranz Umwelttechnik GmbH, Gewerbepark 5, 99084 Erfurt "
                    "(gemeinsam die Parteien)",
                    11,
                    "times",
                )
                + 6
            )
        for title, text in part:
            pg.text(20, y, title, 11.5, "times", "b")
            y = pg.para(20, y + 6, 170, text, 11, "times", justify=True) + 5
        if n == 2:
            for x, who in ((20, "Hansewerk Pumpen AG"), (115, "Kranz Umwelttechnik GmbH")):
                pg.hline(x, x + 70, y + 25, 0.2)
                pg.text(x, y + 27, who, 10, "times")


@document("handout", "neg-lecture", dpi=200, mentions=True, tags=("english", "grade-list"))
def n_lecture_en(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 14, "IS 402 · Information Security Governance · Autumn 2026", 9, "arial", "r", GREY)
    pg.text(20, 24, "Week 5 handout: Classification schemes compared", 16, "arial", "b")
    y = pg.para(
        20,
        35,
        170,
        "Governments protect information according to the damage its "
        "disclosure could cause. The labels differ from country to country, but the "
        "ladders are similar. Learn the terms below for the quiz next week.",
        10.5,
        justify=True,
    )
    y = pg.bullets(
        20,
        y + 5,
        170,
        [
            "United States: TOP SECRET, SECRET and CONFIDENTIAL. Controlled Unclassified "
            "Information (CUI) is not a classification level but a handling regime.",
            "Banner lines such as SECRET//NOFORN appear at the top and bottom of every page; "
            "portion marks like (S) or (U) precede each paragraph.",
            "United Kingdom: OFFICIAL, SECRET and TOP SECRET; OFFICIAL-SENSITIVE is a handling "
            "caveat within OFFICIAL.",
            "NATO: from NATO RESTRICTED up to COSMIC TOP SECRET.",
            "Germany: from VS-NUR FÜR DEN DIENSTGEBRAUCH up to STRENG GEHEIM.",
        ],
        10.5,
    )
    pg.text(20, y + 5, "Discussion questions", 12, "arial", "b")
    pg.bullets(
        20,
        y + 12,
        170,
        [
            "Why do most schemes have exactly four levels?",
            "What goes wrong when a document is over-classified?",
            "How would you mark an e-mail thread that mixes levels?",
        ],
        10.5,
        mark="–",
    )


@document(
    "letter",
    "neg-at",
    dpi=200,
    mentions=True,
    tags=("at", "grade-list", "letterhead"),
    note="Austrian ministry circular explaining the grades; itself unclassified",
)
def n_at_rundschreiben(d: Doc) -> None:
    pg = d.page()
    y = letterhead_at(
        pg,
        ["Bundesministerium", "Landesverteidigung"],
        ["Roßauer Lände 1, 1090 Wien", "Geschäftszahl: 2026-0.601.225", "Informationssicherheit"],
    )
    pg.text(20, y + 4, "Rundschreiben Nr. 12/2026", 14, "arial", "b")
    pg.text(
        20,
        y + 12,
        "Betreff: Informationssicherheit; Klassifizierungsstufen – Erinnerung",
        10.5,
        "arial",
        "b",
    )
    y = pg.para(
        20,
        y + 22,
        170,
        "Aus gegebenem Anlass wird an die Klassifizierungsstufen nach "
        "dem Informationssicherheitsgesetz erinnert. Klassifizierte Informationen sind "
        "je nach Schutzbedarf wie folgt zu bezeichnen:",
        10.5,
        justify=True,
    )
    for grade, text in (
        (
            "EINGESCHRÄNKT",
            "wenn die unbefugte Weitergabe den Interessen der Republik zuwiderlaufen würde;",
        ),
        ("VERTRAULICH", "wenn die Geheimhaltung im öffentlichen Interesse geboten ist;"),
        ("GEHEIM", "wenn die Preisgabe die Gefahr einer erheblichen Schädigung bewirken würde;"),
        ("STRENG GEHEIM", "wenn die Preisgabe die Gefahr eines schweren Schadens bewirken würde."),
    ):
        pg.text(25, y + 3, grade, 10.5, "arial", "b")
        y = pg.para(75, y + 3, 115, text, 10.5) + 1.5
    y = pg.paras(
        20,
        y + 6,
        170,
        [
            "Die Bezeichnung ist am Beginn und am Ende jeder Seite anzubringen. Nicht "
            "klassifizierte "
            "Schriftstücke tragen keine Bezeichnung.",
            "Dieses Rundschreiben ist an alle Bediensteten weiterzugeben.",
        ],
        10.5,
        justify=True,
    )
    pg.text(20, y + 6, "Wien, am 1. September 2026", 10.5)
    pg.text(120, y + 6, "Für die Bundesministerin:", 10.5)
    pg.text(120, y + 16, "Brigadier Mag. Steiner", 10.5)


@document(
    "handout",
    "neg-ch",
    dpi=200,
    mentions=True,
    tags=("ch", "grade-list", "letterhead"),
    note="Swiss federal leaflet explaining the classification levels; itself unclassified",
)
def n_ch_merkblatt(d: Doc) -> None:
    pg = d.page()
    y = letterhead_ch(
        pg,
        "Eidgenössisches Departement für Verteidigung, Bevölkerungsschutz und Sport VBS",
        "Fachstelle Informationssicherheit",
        top=16,
    )
    ch_footer(pg, "Fachstelle Informationssicherheit", "Maulbeerstrasse 9")
    pg.text(20, y + 10, "Merkblatt: Klassifizierung von Informationen", 15, "arial", "b")
    y = pg.para(
        20,
        y + 20,
        170,
        swiss(
            "Informationen des Bundes werden je nach Schutzbedarf "
            "als INTERN, VERTRAULICH oder GEHEIM klassifiziert. Die Klassifizierung erfolgt "
            "durch die Verfasserin oder den Verfasser und ist auf jeder Seite anzubringen."
        ),
        10.5,
        justify=True,
    )
    y = pg.table(
        20,
        y + 5,
        [35, 75, 60],
        [
            ["Stufe", "Wann?", "Beispiele"],
            [
                "INTERN",
                "Kenntnisnahme durch Unberechtigte kann den Interessen des Bundes schaden",
                "Telefonlisten, Entwürfe von Weisungen",
            ],
            [
                "VERTRAULICH",
                "Kenntnisnahme kann den Landesinteressen erheblich schaden",
                "Einsatzpläne, Personalakten",
            ],
            [
                "GEHEIM",
                "Kenntnisnahme kann den Landesinteressen schwer schaden",
                "Verteidigungsplanung",
            ],
        ],
        pt=9.5,
    )
    pg.para(
        20,
        y + 6,
        170,
        swiss(
            "Nicht klassifizierte Informationen werden nicht "
            "bezeichnet. Bei Unsicherheit wenden Sie sich an die Fachstelle."
        ),
        10.5,
    )


@document("press", "neg-eu", dpi=200, mentions=True, tags=("english", "grade-list"))
def n_eu_press(d: Doc) -> None:
    pg = d.page()
    emblem_eu(pg, 20, 16, 22)
    pg.text(47, 17, "Council of the EU", 12, "arial", "b", (0, 51, 153))
    pg.text(47, 23, "PRESS RELEASE", 10, "arial", "r", (0, 51, 153))
    pg.text(190, 17, "612/26", 10, "arial", "b", INK, "r")
    pg.text(190, 23, "15/09/2026", 10, "arial", "r", INK, "r")
    pg.para(
        20,
        42,
        170,
        "Security rules: Council updates the protection of EU classified information",
        16,
        "arial",
        "b",
        lead=1.2,
    )
    pg.paras(
        20,
        62,
        170,
        [
            "The Council today adopted updated security rules for the protection of EU classified "
            "information. The rules cover the four classification levels TRÈS SECRET UE/EU TOP "
            "SECRET, SECRET UE/EU SECRET, CONFIDENTIEL UE/EU CONFIDENTIAL and RESTREINT UE/EU "
            "RESTRICTED.",
            "The new rules simplify the handling of RESTREINT UE/EU RESTRICTED information on "
            "accredited national systems and introduce common standards for electronic "
            "registries.",
            "“These rules allow us to share information faster and more securely”, the "
            "Presidency said. The rules will apply from 1 January 2027.",
            "Background: the security rules were last revised in 2013. They apply to the Council, "
            "the General Secretariat and all bodies that handle EU classified information.",
        ],
        10.5,
        justify=True,
    )
    pg.text(
        20,
        270,
        "Press office – General Secretariat of the Council · press@example.eu",
        8.5,
        "arial",
        "r",
        GREY,
    )


@document("programme", "neg-nato", dpi=200, mentions=True, tags=("english", "table"))
def n_nato_conference(d: Doc) -> None:
    pg = d.page()
    emblem_nato(pg, 30, 26, 9)
    pg.text(44, 18, "NATO Industry Forum 2026", 16, "arial", "b", NAVY)
    pg.text(44, 26, "Programme · Lisbon, 20–21 October 2026", 10.5, "arial", "r", GREY)
    y = pg.table(
        20,
        46,
        [28, 102, 40],
        [
            ["Time", "Session", "Room"],
            ["09:00", "Opening remarks by the Assistant Secretary General", "Plenary"],
            ["09:45", "Panel 1: Capability targets and industrial capacity", "Plenary"],
            [
                "11:00",
                "Panel 2: Protecting NATO SECRET information in federated mission networks",
                "Room A",
            ],
            [
                "11:00",
                "Workshop: From NATO RESTRICTED to public release – what can be shared?",
                "Room B",
            ],
            ["13:30", "Panel 3: Secure supply chains for munitions", "Plenary"],
            ["15:00", "Matchmaking sessions for small and medium enterprises", "Hall 2"],
            ["17:30", "Reception", "Terrace"],
        ],
        pt=10,
    )
    pg.para(
        20,
        y + 8,
        170,
        "Registration closes on 30 September. All sessions are "
        "unclassified and held under the Chatham House rule.",
        10.5,
    )


@document(
    "fax",
    "neg-disclaimer",
    dpi=200,
    degradation="fax",
    mentions=True,
    tags=("fax-cover", "disclaimer"),
)
def n_fax_disclaimer(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 3, "15-09-26 14:22  VON: KANZLEI BERGER   +49 611 555 0120   S. 1/4", 8, "courier")
    pg.text(20, 16, "Berger & Partner", 18, "times", "b")
    pg.text(20, 25, "Rechtsanwälte · Fachanwälte für Arbeitsrecht", 10, "times", "i")
    pg.text(190, 16, "TELEFAX", 22, "arial", "b", INK, "r")
    y = pg.table(
        20,
        40,
        [45, 125],
        [
            ["An", "Hansewerk Pumpen AG, Personalabteilung"],
            ["Fax", "0661 9031-199"],
            ["Von", "RA Dr. Katrin Berger"],
            ["Datum", "15.09.2026"],
            ["Seiten", "4 (inkl. Deckblatt)"],
            ["Betreff", "Arbeitsgerichtsverfahren Müller ./. Hansewerk – Schriftsatz vom 14.09."],
        ],
        pt=11,
        header=False,
        bold_cols=(0,),
    )
    pg.paras(
        20,
        y + 8,
        170,
        [
            "Sehr geehrte Damen und Herren,",
            "anbei übersende ich Ihnen den Schriftsatz der Gegenseite zur Kenntnis. Ich schlage "
            "eine kurze Besprechung in der kommenden Woche vor.",
            "Mit freundlichen Grüßen",
            "Dr. Katrin Berger",
        ],
        11,
        "times",
    )
    pg.rect(20, 225, 170, 30, outline=INK, width=0.3)
    pg.para(
        24,
        228,
        162,
        "Vertraulichkeitshinweis: Dieses Telefax ist vertraulich und "
        "ausschließlich für den angegebenen Empfänger bestimmt. Sollten Sie es irrtümlich "
        "erhalten haben, benachrichtigen Sie uns bitte sofort telefonisch und vernichten "
        "Sie die Unterlagen.",
        9.5,
        "times",
    )


@document(
    "minutes",
    "neg-words",
    dpi=200,
    tags=("ordinary-words", "table"),
    note="'geheime Abstimmung' is a secret ballot",
)
def n_betriebsrat(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 18, "Betriebsrat der Hansewerk Pumpen AG, Werk Fulda", 10, "arial", "r", GREY)
    pg.text(20, 27, "Protokoll der konstituierenden Sitzung", 15, "arial", "b")
    pg.text(20, 35, "3. September 2026, 14:00 bis 15:40 Uhr, Besprechungsraum 2", 10.5)
    y = pg.table(
        20,
        46,
        [15, 110, 45],
        [
            ["TOP", "Beratung / Beschluss", "Ergebnis"],
            ["1", "Feststellung der Beschlussfähigkeit: 9 von 9 Mitgliedern anwesend.", "–"],
            [
                "2",
                "Wahl der/des Vorsitzenden in geheimer Abstimmung. Vorgeschlagen: Anja Roth.",
                "7 Ja, 2 Nein",
            ],
            [
                "3",
                "Wahl der Stellvertretung, ebenfalls geheime Wahl. Vorgeschlagen: Mehmet Aydin.",
                "8 Ja, 1 Enthaltung",
            ],
            [
                "4",
                "Festlegung der Sitzungstermine: jeden zweiten Mittwoch, 14:00 Uhr.",
                "einstimmig",
            ],
            ["5", "Verschiedenes: Schulung der neuen Mitglieder im November.", "–"],
        ],
        pt=10,
    )
    pg.text(20, y + 10, "Protokoll: Jana Weiß", 10.5)
    pg.text(120, y + 10, "Vorsitzende: Anja Roth", 10.5)


@document(
    "listing",
    "neg-news",
    fmt="jpg",
    dpi=200,
    degradation="jpeg",
    mentions=True,
    tags=("tv-guide", "titles"),
)
def n_tv_listing(d: Doc) -> None:
    pg = d.page()
    pg.rect(20, 14, 170, 14, outline=None, fill=(20, 20, 20))
    pg.text(24, 16.5, "TV-Programm · Freitag, 25. September", 16, "arial", "b", WHITE)
    channels = [
        (
            "Das Erste",
            [
                ("20:15", "Tatort: Die stille Stadt", "Krimi, D 2026"),
                ("21:45", "Streng geheim – Spione im Kalten Krieg", "Dokumentation, Teil 3 von 4"),
                ("22:30", "Tagesthemen", "Nachrichten"),
            ],
        ),
        (
            "ZDF",
            [
                ("20:15", "Der Bergdoktor", "Arztserie"),
                ("21:45", "heute-journal", "Nachrichten"),
                ("22:15", "Geheimnisvolle Tiefsee", "Naturdoku"),
            ],
        ),
        (
            "Kabel 1",
            [
                ("20:15", "Top Secret!", "Komödie, USA 1984, mit Val Kilmer"),
                ("22:00", "Die Akte Jane", "Actionfilm, USA 1997"),
            ],
        ),
        (
            "arte",
            [
                ("20:15", "Die geheime Bibliothek", "Doku, F 2025"),
                ("21:10", "Konzert aus Aix-en-Provence", "Musik"),
            ],
        ),
    ]
    y = 36.0
    for name, shows in channels:
        pg.text(20, y, name, 13, "arial", "b", (180, 30, 30))
        y += 7
        for time, title, info in shows:
            pg.text(20, y, time, 11, "arial", "b")
            pg.text(38, y, title, 11, "arial", "b")
            pg.text(38, y + 5, info, 9.5, "arial", "r", GREY)
            y += 12
        y += 4
    pg.rect(120, 36, 70, 70, outline=None, fill=(235, 235, 235))
    pg.para(
        124,
        40,
        62,
        "Tipp des Tages: „Top Secret!“ – die Agentenparodie der Macher von "
        "„Die nackte Kanone“ ist auch nach 40 Jahren noch komisch.",
        10,
        "arial",
    )


@document(
    "book", "neg-history", dpi=240, degradation="aged", mentions=True, tags=("book-page", "serif")
)
def n_history_book(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 18, "182", 10, "garamond")
    pg.text(190, 18, "Kapitel 7 · Das Archiv des Bündnisses", 10, "garamond", "i", INK, "r")
    pg.paras(
        28,
        32,
        154,
        [
            "Als die ersten Bestände 2009 zugänglich wurden, staunten die Historiker über die "
            "Menge. Allein die Unterlagen des Militärausschusses füllten mehr als zwei Kilometer "
            "Regal. Viele der Akten trugen noch den Vermerk COSMIC TOP SECRET, andere waren als "
            "NATO SECRET eingestuft gewesen.",
            "Die Freigabe folgte einem strengen Verfahren. Jedes Dokument wurde von den "
            "Mitgliedstaaten geprüft, deren Informationen es enthielt; nicht selten dauerte das "
            "Jahre. Was als streng geheim gegolten hatte, erwies sich oft als erstaunlich banal: "
            "Lieferlisten, Dienstpläne, Protokolle über die Farbe von Fahrzeugen.",
            "Spannender waren die Randnotizen. Ein belgischer Offizier vermerkte 1961 neben einer "
            "Lagebeurteilung trocken: „Nichts Neues – bitte nicht wieder als GEHEIM einstufen.“ "
            "Solche Bemerkungen zeigen, wie selbstverständlich der Umgang mit Einstufungen im "
            "Alltag der Stäbe geworden war.",
            "Heute können Forscherinnen und Forscher die freigegebenen Akten im Lesesaal in "
            "Brüssel einsehen. Ein Teil ist digitalisiert und online recherchierbar.",
        ],
        11.5,
        "garamond",
        gap=0,
        justify=True,
        lead=1.45,
    )


@document(
    "invoice",
    "neg-invoice",
    dpi=200,
    mentions=True,
    tags=("table", "stamp-words"),
    note="an invoice for rubber stamps that lists their texts",
)
def n_stamp_shop(d: Doc) -> None:
    pg = d.page()
    y = letterhead_logo(
        pg,
        "Stempel-Paul GmbH",
        "Stempel · Schilder · Gravuren",
        (60, 60, 160),
        "circle",
        right=["Kaiserstraße 88", "60329 Frankfurt am Main"],
    )
    address_window(
        pg,
        y + 6,
        "Stempel-Paul GmbH · Kaiserstraße 88 · 60329 Frankfurt",
        ["Hansewerk Pumpen AG", "Einkauf", "Am Pumpwerk 1", "36037 Fulda"],
    )
    pg.text(20, 80, "Rechnung Nr. 26-4471", 14, "arial", "b")
    pg.text(190, 80, "05.09.2026", 10, "arial", "r", INK, "r")
    y = pg.table(
        20,
        92,
        [12, 108, 18, 32],
        [
            ["Pos.", "Artikel", "Menge", "Betrag €"],
            ["1", "Textstempel „VERTRAULICH“, Abdruck rot, 58 × 22 mm", "3", "71,70"],
            ["2", "Textstempel „STRENG GEHEIM“, Abdruck rot, 58 × 22 mm", "1", "23,90"],
            ["3", "Eingangsstempel mit Datum, Abdruck blau", "2", "79,80"],
            ["4", "Stempelkissen rot, Ersatz", "4", "19,60"],
            ["5", "Versand", "1", "5,90"],
        ],
        pt=9.5,
    )
    pg.table(
        110,
        y + 3,
        [50, 30],
        [["Summe netto", "200,90"], ["USt 19 %", "38,17"], ["Gesamt", "239,07"]],
        pt=10,
        header=False,
        bold_cols=(0,),
        grid=False,
    )
    pg.para(20, y + 30, 170, "Zahlbar bis 19.09.2026. Vielen Dank für Ihre Bestellung!", 10)


@document(
    "delivery",
    "neg-words",
    dpi=200,
    tags=("ordinary-words", "logo-vs"),
    note="VS is the company's name",
)
def n_vs_company(d: Doc) -> None:
    pg = d.page()
    pg.rect(20, 14, 22, 16, outline=None, fill=(0, 90, 150))
    pg.text(31, 15.5, "VS", 22, "arial", "b", WHITE, "c")
    pg.text(46, 15, "VS Verpackungssysteme GmbH", 15, "arial", "b", (0, 90, 150))
    pg.text(46, 23, "Kartonagen · Folien · Paletten", 9, "arial", "r", GREY)
    address_window(
        pg,
        42,
        "VS Verpackungssysteme GmbH · Hafenring 2 · 28197 Bremen",
        [
            "Brandt & Söhne Maschinenbau GmbH",
            "Wareneingang",
            "Industriestraße 18",
            "72762 Reutlingen",
        ],
    )
    pg.text(20, 82, "Lieferschein Nr. LS-26-11873", 14, "arial", "b")
    pg.text(190, 82, "Lieferdatum: 11.09.2026", 10, "arial", "r", INK, "r")
    pg.table(
        20,
        94,
        [25, 105, 20, 20],
        [
            ["Art.-Nr.", "Bezeichnung", "Menge", "Einheit"],
            ["K-4020", "Faltkarton 400 × 300 × 200 mm, 2-wellig", "500", "Stück"],
            ["F-0120", "Stretchfolie 23 µm, 500 mm", "24", "Rollen"],
            ["P-1208", "Europalette, neu", "20", "Stück"],
            ["E-0005", "Kantenschutz 50 × 50 mm", "200", "Stück"],
        ],
        pt=10,
    )
    pg.text(20, 160, "Ware vollständig und unbeschädigt erhalten:", 10)
    pg.hline(20, 90, 178, 0.2)
    pg.text(20, 180, "Datum, Unterschrift", 8.5, "arial", "r", GREY)


@document(
    "email",
    "neg-words",
    dpi=200,
    tags=("ordinary-words", "english-word"),
    note="Secret Santa is a gift exchange",
)
def n_secret_santa(d: Doc) -> None:
    pg = d.page()
    y = outlook(
        pg,
        "Team 4 – Vertrieb",
        [
            ("Von:", "Schulte, Ben"),
            ("Gesendet:", "Montag, 21. September 2026 09:02"),
            ("An:", "Team 4 – Vertrieb"),
            ("Betreff:", "Secret Santa 2026 – Wichteln im Team"),
        ],
    )
    pg.paras(
        20,
        y,
        170,
        [
            "Hallo zusammen,",
            "auch dieses Jahr machen wir wieder Secret Santa! Wer mitmachen möchte, trägt sich bis "
            "Ende Oktober in die Liste im Teamkanal ein. Die Auslosung ist am 2. November.",
            "Budget: höchstens 15 Euro. Die Geschenke werden bei der Weihnachtsfeier am 11. "
            "Dezember verteilt – verratet bis dahin bitte nicht, wen ihr gezogen habt.",
            "Viele Grüße",
            "Ben",
        ],
        10.5,
        gap=2.5,
    )


@document(
    "sign",
    "neg-words",
    dpi=150,
    tags=("sign", "english-word"),
    note="a site safety sign; RESTRICTED AREA is not a classification",
)
def n_restricted_area(d: Doc) -> None:
    pg = d.page()
    pg.rect(15, 20, 180, 60, outline=None, fill=(200, 20, 30))
    pg.text(105, 30, "RESTRICTED AREA", 40, "arial", "b", WHITE, "c")
    pg.text(105, 55, "AUTHORISED PERSONNEL ONLY", 20, "arial", "b", WHITE, "c")
    cx, cy, r = 105.0, 135.0, 32.0
    pg.draw.ellipse(
        [pg.px(cx - r), pg.px(cy - r), pg.px(cx + r), pg.px(cy + r)],
        outline=(200, 20, 30),
        width=pg.px(5),
    )
    pg.draw.line(
        [(pg.px(cx - r * 0.7), pg.px(cy + r * 0.7)), (pg.px(cx + r * 0.7), pg.px(cy - r * 0.7))],
        fill=(200, 20, 30),
        width=pg.px(5),
    )
    pg.text(105, 180, "Zutritt nur für befugte Personen", 20, "arial", "b", INK, "c")
    pg.text(105, 195, "Accès réservé au personnel autorisé", 14, "arial", "r", INK, "c")
    pg.text(
        105, 250, "Kestrel Chemicals Ltd · Site Safety · Tank Farm B", 11, "arial", "r", GREY, "c"
    )


@document(
    "leaflet",
    "neg-words",
    dpi=200,
    tags=("multilingual", "ordinary-words"),
    note="Italian 'cui' is an ordinary word",
)
def n_italian_leaflet(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 16, "Wasserkocher WK 17 · Bollitore elettrico · Bouilloire", 14, "arial", "b")
    y = 28.0
    for lang, text in (
        (
            "DE",
            "Füllen Sie den Wasserkocher nur bis zur Markierung MAX. Stellen Sie das "
            "Gerät auf eine ebene Fläche, auf der es nicht umkippen kann. Das Gerät schaltet "
            "sich automatisch ab, sobald das Wasser kocht.",
        ),
        (
            "IT",
            "Riempire il bollitore solo fino al segno MAX. Collocare l'apparecchio su una "
            "superficie piana, in cui non possa ribaltarsi. Il bollitore, di cui si consiglia "
            "la decalcificazione mensile, si spegne automaticamente quando l'acqua bolle. Le "
            "istruzioni per il caso in cui l'apparecchio non si accenda si trovano a pagina 4.",
        ),
        (
            "FR",
            "Ne remplissez la bouilloire que jusqu'au repère MAX. Posez l'appareil sur une "
            "surface plane. L'appareil s'arrête automatiquement dès que l'eau bout.",
        ),
    ):
        pg.rect(20, y, 10, 6, outline=None, fill=(60, 60, 60))
        pg.text(25, y + 0.8, lang, 9, "arial", "b", WHITE, "c")
        y = pg.para(35, y, 155, text, 10.5, justify=True) + 6
    pg.table(
        20,
        y + 4,
        [60, 40, 40, 30],
        [
            ["Technische Daten / Dati tecnici", "Leistung", "Volumen", "Gewicht"],
            ["WK 17", "2200 W", "1,7 l", "1,1 kg"],
        ],
        pt=9.5,
    )


@document(
    "letter",
    "neg-disclaimer",
    dpi=200,
    degradation="fax",
    mentions=True,
    tags=("form", "checkbox", "disclaimer"),
    note="a doctor's practice promises to treat the answers 'streng vertraulich'",
)
def n_arztbrief(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 16, "Praxis Dr. med. Andrea Weber", 15, "times", "b", (0, 100, 110))
    pg.text(
        20,
        23,
        "Fachärztin für Allgemeinmedizin · Lindenstraße 3 · 36037 Fulda",
        9,
        "times",
        "r",
        GREY,
    )
    pg.text(20, 38, "Anamnesebogen für neue Patientinnen und Patienten", 14, "times", "b")
    y = pg.para(
        20,
        47,
        170,
        "Bitte füllen Sie diesen Bogen vor Ihrem ersten Termin aus. Ihre "
        "Angaben werden selbstverständlich streng vertraulich behandelt und unterliegen "
        "der ärztlichen Schweigepflicht.",
        11,
        "times",
    )
    for label in ("Name, Vorname", "Geburtsdatum", "Krankenkasse", "Hausärztin/Hausarzt bisher"):
        pg.text(20, y + 4, label + ":", 11, "times")
        pg.hline(70, 190, y + 9, 0.2)
        y += 9
    y += 6
    for question in (
        "Haben Sie Allergien?",
        "Nehmen Sie regelmäßig Medikamente?",
        "Rauchen Sie?",
        "Hatten Sie Operationen?",
    ):
        pg.text(20, y, question, 11, "times")
        pg.checkbox(130, y + 0.5, 3.5)
        pg.text(135, y, "ja", 11, "times")
        pg.checkbox(150, y + 0.5, 3.5)
        pg.text(155, y, "nein", 11, "times")
        y += 8
    pg.text(20, y + 10, "Datum, Unterschrift: ________________________________", 11, "times")


def courier_slip(d: Doc, ticked: str) -> Page:
    pg = d.page()
    pg.text(20, 15, "Bundesamt für Materialwirtschaft · Zentrale Poststelle", 9, "arial", "r", GREY)
    pg.text(20, 22, "Begleitschein für Kuriersendungen", 16, "arial", "b")
    pg.text(190, 23, "Vordruck ZP 12 (03/2024)", 8, "arial", "r", GREY, "r")
    y = 34.0
    for label, value in (
        ("Absender (Organisationseinheit)", "Z 3 – Innerer Dienst"),
        ("Empfänger", "Bundesamt, Außenstelle Mayen, Poststelle"),
        ("Inhalt / Bezeichnung", "Personalakten (Kopie) zur Übergabe"),
        ("Anzahl Blatt / Datenträger", "112 Blatt"),
        ("Datum der Übergabe", "17.09.2026"),
    ):
        pg.rect(20, y, 170, 13, outline=INK, width=0.25)
        pg.text(22, y + 1.2, label, 7, "arial", "r", GREY)
        pg.text(24, y + 5.8, value, 11, "courier")
        y += 13
    pg.rect(20, y, 170, 16, outline=INK, width=0.25)
    pg.text(22, y + 1.2, "Geheimhaltungsgrad", 7, "arial", "r", GREY)
    x = 24.0
    for option in ("offen", "VS-NfD", "VS-VERTRAULICH", "GEHEIM"):
        pg.checkbox(x, y + 7, 3.6, option == ticked)
        x += pg.text(x + 5, y + 6.8, option, 10.5, "arial", "b" if option == ticked else "r") + 14
    y += 16
    pg.rect(20, y, 85, 28, outline=INK, width=0.25)
    pg.rect(105, y, 85, 28, outline=INK, width=0.25)
    pg.text(22, y + 1.2, "Übergeben (Name, Unterschrift)", 7, "arial", "r", GREY)
    pg.text(107, y + 1.2, "Übernommen (Name, Unterschrift)", 7, "arial", "r", GREY)
    pg.text(24, y + 8, "Köhler", 11, "courier")
    scribble(d, pg, 45, y + 20, 26)
    return pg


@document(
    "form",
    "neg-form",
    dpi=200,
    mentions=True,
    tags=("checkbox", "form"),
    note="the ticked box is 'offen' (unclassified); the grades are unticked options",
)
def n_checkbox_offen(d: Doc) -> None:
    courier_slip(d, "offen")


@document(
    "notice",
    "neg-at",
    dpi=200,
    tags=("at", "ordinary-words", "sign"),
    note="Viennese road-works notice; 'eingeschränkt' describes access",
)
def n_zufahrt(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 16, "Stadt Wien", 16, "arial", "b", (200, 16, 46))
    pg.text(20, 24, "Straßenverwaltung und Straßenbau · Wien", 10, "arial", "r", GREY)
    pg.rect(15, 40, 180, 30, outline=None, fill=(255, 205, 0))
    pg.text(105, 46, "ZUFAHRT EINGESCHRÄNKT", 30, "arial", "b", INK, "c")
    pg.text(105, 82, "Bauarbeiten in der Rathausstraße", 18, "arial", "b", INK, "c")
    pg.text(105, 92, "5. bis 30. Oktober 2026", 16, "arial", "r", INK, "c")
    pg.paras(
        25,
        110,
        160,
        [
            "Wegen der Erneuerung der Wasserleitung ist die Zufahrt zur Rathausstraße zwischen "
            "Lichtenfelsgasse und Stadiongasse nur für Anrainerinnen und Anrainer sowie für "
            "Einsatzfahrzeuge möglich.",
            "Die Haltestelle der Buslinie 13A wird in die Landesgerichtsstraße verlegt. Wir "
            "ersuchen um Verständnis.",
        ],
        12,
        justify=True,
    )
    pg.text(
        105,
        250,
        "Informationen: Tel. 01 4000-8028 · www.wien.gv.at/baustellen",
        10,
        "arial",
        "r",
        GREY,
        "c",
    )


@document(
    "receipt",
    "neg-invoice",
    dpi=200,
    mentions=True,
    tags=("titles", "receipt"),
    note="book titles containing grade words",
)
def n_bookshop(d: Doc) -> None:
    pg = d.page()
    x0, w = 60.0, 90.0
    pg.rect(x0 - 4, 10, w + 8, 170, outline=(210, 210, 210), width=0.2)
    pg.text(105, 16, "Buchhandlung Lesezeichen", 13, "mono", "b", INK, "c")
    pg.text(105, 23, "Marktplatz 5 · 35510 Neuweiler", 8.5, "mono", "r", INK, "c")
    pg.text(105, 27, "Tel. 06441 23456", 8.5, "mono", "r", INK, "c")
    y = 38.0
    for title, price in (
        ("Top Secret 3 - Der Auftrag", "9,99"),
        ("Streng geheim! Rätselbuch", "7,50"),
        ("Geheimakte Tunguska (Roman)", "12,00"),
        ("Kalender 2027 Nordsee", "14,95"),
    ):
        pg.text(x0, y, title, 9, "mono")
        pg.text(x0 + w, y, price, 9, "mono", "r", INK, "r")
        y += 5
    pg.text(x0, y + 2, "-" * 38, 9, "mono")
    pg.text(x0, y + 7, "SUMME EUR", 10, "mono", "b")
    pg.text(x0 + w, y + 7, "44,44", 10, "mono", "b", INK, "r")
    pg.text(x0, y + 13, "Bar", 9, "mono")
    pg.text(x0 + w, y + 13, "50,00", 9, "mono", "r", INK, "r")
    pg.text(x0, y + 18, "Rückgeld", 9, "mono")
    pg.text(x0 + w, y + 18, "5,56", 9, "mono", "r", INK, "r")
    pg.text(x0, y + 26, "enth. MwSt 7 %  2,91", 9, "mono")
    pg.text(105, y + 36, "Vielen Dank für Ihren Einkauf!", 9, "mono", "r", INK, "c")
    pg.text(105, y + 41, "12.09.2026 16:42  Kasse 2", 8.5, "mono", "r", INK, "c")


@document(
    "notes",
    "neg-words",
    dpi=200,
    tags=("ordinary-words", "english-word"),
    note="interne API, Internet Explorer, Secrets as credentials: ordinary words",
)
def n_release_notes(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 18, "Aktenplan-Manager 4.2 – Versionshinweise", 16, "arial", "b")
    pg.text(20, 27, "Veröffentlicht am 10. September 2026", 10, "arial", "r", GREY)
    y = 38.0
    for title, items in (
        (
            "Neu",
            [
                "Eine interne API erlaubt anderen Fachverfahren den Zugriff auf Aktenzeichen.",
                "Suche über mehrere Registraturen gleichzeitig.",
                "Export nach PDF/A-3.",
            ],
        ),
        (
            "Geändert",
            [
                "Geheimnisse (Secrets) für die Schnittstellen werden jetzt im "
                "Schlüsseltresor gespeichert statt in der Konfigurationsdatei.",
                "Die Anmeldung erfolgt über das zentrale Konto.",
            ],
        ),
        ("Entfernt", ["Unterstützung für Internet Explorer 11.", "Der alte Druckdialog."]),
        (
            "Behobene Fehler",
            [
                "Falsche Sortierung bei Aktenzeichen mit Umlauten.",
                "Absturz beim Import großer Verzeichnisse.",
            ],
        ),
    ):
        pg.text(20, y, title, 12, "arial", "b", (0, 100, 160))
        y = pg.bullets(20, y + 7, 170, items, 10.5) + 4


@document(
    "booking",
    "neg-words",
    dpi=200,
    tags=("ordinary-words",),
    note="Internet, Geheimtipp: ordinary words",
)
def n_hotel(d: Doc) -> None:
    pg = d.page()
    y = letterhead_logo(
        pg,
        "Hotel Seeblick",
        "am Plöner See",
        (0, 120, 150),
        "leaf",
        right=["Seestraße 21", "24306 Plön"],
    )
    pg.text(20, y + 6, "Buchungsbestätigung", 15, "arial", "b")
    y = pg.table(
        20,
        y + 16,
        [55, 115],
        [
            ["Gast", "Frau Dr. Henrike Ahlers"],
            ["Anreise", "Freitag, 9. Oktober 2026, ab 15:00 Uhr"],
            ["Abreise", "Sonntag, 11. Oktober 2026, bis 11:00 Uhr"],
            ["Zimmer", "Doppelzimmer Seeseite mit Balkon"],
            ["Preis", "2 × 164,00 € inkl. Frühstück"],
            ["Leistungen", "WLAN/Internet, Parkplatz und Saunabereich kostenfrei"],
        ],
        pt=10.5,
        header=False,
        bold_cols=(0,),
    )
    pg.paras(
        20,
        y + 8,
        170,
        [
            "Liebe Frau Dr. Ahlers, wir freuen uns auf Ihren Besuch!",
            "Unser Geheimtipp: der Sonnenuntergang vom Steg hinter dem Haus. Für Ihren Ausflug auf "
            "den See leihen wir Ihnen gerne ein Ruderboot.",
            "Eine kostenfreie Stornierung ist bis 48 Stunden vor Anreise möglich.",
        ],
        10.5,
    )


@document(
    "minutes",
    "neg-tlp",
    dpi=200,
    mentions=True,
    tags=("tlp-in-text", "table"),
    note="the minutes decide how reports will be labelled; they carry no label themselves",
)
def n_minutes_tlp(d: Doc) -> None:
    pg = d.page()
    letterhead_logo(pg, "Stadtwerke Neuweiler", "IT-Sicherheit", (0, 120, 180), "circle")
    pg.text(20, 42, "Protokoll Jour fixe Informationssicherheit", 14, "arial", "b")
    pg.text(20, 50, "17. September 2026 · Teilnehmende: Scholz, Brandt, Yilmaz, Wolf", 10)
    y = pg.table(
        20,
        60,
        [15, 115, 40],
        [
            ["Nr.", "Thema / Beschluss", "Verantwortlich"],
            [
                "1",
                "Lageberichte an externe Partner werden künftig mit TLP:AMBER gekennzeichnet; "
                "öffentliche Hinweise erhalten TLP:CLEAR.",
                "Yilmaz",
            ],
            [
                "2",
                "Warnmeldungen des CERT mit TLP:RED werden nicht in das Ticketsystem "
                "übernommen, sondern nur mündlich weitergegeben.",
                "Scholz",
            ],
            [
                "3",
                "Phishing-Übung für alle Beschäftigten im November. " + d.pick(DE_IT, 1),
                "Brandt",
            ],
            ["4", "Prüfung der Notfallpläne. " + d.pick(DE_IT, 1), "Wolf"],
        ],
        pt=10,
    )
    pg.text(20, y + 8, "Nächster Termin: 1. Oktober 2026", 10.5)


@document(
    "datasheet",
    "neg-product",
    dpi=200,
    degradation="skew",
    mentions=True,
    tags=("english", "feature-box"),
)
def n_datasheet_en(d: Doc) -> None:
    pg = d.page()
    pg.text(20, 18, "SecurePhone S5", 26, "helv", "b", (0, 80, 60))
    pg.text(20, 31, "Secure mobile communication for government users", 13, "helv", "r", GREY)
    pg.hline(20, 190, 40, 1.0, (0, 80, 60))
    pg.text(20, 48, "Approvals", 13, "helv", "b", (0, 80, 60))
    y = pg.bullets(
        20,
        56,
        170,
        [
            "Approved for NATO RESTRICTED and EU RESTRICTED (RESTREINT UE) information.",
            "German BSI approval for VS-NfD voice and data.",
            "Common Criteria EAL4+ certified secure element.",
        ],
        11,
        "helv",
    )
    pg.text(20, y + 4, "Features", 13, "helv", "b", (0, 80, 60))
    y = pg.bullets(
        20,
        y + 12,
        170,
        [
            "Separate secure and open workspaces on one device.",
            "Encrypted voice calls and messaging with end-to-end key management.",
            "Remote wipe and central policy management.",
        ],
        11,
        "helv",
    )
    pg.table(
        20,
        y + 6,
        [60, 110],
        [
            ["Display", "6.1 inch OLED"],
            ["Battery", "4,500 mAh, up to two days"],
            ["Operating system", "hardened Android 15"],
        ],
        pt=10.5,
        family="helv",
        header=False,
        bold_cols=(0,),
    )


@document(
    "newsletter",
    "neg-words",
    fmt="pdf",
    dpi=200,
    tags=("masthead", "two-column"),
    note="'HAUS INTERN' is the name of the staff newsletter",
)
def n_haus_intern(d: Doc) -> None:
    pg = d.page()
    pg.rect(20, 14, 170, 26, outline=None, fill=(0, 120, 180))
    pg.text(26, 17, "HAUS INTERN", 26, "helv", "b", WHITE)
    pg.text(
        26, 31, "Die Zeitung für die Beschäftigten der Stadtwerke Neuweiler", 10, "helv", "r", WHITE
    )
    pg.text(184, 31, "Ausgabe 3/2026", 10, "helv", "b", WHITE, "r")
    two_columns(
        pg,
        48,
        [
            (
                "Neue Ladesäulen am Betriebshof",
                [
                    "Seit August können Beschäftigte ihre Elektroautos am Betriebshof laden. Die "
                    "zwölf neuen Ladepunkte werden mit Strom aus der eigenen Photovoltaikanlage "
                    "versorgt.",
                    d.pick(DE_CORP, 2),
                ],
            ),
            (
                "Sommerfest",
                [
                    "Mehr als 300 Kolleginnen und Kollegen feierten am 4. Juli im Freibad. Den "
                    "Höhepunkt bildete das Drachenbootrennen der Abteilungen."
                ],
            ),
        ],
        [
            (
                "Wir gratulieren",
                [
                    "Zum 25-jährigen Dienstjubiläum gratulieren wir Petra Lang (Kundenservice) und "
                    "Uwe Brandt (Netzbetrieb)."
                ],
            ),
            ("Termine", ["Betriebsversammlung am 14. Oktober, Gesundheitstag am 5. November."]),
            ("Aus dem Netzbetrieb", [d.pick(DE_IT, 2)]),
        ],
        colour=(0, 120, 180),
        pt=10,
        head_pt=12,
    )
    page_numbers(pg, 1, 1, fmt="HAUS INTERN 3/2026 · Seite {n}", y=287, family="helv")


@document(
    "letter", "neg-letter", fmt="jpg", dpi=200, degradation="jpeg", tags=("letterhead", "plain")
)
def n_behoerde_jpeg(d: Doc) -> None:
    pg = d.page()
    y = letterhead_federal(
        pg,
        ["Bundesamt für Küstenschutz", "und Wasserwirtschaft"],
        ["HAUSANSCHRIFT", "Bernhard-Nocht-Straße 78, 20359 Hamburg", "TEL +49 40 3190-0"],
    )
    y = address_window(
        pg,
        y + 8,
        "BKW, Postfach 30 12 20, 20305 Hamburg",
        ["Gemeinde Elbmarsch", "Bürgermeisteramt", "Deichstraße 1", "21493 Elbmarsch"],
    )
    pg.text(190, y - 6, "Hamburg, 3. September 2026", 10.5, "arial", "r", INK, "r")
    y += 12
    pg.text(20, y, "Informationsveranstaltung zur Deichverstärkung", 11, "arial", "b")
    body_text(
        pg,
        y + 9,
        "Sehr geehrte Damen und Herren,",
        [
            "wir laden Sie herzlich zur Informationsveranstaltung über die geplante Verstärkung "
            "des Elbdeichs ein. Sie findet am 1. Oktober um 18:00 Uhr in der Mehrzweckhalle statt.",
            d.pick(DE_ADMIN, 3),
            "Die Planunterlagen liegen ab 15. September im Rathaus zur Einsicht aus.",
        ],
        ["Mit freundlichen Grüßen", "Im Auftrag", "", "Dr. Henrike Ahlers"],
    )


@document(
    "report",
    "neg-report",
    fmt="tif",
    dpi=200,
    degradation="blur",
    tags=("multi-page", "page-numbers"),
)
def n_report_blur(d: Doc) -> None:
    for n in (1, 2, 3):
        pg = d.page()
        pg.text(20, 12, "Prüfbericht PB-2026-081 · Klimakammer Halle 4", 8, "arial", "r", GREY)
        pg.hline(20, 190, 16, 0.2, GREY)
        page_numbers(pg, n, 3, y=287)
        if n == 1:
            pg.text(20, 26, "Prüfbericht PB-2026-081", 18, "arial", "b")
            pg.text(20, 36, "Klimaprüfung von Gehäusen für Außenstationen", 12)
            y = pg.paras(20, 50, 170, [d.pick(DE_TECH, 4), d.pick(DE_TECH, 3)], 10.5, justify=True)
        elif n == 2:
            y = bar_chart(
                pg,
                30,
                26,
                150,
                70,
                [21, 35, 55, 70, 55, 35, 21],
                (200, 90, 30),
                "Abbildung 1: Temperaturprofil der Prüfung (°C)",
            )
            pg.paras(20, y + 4, 170, [d.pick(DE_TECH, 4)], 10.5, justify=True)
        else:
            y = pg.paras(20, 26, 170, [d.pick(DE_TECH, 3), d.pick(DE_ADMIN, 2)], 10.5, justify=True)
            signature(pg, 20, y + 10, ["Dipl.-Ing. Petra Lang", "Prüfstellenleiterin"])


@document(
    "memo",
    "neg-us",
    dpi=200,
    tags=("banner-unclassified", "portion-marks"),
    note="US memo with UNCLASSIFIED banners: level 0, no label",
)
def n_us_unclassified(d: Doc) -> None:
    pg = d.page()
    pg.text(105, 8, "UNCLASSIFIED", 12, "arial", "b", INK, "c")
    pg.text(105, 284, "UNCLASSIFIED", 12, "arial", "b", INK, "c")
    y = us_head(
        pg,
        "DEPARTMENT OF DEFENSE",
        "OFFICE OF THE UNDER SECRETARY FOR ACQUISITION",
        "WASHINGTON, DC 20301-3010",
    )
    pg.text(190, y, "September 3, 2026", 11, "times", "r", INK, "r")
    y += 10
    pg.text(20, y, "MEMORANDUM FOR ALL ACQUISITION WORKFORCE MEMBERS", 11, "times")
    pg.text(
        20, y + 9, "SUBJECT: (U) Annual Training Requirements for Fiscal Year 2027", 11, "times"
    )
    y += 20
    for mark, text in (
        ("(U)", d.pick(EN_GOV, 3)),
        ("(U)", d.pick(EN_GOV, 3)),
        ("(U)", d.pick(EN_GOV, 2)),
    ):
        y = pg.para(20, y, 170, f"{mark} {text}", 11, "times", justify=True) + 3.5
    signature(
        pg,
        115,
        y + 10,
        ["Michael T. Reyes", "Deputy Director, Acquisition Integration"],
        "times",
        11,
    )


@document("letter", "neg-letter", dpi=240, tags=("french", "letterhead", "plain"))
def n_fr_letter(d: Doc) -> None:
    pg = d.page()
    emblem_tricolore(pg, 20, 20, 13)
    pg.text(20, 30, "RÉPUBLIQUE", 8.5, "arial", "b")
    pg.text(20, 34, "FRANÇAISE", 8.5, "arial", "b")
    pg.text(70, 22, "PRÉFECTURE DU FINISTÈRE", 11, "arial", "b")
    pg.text(70, 27.5, "Direction des sécurités", 10, "arial")
    pg.text(190, 50, "Quimper, le 8 septembre 2026", 10.5, "arial", "r", INK, "r")
    pg.text(20, 62, "Objet : exercice de sécurité civile du 14 octobre 2026", 10.5, "arial", "b")
    y = pg.paras(
        20,
        74,
        170,
        [
            "Madame, Monsieur,",
            "J'ai l'honneur de vous informer qu'un exercice de sécurité civile se déroulera le 14 "
            "octobre 2026 sur le port de Brest. " + d.pick(FR_ADMIN, 2),
            d.pick(FR_ADMIN, 3),
            "Je vous prie d'agréer, Madame, Monsieur, l'expression de ma considération distinguée.",
        ],
        10.5,
        justify=True,
    )
    signature(
        pg, 120, y + 10, ["Pour le préfet,", "le directeur des sécurités", "", "Yann Le Goff"]
    )


# ================================================================ writing the set


LABEL_LEVELS = {
    "VS": 1,
    "VS (amtlich geheimgehalten)": 2,
    "VS-NfD": 1,
    "VS-VERTRAULICH": 2,
    "GEHEIM": 3,
    "STRENG GEHEIM": 4,
    "NATO RESTRICTED": 1,
    "NATO CONFIDENTIAL": 2,
    "NATO SECRET": 3,
    "COSMIC TOP SECRET": 4,
    "RESTREINT UE": 1,
    "CONFIDENTIEL UE": 2,
    "SECRET UE": 3,
    "TRÈS SECRET UE": 4,
    "AT EINGESCHRÄNKT": 1,
    "AT VERTRAULICH": 2,
    "AT GEHEIM": 3,
    "AT STRENG GEHEIM": 4,
    "CH INTERN": 1,
    "CH VERTRAULICH": 2,
    "CH GEHEIM": 3,
    "US CUI": 1,
    "US CONFIDENTIAL": 2,
    "US SECRET": 3,
    "US TOP SECRET": 4,
    "UK OFFICIAL-SENSITIVE": 1,
    "FR DIFFUSION RESTREINTE": 1,
}
TLPS = {"RED", "AMBER", "AMBER+STRICT", "GREEN", "CLEAR"}
COMPANY = {
    "STRENG VERTRAULICH",
    "VERTRAULICH",
    "INTERN",
    "CONFIDENTIAL",
    "INTERNAL",
    "GESCHÄFTSGEHEIMNIS",
}


def check(d: Doc) -> None:
    """The entry must agree with what the builder drew."""
    levels = [pg.level for pg in d.pages]
    problems = []
    if d.label is not None and LABEL_LEVELS.get(d.label) != d.level:
        problems.append(f"label {d.label} is not level {d.level}")
    if d.label is None and d.level:
        problems.append("level without label")
    if d.level and max(levels) != d.level:
        problems.append(f"pages {levels} do not reach level {d.level}")
    if not d.level and any(levels):
        problems.append(f"pages {levels} marked in an unclassified document")
    if d.tlp is not None and d.tlp not in TLPS:
        problems.append(f"tlp {d.tlp}")
    if d.company is not None and d.company not in COMPANY:
        problems.append(f"company {d.company}")
    carries = bool(d.label or d.tlp or d.company)
    if carries != bool(d.markings):
        problems.append(f"markings {d.markings} for a document that carries={carries}")
    if problems:
        raise SystemExit(f"{d.name} ({d.build.__name__}): " + "; ".join(problems))


def generate(out: Path) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    order = list(range(len(DOCS)))
    random.Random(20260923).shuffle(order)
    entries = []
    for number, index in enumerate(order, 1):
        d = DOCS[index]
        seed = zlib.crc32(d.build.__name__.encode())
        d.rng = random.Random(seed)
        d.noise = np.random.default_rng(seed)
        d.pages, d.markings, d.used = [], [], {}
        d.build(d)
        check(d)
        images, dpi = [], d.dpi
        for pg in d.pages:
            image, dpi = degrade(d, pg.img)
            images.append(image)
        file = f"h{number:03d}_{d.name}.{d.fmt}"
        save(d, images, dpi, out / file)
        entries.append(
            {
                "file": file,
                "category": d.category,
                "scheme": d.scheme,
                "level": d.level,
                "label": d.label,
                "tlp": d.tlp,
                "company": d.company,
                "pages": [pg.level for pg in d.pages],
                "mentions": d.mentions,
                "degradation": d.degradation,
                "text_layer": False,
                "markings": d.markings,
                "tags": list(d.tags),
                "note": d.note,
            }
        )
        print(f"{file:40} {d.category:16} L{d.level} {d.label or d.tlp or d.company or '-'}")
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("/tmp/vs/holdout"))
    parser.add_argument("--only", help="build only documents whose builder name contains this")
    args = parser.parse_args()
    if args.only:
        DOCS[:] = [d for d in DOCS if args.only in d.build.__name__]
    entries = generate(args.out)
    manifest = {"version": 1, "files": entries}
    (args.out / "ground_truth.json").write_text(
        json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    positives = sum(e["level"] >= 1 or bool(e["tlp"] or e["company"]) for e in entries)
    print(
        f"{len(entries)} files, {sum(len(e['pages']) for e in entries)} pages, "
        f"{positives} carrying a marking -> {args.out}"
    )
    for key in ("category", "scheme", "degradation"):
        counts = Counter(e[key] for e in entries)
        print(f"{key}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
