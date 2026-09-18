# Roadmap

Ordered by how much the measurements say it matters, not by how interesting it is
to build. Every item here is a known gap, named in [[Accuracy]] or
[[Languages]] with the number that justifies it.

## 1. Table structure recognition

**Why:** the largest remaining accuracy loss by a wide margin, and the one place
where a threshold is doing a model's job. Receipts sit at CER 0.188 with word
recall 0.882, drawings at 0.170 / 0.855, forms at 0.063 / 0.744, and the A0 sheet
at 0.151 / 0.868. The words are read correctly; their *order* is wrong. Reading
order today is geometric — XY-cut with baseline merging and repeating-column
detection — and it cannot infer that a form's cells are a grid, or that a title
block's two columns are fields rather than a row.

The A0 sheet makes the case concretely: joining a title block's fields into rows
is what the A3 drawings need (0.391 → 0.197) and what the A0 sheet loses by
(0.042 → 0.151). No gap threshold separates those two, because they are the same
document at two sheet sizes. A structure model would decide it properly.

**Shape of the fix:** cell detection over the detected boxes (ruling lines when
they exist, alignment clustering when they do not), then row/column assignment,
then a `Table` block kind that Markdown, hOCR and CSV can render — and, with
cells known, field-wise rather than row-wise text for blocks that are forms
rather than tables. A learned table-structure model would do better; a geometric
one would already close most of the gap.

## 2. More scripts

**Why:** Cyrillic (Russian, Ukrainian, Bulgarian, Serbian), Korean, Arabic and
Devanagari are known, checked and **not covered** — `Ocr(lang="ru")` refuses
rather than returning nonsense, which is correct but not useful.

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

**Why:** mean line confidence is 0.99 on clean pages and 0.93 on Greek — usable
for routing, but the absolute values are optimistic: a line can be confidently
wrong when the model is out of its depth. Anyone using a threshold to trigger
human review is guessing at the number.

**Shape of the fix:** measure confidence against the corpus's ground truth, report
the calibration curve, and document a threshold that means something.

## Done since this list was written

- **Unicode PDF text layers.** CJK, Cyrillic and Greek go into the layer as
  UTF-16 through a Type0 font with a `ToUnicode` map, instead of `?`. Nothing is
  embedded, because nothing is drawn. Corpus `unmappable_chars`: 340 → 0.
- **Swallowed word spaces are restored** from the column ink of each crop, which
  took image-only PDFs from CER 0.014 to 0.004 — see [[Accuracy]].
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
  model is exactly the behaviour [[Languages]] exists to prevent.

## Ideas worth measuring first

- **Server-class detection** (`PP-OCRv5_server_det`) on small print: better, but
  how much, at what cost per page?
- **Adaptive DPI**: rasterize at 100 dpi, detect, and re-run only the regions with
  small text at 300. Could be faster *and* more accurate; could also be a
  complexity trap.
- **INT8 quantized models**: a third of the size and maybe faster on CPU — needs
  a CER measurement before it ships, not after.
