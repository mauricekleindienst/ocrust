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

- 35 languages known, with per-language alphabets cross-checked against
  PaddleOCR's dictionaries.
- `lang=` is a check, not a hint: building an engine fails when the recognition
  model cannot spell a requested language, naming the missing characters.
- `ocrust languages` reports covered and nearly covered languages.

### Line assembly and speed

- **Boxes on one baseline now become one line.** A detector returns boxes, not
  lines: a receipt's item and its right-aligned price are two boxes, and so are
  the cells of a table row. Joining them fixed receipts (CER 0.332 → 0.188),
  ruled forms (0.156 → 0.063) and drawings (0.197 on the worst sheet), while clean
  pages, newspapers, faxes and multi-page scans stayed exactly as they were. It
  costs one category: the A0 sheet's title block is now read row-wise, which its
  ground truth lists field by field (0.042 → 0.147, word recall 0.895) — see
  `docs/evaluation.md`.
- Preprocessing now **earns its keep**: because lines are assembled from
  baselines, deskewing decides whether a skewed page's line is assembled
  correctly. `preprocess=False` went from free to costing ten times the error rate
  on skewed, aged and inverted pages (CER 0.004 against 0.040).
- Whether a wide gap belongs to one row is decided by **repeating columns**: a
  table or price list puts cells at the same x positions row after row, a
  drawing's labels merely share a height. The decision is taken per region, so a
  drawing's title block can be tabular while the sheet around it is not.
- A column split now also requires **two lines on each side** and a side that is
  wide relative to the gutter, which is what separates a newspaper's columns from
  a table's cells.
- **Fewer calls per page**: the line orientation classifier asks about a sample of
  crops and applies a unanimous verdict to the rest instead of classifying every
  line, and the skew estimate works on a smaller probe with a coarser first pass.
  A clean 200 dpi A4 page takes about 660 ms on four cores; the corpus median is
  669 ms per page.

### Searchable in every script

- **PDF text layers are no longer limited to WinAnsi.** A line with CJK, Cyrillic
  or Greek in it is written with a Type0 font whose two-byte codes are UTF-16 code
  units, plus a `ToUnicode` CMap; Western text keeps the base-14 font it had. No
  font file is embedded, because the layer is invisible and no glyph outline is
  ever drawn — so the wheel stays free of a bundled font and its license, and the
  added objects cost about nine kilobytes per document.
- A Japanese scan now extracts as Japanese: `unmappable_chars` went from 340 over
  the corpus to **0**, and the font objects are added only to documents that need
  them, so a German invoice contains exactly what it did before.

### Looking things up

- **`doc.search("gesamtbetrag")`** returns every hit with its page and the box
  around the matching *words*, so it can be highlighted. Case-insensitive by
  default (OCR case is not reliable enough to search on), with `regex=True`,
  `case=True` and `whole_words=True` when you need them. It falls back to the line
  box when word boxes are off.
- **`progress=`** on `Ocr.scan` is called after every page with
  `(page, total_pages, lines)`, and `ocrust scan --progress` prints it. Raising
  inside the callback aborts the scan, and the exception is the one you raised.
- `scan_many` accepts directories and glob patterns like the CLI does. Files you
  name yourself stay exactly as given, duplicates included, so one document comes
  back per input; only expanded files are de-duplicated.

### Word spaces the recognizer swallowed

- **Missing spaces are restored from the pixels.** A CTC recognizer emits a space
  only when it is confident about the space class, and on a JPEG-compressed scan
  or a fax it drops them: `Gesamtbetrag: 5.726,88 EUR` came back as
  `Gesamtbetrag:5.726,88EUR`. Each recognition crop now also yields a column ink
  profile, and a gap becomes a space when it is wide on the CTC timeline, blank on
  the paper, and allowed by typography. The timeline alone is not enough — it
  leaves just as wide a gap after a capital `M` or between a doubled `mm`.
- Measured over the whole corpus: **mean CER 0.045 → 0.040, median 0.013 → 0.006,
  WER 0.176 → 0.120, word recall 0.836 → 0.892**, with no category worse. The
  biggest movers are the ones that go through a lossy encoder: image-only PDFs
  0.014 → 0.004 (recall 0.848 → 0.957), rotated PDFs 0.082 → 0.054 (recall
  0.514 → 0.884), aged scans 0.012 → 0.006, every raster format 0.007 → 0.001. A
  receipt's WER fell from 0.382 to 0.059.
- The decoder also keeps the *run* of timesteps a class occupies instead of only
  its first step, so `m` is wider than `i`. That is what makes the gaps mean
  something, and it makes per-word boxes more accurate as a side effect.
- `Ocr(rec_space_gap=0)` turns the pass off.

### Batches from the shell

- `ocrust scan` accepts **directories** (walked recursively, filtered to readable
  extensions) and **glob patterns**, which is what `ocrust scan '*.pdf'` needs on
  Windows, where the shell expands nothing. Inputs are sorted and de-duplicated,
  so a batch writes the same output twice in a row, and a directory with nothing
  readable in it is an error rather than a silent success.

### Models and offline installs

- The model manifest points at this repository's own `models/ppocrv6/` on
  `raw.githubusercontent.com`, so `ocrust install-models` and the repository are
  the same source.
- Model downloads authenticate with `OCRUST_GITHUB_TOKEN` or `GITHUB_TOKEN` when
  one is set, which is what a private repository or a GitHub Enterprise mirror
  needs. The token is sent only to GitHub hosts — never to a mirror URL out of a
  manifest — and that is unit-tested.

### Network shares

- **Reads retry through a dropped connection.** Scanning `\\fileserver\scans` is
  not a local read: SMB and NFS time out, reset and answer "the network name is
  no longer available" for reasons that have nothing to do with the file. Reads
  in the engine and writes from the CLI now retry twice with backoff. "Not
  found" and "permission denied" are answers, not hiccups, and are never
  retried; the Windows redirector's own codes (network name deleted, unexpected
  network error, out of system resources) are.
- `Ocr(io_retries=…)` and `--io-retries N` tune it; `0` fails on the first error.
- **An unreachable share says why.** `Path.exists()` answers *False* both for a
  missing file and for a server nobody can reach, so the CLI asks again and
  reports the real reason: `\\fileserver\scans: [WinError 53] The network path
  was not found` instead of a bare "no such file".
- UNC paths, mapped drives and POSIX mounts work everywhere a path does,
  including recursive directory walks and `'\\server\share\*.pdf'` patterns,
  which `ocrust` expands itself because the Windows shell does not.

### A test application

- `examples/app.py`: a local web app with **no dependencies beyond the library**
  — standard-library `http.server` and a hand-rolled multipart reader, no Flask,
  no Streamlit. Drop in a scan, a photo or a PDF and see the text, the line boxes
  over the image, all six export formats, search with highlighted hits, mean
  confidence and per-page timing, and a one-click searchable PDF.
- It doubles as a small HTTP API (`POST /scan`, `POST /scan?q=`, `POST /pdf`,
  `GET /languages`, `GET /health`), so it is also the quickest way to check an
  installation. `POST /pdf` returns an `X-Ocrust-Report` header, so a PDF that
  already had text reads as "nothing added" instead of looking like a failure.
- Covered by `tests/test_example_app.py` over real HTTP, and exercised by
  `scripts/dev_e2e.sh`, so the example cannot quietly rot.

### Documentation

- A logo (`assets/ocrust.svg`) at the top of the README and the wiki.
- A 15-page wiki under `wiki/`: installation, quickstart, the Python API, the
  CLI, PDF workflows, languages, models, performance, accuracy, architecture,
  evaluation, troubleshooting, contributing and the roadmap.

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
- Installing needs no compiler and no admin rights: prebuilt abi3 wheels, and
  ONNX Runtime is loaded at run time from the `onnxruntime` wheel. A source build
  is pure Rust as well, except for the optional model downloader, whose TLS stack
  (`ring`) compiles C — `--no-default-features --features pdf` avoids it.
- Models ship as the `ocrust-models` wheel or are fetched from
  `raw.githubusercontent.com` with SHA-256 verification. No other hosts are ever
  contacted.
- CLI: `scan`, `ocr`, `pdf`, `tiff`, `languages`, `models`, `install-models`,
  `doctor`.
