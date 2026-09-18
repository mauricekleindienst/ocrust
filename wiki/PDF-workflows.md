# PDF workflows

"Put an OCR layer on a PDF" means two different jobs, and confusing them is how
archives get ruined.

| Goal | Command | What happens to the pages |
|---|---|---|
| A scanned PDF should become searchable | `ocrust ocr scan.pdf` | Kept exactly as they are; an invisible text layer is added |
| An image should become a PDF | `ocrust pdf photo.jpg` | A new PDF is built: JPEG plus an invisible text layer |

## Making an existing PDF searchable

```bash
ocrust ocr scan.pdf                 # -> scan.ocr.pdf
ocrust ocr scan.pdf -o out.pdf --dpi 300 --lang de
```

```python
import ocrust

ocr = ocrust.Ocr(lang="de")
pdf, report = ocr.ocr_pdf("scan.pdf")
open("scan.ocr.pdf", "wb").write(pdf)

print(report)
# {'pages': 12, 'pages_with_layer': 9, 'pages_skipped': 3, 'lines': 214,
#  'unmappable_chars': 0}
```

### What is preserved

Nothing in the original is re-encoded. Per page, exactly two objects are added: a
content stream holding the invisible text, and one shared base-14 font. Images,
vector content, compression filters, annotations, bookmarks, form fields and
metadata come out byte-identical. The visible page cannot change, because the
text is drawn in rendering mode 3 — no glyphs, only selectable positions.

This is the archival path. `ocrust pdf` would rasterize the document and throw
away everything that was not a picture.

### What it gets right

- **Pages that already have text** are skipped, so running the tool across a
  mixed archive is safe. Detection looks for real text operators (`Tj`, `TJ`,
  `'`, `"`) in the page's content streams. `--force` overrides it.
- **Rotation.** `/Rotate 90`, `180` and `270` are everywhere in scanner output.
  Viewers apply the rotation, so OCR coordinates live in display space; the layer
  carries a `cm` matrix mapping them back to page space. All four quarter turns
  are unit-tested by mapping display corners onto page corners.
- **Inherited resources.** `/Resources` often lives on an ancestor page-tree
  node. Writing a fresh dictionary onto the page would shadow it and break the
  page's own content, so the inherited one is copied and extended.
- **Rotated lines.** A line at an angle gets a rotated text matrix, so selection
  follows the baseline instead of a horizontal box.
- **Geometry.** The overlay path deliberately disables deskew and rescaling for
  the pages it OCRs: the text must line up with the *original* page, not with a
  straightened copy of it.

### Deciding before you write

```bash
ocrust ocr archive.pdf --dry-run
```

```console
page 1: 595x842 pt -> ocr
page 2: 595x842 pt, rotated 90deg -> ocr
page 3: 595x842 pt -> skip (has text)

2 of 3 page(s) would get a text layer
```

```python
for page in ocr.plan_pdf("archive.pdf"):
    print(page)
# {'index': 0, 'width': 595.44, 'height': 841.68, 'rotate': 90, 'needs_ocr': True}
```

`plan_pdf` reads the page tree only — no OCR, so it is instant even on a
thousand-page file. Useful for planning a batch: count the pages that actually
need work before spending the CPU.

### The text layer's character set

The layer uses a WinAnsi (CP1252) base-14 font, which covers Western European
text: German umlauts, French accents, Scandinavian letters, the euro sign.
Characters outside it — CJK, Cyrillic, Greek — are counted in
`unmappable_chars` and written as `?` **in the invisible layer only**. The
recognized text itself is complete in every other output:

```bash
ocrust scan scan.pdf -f json -o scan.json   # full text, whatever the script
```

So `unmappable_chars: 0` in the report means the searchable layer is faithful; a
non-zero count means search will miss those characters in that PDF. Embedding a
Unicode font is on the [[Roadmap]].

### Batches

```python
from pathlib import Path
import ocrust

ocr = ocrust.Ocr(lang="de", page_workers=4)

for path in Path("archive").rglob("*.pdf"):
    out = path.with_suffix(".ocr.pdf")
    if out.exists():
        continue
    try:
        pdf, report = ocr.ocr_pdf(path)
    except (IOError, ValueError) as exc:
        print(f"{path}: {exc}")
        continue
    if report["pages_with_layer"]:
        out.write_bytes(pdf)
    print(f"{path}: {report['pages_with_layer']}/{report['pages']} layered")
```

Or from the shell, four files at a time:

```bash
find archive -name '*.pdf' -print0 | xargs -0 -n1 -P4 ocrust ocr
```

## Building a PDF from images

```bash
ocrust pdf photo.jpg -o photo.pdf --dpi 300 --quality 90
```

```python
open("photo.pdf", "wb").write(ocrust.searchable_pdf("photo.jpg", jpeg_quality=90))
```

Each page becomes a JPEG at the chosen quality with the text layer on top. The
writer is self-contained — no PDF library is involved in this direction.

| Option | Effect |
|---|---|
| `--quality` / `jpeg_quality` | 80 by default; 90+ for documents you will reprint, 60 for bulk storage |
| `--dpi` / `dpi` | the resolution the image is assumed to be, which sets the page size in points |

## Archiving as TIFF

```bash
ocrust tiff scan.pdf --gray --sidecar text -o scan.tiff
```

A deskewed, upright multi-page TIFF plus the recognized text beside it — the
shape most document archives ask for. `--gray` roughly halves the file for
bitonal scans.

```python
data, doc = ocrust.to_tiff("scan.pdf", gray=True)
open("scan.tiff", "wb").write(data)
open("scan.txt", "w").write(doc.text)
```

## Which DPI?

Rasterization resolution is the one knob that matters for PDF accuracy. Measured
over clean, aged and fax PDFs ([[Evaluation]]):

| dpi | mean CER | word recall | time |
|---:|---:|---:|---:|
| 100 | 0.003 | 0.969 | 4.2 s |
| 150 | 0.004 | 0.964 | 4.2 s |
| 200 (default) | 0.007 | 0.910 | 4.4 s |
| 300 | 0.002 | 0.980 | 5.2 s |

The differences are small and not monotonic, because the detector resizes the
page anyway. The practical rule: leave it at 200, raise it to 300 when the print
is genuinely small (footnotes, drawing labels), and drop to 100 when throughput
matters more than the last half percent.
