# Changelog

## Unreleased

### Removed

- **Greek is no longer claimed as a supported language.** `ocrust languages` now
  reports 26 covered, not 27, and `lang="el"` fails naming the characters the
  model cannot emit.

  The bundled PP-OCRv6 charset holds all 24 plain Greek letters in both cases and
  not one accented vowel — no `ά έ ή ί ό ύ ώ`, no dialytika, and no final sigma
  `ς`. Greek cannot be written without them: every polysyllabic word carries an
  accent, and `ς` ends a large share of them. What came out instead was
  accent-stripped text with Latin lookalikes standing in, `Μηχανουργεία
  Θεσσαλονίκης` as `Mηχανουργεα Θεσσαλονη` and `Σύνολο` as `Σúνoλo`, at CER 0.180
  and a reported confidence of 0.93 — the exact failure the language check exists
  to prevent, waved through because the check was asked the wrong question.

  The language table declared Greek as those 48 plain letters, so the coverage
  check found nothing missing. It now declares monotonic Greek in full, 69
  characters, of which the bundle has 48: coverage comes out at 70%, below the
  threshold for a near miss, and Greek moves to where Russian and Korean already
  were — known, refused with its missing characters listed, and covered again by
  itself the moment a Greek-capable recognizer is installed. A unit test pins
  that the plain letters alone do not count as coverage.

  Accuracy figures now cover the 108 corpus files in a language the bundle can
  write: mean CER **0.031**, median 0.006, WER 0.093, word recall **0.918**. No
  document reads differently — the four Greek pages stay in the corpus and are
  scored in a section of their own in the evaluation report, so the cost of the
  gap stays on the record instead of being averaged into the languages that work.

## 0.2.1 — 2026-09-19

Three layout bugs, all found by probing pages the corpus did not have.
Accuracy over the documents that were already there is unchanged file for file;
the corpus itself grew by the shapes that were missing, and over all 112 files
with ground truth it now reads mean CER 0.036, median 0.006, WER 0.107 and word
recall 0.904.

### Fixed

- **A page laid out in columns was read across them** when its columns sat on one
  baseline grid — a newspaper, a journal paper, anything set on a leading that
  leaves white space between every pair of lines. The XY-cut took the horizontal
  gap first, which cut the columns into rows, and the two columns of each row
  were then joined into one line: `Der Stadtrat hat beschlossen, Anwohner fordern
  mehr Raum`. Columns are now looked for before rows, and only where they are a
  property of the page: a wide corridor that runs the height of the region, with
  a column of text on either side of it — several baselines tall, one box on each
  — and columns of near enough the same width. A drawing's title block is two
  such sides and is not two columns, so a corridor has to divide a region that
  covers most of the page's width. Three-column and two-column corpus pages now
  read at CER 0.000; they read at 0.003 and as nonsense before.
- **A table's rows were split into columns** when its cells were wide enough that
  neither side of a gutter looked narrow: a price list came back as every article
  name, then every figure. What says the gutter runs through the rows rather than
  between columns is that one side puts several boxes on each baseline, which is
  a row of cells and never a column of text.
- **A page that is nothing but a table measured its own gutters as word spaces.**
  Its cells hold a word each, so almost every gap on the page is a column gap and
  even the quarter point lands among them; a nine-row price list came back as two
  columns instead of four. Where the gaps fall into two groups — word spaces well
  below, gutters well above — the threshold is now taken from the stretch between
  them, and the quarter point stands in only where there is no such stretch,
  which is what a page of prose looks like. The highest stretch wins, not the
  widest: a heading whose two words sit further apart than the rows beneath it
  opens one of its own, and a threshold taken from that cuts the heading into
  cells and hands it to the table as a header row. The estimate is never widened
  by this either, so no table that was found before is lost: the corpus still
  finds the same 16, plus the 8 new price lists as 9×4.

### Changed

- The corpus generator's `columns` option now spreads the lines over the columns
  instead of filling each to the bottom of the page, so a page asked for in three
  columns is in three columns. It was not, which is why nothing in the corpus
  exercised column layout and why the bug above survived. It also refuses to draw
  a line wider than its column — that caught `extremes/long_receipt.png`, whose
  invoice lines ran off the edge of the strip while its ground truth claimed the
  text was there (CER 0.054 → 0.000 once the strip is wide enough for them).
- New corpus fixtures for the shapes that were missing: an unruled full-page
  price list (PNG and PDF) and a two-column page set on one baseline grid.
- `scripts/collect_quality.py` matched a recognized line against at most three
  ground-truth lines in a row. The layout joins a table row into one line, so a
  five-column form's rows scored as though most of each row had been invented.
  Matching runs of up to five puts the gap between a line's confidence and its
  measured precision at 1.2% over the corpus rather than 2.2%, and at 1.4% rather
  than 12.2% over the forms.
- A CLI test built an engine it expected to fail, which it did not on a machine
  with `OCRUST_MODELS_DIR` set — the model search falls back to the environment.
  The test now clears it.

## 0.2.0 — 2026-09-19

Accuracy is unchanged throughout: the corpus run reproduces mean CER 0.040,
median 0.006, WER 0.120 and word recall 0.892 file for file, and a drawing's
recognized text is byte-identical to 0.1.0's. What is new reads the same
characters and does more with them; what changed is memory and output size; and
every fix below is a bug that produced a wrong box, a wrong page, a crash or a
build that would not compile.

### Changed

- **Peak memory no longer grows with the length of a document.** `ingest` used to
  rasterize every page into one list before the first line was recognized, so a
  scan cost about 10 MB per page: measured on a synthetic A4 invoice at 200 dpi,
  413 MB for one page, 999 MB for 40 and 1811 MB for 120, which is more than a
  laptop has by page 400. Pages are now decoded on demand and dropped as they are
  read — a PDF is parsed once and rendered page by page, sharing one render cache;
  a TIFF's directories are walked forward; the searchable-PDF path compresses each
  page as it is scanned and keeps the JPEG; the TIFF path encodes each page into
  the archive and lets it go. The same three scans now cost 233 MB, 249 MB and
  289 MB, and the PDF and TIFF conversions 260 MB each.

  ONNX Runtime is also told not to keep an allocation arena or plan tensor reuse.
  Both assume the tensor shapes repeat; pages are all different sizes, so the
  arena accumulates blocks it never reuses. That is the difference between 584 MB
  and 249 MB over a 40-page scan, for about 10% more time at one page worker and
  none at four (where it is 935 MB against 2010 MB). `Ocr(memory="fast")` or
  `--memory fast` buys the time back. Recognized text is identical either way.

- **`ocrust tiff` compresses by default** (LZW, understood by every TIFF reader
  since 1992). A 40-page colour archive was 442 MB and is now 3.7 MB, pixel for
  pixel identical. `--compression deflate` packs a little tighter,
  `--compression none` restores the old behaviour.

### Added

- **Tables.** A block whose cells line up into columns comes back as a table:
  `Page.tables`, `Document.tables`, `Block.table`, cells carrying their row,
  column, span, box and confidence, a Markdown pipe table from
  `render("markdown")`, and `Table.to_csv()` / `.as_rows()` / `.row_text()`. No
  table model and no ruling lines are read, so the wheel stays one
  self-contained artifact. Two things say where the cells are: the detector
  returns one box per cell when the columns are far enough apart, and the layout
  now keeps those boxes on the row it joins them into (`Line.segments`); inside a
  box, the recognizer's word positions show the gaps. A column is then a stretch
  of the page that row after row puts ink in.

  **How wide a gap has to be is measured rather than assumed**, because word
  spaces and column gutters are both gaps and their widths depend entirely on the
  document: a corpus receipt's word spaces run to 0.87 of the text height while
  its narrowest gutter is 2.5 times it, and a dense invoice has spaces at 0.66 and
  gutters from 0.83. No multiple of the text height sits between both pairs, so
  the threshold is twice the quarter-point of every word gap on the page. A page
  with no table has all its gaps within a factor of two of that point, so nothing
  is split and no table is found.

  Three guardrails keep prose out: three rows and two columns at least, gaps that
  fall in the same places row after row, and most rows carrying more than one
  cell. Over the 102-file evaluation corpus that finds 16 tables — the four ruled
  forms as 5x4, the four receipts as 8x2, the eight engineering drawings' title
  blocks as 5x2 — and nothing at all in the newspapers, letters, screenshots,
  faxes, clean pages or degraded scans. It does not read row spans, and a table
  whose columns are closer than twice its own word spacing is read as text.
  `Ocr(tables=False)` / `--no-tables` turns it off.

- **Batch ergonomics.** `--skip-existing` leaves inputs whose output file is
  already there, comparing against the file the run *would* write so a text pass
  does not make a later `-f markdown` pass think it is finished — a batch
  interrupted at file 300 of 400 picks up where it stopped. `-` reads one document
  from stdin, for `curl … | ocrust scan -`. `--watch` keeps running and scans
  whatever appears in the input directories, waiting until a file stops growing
  because half a PDF is not a PDF. And `ocrust completions bash|zsh|fish|powershell`
  prints a completion script generated from the argument parser itself, so it
  lists the flags this version has rather than the ones it had when someone last
  remembered; zsh and fish also get each flag's help text and the value sets for
  `--format`, `--compression` and the rest.

- **`Page.quality` and `Document.quality`: an estimate of how much of a document
  is right.** `confidence` answers a narrower question than people read into it.
  Over the evaluation corpus it spans 0.916 to 0.997 while the character error
  rate spans 0 to 0.206, and the worst page in the set is reported at 98.8% —
  above the median. That is not the recognizer being wrong: per line its
  confidence lands within 1.4% of the share of that line's text that is actually
  right. A page's error rate is simply made of what recognition never sees — text
  the detector missed, a column read out of order, a label broken into fragments.
  The new estimate is fitted from the shape of the output (characters per line,
  the tenth-percentile line confidence, mean line height relative to the page,
  mean confidence, the weakest line's margin) against 100 ground-truth files:
  **Spearman −0.75 against the error rate, where confidence manages −0.46**,
  better on 20 of 20 held-out splits, and it catches all 18 pages with a 10% or
  worse error rate when flagging below 0.96. `confidence` keeps its meaning and
  its callers — `drop_score` and `--min-confidence` still filter lines on the
  recognizer's own score. `Line.margin` exposes the runner-up distance the
  estimate is partly built from, and `scripts/collect_quality.py` with
  `scripts/fit_quality.py` re-fit the weights for another corpus. It is `None`
  for a page with fewer than five lines: every file it was fitted on has at
  least that many, and a two-line letter in large print reads to the features
  like a fragmented drawing, so the honest answer there is nothing at all.

- **A command line that reads like one.** Rust orange (`#F74C00`) is the accent —
  what was written, the `done:` total, the version, the language scripts — with
  green, amber and red kept for what they mean: how much a number can be trusted.
  Colour is used sparingly, switched off for a pipe (`NO_COLOR` honoured,
  `FORCE_COLOR` respected), and stepped down through 256 and 16 colours for
  terminals that cannot do 24-bit. A red `ocrust:` on errors, dim labels in
  `doctor`, `languages` and `models`, and long paths folded on their separators
  so a diagnostic never runs off the screen. Counts in English (`1 page, 2 lines`, not
  `1 page(s)`), durations that turn into seconds and minutes when they should,
  sizes in kB or MB, a `done:` total after a batch, a hint when `ocr` skipped
  every page because it already had text, long messages folded to the terminal
  width, and `--help` that ends with the seven commands people actually type.

### Fixed

- **GPU builds did not compile.** `--features cuda`, `coreml` or `directml`
  failed with *could not find `CUDAExecutionProvider` in `ep`*: the provider
  types are `ort::ep::CUDA`, `CoreML` and `DirectML` in the ONNX Runtime binding
  this release pins. Nothing in CI built them, so nothing noticed — the workflow
  now runs `cargo check --all-features`.
- **`tensorrt`, `rocm`, `openvino` and `webgpu` did nothing.** The features
  existed and pulled in the provider, but no code ever registered it. `device="auto"`
  now offers every provider the build was compiled with, TensorRT first because it
  falls back to CUDA by itself.
- **A page filter that matches nothing is an error.** `scan(pdf, pages=[99])` on a
  three-page document returned an empty document, which reads exactly like a blank
  scan; it now says *the document has no page 100*. An in-memory image honours
  `pages` as well, instead of ignoring the filter.
- **Word boxes follow the line.** Character positions were mapped onto the
  *bounding box* of the detection polygon, so on a tilted line every word got a
  box as tall as the whole line and offset along it. They are now interpolated
  along the quad's own edges. Vertical lines — whose crop the recognizer reads
  standing up — had their words spread left to right across the page instead of
  bottom to top, and a crop the orientation classifier turned around had them
  mirrored to the wrong end of the line.
- **Markdown no longer eats a minus sign.** A list item's leading `-` was trimmed
  unconditionally, so `- Gutschrift` and `-19,90 EUR` in one block turned a credit
  into a charge. A `-` counts as a bullet only when a space follows it, and the
  exporter and the block classifier now share one definition of what a bullet is.
- **Deskewing could exceed its own limit.** The coarse angle search stepped in
  whole degrees without checking them against `max_skew_deg`, so a limit below one
  degree still allowed a one-degree rotation.
- **Recursive glob patterns are expanded.** `ocrust scan 'archive/**/*.pdf'` and
  `scan_many(["archive/**/*.pdf"])` matched nothing, because the pattern was
  anchored at its parent directory (`archive/**`) and only its last component was
  globbed. Both now glob from the last directory that is spelled out.
- **`--pages 1-x` is a message, not a traceback**: a malformed page spec exits
  with *'1-x' is not a page or a page range* — and with code 2, which the
  documented table reserves for a bad argument, rather than 1.
- **A de-hyphenated paragraph is no longer a heading.** Joining a word across a
  line break unions the two line boxes, and the result read as one line of twice
  the page's median height: short hyphenated paragraphs came out of the Markdown
  exporter as `## `. The classifier now measures the detection polygon, which
  de-hyphenation does not touch.
- **An engine that cannot be built printed a Python traceback.** `ocrust scan x
  --lang klingon`, a missing model directory, an unknown device: the error was
  raised while building the engine, which no command guarded, so the user got a
  stack trace instead of a sentence. One handler in `main` reports it as one line.
- **`--progress` garbled a log.** It rewrote its line with a carriage return
  whatever stderr was, so a redirected run came out as one long line. On a
  terminal it still rewrites in place; into a pipe or a file it prints a line per
  page.
- **`-q` works on `ocr`, `pdf` and `tiff`**, not only `scan`; the wiki's own
  `find | xargs -P4 ocrust ocr` recipe had no way to silence the per-file
  summary. A `--sidecar` file is written through the same retrying path as every
  other output, so a network share hiccup no longer fails it alone.
- **`ocrust scan book.pdf | head -1` no longer ends in a traceback.** A reader
  that closes the pipe early is its own business; the CLI flushes stdout while it
  can still see the error and exits quietly instead of raising `BrokenPipeError`,
  returning 1 and printing *Exception ignored* on the way out. `Ctrl-C` during a
  long scan prints *interrupted* and exits 130 rather than a stack trace.
- **`ocrust languages` crashed on a Windows console.** A redirected stdout there
  is cp1252, and the command prints the Vietnamese characters the model is
  missing: `UnicodeEncodeError: 'charmap' codec can't encode character '\u1ea1'`.
  Both output streams are now UTF-8 with unrepresentable characters replaced, so
  a diagnostic can no longer take the command down.
- **`lang="zh_hant"` silently meant Simplified Chinese.** A tag written with an
  underscore is now the same tag as one written with a hyphen, so it no longer
  falls through to its primary subtag.
- **`pages` was ignored for numpy and PIL input.** `scan(array, pages=[7])` read
  the array as page 1 and reported success; the selection is now honoured there
  too, as is `progress`.
- **`Match.page` is the page's own index**, so a hit found while scanning a subset
  of a PDF reports the page number of the source document. `Document.to_dict()` no
  longer raises on a document built in Python rather than by the engine.
- A recognition model whose output has no character classes, or a row of NaNs, is
  reported as a model error instead of panicking inside the decoder. A batch that
  comes back the wrong size is an error instead of silently empty lines, and a
  class the dictionary does not cover no longer widens the character before it.
- **`ocrust install-models` failed wherever a `GITHUB_TOKEN` exists.** The token
  is sent to GitHub hosts so that a private model repository works — but a token
  that does not cover *this* repository makes `raw.githubusercontent.com` answer
  404 for a public file, which is every GitHub Actions job and many shells. An
  authenticated download that fails is now retried without the token; the
  authenticated error is still the one reported when both fail. Verified end to
  end: 31.7 MB fetched and checksum-verified.
- Model manifests are validated before anything is written: a bundle or file name
  has to be a plain name (a `../` in one would have written outside the model
  cache), the checksum has to be 64 hex digits, and a download stops at the size
  the manifest declares instead of reading a mirror until memory runs out.
  Concurrent installs no longer share one `.part` scratch file.

## 0.1.0

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

### Packaging and licensing

- **Apache-2.0 in the box.** The repository ships the license text and a NOTICE
  naming the bundled PP-OCR models, their upstream and their checksums, and both
  travel inside both wheels — a redistribution of someone else's Apache-2.0 work
  should say so where it lands, not only in a README.
- Project metadata points at this repository: homepage, source, documentation,
  changelog and issues, in `pyproject.toml` and in the crates.
- README images use absolute URLs, so the page renders on PyPI as well as on
  GitHub. `twine check` passes for both distributions.
- The release job publishes **two** projects, `ocrust` and `ocrust-models`, in
  separate steps: trusted publishing mints a token for one project at a time, so
  uploading them together would have been rejected on the first tag.

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
