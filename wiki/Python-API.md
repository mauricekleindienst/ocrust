# Python API

Everything is reachable from the top-level module. There is one class, `Ocr`, a
set of result dataclasses, and module-level helpers that use a shared default
engine.

```python
import ocrust

ocrust.read(source)          -> str
ocrust.scan(source)          -> Document
ocrust.scan_many(sources)    -> Iterator[Document]
ocrust.ocr_pdf(source)       -> tuple[bytes, dict[str, int]]
ocrust.searchable_pdf(src)   -> bytes
ocrust.searchable_pdf_many(sources) -> tuple[bytes, int]   # one PDF, many inputs
ocrust.to_tiff(source)       -> tuple[bytes, Document]
ocrust.languages()           -> tuple[dict, ...]
ocrust.known_languages()     -> tuple[dict, ...]
ocrust.install_models()      -> dict[str, str | None]
ocrust.models_cache_dir()    -> str
ocrust.runtime_info()        -> dict
```

The module-level helpers build one engine on first use and reuse it. That is
convenient for a script and wrong for a service: an explicit `Ocr` lets you pick
the device, the language check and the worker count.

## `Ocr`

```python
ocr = ocrust.Ocr(
    models_dir=None,
    device=None,          # "cpu" (default), "auto", "cuda", "cuda:1", "coreml", "directml"
    threads=None,         # threads per inference operator
    page_workers=None,    # pages in parallel; None -> 1
    pdf_dpi=None,         # PDF rasterization DPI, default 200
    pdf_text="never",     # "auto": a born-digital page's own text, exact; "always"; "never"
    password=None,        # for encrypted PDFs; owner-only protection needs none
    max_pixels=None,      # decompression-bomb guard; ~179 million by default, 0 = off
    io_retries=None,      # extra attempts on a transient read failure; 2 by default
    preprocess=True,      # auto-invert, deskew, rescale
    deskew=None,          # deskew alone, when preprocess is on
    word_boxes=True,      # per-word geometry (hOCR/ALTO word output needs it)
    drop_score=None,      # minimum mean line confidence, default 0.5
    lang=None,            # "de", "de,fr" or ["de", "fr"] -> checked, not a hint
    keep_page_images=False,
    read_stamps=False,    # read coloured ink (stamps) a second time on its own
    tick_boxes=False,     # find tick boxes on forms, ticked or not
    # model overrides
    detection_model=None, recognition_model=None, orientation_model=None, dictionary=None,
    # tuning
    fix_orientation=True, det_limit_side=None, det_box_threshold=None,
    det_unclip_ratio=None, rec_batch_size=None, rec_image_height=None,
    rec_space_gap=None,   # 0 turns off restoring swallowed word spaces
)
```

Building one loads the models, so keep it. Instances are thread-safe and release
the GIL for the whole scan, so a thread pool in Python parallelizes properly.

| Argument | When to change it |
|---|---|
| `pdf_dpi` | 200 is the default. Raise to 300 for small print; 100 is faster and, on ordinary scans, just as accurate ([Performance](Performance.md)). |
| `pdf_text` | Born-digital PDFs. `"auto"` reads a page whose text is drawn in real, visible, mapped glyphs straight from the page — exact, and without any model time — and recognizes the rest: scans, pictures of text, a page whose text layer is someone else's invisible OCR, a font without a Unicode mapping. Such pages have origin `"pdf_text"` and confidence 1.0. `"always"` trusts any text layer; `"never"`, the default, recognizes every page. `ocrust markdown` uses `"auto"`. |
| `page_workers` | Multi-page documents and batches. Defaults to 1 because workers and inference threads share the same cores. |
| `preprocess` | `False` buys ~9% and costs nothing measurable in CER; it can hurt multi-column layout. |
| `word_boxes` | `False` when you only want text — it skips the per-character bookkeeping. |
| `drop_score` | Raise it to suppress noise lines, lower it to keep faint print. |
| `det_limit_side` | The detector's working size (960). Large formats are tiled automatically above 3840 px. |
| `det_unclip_ratio` | 1.5. Raise it when characters are clipped, lower it when neighbouring lines merge. |
| `lang` | Always, in production. See [Languages](Languages.md). |
| `password` | A PDF that needs a password to open. Protection by an owner password alone — the usual bank statement — needs none. A text layer added with `ocr_pdf` is written without the password. |
| `max_pixels` | An image is refused on the size it declares, before any of it is decoded. The default is Pillow's threshold (178 956 970), which admits an A0 sheet at 300 dpi; lower it for a service that accepts uploads, `0` removes it. |
| `io_retries` | Reading off a network share. Two retries by default; raise it for a share that drops, set `0` to fail fast. See [Network shares](Network-shares.md). |
| `read_stamps` | Finding stamps. A red or blue stamp printed across the text is lost among the lines it crosses; this reads the page's coloured ink again on its own and adds what it finds as blocks of kind `"stamp"`. A page without coloured ink costs about 1 % more, a page with a stamp about 13 %. `ocrust vs` turns it on. |
| `tick_boxes` | Forms. The recognizer reads the words beside a tick box but hardly ever the box; this finds the boxes in the pixels and adds each as a block of kind `"tick_box"` reading `☒` or `☐`, where it is printed. About 40 ms a page. `ocrust vs` turns it on. |
| `rec_space_gap` | Almost never. `0` disables space restoration, which you want only if a swallowed space is preferable to a wrongly inserted one. |

### Methods

```python
ocr.scan(source, *, pages=None, name=None, progress=None) -> Document
ocr.read(source, **kw) -> str
ocr.scan_many(sources) -> Iterator[Document]      # paths, directories, patterns

ocr.ocr_pdf(source, *, dpi=None, skip_pages_with_text=True, compress=True) -> (bytes, report)
ocr.plan_pdf(source, *, skip_pages_with_text=True) -> tuple[dict, ...]
ocr.searchable_pdf(source, *, dpi=None, jpeg_quality=80) -> bytes
ocr.searchable_pdf_many(sources, *, dpi=None, jpeg_quality=80) -> (bytes, pages)
ocr.to_tiff(source, *, gray=False) -> (bytes, Document)

ocr.models            # the four resolved file paths
ocr.languages         # languages the loaded model covers completely
ocr.charset_size      # characters the recognizer can emit
ocr.partial_languages(min_ratio=0.8)
```

`source` can be a path, `bytes`, a numpy array (`(H, W)`, `(H, W, 3)` or
`(H, W, 4)`, any dtype) or a PIL image. Neither numpy nor Pillow is a
dependency — they are used only if you already have them.

```python
import numpy as np
from PIL import Image

ocr.read(np.asarray(Image.open("page.png")))
ocr.read(Image.open("page.png"))
ocr.read(open("page.png", "rb").read())
ocr.read("scan.pdf", pages=[0, 2, 4])          # zero-based
```

A selection that matches no page at all raises `ValueError` — an empty document
would be indistinguishable from a blank scan.

## Scanning

### Progress

```python
def tick(page: int, total: int, lines: int) -> None:
    print(f"page {page + 1}/{total}, {lines} lines")

doc = ocr.scan("book.pdf", progress=tick)
```

Called after every page, with the GIL held for the call and released again for
the next page. Raising inside it aborts the scan and the exception comes back out
unchanged — which is how you cancel one.

### Batches, directories and patterns

```python
for doc in ocr.scan_many(["a.pdf", "b.png"]):
    ...
for doc in ocr.scan_many(["archive/"]):        # recursive, readable files only
    ...
for doc in ocr.scan_many(["scans/*.tiff"]):    # expanded here, not by the shell
    ...
```

Files you name yourself come back one document per input, duplicates included, so
`zip(paths, docs)` lines up. Only expanded files are de-duplicated.

`scan_many` returns once the whole list is scanned and raises on the first file
it cannot read. For large batches, `scan_each` is the loop to write:

```python
for path, result in ocr.scan_each(["archive/"], chunk=32):
    if isinstance(result, Exception):   # OSError, ValueError or OcrustError
        print(path, "skipped:", result)
    else:
        print(path, len(result.pages))
```

It scans a chunk of files at a time in parallel across the page workers, so
results arrive while the batch runs and memory is bounded by the chunk, and a
file that cannot be read arrives as its exception instead of ending the batch.

### Searching a result

```python
for hit in doc.search("gesamtbetrag"):
    print(hit.page, hit.text, hit.box.as_tuple())

doc.search(r"\d+,\d\d\s*EUR", regex=True)
doc.search("EUR", whole_words=True)
doc.search("Grüße", case=True)
```

`search` returns a tuple of `Match(text, page, box, line, boxes)`. `page` is the
page's own `index`, so a hit still names the right page of the source document
when only some pages were scanned. The box is the union of the *words* the hit
covers, which is what makes highlighting possible; without word boxes
(`word_boxes=False`) it is the line's box. `boxes` has one box per row of print:
two for a word hyphenated at a line end, which the layout joins into one line. Matching is case-insensitive by default, because OCR case is not
reliable enough to search on. The query is normalized to composed Unicode first,
so `Grüße` typed on a Mac — where `ü` often arrives as `u` plus a combining
diaeresis — still finds the text.

## Converting into one PDF

```python
data, pages = ocrust.searchable_pdf_many(["fax.tiff", "photo.jpg", "old.pdf"])
data, pages = ocrust.searchable_pdf_many(["archive/"])        # a folder, recursively
data, pages = ocrust.searchable_pdf_many(["scans/*.tif"])     # or a pattern
Path("one.pdf").write_bytes(data)
```

Anything in `READABLE_SUFFIXES` can be mixed — images in any offered format,
multi-page TIFFs, existing PDFs — and they become the pages of one searchable
document, in the order given. Folders are walked recursively and sorted, so the
same folder gives the same PDF twice running. Each page keeps its own size,
computed from its pixels and `dpi`, rather than being stretched onto a common
sheet. The second return value is the page count, which is not the number of
inputs when a multi-page TIFF or PDF is among them.

Pages are compressed as they are scanned, so a hundred inputs cost the memory of
one.

It re-renders every page, which is what lets different formats sit on equal
footing. For a PDF whose pages must stay exactly as they are, use `ocr_pdf`
instead — it only adds the text layer.

## Results

```python
Document(source, pages, elapsed_ms)
  .text                      # every page, reading order applied
  .lines  .words             # flattened across pages
  .tables                    # every table, in reading order
  .confidence                # mean line confidence, or None
  .quality                   # estimated share of the text that is right, or None
  .render(format) / .markdown() / .json() / .hocr() / .alto() / .csv()
  .search(needle, *, regex=False, case=False, whole_words=False)
  .to_dict()

Page(index, width, height, rotation, origin, blocks, elapsed_ms)
  .text  .lines  .tables  .confidence  .quality

Block(kind, box, lines, table)   # kind: "paragraph", "heading", "list", "table"
  .text

Table(rows, columns, cells)
  .row(index)                # the cells of one row, left to right
  .row_text(index)           # one string per column, "" for the gaps
  .as_rows()                 # the whole grid as rows of strings
  .to_csv()                  # quoted per RFC 4180

Cell(row, column, text, box, confidence, column_span)
Line(text, box, confidence, angle, margin, words, polygon, segments)
Segment(text, box, confidence)   # a detector box, before it became part of a line
Word(text, box, confidence)
Box(x0, y0, x1, y1)  .width  .height  .as_tuple()
Match(text, page, box, line, boxes)
```

All coordinates are pixels in the **preprocessed** page image, whose size is
`Page.width` × `Page.height`. `Line.polygon` keeps the detector's four corners,
so rotated lines are not squashed into their bounding box, and `Line.angle` is
the baseline angle in degrees.

The dataclasses are frozen, so they hash, compare and pickle without surprises.

```python
doc = ocrust.scan("form.pdf")

for page in doc.pages:
    for block in page.blocks:
        if block.kind == "heading":
            print(page.index, block.text)

low = [l for l in doc.lines if l.confidence < 0.8]
print(f"{len(low)} line(s) worth a human look")
```

### Tables

A block whose cells line up into columns is read as a table. `Table.cells` holds
them in reading order with their own box and confidence; a row with nothing in a
column simply has no cell for it, and `row_text` fills the gap with an empty
string so every row has the same width.

```python
doc = ocrust.scan("rechnung.pdf")

for table in doc.tables:
    header, *body = table.as_rows()
    for row in body:
        print(dict(zip(header, row)))

# Or hand the grid straight to something that eats CSV.
import csv, io
rows = list(csv.reader(io.StringIO(doc.tables[0].to_csv())))
```

`Ocr(tables=False)` leaves every block as running text. There is no table model
behind this and no ruling lines are read — see
[Accuracy](Accuracy.md#tables) for how the columns are found, what the guardrails
are, and what it cannot see.

### Search profiles

```python
profile = ocrust.terms.load("profil.toml")     # .toml, .json, one phrase per line, or a list
for hit in ocrust.scan("akte.pdf").find(profile):
    print(hit.page, hit.term, hit.text, hit.how, hit.score)
```

`Document.find` returns a `TermReport`: every occurrence of every term, found
however the scan broke it up — letter-spaced, hyphenated, over two lines, glued,
misread — with its page, boxes, zone, severity and how it was broken. See
[Search profiles](Search-profiles.md).

### Classification markings

```python
report = ocrust.scan("akte.pdf").markings()
report.label, report.level, report.unmarked_pages   # ('VS-NfD', 1, (5,))
```

`Document.markings()` returns a `MarkingReport`: the highest grade on a common
1–4 scale, TLP and company markings beside it, and every finding with its page,
box and the reason it was judged a marking rather than a mention.
`ocrust.markings.inspect(doc, file=path)` also reads the file's sensitivity
label. See [Classification markings](Classification-markings.md).

## Errors

Everything the engine refuses raises `ocrust.OcrustError` (a `RuntimeError`),
with the underlying reason in the message:

```python
try:
    ocr = ocrust.Ocr(lang="ru")
except ocrust.OcrustError as exc:
    print(exc)   # invalid configuration: the recognition model cannot write Russian …
```

A file that cannot be read raises `OSError` (it cannot be opened), `ValueError`
(it is not a document ocrust can decode) or `OcrustError` (it opened, but a page
could not be read — a damaged PDF, say). The engine's own configuration fails
when the `Ocr` is built, so a loop over files can catch all three:

```python
for path in paths:
    try:
        print(ocrust.read(path))
    except (OSError, ValueError, ocrust.OcrustError) as exc:
        print(f"{path}: skipped ({exc})")
```

`scan_many` raises `OcrustError` for a file that failed, after yielding the ones
that succeeded before it.

A PDF that needs a password, or an image over `max_pixels`, is an unreadable
file like any other: `scan` and `read` raise `ValueError`, with a message that
says what to pass. `ocr_pdf` reports every failure as `OcrustError`, these two
included.

## Diagnostics

```python
ocrust.runtime_info()
```

```python
{'python': '3.12.3',
 'platform': 'linux',
 'onnxruntime_dylib': '.../onnxruntime/capi/libonnxruntime.so.1.30.0',
 'onnxruntime_version': '1.30.0',
 'onnxruntime_loaded': 'ONNX Runtime (API level 22)',
 'models_dir': '.../ocrust_models/models',
 'models': {'detection': '.../ppocrv6_det.onnx',
            'recognition': '.../ppocrv6_rec.onnx',
            'orientation': '.../ppocr_cls.onnx',
            'dictionary': None},
 'models_cache_dir': '/root/.cache/ocrust/models',
 'ocrust': '0.1.0'}
```

See [Troubleshooting](Troubleshooting.md) when a field says `unavailable`.
