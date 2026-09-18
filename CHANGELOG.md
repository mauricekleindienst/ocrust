# Changelog

## 0.1.0 — unreleased

First release.

### Engine

- Text detection (DB), 180° text-line orientation classification and CTC
  recognition from the PP-OCR family, run through ONNX Runtime.
- Bundled **PP-OCRv6** models: 18 708 character classes, complete coverage of 27
  languages.
- Preprocessing: EXIF orientation, auto-inversion of light-on-dark pages, skew
  estimation and correction, rescaling, optional denoise and contrast stretch.
- Layout: XY-cut reading order with column detection, paragraph grouping,
  de-hyphenation across line breaks, heading and list classification, per-word
  boxes derived from CTC character positions.

### Input

- Images: PNG, JPEG, WebP, BMP, GIF, PNM/PBM/PGM/PPM, TGA, DDS, HDR, OpenEXR,
  QOI, ICO. Formats without magic bytes are resolved from the file name.
- Multi-page TIFF (every page) and PDF (every page, rasterized by the pure-Rust
  `hayro` renderer).
- In memory: `bytes`, `numpy` arrays and PIL images.

### Output

- Plain text, Markdown, JSON with full geometry and confidences, hOCR, ALTO XML,
  CSV.
- Searchable PDF built from images.
- **OCR text layer added to an existing PDF**, preserving its pages, images and
  compression; pages that already contain text are skipped, `/Rotate` is handled
  and inherited resources are kept.
- Multi-page TIFF, colour or greyscale.

### Languages

- 36 languages known, with per-language alphabets cross-checked against
  PaddleOCR's dictionaries.
- `lang=` is a check, not a hint: building an engine fails when the recognition
  model cannot spell a requested language, naming the missing characters.
- `ocrust languages` reports covered and nearly covered languages.

### Found by the corpus, fixed

- **Large-format sheets.** An A0 drawing at 300 dpi scored a 96% character error
  rate: detection scaled the whole sheet to 960 px, leaving 8 pt labels four
  pixels tall. Detection now runs in overlapping tiles above four times the
  working size, with each tile owning exactly its half of every overlap
  (CER 0.96 → 0.04).
- **Sideways pages.** Pages rotated a quarter turn were recognized but returned
  in column order. The share of tall boxes now decides the turn, and detection
  re-runs on the straightened page.
- **Upside-down pages.** Every line was read correctly but in reverse order. The
  180-degree line classifier's verdict now rotates the page geometry as well
  (CER 0.75 → 0.11).
- **Ruled tables** were read column by column, because a table's cell gaps looked
  like page columns to the XY-cut. A column split now also has to be at least
  3.5% of the content width.
- **Thread oversubscription.** Page workers each asked ONNX Runtime for every
  core, so eight workers were 20% *slower* than one. Workers now divide the
  cores; a 12-page scan went from 8.5 s to 5.8 s with four of them. The default
  is one worker, which is fastest for a single page.
- **Text-layer alignment.** The PDF overlay assumed OCR coordinates matched the
  rendered page, which deskewing and rescaling silently broke. The overlay now
  scans without geometry changes, and rotated lines get a rotated baseline.

### Evaluation

- `scripts/make_corpus.py` generates a torture-test corpus with ground truth:
  aged and stained scans, 1-bit faxes, A3 technical drawings with title blocks
  and vertical labels, ruled forms, thermal receipts, three-column newspapers,
  skewed and `/Rotate`-d pages, dark-mode screenshots, an A0 sheet at 300 dpi,
  multi-page PDFs and TIFFs, born-digital PDFs and deliberately broken files.
- `scripts/evaluate_corpus.py` reports character error rate, word error rate and
  word recall per category and language, plus throughput, worker scaling, a DPI
  sweep, preprocessing on/off, every export format, the PDF text layer, archive
  TIFF output and robustness against broken input.

### Packaging

- Prebuilt abi3 wheel, Python 3.9 and up. No Tesseract, PaddlePaddle, PyTorch,
  Poppler or PDFium.
- No C, C++ or CMake dependency anywhere in the tree; no admin rights needed.
- Models ship as the `ocrust-models` wheel or are fetched from
  `raw.githubusercontent.com` with SHA-256 verification. No other hosts are ever
  contacted.
- CLI: `scan`, `ocr`, `pdf`, `tiff`, `languages`, `models`, `install-models`,
  `doctor`.
