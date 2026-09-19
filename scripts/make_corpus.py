#!/usr/bin/env python3
"""Generate a torture-test corpus of documents with known ground truth.

Real archives are not clean: faxes, 1990s photocopies, technical drawings with
vertical labels, rotated scans, dark-mode screenshots, PDFs that are nothing but
a JPEG per page. This builds all of that synthetically, so every file comes with
the exact text it contains and error rates can be measured instead of guessed.

    python scripts/make_corpus.py --out /tmp/corpus            # ~400 files
    python scripts/make_corpus.py --out /tmp/corpus --small    # quick run

Needs Pillow and numpy (test-time only, never a runtime dependency of ocrust).
"""

from __future__ import annotations

import argparse
import json
import random
import zlib
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------- fonts

FONT_CANDIDATES = {
    "sans": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
    "sans-bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    "serif": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    ],
    "mono": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ],
    "jp": [
        "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
        "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    ],
}


def font_path(kind: str) -> str:
    for candidate in FONT_CANDIDATES[kind]:
        if Path(candidate).exists():
            return candidate
    raise SystemExit(f"no font found for {kind}; install fonts-dejavu")


def load_font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(kind), size)


# ---------------------------------------------------------------- content

#: Corpus languages the bundled recognizer cannot write, kept as evidence.
#:
#: Their pages are still generated and still scanned — a reader deserves to see
#: what an unsupported script actually looks like coming out — but they are held
#: out of the accuracy figures, which describe the languages ocrust claims.
UNSUPPORTED_SCRIPTS: frozenset[str] = frozenset({"el"})

TEXTS: dict[str, list[str]] = {
    "de": [
        "RECHNUNG Nr. 2026-04-1187",
        "Kleindienst Maschinenbau GmbH",
        "Industriestraße 14, 85748 Garching",
        "Lieferdatum: 17.03.2026",
        "Position 1: Führungsschiene FS-220, Stückzahl 12",
        "Position 2: Kugellager 6205-2RS, Stückzahl 48",
        "Zwischensumme: 4.812,50 EUR",
        "Umsatzsteuer 19 %: 914,38 EUR",
        "Gesamtbetrag: 5.726,88 EUR",
        "Zahlbar innerhalb 30 Tagen ohne Abzug.",
        "Grüße aus München, Ihre Buchhaltung",
    ],
    "en": [
        "PURCHASE ORDER 88-4471-C",
        "Northfield Instruments Ltd.",
        "42 Kingsway, Manchester M1 4BT",
        "Delivery date: 2026-03-17",
        "Item 1: Linear guide rail LG-220, qty 12",
        "Item 2: Ball bearing 6205-2RS, qty 48",
        "Subtotal: 4,812.50 GBP",
        "VAT at 20%: 962.50 GBP",
        "Total due: 5,775.00 GBP",
        "Payment terms: net 30 days.",
        "Thank you for your business.",
    ],
    "fr": [
        "FACTURE N 2026-04-1187",
        "Ateliers Mécaniques de Lyon",
        "14 rue de la Fonderie, 69007 Lyon",
        "Date de livraison : 17/03/2026",
        "Article 1 : rail de guidage FS-220, quantité 12",
        "Article 2 : roulement à billes 6205-2RS, quantité 48",
        "Sous-total : 4 812,50 EUR",
        "TVA 20 % : 962,50 EUR",
        "Total à payer : 5 775,00 EUR",
        "Déjà réglé par virement le 12 mars.",
        "Où être conseillé ? Appelez le 04 72 00 11 22.",
    ],
    "pl": [
        "FAKTURA nr 2026-04-1187",
        "Zakład Mechaniczny Wrocław",
        "ulica Świdnicka 42, 50-068 Wrocław",
        "Data dostawy: 17.03.2026",
        "Pozycja 1: szyna prowadząca FS-220, ilość 12",
        "Pozycja 2: łożysko kulkowe 6205-2RS, ilość 48",
        "Wartość netto: 4 812,50 PLN",
        "Podatek VAT 23 %: 1 106,88 PLN",
        "Do zapłaty: 5 919,38 PLN",
        "Zażółć gęślą jaźń, prosimy o terminową płatność.",
    ],
    "cs": [
        "FAKTURA číslo 2026-04-1187",
        "Strojírny Plzeň a.s.",
        "Škrétova 12, 301 00 Plzeň",
        "Datum dodání: 17.03.2026",
        "Položka 1: vodicí lišta FS-220, počet 12",
        "Položka 2: kuličkové ložisko 6205-2RS, počet 48",
        "Částka bez DPH: 4 812,50 CZK",
        "DPH 21 %: 1 010,63 CZK",
        "Celkem k úhradě: 5 823,13 CZK",
        "Příliš žluťoučký kůň úpěl ďábelské ódy.",
    ],
    "el": [
        "ΤΙΜΟΛΟΓΙΟ 2026-04-1187",
        "Μηχανουργεία Θεσσαλονίκης",
        "Οδός Εγνατίας 42, 54625",
        "Ημερομηνία παράδοσης: 17/03/2026",
        "Είδος 1: οδηγός FS-220, ποσότητα 12",
        "Σύνολο: 4.812,50 EUR",
        "ΦΠΑ 24 %: 1.155,00 EUR",
        "Πληρωτέο: 5.967,50 EUR",
    ],
    "ja": [
        "請求書 2026-04-1187",
        "株式会社ミュンヘン機械",
        "納品日 2026年3月17日",
        "品目 1 案内レール FS-220 数量 12",
        "合計金額 572688 円",
        "お支払い期限 30日",
    ],
}

DRAWING_LABELS = [
    "TITEL: Lagerbock LB-4711",
    "WERKSTOFF: S355J2",
    "MASSSTAB 1:2",
    "ZEICHNUNGS-NR 4711-02-A",
    "DATUM 14.02.1994",
    "GEZ. MUELLER",
    "GEPR. SCHMIDT",
    "TOLERANZ ISO 2768-mK",
    "OBERFLAECHE Rz 25",
    "BLATT 1 VON 3",
]

COLUMN_TEXT = [
    "Der Stadtrat tagte",
    "bis in die Nacht.",
    "Die neuen Radwege",
    "sollen 14 Kilometer",
    "lang werden.",
    "Die Kosten liegen",
    "bei 3,4 Millionen.",
    "Kritik kam von der",
    "Handelskammer.",
    "Der Ausbau beginnt",
    "im Mai 2026.",
    "Anwohner fordern",
    "mehr Parkplaetze.",
    "Ein Gutachten wird",
    "im April vorgelegt.",
    "Die Verwaltung",
    "prueft die Plaene.",
    "Der Beschluss gilt",
]

NEWSPAPER = [
    "Stadtrat beschliesst neue Radwege",
    "Die Sitzung dauerte bis in die Nacht.",
    "Insgesamt 14 Kilometer sollen entstehen.",
    "Die Kosten liegen bei 3,4 Millionen Euro.",
    "Kritik kam von der Handelskammer.",
    "Der Ausbau beginnt im Mai 2026.",
    "Anwohner fordern mehr Parkplaetze.",
    "Ein Gutachten wird im April vorgelegt.",
]


# ---------------------------------------------------------------- page drawing


@dataclass
class PageSpec:
    """One rendered page and the text it contains."""

    image: Image.Image
    lines: list[str] = field(default_factory=list)


def blank(width: int, height: int, shade: int = 255) -> Image.Image:
    return Image.new("RGB", (width, height), (shade, shade, shade))


def text_page(
    lines: list[str],
    *,
    dpi: int = 200,
    font_kind: str = "serif",
    point_size: int = 12,
    width_in: float = 8.27,
    height_in: float = 11.69,
    margin_in: float = 0.9,
    line_spacing: float = 1.6,
    columns: int = 1,
) -> PageSpec:
    """Renders `lines` onto an A4-ish page at `dpi`."""
    width = int(width_in * dpi)
    height = int(height_in * dpi)
    image = blank(width, height)
    draw = ImageDraw.Draw(image)
    px = max(8, int(point_size * dpi / 72))
    font = load_font(font_kind, px)

    margin = int(margin_in * dpi)
    column_width = (width - 2 * margin - (columns - 1) * margin // 2) // columns
    step = int(px * line_spacing)
    per_column = max(1, (height - 2 * margin) // step)
    if columns > 1:
        # Spread the lines over the columns. Filling each one to the bottom of
        # the page first leaves a page with fewer lines than that single-column,
        # which is not the layout that was asked for — and a layout nothing in
        # the corpus would then cover.
        per_column = min(per_column, -(-len(lines) // columns))

    # A line wider than its column would be drawn over the next one, and a
    # fixture like that measures nothing.
    too_wide = [line for line in lines if font.getlength(line) > column_width - px]
    if too_wide:
        raise ValueError(
            f"{len(too_wide)} line(s) do not fit a column of {column_width}px at "
            f"{point_size}pt: {too_wide[0]!r}"
        )

    drawn: list[str] = []
    for index, line in enumerate(lines):
        column = min(index // per_column, columns - 1)
        row = index - column * per_column
        x = margin + column * (column_width + margin // 2)
        y = margin + row * step
        if y + step > height - margin:
            continue
        draw.text((x, y), line, font=font, fill=(15, 15, 15))
        drawn.append(line)
    return PageSpec(image, drawn)


def drawing_page(dpi: int = 200, *, size_in: tuple[float, float] = (16.5, 11.7)) -> PageSpec:
    """A technical drawing: frame, title block, dimensions and vertical labels."""
    width, height = int(size_in[0] * dpi), int(size_in[1] * dpi)
    image = blank(width, height)
    draw = ImageDraw.Draw(image)
    ink = (20, 20, 20)
    small = load_font("sans", max(9, int(8 * dpi / 72)))
    tiny = load_font("sans", max(8, int(6.5 * dpi / 72)))

    # Sheet frame and title block.
    pad = int(0.3 * dpi)
    draw.rectangle([pad, pad, width - pad, height - pad], outline=ink, width=3)
    block_w, block_h = int(4.4 * dpi), int(1.9 * dpi)
    bx, by = width - pad - block_w, height - pad - block_h
    draw.rectangle([bx, by, width - pad, height - pad], outline=ink, width=3)

    # Ground truth is collected with the position each label is drawn at, then
    # sorted into reading order at the end: a drawing's title block sits at the
    # bottom right, so generation order is not reading order.
    placed: list[tuple[float, float, str]] = []
    row_h = block_h // 5
    for i, label in enumerate(DRAWING_LABELS[:10]):
        col = i % 2
        row = i // 2
        x = bx + 12 + col * (block_w // 2)
        y = by + 8 + row * row_h
        draw.text((x, y), label, font=tiny, fill=ink)
        placed.append((y, x, label))
        if col == 0 and row > 0:
            draw.line([bx, y - 6, width - pad, y - 6], fill=ink, width=1)

    # A part: circles, hatching and dimension lines with labels.
    cx, cy = int(width * 0.34), int(height * 0.42)
    draw.rectangle([cx - 320, cy - 200, cx + 320, cy + 200], outline=ink, width=4)
    for r in (70, 110):
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ink, width=3)
    for i in range(-300, 301, 24):
        draw.line([cx + i, cy + 200, cx + i + 40, cy + 260], fill=(90, 90, 90), width=1)

    dims = ["640", "400", "R110", "4x M12", "22,5 +0,1", "Ø 70 H7"]
    positions = [
        (cx - 40, cy - 250),
        (cx + 350, cy - 20),
        (cx + 120, cy + 120),
        (cx - 300, cy + 230),
        (cx - 60, cy + 250),
        (cx - 150, cy - 140),
    ]
    for label, (x, y) in zip(dims, positions):
        draw.text((x, y), label, font=small, fill=ink)
        placed.append((float(y), float(x), label))
    draw.line([cx - 320, cy - 240, cx + 320, cy - 240], fill=ink, width=2)
    draw.line([cx + 340, cy - 200, cx + 340, cy + 200], fill=ink, width=2)

    # Vertical label, as drawings always have.
    vertical = "ANSICHT A-A GESCHNITTEN"
    strip = Image.new("RGB", (int(3.4 * dpi), int(0.4 * dpi)), (255, 255, 255))
    ImageDraw.Draw(strip).text((4, 2), vertical, font=small, fill=ink)
    image.paste(strip.rotate(90, expand=True), (int(0.45 * dpi), int(height * 0.28)))
    placed.append((float(height) * 0.28, float(0.45 * dpi), vertical))

    # Sort into bands so labels on roughly the same height read left to right.
    band = max(1.0, 0.25 * dpi)
    placed.sort(key=lambda item: (round(item[0] / band), item[1]))
    return PageSpec(image, [label for _, _, label in placed])


def form_page(dpi: int = 200) -> PageSpec:
    """A ruled form with a table, the layout most invoices actually have."""
    spec = text_page(
        TEXTS["de"][:4],
        dpi=dpi,
        font_kind="sans",
        point_size=11,
        line_spacing=1.8,
    )
    image = spec.image
    draw = ImageDraw.Draw(image)
    font = load_font("sans", max(9, int(9 * dpi / 72)))
    ink = (25, 25, 25)

    top = int(3.2 * dpi)
    left = int(0.9 * dpi)
    right = image.width - int(0.9 * dpi)
    row_h = int(0.34 * dpi)
    headers = ["Pos", "Artikel", "Menge", "Einzelpreis", "Gesamt"]
    rows = [
        ["1", "Führungsschiene FS-220", "12", "184,50", "2.214,00"],
        ["2", "Kugellager 6205-2RS", "48", "12,40", "595,20"],
        ["3", "Dichtring DIN 3760", "120", "0,85", "102,00"],
        ["4", "Schraube M12x40 8.8", "480", "0,31", "148,80"],
    ]
    columns = [
        left,
        left + int(0.6 * dpi),
        left + int(4.0 * dpi),
        left + int(5.0 * dpi),
        right - int(1.2 * dpi),
    ]

    for r, row in enumerate([headers, *rows]):
        y = top + r * row_h
        draw.line([left, y - 6, right, y - 6], fill=ink, width=2 if r == 0 else 1)
        for x, cell in zip(columns, row):
            draw.text((x + 6, y), cell, font=font, fill=ink)
            spec.lines.append(cell)
    draw.line(
        [left, top + (len(rows) + 1) * row_h - 6, right, top + (len(rows) + 1) * row_h - 6],
        fill=ink,
        width=2,
    )
    for x in columns[1:]:
        draw.line([x, top - 6, x, top + (len(rows) + 1) * row_h - 6], fill=ink, width=1)
    return spec


def price_list_page(dpi: int = 200) -> PageSpec:
    """A page that is nothing but a price list, with no rules to go by.

    The hardest shape for a layout that works from white space alone: there is no
    prose to measure a word space against, so the gaps between the columns are
    almost all the gaps there are.
    """
    width, height = int(8.27 * dpi), int(11.69 * dpi)
    image = blank(width, height)
    draw = ImageDraw.Draw(image)
    font = load_font("sans", max(9, int(11 * dpi / 72)))
    ink = (18, 18, 18)

    rows = [
        ["Artikel", "Menge", "Preis", "Summe"],
        ["Schraube M4", "100", "0,12", "12,00"],
        ["Mutter M4", "100", "0,08", "8,00"],
        ["Scheibe A4", "200", "0,03", "6,00"],
        ["Feder D12", "50", "0,45", "22,50"],
        ["Bolzen M8", "25", "1,20", "30,00"],
        ["Splint 3x20", "75", "0,15", "11,25"],
        ["Lager 6002", "10", "3,40", "34,00"],
        ["Dichtung 40", "60", "0,95", "57,00"],
    ]
    columns = [int(0.9 * dpi), int(2.2 * dpi), int(2.9 * dpi), int(3.6 * dpi)]
    top = int(1.2 * dpi)
    row_h = int(0.42 * dpi)

    lines: list[str] = []
    for r, row in enumerate(rows):
        y = top + r * row_h
        for x, cell in zip(columns, row):
            draw.text((x, y), cell, font=font, fill=ink)
            lines.append(cell)
    return PageSpec(image, lines)


def receipt_page(dpi: int = 200) -> PageSpec:
    """A narrow thermal receipt: tall, monospaced, faint."""
    lines = [
        "SUPERMARKT AM PLATZ",
        "Bahnhofstr. 9, 80335",
        "--------------------",
        "Milch 1L        1,19",
        "Brot 500g       2,49",
        "Kaffee 500g     6,99",
        "Butter 250g     2,29",
        "Apfel 1kg       3,49",
        "--------------------",
        "SUMME          16,45",
        "MwSt 7%         1,08",
        "EC-Karte       16,45",
        "17.03.2026 10:42",
        "VIELEN DANK",
    ]
    return text_page(
        lines,
        dpi=dpi,
        font_kind="mono",
        point_size=10,
        width_in=3.1,
        height_in=7.0,
        margin_in=0.25,
        line_spacing=1.9,
    )


# ---------------------------------------------------------------- ageing


def to_array(image: Image.Image) -> np.ndarray:
    return np.asarray(image, dtype=np.float32)


def to_image(array: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), "RGB")


def age(image: Image.Image, rng: random.Random, *, level: float = 1.0) -> Image.Image:
    """Applies photocopier and shelf-life damage."""
    out = image
    # Faded ink and reduced contrast.
    array = to_array(out)
    array = 255 - (255 - array) * (1.0 - 0.28 * level)
    # Yellowed paper.
    array[:, :, 2] *= 1.0 - 0.10 * level
    array[:, :, 1] *= 1.0 - 0.03 * level
    out = to_image(array)
    # Blur from a soft scanner, then grain.
    out = out.filter(ImageFilter.GaussianBlur(0.4 + 0.7 * level))
    array = to_array(out)
    array += np.random.default_rng(rng.randrange(1 << 30)).normal(0, 6 + 8 * level, array.shape)
    out = to_image(array)
    # Coffee stains and speckles.
    draw = ImageDraw.Draw(out, "RGBA")
    for _ in range(int(3 * level) + 1):
        r = rng.randint(out.width // 14, out.width // 6)
        x, y = rng.randrange(out.width), rng.randrange(out.height)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(150, 120, 60, rng.randint(14, 34)))
    for _ in range(int(900 * level)):
        x, y = rng.randrange(out.width), rng.randrange(out.height)
        s = rng.randint(1, 2)
        draw.ellipse([x, y, x + s, y + s], fill=(40, 40, 40, rng.randint(90, 200)))
    return out


def fax(image: Image.Image, rng: random.Random) -> Image.Image:
    """1-bit fax look: dithered, streaked, low resolution."""
    small = image.resize((image.width // 2, image.height // 2), Image.LANCZOS)
    bilevel = small.convert("L").convert("1", dither=Image.FLOYDSTEINBERG).convert("RGB")
    draw = ImageDraw.Draw(bilevel)
    for _ in range(rng.randint(2, 6)):
        y = rng.randrange(bilevel.height)
        draw.line([0, y, bilevel.width, y], fill=(255, 255, 255), width=rng.randint(1, 3))
    for _ in range(rng.randint(1, 3)):
        x = rng.randrange(bilevel.width)
        draw.line([x, 0, x, bilevel.height], fill=(0, 0, 0), width=1)
    return bilevel


def bleed_through(image: Image.Image) -> Image.Image:
    """Ghost of the reverse side, as thin paper always shows."""
    ghost = image.transpose(Image.FLIP_LEFT_RIGHT).filter(ImageFilter.GaussianBlur(1.6))
    return Image.blend(image, ghost, 0.12)


def skew(image: Image.Image, degrees: float) -> Image.Image:
    return image.rotate(degrees, resample=Image.BICUBIC, expand=True, fillcolor=(252, 250, 246))


def dark_mode(image: Image.Image) -> Image.Image:
    array = 255 - to_array(image)
    array[:, :, 0] *= 0.92
    return to_image(array)


def jpeg_cycles(image: Image.Image, rounds: int = 3, quality: int = 45) -> Image.Image:
    """Repeated recompression, the way documents degrade in email chains."""
    out = image
    for _ in range(rounds):
        buffer = BytesIO()
        out.save(buffer, "JPEG", quality=quality)
        buffer.seek(0)
        out = Image.open(buffer).convert("RGB")
    return out


# ---------------------------------------------------------------- PDF writers


def _pdf_from_objects(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def image_only_pdf(
    pages: list[Image.Image], *, dpi: int = 200, quality: int = 70, rotate: int = 0
) -> bytes:
    """A PDF that is nothing but one JPEG per page — a plain scan, no text layer."""
    objects: list[bytes] = [b"", b""]  # catalog and page tree are filled in later
    page_ids: list[int] = []
    for image in pages:
        buffer = BytesIO()
        image.save(buffer, "JPEG", quality=quality)
        jpeg = buffer.getvalue()
        w_pt = image.width * 72.0 / dpi
        h_pt = image.height * 72.0 / dpi

        objects.append(
            f"<< /Type /XObject /Subtype /Image /Width {image.width} /Height {image.height} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
            f"/Length {len(jpeg)} >>\nstream\n".encode()
            + jpeg
            + b"\nendstream"
        )
        image_id = len(objects)
        content = f"q\n{w_pt:.2f} 0 0 {h_pt:.2f} 0 0 cm\n/Im0 Do\nQ\n".encode()
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"
        )
        content_id = len(objects)
        rotate_entry = f" /Rotate {rotate}" if rotate else ""
        objects.append(
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {w_pt:.2f} {h_pt:.2f}]{rotate_entry} "
            f"/Resources << /XObject << /Im0 {image_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>".encode()
        )
        page_ids.append(len(objects))

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    return _pdf_from_objects(objects)


def born_digital_pdf(lines: list[str], *, pages: int = 1) -> bytes:
    """A PDF with a real text layer, for testing that such pages are skipped."""

    def literal(text: str) -> str:
        out = []
        for ch in text:
            if ch in "()\\":
                out.append("\\" + ch)
            elif ord(ch) < 0x80:
                out.append(ch)
            elif 0xA0 <= ord(ch) <= 0xFF:
                out.append(f"\\{ord(ch):03o}")
            else:
                out.append("?")
        return "".join(out)

    body = ["BT"]
    y = 740
    for line in lines:
        body.append(f"/F1 12 Tf 1 0 0 1 60 {y} Tm ({literal(line)}) Tj")
        y -= 26
    body.append("ET")
    stream = "\n".join(body).encode()

    objects: list[bytes] = [b"", b""]
    page_ids = []
    for _ in range(pages):
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
        content_id = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {{FONT}} 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        page_ids.append(len(objects))
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    font_id = len(objects)
    objects = [o.replace(b"{FONT}", str(font_id).encode()) for o in objects]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    return _pdf_from_objects(objects)


# ---------------------------------------------------------------- corpus


class Corpus:
    def __init__(self, root: Path, seed: int = 20260317) -> None:
        self.root = root
        self.rng = random.Random(seed)
        self.truth: dict[str, dict] = {}

    def add(
        self,
        relative: str,
        data: bytes,
        *,
        category: str,
        lines: list[str],
        language: str = "de",
        pages: int = 1,
        notes: str = "",
        expect_error: bool = False,
        unsupported_script: bool = False,
    ) -> None:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.truth[relative] = {
            "category": category,
            "language": language,
            "pages": pages,
            "lines": lines,
            "notes": notes,
            "expect_error": expect_error,
            "unsupported_script": unsupported_script,
            "bytes": len(data),
        }

    def save_image(self, relative: str, image: Image.Image, **kwargs) -> bytes:
        buffer = BytesIO()
        suffix = Path(relative).suffix.lower().lstrip(".")
        formats = {
            "jpg": "JPEG",
            "jpeg": "JPEG",
            "png": "PNG",
            "webp": "WEBP",
            "bmp": "BMP",
            "gif": "GIF",
            "tif": "TIFF",
            "tiff": "TIFF",
            "ppm": "PPM",
            "tga": "TGA",
        }
        image.save(buffer, formats[suffix], **kwargs)
        return buffer.getvalue()

    def write_manifest(self) -> None:
        path = self.root / "ground_truth.json"
        path.write_text(json.dumps(self.truth, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\n{len(self.truth)} files, manifest -> {path}")


def build(root: Path, small: bool = False) -> Corpus:
    corpus = Corpus(root)
    rng = corpus.rng
    scale = 1 if small else 2

    print("clean pages ...")
    for language, lines in TEXTS.items():
        kind = "jp" if language == "ja" else "serif"
        unsupported = language in UNSUPPORTED_SCRIPTS
        for dpi in (200, 300) if not small else (200,):
            spec = text_page(lines, dpi=dpi, font_kind=kind, point_size=12)
            name = f"clean/{language}_{dpi}dpi.png"
            corpus.add(
                name,
                corpus.save_image(name, spec.image),
                category="clean",
                lines=spec.lines,
                language=language,
                notes=f"crisp render at {dpi} dpi",
                unsupported_script=unsupported,
            )
            pdf = image_only_pdf([spec.image], dpi=dpi)
            corpus.add(
                f"clean/{language}_{dpi}dpi.pdf",
                pdf,
                category="clean-pdf",
                lines=spec.lines,
                language=language,
                notes="image-only PDF, no text layer",
                unsupported_script=unsupported,
            )

    print("aged and damaged ...")
    for index in range(4 * scale):
        level = 0.4 + 0.25 * (index % 4)
        language = ["de", "en", "fr", "pl"][index % 4]
        spec = text_page(TEXTS[language], dpi=200, font_kind="serif", point_size=12)
        damaged = age(spec.image, rng, level=level)
        if index % 2:
            damaged = bleed_through(damaged)
        if index % 3 == 0:
            damaged = jpeg_cycles(damaged, rounds=3, quality=40)
        name = f"aged/aged_{index:02d}_{language}.jpg"
        corpus.add(
            name,
            corpus.save_image(name, damaged, quality=70),
            category="aged",
            lines=spec.lines,
            language=language,
            notes=f"ageing level {level:.2f}",
        )
        corpus.add(
            f"aged/aged_{index:02d}_{language}.pdf",
            image_only_pdf([damaged], dpi=200, quality=60),
            category="aged-pdf",
            lines=spec.lines,
            language=language,
            notes=f"scanned-looking PDF, ageing level {level:.2f}",
        )

    print("fax and photocopies ...")
    for index in range(2 * scale):
        language = ["de", "en"][index % 2]
        spec = text_page(TEXTS[language], dpi=200, font_kind="sans", point_size=13)
        faxed = fax(age(spec.image, rng, level=0.5), rng)
        name = f"fax/fax_{index:02d}_{language}.tiff"
        corpus.add(
            name,
            corpus.save_image(name, faxed.copy()),
            category="fax",
            lines=spec.lines,
            language=language,
            notes="1-bit dithered fax at half resolution",
        )
        corpus.add(
            f"fax/fax_{index:02d}_{language}.pdf",
            image_only_pdf([faxed], dpi=100, quality=50),
            category="fax-pdf",
            lines=spec.lines,
            language=language,
            notes="fax wrapped in a PDF",
        )

    print("technical drawings ...")
    for index in range(2 * scale):
        spec = drawing_page(dpi=200 if index % 2 == 0 else 150)
        image = spec.image
        if index % 2:
            image = age(image, rng, level=0.5)
        name = f"drawings/drawing_{index:02d}.png"
        corpus.add(
            name,
            corpus.save_image(name, image),
            category="drawing",
            lines=spec.lines,
            notes="A3 landscape with title block, dimensions and vertical label",
        )
        corpus.add(
            f"drawings/drawing_{index:02d}.pdf",
            image_only_pdf([image], dpi=200 if index % 2 == 0 else 150),
            category="drawing-pdf",
            lines=spec.lines,
            notes="drawing as an image-only PDF",
        )

    print("forms, receipts, newspapers ...")
    for index in range(2 * scale):
        spec = form_page(dpi=200)
        image = age(spec.image, rng, level=0.3) if index % 2 else spec.image
        name = f"forms/form_{index:02d}.png"
        corpus.add(
            name,
            corpus.save_image(name, image),
            category="form",
            lines=spec.lines,
            notes="ruled table with five columns",
        )
        spec = receipt_page(dpi=200)
        faint = to_image(255 - (255 - to_array(spec.image)) * 0.55)
        name = f"receipts/receipt_{index:02d}.png"
        corpus.add(
            name,
            corpus.save_image(name, faint),
            category="receipt",
            lines=spec.lines,
            notes="narrow thermal receipt, faint print",
        )
        spec = text_page(COLUMN_TEXT, dpi=200, font_kind="serif", point_size=10, columns=3)
        name = f"newspaper/news_{index:02d}.png"
        corpus.add(
            name,
            corpus.save_image(name, spec.image),
            category="newspaper",
            lines=spec.lines,
            notes="three-column layout",
        )
        # Two columns set on one baseline grid, widely leaded: every pair of
        # lines has white space between it, so reading the page by rows is the
        # mistake to catch here.
        spec = text_page(
            COLUMN_TEXT,
            dpi=200,
            font_kind="serif",
            point_size=11,
            line_spacing=2.1,
            columns=2,
        )
        name = f"newspaper/twocol_{index:02d}.png"
        corpus.add(
            name,
            corpus.save_image(name, spec.image),
            category="newspaper",
            lines=spec.lines,
            notes="two columns on one baseline grid",
        )
        spec = price_list_page(dpi=200)
        name = f"pricelists/pricelist_{index:02d}.png"
        corpus.add(
            name,
            corpus.save_image(name, spec.image),
            category="pricelist",
            lines=spec.lines,
            notes="a page that is nothing but an unruled price list",
        )
        corpus.add(
            f"pricelists/pricelist_{index:02d}.pdf",
            image_only_pdf([spec.image], dpi=200),
            category="pricelist-pdf",
            lines=spec.lines,
            notes="the same price list as an image-only PDF",
        )

    print("rotated and skewed ...")
    for degrees in [-7, -2.5, 1.5, 6] if not small else [-4, 3]:
        spec = text_page(TEXTS["de"], dpi=200, font_kind="sans", point_size=12)
        tilted = skew(spec.image, degrees)
        name = f"rotated/skew_{degrees:+.1f}.png".replace("+", "p").replace("-", "m")
        corpus.add(
            name,
            corpus.save_image(name, tilted),
            category="skewed",
            lines=spec.lines,
            notes=f"rotated by {degrees} degrees",
        )
    for rotate in (90, 180, 270):
        spec = text_page(TEXTS["de"], dpi=200, font_kind="sans", point_size=12)
        corpus.add(
            f"rotated/pdf_rotate_{rotate}.pdf",
            image_only_pdf([spec.image], dpi=200, rotate=rotate),
            category="rotated-pdf",
            lines=spec.lines,
            notes=f"/Rotate {rotate} in the page dictionary",
        )

    print("dark mode and inverted ...")
    spec = text_page(TEXTS["en"], dpi=200, font_kind="sans", point_size=12)
    inverted = dark_mode(spec.image)
    corpus.add(
        "screenshots/dark_mode.png",
        corpus.save_image("screenshots/dark_mode.png", inverted),
        category="inverted",
        lines=spec.lines,
        language="en",
        notes="light text on dark background",
    )

    print("extreme sizes ...")
    spec = drawing_page(dpi=300, size_in=(33.1, 23.4))  # A0
    corpus.add(
        "extremes/a0_drawing_300dpi.png",
        corpus.save_image("extremes/a0_drawing_300dpi.png", spec.image),
        category="huge",
        lines=spec.lines,
        notes="A0 at 300 dpi, ~9900x7000 px",
    )
    spec = text_page(TEXTS["de"][:5], dpi=60, font_kind="sans", point_size=11)
    corpus.add(
        "extremes/thumbnail_60dpi.png",
        corpus.save_image("extremes/thumbnail_60dpi.png", spec.image),
        category="tiny",
        lines=spec.lines,
        notes="60 dpi thumbnail, tiny glyphs",
    )
    spec = text_page(
        TEXTS["de"] * 4,
        dpi=200,
        font_kind="mono",
        point_size=9,
        # Wide enough for the longest line: at three inches the invoice lines ran
        # off the edge of the strip, and the ground truth claimed text that was
        # not in the image.
        width_in=4.2,
        height_in=24.0,
        margin_in=0.2,
    )
    corpus.add(
        "extremes/long_receipt.png",
        corpus.save_image("extremes/long_receipt.png", spec.image),
        category="long",
        lines=spec.lines,
        notes="4.2 x 24 inch strip",
    )

    print("multi-page documents ...")
    page_counts = [5, 12] if small else [5, 12, 30]
    for count in page_counts:
        pages, all_lines = [], []
        for page_index in range(count):
            language = ["de", "en", "fr"][page_index % 3]
            spec = text_page(
                [f"Seite {page_index + 1} von {count}", *TEXTS[language][:8]],
                dpi=150,
                font_kind="serif",
                point_size=12,
            )
            image = age(spec.image, rng, level=0.35) if page_index % 3 == 0 else spec.image
            pages.append(image)
            all_lines.extend(spec.lines)
        corpus.add(
            f"multipage/scan_{count}p.pdf",
            image_only_pdf(pages, dpi=150, quality=60),
            category="multipage-pdf",
            lines=all_lines,
            pages=count,
            notes=f"{count}-page image-only scan",
        )
        # Pillow cannot write a multi-page TIFF into a file-like object, so it
        # goes through a temporary file.
        # Pillow keeps per-image `encoderinfo` from earlier saves (these pages
        # were written as JPEG into the PDF above), and it leaks into the TIFF
        # encoder. Copies start clean.
        fresh = [page.copy() for page in pages]
        temporary = corpus.root / f".multipage_{count}.tiff"
        fresh[0].save(temporary, "TIFF", save_all=True, append_images=fresh[1:])
        corpus.add(
            f"multipage/scan_{count}p.tiff",
            temporary.read_bytes(),
            category="multipage-tiff",
            lines=all_lines,
            pages=count,
            notes=f"{count}-page TIFF",
        )
        temporary.unlink()

    print("born-digital PDFs ...")
    corpus.add(
        "borndigital/invoice_text.pdf",
        born_digital_pdf(TEXTS["de"]),
        category="born-digital",
        lines=TEXTS["de"],
        notes="real text layer, must be skipped",
    )
    corpus.add(
        "borndigital/report_text_3p.pdf",
        born_digital_pdf(TEXTS["en"], pages=3),
        category="born-digital",
        # Every page carries the same text, so the ground truth repeats too —
        # otherwise the comparison penalizes reading all three pages.
        lines=TEXTS["en"] * 3,
        language="en",
        pages=3,
        notes="three pages with a text layer",
    )

    print("every raster format ...")
    spec = text_page(TEXTS["de"][:8], dpi=200, font_kind="sans", point_size=12)
    for suffix, kwargs in {
        "png": {},
        "jpg": {"quality": 85},
        "webp": {"quality": 85},
        "bmp": {},
        "gif": {},
        "ppm": {},
        "tga": {},
        "tiff": {},
    }.items():
        name = f"formats/page.{suffix}"
        corpus.add(
            name,
            corpus.save_image(name, spec.image, **kwargs),
            category="format",
            lines=spec.lines,
            notes=f"same page as {suffix.upper()}",
        )

    print("broken and hostile inputs ...")
    good = corpus.save_image("formats/page.png", spec.image)
    corpus.add(
        "broken/truncated.png",
        good[: len(good) // 3],
        category="broken",
        lines=[],
        notes="truncated PNG",
        expect_error=True,
    )
    corpus.add(
        "broken/empty.pdf", b"", category="broken", lines=[], notes="zero bytes", expect_error=True
    )
    corpus.add(
        "broken/garbage.pdf",
        bytes(rng.randrange(256) for _ in range(4096)),
        category="broken",
        lines=[],
        notes="random bytes with a PDF extension",
        expect_error=True,
    )
    corpus.add(
        "broken/png_named_pdf.pdf",
        good,
        category="broken",
        lines=spec.lines,
        notes="PNG with a .pdf extension: content sniffing must win",
    )
    corpus.add(
        "broken/header_only.pdf",
        b"%PDF-1.4\n",
        category="broken",
        lines=[],
        notes="PDF header without objects",
        expect_error=True,
    )
    corpus.add(
        "broken/blank_page.png",
        corpus.save_image("broken/blank_page.png", blank(1200, 1600)),
        category="blank",
        lines=[],
        notes="empty white page, no text at all",
    )
    corpus.add(
        "broken/deflate_stream.bin",
        zlib.compress(b"not an image" * 100),
        category="broken",
        lines=[],
        notes="compressed data, unknown format",
        expect_error=True,
    )

    corpus.write_manifest()
    return corpus


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("/tmp/ocrust-corpus"))
    parser.add_argument("--small", action="store_true", help="fewer variants, for a quick run")
    args = parser.parse_args()

    if args.out.exists() and any(args.out.iterdir()):
        raise SystemExit(f"{args.out} is not empty; remove it first")
    args.out.mkdir(parents=True, exist_ok=True)
    corpus = build(args.out, small=args.small)

    total_bytes = sum(entry["bytes"] for entry in corpus.truth.values())
    total_pages = sum(entry["pages"] for entry in corpus.truth.values())
    print(f"{total_pages} pages, {total_bytes / 1e6:.1f} MB in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
