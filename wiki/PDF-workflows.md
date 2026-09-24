# PDF workflows

"Put an OCR layer on a PDF" means two different jobs, and confusing them is how
archives get ruined.

| Goal | Command | What happens to the pages |
|---|---|---|
| A scanned PDF should become searchable | `ocrust ocr scan.pdf` | Kept exactly as they are; an invisible text layer is added |
| An image should become a PDF | `ocrust pdf photo.jpg` | A new PDF is built: JPEG plus an invisible text layer |
| Many files should become one PDF | `ocrust pdf archive/ -o all.pdf` | Every readable input, in order, as the pages of one searchable PDF |

## Converting anything into one PDF

```bash
ocrust pdf archive/ -o archive.pdf                  # a folder, recursively
ocrust pdf fax.tiff photo.jpg old.pdf -o one.pdf    # mixed kinds, in this order
```

This is the converter. Anything the reader opens goes in — images in any offered
format, multi-page TIFFs, existing PDFs — and one searchable PDF comes out, page
sizes taken from each input's own pixels rather than forced onto a common sheet.
A folder is walked recursively and sorted, so the result is the same twice
running.

Pages are compressed as they are scanned, so a hundred files cost the memory of
one. What it does *not* do is preserve an input PDF's vector content: every page
is re-rendered, which is the price of putting different formats on equal footing.
For a PDF whose pages must stay exactly as they are, use `ocrust ocr` below.

In Python:

```python
import ocrust

data, pages = ocrust.searchable_pdf_many(["fax.tiff", "photo.jpg", "old.pdf"])
Path("one.pdf").write_bytes(data)
print(pages, "pages")
```

A folder or a glob can be passed instead of a list of files.

## Password-protected PDFs

```bash
ocrust ocr statement.pdf --password geheim
OCRUST_PASSWORD=geheim ocrust pdf statements/ -o all.pdf
```

A PDF protected by an owner password alone — printing or copying restricted, but
readable by anyone — opens without one, and that covers most bank statements and
e-invoices. A PDF that needs a password to *read* is an error that says so until
the password is given, on every command.

The output of `ocrust ocr` is written without the password: the text layer is
added to the document as it reads once opened, and the command says so. If the
file has to stay locked, encrypt it again afterwards. `ocrust pdf` builds a new
document, so its output was never going to carry the old protection.

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

### Every script, not just Western Europe

Western European text uses a WinAnsi (CP1252) base-14 font: German umlauts,
French accents, Scandinavian letters, the euro sign. A line with anything else —
CJK, Cyrillic, Greek — is written with a Type0 font whose two-byte codes are
UTF-16 code units, plus a `ToUnicode` CMap that maps them back.

No font file is embedded. The layer is drawn in rendering mode 3, so no glyph
outline is ever needed — only the positions and the text behind them. That keeps
the wheel free of a bundled font and its license, adds about nine kilobytes to a
document that needs it, and nothing at all to one that does not.

```python
pdf, report = ocrust.ocr_pdf("請求書.pdf")
report["unmappable_chars"]      # 0
```

```console
$ python -c "from pypdf import PdfReader; print(PdfReader('請求書.ocr.pdf').pages[0].extract_text())"
請求書 2026-04-1187
株式会社ミュンヘン機械
```

`unmappable_chars` counts what the layer really lost, so it should read 0; over
the whole corpus it went from 340 to 0 when the Unicode font landed.

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
    except (OSError, ValueError, ocrust.OcrustError) as exc:
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
over clean, aged and fax PDFs ([Evaluation](Evaluation.md)):

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
