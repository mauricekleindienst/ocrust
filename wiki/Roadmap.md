# Roadmap

Ordered by how much the measurements say it matters, not by how interesting it is
to build. Every item here is a known gap, named in [Accuracy](Accuracy.md) or
[Languages](Languages.md) with the number that justifies it.

## 1. Table structure from a model, not a threshold

The geometric half of this shipped in 0.2.0: cells are found from ruling lines
and alignment, rows and columns are assigned, and a `Table` block renders into
Markdown, hOCR and CSV. Price lists went to CER 0.000 and the corpus finds 16
tables. What remains is the part a threshold cannot do.

**Why:** it is still the largest accuracy loss by a wide margin, and the words are
read correctly — their *order* is wrong. Drawings sit at CER 0.170 with word
recall 0.855, forms at 0.063 / 0.744, the A0 sheet at 0.147 / 0.895. Reading order
is geometric, and it cannot infer that a form's cells are a grid or that a title
block's two columns are fields rather than a row.

The A0 sheet makes the case concretely: joining a title block's fields into rows
is what the A3 drawings need (0.391 → 0.197) and what the A0 sheet loses by
(0.042 → 0.147). No gap threshold separates those two, because they are the same
document at two sheet sizes. A structure model would decide it properly.

**Shape of the fix:** a learned table- and form-structure model over the detected
boxes, and — with cells known — field-wise rather than row-wise text for blocks
that are forms rather than tables. Receipts show the other half of the problem:
CER 0.183 against a word error rate of 0.059, which is a reading-order cost, not
a recognition one.

## 2. More scripts

**Why:** Cyrillic (Russian, Ukrainian, Bulgarian, Serbian), Korean, Arabic and
Devanagari are known, checked and **not covered** — `Ocr(lang="ru")` refuses
rather than returning nonsense, which is correct but not useful. Greek and
Vietnamese joined them in 0.2.2: the bundle has their plain letters and not the
accents or tone marks the languages are written with, which is the same gap in a
less obvious disguise.

**Shape of the fix:** additional PP-OCR recognition bundles in `models/`, each
with its manifest entry, plus per-bundle language coverage so `ocrust languages`
reports the union. The engine already supports swapping the recognizer; this is
packaging and measurement work, not new code.

## 3. Lower memory

**Why:** ~700–800 MB resident while scanning A4 pages at 200 dpi, most of it ONNX
Runtime's CPU arena. It plateaus, but it makes a 1 GB container limit
uncomfortable, and a second engine (the PDF-layer sibling) adds another ~100 MB.

**Shape of the fix:** tune the arena configuration, reuse one engine for the PDF
paths by default instead of building a sibling, and bound the recognition batch
by pixels rather than count.

## 4. Confidence calibration

**Why:** mean line confidence is 0.99 on clean pages and 0.93 on Greek, which the
bundled charset cannot even write — usable for routing, but the absolute values
are optimistic: a line can be confidently wrong when the model is out of its
depth, and Greek is the proof. Anyone using a threshold to trigger human review
is guessing at the number.

**Shape of the fix:** measure confidence against the corpus's ground truth, report
the calibration curve, and document a threshold that means something.

## Done since this list was written

- **A converter.** `ocrust pdf` takes files, folders and patterns together and
  writes anything readable as the pages of one searchable PDF (0.2.3).
- **Bilevel TIFF is readable.** Mode-1 pages — scanned archives and CCITT faxes —
  failed outright until 0.2.3, and four formats that could not be opened at all
  are no longer offered.
- **Tables from line geometry**, with `Table` and `Cell` in the API and Markdown,
  hOCR and CSV renderings (0.2.0). See item 1 for what is left.
- **Pages are streamed, not buffered.** Peak memory no longer follows the page
  count, for TIFF, PDF and the searchable-PDF path (0.2.0).
- **The language check tells the truth.** Greek and Vietnamese were reported as
  covered while the bundle could not write them; both are now refused with their
  missing characters named, and `ẞ` is reported as a substitution rather than
  costing German its coverage (0.2.2).
- **Unicode PDF text layers.** CJK, Cyrillic and Greek go into the layer as
  UTF-16 through a Type0 font with a `ToUnicode` map, instead of `?`. Nothing is
  embedded, because nothing is drawn. Corpus `unmappable_chars`: 340 → 0.
- **Swallowed word spaces are restored** from the column ink of each crop, which
  took image-only PDFs from CER 0.014 to 0.004 — see [Accuracy](Accuracy.md).
- **Directory and glob inputs**, in the CLI and in `scan_many`.
- **Progress callbacks** (`Ocr.scan(progress=…)`, `ocrust scan --progress`).
- **`doc.search()`** with word-level boxes, regex, case and whole-word options.

## Deliberately not planned

- **Handwriting.** A different problem and a different model family. The PP-OCR
  bundle is not going to do it, and pretending otherwise would be dishonest.
- **A GUI.** `ocrust` is a library and a CLI; a viewer belongs in its own project.
- **Bundling ONNX Runtime in the wheel.** It would double the download to save one
  `pip install` line, and it would make GPU support harder, not easier.
- **A "just works" auto-language mode.** Language detection that silently picks a
  model is exactly the behaviour [Languages](Languages.md) exists to prevent.

## Ideas worth measuring first

- **Server-class detection** (`PP-OCRv5_server_det`) on small print: better, but
  how much, at what cost per page?
- **Adaptive DPI**: rasterize at 100 dpi, detect, and re-run only the regions with
  small text at 300. Could be faster *and* more accurate; could also be a
  complexity trap.
- **INT8 quantized models**: a third of the size and maybe faster on CPU — needs
  a CER measurement before it ships, not after.
