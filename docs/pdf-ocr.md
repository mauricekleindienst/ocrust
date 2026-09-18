# Putting an OCR layer on PDFs

Two different jobs share the same phrase, and mixing them up is how archives get
ruined:

| Goal | Command | What happens |
|---|---|---|
| A scanned PDF should become searchable | `ocrust ocr scan.pdf` | The original pages are kept byte for byte; an invisible text layer is added |
| An image should become a PDF | `ocrust pdf photo.jpg` | A new PDF is built: the image plus an invisible text layer |

## Adding a layer to an existing PDF

```bash
ocrust ocr scan.pdf                      # -> scan.ocr.pdf
ocrust ocr scan.pdf -o searchable.pdf
ocrust ocr scan.pdf --dpi 300            # small print
ocrust ocr scan.pdf --dry-run            # report only, change nothing
ocrust ocr scan.pdf --force              # also OCR pages that already have text
ocrust ocr scan.pdf --lang de,fr         # fail if the model cannot spell them
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

Nothing in the original document is re-encoded. Only two objects are added per
page: a content stream with the invisible text, and one shared font. Images,
vector content, compression, annotations, bookmarks and metadata are untouched.

### What is handled

- **Pages that already have text.** Skipped by default, so the tool is safe to
  run across a mixed archive where some files are born-digital. `--force`
  overrides it; `--dry-run` shows the decision per page first.
- **Rotation.** `/Rotate 90`, `180` and `270` are common in scans. Renderers
  apply the rotation, so OCR coordinates are in display space; the text layer
  carries a transform that maps them back to page space, and selection lines up.
- **Inherited resources.** `/Resources` often lives on an ancestor of the page.
  Writing a fresh dictionary onto the page would shadow it and break the page's
  own content, so the inherited one is copied and extended.

### Dry run

```console
$ ocrust ocr archive.pdf --dry-run
page 1: 612x792 pt -> ocr
page 2: 612x792 pt, rotated 90deg -> ocr
page 3: 612x792 pt -> skip (has text)

2 of 3 page(s) would get a text layer
```

### The text layer's character set

Any script the recognizer can read ends up in the layer. Western European text
uses a base-14 WinAnsi font, exactly as before; a line with anything else — CJK,
Cyrillic, Greek — is written with a Type0 font whose two-byte codes are UTF-16
code units, plus a `ToUnicode` map that says so. Nothing is embedded, because
nothing is drawn: the layer is invisible, so no glyph outline is ever needed, and
the wheel stays free of a bundled font and its license.

```python
pdf, report = ocrust.ocr_pdf("請求書.pdf")
report["unmappable_chars"]     # 0 — the Japanese text is in the layer
```

```console
$ python -c "from pypdf import PdfReader; print(PdfReader('請求書.ocr.pdf').pages[0].extract_text())"
請求書 2026-04-1187
株式会社ミュンヘン機械
```

`unmappable_chars` therefore counts what the layer really lost, which is now
nothing in practice. The Unicode font is added only when a page needs it, so a
Western document contains exactly the objects it did before.

## Building a PDF from images

```bash
ocrust pdf photo.jpg -o photo.pdf
ocrust pdf photo.jpg --dpi 300 --quality 90
```

Each page becomes a JPEG at the chosen quality with the text layer on top. Use
this for phone photos and loose scans; use `ocrust ocr` whenever a PDF already
exists.

## Archiving as TIFF

```bash
ocrust tiff scan.pdf --gray --sidecar text -o scan.tiff
```

Writes a deskewed, upright multi-page TIFF plus the recognized text next to it —
the shape most document archives expect.
